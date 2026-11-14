"""Durable local jobs call existing image, research, decision and draft adapters."""
import asyncio
import copy
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from cards import validate_card
from decisions import plan
from fixture_market import TestMarket
from image_agent import recognize_card
from research import research_market
from selling import draft

from .models import Card, DecisionInput, ResearchRequest
from .store import APIError, Store, encoded, now, uid

TERMINAL = {"succeeded", "failed", "cancelled"}


@dataclass
class Settings:
    data_dir: Path = Path("data")
    identify_enabled: bool = False
    research_enabled: bool = False
    fixture_listings: list[dict] = field(default_factory=list)
    identify: Callable = recognize_card
    research: Callable[..., Awaitable[dict]] = research_market
    job_timeout: float = 180
    allowed_origins: list[str] = field(default_factory=list)
    main_agent: Callable | None = None


def new_job(owner: str, kind: str, inputs: dict) -> dict:
    return {"job_id": uid("job"), "type": kind, "state": "queued", "revision": 1,
            "created_at": now(), "updated_at": now(), "stage": "queued",
            "progress": {"message": "Waiting for a worker.", "completed_steps": 0, "total_steps": None},
            "questions": [], "result": None, "error": None, "_input": inputs,
            "_owner": owner, "_created_ts": time.time()}


def public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def question(field: str, prompt: str, value_type: str, choices: list[str] | None = None) -> dict:
    return {"question_id": uid("question"), "field": field, "prompt": prompt,
            "value_type": value_type, "choices": choices, "required": True}


