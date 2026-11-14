"""Shared, standard-library-only activity publisher for every PokeScout agent."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

HEADER = "X-PokeScout-Activity"
DEFAULT_PUBLISH_URL = "http://127.0.0.1:8000/internal/v1/activity/events"
LOGGER = logging.getLogger(__name__)
CURRENT: ContextVar[ActivityEmitter | None] = ContextVar("activity_emitter", default=None)
PARENT: ContextVar[str | None] = ContextVar("activity_parent", default=None)


@dataclass(frozen=True)
class ActivityContext:
    job_id: str
    producer_id: str
    publisher_token: str = field(repr=False)
    publish_url: str = DEFAULT_PUBLISH_URL
    parent_span_id: str | None = None

    def to_header(self) -> str:
        """Transport credentials outside domain arguments and SDK input journals."""
        return base64.urlsafe_b64encode(json.dumps(self.__dict__).encode()).decode()

    @classmethod
    def from_header(cls, value: str) -> ActivityContext:
        if len(value) > 8192:
            raise ValueError("Activity context too large")
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        context = cls(**data)
        expected = os.getenv("ACTIVITY_PUBLISH_URL", DEFAULT_PUBLISH_URL)
        if context.publish_url != expected:
            raise ValueError("Unexpected activity service address")
        for name in ("job_id", "producer_id", "publisher_token"):
            item = getattr(context, name)
            if not isinstance(item, str) or not item or len(item) > 256:
                raise ValueError("Invalid activity context")
        return context


class ActivityEmitter:
    """Bounded background publisher. Delivery failure never reruns provider work."""

    def __init__(self, context: ActivityContext) -> None:
        self.context = context
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=200)
        self.worker: asyncio.Task | None = None
        self.dropped = 0
        self.delivered = 0

    async def __aenter__(self) -> ActivityEmitter:
        self.worker = asyncio.create_task(self._work())
        return self

    async def __aexit__(self, *_: object) -> None:
        try:
            await asyncio.wait_for(self.queue.join(), timeout=5)
        except TimeoutError:
            LOGGER.warning("Activity delivery incomplete; private run journal remains authoritative.")
        finally:
            if self.worker:
                self.worker.cancel()
                try:
                    await self.worker
                except asyncio.CancelledError:
                    pass

    def emit(self, event_type: str, operation: str, message: str, *, span_id: str | None = None,
             parent_span_id: str | None = None, attempt: int = 1, level: str = "info",
             data: dict | None = None) -> str:
        event_id = str(uuid.uuid4())
        event = {"schema_version": "1.0", "event_id": event_id, "job_id": self.context.job_id,
                 "type": event_type, "operation": operation, "span_id": span_id or PARENT.get() or event_id,
                 "parent_span_id": parent_span_id, "attempt": attempt,
                 "occurred_at": datetime.now(timezone.utc).isoformat(), "level": level,
                 "message": message[:500], "data": {"provider": None, "progress": None, "counts": {},
                 "references": [], "result_status": None, "error_code": None, **(data or {})}}
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            LOGGER.warning("Activity queue full; update retained only in private journal.")
        return event_id

    def _post(self, event: dict) -> bool:
        payload = json.dumps(event, ensure_ascii=False).encode()
        for attempt in range(3):
            call = urllib.request.Request(self.context.publish_url, data=payload, method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.context.publisher_token})
            try:
                # Redirects must not forward the publisher credential to another service.
                with urllib.request.build_opener(_NoRedirect()).open(call, timeout=2) as response:
                    return response.status in (200, 201)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    LOGGER.warning("Activity event rejected (HTTP %s).", exc.code)
                    return False
                delay = min(1.0, max(0.0, float(exc.headers.get("Retry-After", "0.25")))) if attempt < 2 else 0
            except (urllib.error.URLError, TimeoutError, OSError):
                delay = 0.25 if attempt < 2 else 0
            if delay:
                time.sleep(delay)
        LOGGER.warning("Activity service unavailable; agent work continues.")
        return False

    async def _work(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                if event is not None and await asyncio.to_thread(self._post, event):
                    self.delivered += 1
                else:
                    self.dropped += 1
            except Exception:
                self.dropped += 1
                LOGGER.warning("Activity delivery failed; no provider work was retried.")
            finally:
                self.queue.task_done()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def emit(event_type: str, operation: str, message: str, **kwargs: object) -> None:
    publisher = CURRENT.get()
    if publisher:
        publisher.emit(event_type, operation, message, **kwargs)


@asynccontextmanager
async def step(operation: str, message: str, *, provider: str | None = None) -> AsyncIterator[str]:
    span = "span_" + uuid.uuid4().hex
    parent = PARENT.get()
    emit("step.started", operation, message, span_id=span, parent_span_id=parent, data={"provider": provider})
    token = PARENT.set(span)
    try:
        # Let the publisher start before SDK calls that may block the event loop.
        await asyncio.sleep(0)
        yield span
    except BaseException:
        emit("step.failed", operation, "Operation did not complete.", span_id=span, parent_span_id=parent,
             level="error", data={"provider": provider, "error_code": "STEP_FAILED"})
        raise
    else:
        emit("step.completed", operation, "Operation completed.", span_id=span, parent_span_id=parent,
             data={"provider": provider})
    finally:
        PARENT.reset(token)


class ActivityMiddleware:
    """Bind a header-supplied emitter without putting credentials in AgentField inputs."""

    def __init__(self, app: object) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: object, send: object) -> None:
        value = next((v for k, v in scope.get("headers", []) if k.lower() == HEADER.lower().encode()), None)
        if scope["type"] != "http" or value is None:
            await self.app(scope, receive, send)
            return
        try:
            context = ActivityContext.from_header(value.decode("ascii"))
        except (ValueError, TypeError, UnicodeError):
            body = b'{"detail":"Invalid activity context"}'
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        # Remove the private header before downstream middleware/SDK request logging.
        scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k.lower() != HEADER.lower().encode()]}
        async with ActivityEmitter(context) as publisher:
            emitter_token = CURRENT.set(publisher)
            parent_token = PARENT.set(context.parent_span_id)
            try:
                await self.app(scope, receive, send)
            finally:
                PARENT.reset(parent_token)
                CURRENT.reset(emitter_token)
