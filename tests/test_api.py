"""HTTP integration tests: real upload/database/worker/fixture-order boundaries."""
import copy
import io
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from backend.app import create_app
from backend.worker import Settings

ROOT = Path(__file__).resolve().parents[1]
CARD = json.loads((ROOT / "examples/v2/owned-card.json").read_text(encoding="utf-8"))
LISTINGS = json.loads((ROOT / "examples/v2/listings.json").read_text(encoding="utf-8"))


def identify(paths: list[str]) -> dict:
    assert all(Path(path).is_file() for path in paths)
    return {"ok": True, "card": copy.deepcopy(CARD), "candidate_complete": True,
            "missing_fields": [], "provider": "scripted-test", "returned_model": "test"}


async def research(request: dict) -> dict:
    return {"schema_version": "1.0", "run_id": "market_" + uuid.uuid4().hex,
            "status": "partial", "request": request, "started_at": "2026-11-14T09:00:00Z",
            "completed_at": "2026-11-14T09:00:01Z", "listings": [], "comparable_offers": [],
            "lowest_qualifying_observed_price": None, "findings": [], "coverage": {"searches": 1},
            "failures": [], "uncertainty": ["No verified stock"], "suggested_next_steps": [],
            "log_path": "C:/private/provider.log"}


class APITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root, identify_enabled=True, research_enabled=True,
                                 identify=identify, research=research, fixture_listings=copy.deepcopy(LISTINGS))
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.csrf = self.client.get("/api/v1/session").json()["data"]["csrf_token"]

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def headers(self, key: str | None = None, rev: int | None = None) -> dict:
        headers = {"X-CSRF-Token": self.csrf, "Idempotency-Key": key or str(uuid.uuid4())}
        if rev is not None:
            headers["If-Match"] = f'"{rev}"'
        return headers

    def post(self, path: str, body: dict, *, rev: int | None = None, key: str | None = None):
        return self.client.post("/api/v1" + path, json=body, headers=self.headers(key, rev))

    def wait(self, job: dict) -> dict:
        for _ in range(200):
            current = self.client.get("/api/v1/jobs/" + job["job_id"]).json()["data"]
            if current["state"] in {"succeeded", "failed", "cancelled", "awaiting_input"}:
                return current
            time.sleep(0.02)
        self.fail("Job did not settle")

    def upload(self, key: str | None = None):
        image = io.BytesIO()
        Image.new("RGB", (16, 16), "orange").save(image, "PNG")
        return self.client.post("/api/v1/uploads", headers=self.headers(key),
                                data={"manifest": json.dumps([{"role": "front"}])},
                                files=[("files", ("card.png", image.getvalue(), "image/png"))])

    def candidate(self) -> dict:
        upload = self.upload()
        self.assertEqual(upload.status_code, 201, upload.text)
        job = self.post("/jobs", {"type": "identify", "input": {"upload_id": upload.json()["data"]["upload_id"]}})
        self.assertEqual(job.status_code, 202, job.text)
        done = self.wait(job.json()["data"])
        self.assertEqual(done["state"], "succeeded", done)
        return self.client.get(done["result"]["url"]).json()["data"]

    def test_upload_recognition_confirmation_persist_in_existing_database(self) -> None:
        candidate = self.candidate()
        with closing(sqlite3.connect(self.root / "collection.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM copies").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM api_records WHERE kind='candidate'").fetchone()[0], 1)
        key = str(uuid.uuid4())
        body = {"candidate_id": candidate["candidate_id"], "confirmed_card": CARD,
                "confirm_owned": True, "allow_sale": True, "cost_jpy": 1200}
        first = self.post("/copies/confirm", body, key=key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(self.post("/copies/confirm", body, key=key).json()["data"], first.json()["data"])
        self.assertEqual(self.post("/copies/confirm", body).status_code, 409)
        with closing(sqlite3.connect(self.root / "collection.sqlite3")) as db:
            row = db.execute("SELECT id,card,cost,allow_sale FROM copies").fetchone()
            self.assertEqual(row[0], first.json()["data"]["id"])
            self.assertEqual(json.loads(row[1]), CARD)
            self.assertEqual(row[2:], (1200, 1))
        self.assertEqual(len(self.client.get("/api/v1/collection").json()["data"]["items"]), 1)
        image = self.client.get(first.json()["data"]["images"][0]["url"])
        self.assertEqual(image.status_code, 200)
        self.assertTrue(image.headers["content-type"].startswith("image/png"))

    def test_upload_idempotency_ownership_and_csrf(self) -> None:
        key = str(uuid.uuid4())
        first, second = self.upload(key), self.upload(key)
        self.assertEqual(first.json()["data"], second.json()["data"])
        other = TestClient(self.app)
        other.get("/api/v1/session")
        self.assertEqual(other.get("/api/v1/uploads/" + first.json()["data"]["upload_id"]).status_code, 404)
        self.assertEqual(other.get(first.json()["data"]["images"][0]["url"]).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/jobs", json={}).status_code, 403)

    def test_invalid_images_rejected_before_recognition(self) -> None:
        response = self.client.post("/api/v1/uploads", headers=self.headers(),
                                    data={"manifest": '[{"role":"front"}]'},
                                    files=[("files", ("card.png", b"not a png", "image/png"))])
        self.assertEqual(response.status_code, 415)
        with closing(sqlite3.connect(self.root / "collection.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM api_records WHERE kind='upload'").fetchone()[0], 0)

    def test_partial_research_is_successful_job_and_private_paths_are_omitted(self) -> None:
        response = self.post("/jobs", {"type": "research", "input": {"request": {
            "confirmed_target": {"name_ja": "ミュウツーV", "number": "273/S-P"}}}})
        self.assertEqual(response.status_code, 202, response.text)
        done = self.wait(response.json()["data"])
        self.assertEqual(done["state"], "succeeded", done)
        report = self.client.get(done["result"]["url"])
        self.assertEqual(report.json()["data"]["status"], "partial")
        self.assertNotIn("log_path", report.text)
        self.assertNotIn("private/provider", report.text)

    def test_clarification_and_fixture_purchase_are_end_to_end_idempotent(self) -> None:
        target = LISTINGS[0]["card"]
        payload = {"type": "decision", "input": {"goal": {"intent": "buy", "message": "Buy fixture A",
                   "targets": [{"card": target}]}}}
        response = self.post("/jobs", payload)
        waiting = self.wait(response.json()["data"])
        self.assertEqual(waiting["state"], "awaiting_input", waiting)
        values = {"budget_jpy": 5000, "allowed_conditions": ["near_mint"]}
        answers = {"answers": [{"question_id": q["question_id"], "value": values[q["field"]]} for q in waiting["questions"]]}
        self.assertEqual(self.post("/jobs/" + waiting["job_id"] + "/answers", answers, rev=0).status_code, 412)
        resumed = self.post("/jobs/" + waiting["job_id"] + "/answers", answers, rev=waiting["revision"])
        self.assertEqual(resumed.status_code, 202, resumed.text)
        done = self.wait(resumed.json()["data"])
        self.assertEqual(done["state"], "succeeded", done)
        decision = self.client.get(done["result"]["url"]).json()["data"]
        self.assertEqual(decision["recommendation"], "buy", decision)
        listing = decision["purchase_candidates"][0]
        prepared = self.post("/action-intents", {"action": "buy", "execution_mode": "fixture",
                              "decision_id": decision["decision_id"], "listing_id": listing["id"], "max_total_jpy": 5000})
        self.assertEqual(prepared.status_code, 201, prepared.text)
        intent = prepared.json()["data"]
        base = "/action-intents/" + intent["intent_id"]
        self.assertEqual(self.post(base + "/execute", {}, rev=intent["revision"]).status_code, 409)
        authorized = self.post(base + "/authorize", {"payload_hash": intent["payload_hash"], "confirm": True}, rev=intent["revision"])
        self.assertEqual(authorized.status_code, 200, authorized.text)
        current = authorized.json()["data"]
        executed = self.post(base + "/execute", {}, rev=current["revision"])
        self.assertEqual(executed.status_code, 202, executed.text)
        finished = self.wait(executed.json()["data"])
        self.assertEqual(finished["state"], "succeeded", finished)
        latest = self.client.get("/api/v1" + base).json()["data"]
        self.assertEqual(latest["outcome"]["status"], "confirmed", latest)
        replay = self.post(base + "/execute", {}, rev=latest["revision"])
        self.assertEqual(replay.json()["data"]["job_id"], executed.json()["data"]["job_id"])
        with closing(sqlite3.connect(self.root / "fixture-market.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM orders").fetchone()[0], 1)
        self.assertTrue(latest["outcome"]["receipt_ref"].startswith("TEST-"))

    def test_sale_permission_then_draft_and_live_disabled(self) -> None:
        candidate = self.candidate()
        owned = self.post("/copies/confirm", {"candidate_id": candidate["candidate_id"], "confirmed_card": CARD,
                                               "confirm_owned": True}).json()["data"]
        self.assertEqual(self.post("/sale-drafts", {"copy_id": owned["id"], "asking_price_jpy": 1800}).status_code, 409)
        response = self.client.patch("/api/v1/copies/" + owned["id"], json={"allow_sale": True}, headers=self.headers(rev=1))
        self.assertEqual(response.status_code, 200, response.text)
        draft = self.post("/sale-drafts", {"copy_id": owned["id"], "asking_price_jpy": 1800})
        done = self.wait(draft.json()["data"])
        self.assertEqual(done["state"], "succeeded", done)
        result = self.client.get(done["result"]["url"]).json()["data"]
        self.assertFalse(result["publish_ready"])
        self.assertEqual(result["required_defects"], CARD["defects"])
        self.assertEqual(self.post("/action-intents", {"action": "buy", "execution_mode": "live"}).status_code, 409)

    def test_changed_idempotency_payload_conflicts(self) -> None:
        key = str(uuid.uuid4())
        base = {"type": "decision", "input": {"goal": {"intent": "buy", "message": "A"}}}
        self.assertEqual(self.post("/jobs", base, key=key).status_code, 202)
        base["input"]["goal"]["message"] = "B"
        self.assertEqual(self.post("/jobs", base, key=key).status_code, 409)

    def test_persisted_candidate_survives_backend_restart_with_same_session(self) -> None:
        candidate = self.candidate()
        cookies = dict(self.client.cookies)
        self.client.__exit__(None, None, None)
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.cookies.update(cookies)
        self.client.__enter__()
        response = self.client.get("/api/v1/candidates/" + candidate["candidate_id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["card"], CARD)

    def test_request_limits_and_strict_validation(self) -> None:
        self.assertEqual(self.post("/jobs", {"type": "decision", "input": {"goal": {
            "intent": "buy", "message": "x", "budget_jpy": True}}}).status_code, 422)
        response = self.client.post("/api/v1/jobs", content=b"{", headers={**self.headers(), "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post("/api/v1/jobs", content=b"x" * 1_048_577, headers={**self.headers(), "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.post("/jobs", {"type": "research", "input": {"request": {
            "confirmed_target": {"name_ja": "ミュウツーV", "number": "273/S-P"},
            "source_limits": {"allowed_domains": ["evil.example"]}}}}).status_code, 422)

    def test_lifecycle_hook_shares_atomic_job_transaction(self) -> None:
        seen = []
        def hook(db, owner, job, event_type):
            stored = json.loads(db.execute("SELECT data FROM api_records WHERE id=?", (job["job_id"],)).fetchone()[0])
            self.assertEqual(stored["state"], job["state"])
            seen.append(event_type)
        self.app.state.store.on_job_event = hook
        response = self.post("/jobs", {"type": "decision", "input": {"goal": {"intent": "buy", "message": "A"}}})
        self.wait(response.json()["data"])
        self.assertIn("job.queued", seen)
        self.assertIn("job.awaiting_input", seen)

    def test_allowed_frontend_origin_can_preflight_before_session(self) -> None:
        self.settings.allowed_origins = ["http://localhost:5173"]
        app = create_app(self.settings)
        with TestClient(app) as client:
            response = client.options("/api/v1/uploads", headers={
                "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-csrf-token,idempotency-key"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")


if __name__ == "__main__":
    unittest.main()
