"""Connect the central journal to application sessions and atomic job writes."""
import json
import os
import time

from fastapi import HTTPException

from activity import DEFAULT_PUBLISH_URL
from activity_server import ActivityStore, activity_router


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
