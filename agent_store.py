"""Additive, single-user application storage; legacy Collection tables stay intact."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from collection import Collection


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return prefix + "-" + uuid.uuid4().hex


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


class AgentError(ValueError):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable

    def as_dict(self):
        return {"code": self.code, "message": str(self), "retryable": self.retryable,
                "field_errors": [], "resource_url": None}


class AgentStore:
    """Trusted local application API, not an authenticated multi-user HTTP server."""

    def __init__(self, data_dir):
        self.root = Path(data_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.collection = Collection(self.root / "collection.sqlite3")
        self.db = self.collection.db
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS main_schema(version INTEGER PRIMARY KEY)")
            version = self.db.execute("SELECT MAX(version) FROM main_schema").fetchone()[0]
            if version not in (None, 1):
                raise AgentError("SCHEMA_UNSUPPORTED", "Unsupported main-agent database version.")
            if version is None:
                for statement in (
                    "CREATE TABLE main_jobs(id TEXT PRIMARY KEY, body TEXT NOT NULL, input TEXT NOT NULL, checkpoint TEXT NOT NULL)",
                    "CREATE TABLE main_records(kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(kind,id))",
                    "CREATE TABLE main_requests(scope TEXT NOT NULL, key TEXT NOT NULL, hash TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(scope,key))",
                    "CREATE TABLE main_confirmations(candidate_id TEXT PRIMARY KEY, copy_id TEXT NOT NULL UNIQUE REFERENCES copies(id))",
                    "CREATE TABLE main_assets(sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL)",
                    "CREATE TABLE main_copy_assets(copy_id TEXT NOT NULL REFERENCES copies(id), sha256 TEXT NOT NULL REFERENCES main_assets(sha256), role TEXT NOT NULL, PRIMARY KEY(copy_id,sha256,role))",
                    "CREATE TABLE main_intents(id TEXT PRIMARY KEY, body TEXT NOT NULL)",
                    "CREATE TABLE main_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES main_jobs(id), body TEXT NOT NULL)",
                ):
                    self.db.execute(statement)
                self.db.execute("INSERT INTO main_schema VALUES(1)")

    def close(self):
        self.collection.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def replay(self, scope, key, request):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise AgentError("INVALID_INPUT", "Supply a bounded idempotency key.")
        row = self.db.execute("SELECT hash,response FROM main_requests WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row:
            if row["hash"] != digest(request):
                raise AgentError("IDEMPOTENCY_CONFLICT", "This key was used for a different request.")
            return json.loads(row["response"])

    def remember(self, scope, key, request, response):
        self.db.execute("INSERT INTO main_requests VALUES(?,?,?,?)", (scope, key, digest(request), encoded(response)))

    def save_bytes(self, relative, raw):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # Hard-link publication fails if a final file already exists; no evidence overwrite.
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise AgentError("ARTIFACT_CONFLICT", "An immutable artifact already exists with different content.") from None
        finally:
            os.unlink(temporary)
        return relative

    def artifact(self, kind, record_id, body):
        if kind not in {"uploads", "candidates", "research", "facts", "decisions", "drafts", "snapshots"}:
            raise ValueError("Unsupported artifact kind.")
        if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", record_id):
            raise AgentError("INVALID_INPUT", "Invalid artifact identifier.")
        relative = f"research/{record_id}/report.json" if kind == "research" else f"{kind}/{record_id}.json"
        self.save_bytes(relative, (encoded(body) + "\n").encode("utf-8"))
        existing = self.db.execute("SELECT body FROM main_records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        if existing:
            if existing[0] != encoded(body):
                raise AgentError("ARTIFACT_CONFLICT", "Artifact identity conflict.")
        else:
            self.db.execute("INSERT INTO main_records VALUES(?,?,?)", (kind, record_id, encoded(body)))
        return body

    def read(self, kind, record_id):
        row = self.db.execute("SELECT body FROM main_records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        if not row:
            raise AgentError("NOT_FOUND", "Record not found.")
        return json.loads(row[0])

    def get_job(self, job_id):
        row = self.db.execute("SELECT body FROM main_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise AgentError("NOT_FOUND", "Job not found.")
        return json.loads(row[0])

    def create_job(self, job, input_value, checkpoint):
        self.db.execute(
            "INSERT INTO main_jobs(id,body,input,checkpoint) VALUES(?,?,?,?)",
            (job["job_id"], encoded(job), encoded(input_value), encoded(checkpoint)),
        )
        self.db.execute(
            "INSERT INTO main_events(job_id,body) VALUES(?,?)",
            (job["job_id"], encoded({"at": job["created_at"], "state": "queued", "stage": "queued", "revision": 0})),
        )

    def private_job(self, job_id):
        self.get_job(job_id)
        row = self.db.execute("SELECT input,checkpoint FROM main_jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]), json.loads(row[1])

    def put_job(self, job, *, input_value=None, checkpoint=None):
        job["revision"] += 1
        job["updated_at"] = utcnow()
        self.db.execute("UPDATE main_jobs SET body=? WHERE id=?", (encoded(job), job["job_id"]))
        if input_value is not None:
            self.db.execute("UPDATE main_jobs SET input=? WHERE id=?", (encoded(input_value), job["job_id"]))
        if checkpoint is not None:
            self.db.execute("UPDATE main_jobs SET checkpoint=? WHERE id=?", (encoded(checkpoint), job["job_id"]))
        self.db.execute("INSERT INTO main_events(job_id,body) VALUES(?,?)", (job["job_id"], encoded({
            "at": job["updated_at"], "state": job["state"], "stage": job["stage"], "revision": job["revision"],
            "result": job["result"], "error_code": job["error"]["code"] if job["error"] else None})))
        return job

    @staticmethod
    def require_revision(resource, revision):
        if revision is None:
            raise AgentError("PRECONDITION_REQUIRED", "Supply the current revision.")
        if type(revision) is not int or revision != resource["revision"]:
            raise AgentError("REVISION_CONFLICT", "The record changed; reload it before updating.")

    def get_intent(self, intent_id):
        row = self.db.execute("SELECT body FROM main_intents WHERE id=?", (intent_id,)).fetchone()
        if not row:
            raise AgentError("NOT_FOUND", "Intent not found.")
        return json.loads(row[0])

    def put_intent(self, intent):
        self.db.execute("UPDATE main_intents SET body=? WHERE id=?", (encoded(intent), intent["intent_id"]))
