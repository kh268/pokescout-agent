"""Verify that HTTP research jobs feed the shared evidence-based decision adapter."""
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.worker import Settings
from orchestrator import fixture_report


ROOT = Path(__file__).resolve().parents[1]


class ResearchDecisionBridgeTests(unittest.TestCase):
    def test_unconfirmed_research_observation_is_rejected_by_http_pipeline(self):
        goal = json.loads((ROOT / "examples/v2/goal.json").read_text(encoding="utf-8"))
        goal.pop("source_mode")
        goal.update(message="Find these exact cards", intent="buy", allowed_conditions=["near_mint"])
        rows = json.loads((ROOT / "examples/v2/listings.json").read_text(encoding="utf-8"))
        report, _ = fixture_report([rows[1]])
        url = "https://jp.mercari.com/item/m123456"
        report["listings"][0]["url"] = url
        report["listings"][0]["evidence"][0]["source_url"] = url

        async def research(_request):
            value = json.loads(json.dumps(report))
            value["run_id"] = "market_" + uuid.uuid4().hex
            value["request"] = _request
            return value

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Settings(data_dir=Path(directory), research_enabled=True, research=research))
            with TestClient(app) as client:
                csrf = client.get("/api/v1/session").json()["data"]["csrf_token"]
                response = client.post("/api/v1/jobs", json={"type": "decision", "input": {"goal": goal}},
                                       headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())})
                self.assertEqual(response.status_code, 202, response.text)
                job_id = response.json()["data"]["job_id"]
                for _ in range(150):
                    job = client.get("/api/v1/jobs/" + job_id).json()["data"]
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.02)
                self.assertEqual(job["state"], "succeeded", job)
                decision = client.get(job["result"]["url"]).json()["data"]
                self.assertEqual(decision["recommendation"], "insufficient_evidence")
                self.assertIn("LISTING_FACTS_UNCONFIRMED", decision["rejections"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
