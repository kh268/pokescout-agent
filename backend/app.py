"""Frontend API for a loopback-only, single-user hackathon deployment."""
import asyncio
import hashlib
import io
import json
import os
import secrets
import sqlite3
import time
import uuid
import warnings
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import StrictBool
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cards import validate_card
from decisions import plan as structured_plan

from .models import (Answers, Authorization, Card, ConfirmCopy, DecisionInput, DraftRequest,
                     Empty, Goal, IntentRequest, JobRequest, Model, ListingConfirmation, StructuredPlanInput)
from .store import APIError, Store, digest, encoded, now, uid
from .worker import Settings, TERMINAL, Worker, new_job, public_job

PREFIX = "/api/v1"
COOKIE = "pokescout_session"


class PermissionUpdate(Model):
    allow_sale: StrictBool


def envelope(request: Request, data: object, status: int = 200) -> JSONResponse:
    response = JSONResponse({"api_version": "1.0", "request_id": request.state.request_id, "data": data}, status_code=status)
    if isinstance(data, dict) and "revision" in data:
        response.headers["ETag"] = f'"{data["revision"]}"'
    if status == 202 and isinstance(data, dict) and "job_id" in data:
        response.headers["Location"] = PREFIX + "/jobs/" + data["job_id"]
        response.headers["Retry-After"] = "2"
    return response


