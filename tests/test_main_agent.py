"""Main-agent integration tests: evidence, durable jobs and fixture trade boundaries."""
import asyncio
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_store import AgentError, digest, encoded
from agent_trading import FixtureExecutor
from main_agent import MainAgent
from main_agent_cli import install_demo_evidence
from research_decisions import observation_key

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / "examples" / "v2" / name).read_text(encoding="utf-8"))


class MainAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.agent = MainAgent(self.data)
        self.goal = fixture("goal.json")
        self.goal.pop("source_mode")
        self.goal.update(message="I want these cards", intent="buy", allowed_conditions=["near_mint"])
        self.rows = fixture("listings.json")
        self.calls = 0

    def tearDown(self):
        self.agent.close()
        self.temp.cleanup()

    async def decision(self, goal=None, runs=None, key=None):
        if runs is None:
            runs = install_demo_evidence(self.agent, self.goal, self.rows)
        job = self.agent.create_job("decision", {"goal": goal or self.goal, "research_run_ids": runs}, key=key or os.urandom(8).hex())
        return await self.agent.run_job(job["job_id"])

    def report(self, run_id):
        return self.agent.store.read("research", run_id)

    def decision_value(self, job):
        self.assertEqual(job["state"], "succeeded", job)
        return self.agent.store.read("decisions", job["result"]["id"])

    def copy_report(self, report, *, run_id="modified", mode="fixture", with_facts=True):
        result = copy.deepcopy(report)
        result["run_id"] = run_id
        self.agent.import_research(result, source_mode=mode)
        if with_facts:
            for row in result["listings"]:
                old_key = observation_key(report["run_id"], row)
                try:
                    original = self.agent.store.read("facts", old_key)
                except AgentError:
                    original = next((r for r in self.rows if r["id"] == row["listing_id"]), None)
                    facts = {"card": original["card"], "quantity": 1, "purchase_fee_jpy": 0}
                else:
                    facts = {k: original[k] for k in ("card", "quantity", "purchase_fee_jpy", "condition_record", "registry_status")}
                if facts.get("condition_record"):
                    facts["condition_record"]["condition_raw"] = row["condition"]
                self.agent.confirm_listing(run_id, row["listing_id"], row["observed_at"], facts, confirmed=True)
        return run_id

    async def test_full_collection_research_decision_and_draft(self):
        owned = self.agent.store.collection.add(fixture("owned-card.json"), cost=1200, allow_sale=True)
        job = await self.decision()
        result = self.decision_value(job)
        self.assertEqual(result["recommendation"], "buy")
        self.assertEqual([r["listing_ref"]["listing_id"] for r in result["purchase_candidates"]], ["A2", "B1"])
        self.assertEqual(result["calculations"]["purchase_total_jpy"], 8900)
        self.assertEqual(result["calculations"]["remaining_allowance_jpy"], 1100)
        self.assertIsNone(result["calculations"]["confirmed_cash_jpy"])
        self.assertEqual(self.agent.store.collection.actions(), [])
        for ref in result["evidence_refs"]:
            self.assertTrue(self.report(ref["run_id"]))
        self.assertEqual(result["execution"], "none")
        sale = self.agent.create_sale_draft(owned["id"], 1800, key="sale")
        sale = await self.agent.run_job(sale["job_id"])
        sale = self.agent.store.read("drafts", sale["result"]["id"])
        self.assertFalse(sale["publish_ready"])
        self.assertIn(owned["card"]["defects"][0], sale["description"])

    async def test_replan_uses_new_prices_and_preserves_old_decision(self):
        first = self.decision_value(await self.decision())
        self.rows[1]["price_jpy"] = 3800
        second = self.decision_value(await self.decision())
        self.assertEqual([c["listing_ref"]["listing_id"] for c in second["purchase_candidates"]], ["A1", "B1"])
        self.assertEqual(second["calculations"]["purchase_total_jpy"], 9000)
        self.assertEqual(self.agent.store.read("decisions", first["decision_id"]), first)

    async def test_missing_material_constraints_wait_and_resume_same_job(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        job = self.agent.create_job("decision", {"goal": {"message": "I want a card", "intent": "buy"}, "research_run_ids": runs[:1]}, key="clarify")
        job = await self.agent.run_job(job["job_id"])
        self.assertEqual(job["state"], "awaiting_input")
        self.assertEqual(len(job["questions"]), 3)
        values = {"targets.0.card": self.goal["targets"][0]["card"], "budget_jpy": 5000, "allowed_conditions": ["near_mint"]}
        answers = [{"question_id": q["question_id"], "value": values[q["field"]]} for q in job["questions"]]
        with self.assertRaises(AgentError) as error:
            self.agent.answers(job["job_id"], answers, revision=job["revision"] - 1, key="stale")
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")
        resumed = self.agent.answers(job["job_id"], answers, revision=job["revision"], key="answers")
        replay = self.agent.answers(job["job_id"], answers, revision=job["revision"], key="answers")
        self.assertEqual(resumed, replay)
        result = await self.agent.run_job(job["job_id"])
        self.assertEqual(result["job_id"], job["job_id"])
        self.assertEqual(self.decision_value(result)["calculations"]["purchase_total_jpy"], 3900)
        with self.assertRaises(AgentError):
            self.agent.answers(job["job_id"], answers, revision=result["revision"], key="stale-questions")

    async def test_incomplete_identity_gets_typed_card_question(self):
        goal = copy.deepcopy(self.goal)
        goal["targets"][0]["card"]["set"] = None
        job = self.agent.create_job("decision", {"goal": goal}, key="identity")
        job = await self.agent.run_job(job["job_id"])
        self.assertEqual(job["state"], "awaiting_input")
        self.assertEqual(job["questions"][0]["value_type"], "card")

    async def test_job_idempotency_conflicts_and_concurrent_limit(self):
        request = {"goal": self.goal}
        first = self.agent.create_job("decision", request, key="one")
        self.assertEqual(first, self.agent.create_job("decision", request, key="one"))
        changed = copy.deepcopy(request); changed["goal"]["budget_jpy"] = 5
        with self.assertRaises(AgentError) as error:
            self.agent.create_job("decision", changed, key="one")
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        self.agent.create_job("decision", request, key="two")
        with self.assertRaises(AgentError) as error:
            self.agent.create_job("decision", request, key="three")
        self.assertEqual(error.exception.code, "JOB_LIMIT_REACHED")

    async def test_queued_jobs_survive_restart_and_recover_does_not_repeat_running_work(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        job = self.agent.create_job("decision", {"goal": self.goal, "research_run_ids": runs}, key="restart")
        self.agent.close(); self.agent = MainAgent(self.data)
        self.assertEqual(self.agent.get_job(job["job_id"])["state"], "queued")
        self.assertEqual((await self.agent.work())[0]["state"], "succeeded")
        other = self.agent.create_job("decision", {"goal": self.goal, "research_run_ids": runs}, key="interrupted")
        other["state"] = "running"
        with self.agent.store.transaction(): self.agent.store.put_job(other)
        recovered = self.agent.recover_interrupted()
        self.assertEqual(recovered[0]["error"]["code"], "JOB_INTERRUPTED")
        self.assertEqual(await self.agent.work(), [])

    async def test_cancel_before_work_makes_no_provider_call(self):
        job = self.agent.create_job("decision", {"goal": self.goal}, key="cancel")
        self.agent.cancel(job["job_id"], revision=job["revision"], key="cancel-request")
        job = await self.agent.run_job(job["job_id"])
        self.assertEqual(job["state"], "cancelled")

    async def test_missing_research_service_fails_without_fixture_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            job = self.agent.create_job("decision", {"goal": self.goal}, key="no-service")
            job = await self.agent.run_job(job["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "PROVIDER_UNAVAILABLE")

    async def test_research_job_retains_partial_report_and_evidence(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        report = self.report(runs[0]); report["run_id"] = "partial-live"; report["status"] = "partial"
        report["failures"] = [{"stage": "inspection", "source_url": None, "message": "blocked"}]
        async def researcher(request):
            self.calls += 1
            return report
        self.agent.researcher = researcher; self.agent.research_configured = True
        job = self.agent.create_job("research", {"request": report["request"]}, key="partial")
        job = await self.agent.run_job(job["job_id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(self.agent.research_report("partial-live")["status"], "partial")
        self.assertNotIn("log_path", self.agent.research_report("partial-live"))
        self.assertEqual(self.calls, 1)
        self.assertEqual(await self.agent.run_job(job["job_id"]), job)
        self.assertEqual(self.calls, 1)
        self.assertTrue(list((self.data / "evidence").glob("*.txt")))

    async def test_timeout_is_durable_and_provider_errors_are_safe(self):
        async def slow(request): await asyncio.sleep(10)
        self.agent.researcher = slow; self.agent.research_configured = True
        # Exercise wait_for without a long test delay; requests still carry bounded limits.
        async def timed_out(*args): raise asyncio.TimeoutError()
        self.agent._research = timed_out
        job = self.agent.create_job("decision", {"goal": self.goal}, key="timeout")
        job = await self.agent.run_job(job["job_id"])
        self.assertEqual(job["error"]["code"], "JOB_TIMEOUT")
        async def failed(*args): raise RuntimeError("secret-key /private/provider/path")
        self.agent._research = failed
        other = self.agent.create_job("decision", {"goal": self.goal}, key="provider-error")
        other = await self.agent.run_job(other["job_id"])
        self.assertNotIn("secret-key", json.dumps(other))
        self.assertNotIn("/private/provider", json.dumps(other))

    async def test_research_qualifies_does_not_supply_missing_listing_identity_or_fees(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        report = self.report(runs[0])
        unconfirmed = self.copy_report(report, run_id="no-facts", with_facts=False)
        goal = dict(self.goal, targets=self.goal["targets"][:1])
        result = self.decision_value(await self.decision(goal, [unconfirmed]))
        self.assertEqual(result["recommendation"], "insufficient_evidence")
        self.assertEqual(result["purchase_candidates"], [])
        self.assertTrue(any("LISTING_FACTS_UNCONFIRMED" in r["reasons"] for r in result["rejections"]))

    async def test_invalid_jpy_and_unknown_costs_cannot_be_selected(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        goal = dict(self.goal, targets=self.goal["targets"][:1])
        for index, value in enumerate([3.5, float("inf"), True, -1, 100_000_001]):
            report = self.report(runs[0])
            report["listings"] = [report["listings"][0]]
            report["listings"][0]["price"] = value
            if value == float("inf"):
                with self.assertRaises(ValueError): self.copy_report(report, run_id=f"bad-{index}")
                continue
            new_run = self.copy_report(report, run_id=f"bad-{index}")
            result = self.decision_value(await self.decision(goal, [new_run]))
            self.assertFalse(result["purchase_candidates"])
            self.assertIn("INVALID_JPY_AMOUNT", result["rejections"][0]["reasons"])

    async def test_shop_letter_condition_is_unknown_until_explicitly_mapped(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        report = self.report(runs[0]); report["listings"] = report["listings"][:1]
        report["listings"][0]["condition"] = "A−"
        run_id = self.copy_report(report, run_id="shop-letter")
        goal = dict(self.goal, targets=self.goal["targets"][:1])
        result = self.decision_value(await self.decision(goal, [run_id]))
        self.assertEqual(result["recommendation"], "insufficient_evidence")
        self.assertIn("CONDITION_BELOW_REQUIREMENT_OR_UNKNOWN", result["rejections"][0]["reasons"])

    async def test_newer_sold_observation_invalidates_older_available_listing(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        old = self.report(runs[0]); old["listings"] = old["listings"][:1]
        old_run = self.copy_report(old, run_id="old")
        newer = copy.deepcopy(old)
        newer["listings"][0]["observed_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        newer["listings"][0]["availability"] = "sold"
        new_run = self.copy_report(newer, run_id="sold", with_facts=False)
        result = self.decision_value(await self.decision(dict(self.goal, targets=self.goal["targets"][:1]), [old_run, new_run]))
        self.assertFalse(result["purchase_candidates"])
        self.assertTrue(any("DUPLICATE_OBSERVATION" in r["reasons"] for r in result["rejections"]))

    async def test_same_listing_in_multiple_runs_does_not_supply_two_copies(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        report = self.report(runs[0]); report["listings"] = report["listings"][:1]
        first = self.copy_report(report, run_id="one-listing")
        second = self.copy_report(report, run_id="same-listing")
        goal = dict(self.goal, targets=[dict(self.goal["targets"][0], quantity=2)])
        result = self.decision_value(await self.decision(goal, [first, second]))
        self.assertEqual(len(result["purchase_candidates"]), 1)

    async def test_stale_replay_is_excluded_and_cannot_be_relabelled(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        report = self.report(runs[0]); report["listings"] = report["listings"][:1]
        row = report["listings"][0]
        row["url"] = "https://jp.mercari.com/item/m10000000"
        row["observed_at"] = "2020-01-01T00:00:00+00:00"
        row["evidence"][0]["source_url"] = row["url"]
        run_id = self.copy_report(report, run_id="stale", mode="replay")
        result = self.decision_value(await self.decision(dict(self.goal, targets=self.goal["targets"][:1]), [run_id]))
        self.assertIn("EVIDENCE_STALE", result["rejections"][0]["reasons"])
        with self.assertRaises(AgentError): self.agent.import_research(self.report(run_id), source_mode="fixture")

    async def test_owned_quantity_avoids_duplicate_buy(self):
        self.agent.store.collection.add(self.goal["targets"][0]["card"])
        result = self.decision_value(await self.decision())
        self.assertEqual([c["listing_ref"]["listing_id"] for c in result["purchase_candidates"]], ["B1"])

    async def test_sale_protection_and_no_unsupported_valuation(self):
        protected = self.agent.store.collection.add(fixture("owned-card.json"))
        with self.assertRaises(AgentError): self.agent.create_sale_draft(protected["id"], 1800, key="protected")
        goal = {"message": "Sell this card", "intent": "sell", "copy_ids": [protected["id"]]}
        job = self.agent.create_job("decision", {"goal": goal}, key="sell-protected")
        job = await self.agent.run_job(job["job_id"])
        self.assertEqual(job["state"], "awaiting_input")
        permitted = self.agent.store.collection.add(fixture("owned-card.json"), allow_sale=True)
        self.agent.answers(job["job_id"], [{"question_id": job["questions"][0]["question_id"], "value": permitted["id"]}], revision=job["revision"], key="new-copy")
        result = self.decision_value(await self.agent.run_job(job["job_id"]))
        self.assertEqual(result["recommendation"], "insufficient_evidence")
        self.assertIn("SALE_VALUATION_UNAVAILABLE", result["requirements"])
        self.assertIsNone(result["sale_candidates"][0]["asking_price_jpy"])

    async def test_unexpected_fields_and_source_budgets_are_rejected(self):
        with self.assertRaises(AgentError): self.agent.create_job("decision", {"goal": dict(self.goal, execute=True)}, key="extra")
        for limits in ({"max_searches": 100}, {"allowed_domains": ["127.0.0.1"]}, {"max_inspections": True}):
            with self.assertRaises(AgentError):
                self.agent.create_job("research", {"request": {"confirmed_target": {"name_ja": "Mewtwo", "number": "273/S-P"}, "source_limits": limits}}, key="budget")

    async def test_evidence_hash_and_immutable_report_fail_closed(self):
        runs = install_demo_evidence(self.agent, self.goal, self.rows)
        report = self.report(runs[0]); report["listings"][0]["evidence"][0]["content"] = "invented"
        with self.assertRaises(AgentError): self.agent.import_research(report)
        report = self.report(runs[0]); report["status"] = "failed"
        with self.assertRaises(AgentError): self.agent.import_research(report, source_mode="fixture")

    async def test_identification_retains_images_and_requires_separate_possession_confirmation(self):
        from PIL import Image
        path = self.data / "input.png"
        Image.new("RGB", (10, 10), "red").save(path)
        upload = self.agent.save_upload([path], key="upload")
        self.assertEqual(upload, self.agent.save_upload([path], key="upload"))
        def recognizer(paths):
            self.assertTrue(Path(paths[0]).is_file())
            return {"ok": True, "card": self.goal["targets"][0]["card"], "candidate_complete": True,
                    "missing_fields": [], "provider": "mock", "observed_at": datetime.now(timezone.utc).isoformat()}
        self.agent.recognizer = recognizer
        job = self.agent.create_job("identify", {"upload_id": upload["upload_id"]}, key="identify")
        job = await self.agent.run_job(job["job_id"])
        candidate = self.agent.store.read("candidates", job["result"]["id"])
        self.assertTrue(candidate["requires_confirmation"])
        self.assertEqual(self.agent.store.collection.list(), [])
        with self.assertRaises(AgentError): self.agent.confirm_copy(candidate["candidate_id"], candidate["card"], confirm_owned=False, key="no-possession")
        owned = self.agent.confirm_copy(candidate["candidate_id"], candidate["card"], confirm_owned=True, key="confirm")
        self.assertIsNone(owned["cost"])
        self.assertFalse(owned["allow_sale"])
        self.assertEqual(self.agent.confirm_copy(candidate["candidate_id"], candidate["card"], confirm_owned=True, key="confirm"), owned)
        with self.assertRaises(AgentError) as error:
            self.agent.confirm_copy(candidate["candidate_id"], candidate["card"], confirm_owned=True, key="other-key")
        self.assertEqual(error.exception.code, "ALREADY_CONFIRMED")
        self.agent.close(); self.agent = MainAgent(self.data)
        self.assertEqual(self.agent.store.collection.get(owned["id"]), owned)
        self.assertEqual(self.agent.store.db.execute("SELECT COUNT(*) FROM main_copy_assets").fetchone()[0], 1)

    async def test_invalid_image_bytes_are_not_trusted_by_extension(self):
        path = self.data / "fake.png"; path.write_text("not an image")
        with self.assertRaises(AgentError) as error: self.agent.save_upload([path], key="fake")
        self.assertEqual(error.exception.code, "UNSUPPORTED_MEDIA_TYPE")

    async def test_existing_collection_schema_and_rows_are_preserved(self):
        owned = self.agent.store.collection.add(fixture("owned-card.json"), cost=None)
        columns = [r[1] for r in self.agent.store.db.execute("PRAGMA table_info(copies)")]
        self.agent.close(); self.agent = MainAgent(self.data)
        self.assertEqual(self.agent.store.collection.get(owned["id"]), owned)
        self.assertEqual(columns, [r[1] for r in self.agent.store.db.execute("PRAGMA table_info(copies)")])


class FixtureActionTests(unittest.IsolatedAsyncioTestCase):
    setUp = MainAgentTests.setUp
    tearDown = MainAgentTests.tearDown
    decision = MainAgentTests.decision
    decision_value = MainAgentTests.decision_value
    async def setup_action(self):
        from fixture_market import TestMarket
        self.market = TestMarket(self.rows, self.data / "market.sqlite3")
        self.market.__enter__()
        self.addCleanup(self.market.__exit__, None, None, None)
        self.agent.trading.executor = FixtureExecutor(self.market.base)
        decision = self.decision_value(await self.decision())
        first = decision["purchase_candidates"][0]
        intent = self.agent.trading.prepare(decision["decision_id"], first["listing_ref"]["observation_id"], first["acquisition_total_jpy"], key="prepare")
        return decision, intent

    def authorize(self, intent):
        return self.agent.trading.authorize(intent["intent_id"], intent["payload_hash"], revision=intent["revision"], key="authorize")

    async def execute(self, intent, key="execute"):
        job = self.agent.create_action_job(intent["intent_id"], revision=intent["revision"], key=key)
        return await self.agent.run_job(job["job_id"])

    async def test_fixture_execution_requires_exact_authorization_and_is_idempotent(self):
        _, intent = await self.setup_action()
        with self.assertRaises(AgentError): await self.agent.trading.execute(intent["intent_id"])
        with self.assertRaises(AgentError): self.agent.trading.authorize(intent["intent_id"], "wrong", revision=intent["revision"], key="wrong-hash")
        intent = self.authorize(intent)
        job = await self.execute(intent)
        self.assertEqual(job["state"], "succeeded")
        result = self.agent.store.get_intent(intent["intent_id"])
        self.assertEqual(result["state"], "confirmed")
        self.assertTrue(result["outcome"]["receipt_ref"].startswith("TEST-"))
        self.assertEqual(result["outcome"]["ownership_state"], "purchase_pending_receipt")
        self.assertEqual(self.agent.store.collection.list(), [])
        replay = self.agent.create_action_job(intent["intent_id"], revision=intent["revision"], key="execute")
        self.assertEqual(replay["job_id"], job["job_id"])
        again = await self.agent.trading.execute(intent["intent_id"])
        self.assertEqual(again["outcome"]["receipt_ref"], result["outcome"]["receipt_ref"])
        self.assertEqual(self.agent.store.db.execute("SELECT COUNT(*) FROM main_fixture_reservations").fetchone()[0], 1)

    async def test_price_change_requires_review_and_no_submission(self):
        _, intent = await self.setup_action(); intent = self.authorize(intent)
        self.market.listings["A2"]["price_jpy"] += 1
        await self.execute(intent)
        result = self.agent.store.get_intent(intent["intent_id"])
        self.assertEqual(result["state"], "needs_user_action")
        self.assertIsNone(self.market.lookup(intent["intent_id"]))
        self.assertEqual(self.agent.store.db.execute("SELECT state FROM main_fixture_reservations").fetchone()[0], "released")

    async def test_lost_response_stays_unknown_until_independent_reconciliation(self):
        _, intent = await self.setup_action(); intent = self.authorize(intent)
        executor = self.agent.trading.executor
        submit = executor.submit
        async def lost(action):
            await submit(action)
            raise TimeoutError("simulated lost response")
        executor.submit = lost
        await self.execute(intent)
        result = self.agent.store.get_intent(intent["intent_id"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(self.agent.store.db.execute("SELECT state FROM main_fixture_reservations").fetchone()[0], "unknown")
        with self.assertRaises(AgentError): await self.agent.trading.execute(intent["intent_id"])
        result = await self.agent.trading.reconcile(intent["intent_id"])
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(self.market.lookup(intent["intent_id"])["receipt"], result["outcome"]["receipt_ref"])
        self.assertEqual(self.agent.store.db.execute("SELECT COUNT(*) FROM main_fixture_reservations").fetchone()[0], 1)

    async def test_two_intents_cannot_reserve_the_same_listing(self):
        decision, first = await self.setup_action(); first = self.authorize(first)
        candidate = decision["purchase_candidates"][0]
        second = self.agent.trading.prepare(decision["decision_id"], candidate["listing_ref"]["observation_id"], candidate["acquisition_total_jpy"], key="prepare-two")
        second = self.agent.trading.authorize(second["intent_id"], second["payload_hash"], revision=second["revision"], key="auth-two")
        outcomes = await asyncio.gather(self.agent.trading.execute(first["intent_id"]), self.agent.trading.execute(second["intent_id"]), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, AgentError) for r in outcomes), 1)
        self.assertEqual(self.agent.store.db.execute("SELECT COUNT(*) FROM main_fixture_reservations").fetchone()[0], 1)

    async def test_fixture_reservations_reduce_subsequent_plan(self):
        _, intent = await self.setup_action(); intent = self.authorize(intent)
        await self.execute(intent)
        result = self.decision_value(await self.decision())
        self.assertEqual(result["calculations"]["active_reservations_jpy"], 3900)
        self.assertLessEqual(result["calculations"]["purchase_total_jpy"], 5100)

    async def test_live_execution_is_explicitly_unavailable(self):
        decision, _ = await self.setup_action()
        candidate = decision["purchase_candidates"][0]
        with self.assertRaises(AgentError) as error:
            self.agent.trading.prepare(decision["decision_id"], candidate["listing_ref"]["observation_id"], 3900, key="live", execution_mode="live")
        self.assertEqual(error.exception.code, "FEATURE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
