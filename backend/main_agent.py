"""API-owned adapter to the merged, persistent Pipeline implementation."""
import uuid
from pathlib import Path

from orchestrator import Pipeline
from agent_store import AgentError, digest


class MainAgentNeedsInput(Exception):
    def __init__(self, questions):
        self.questions = questions


def decide(goal: dict, copies: list[dict], reports: list[dict], reservations: int,
           *, data_dir: Path, job_id: str, facts=(), on_activity=None) -> dict:
    """Run the merged pipeline using API-authorized evidence and collection."""
    class ScopedPipeline(Pipeline):
        def _collection_snapshot(self, fixture):
            snapshot = {"schema_version": "1.0", "snapshot_id": "collection-" + uuid.uuid4().hex,
                        "copies": copies, "confirmed_cash_jpy": None,
                        "active_reservations_jpy": reservations}
            self._artifact("snapshots", snapshot["snapshot_id"], snapshot)
            return snapshot

        def _update(self, job, checkpoint, state, stage, message, completed, **kwargs):
            super()._update(job, checkpoint, state, stage, message, completed, **kwargs)
            if on_activity:
                try:
                    on_activity(state, stage)
                except Exception:
                    import logging
                    logging.getLogger(__name__).warning("Pipeline activity publication failed.")

    for report in reports:
        target = report.get("request", {}).get("confirmed_target", {})
        if not any(target.get("number") == item["card"]["number"] and
                   target.get("name_ja") == item["card"]["name"] for item in goal["targets"]):
            raise AgentError("INVALID_INPUT", "Research report does not match a confirmed target.")
    pipeline = ScopedPipeline(data_dir)
    pipeline.store.collection.list = lambda: copies
    try:
        run_ids = {report["run_id"] for report in reports}
        sidecar = {fact["fact_id"]: {key: fact[key] for key in
                   ("card", "quantity", "purchase_fee_jpy", "condition_record", "registry_status")}
                   for fact in facts if fact["run_id"] in run_ids}
        if not reports and goal["intent"] != "sell":
            raise AgentError("RESEARCH_UNAVAILABLE", "No research report is available for this decision.")
        done = pipeline.start({"goal": goal, "reports": reports, "facts": sidecar},
                              idempotency_key=job_id + ":" + digest(goal)[:32])
        if done["state"] == "awaiting_input":
            questions = done["questions"]
            for question in questions:
                if question["field"].startswith("targets."):
                    question["field"] = "target_card." + question["field"].split(".")[1]
                elif question["field"] == "copy_ids":
                    question["field"] = "copy_id"
            with pipeline.store.transaction():
                child = pipeline.get(done["job_id"])
                child.update(state="cancelled", stage="finished", questions=[])
                pipeline.store.put_job(child)
            raise MainAgentNeedsInput(questions)
        if done["state"] != "succeeded":
            error = done.get("error") or {}
            raise AgentError(error.get("code", "PIPELINE_INCOMPLETE"),
                             error.get("message", "Pipeline did not produce a decision."))
        return pipeline.result(done["job_id"])
    finally:
        pipeline.close()
