"""API-owned adapter to the teammate-owned main decision implementation."""
import asyncio
import uuid
from pathlib import Path

from main_agent import MainAgent
from agent_store import AgentError


def decide(goal: dict, copies: list[dict], reports: list[dict], reservations: int,
           *, data_dir: Path, job_id: str) -> dict:
    """Run the real main agent, retaining its checkpoints and report provenance."""
    class ScopedMainAgent(MainAgent):
        def _snapshot(self, *, fixture=False):
            snapshot = {"schema_version": "1.0", "snapshot_id": "collection-" + uuid.uuid4().hex,
                        "copies": copies, "confirmed_cash_jpy": None,
                        "active_reservations_jpy": reservations}
            return self.store.artifact("snapshots", snapshot["snapshot_id"], snapshot)

    agent = ScopedMainAgent(data_dir)
    # API session ownership remains authoritative; do not read another user's
    # collection merely because the main agent supports a local single user CLI.
    agent.store.collection.list = lambda: copies
    try:
        run_ids = [agent.import_research(report, source_mode="live") for report in reports]
        job = agent.create_job("decision", {"goal": goal, "research_run_ids": run_ids}, key=job_id)
        done = asyncio.run(agent.run_job(job["job_id"]))
        if done["state"] != "succeeded":
            error = done.get("error") or {}
            raise AgentError(error.get("code", "MAIN_AGENT_INCOMPLETE"),
                             error.get("message", "Main agent did not produce a decision."))
        return agent.store.read("decisions", done["result"]["id"])
    finally:
        agent.close()
