"""Persisted single-user workflow from card intake through research and decision.

Live marketplace actions are deliberately outside this workflow until a supported
marketplace adapter and action-intent authorization are available. The optional
browser step executes orders only on the labeled, synthetic test marketplace.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from agent_inputs import apply_answers, goal_questions, object_fields, validate_goal
from agent_store import AgentError, AgentStore, digest, new_id, utcnow
from cards import money, validate_card
from image_agent import recognize_card
from research import ResearchUnavailable, research_market
from research_decisions import confirmed_facts, observation_key, plan_decision, validate_report
from selling import draft


INPUT_FIELDS = {"goal", "images", "reports", "facts", "fixture_listings", "asking_price_jpy", "execute_fixture"}
TERMINAL = {"succeeded", "failed", "cancelled"}


def validate_input(value):
    object_fields(value, INPUT_FIELDS, ("goal",))
    result = dict(value)
    result["goal"] = validate_goal(value["goal"])
    result.setdefault("images", [])
    result.setdefault("reports", [])
    result.setdefault("facts", {})
    result.setdefault("fixture_listings", None)
    result.setdefault("asking_price_jpy", None)
    result.setdefault("execute_fixture", False)
    if not isinstance(result["images"], list) or len(result["images"]) > 4 or any(
        not isinstance(path, str) or not path for path in result["images"]
    ):
        raise AgentError("INVALID_INPUT", "Supply at most four image paths for one card.")
    if not isinstance(result["reports"], list) or len(result["reports"]) > 10:
        raise AgentError("INVALID_INPUT", "Supply at most ten research reports.")
    if result["reports"] and result["fixture_listings"] is not None:
        raise AgentError("INVALID_INPUT", "Choose research reports or fixture listings.")
    if not isinstance(result["facts"], dict):
        raise AgentError("INVALID_INPUT", "Listing confirmations must be keyed by observation ID.")
    if result["fixture_listings"] is not None:
        rows = result["fixture_listings"]
        if not isinstance(rows, list) or len(rows) > 32:
            raise AgentError("INVALID_INPUT", "Supply at most 32 fixture listings.")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise AgentError("INVALID_INPUT", "Fixture listings need IDs.")
            validate_card(row["card"], confirmed=True)
    if result["asking_price_jpy"] is not None:
        money(result["asking_price_jpy"], "asking_price_jpy")
    if type(result["execute_fixture"]) is not bool:
        raise AgentError("INVALID_INPUT", "execute_fixture must be boolean.")
    if result["execute_fixture"] and (result["fixture_listings"] is None or result["goal"]["intent"] != "buy"):
        raise AgentError("INVALID_INPUT", "Test browser execution requires a fixture buy workflow.")
    return result


def fixture_report(rows):
    """Turn explicitly synthetic observations into the research contract."""
    timestamp = utcnow()
    run_id = new_id("fixture-research")
    observations, facts = [], {}
    for row in rows:
        raw = json.dumps(row, ensure_ascii=False, sort_keys=True)
        observation = {
            "listing_id": row["id"], "url": row.get("source_url") or "fixture://" + row["id"],
            "title": " ".join(str(row["card"][key]) for key in ("name", "set", "number")),
            "price": row.get("price_jpy"), "currency": "JPY", "shipping": row.get("shipping_jpy"),
            "total_price": None, "condition": row.get("condition") or "unknown",
            "availability": row.get("availability", "unknown"), "observed_at": timestamp,
            "verification": "verified_listing", "qualifies": False, "exclusion_reasons": [],
            "evidence": [{"source_url": row.get("source_url") or "fixture://" + row["id"],
                          "observed_at": timestamp, "kind": "direct_page", "content": raw,
                          "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}],
            "field_evidence": {"listing": raw},
        }
        observations.append(observation)
        condition = observation["condition"]
        sidecar = {"card": row["card"], "quantity": row.get("quantity", 1),
                   "purchase_fee_jpy": row.get("purchase_fee_jpy")}
        if condition in {"near_mint", "unused", "played", "damaged"}:
            sidecar["condition_record"] = {"condition_raw": condition,
                "condition_normalized": condition, "condition_basis": "user_confirmed"}
        fact = confirmed_facts(run_id, observation, sidecar, fixture=True)
        facts[fact["fact_id"]] = fact
    report = {"schema_version": "1.0", "run_id": run_id, "status": "complete",
              "request": {"confirmed_target": {"name_ja": "synthetic fixture", "number": "fixture"}},
              "started_at": timestamp, "completed_at": timestamp, "listings": observations,
              "comparable_offers": [], "lowest_qualifying_observed_price": None,
              "findings": [], "uncertainty": ["Synthetic test data; no live marketplace claim."],
              "coverage": {"mode": "fixture", "observed": len(rows)}, "failures": [],
              "suggested_next_steps": [], "log_path": ""}
    return validate_report(report), facts


class Pipeline:
    """Synchronous application service. Callers may resume queued jobs after restart."""

    def __init__(self, data_dir, *, identify=recognize_card, research=research_market,
                 fixture_executor=None):
        self.store = AgentStore(data_dir)
        self.identify = identify
        self.research = research
        self.fixture_executor = fixture_executor

    def close(self):
        self.store.close()

    def get(self, job_id):
        return self.store.get_job(job_id)

    def result(self, job_id):
        job = self.get(job_id)
        if not job["result"]:
            return None
        ref = job["result"]
        return self.store.read({"decision": "decisions", "sale_draft": "drafts"}[ref["kind"]], ref["id"])

    def start(self, request, *, idempotency_key=None):
        request = validate_input(request)
        key = idempotency_key or new_id("request")
        with self.store.transaction():
            previous = self.store.replay("pipeline.start", key, request)
            if previous:
                return self.get(previous["job_id"])
            timestamp = utcnow()
            job = {"job_id": new_id("job"), "type": "decision", "state": "queued", "revision": 0,
                   "created_at": timestamp, "updated_at": timestamp, "stage": "queued",
                   "progress": {"message": "Queued", "completed_steps": 0, "total_steps": 4},
                   "questions": [], "result": None, "error": None}
            self.store.create_job(job, request, {})
            self.store.remember("pipeline.start", key, request, {"job_id": job["job_id"]})
        return self.run(job["job_id"])

    def answer(self, job_id, answers, *, revision):
        with self.store.transaction():
            job = self.get(job_id)
            self.store.require_revision(job, revision)
            if job["state"] != "awaiting_input":
                raise AgentError("INVALID_STATE", "Job is not waiting for answers.")
            request, checkpoint = self.store.private_job(job_id)
            request["goal"] = apply_answers(request["goal"], job["questions"], answers)
            job["state"], job["stage"], job["questions"] = "queued", "queued", []
            self.store.put_job(job, input_value=request, checkpoint=checkpoint)
        return self.run(job_id)

    def _update(self, job, checkpoint, state, stage, message, completed, *, result=None, questions=None, error=None):
        job.update(state=state, stage=stage, progress={"message": message,
            "completed_steps": completed, "total_steps": 4}, questions=questions or [], error=error)
        if result is not None:
            job["result"] = result
        with self.store.transaction():
            self.store.put_job(job, checkpoint=checkpoint)

    def _artifact(self, kind, record_id, value):
        with self.store.transaction():
            self.store.artifact(kind, record_id, value)

    def _collection_snapshot(self, fixture):
        copies = self.store.collection.list()
        reservations = 0
        if fixture:
            reservations = sum(action["total"] for action in self.store.collection.actions()
                               if action["state"] in {"reserved", "unknown", "confirmed"})
        snapshot = {"copies": copies, "active_reservations_jpy": reservations,
                    "cash_status": "unknown", "fixture": fixture}
        snapshot["snapshot_id"] = "snapshot-" + digest(snapshot)
        self._artifact("snapshots", snapshot["snapshot_id"], snapshot)
        return snapshot

    def run(self, job_id):
        job = self.get(job_id)
        if job["state"] in TERMINAL or job["state"] == "awaiting_input":
            return job
        if job["state"] != "queued":
            raise AgentError("JOB_INTERRUPTED", "A running job cannot be submitted again.")
        request, checkpoint = self.store.private_job(job_id)
        try:
            self._update(job, checkpoint, "running", "identify", "Resolving card identity", 0)
            if request["images"] and "candidate_id" not in checkpoint:
                candidate = self.identify(request["images"])
                if not candidate.get("ok"):
                    failure = candidate.get("error") or {}
                    raise AgentError(failure.get("code", "IDENTIFICATION_FAILED"),
                                     failure.get("message", "Card identification failed."),
                                     retryable=failure.get("retryable", False))
                candidate_id = new_id("candidate")
                self._artifact("candidates", candidate_id, candidate)
                checkpoint["candidate_id"] = candidate_id
                self._update(job, checkpoint, "running", "identify", "Candidate saved for confirmation", 1)
            snapshot = self._collection_snapshot(request["fixture_listings"] is not None)
            questions = goal_questions(request["goal"], snapshot["copies"])
            if questions:
                self._update(job, checkpoint, "awaiting_input", "clarify", "Confirm missing goal fields", 1,
                             questions=questions)
                return job
            self._update(job, checkpoint, "running", "research", "Collecting bounded evidence", 1)
            reports = []
            facts = {}
            source_modes = {}
            if request["goal"]["intent"] != "sell":
                if request["fixture_listings"] is not None:
                    report, fixture_facts = fixture_report(request["fixture_listings"])
                    reports.append(report)
                    facts.update(fixture_facts)
                    source_modes[report["run_id"]] = "fixture"
                elif request["reports"]:
                    for report in request["reports"]:
                        reports.append(validate_report(report))
                        source_modes[report["run_id"]] = "trusted_report"
                else:
                    for target in request["goal"]["targets"]:
                        card = target["card"]
                        query = {"confirmed_target": {"name_ja": card["name"], "number": card["number"]},
                                 "currency": "JPY", "raw_only": card["presentation"] == "raw",
                                 "price_basis": "delivered_price", "source_limits": {
                                     "allowed_domains": ["jp.mercari.com", "item.fril.jp"],
                                     "max_searches": 2, "max_inspections": 4,
                                     "results_per_search": 5, "timeout_seconds": 120}}
                        reports.append(validate_report(asyncio.run(self.research(query))))
                        source_modes[reports[-1]["run_id"]] = "live_research"
                for report in reports:
                    self._artifact("research", report["run_id"], report)
            # A caller's sidecar is validated against the exact saved observation.
            # A supplied fact ID or model text alone cannot mark evidence confirmed.
            matched = set()
            for report in reports:
                for row in report["listings"]:
                    key = observation_key(report["run_id"], row)
                    if key in request["facts"]:
                        facts[key] = confirmed_facts(report["run_id"], row, request["facts"][key])
                        matched.add(key)
            if set(request["facts"]) != matched:
                raise AgentError("INVALID_INPUT", "Listing confirmations must match current observation IDs.")
            checkpoint["research_run_ids"] = [report["run_id"] for report in reports]
            self._update(job, checkpoint, "running", "decision", "Applying purchase rules", 2)
            decision = plan_decision(request["goal"], snapshot, reports, facts, source_modes)
            self._artifact("decisions", decision["decision_id"], decision)
            checkpoint["decision_id"] = decision["decision_id"]
            ref = {"kind": "decision", "id": decision["decision_id"],
                   "url": None}
            self._update(job, checkpoint, "running", "decision", "Decision saved", 3, result=ref)
            if request["goal"]["intent"] == "sell" and request["asking_price_jpy"] is not None:
                copy_id = request["goal"]["copy_ids"][0]
                sale = draft(self.store.collection.get(copy_id), request["asking_price_jpy"])
                draft_id = new_id("draft")
                self._artifact("drafts", draft_id, sale)
                ref = {"kind": "sale_draft", "id": draft_id, "url": None}
            if request["execute_fixture"]:
                if checkpoint.get("fixture_execution_started"):
                    raise AgentError("ACTION_UNRESOLVED", "Fixture action was already started; inspect its ledger.")
                checkpoint["fixture_execution_started"] = True
                self._update(job, checkpoint, "running", "execute", "Executing test-market orders", 3)
                executor = self.fixture_executor
                if executor is None:
                    from fixture_market import run_browser_fixture
                    executor = run_browser_fixture
                selected = [item["listing_ref"]["listing_id"] for item in decision["purchase_candidates"]]
                execution = executor(request["goal"], request["fixture_listings"], selected,
                                     self.store.root / "fixture_browser" / job_id)
                self._artifact("snapshots", "execution-" + job_id, execution)
                checkpoint["fixture_execution_id"] = "execution-" + job_id
            self._update(job, checkpoint, "succeeded", "finished", "Workflow complete", 4, result=ref)
            return job
        except Exception as exc:
            if isinstance(exc, AgentError):
                error = exc.as_dict()
            elif isinstance(exc, ResearchUnavailable):
                error = AgentError("RESEARCH_UNAVAILABLE", str(exc), retryable=True).as_dict()
            else:
                error = AgentError("PIPELINE_FAILED", "Workflow failed; inspect private runtime diagnostics.").as_dict()
            self._update(job, checkpoint, "failed", "finished", "Workflow failed", 0, error=error)
            return job