def error_response(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse({"api_version": "1.0", "request_id": request.state.request_id, "error": exc.error}, status_code=exc.status)


def public_record(record: dict) -> dict:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def revision(request: Request, record: dict) -> None:
    value = request.headers.get("if-match")
    if value is None:
        raise APIError(428, "PRECONDITION_REQUIRED", "Send the resource ETag in If-Match.")
    if value != f'"{record["revision"]}"':
        raise APIError(412, "REVISION_CONFLICT", "Refresh the resource before editing it.")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings(
        data_dir=Path(os.getenv("POKESCOUT_DATA_DIR", "data")),
        identify_enabled=bool(os.getenv("GEMINI_API_KEY")),
        research_enabled=bool(os.getenv("RESEARCH_AGENT_URL")))
    store, worker = Store(settings.data_dir), None
    worker = Worker(store, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A single application process owns two local workers. No agent manages
        # its own frontend jobs. Unknown interrupted submissions need reconciliation.
        with store.transaction() as db:
            for row in db.execute("SELECT owner,data FROM api_records WHERE kind='job'").fetchall():
                job = json.loads(row["data"])
                if job["state"] == "running":
                    job["error"] = APIError(409, "JOB_INTERRUPTED", "Backend restarted; retained artifacts are available.").error
                    store.save_job(db, row["owner"], job, "failed")
        tasks = [asyncio.create_task(worker.loop()) for _ in range(2)]
        app.state.worker_tasks = tasks
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="PokéScout application API", version="1.0", lifespan=lifespan)
    app.state.store, app.state.settings = store, settings
    from .activity import attach_activity
    attach_activity(app, store, worker)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    if settings.allowed_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PATCH"],
                           allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "If-Match"],
                           expose_headers=["ETag", "Location", "Retry-After"])

    @app.middleware("http")
    async def session_boundary(request: Request, call_next: Callable):
        request.state.request_id = uid("request")
        public = request.method == "OPTIONS" or request.url.path in {"/health", "/docs", "/docs/oauth2-redirect", "/openapi.json", PREFIX + "/session", "/internal/v1/activity/events"}
        try:
            if not public:
                token = request.cookies.get(COOKIE, "")
                with store.transaction() as db:
                    session = db.execute("SELECT * FROM api_sessions WHERE id=? AND expires>?", (token, time.time())).fetchone()
                if not session:
                    raise APIError(401, "AUTH_REQUIRED", "Initialize a local session first.")
                request.state.owner = session["id"]
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                        raise APIError(403, "CSRF_INVALID", "Missing or invalid CSRF token.")
                    origin = request.headers.get("origin")
                    expected = f"{request.url.scheme}://{request.url.netloc}"
                    if origin and origin != expected and origin not in settings.allowed_origins:
                        raise APIError(403, "CSRF_INVALID", "Origin is not allowed.")
            if request.method in {"POST", "PATCH"}:
                multipart = request.headers.get("content-type", "").startswith("multipart/")
                limit = 13_000_000 if multipart else 1_048_576
                body, count = [], 0
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > limit:
                        raise APIError(413, "BODY_TOO_LARGE", "Request body exceeds the limit.")
                    body.append(chunk)
                request._body = b"".join(body)
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["Cache-Control"] = "no-store"
            return response
        except APIError as exc:
            return error_response(request, exc)

    @app.exception_handler(APIError)
    async def application_error(request: Request, exc: APIError):
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        malformed = any(item["type"] == "json_invalid" for item in exc.errors())
        error = APIError(400 if malformed else 422, "INVALID_JSON" if malformed else "INVALID_INPUT", "Request validation failed.")
        error.error["field_errors"] = [{"field": ".".join(map(str, item["loc"])), "message": item["msg"]}
                                       for item in exc.errors()]
        return error_response(request, error)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(request, APIError(exc.status_code, "NOT_FOUND" if exc.status_code == 404 else "INVALID_INPUT", "Resource or request unavailable."))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return error_response(request, APIError(500, "INTERNAL_ERROR", "Backend error; use the request ID for diagnosis."))

    def mutate(request: Request, payload: object, status: int, operation: Callable) -> JSONResponse:
        raw_key = request.headers.get("idempotency-key", "")
        try:
            key = str(uuid.UUID(raw_key))
        except ValueError:
            raise APIError(422, "INVALID_INPUT", "Idempotency-Key must be a UUID.") from None
        owner, route, hashed = request.state.owner, request.method + " " + request.url.path, digest(payload)
        with store.transaction() as db:
            old = db.execute("SELECT * FROM api_idempotency WHERE owner=? AND route=? AND key=?", (owner, route, key)).fetchone()
            if old:
                if old["hash"] != hashed:
                    raise APIError(409, "IDEMPOTENCY_CONFLICT", "This key was used for a different request.")
                data, status = json.loads(old["data"]), old["status"]
            else:
                data = operation(db, owner)
                db.execute("INSERT INTO api_idempotency VALUES(?,?,?,?,?,?)", (owner, route, key, hashed, status, encoded(data)))
        return envelope(request, data, status)

    def enqueue(db: sqlite3.Connection, owner: str, kind: str, inputs: dict) -> dict:
        active = [j for j in store.rows(db, owner, "job") if j["state"] not in TERMINAL]
        if len(active) >= 2:
            raise APIError(429, "JOB_LIMIT_REACHED", "Two jobs are already active.", retryable=True)
        job = new_job(owner, kind, inputs)
        store.put(db, owner, "job", job["job_id"], job)
        store.event(db, owner, job, "job.queued")
        return public_job(job)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "local_single_user"}

    @app.get(PREFIX + "/session")
    def session(request: Request):
        token = request.cookies.get(COOKIE, "")
        with store.transaction() as db:
            old = db.execute("SELECT * FROM api_sessions WHERE id=? AND expires>?", (token, time.time())).fetchone()
            if old:
                token, csrf = old["id"], old["csrf"]
            else:
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                db.execute("INSERT INTO api_sessions VALUES(?,?,?)", (token, csrf, time.time() + 7 * 86400))
        response = envelope(request, {"csrf_token": csrf, "mode": "local_single_user"})
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https", max_age=7 * 86400)
        return response

    @app.get(PREFIX + "/capabilities")
    def capabilities(request: Request):
        return envelope(request, {"features": {"identify": settings.identify_enabled, "research": settings.research_enabled,
            "tavily_plan": bool(os.getenv("TAVILY_API_KEY") and os.getenv("NOSANA_API_KEY")),
            "decision": True, "sale_draft": True, "fixture_execution": bool(settings.fixture_listings),
            "live_execution": False, "watch": False, "forecast": False,
            "activity_stream": bool(getattr(app.state, "activity_stream_enabled", False))},
            "limits": {"max_views_per_upload": 4, "max_upload_bytes": 12_000_000,
                       "max_active_jobs": 2, "job_timeout_seconds": settings.job_timeout}})

    @app.post(PREFIX + "/uploads")
    async def upload(request: Request, files: Annotated[list[UploadFile], File()], manifest: Annotated[str, Form()]):
        try:
            roles = json.loads(manifest)
        except ValueError:
            raise APIError(400, "INVALID_JSON", "Manifest must be a JSON array.") from None
        if not 1 <= len(files) <= 4 or not isinstance(roles, list) or len(roles) != len(files):
            raise APIError(422, "INVALID_INPUT", "Provide 1–4 views of one physical card with matching roles.")
        images, total = [], 0
        for file, entry in zip(files, roles):
            if not isinstance(entry, dict) or set(entry) != {"role"} or entry["role"] not in {"front", "back", "slab_label", "detail", "other"}:
                raise APIError(422, "INVALID_INPUT", "Invalid image role.")
            raw = await file.read(12_000_001)
            await file.close()
            total += len(raw)
            if total > 12_000_000:
                raise APIError(413, "UPLOAD_TOO_LARGE", "Combined image size exceeds 12 MB.")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(raw)) as image:
                        fmt = image.format
                        if image.width * image.height > 25_000_000:
                            raise ValueError("Too many pixels")
                        image.verify()
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
                raise APIError(415, "UNSUPPORTED_MEDIA_TYPE", "Invalid or oversized image; use JPEG, PNG or WebP.") from None
            formats = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp")}
            if fmt not in formats:
                raise APIError(415, "UNSUPPORTED_MEDIA_TYPE", "Use JPEG, PNG or WebP.")
            extension, mime = formats[fmt]
            sha = hashlib.sha256(raw).hexdigest()
            relative = f"images/{sha}.{extension}"
            path = store.root / relative
            path.parent.mkdir(exist_ok=True)
            if not path.exists():
                temporary = path.with_name(uid("image") + ".tmp")
                temporary.write_bytes(raw)
                temporary.replace(path)
            images.append({"role": entry["role"], "sha256": sha, "mime_type": mime,
                           "size_bytes": len(raw), "_path": relative})
        def save(db, owner):
            saved = []
            for image in images:
                key = uid("asset")
                asset = {**image, "asset_id": key, "url": PREFIX + "/assets/" + key}
                store.put(db, owner, "asset", key, asset)
                saved.append(public_record(asset))
            key = uid("upload")
            data = {"upload_id": key, "created_at": now(), "images": saved}
            store.put(db, owner, "upload", key, data)
            return data
        return mutate(request, [{k: v for k, v in image.items() if k != "_path"} for image in images], 201, save)

    @app.post(PREFIX + "/jobs")
    def jobs(request: Request, body: JobRequest):
        payload = body.model_dump(exclude_none=True)
        def accept(db, owner):
            kind, inputs = payload["type"], payload["input"]
            if kind == "identify":
                if not settings.identify_enabled:
                    raise APIError(409, "FEATURE_UNAVAILABLE", "Image recognition is not configured.")
                store.get(db, owner, "upload", inputs["upload_id"])
            elif kind == "research" and not settings.research_enabled:
                raise APIError(409, "FEATURE_UNAVAILABLE", "Research service is not configured.")
            elif kind == "tavily_plan" and not (os.getenv("TAVILY_API_KEY") and os.getenv("NOSANA_API_KEY")):
                raise APIError(409, "FEATURE_UNAVAILABLE", "Set TAVILY_API_KEY and NOSANA_API_KEY on the Python server for live research.")
            elif kind == "decision":
                # Include nullable defaults for stable resumption validation.
                inputs = body.input.model_dump()
                for key in inputs["research_run_ids"]:
                    store.get(db, owner, "research_report", key)
                for key in inputs["goal"]["copy_ids"]:
                    store.get(db, owner, "copy", key)
            return enqueue(db, owner, kind, inputs)
        return mutate(request, payload, 202, accept)

    @app.get(PREFIX + "/jobs/{key}")
    def get_job(request: Request, key: str):
        with store.transaction() as db:
            data = public_job(store.get(db, request.state.owner, "job", key))
        return envelope(request, data)

    @app.post(PREFIX + "/jobs/{key}/answers")
    def answers(request: Request, key: str, body: Answers):
        def resume(db, owner):
            job = store.get(db, owner, "job", key)
            revision(request, job)
            if job["state"] != "awaiting_input":
                raise APIError(409, "INVALID_STATE", "Job is not awaiting input.")
            supplied = {a.question_id: a.value for a in body.answers}
            if len(supplied) != len(body.answers) or set(supplied) != {q["question_id"] for q in job["questions"]}:
                raise APIError(422, "INVALID_INPUT", "Answer each pending question exactly once.")
            goal = job["_input"]["goal"]
            try:
                for q in job["questions"]:
                    value, field = supplied[q["question_id"]], q["field"]
                    if field.startswith("target_card."):
                        card = validate_card(Card.model_validate(value).model_dump(), True)
                        index = int(field.split(".")[1])
                        if index == len(goal["targets"]):
                            goal["targets"].append({"card": card, "quantity": 1})
                        else:
                            goal["targets"][index]["card"] = card
                    elif field == "copy_id":
                        store.get(db, owner, "copy", value)
                        goal["copy_ids"] = [value]
                    else:
                        goal[field] = value
                job["_input"]["goal"] = Goal.model_validate(goal).model_dump()
            except (ValueError, TypeError):
                raise APIError(422, "INVALID_INPUT", "Clarification value is invalid.") from None
            job["questions"], job["stage"] = [], "queued"
            store.save_job(db, owner, job, "queued")
            return public_job(job)
        return mutate(request, body.model_dump(), 202, resume)

    @app.post(PREFIX + "/jobs/{key}/cancel")
    def cancel(request: Request, key: str, body: Empty):
        def stop(db, owner):
            job = store.get(db, owner, "job", key)
            revision(request, job)
            if job["type"] in {"execute", "reconcile"} and job["state"] == "running":
                raise APIError(409, "ACTION_ALREADY_SUBMITTED", "An action may be submitted; reconcile its intent.")
            if job["state"] in TERMINAL:
                raise APIError(409, "INVALID_STATE", "The job has already finished.")
            store.save_job(db, owner, job, "cancel_requested")
            return public_job(job)
        return mutate(request, {}, 202, stop)

    @app.post(PREFIX + "/copies/confirm")
    def confirm(request: Request, body: ConfirmCopy):
        try:
            card = validate_card(body.confirmed_card.model_dump(), True)
        except ValueError:
            raise APIError(422, "IDENTITY_INCOMPLETE", "Resolve card identity and uncertainty before confirmation.") from None
        def save(db, owner):
            candidate = store.get(db, owner, "candidate", body.candidate_id)
            if db.execute("SELECT 1 FROM api_confirmations WHERE candidate=?", (body.candidate_id,)).fetchone():
                raise APIError(409, "ALREADY_CONFIRMED", "This candidate is already registered.")
            upload = store.get(db, owner, "upload", candidate["upload_id"])
            primary = upload["images"][0]["sha256"]
            key, created = "copy-" + uuid.uuid4().hex[:12], now()
            try:
                db.execute("INSERT INTO copies VALUES(?,?,?,?,?,?,?)", (key, encoded(card), primary,
                           body.cost_jpy, int(body.allow_sale), "owned", created))
            except sqlite3.IntegrityError:
                raise APIError(409, "ALREADY_CONFIRMED", "This image already represents an owned copy.") from None
            data = {"id": key, "card": card, "image_hash": primary, "cost": body.cost_jpy,
                    "allow_sale": body.allow_sale, "state": "owned", "created": created,
                    "revision": 1, "images": upload["images"], "condition_record": None,
                    "_candidate_id": body.candidate_id}
            db.execute("INSERT INTO api_confirmations VALUES(?,?)", (body.candidate_id, key))
            store.put(db, owner, "copy", key, data)
            return public_record(data)
        return mutate(request, body.model_dump(), 201, save)

    @app.patch(PREFIX + "/copies/{key}")
    def permission(request: Request, key: str, body: PermissionUpdate):
        def change(db, owner):
            owned = store.get(db, owner, "copy", key)
            revision(request, owned)
            if owned["state"] != "owned":
                raise APIError(409, "INVALID_STATE", "Only owned copies can change sale permission.")
            owned["allow_sale"], owned["revision"] = body.allow_sale, owned["revision"] + 1
            db.execute("UPDATE copies SET allow_sale=? WHERE id=?", (int(body.allow_sale), key))
            store.put(db, owner, "copy", key, owned)
            return public_record(owned)
        return mutate(request, body.model_dump(), 200, change)

    @app.get(PREFIX + "/collection")
    def collection(request: Request, limit: int = 20, cursor: str | None = None):
        if not 1 <= limit <= 100:
            raise APIError(422, "INVALID_INPUT", "Limit must be 1–100.")
        with store.transaction() as db:
            rows = store.rows(db, request.state.owner, "copy")
        keys = [r["id"] for r in rows]
        if cursor is not None and cursor not in keys:
            raise APIError(422, "INVALID_INPUT", "Invalid pagination cursor.")
        start = keys.index(cursor) + 1 if cursor else 0
        page = rows[start:start + limit]
        return envelope(request, {"items": [public_record(r) for r in page],
                                  "next_cursor": page[-1]["id"] if page and start + limit < len(rows) else None})

    @app.post(PREFIX + "/structured-plans")
    def calculate_structured(request: Request, body: StructuredPlanInput):
        """Persist a Python calculation from traceable user-supplied offers."""
        payload = body.model_dump()
        goal, listings = payload["goal"], payload["listings"]
        source_mode = goal.get("source_mode")
        if source_mode not in {"user_supplied_observations", "synthetic_frontend_demo"}:
            raise APIError(422, "INVALID_SOURCE_MODE", "Choose user_supplied_observations or synthetic_frontend_demo explicitly.")
        stale_ids = set()
        for row in listings:
            source = row.get("source_url")
            parsed = urlsplit(source) if isinstance(source, str) else None
            if source_mode == "synthetic_frontend_demo":
                if not parsed or parsed.scheme != "demo" or not parsed.netloc or row.get("source_mode") != "synthetic_frontend_demo":
                    raise APIError(422, "SOURCE_REQUIRED", "Demo plans accept only explicitly synthetic demo offers.")
            elif not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise APIError(422, "SOURCE_REQUIRED", "Each offer needs an HTTPS source URL.")
            stamp = row.get("observed_at")
            try:
                observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                raise APIError(422, "TIMESTAMP_REQUIRED", "Each offer needs an ISO observation timestamp.") from None
            if observed.tzinfo is None or (source_mode == "user_supplied_observations" and row.get("source_mode", "user_supplied") != "user_supplied"):
                raise APIError(422, "SOURCE_REQUIRED", "Timestamp needs a timezone and source_mode must match the workspace.")
            if datetime.now(timezone.utc) - observed > timedelta(hours=24) or observed - datetime.now(timezone.utc) > timedelta(minutes=5):
                stale_ids.add(row.get("id"))
        def save(db, owner):
            copies = store.rows(db, owner, "copy")
            try:
                # Keep original source facts in the saved record; only the planning
                # view marks old or future-dated offers unavailable.
                planning_rows = [{**row, "availability": "stale"} if row.get("id") in stale_ids else row
                                 for row in listings]
                calculation = structured_plan(goal, planning_rows, copies)
                for checked in calculation["checked"]:
                    if checked["id"] in stale_ids:
                        checked["reasons"].append("STALE_OBSERVATION")
                        checked["eligible"] = False
            except (ValueError, KeyError, TypeError) as exc:
                raise APIError(422, "INVALID_INPUT", str(exc)) from None
            key = uid("structured_plan")
            result = {"plan_id": key, "version": 1, "created_at": now(), "mode": "synthetic_demo" if source_mode == "synthetic_frontend_demo" else "user_supplied",
                      "currency": "JPY", "goal": goal, "listings": listings, "plan": calculation,
                      "disclosure": "Synthetic demo offers; no market call or transaction." if source_mode == "synthetic_frontend_demo" else
                                    "User-entered observations; sources and availability were not independently verified. No transaction."}
            store.put(db, owner, "structured_plan", key, result)
            return result
        return mutate(request, payload, 201, save)

    @app.get(PREFIX + "/structured-plans")
    def structured_plans(request: Request):
        with store.transaction() as db:
            rows = store.rows(db, request.state.owner, "structured_plan")
        return envelope(request, {"items": [public_record(r) for r in rows[-30:][::-1]], "next_cursor": None})

    @app.get(PREFIX + "/structured-plans/{key}")
    def structured_plan_record(request: Request, key: str):
        with store.transaction() as db:
            result = store.get(db, request.state.owner, "structured_plan", key)
        return envelope(request, public_record(result))

    # Separate route functions preserve OpenAPI parameter/path consistency.
    def read_record(kind: str):
        def read(request: Request, key: str):
            with store.transaction() as db:
                data = public_record(store.get(db, request.state.owner, kind, key))
            return envelope(request, data)
        return read
    for path, kind in {"uploads": "upload", "candidates": "candidate", "copies": "copy", "live-plans": "live_plan",
                       "research-reports": "research_report", "decisions": "decision",
                       "sale-drafts": "sale_draft", "action-intents": "action_intent", "evidence": "evidence"}.items():
        app.add_api_route(PREFIX + f"/{path}/{{key}}", read_record(kind), methods=["GET"], name="get_" + kind)

    @app.get(PREFIX + "/assets/{key}")
    def asset(request: Request, key: str):
        with store.transaction() as db:
            data = store.get(db, request.state.owner, "asset", key)
        path = (store.root / data["_path"]).resolve()
        if not path.is_relative_to(store.root) or not path.is_file():
            raise APIError(404, "NOT_FOUND", "Image not found.")
        return FileResponse(path, media_type=data["mime_type"], headers={"X-Content-Type-Options": "nosniff"})

    @app.post(PREFIX + "/research-reports/{key}/listing-confirmations")
    def confirm_listing(request: Request, key: str, body: ListingConfirmation):
        def save(db, owner):
            from agent_store import AgentError
            from research_decisions import confirmed_facts
            report = store.get(db, owner, "research_report", key)
            row = next((row for row in report["listings"] if row["listing_id"] == body.listing_id
                        and row["observed_at"] == body.observed_at), None)
            if row is None:
                raise APIError(404, "NOT_FOUND", "Observation not found in this research report.")
            try:
                fact = confirmed_facts(key, row, body.facts.model_dump())
            except (AgentError, ValueError, TypeError):
                raise APIError(422, "INVALID_INPUT", "Listing facts could not be confirmed.") from None
            existing = db.execute("SELECT owner,data FROM api_records WHERE kind='listing_fact' AND id=?", (fact["fact_id"],)).fetchone()
            if existing:
                old = json.loads(existing["data"])
                if existing["owner"] != owner or {k:v for k,v in old.items() if k != "confirmed_at"} != {k:v for k,v in fact.items() if k != "confirmed_at"}:
                    raise APIError(409, "ARTIFACT_CONFLICT", "Confirmed facts are immutable; use a new observation for corrections.")
                return old
            store.put(db, owner, "listing_fact", fact["fact_id"], fact)
            return fact
        return mutate(request, body.model_dump(), 201, save)

    @app.post(PREFIX + "/sale-drafts")
    def sale_draft(request: Request, body: DraftRequest):
        def accept(db, owner):
            owned = store.get(db, owner, "copy", body.copy_id)
            if not owned["allow_sale"] or owned["state"] != "owned":
                raise APIError(409, "INVALID_STATE", "This copy is protected or unavailable for sale.")
            if body.decision_id:
                store.get(db, owner, "decision", body.decision_id)
            return enqueue(db, owner, "sale_draft", body.model_dump())
        return mutate(request, body.model_dump(), 202, accept)

    @app.post(PREFIX + "/action-intents")
    def prepare(request: Request, body: IntentRequest):
        def create(db, owner):
            if body.execution_mode != "fixture" or not settings.fixture_listings or body.action != "buy":
                raise APIError(409, "FEATURE_UNAVAILABLE", "Only configured fixture purchases can execute; sale publication and live trading are unavailable.")
            if not body.decision_id or not body.listing_id or body.max_total_jpy is None or body.draft_id:
                raise APIError(422, "INVALID_INPUT", "Provide a decision, selected listing and total ceiling.")
            decision = store.get(db, owner, "decision", body.decision_id)
            if decision["source_mode"] != "synthetic_fixture":
                raise APIError(409, "INVALID_STATE", "A live observation cannot become a fixture trade.")
            listing = next((r for r in decision["purchase_candidates"] if r["id"] == body.listing_id), None)
            if not listing:
                raise APIError(422, "INVALID_INPUT", "Listing is not selected by this decision.")
            total = sum(listing[k] for k in ("price_jpy", "shipping_jpy", "purchase_fee_jpy"))
            if total > body.max_total_jpy:
                raise APIError(422, "COST_UNKNOWN", "Selected total exceeds the proposed ceiling.")
            previous = store.rows(db, owner, "action_intent")
            for old in previous:
                if old["state"] == "prepared" and old["_expiry"] <= time.time():
                    old["state"], old["authorization_state"] = "expired", "expired"
                    old["revision"] += 1
                    store.put(db, owner, "action_intent", old["intent_id"], old)
                    db.execute("UPDATE api_reservations SET state='released' WHERE intent=?", (old["intent_id"],))
            duplicate = [r for r in previous if r["listing_ref"] == body.listing_id
                         and r["state"] not in {"failed", "cancelled", "expired"}]
            if duplicate:
                raise APIError(409, "INVALID_STATE", "This listing already has an intent; inspect or reconcile it.")
            used = db.execute("SELECT COALESCE(SUM(total),0) FROM api_reservations WHERE owner=? AND state='reserved'", (owner,)).fetchone()[0]
            goal = decision["goal_snapshot"]
            if used + total > goal["budget_jpy"] - goal["reserve_jpy"]:
                raise APIError(409, "INVALID_STATE", "Reservations exceed the fixture budget.")
            key, expiry = uid("intent"), time.time() + 300
            payload = {"listing_id": body.listing_id, "quantity": 1, "max_total_jpy": body.max_total_jpy,
                       "currency": "JPY", "mode": "fixture"}
            data = {"intent_id": key, "idempotency_key": key, "action": "buy", "execution_mode": "fixture",
                    "marketplace": "test_marketplace", "decision_ref": body.decision_id, "draft_ref": None,
                    "evidence_refs": decision["evidence_refs"], "listing_ref": body.listing_id,
                    "payload": payload, "payload_hash": digest(payload), "limits": {"max_total_jpy": body.max_total_jpy},
                    "created_at": now(), "expires_at": datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
                    "policy_version": "fixture-1.0", "authorization_ref": None, "reservation_refs": [key],
                    "authorization_state": "required", "state": "prepared", "revision": 1,
                    "outcome": None, "_expiry": expiry}
            db.execute("INSERT INTO api_reservations VALUES(?,?,?,?)", (key, owner, total, "reserved"))
            store.put(db, owner, "action_intent", key, data)
            return public_record(data)
        return mutate(request, body.model_dump(), 201, create)

    @app.post(PREFIX + "/action-intents/{key}/authorize")
    def authorize(request: Request, key: str, body: Authorization):
        def save(db, owner):
            intent = store.get(db, owner, "action_intent", key)
            revision(request, intent)
            if intent["_expiry"] <= time.time():
                raise APIError(409, "INTENT_EXPIRED", "Prepare a fresh intent.")
            if intent["state"] != "prepared" or body.payload_hash != intent["payload_hash"]:
                raise APIError(409, "INVALID_STATE", "Payload or state no longer matches.")
            intent["authorization_state"], intent["authorization_ref"] = "authorized", uid("authorization")
            intent["revision"] += 1
            store.put(db, owner, "action_intent", key, intent)
            return public_record(intent)
        return mutate(request, body.model_dump(), 200, save)

    def action_job(request: Request, key: str, kind: str):
        def accept(db, owner):
            intent = store.get(db, owner, "action_intent", key)
            revision(request, intent)
            existing = [j for j in store.rows(db, owner, "job") if j["type"] == kind and j["_input"].get("intent_id") == key
                        and j["state"] not in {"failed", "cancelled"}]
            if existing:
                return public_job(existing[-1])
            if kind == "execute":
                if intent["state"] != "prepared":
                    raise APIError(409, "ACTION_ALREADY_SUBMITTED", "Reconcile the existing intent.")
                if intent["authorization_state"] != "authorized":
                    raise APIError(409, "AUTHORIZATION_REQUIRED", "Authorize the exact action first.")
                if intent["_expiry"] <= time.time():
                    raise APIError(409, "INTENT_EXPIRED", "Prepare a fresh intent.")
            elif intent["state"] == "prepared":
                raise APIError(409, "INVALID_STATE", "No action has been submitted.")
            return enqueue(db, owner, kind, {"intent_id": key})
        return mutate(request, {}, 202, accept)

    @app.post(PREFIX + "/action-intents/{key}/execute")
    def execute(request: Request, key: str, body: Empty):
        return action_job(request, key, "execute")

    @app.post(PREFIX + "/action-intents/{key}/reconcile")
    def reconcile(request: Request, key: str, body: Empty):
        return action_job(request, key, "reconcile")

    return app
