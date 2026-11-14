"""End-to-end checks for persisted workflow boundaries using synthetic evidence."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_store import AgentError
from orchestrator import Pipeline, fixture_report


ROOT = Path(__file__).resolve().parents[1]


def sample(name):
    return json.loads((ROOT / "examples" / "v2" / name).read_text(encoding="utf-8"))


def buy_goal():
    goal = sample("goal.json")
    goal.pop("source_mode")
    goal.update(message="Buy the exact fixture cards", intent="buy", allowed_conditions=["near_mint"])
    return goal


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.rows = sample("listings.json")
        self.pipeline = Pipeline(self.path)

    def tearDown(self):
        self.pipeline.close()
        self.temp.cleanup()

    def test_fixture_research_to_decision_is_persisted_and_idempotent(self):
        request = {"goal": buy_goal(), "fixture_listings": self.rows}
        job = self.pipeline.start(request, idempotency_key="same-request")
        self.assertEqual(job["state"], "succeeded")
        decision = self.pipeline.result(job["job_id"])
        self.assertEqual(decision["recommendation"], "buy")
        self.assertEqual([row["listing_ref"]["listing_id"] for row in decision["purchase_candidates"]],
                         ["A2", "B1"])
        self.assertEqual(decision["calculations"]["purchase_total_jpy"], 8900)
        self.assertEqual(len(decision["rejections"]), 4)
        self.assertEqual(self.pipeline.start(request, idempotency_key="same-request")["job_id"], job["job_id"])
        with self.assertRaisesRegex(AgentError, "different request"):
            self.pipeline.start({"goal": buy_goal(), "fixture_listings": []}, idempotency_key="same-request")
        self.pipeline.close()
        self.pipeline = Pipeline(self.path)
        self.assertEqual(self.pipeline.result(job["job_id"])["decision_id"], decision["decision_id"])

    def test_image_candidate_requires_answer_and_is_not_reidentified(self):
        calls = []
        def identify(images):
            calls.append(images)
            return {"ok": True, "card": self.rows[0]["card"], "requires_confirmation": True}
        self.pipeline.close()
        self.pipeline = Pipeline(self.path, identify=identify)
        goal = {"message": "Find this card", "intent": "buy"}
        job = self.pipeline.start({"goal": goal, "images": ["card.png"], "fixture_listings": self.rows})
        self.assertEqual(job["state"], "awaiting_input")
        self.assertEqual(len(calls), 1)
        answers = []
        for question in job["questions"]:
            value = {"targets.0.card": self.rows[0]["card"], "budget_jpy": 5000,
                     "allowed_conditions": ["near_mint"]}[question["field"]]
            answers.append({"question_id": question["question_id"], "value": value})
        with self.assertRaises(AgentError):
            self.pipeline.answer(job["job_id"], answers, revision=job["revision"] - 1)
        completed = self.pipeline.answer(job["job_id"], answers, revision=job["revision"])
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.pipeline.result(job["job_id"])["recommendation"], "buy")

    def test_image_with_existing_target_still_requires_confirmation(self):
        self.pipeline.close()
        self.pipeline = Pipeline(self.path, identify=lambda _: {"ok": True, "card": self.rows[0]["card"]})
        goal = buy_goal()
        goal["targets"] = goal["targets"][:1]
        job = self.pipeline.start({"goal": goal, "images": ["card.png"],
                                   "fixture_listings": self.rows})
        self.assertEqual(job["state"], "awaiting_input")
        self.assertEqual([q["field"] for q in job["questions"]], ["targets.0.card"])
        answered = self.pipeline.answer(job["job_id"], [{"question_id": job["questions"][0]["question_id"],
            "value": self.rows[0]["card"]}], revision=job["revision"])
        self.assertEqual(answered["state"], "succeeded")

    def test_unconfirmed_live_listing_does_not_become_buy_candidate(self):
        report, _ = fixture_report([self.rows[1]])
        report["listings"][0]["url"] = "https://jp.mercari.com/item/m123456"
        report["listings"][0]["evidence"][0]["source_url"] = report["listings"][0]["url"]
        job = self.pipeline.start({"goal": buy_goal(), "reports": [report]})
        self.assertEqual(job["state"], "succeeded")
        decision = self.pipeline.result(job["job_id"])
        self.assertEqual(decision["recommendation"], "insufficient_evidence")
        self.assertIn("LISTING_FACTS_UNCONFIRMED", decision["rejections"][0]["reasons"])

    def test_unmatched_confirmation_cannot_make_listing_eligible(self):
        report, _ = fixture_report([self.rows[1]])
        job = self.pipeline.start({"goal": buy_goal(), "reports": [report],
                                   "facts": {"unknown-observation": {"card": self.rows[1]["card"],
                                             "quantity": 1, "purchase_fee_jpy": 0}}})
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "INVALID_INPUT")

    def test_sale_preview_preserves_defects_and_never_publishes(self):
        copy = self.pipeline.store.collection.add(sample("owned-card.json"), allow_sale=True)
        goal = {"message": "Sell my owned card", "intent": "sell", "copy_ids": [copy["id"]]}
        job = self.pipeline.start({"goal": goal, "asking_price_jpy": 1800})
        self.assertEqual(job["state"], "succeeded")
        sale = self.pipeline.result(job["job_id"])
        self.assertIn(copy["card"]["defects"][0], sale["description"])
        self.assertFalse(sale["publish_ready"])

    def test_fixture_browser_execution_is_explicit_and_not_replayed(self):
        calls = []
        def execute(goal, listings, selected, out):
            calls.append(selected)
            by_id = {row["id"]: row for row in listings}
            orders = [{"listing": listing_id, "total": sum(by_id[listing_id][field]
                       for field in ("price_jpy", "shipping_jpy", "purchase_fee_jpy")),
                       "receipt": "TEST-" + listing_id} for listing_id in selected]
            return {"mode": "synthetic_fixture", "orders": orders, "selected": selected,
                    "live_marketplace_transactions": 0}
        self.pipeline.close()
        self.pipeline = Pipeline(self.path, fixture_executor=execute)
        job = self.pipeline.start({"goal": buy_goal(), "fixture_listings": self.rows,
                                   "execute_fixture": True}, idempotency_key="test-order")
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(calls, [["A2", "B1"]])
        self.pipeline.start({"goal": buy_goal(), "fixture_listings": self.rows,
                             "execute_fixture": True}, idempotency_key="test-order")
        self.assertEqual(len(calls), 1)
        another = self.pipeline.start({"goal": buy_goal(), "fixture_listings": self.rows,
                                       "execute_fixture": True})
        self.assertEqual(another["state"], "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.pipeline.result(another["job_id"])["recommendation"], "insufficient_evidence")

    def test_lost_fixture_outcome_stays_reserved_and_is_not_retried(self):
        calls = []
        def fail_after_submission(*args):
            calls.append(True)
            raise TimeoutError("Simulated response loss")
        self.pipeline.close()
        self.pipeline = Pipeline(self.path, fixture_executor=fail_after_submission)
        request = {"goal": buy_goal(), "fixture_listings": self.rows, "execute_fixture": True}
        job = self.pipeline.start(request, idempotency_key="lost-test-order")
        self.assertEqual(job["state"], "failed")
        self.assertEqual({row["state"] for row in self.pipeline.store.collection.actions()}, {"unknown"})
        self.pipeline.start(request, idempotency_key="lost-test-order")
        self.assertEqual(len(calls), 1)
        another = self.pipeline.start(request)
        self.assertEqual(another["state"], "succeeded")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