class Worker:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store, self.settings = store, settings
        self.activity_context = None

    async def run_research(self, job: dict, request: dict) -> dict:
        if self.settings.research is research_market and self.activity_context:
            return await self.settings.research(request, activity_context=self.activity_context(job))
        return await self.settings.research(request)

    async def loop(self) -> None:
        while True:
            claimed = None
            with self.store.transaction() as db:
                for row in db.execute("SELECT owner,data FROM api_records WHERE kind='job' ORDER BY created,id"):
                    job = json.loads(row["data"])
                    if job["state"] == "cancel_requested":
                        self.store.save_job(db, row["owner"], job, "cancelled")
                    elif job["state"] == "awaiting_input" and time.time() - job.get("_waiting_at", time.time()) > 86400:
                        job["error"] = APIError(409, "INPUT_EXPIRED", "The clarification expired.").error
                        self.store.save_job(db, row["owner"], job, "failed")
                    elif job["state"] == "queued" and claimed is None:
                        job["stage"] = {"sale_draft": "draft"}.get(job["type"], job["type"])
                        job["progress"]["message"] = "Running " + job["stage"] + "."
                        self.store.save_job(db, row["owner"], job, "running")
                        claimed = (row["owner"], job)
            if claimed is None:
                await asyncio.sleep(0.1)
                continue
            owner, job = claimed
            try:
                await asyncio.wait_for(self.perform(owner, job), timeout=self.settings.job_timeout)
            except asyncio.CancelledError:
                self.fail(owner, job["job_id"], "JOB_INTERRUPTED", "Worker stopped; completed evidence was retained.")
                raise
            except TimeoutError:
                self.fail(owner, job["job_id"], "JOB_TIMEOUT", "The job reached its time limit.")
            except APIError as exc:
                self.fail(owner, job["job_id"], exc.error["code"], exc.error["message"])
            except Exception:
                self.fail(owner, job["job_id"], "PROVIDER_UNAVAILABLE", "The operation failed. Check backend diagnostics/configuration.")

    def fail(self, owner: str, job_id: str, code: str, message: str) -> None:
        with self.store.transaction() as db:
            job = self.store.get(db, owner, "job", job_id)
            if job["state"] not in TERMINAL:
                job["error"] = APIError(500, code, message, retryable=code in {"JOB_TIMEOUT", "PROVIDER_UNAVAILABLE"}).error
                self.store.save_job(db, owner, job, "cancelled" if job["state"] == "cancel_requested" else "failed")

    def finish(self, owner: str, job_id: str, kind: str, key: str, result: dict) -> None:
        with self.store.transaction() as db:
            job = self.store.get(db, owner, "job", job_id)
            self.store.put(db, owner, kind, key, result)
            paths = {"candidate": "candidates", "research_report": "research-reports", "decision": "decisions",
                     "sale_draft": "sale-drafts", "action_intent": "action-intents"}
            job["result"] = {"kind": kind, "id": key, "url": f"/api/v1/{paths[kind]}/{key}"}
            job["progress"] = {"message": "Result saved.", "completed_steps": 1, "total_steps": 1}
            self.store.save_job(db, owner, job, "cancelled" if job["state"] == "cancel_requested" else "succeeded")

    async def perform(self, owner: str, job: dict) -> None:
        inputs = job["_input"]
        if job["type"] == "identify":
            with self.store.transaction() as db:
                upload = self.store.get(db, owner, "upload", inputs["upload_id"])
                paths = [str(self.store.root / self.store.get(db, owner, "asset", image["asset_id"])["_path"])
                         for image in upload["images"]]
            result = await asyncio.to_thread(self.settings.identify, paths)
            if not result.get("ok"):
                error = result.get("error", {})
                raise APIError(503, error.get("code", "IDENTIFICATION_FAILED"), error.get("message", "Image identification failed."))
            card = validate_card(result["card"])
            candidate_id = uid("candidate")
            candidate = {"candidate_id": candidate_id, "upload_id": upload["upload_id"], "card": card,
                         "candidate_complete": result.get("candidate_complete", False),
                         "missing_fields": result.get("missing_fields", []), "requires_confirmation": True,
                         "observed_at": result.get("observed_at", now()),
                         **{k: result.get(k) for k in ("provider", "configured_model", "returned_model")}}
            self.finish(owner, job["job_id"], "candidate", candidate_id, candidate)
        elif job["type"] == "research":
            report = await self.run_research(job, inputs["request"])
            self.save_report(owner, report)
            if report.get("status") == "failed":
                raise APIError(502, "RESEARCH_FAILED", "Research failed; its evidence report was retained.")
            self.finish(owner, job["job_id"], "research_report", report["run_id"], report)
        elif job["type"] == "decision":
            await self.make_decision(owner, job)
        elif job["type"] == "sale_draft":
            with self.store.transaction() as db:
                owned = self.store.get(db, owner, "copy", inputs["copy_id"])
            result = draft(owned, inputs["asking_price_jpy"])
            key = uid("draft")
            self.finish(owner, job["job_id"], "sale_draft", key,
                        {**result, "draft_id": key, "revision": 1, "created_at": now()})
        elif job["type"] in {"execute", "reconcile"}:
            await self.execute_fixture(owner, job)

    def save_report(self, owner: str, report: dict) -> None:
        report.pop("log_path", None)
        if report.get("schema_version") != "1.0" or not isinstance(report.get("run_id"), str):
            raise APIError(502, "INVALID_RESPONSE", "Research returned an unsupported report.")
        # IDs from the external service never become path components.
        with self.store.transaction() as db:
            for listing in report.get("listings", []):
                for evidence in listing.get("evidence", []):
                    key = uid("evidence")
                    self.store.put(db, owner, "evidence", key, evidence)
                    evidence["evidence_url"] = f"/api/v1/evidence/{key}"
            self.store.put(db, owner, "research_report", report["run_id"], report)

    async def make_decision(self, owner: str, job: dict) -> None:
        inputs = DecisionInput.model_validate(job["_input"]).model_dump()
        goal = inputs["goal"]
        questions = []
        if not goal["targets"] and goal["intent"] != "sell":
            questions.append(question("target_card.0", "Confirm the exact card printing.", "card"))
        if goal["intent"] != "sell" and goal["budget_jpy"] is None:
            questions.append(question("budget_jpy", "What is your purchase budget in JPY?", "integer"))
        if goal["intent"] != "sell" and not goal["allowed_conditions"]:
            questions.append(question("allowed_conditions", "Which conditions do you accept?", "condition_list",
                                      ["near_mint", "unused", "played", "damaged"]))
        if goal["intent"] == "sell" and not goal["copy_ids"]:
            questions.append(question("copy_id", "Which owned copy do you want to sell?", "string"))
        for index, target in enumerate(goal["targets"]):
            try:
                validate_card(target["card"], True)
            except ValueError:
                questions.append(question(f"target_card.{index}", "Correct the incomplete card identity.", "card"))
                break
        if questions:
            with self.store.transaction() as db:
                current = self.store.get(db, owner, "job", job["job_id"])
                if current["state"] == "cancel_requested":
                    self.store.save_job(db, owner, current, "cancelled")
                else:
                    current["questions"], current["stage"], current["_waiting_at"] = questions, "clarify", time.time()
                    self.store.save_job(db, owner, current, "awaiting_input")
            return
        with self.store.transaction() as db:
            copies = self.store.rows(db, owner, "copy")
            for copy_id in goal["copy_ids"]:
                self.store.get(db, owner, "copy", copy_id)
            reports = [self.store.get(db, owner, "research_report", key) for key in inputs["research_run_ids"]]
        if not reports and not self.settings.fixture_listings and goal["intent"] != "sell":
            if not self.settings.research_enabled:
                raise APIError(409, "FEATURE_UNAVAILABLE", "Research is not configured; provide a saved report.")
            for target in goal["targets"]:
                card = target["card"]
                report = await self.run_research(job, ResearchRequest.model_validate({
                    "confirmed_target": {"name_ja": card["name"], "number": card["number"], "language": card["language"]}
                }).model_dump(exclude_none=True))
                self.save_report(owner, report)
                reports.append(report)
                with self.store.transaction() as db:
                    current = self.store.get(db, owner, "job", job["job_id"])
                    current["_input"]["research_run_ids"].append(report["run_id"])
                    self.store.put(db, owner, "job", job["job_id"], current)
        if not self.settings.fixture_listings:
            from .main_agent import decide, MainAgentNeedsInput
            with self.store.transaction() as db:
                reservations = db.execute("SELECT COALESCE(SUM(total),0) FROM api_reservations WHERE owner=? AND state='reserved'", (owner,)).fetchone()[0]
                facts = self.store.rows(db, owner, "listing_fact")
            if self.settings.main_agent:
                decision = await asyncio.to_thread(self.settings.main_agent, goal, copies, reports, reservations)
            else:
                from agent_store import AgentError
                from .store import digest
                try:
                    decision = await asyncio.to_thread(decide, goal, copies, reports, reservations,
                        data_dir=self.store.root / "main-agent" / digest(owner),
                        job_id=job["job_id"] + ":r" + str(job["revision"]), facts=facts)
                except MainAgentNeedsInput as exc:
                    with self.store.transaction() as db:
                        current = self.store.get(db, owner, "job", job["job_id"])
                        if current["state"] == "cancel_requested":
                            self.store.save_job(db, owner, current, "cancelled")
                        else:
                            current["questions"], current["stage"], current["_waiting_at"] = exc.questions, "clarify", time.time()
                            self.store.save_job(db, owner, current, "awaiting_input")
                    return
                except AgentError as exc:
                    raise APIError(502, exc.code, str(exc), retryable=exc.retryable) from None
            decision["goal_snapshot"] = goal
            decision["collection_snapshot"] = copies
            decision["source_mode"] = "research_reports"
            self.finish(owner, job["job_id"], "decision", decision["decision_id"], decision)
            return
        # The current research schema does not establish complete set/variant identity
        # or purchase fees. Keep those observations as excluded evidence, not guessed offers.
        listings = copy.deepcopy(self.settings.fixture_listings)
        legacy_goal = {**goal, "budget_jpy": goal["budget_jpy"] or 0,
                       "allowed_conditions": goal["allowed_conditions"] or ["near_mint"],
                       "source_mode": "synthetic_fixture" if listings else "research_reports"}
        result = plan(legacy_goal, listings, copies) if goal["intent"] != "sell" else {"selected_ids": [], "checked": []}
        sales = [c for c in copies if c["id"] in goal["copy_ids"] and c["allow_sale"] and c["state"] == "owned"]
        key = uid("decision")
        recommendation = ("sell" if sales and goal["intent"] == "sell" else "buy" if result["selected_ids"]
                          else "insufficient_evidence" if reports or goal["intent"] == "sell" else "hold")
        decision = {"decision_id": key, "created_at": now(), "rule_version": "api-1.0",
                    "goal_ref": job["job_id"], "collection_ref": "embedded_snapshot",
                    "research_refs": [r["run_id"] for r in reports], "recommendation": recommendation,
                    "purchase_candidates": [r for r in listings if r["id"] in result["selected_ids"]],
                    "sale_candidates": [{"copy_id": c["id"]} for c in sales], "calculations": result,
                    "rejections": result["checked"], "rationale": "Deterministic matching; no forecast or trade performed.",
                    "requirements": ([] if result["selected_ids"] or sales else
                                     ["Choose an owned copy and enable explicit sale permission."] if goal["intent"] == "sell" else
                                     ["Complete printing identity, condition and acquisition costs must be evidenced."]),
                    "evidence_refs": [r["run_id"] for r in reports], "goal_snapshot": goal,
                    "collection_snapshot": copies,
                    "source_mode": legacy_goal["source_mode"]}
        if reports:
            decision["rejections"].extend({"id": listing["listing_id"], "eligible": False,
                                           "reasons": ["FULL_PRINTING_OR_FEE_EVIDENCE_MISSING"]}
                                          for report in reports for listing in report.get("listings", []))
        self.finish(owner, job["job_id"], "decision", key, decision)

    async def execute_fixture(self, owner: str, job: dict) -> None:
        intent_id = job["_input"]["intent_id"]
        with self.store.transaction() as db:
            intent = self.store.get(db, owner, "action_intent", intent_id)
            if job["type"] == "execute":
                if intent["authorization_state"] != "authorized" or intent["_expiry"] <= time.time():
                    raise APIError(409, "INTENT_EXPIRED", "Authorization expired before execution.")
                intent["state"] = "submitted"
                intent["revision"] += 1
                self.store.put(db, owner, "action_intent", intent_id, intent)
        def transact() -> dict:
            import urllib.request
            with TestMarket(self.settings.fixture_listings, self.store.root / "fixture-market.sqlite3") as market:
                if job["type"] == "execute":
                    body = encoded({"intent": intent_id, "listing": intent["listing_ref"],
                                    "ceiling": intent["limits"]["max_total_jpy"]}).encode()
                    request = urllib.request.Request(market.base + "/purchase", data=body,
                                                     headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=10) as response:
                        json.load(response)
                # Verify through the independent order-read endpoint.
                with urllib.request.urlopen(market.base + "/order/" + intent_id, timeout=10) as response:
                    return json.load(response)
        try:
            receipt = await asyncio.to_thread(transact)
            outcome = {"status": "confirmed", "observed_at": now(), "receipt_ref": receipt["receipt"],
                       "actual_total_jpy": receipt["total"], "evidence_refs": [], "error": None,
                       "next_step": None, "execution_mode": "fixture"}
        except Exception:
            outcome = {"status": "unknown", "observed_at": now(), "receipt_ref": None,
                       "actual_total_jpy": None, "evidence_refs": [], "error": None,
                       "next_step": "Reconcile this intent before any resubmission.", "execution_mode": "fixture"}
        with self.store.transaction() as db:
            intent = self.store.get(db, owner, "action_intent", intent_id)
            intent["outcome"], intent["state"] = outcome, outcome["status"]
            intent["revision"] += 1
            self.store.put(db, owner, "action_intent", intent_id, intent)
        self.finish(owner, job["job_id"], "action_intent", intent_id, intent)
