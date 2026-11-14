"""Connect the central journal to application sessions and atomic job writes."""
import json
import os
import time
import uuid

from fastapi import HTTPException

from activity import DEFAULT_PUBLISH_URL
from activity_server import ActivityStore, Event, PublicData, activity_router, now


def attach_activity(app, store, worker) -> None:
    journal = ActivityStore(store.path)
    store.on_job_event = journal.backend_job_event
    with store.transaction() as db:
        for row in db.execute("SELECT owner,data FROM api_records WHERE kind='job'").fetchall():
            job = json.loads(row["data"])
            if not db.execute("SELECT 1 FROM activity_job_scopes WHERE job_id=?", (job["job_id"],)).fetchone():
                journal.backend_job_event(db, row["owner"], job, "job.snapshot")

    def owner(request):
        with journal.connect() as db:
            row = db.execute("SELECT id FROM api_sessions WHERE id=? AND expires>?",
                             (request.cookies.get("pokescout_session", ""), time.time())).fetchone()
        if not row:
            raise HTTPException(401, "AUTH_REQUIRED")
        return row[0]

    app.include_router(activity_router(journal, owner))
    app.state.activity_store = journal
    app.state.activity_stream_enabled = True
    worker.activity_context = lambda job: journal.issue_context(
        job["job_id"], "pokescout-research", ["research.*"], parent_span_id=job["job_id"],
        publish_url=os.getenv("ACTIVITY_PUBLISH_URL", DEFAULT_PUBLISH_URL))

    def pipeline_activity(job):
        context = journal.issue_context(job["job_id"], "pokescout-main", ["pipeline.*"])
        def publish(state, stage):
            safe_stage = stage if stage in {"identify", "clarify", "research", "decision", "draft", "execute", "finished"} else "workflow"
            event_type = "step.failed" if state == "failed" else "step.completed" if state == "succeeded" else "step.progress"
            journal.append(context.publisher_token, Event(schema_version="1.0", event_id=str(uuid.uuid4()),
                job_id=job["job_id"], type=event_type, operation="pipeline." + safe_stage,
                span_id=job["job_id"] + "-pipeline", parent_span_id=job["job_id"], attempt=1,
                occurred_at=now(), level="error" if state == "failed" else "info",
                message="Main pipeline: " + safe_stage + ".", data=PublicData()))
        return publish
    worker.pipeline_activity = pipeline_activity
