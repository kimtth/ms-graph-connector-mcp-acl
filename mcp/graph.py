import asyncio
import time
from typing import Any

import httpx
from config import Settings
from source import Ticket

BASE = "https://graph.microsoft.com/v1.0"


class Graph:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def token(self, http: httpx.AsyncClient) -> str:
        s = self.settings
        response = await http.post(
            f"https://login.microsoftonline.com/{s.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": str(s.ingest_client_id),
                "client_secret": s.ingest_client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"Indexer authentication failed: HTTP {response.status_code}")
        return response.json()["access_token"]

    @staticmethod
    async def request(
        http: httpx.AsyncClient,
        token: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        allowed: tuple[int, ...] = (200, 201, 202, 204),
    ) -> httpx.Response:
        response = await http.request(
            method, BASE + path, json=payload, headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code not in allowed:
            raise RuntimeError(
                f"Graph HTTP {response.status_code}; request-id="
                f"{response.headers.get('request-id', 'unknown')}; "
                f"Retry-After={response.headers.get('retry-after', 'unspecified')}"
            )
        return response

    async def index(self, tickets: list[Ticket]) -> None:
        s = self.settings
        async with httpx.AsyncClient(timeout=30) as http:
            token = await self.token(http)
            for ticket in tickets:
                await self.request(
                    http,
                    token,
                    "PUT",
                    f"/external/connections/{s.connection_id}/items/{ticket.id}",
                    {
                        "acl": [
                            {"type": "user", "value": oid, "accessType": "grant"}
                            for oid in ticket.readers
                        ],
                        "properties": {
                            "ticketId": ticket.id,
                            "title": ticket.title,
                            "body": ticket.body,
                            "status": ticket.status,
                            "version": ticket.version,
                            "url": f"{s.public_url.rstrip('/')}/?ticket={ticket.id}",
                        },
                        "content": {"type": "text", "value": f"{ticket.title}\n{ticket.body}"},
                    },
                )

    async def provision(self) -> None:
        path = f"/external/connections/{self.settings.connection_id}"
        async with httpx.AsyncClient(timeout=30) as http:
            token = await self.token(http)
            existing = await self.request(http, token, "GET", path, allowed=(200, 404))
            if existing.status_code == 404:
                await self.request(
                    http,
                    token,
                    "POST",
                    "/external/connections",
                    {
                        "id": self.settings.connection_id,
                        "name": "Demo service desk",
                        "description": "Ticket search with user ACLs and MCP write-back",
                    },
                )
            properties = [
                {
                    "name": name,
                    "type": "int64" if name == "version" else "string",
                    "isRetrievable": True,
                    **({"isSearchable": True} if name in {"title", "body"} else {}),
                    **({"labels": [name]} if name in {"title", "url"} else {}),
                }
                for name in ("ticketId", "title", "body", "status", "version", "url")
            ]
            response = await self.request(
                http,
                token,
                "PATCH",
                path + "/schema",
                {"baseType": "microsoft.graph.externalItem", "properties": properties},
            )
            if response.status_code != 202:
                return
            location = response.headers["Location"]
            if not location.startswith(BASE + "/"):
                raise RuntimeError("Unexpected schema operation URL")
            deadline = time.monotonic() + 1200
            while time.monotonic() < deadline:
                result = await self.request(http, token, "GET", location[len(BASE) :])
                state = result.json()["status"]
                if state == "completed":
                    return
                if state == "failed":
                    raise RuntimeError("Graph schema registration failed")
                await asyncio.sleep(5)
            raise TimeoutError(
                "Schema registration exceeded 20 minutes; check Graph before retrying"
            )
