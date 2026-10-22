import argparse
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
import jwt
import uvicorn
from config import ROOT, Settings
from graph import Graph
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from source import Change, Source, Status
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

log = logging.getLogger(__name__)


def create_app(s: Settings) -> Starlette:
    source, graph = Source(s), Graph(s)
    writes = asyncio.Lock()
    metadata_path = "/.well-known/oauth-protected-resource/mcp"
    keys = jwt.PyJWKClient(
        f"https://login.microsoftonline.com/{s.tenant_id}/discovery/v2.0/keys", timeout=10
    )

    def verify(token: str) -> dict[str, str]:
        claims = jwt.decode(
            token,
            keys.get_signing_key_from_jwt(token).key,
            algorithms=["RS256"],
            audience=str(s.api_client_id),
            issuer=f"https://login.microsoftonline.com/{s.tenant_id}/v2.0",
            options={"require": ["exp", "iat", "oid", "tid"]},
        )
        if (
            claims["tid"] != str(s.tenant_id)
            or "Tickets.Access" not in claims.get("scp", "").split()
        ):
            raise jwt.InvalidTokenError("Wrong tenant or missing delegated Tickets.Access scope")
        return {"oid": claims["oid"], "name": claims.get("name", claims["oid"])}

    class Authenticate:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            public = {"/", "/config", "/health", metadata_path}
            if scope["type"] == "http" and scope["path"] not in public:
                request = Request(scope)
                try:
                    auth = request.headers.get("authorization", "")
                    if not auth.startswith("Bearer "):
                        raise jwt.InvalidTokenError("Bearer token required")
                    scope.setdefault("state", {})["user"] = await asyncio.to_thread(
                        verify, auth[7:]
                    )
                except jwt.PyJWTError as exc:
                    log.warning("Authentication rejected: %s", type(exc).__name__)
                    await JSONResponse(
                        {"error": "Invalid or missing delegated access token"},
                        401,
                        headers={
                            "WWW-Authenticate": (
                                f'Bearer resource_metadata="{s.public_url.rstrip("/")}'
                                f'{metadata_path}"'
                            )
                        },
                    )(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    mcp = FastMCP(
        "Service desk",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                urlparse(s.public_url).netloc,
                "localhost:*",
                "127.0.0.1:*",
                "testserver",
            ],
            allowed_origins=[s.public_url.rstrip("/"), "http://localhost:*", "http://127.0.0.1:*"],
        ),
    )

    def user(ctx: Context) -> str:
        request = ctx.request_context.request
        if not isinstance(request, Request):
            raise PermissionError("Authenticated HTTP request required")
        return request.state.user["oid"]

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def search_tickets(
        query: Annotated[str, Field(min_length=1, max_length=300)], ctx: Context
    ) -> dict[str, Any]:
        """Search current source records allowed by the signed-in user's read ACL."""
        oid = user(ctx)
        return {
            "tickets": [
                source.view(t, oid)
                for t in source.tickets()
                if oid in t.readers and query.casefold() in f"{t.title} {t.body}".casefold()
            ]
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def fetch_ticket(ticket_id: str, ctx: Context) -> dict[str, Any]:
        """Fetch a readable source record and its current version before editing."""
        oid = user(ctx)
        return source.view(source.fetch(oid, ticket_id), oid)

    @mcp.tool(
        structured_output=False,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    )
    async def update_ticket(
        ticket_id: str, expected_version: int, body: str, status: Status, ctx: Context
    ) -> CallToolResult:
        """After explicit user approval, update a ticket and submit it to Graph.

        Source read/write ACLs and version are checked independently of search permissions.
        On index failure, the source change remains committed; report it and run sync to retry.
        Graph acceptance does not mean the change is already visible in search.
        """
        oid = user(ctx)
        async with writes:
            ticket, audit_id = source.update(
                oid, ticket_id, Change(expected_version=expected_version, body=body, status=status)
            )
            result = {
                "ticket": source.view(ticket, oid),
                "audit_id": audit_id,
                "source_update": "committed",
                "index_update": "submitted",
            }
            try:
                await graph.index([ticket])
            except (httpx.HTTPError, RuntimeError) as exc:
                log.error("Source updated but indexing failed for %s: %s", ticket_id, exc)
                result.update(index_update="failed", error=str(exc))
        return CallToolResult(
            isError=result["index_update"] == "failed",
            structuredContent=result,
            content=[TextContent(type="text", text=json.dumps(result))],
        )

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    async def home(request):
        return FileResponse(ROOT / "mcp" / "web" / "index.html")

    async def health(request):
        return JSONResponse({"status": "ok"})

    async def config(request):
        return JSONResponse(
            {
                "tenant": str(s.tenant_id),
                "client": str(s.spa_client_id),
                "scope": f"api://{s.api_client_id}/Tickets.Access",
                "connection": s.connection_id,
            }
        )

    async def me(request):
        return JSONResponse(request.state.user)

    async def audit(request):
        return JSONResponse(source.audit(request.state.user["oid"]))

    async def metadata(request):
        return JSONResponse(
            {
                "resource": f"{s.public_url.rstrip('/')}/mcp/",
                "authorization_servers": [f"https://login.microsoftonline.com/{s.tenant_id}/v2.0"],
                "scopes_supported": ["Tickets.Access"],
            }
        )

    app = Starlette(
        routes=[
            Route("/", home),
            Route("/config", config),
            Route("/health", health),
            Route("/me", me),
            Route("/audit", audit),
            Route(metadata_path, metadata),
            Mount("/mcp", app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(Authenticate)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Service desk Graph + MCP demo")
    parser.add_argument("command", choices=["serve", "init", "sync"], nargs="?", default="serve")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    if args.command == "serve":
        if not (ROOT / "mcp" / "web" / "index.html").is_file():
            parser.error("Run npm ci and npm run build first")
        uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, proxy_headers=False)
    elif args.command == "init":
        asyncio.run(Graph(settings).provision())
        print("Graph connection/schema ready. Run: uv run python mcp\\server.py sync")
    else:
        asyncio.run(Graph(settings).index(Source(settings).tickets()))
        print("Records submitted. Verify search visibility under each real user.")


if __name__ == "__main__":
    main()
