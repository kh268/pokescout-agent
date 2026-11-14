"""Durable central activity journal and SSE router. No agent execution or trading."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, Iterator, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from activity import ActivityContext, DEFAULT_PUBLISH_URL

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "collection.sqlite3"
TERMINAL = {"succeeded", "failed", "cancelled"}
AGENT_TYPES = {"step.started", "step.progress", "step.completed", "step.failed", "finding.observed", "artifact.created", "activity.warning"}
TRANSITIONS = {"queued": {"running", "cancel_requested", "failed"},
               "running": {"succeeded", "failed", "awaiting_input", "cancel_requested"},
               "awaiting_input": {"queued", "cancel_requested", "failed"},
               "cancel_requested": {"cancelled", "succeeded", "failed"}}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def envelope(data: object) -> dict:
    return {"api_version": "1.0", "request_id": "request_" + uuid.uuid4().hex, "data": data}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Progress(StrictModel):
    completed: int = Field(ge=0)
    total: int | None
    unit: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def check_total(self) -> Progress:
        if self.total is not None and (self.total < 0 or self.completed > self.total):
            raise ValueError("Invalid progress total")
        return self


class Reference(StrictModel):
    kind: Literal["listing", "research_report", "decision", "draft", "intent", "candidate", "evidence"]
    id: str | None = Field(default=None, max_length=160)
    url: str | None = Field(default=None, max_length=2000)
    label: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def safe_url(self) -> Reference:
        if self.url:
            p = urlsplit(self.url)
            if self.url.startswith("/api/v1/") and not p.netloc and not p.scheme and not p.query and not p.fragment:
                return self
            if p.scheme != "https" or p.username or p.password or p.query or p.fragment or p.port not in (None, 443):
                raise ValueError("Unsafe activity reference URL")
            paths = {"jp.mercari.com": r"/item/m\d+", "item.fril.jp": r"/[a-fA-F0-9]{20,64}"}
            if not re.fullmatch(paths.get(p.hostname, r"(?!)"), p.path.rstrip("/")):
                raise ValueError("Unsupported listing reference URL")
        return self


class PublicData(StrictModel):
    provider: str | None = Field(default=None, max_length=80)
    progress: Progress | None = None
    counts: dict[str, int] = Field(default_factory=dict, max_length=20)
    references: list[Reference] = Field(default_factory=list, max_length=20)
    result_status: str | None = Field(default=None, max_length=80)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    job: dict | None = None

    @model_validator(mode="after")
    def valid_counts(self) -> PublicData:
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", k) or type(v) is not int or v < 0 for k, v in self.counts.items()):
            raise ValueError("Invalid activity counters")
        return self


class Event(StrictModel):
    schema_version: Literal["1.0"]
    event_id: str
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    type: str
    operation: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", max_length=80)
    span_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")
    attempt: int = Field(ge=1, le=1000)
    occurred_at: str | None
    level: Literal["info", "warning", "error"]
    message: str = Field(min_length=1, max_length=500)
    data: PublicData

    @model_validator(mode="after")
    def safe_payload(self) -> Event:
        uuid.UUID(self.event_id)
        if self.occurred_at and datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00")).utcoffset() is None:
            raise ValueError("Timestamp requires timezone")
        text = self.model_dump_json()
        if re.search(r"tvly-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]+|AQ\.[A-Za-z0-9_-]{20,}|Bearer\s+\S+|[A-Z]:\\|/(?:Users|home|tmp)/", text):
            raise ValueError("Private data must not enter public activities")
        if any(value in text for name, value in os.environ.items() if value and len(value) > 8 and
               any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET"))):
            raise ValueError("Private data must not enter public activities")
        return self


class CursorError(ValueError):
    def __init__(self, reason: str, high: str) -> None:
        self.reason, self.high = reason, high


class ActivityStore:
    """Additive SQLite tables; each operation closes its connection on Windows too."""

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS activity_job_scopes(
                    job_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, state TEXT NOT NULL,
                    revision INTEGER NOT NULL, stage TEXT NOT NULL, expires REAL NOT NULL, finished REAL);
                CREATE TABLE IF NOT EXISTS activity_sessions(
                    token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS activity_publishers(
                    token_hash TEXT PRIMARY KEY, job_id TEXT NOT NULL, producer_id TEXT NOT NULL,
                    operations TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS activity_rates(
                    job_id TEXT NOT NULL, producer_id TEXT NOT NULL, tokens REAL NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(job_id,producer_id));
                CREATE TABLE IF NOT EXISTS activity_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, producer_id TEXT NOT NULL,
                    event_id TEXT NOT NULL, payload_hash TEXT NOT NULL, recorded REAL NOT NULL, event TEXT NOT NULL,
                    UNIQUE(job_id,producer_id,event_id));
                CREATE INDEX IF NOT EXISTS activity_job_cursor ON activity_events(job_id,seq);
                CREATE TABLE IF NOT EXISTS activity_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
                INSERT OR IGNORE INTO activity_meta VALUES('floor',0);
            ''')

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def create_session(self, owner: str, ttl: int = 86400) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute("INSERT INTO activity_sessions VALUES(?,?,?,?)", (digest(token), owner, csrf, time.time() + ttl))
        return token, csrf

    def session_owner(self, token: str | None) -> str:
        with self.connect() as db:
            row = db.execute("SELECT owner_id FROM activity_sessions WHERE token_hash=? AND expires>?", (digest(token or ""), time.time())).fetchone()
        if not row:
            raise HTTPException(401, "AUTH_REQUIRED")
        return row[0]

    def revoke_session(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM activity_sessions WHERE token_hash=?", (digest(token),))

    def require_job(self, owner: str, job_id: str) -> None:
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM activity_job_scopes WHERE owner_id=? AND job_id=?", (owner, job_id)).fetchone()
        if not row:
            raise HTTPException(404, "NOT_FOUND")

    def register_job(self, owner: str, job_id: str, *, ttl: int = 180,
                     persist: Callable[[sqlite3.Connection], None] | None = None) -> None:
        """Trusted backend hook: create its real job and activity scope in one transaction."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", job_id) or not 1 <= ttl <= 86400:
            raise ValueError("Invalid job scope")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if persist:
                    persist(db)
                db.execute("INSERT INTO activity_job_scopes VALUES(?,?, 'queued',1,'queued',?,NULL)", (job_id, owner, time.time() + ttl))
                self._job_event(db, job_id, "job.queued", "Job queued.")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def backend_job_event(self, db: sqlite3.Connection, owner: str, job: dict, event_type: str) -> None:
        """Store.on_job_event adapter: use the caller's existing transaction."""
        existing = db.execute("SELECT owner_id FROM activity_job_scopes WHERE job_id=?", (job["job_id"],)).fetchone()
        if existing and existing[0] != owner:
            raise ValueError("Job owner cannot change")
        db.execute("""INSERT INTO activity_job_scopes VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,revision=excluded.revision,
                stage=excluded.stage,expires=excluded.expires,finished=excluded.finished""",
            (job["job_id"], owner, job["state"], job["revision"], job["stage"], time.time() + 86400,
             time.time() if job["state"] in TERMINAL else None))
        self._job_event(db, job["job_id"], event_type, "Job " + job["state"].replace("_", " ") + ".")

    def transition_job(self, job_id: str, state: str, stage: str, *, expected_revision: int,
                       persist: Callable[[sqlite3.Connection], None] | None = None) -> None:
        """Trusted hook; real application state and its lifecycle event commit atomically."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM activity_job_scopes WHERE job_id=?", (job_id,)).fetchone()
                if not row or row["revision"] != expected_revision or state not in TRANSITIONS.get(row["state"], set()):
                    raise ValueError("Invalid or stale job transition")
                if persist:
                    persist(db)
                db.execute("UPDATE activity_job_scopes SET state=?,stage=?,revision=revision+1,finished=? WHERE job_id=?",
                           (state, stage, time.time() if state in TERMINAL else None, job_id))
                event_type = {"running": "job.started", "queued": "job.resumed"}.get(state, "job." + state)
                self._job_event(db, job_id, event_type, "Job " + state.replace("_", " ") + ".")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _job_event(self, db: sqlite3.Connection, job_id: str, event_type: str, message: str) -> None:
        row = db.execute("SELECT state,revision,stage FROM activity_job_scopes WHERE job_id=?", (job_id,)).fetchone()
        event = Event(schema_version="1.0", event_id=str(uuid.uuid4()), job_id=job_id, type=event_type,
                      operation="job.lifecycle", span_id=job_id, parent_span_id=None, attempt=1,
                      occurred_at=now(), level="error" if event_type == "job.failed" else "info", message=message,
                      data=PublicData(job=dict(row)))
        self._insert(db, "application", event.model_dump())

    def issue_context(self, job_id: str, producer_id: str, operations: list[str], *,
                      publish_url: str = DEFAULT_PUBLISH_URL, parent_span_id: str | None = None) -> ActivityContext:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", producer_id):
            raise ValueError("Invalid producer ID")
        if not operations or any(not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*(?:\.\*)?", op) for op in operations):
            raise ValueError("Invalid producer operations")
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            row = db.execute("SELECT * FROM activity_job_scopes WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["state"] in TERMINAL or row["expires"] < time.time():
                raise ValueError("Job is missing, expired or terminal")
            db.execute("INSERT INTO activity_publishers VALUES(?,?,?,?,?)", (digest(token), job_id, producer_id, json.dumps(operations), row["expires"] + 60))
        return ActivityContext(job_id, producer_id, token, publish_url, parent_span_id)

    def _insert(self, db: sqlite3.Connection, producer: str, payload: dict) -> tuple[str, dict]:
        timestamp = time.time()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        public = {**payload, "producer_id": producer, "recorded_at": now()}
        cursor = db.execute("INSERT INTO activity_events(job_id,producer_id,event_id,payload_hash,recorded,event) VALUES(?,?,?,?,?,?)",
                            (payload["job_id"], producer, payload["event_id"], digest(canonical), timestamp, json.dumps(public, ensure_ascii=False))).lastrowid
        return str(cursor), public

    def append(self, token: str, event: Event) -> tuple[dict, bool]:
        payload = event.model_dump()
        if token and token in json.dumps(payload):
            raise HTTPException(422, "PRIVATE_ACTIVITY_DATA")
        if len(json.dumps(payload).encode()) > 16384:
            raise HTTPException(413, "BODY_TOO_LARGE")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                producer = db.execute("SELECT * FROM activity_publishers WHERE token_hash=? AND expires>?", (digest(token), time.time())).fetchone()
                if not producer:
                    raise HTTPException(403, "PUBLISHER_INVALID")
                if producer["job_id"] != event.job_id or event.type not in AGENT_TYPES or event.data.job is not None:
                    raise HTTPException(403, "PUBLISHER_SCOPE_INVALID")
                operations = json.loads(producer["operations"])
                if not any(event.operation == op or op.endswith(".*") and event.operation.startswith(op[:-1]) for op in operations):
                    raise HTTPException(403, "PUBLISHER_SCOPE_INVALID")
                old = db.execute("SELECT * FROM activity_events WHERE job_id=? AND producer_id=? AND event_id=?",
                                 (event.job_id, producer["producer_id"], event.event_id)).fetchone()
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                if old:
                    if old["payload_hash"] != digest(canonical):
                        raise HTTPException(409, "EVENT_ID_CONFLICT")
                    public = json.loads(old["event"])
                    receipt = {"event_id": event.event_id, "cursor": str(old["seq"]), "recorded_at": public["recorded_at"],
                               "producer_id": producer["producer_id"], "duplicate": True}
                    db.execute("COMMIT")
                    return receipt, True
                job = db.execute("SELECT * FROM activity_job_scopes WHERE job_id=?", (event.job_id,)).fetchone()
                if job["state"] in TERMINAL:
                    raise HTTPException(409, "JOB_ALREADY_TERMINAL")
                if job["expires"] < time.time():
                    raise HTTPException(409, "JOB_EXPIRED")
                count = db.execute("SELECT COUNT(*) FROM activity_events WHERE job_id=? AND producer_id!='application'", (event.job_id,)).fetchone()[0]
                if count >= 10000:
                    raise HTTPException(429, "ACTIVITY_LIMIT", headers={"Retry-After": "1"})
                stamp = time.time()
                rate = db.execute("SELECT * FROM activity_rates WHERE job_id=? AND producer_id=?", (event.job_id, producer["producer_id"])).fetchone()
                tokens = min(20.0, rate["tokens"] + max(0, stamp - rate["updated"]) * 10) if rate else 20.0
                if tokens < 1:
                    raise HTTPException(429, "ACTIVITY_RATE_LIMIT", headers={"Retry-After": "1"})
                db.execute("INSERT INTO activity_rates VALUES(?,?,?,?) ON CONFLICT(job_id,producer_id) DO UPDATE SET tokens=excluded.tokens,updated=excluded.updated",
                           (event.job_id, producer["producer_id"], tokens - 1, stamp))
                cursor, public = self._insert(db, producer["producer_id"], payload)
                db.execute("COMMIT")
                return {"event_id": event.event_id, "cursor": cursor, "recorded_at": public["recorded_at"],
                        "producer_id": producer["producer_id"], "duplicate": False}, False
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def cursor(self, owner: str, value: str | None) -> int:
        with self.connect() as db:
            high = db.execute("SELECT COALESCE(MAX(e.seq),0) FROM activity_events e JOIN activity_job_scopes j ON j.job_id=e.job_id WHERE j.owner_id=?", (owner,)).fetchone()[0]
            global_high = db.execute("SELECT COALESCE(MAX(seq),0) FROM activity_events").fetchone()[0]
            floor = db.execute("SELECT value FROM activity_meta WHERE key='floor'").fetchone()[0]
        high = max(high, floor)
        if value is None:
            return floor
        if not re.fullmatch(r"\d{1,18}", value):
            raise CursorError("cursor_invalid", str(high))
        number = int(value)
        if number < floor:
            raise CursorError("cursor_expired", str(high))
        if number > max(global_high, floor):
            raise CursorError("cursor_ahead", str(high))
        return number

    def read(self, owner: str, after: int, job_id: str | None = None, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT e.seq,e.event FROM activity_events e JOIN activity_job_scopes j ON j.job_id=e.job_id WHERE j.owner_id=? AND e.seq>? AND (? IS NULL OR e.job_id=?) ORDER BY e.seq LIMIT ?",
                              (owner, after, job_id, job_id, limit)).fetchall()
        return [{**json.loads(row["event"]), "cursor": str(row["seq"])} for row in rows]

    def prune(self, age_seconds: int = 7 * 86400) -> int:
        cutoff = time.time() - age_seconds
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            first_kept = db.execute("SELECT MIN(e.seq) FROM activity_events e JOIN activity_job_scopes j ON j.job_id=e.job_id WHERE j.finished IS NULL OR j.finished>?", (cutoff,)).fetchone()[0]
            floor = (first_kept - 1) if first_kept else db.execute("SELECT COALESCE(MAX(seq),0) FROM activity_events").fetchone()[0]
            floor = max(floor, db.execute("SELECT value FROM activity_meta WHERE key='floor'").fetchone()[0])
            deleted = db.execute("DELETE FROM activity_events WHERE seq<=?", (floor,)).rowcount
            db.execute("UPDATE activity_meta SET value=? WHERE key='floor'", (floor,))
            db.execute("COMMIT")
        return deleted


class TimedStreamingResponse(StreamingResponse):
    async def stream_response(self, send: Callable) -> None:
        async def bounded(message: dict) -> None:
            async with asyncio.timeout(10):
                await send(message)
        await super().stream_response(bounded)


def activity_router(store: ActivityStore, owner_for_request: Callable[[Request], str], *,
                    heartbeat_seconds: float = 15, poll_seconds: float = 0.25) -> APIRouter:
    """Mount in the real application with its existing session ownership resolver."""
    router = APIRouter()

    @router.post("/internal/v1/activity/events")
    async def publish(request: Request) -> JSONResponse:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise HTTPException(413, "BODY_TOO_LARGE")
        try:
            event = Event.model_validate_json(bytes(body))
        except (ValueError, ValidationError):
            raise HTTPException(422, "INVALID_ACTIVITY") from None
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(403, "PUBLISHER_INVALID")
        receipt, duplicate = await asyncio.to_thread(store.append, authorization[7:], event)
        return JSONResponse(envelope(receipt), status_code=200 if duplicate else 201)

    @router.get("/api/v1/activity")
    async def history(request: Request, after: str | None = None, job_id: str | None = None, limit: int = 100) -> dict:
        owner = owner_for_request(request)
        if not 1 <= limit <= 200:
            raise HTTPException(422, "INVALID_LIMIT")
        if job_id:
            store.require_job(owner, job_id)
        try:
            cursor = store.cursor(owner, after)
        except CursorError as exc:
            raise HTTPException(409, {"code": exc.reason, "resume_after": exc.high}) from None
        items = await asyncio.to_thread(store.read, owner, cursor, job_id, limit + 1)
        return envelope({"items": items[:limit], "next_cursor": items[min(len(items), limit) - 1]["cursor"] if items else str(cursor), "has_more": len(items) > limit})

    @router.get("/api/v1/activity/stream")
    async def stream(request: Request, after: str | None = None, job_id: str | None = None) -> StreamingResponse:
        owner = owner_for_request(request)
        if job_id:
            store.require_job(owner, job_id)
        requested = request.headers.get("last-event-id", after)

        async def frames() -> AsyncIterator[str]:
            try:
                cursor = store.cursor(owner, requested)
            except CursorError as exc:
                yield "event: stream.reset\ndata: " + json.dumps({"reason": exc.reason, "resume_after": exc.high,
                    "history_url": "/api/v1/activity"}) + "\n\n"
                return
            yield "retry: 2000\n\n"
            last_sent = time.monotonic()
            while not await request.is_disconnected():
                try:
                    if owner_for_request(request) != owner:
                        return
                except HTTPException:
                    return
                try:
                    store.cursor(owner, str(cursor))
                except CursorError as exc:
                    yield "event: stream.reset\ndata: " + json.dumps({"reason": exc.reason, "resume_after": exc.high,
                        "history_url": "/api/v1/activity"}) + "\n\n"
                    return
                items = await asyncio.to_thread(store.read, owner, cursor, job_id, 100)
                for item in items:
                    yield "id: " + item["cursor"] + "\nevent: activity\ndata: " + json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n\n"
                    cursor = int(item["cursor"])
                    last_sent = time.monotonic()
                if not items:
                    if time.monotonic() - last_sent >= heartbeat_seconds:
                        yield ": heartbeat\n\n"
                        last_sent = time.monotonic()
                    await asyncio.sleep(poll_seconds)
        return TimedStreamingResponse(frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    return router


def create_app(path: Path | str = DEFAULT_DB, *, heartbeat_seconds: float = 15,
               poll_seconds: float = 0.25) -> FastAPI:
    """Loopback-only single-user standalone host; production mounts the router instead."""
    store = ActivityStore(path)
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def cleanup() -> None:
            while True:
                await asyncio.to_thread(store.prune)
                await asyncio.sleep(300)
        task = asyncio.create_task(cleanup())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    app = FastAPI(title="PokeScout central activity service", lifespan=lifespan)
    app.state.activity_store = store
    resolve_owner = lambda request: store.session_owner(request.cookies.get("pokescout_session"))
    app.include_router(activity_router(store, resolve_owner, heartbeat_seconds=heartbeat_seconds, poll_seconds=poll_seconds))

    @app.exception_handler(HTTPException)
    async def error(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        body = {"api_version": "1.0", "request_id": "request_" + uuid.uuid4().hex,
                "error": {"code": detail if isinstance(detail, str) else detail.get("code", "INVALID_REQUEST"),
                          "message": "Activity request could not be completed.", "retryable": exc.status_code in (429, 503),
                          "field_errors": [], "resource_url": None}}
        if isinstance(detail, dict):
            body["error"].update(detail)
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.get("/api/v1/session")
    async def session(request: Request) -> JSONResponse:
        existing = request.cookies.get("pokescout_session")
        try:
            store.session_owner(existing)
            with store.connect() as db:
                csrf = db.execute("SELECT csrf FROM activity_sessions WHERE token_hash=?", (digest(existing),)).fetchone()[0]
            response = JSONResponse(envelope({"csrf_token": csrf, "mode": "local_single_user"}))
        except HTTPException:
            token, csrf = store.create_session("local_user")
            response = JSONResponse(envelope({"csrf_token": csrf, "mode": "local_single_user"}))
            response.set_cookie("pokescout_session", token, httponly=True, samesite="lax", max_age=86400)
        return response

    @app.get("/api/v1/capabilities")
    async def capabilities(request: Request) -> dict:
        resolve_owner(request)
        return envelope({"features": {"activity_stream": True}, "limits": {"max_activity_bytes": 16384},
                         "mode": "local_single_user", "scope": "activity_only"})

    return app


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port)
