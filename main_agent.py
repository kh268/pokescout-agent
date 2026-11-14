"""Persistent main agent coordinating existing image, research, decision and trading roles.

The application is a trusted, local, single-user Python boundary. It deliberately
does not expose the frontend contract as an unauthenticated HTTP server.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from agent_inputs import apply_answers, goal_questions, object_fields, validate_goal, card_input
from agent_store import AgentError, AgentStore, digest, encoded, new_id, utcnow
from agent_trading import Trading
from cards import money
from image_agent import recognize_card
from research import ResearchUnavailable, research_market
from research_decisions import confirmed_facts, observation_key, parse_time, plan_decision, validate_report
from selling import draft

TERMINAL = {"succeeded", "failed", "cancelled"}
RESULT_PATHS = {"candidate": "candidates", "research_report": "research-reports", "decision": "decisions",
                "sale_draft": "sale-drafts", "action_intent": "action-intents"}


class JobCancelled(Exception):
    pass


def validate_research_request(value):
    object_fields(value, {"confirmed_target", "source_limits", "currency", "raw_only", "accepted_conditions", "price_basis", "objective"}, ("confirmed_target",))
    request = json.loads(encoded(value))
    target = request["confirmed_target"]
    object_fields(target, {"name_ja", "number", "rarity", "language"}, ("name_ja", "number"))
    if any(not isinstance(v, str) or not 1 <= len(v.strip()) <= 300 for v in target.values()):
        raise AgentError("INVALID_INPUT", "Research target fields must be bounded strings.")
    if request.get("currency", "JPY") != "JPY" or type(request.get("raw_only", True)) is not bool:
        raise AgentError("INVALID_INPUT", "Research supports explicit JPY and boolean raw_only.")
    if request.get("price_basis", "item_price") not in {"item_price", "delivered_price"}:
        raise AgentError("INVALID_INPUT", "Invalid research price basis.")
    conditions = request.get("accepted_conditions", [])
    if not isinstance(conditions, list) or len(conditions) > 10 or any(not isinstance(c, str) or len(c) > 100 for c in conditions):
        raise AgentError("INVALID_INPUT", "Invalid research condition filter.")
    if not isinstance(request.get("objective", ""), str) or len(request.get("objective", "")) > 1000:
        raise AgentError("INVALID_INPUT", "Research objective must be bounded text.")
    limits = request.setdefault("source_limits", {})
    object_fields(limits, {"allowed_domains", "max_searches", "max_inspections", "results_per_search", "timeout_seconds"})
    bounds = {"max_searches": (3, 1, 4), "max_inspections": (4, 0, 6),
              "results_per_search": (5, 1, 8), "timeout_seconds": (120, 15, 165)}
    for field, (default, minimum, maximum) in bounds.items():
        number = limits.setdefault(field, default)
        if type(number) is not int or not minimum <= number <= maximum:
            raise AgentError("INVALID_INPUT", "Research source limits exceed the application budget.")
    domains = limits.setdefault("allowed_domains", ["jp.mercari.com", "item.fril.jp"])
    if not isinstance(domains, list) or not 1 <= len(domains) <= 2 or any(d not in {"jp.mercari.com", "item.fril.jp"} for d in domains):
        raise AgentError("INVALID_INPUT", "Only the contracted research marketplaces are supported.")
    return request


class MainAgent:
    def __init__(self, data_dir=None, *, researcher=None, recognizer=None, executor=None,
                 job_timeout_seconds=180, evidence_max_age_seconds=900):
        # Resolve the default relative to the repository, never the caller's cwd.
        default = Path(__file__).resolve().parent / "data"
        self.store = AgentStore(data_dir or os.getenv("POKESCOUT_DATA_DIR") or default)
        self.researcher = researcher or research_market
        self.recognizer = recognizer or recognize_card
        self.research_configured = researcher is not None or bool(os.getenv("RESEARCH_AGENT_URL"))
        if type(job_timeout_seconds) is not int or not 1 <= job_timeout_seconds <= 180:
            self.store.close()
            raise ValueError("Active job timeout must be 1–180 seconds.")
        if type(evidence_max_age_seconds) is not int or not 1 <= evidence_max_age_seconds <= 3600:
            self.store.close()
            raise ValueError("Evidence freshness must be 1–3600 seconds.")
        self.job_timeout_seconds = job_timeout_seconds
        self.evidence_max_age_seconds = evidence_max_age_seconds
        self.trading = Trading(self.store, executor)

    def close(self):
        self.store.close()

    def capabilities(self):
        try:
            vision = bool(os.getenv("GEMINI_API_KEY")) and importlib.util.find_spec("google.genai") is not None and importlib.util.find_spec("PIL") is not None
        except (ImportError, ModuleNotFoundError):
            vision = False
        return {"mode": "local_single_user", "features": {"identify": vision, "research": self.research_configured,
                "decision": True, "sale_draft": True, "fixture_execution": self.trading.executor is not None,
                "live_execution": False, "watch": False, "forecast": False},
                "limits": {"max_views_per_upload": 4, "max_upload_bytes": 12_000_000, "max_active_jobs": 2,
                           "job_timeout_seconds": self.job_timeout_seconds}}

    def save_upload(self, images, *, roles=None, key):
        """Retain validated local image bytes; HTTP multipart transport belongs above this API."""
        if not isinstance(images, (list, tuple)) or not 1 <= len(images) <= 4:
            raise AgentError("INVALID_INPUT", "Supply 1–4 views of one physical card.")
        roles = roles or ["front"] + ["other"] * (len(images) - 1)
        if len(roles) != len(images) or any(r not in {"front", "back", "slab_label", "detail", "other"} for r in roles):
            raise AgentError("INVALID_INPUT", "Each image needs a supported role.")
        try:
            from PIL import Image
        except ImportError:
            raise AgentError("FEATURE_UNAVAILABLE", "Install requirements-vision.txt to validate retained image bytes.") from None
        assets, total = [], 0
        for path, role in zip(images, roles):
            with Path(path).open("rb") as stream:
                raw = stream.read(12_000_001 - total)
            total += len(raw)
            if total > 12_000_000:
                raise AgentError("UPLOAD_TOO_LARGE", "Image group exceeds 12 MB.")
            try:
                with Image.open(BytesIO(raw)) as image:
                    if image.width * image.height > 25_000_000:
                        raise AgentError("UPLOAD_TOO_LARGE", "Image exceeds the decoded pixel limit.")
                    format_name = image.format
                    image.verify()
                with Image.open(BytesIO(raw)) as image:
                    image.load()
            except AgentError:
                raise
            except Exception:
                raise AgentError("UNSUPPORTED_MEDIA_TYPE", "Use valid JPEG, PNG or WebP image bytes.") from None
            extension, mime = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp")}.get(format_name, (None, None))
            if not extension:
                raise AgentError("UNSUPPORTED_MEDIA_TYPE", "Use JPEG, PNG or WebP.")
            sha = hashlib.sha256(raw).hexdigest()
            self.store.save_bytes(f"images/{sha}.{extension}", raw)
            assets.append({"asset_id": sha, "sha256": sha, "role": role, "mime_type": mime,
                           "size_bytes": len(raw), "url": "/api/v1/assets/" + sha,
                           "relative_path": f"images/{sha}.{extension}"})
        request = [{"sha256": a["sha256"], "role": a["role"]} for a in assets]
        with self.store.transaction():
            replay = self.store.replay("upload", key, request)
            if replay:
                return self.store.read("uploads", replay["upload_id"])
            upload = {"schema_version": "1.0", "upload_id": new_id("upload"), "created_at": utcnow(),
                      "images": [{k: v for k, v in a.items() if k != "relative_path"} for a in assets]}
            for asset in assets:
                self.store.db.execute("INSERT OR IGNORE INTO main_assets VALUES(?,?,?,?,?)",
                    (asset["sha256"], asset["relative_path"], asset["mime_type"], asset["size_bytes"], upload["created_at"]))
            self.store.artifact("uploads", upload["upload_id"], upload)
            self.store.remember("upload", key, request, {"upload_id": upload["upload_id"]})
            return upload

    def _enqueue(self, job_type, value, key, scope="job"):
        with self.store.transaction():
            request = {"type": job_type, "input": value}
            replay = self.store.replay(scope, key, request)
            if replay:
                return self.store.get_job(replay["job_id"])
            active = self.store.db.execute("SELECT COUNT(*) FROM main_jobs WHERE json_extract(body,'$.state') NOT IN ('succeeded','failed','cancelled')").fetchone()[0]
            if active >= 2:
                raise AgentError("JOB_LIMIT_REACHED", "Two jobs are already active; finish or cancel one first.")
            stamp = utcnow()
            job = {"job_id": new_id("job"), "type": job_type, "state": "queued", "revision": 1,
                   "created_at": stamp, "updated_at": stamp, "stage": "queued",
                   "progress": {"message": "Waiting for a worker.", "completed_steps": 0, "total_steps": None},
                   "questions": [], "result": None, "error": None}
            self.store.db.execute("INSERT INTO main_jobs VALUES(?,?,?,?)", (job["job_id"], encoded(job), encoded(value), "{}"))
            self.store.remember(scope, key, request, {"job_id": job["job_id"]})
            return job

    def create_job(self, job_type, value, *, key):
        if job_type == "identify":
            object_fields(value, {"upload_id"}, ("upload_id",))
            self.store.read("uploads", value["upload_id"])
        elif job_type == "research":
            object_fields(value, {"request"}, ("request",))
            value = {"request": validate_research_request(value["request"])}
        elif job_type == "decision":
            object_fields(value, {"goal", "research_run_ids"}, ("goal",))
            ids = value.get("research_run_ids", [])
            if not isinstance(ids, list) or len(ids) > 10 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                raise AgentError("INVALID_INPUT", "Supply at most ten distinct owned research run IDs.")
            for run_id in ids:
                self.store.read("research", run_id)
            value = {"goal": validate_goal(value["goal"]), "research_run_ids": ids}
        else:
            raise AgentError("INVALID_INPUT", "Client jobs must be identify, research or decision.")
        return self._enqueue(job_type, value, key)

    def create_sale_draft(self, copy_id, asking_price_jpy, *, key, decision_id=None):
        money(asking_price_jpy, "asking_price_jpy")
        copy = self.store.collection.get(copy_id)
        if not copy["allow_sale"] or copy["state"] != "owned":
            raise AgentError("INVALID_INPUT", "This copy is protected or unavailable for sale.")
        if decision_id:
            decision = self.store.read("decisions", decision_id)
            if copy_id not in [c["copy_id"] for c in decision["sale_candidates"]]:
                raise AgentError("INVALID_INPUT", "This copy is not a sale candidate in that decision.")
        return self._enqueue("sale_draft", {"copy_id": copy_id, "asking_price_jpy": asking_price_jpy, "decision_id": decision_id}, key, "sale-draft")

    def create_action_job(self, intent_id, *, revision, key, reconcile=False):
        scope = "reconcile" if reconcile else "execute"
        value = {"intent_id": intent_id}
        # Replay before revision checks, including after the original job completes.
        replay = self.store.replay(scope, key, {"type": scope, "input": value})
        if replay:
            return self.store.get_job(replay["job_id"])
        intent = self.store.get_intent(intent_id)
        self.store.require_revision(intent, revision)
        if not reconcile and intent["state"] not in {"authorized", "confirmed"}:
            raise AgentError("INVALID_STATE", "Authorize first, or reconcile a potentially submitted intent.")
        return self._enqueue(scope, value, key, scope)

    def get_job(self, job_id):
        return self.store.get_job(job_id)

    def list_jobs(self):
        return [json.loads(r[0]) for r in self.store.db.execute("SELECT body FROM main_jobs ORDER BY json_extract(body,'$.created_at'),id")]

    def answers(self, job_id, answers, *, revision, key):
        request = {"job_id": job_id, "answers": answers}
        with self.store.transaction():
            replay = self.store.replay("answers", key, request)
            job = self.store.get_job(job_id)
            if replay:
                return job
            self.store.require_revision(job, revision)
            if job["state"] != "awaiting_input":
                raise AgentError("INVALID_STATE", "This job is not waiting for answers.")
            if (datetime.now(timezone.utc) - parse_time(job["updated_at"])).total_seconds() > 86400:
                raise AgentError("INPUT_EXPIRED", "The clarification window has expired.")
            value, _ = self.store.private_job(job_id)
            value["goal"] = apply_answers(value["goal"], job["questions"], answers)
            job.update(state="queued", stage="queued", questions=[])
            self.store.put_job(job, input_value=value)
            self.store.remember("answers", key, request, {"job_id": job_id})
            return job

    def cancel(self, job_id, *, revision, key):
        request = {"job_id": job_id}
        with self.store.transaction():
            replay = self.store.replay("cancel", key, request)
            job = self.store.get_job(job_id)
            if replay:
                return job
            self.store.require_revision(job, revision)
            if job["state"] in TERMINAL:
                raise AgentError("INVALID_STATE", "The job is already finished.")
            value, _ = self.store.private_job(job_id)
            if job["type"] in {"execute", "reconcile"}:
                intent = self.store.get_intent(value["intent_id"])
                if intent["state"] in {"reserved", "submitting", "unknown", "confirmed"}:
                    raise AgentError("ACTION_ALREADY_SUBMITTED", "Do not cancel potentially submitted actions; reconcile the intent.")
            job.update(state="cancel_requested", questions=[])
            self.store.put_job(job)
            self.store.remember("cancel", key, request, {"job_id": job_id})
            return job

    def import_research(self, report, *, source_mode="replay"):
        """Explicit import: never relocate or relabel historical experiments/evidence."""
        validate_report(report)
        if source_mode not in {"live", "replay", "fixture"}:
            raise AgentError("INVALID_INPUT", "Research mode must be live, replay or fixture.")
        manifest = []
        for row in report["listings"]:
            for evidence in row["evidence"]:
                if not isinstance(evidence, dict) or not isinstance(evidence.get("content"), str):
                    raise AgentError("INVALID_RESPONSE", "Evidence content is missing.")
                raw = evidence["content"].encode("utf-8")
                sha = hashlib.sha256(raw).hexdigest()
                if evidence.get("sha256") != sha:
                    raise AgentError("INVALID_RESPONSE", "Evidence hash does not match the supplied content.")
                self.store.save_bytes(f"evidence/{sha}.txt", raw)
                manifest.append({"sha256": sha, "path": f"evidence/{sha}.txt", "source_url": evidence.get("source_url"),
                                 "observed_at": evidence.get("observed_at"), "kind": evidence.get("kind")})
        run_id = report["run_id"]
        # artifact() validates identifiers before any run-derived file path is used.
        with self.store.transaction():
            self.store.artifact("research", run_id, report)
            metadata_id = "research-import-" + digest(run_id)
            try:
                existing = self.store.read("snapshots", metadata_id)
            except AgentError as error:
                if error.code != "NOT_FOUND":
                    raise
                existing = None
            if existing and existing["source_mode"] != source_mode:
                raise AgentError("ARTIFACT_CONFLICT", "An imported run cannot be relabeled with a different evidence mode.")
            if not existing:
                metadata = {"schema_version": "1.0", "run_id": run_id, "source_mode": source_mode,
                            "imported_at": utcnow(), "report_sha256": digest(report), "evidence_manifest": manifest}
                self.store.artifact("snapshots", metadata_id, metadata)
            self.store.save_bytes(f"research/{run_id}/request.json", (encoded(report["request"]) + "\n").encode("utf-8"))
            self.store.save_bytes(f"research/{run_id}/evidence-manifest.json", (encoded(manifest) + "\n").encode("utf-8"))
        return run_id

    def research_report(self, run_id):
        report = self.store.read("research", run_id)
        report.pop("log_path", None)
        return report

    def confirm_listing(self, run_id, listing_id, observed_at, facts, *, confirmed):
        if confirmed is not True:
            raise AgentError("INVALID_INPUT", "Explicitly confirm the listing facts; model output cannot confirm them.")
        report = self.store.read("research", run_id)
        row = next((r for r in report["listings"] if r["listing_id"] == listing_id and r["observed_at"] == observed_at), None)
        if row is None:
            raise AgentError("NOT_FOUND", "Observation not found in this run.")
        metadata = self.store.read("snapshots", "research-import-" + digest(run_id))
        result = confirmed_facts(run_id, row, facts, fixture=metadata["source_mode"] == "fixture")
        try:
            existing = self.store.read("facts", result["fact_id"])
        except AgentError as error:
            if error.code != "NOT_FOUND":
                raise
        else:
            if {k: v for k, v in result.items() if k != "confirmed_at"} != {k: v for k, v in existing.items() if k != "confirmed_at"}:
                raise AgentError("ARTIFACT_CONFLICT", "Confirmed observation facts are immutable; import a new observation for corrections.")
            return existing
        return self.store.artifact("facts", result["fact_id"], result)

    def confirm_copy(self, candidate_id, confirmed_card, *, confirm_owned, key, allow_sale=False, cost_jpy=None):
        request = {"candidate_id": candidate_id, "confirmed_card": confirmed_card, "confirm_owned": confirm_owned,
                   "allow_sale": allow_sale, "cost_jpy": cost_jpy}
        with self.store.transaction():
            replay = self.store.replay("confirm-copy", key, request)
            if replay:
                return self.store.collection.get(replay["copy_id"])
            if confirm_owned is not True:
                raise AgentError("INVALID_INPUT", "Explicit possession confirmation is required.")
            card = card_input(confirmed_card, confirmed=True)
            candidate = self.store.read("candidates", candidate_id)
            if self.store.db.execute("SELECT 1 FROM main_confirmations WHERE candidate_id=?", (candidate_id,)).fetchone():
                raise AgentError("ALREADY_CONFIRMED", "This candidate already registered an owned copy.")
            upload = self.store.read("uploads", candidate["upload_id"])
            copy = self.store.collection.add(card, upload["images"][0]["sha256"], cost_jpy, allow_sale)
            self.store.db.execute("INSERT INTO main_confirmations VALUES(?,?)", (candidate_id, copy["id"]))
            for asset in upload["images"]:
                self.store.db.execute("INSERT OR IGNORE INTO main_copy_assets VALUES(?,?,?)", (copy["id"], asset["sha256"], asset["role"]))
            self.store.artifact("snapshots", "confirmation-" + copy["id"], {"schema_version": "1.0", "copy_id": copy["id"],
                "candidate_id": candidate_id, "original_candidate": candidate, "confirmed_card": card, "created_at": utcnow()})
            self.store.remember("confirm-copy", key, request, {"copy_id": copy["id"]})
            return copy

    def _snapshot(self, *, fixture=False):
        used = self.store.db.execute("SELECT COALESCE(SUM(total),0) FROM main_fixture_reservations WHERE state IN ('reserved','unknown','confirmed')").fetchone()[0] if fixture else 0
        snapshot = {"schema_version": "1.0", "snapshot_id": new_id("collection"), "created_at": utcnow(),
                    "copies": self.store.collection.list(), "confirmed_cash_jpy": None,
                    "active_reservations_jpy": used, "reservation_basis": "fixture allowance only" if fixture else "no live trading ledger implemented"}
        return self.store.artifact("snapshots", snapshot["snapshot_id"], snapshot)

    def _update(self, job_id, **changes):
        with self.store.transaction():
            job = self.store.get_job(job_id)
            if job["state"] == "cancel_requested":
                raise JobCancelled()
            job.update(changes)
            return self.store.put_job(job)

    def _checkpoint(self, job_id, checkpoint):
        with self.store.transaction():
            job = self.store.get_job(job_id)
            if job["state"] == "cancel_requested":
                raise JobCancelled()
            self.store.put_job(job, checkpoint=checkpoint)

    def _result(self, job_id, kind, record_id):
        return self._update(job_id, state="succeeded", stage="finished", questions=[],
            progress={"message": "Completed; inspect the result's evidence and action status.", "completed_steps": 1, "total_steps": 1},
            result={"kind": kind, "id": record_id, "url": f"/api/v1/{RESULT_PATHS[kind]}/{record_id}"})

    async def _research(self, request, remaining):
        if not self.research_configured:
            raise AgentError("PROVIDER_UNAVAILABLE", "Set RESEARCH_AGENT_URL and start the contracted research service, or explicitly import a report.")
        request = validate_research_request(request)
        if remaining < 30:
            raise AgentError("JOB_TIMEOUT", "Insufficient active budget remains for another bounded research call.")
        request["source_limits"]["timeout_seconds"] = min(request["source_limits"]["timeout_seconds"], int(remaining) - 15)
        report = await asyncio.wait_for(self.researcher(request), timeout=min(remaining, request["source_limits"]["timeout_seconds"] + 15))
        if report.get("request", {}).get("confirmed_target", {}).get("number") != request["confirmed_target"]["number"]:
            raise AgentError("INVALID_RESPONSE", "Research returned a different target.")
        return self.import_research(report, source_mode="live")

    async def _steps(self, job, value, checkpoint, deadline):
        job_id, kind = job["job_id"], job["type"]
        if kind == "identify":
            self._update(job_id, stage="identify")
            upload = self.store.read("uploads", value["upload_id"])
            paths = [str(self.store.root / self.store.db.execute("SELECT path FROM main_assets WHERE sha256=?", (a["sha256"],)).fetchone()[0]) for a in upload["images"]]
            result = await asyncio.to_thread(self.recognizer, paths)
            if not isinstance(result, dict) or not result.get("ok"):
                code = result.get("error", {}).get("code", "IDENTIFICATION_FAILED") if isinstance(result, dict) else "IDENTIFICATION_FAILED"
                raise AgentError(code, "Image identification failed; check the configured provider and input images.")
            card = card_input(result["card"])
            candidate = {k: result.get(k) for k in ("observed_at", "provider", "configured_model", "returned_model", "image_sha256", "missing_fields", "candidate_complete")}
            candidate.update(schema_version="1.0", candidate_id=new_id("candidate"), upload_id=value["upload_id"],
                             card=card, requires_confirmation=True, physical_authenticity="not established")
            self.store.artifact("candidates", candidate["candidate_id"], candidate)
            return self._result(job_id, "candidate", candidate["candidate_id"])
        if kind == "research":
            self._update(job_id, stage="research")
            run_id = checkpoint.get("research_run_id")
            if not run_id:
                run_id = await self._research(value["request"], deadline - time.monotonic())
                checkpoint["research_run_id"] = run_id
                self._checkpoint(job_id, checkpoint)
            return self._result(job_id, "research_report", run_id)
        if kind == "decision":
            goal = value["goal"]
            questions = goal_questions(goal, self.store.collection.list())
            if questions:
                return self._update(job_id, state="awaiting_input", stage="clarify", questions=questions,
                    progress={"message": "Answer the material constraints before planning.", "completed_steps": 0, "total_steps": None})
            ids = list(value["research_run_ids"] or checkpoint.get("research_run_ids", []))
            if not value["research_run_ids"] and goal["intent"] != "sell":
                self._update(job_id, stage="research")
                for target in goal["targets"][len(ids):]:
                    card = target["card"]
                    if card["presentation"] != "raw":
                        # Existing service only proves raw-card matching. Supplied graded
                        # reports can still be evaluated by the common cohort planner.
                        raise AgentError("FEATURE_UNAVAILABLE", "Automatic graded-card research is not implemented; import compatible evidence explicitly.")
                    request = {"confirmed_target": {"name_ja": card["name"], "number": card["number"], "language": card["language"]},
                               "currency": "JPY", "raw_only": True, "price_basis": "delivered_price"}
                    ids.append(await self._research(request, deadline - time.monotonic()))
                    checkpoint["research_run_ids"] = ids
                    self._checkpoint(job_id, checkpoint)
            reports = [self.store.read("research", run_id) for run_id in ids]
            for report in reports:
                requested = report["request"].get("confirmed_target", {})
                if not any(requested.get("number") == t["card"]["number"] and requested.get("name_ja") == t["card"]["name"] for t in goal["targets"]):
                    raise AgentError("INVALID_INPUT", "A supplied research report is incompatible with the confirmed goal.")
            modes = {run_id: self.store.read("snapshots", "research-import-" + digest(run_id))["source_mode"] for run_id in ids}
            if "fixture" in modes.values() and any(mode != "fixture" for mode in modes.values()):
                raise AgentError("INVALID_INPUT", "Synthetic and real evidence cannot be combined into one acquisition plan.")
            facts = {}
            for report in reports:
                for row in report["listings"]:
                    ref = observation_key(report["run_id"], row)
                    try:
                        facts[ref] = self.store.read("facts", ref)
                    except AgentError as error:
                        if error.code != "NOT_FOUND":
                            raise
            self._update(job_id, stage="decision")
            snapshot = self._snapshot(fixture=bool(modes) and all(m == "fixture" for m in modes.values()))
            decision = plan_decision(goal, snapshot, reports, facts, modes, max_age_seconds=self.evidence_max_age_seconds)
            self.store.artifact("decisions", decision["decision_id"], decision)
            return self._result(job_id, "decision", decision["decision_id"])
        if kind == "sale_draft":
            self._update(job_id, stage="draft")
            copy = self.store.collection.get(value["copy_id"])
            result = draft(copy, value["asking_price_jpy"])
            result.update(schema_version="1.0", draft_id=new_id("draft"), revision=1, created_at=utcnow(), currency="JPY",
                          decision_ref=value["decision_id"], confirmed_facts=copy,
                          evidence_refs=[copy["id"]], unresolved_fields=["shipping", "photos", "human_review"],
                          claim_map=[{"claim": text, "fact_ref": "confirmed_facts.card.defects"} for text in result["required_defects"]])
            self.store.artifact("drafts", result["draft_id"], result)
            return self._result(job_id, "sale_draft", result["draft_id"])
        if kind in {"execute", "reconcile"}:
            self._update(job_id, stage="preflight" if kind == "execute" else "reconcile")
            await (self.trading.execute(value["intent_id"]) if kind == "execute" else self.trading.reconcile(value["intent_id"]))
            return self._result(job_id, "action_intent", value["intent_id"])
        raise AgentError("INVALID_INPUT", "Unsupported internal job type.")

    async def run_job(self, job_id):
        with self.store.transaction():
            job = self.store.get_job(job_id)
            if job["state"] == "cancel_requested":
                job.update(state="cancelled", stage="finished")
                return self.store.put_job(job)
            if job["state"] != "queued":
                return job
            value, checkpoint = self.store.private_job(job_id)
            job.update(state="running")
            self.store.put_job(job)
        started = time.monotonic()
        remaining = self.job_timeout_seconds - checkpoint.get("active_seconds", 0)
        try:
            if remaining <= 0:
                raise asyncio.TimeoutError()
            await asyncio.wait_for(self._steps(job, value, checkpoint, started + remaining), timeout=remaining)
        except JobCancelled:
            with self.store.transaction():
                current = self.store.get_job(job_id)
                current.update(state="cancelled", stage="finished", questions=[])
                self.store.put_job(current)
        except Exception as error:
            # Never expose provider exception bodies, paths, stack traces or credentials.
            if isinstance(error, AgentError):
                safe = error
            elif isinstance(error, (TimeoutError, asyncio.TimeoutError)):
                safe = AgentError("JOB_TIMEOUT", "The active-work deadline was reached.", retryable=False)
            elif isinstance(error, ResearchUnavailable):
                safe = AgentError("PROVIDER_UNAVAILABLE", "Research service is unavailable; inspect its configuration and private logs.", retryable=True)
            else:
                safe = AgentError("INTERNAL_ERROR", "The job failed validation or execution. Inspect local diagnostics without resubmitting a trade.")
            if job["type"] in {"execute", "reconcile"}:
                intent = self.store.get_intent(value["intent_id"])
                if intent["state"] in {"reserved", "submitting", "unknown"}:
                    self.trading._finish(intent["intent_id"], "unknown", message="Worker interrupted; reconcile the original action.")
            with self.store.transaction():
                current = self.store.get_job(job_id)
                current.update(state="failed", stage="finished", error=safe.as_dict())
                self.store.put_job(current)
        finally:
            checkpoint["active_seconds"] = checkpoint.get("active_seconds", 0) + time.monotonic() - started
            with self.store.transaction():
                current = self.store.get_job(job_id)
                self.store.put_job(current, checkpoint=checkpoint)
        return self.store.get_job(job_id)

    async def work(self):
        # Snapshot the queue so input-waiting jobs do not cause a busy loop.
        jobs = [j["job_id"] for j in self.list_jobs() if j["state"] in {"queued", "cancel_requested"}]
        return [await self.run_job(job_id) for job_id in jobs]

    def recover_interrupted(self):
        """Call only after stopping the previous worker. Never rerun a running job."""
        recovered = []
        for job in self.list_jobs():
            if job["state"] == "awaiting_input" and (datetime.now(timezone.utc) - parse_time(job["updated_at"])).total_seconds() > 86400:
                error = AgentError("INPUT_EXPIRED", "The input-waiting window expired.")
            elif job["state"] == "running":
                error = AgentError("JOB_INTERRUPTED", "The previous worker stopped; retained artifacts require review.")
            else:
                continue
            value, _ = self.store.private_job(job["job_id"])
            if job["type"] in {"execute", "reconcile"}:
                intent = self.store.get_intent(value["intent_id"])
                if intent["state"] in {"reserved", "submitting", "unknown"}:
                    self.trading._finish(intent["intent_id"], "unknown", message="Interrupted action; reconcile before another submission.")
            with self.store.transaction():
                job.update(state="failed", stage="finished", questions=[], error=error.as_dict())
                self.store.put_job(job)
            recovered.append(job)
        return recovered
