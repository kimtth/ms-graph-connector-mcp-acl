import json
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from typing import Literal

from config import Settings
from pydantic import BaseModel, ConfigDict, Field

Status = Literal["open", "resolved"]


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=8000)
    status: Status


class Ticket(BaseModel):
    id: str
    title: str
    body: str
    readers: list[str]
    writers: list[str]
    status: Status = "open"
    version: int = 1


class Source:
    def __init__(self, settings: Settings):
        self.path = settings.database
        self.path.parent.mkdir(parents=True, exist_ok=True)
        users = [str(settings.alice_oid), str(settings.bob_oid), str(settings.eve_oid)]
        alice, bob, eve = users
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (users TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, at TEXT, ticket TEXT, actor TEXT,
                    action TEXT, detail TEXT
                );
            """)
            saved = db.execute("SELECT users FROM config").fetchone()
            if saved:
                if json.loads(saved[0]) != users:
                    raise ValueError("Database belongs to different users; use a new database path")
                return
            db.execute("INSERT INTO config VALUES (?)", (json.dumps(users),))
            for tid, title, readers, writers in [
                ("T100", "Employee onboarding", users, [alice]),
                ("T200", "VPN outage", [alice, bob], [alice]),
                ("T300", "Payroll integration", [eve], [eve]),
                ("T400", "Private escalation", [alice], [alice]),
            ]:
                ticket = Ticket(
                    id=tid,
                    title=title,
                    body=f"Service desk: {title}.",
                    readers=readers,
                    writers=writers,
                )
                db.execute("INSERT INTO tickets VALUES (?,?)", (tid, ticket.model_dump_json()))

    @contextmanager
    def db(self) -> Generator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def tickets(self) -> list[Ticket]:
        with self.db() as db:
            return [
                Ticket.model_validate_json(r[0])
                for r in db.execute("SELECT data FROM tickets ORDER BY id")
            ]

    def fetch(self, user: str, ticket_id: str) -> Ticket:
        ticket = next((t for t in self.tickets() if t.id == ticket_id and user in t.readers), None)
        if ticket is None:
            raise PermissionError("Record not found or access denied")
        return ticket

    @staticmethod
    def view(ticket: Ticket, user: str) -> dict:
        return {**ticket.model_dump(), "can_write": user in ticket.writers}

    @staticmethod
    def log(db: sqlite3.Connection, user: str, tid: str, action: str, detail: dict) -> int:
        cursor = db.execute(
            "INSERT INTO audit (at,ticket,actor,action,detail) VALUES (?,?,?,?,?)",
            (datetime.now(UTC).isoformat(), tid, user, action, json.dumps(detail)),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def update(self, user: str, ticket_id: str, change: Change) -> tuple[Ticket, int]:
        denied = False
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if row is None:
                raise PermissionError("Record not found or access denied")
            ticket = Ticket.model_validate_json(row[0])
            if user not in ticket.readers or user not in ticket.writers:
                self.log(db, user, ticket_id, "denied", {})
                denied = True
            else:
                if ticket.version != change.expected_version:
                    raise ValueError("Version conflict: fetch again before updating")
                updated = ticket.model_copy(
                    update={
                        "body": change.body,
                        "status": change.status,
                        "version": ticket.version + 1,
                    }
                )
                db.execute(
                    "UPDATE tickets SET data=? WHERE id=?", (updated.model_dump_json(), ticket_id)
                )
                audit_id = self.log(
                    db,
                    user,
                    ticket_id,
                    "updated",
                    {"before": ticket.model_dump(), "after": updated.model_dump()},
                )
        if denied:
            raise PermissionError("Enterprise system denied write permission")
        return updated, audit_id

    def audit(self, user: str) -> list[dict]:
        visible = {t.id for t in self.tickets() if user in t.readers}
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM audit ORDER BY id DESC")
                if r["ticket"] in visible
            ]
