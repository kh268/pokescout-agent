"""Local backend demonstration: publish synthetic steps or run real research with SSE."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from activity import ActivityEmitter
from activity_server import ActivityStore, DEFAULT_DB


async def run(args: argparse.Namespace) -> None:
    store = ActivityStore(args.db)
    job_id = "job_" + uuid.uuid4().hex
    store.register_job("local_user", job_id, ttl=180)
    store.transition_job(job_id, "running", "research" if args.research else "demo", expected_revision=1)
    print(json.dumps({"job_id": job_id, "stream_url": "http://127.0.0.1:18082/api/v1/activity/stream?job_id=" + job_id}), flush=True)
    try:
        if args.research:
            from research import research_market
            context = store.issue_context(job_id, "pokescout-research", ["research.*"], parent_span_id=job_id)
            request = json.loads(args.request.read_text(encoding="utf-8"))
            report = await research_market(request, activity_context=context)
            print(json.dumps({"run_id": report["run_id"], "research_status": report["status"]}), flush=True)
            state = "failed" if report["status"] == "failed" else "succeeded"
        else:
            for producer, operation in (("image-agent", "identify.card"), ("pokescout-research", "research.search")):
                context = store.issue_context(job_id, producer, [operation], parent_span_id=job_id)
                span_id = "span_" + uuid.uuid4().hex
                async with ActivityEmitter(context) as emitter:
                    emitter.emit("step.started", operation, "Synthetic transport demonstration; no provider call.", span_id=span_id, parent_span_id=job_id)
                    await asyncio.sleep(0.3)
                    emitter.emit("step.completed", operation, "Synthetic demonstration step completed.", span_id=span_id, parent_span_id=job_id)
            state = "succeeded"
        store.transition_job(job_id, state, "finished", expected_revision=2)
    except BaseException:
        store.transition_job(job_id, "failed", "finished", expected_revision=2)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--research", action="store_true")
    parser.add_argument("--request", type=Path, default=Path(__file__).parent / "experiments/sc/market-request.json")
    asyncio.run(run(parser.parse_args()))
