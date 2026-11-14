"""Activity protocol checks plus real-socket SSE replay and concurrent publishers."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import httpx
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient
    from pydantic import ValidationError
except ImportError:
    raise unittest.SkipTest("Install requirements-activity.txt to test the activity service")

from activity import ActivityContext, ActivityEmitter, ActivityMiddleware, CURRENT, HEADER
from activity_server import ActivityStore, CursorError, Event, create_app


def payload(job: str, operation: str = "research.search") -> dict:
    return {"schema_version": "1.0", "event_id": str(uuid.uuid4()), "job_id": job,
            "type": "step.started", "operation": operation, "span_id": "span_demo", "parent_span_id": None,
            "attempt": 1, "occurred_at": "2026-11-14T00:00:00Z", "level": "info",
            "message": "Searching a marketplace.", "data": {"provider": "tavily", "progress": None,
            "counts": {}, "references": [], "result_status": None, "error_code": None}}


class ActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name) / "collection.sqlite3")
        self.store = self.app.state.activity_store
        self.store.register_job("alice", "job_a")
        self.store.register_job("bob", "job_b")
        self.token, _ = self.store.create_session("alice")
        self.context = self.store.issue_context("job_a", "research-agent", ["research.*"])
        self.client = TestClient(self.app)
        self.client.cookies.set("pokescout_session", self.token)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def post(self, data: dict) -> httpx.Response:
        return self.client.post("/internal/v1/activity/events", json=data,
                                headers={"Authorization": "Bearer " + self.context.publisher_token})

    def test_receipt_replay_conflict_and_terminal_duplicate(self) -> None:
        body = payload("job_a")
        first, replay = self.post(body), self.post(body)
        self.assertEqual((first.status_code, replay.status_code), (201, 200))
        self.assertEqual(first.json()["data"]["cursor"], replay.json()["data"]["cursor"])
        self.assertTrue(replay.json()["data"]["duplicate"])
        self.assertEqual(self.post({**body, "message": "Changed"}).status_code, 409)
        self.store.transition_job("job_a", "running", "research", expected_revision=1)
        self.store.transition_job("job_a", "succeeded", "finished", expected_revision=2)
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(self.post(payload("job_a")).status_code, 409)

    def test_owner_and_producer_boundaries(self) -> None:
        self.assertEqual(self.post(payload("job_b")).status_code, 403)
        self.assertEqual(self.post(payload("job_a", "trading.execute")).status_code, 403)
        self.assertEqual(self.post({**payload("job_a"), "type": "job.succeeded"}).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/activity", params={"job_id": "job_b"}).status_code, 404)
        items = self.client.get("/api/v1/activity").json()["data"]["items"]
        self.assertEqual({event["job_id"] for event in items}, {"job_a"})
        self.store.revoke_session(self.token)
        self.assertEqual(self.client.get("/api/v1/activity").status_code, 401)

    def test_payload_projection_rejects_private_or_arbitrary_data(self) -> None:
        for message in ("tvly-do-not-publish-this-key", r"C:\Users\user\private.json", "Bearer private-credential"):
            self.assertEqual(self.post({**payload("job_a"), "message": message}).status_code, 422)
        body = payload("job_a")
        body["data"]["raw_provider_response"] = {"secret": "never public"}
        self.assertEqual(self.post(body).status_code, 422)
        self.assertEqual(self.client.post("/internal/v1/activity/events", content=b"x" * 17000).status_code, 413)
        body = payload("job_a")
        body["data"]["references"] = [{"kind": "listing", "url": "https://evil.example/card"}]
        self.assertEqual(self.post(body).status_code, 422)
        self.assertEqual(self.post({**payload("job_a"), "message": self.context.publisher_token}).status_code, 422)

    def test_atomic_backend_transition(self) -> None:
        with self.store.connect() as db:
            db.execute("CREATE TABLE actual_jobs(id TEXT PRIMARY KEY,state TEXT)")
            db.execute("INSERT INTO actual_jobs VALUES('job_a','queued')")
        def broken(db: object) -> None:
            db.execute("UPDATE actual_jobs SET state='running' WHERE id='job_a'")
            raise RuntimeError("Worker failed before commit")
        before = len(self.store.read("alice", 0))
        with self.assertRaises(RuntimeError):
            self.store.transition_job("job_a", "running", "research", expected_revision=1, persist=broken)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM actual_jobs WHERE id='job_a'").fetchone()[0], "queued")
            self.assertEqual(db.execute("SELECT state FROM activity_job_scopes WHERE job_id='job_a'").fetchone()[0], "queued")
        self.assertEqual(len(self.store.read("alice", 0)), before)
        self.store.transition_job("job_a", "running", "research", expected_revision=1,
            persist=lambda db: db.execute("UPDATE actual_jobs SET state='running' WHERE id='job_a'"))
        last = self.store.read("alice", 0)[-1]
        self.assertEqual(last["type"], "job.started")
        self.assertEqual(last["data"]["job"]["revision"], 2)

    def test_rate_limit_and_progress_validation(self) -> None:
        self.assertEqual(self.post(payload("job_a")).status_code, 201)
        with self.store.connect() as db:
            db.execute("UPDATE activity_rates SET tokens=0,updated=?", (time.time(),))
        self.assertEqual(self.post(payload("job_a")).status_code, 429)
        body = payload("job_a")
        body["data"]["progress"] = {"completed": 3, "total": 2, "unit": "queries"}
        self.assertEqual(self.post(body).status_code, 422)

    def test_restart_history_and_retention_floor(self) -> None:
        self.assertEqual(self.post(payload("job_a")).status_code, 201)
        restarted = ActivityStore(self.store.path)
        self.assertEqual(len(restarted.read("alice", 0)), 2)
        for job in ("job_a", "job_b"):
            restarted.transition_job(job, "running", "research", expected_revision=1)
            restarted.transition_job(job, "succeeded", "finished", expected_revision=2)
        self.assertGreater(restarted.prune(age_seconds=-1), 0)
        with self.assertRaises(CursorError) as result:
            restarted.cursor("alice", "0")
        self.assertEqual(result.exception.reason, "cursor_expired")
        self.assertGreaterEqual(restarted.cursor("alice", result.exception.high), 1)

    def test_private_context_never_reaches_agent_inputs_or_headers(self) -> None:
        app = FastAPI()
        app.add_middleware(ActivityMiddleware)
        @app.post("/reasoners/test")
        async def reasoner(request: Request) -> dict:
            self.assertNotIn(HEADER.lower(), request.headers)
            body = await request.json()
            self.assertEqual(body, {"request": {"card": "example"}})
            self.assertEqual(CURRENT.get().context.job_id, "job_a")
            CURRENT.get().emit("step.started", "research.search", "Safe public update.")
            return {"ok": True}
        with patch.object(ActivityEmitter, "_post", return_value=True), TestClient(app) as client:
            response = client.post("/reasoners/test", json={"request": {"card": "example"}},
                                   headers={HEADER: self.context.to_header()})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.context.publisher_token, repr(self.context))


class LiveSSETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = create_app(Path(cls.temp.name) / "activity.sqlite3", heartbeat_seconds=0.05, poll_seconds=0.01)
        cls.store = cls.app.state.activity_store
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.port = cls.sock.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(cls.app, log_level="error", access_log=False))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 5
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            raise RuntimeError("SSE test server did not start")
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(5)
        cls.sock.close()
        cls.temp.cleanup()

    @staticmethod
    def events(response: httpx.Response, count: int) -> list[dict]:
        output = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                item = json.loads(line[6:])
                if "event_id" in item:
                    output.append(item)
                    if len(output) == count:
                        break
        return output

    def test_two_producers_live_delivery_reconnect_and_header_precedence(self) -> None:
        owner, job = "owner_" + uuid.uuid4().hex, "job_" + uuid.uuid4().hex
        self.store.register_job(owner, job)
        session, _ = self.store.create_session(owner)
        contexts = [self.store.issue_context(job, "image-agent", ["identify.*"]),
                    self.store.issue_context(job, "research-agent", ["research.*"])]
        with httpx.Client(base_url=self.base, cookies={"pokescout_session": session}, timeout=3) as browser:
            with browser.stream("GET", "/api/v1/activity/stream") as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/event-stream", response.headers["content-type"])
                for context, operation in zip(contexts, ("identify.card", "research.search")):
                    result = httpx.post(self.base + "/internal/v1/activity/events", json=payload(job, operation),
                        headers={"Authorization": "Bearer " + context.publisher_token}, timeout=3)
                    self.assertEqual(result.status_code, 201)
                events = self.events(response, 3)
            self.assertEqual([event["producer_id"] for event in events], ["application", "image-agent", "research-agent"])
            self.assertEqual([int(event["cursor"]) for event in events], sorted(int(event["cursor"]) for event in events))
            with browser.stream("GET", "/api/v1/activity/stream?after=invalid",
                                headers={"Last-Event-ID": events[1]["cursor"]}) as replay:
                missed = self.events(replay, 1)
            self.assertEqual(missed[0]["event_id"], events[2]["event_id"])
            # Disconnecting the browser did not finish/cancel the job or prevent publication.
            result = httpx.post(self.base + "/internal/v1/activity/events", json=payload(job),
                headers={"Authorization": "Bearer " + contexts[1].publisher_token}, timeout=3)
            self.assertEqual(result.status_code, 201)

    def test_invalid_cursor_reset_heartbeat_and_session_revocation(self) -> None:
        owner, job = "owner_" + uuid.uuid4().hex, "job_" + uuid.uuid4().hex
        self.store.register_job(owner, job)
        session, _ = self.store.create_session(owner)
        with httpx.Client(base_url=self.base, cookies={"pokescout_session": session}, timeout=3) as browser:
            invalid = browser.get("/api/v1/activity/stream?after=invalid")
            self.assertIn("event: stream.reset", invalid.text)
            self.assertNotIn("\nid:", invalid.text)
            cursor = browser.get("/api/v1/activity").json()["data"]["next_cursor"]
            with browser.stream("GET", "/api/v1/activity/stream", params={"after": cursor}) as response:
                lines = response.iter_lines()
                heartbeat = next(line for line in lines if line.startswith(": heartbeat"))
                self.assertEqual(heartbeat, ": heartbeat")
                self.store.revoke_session(session)
                self.assertFalse(any(line for line in lines))


if __name__ == "__main__":
    unittest.main()
