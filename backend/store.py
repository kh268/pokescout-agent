"""SQLite records, idempotency and job events share one transaction boundary."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from collection import Collection


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False,
                 resource_url: str | None = None) -> None:
        self.status = status
        self.error = {"code": code, "message": message, "retryable": retryable,
                      "field_errors": [], "resource_url": resource_url}


class Store:
    def __init__(self, root: Path) -> None:
        self.on_job_event: Callable[[sqlite3.Connection, str, dict, str], None] | None = None
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "collection.sqlite3"
        collection = Collection(self.path)
        collection.close()
        with closing(self.connect()) as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS api_sessions(
                id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS api_records(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                created TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS api_owner_kind ON api_records(owner,kind,created,id);
            CREATE TABLE IF NOT EXISTS api_idempotency(
                owner TEXT, route TEXT, key TEXT, hash TEXT NOT NULL,
                status INTEGER NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(owner,route,key));
            CREATE TABLE IF NOT EXISTS api_confirmations(
                candidate TEXT PRIMARY KEY, copy_id TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS api_reservations(
                intent TEXT PRIMARY KEY, owner TEXT NOT NULL, total INTEGER NOT NULL,
                state TEXT NOT NULL);
            ''')

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, db: sqlite3.Connection, owner: str, kind: str, key: str) -> dict:
        row = db.execute("SELECT data FROM api_records WHERE id=? AND owner=? AND kind=?",
                         (key, owner, kind)).fetchone()
        if row is None:
            raise APIError(404, "NOT_FOUND", "Record not found.")
        return json.loads(row["data"])

    def put(self, db: sqlite3.Connection, owner: str, kind: str, key: str, data: dict) -> None:
        existing = db.execute("SELECT owner,kind FROM api_records WHERE id=?", (key,)).fetchone()
        if existing is not None and (existing["owner"] != owner or existing["kind"] != kind):
            raise APIError(409, "INVALID_STATE", "Record identifier is already assigned.")
        db.execute("INSERT INTO api_records VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                   (key, owner, kind, data.get("created_at", now()), encoded(data)))

    def rows(self, db: sqlite3.Connection, owner: str, kind: str) -> list[dict]:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM api_records WHERE owner=? AND kind=? ORDER BY created,id", (owner, kind))]

    def event(self, db: sqlite3.Connection, owner: str, job: dict, event_type: str) -> None:
        # The separately owned activity service can append using THIS transaction.
        if self.on_job_event is not None:
            self.on_job_event(db, owner, job, event_type)

    def save_job(self, db: sqlite3.Connection, owner: str, job: dict, state: str) -> None:
        job["state"] = state
        job["revision"] += 1
        job["updated_at"] = now()
        if state in {"succeeded", "failed", "cancelled"}:
            job["stage"] = "finished"
        self.put(db, owner, "job", job["job_id"], job)
        suffix = {"running": "started", "awaiting_input": "awaiting_input"}.get(state, state)
        self.event(db, owner, job, "job." + suffix)
