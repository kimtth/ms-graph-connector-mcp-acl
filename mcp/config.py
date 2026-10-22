from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEMO_", env_file=ROOT / ".env", extra="ignore")

    tenant_id: UUID
    api_client_id: UUID
    spa_client_id: UUID
    ingest_client_id: UUID
    ingest_client_secret: str
    alice_oid: UUID
    bob_oid: UUID
    eve_oid: UUID
    public_url: str
    connection_id: str = "helpdeskdemo"
    database: Path = ROOT / "data" / "tickets.db"

    @model_validator(mode="after")
    def validate_demo(self) -> Self:
        from urllib.parse import urlparse

        url = urlparse(self.public_url)
        if url.scheme != "https" or not url.hostname or url.path not in ("", "/"):
            raise ValueError("PUBLIC_URL must be an HTTPS origin")
        if url.query or url.fragment or url.username:
            raise ValueError("PUBLIC_URL cannot contain credentials, a query, or a fragment")
        if not self.connection_id.isalnum() or not 3 <= len(self.connection_id) <= 32:
            raise ValueError("CONNECTION_ID must be 3-32 alphanumeric characters")
        if len({self.alice_oid, self.bob_oid, self.eve_oid}) != 3:
            raise ValueError("Use three distinct test users")
        return self
