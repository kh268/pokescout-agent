"""Main-agent trading boundary. Only the team-owned HTTP fixture is enabled."""
from __future__ import annotations

import asyncio
import html
import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from agent_store import AgentError, digest, encoded, new_id, utcnow
from cards import money
from research_decisions import parse_time


class FixtureExecutor:
    """Calls the existing TestMarket HTTP server; no browser or live-market claim."""
    def __init__(self, origin):
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path:
            raise ValueError("Fixture executor requires an explicit loopback TestMarket origin.")
        self.origin = origin

    async def _request(self, path, body=None):
        def call():
            request = urllib.request.Request(self.origin + path, data=None if body is None else encoded(body).encode("utf-8"),
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Fixture response exceeded limit.")
            return raw.decode("utf-8")
        return await asyncio.to_thread(call)

    async def preflight(self, intent):
        raw = await self._request("/offer/" + intent["listing_ref"]["market_listing_id"])
        match = re.search(r'<pre id="offer">(.*?)</pre>', raw, re.S)
        if not match:
            raise ValueError("Fixture offer missing.")
        return json.loads(html.unescape(match[1]))

    async def submit(self, intent):
        await self._request("/purchase", {"intent": intent["intent_id"],
            "listing": intent["listing_ref"]["market_listing_id"], "ceiling": intent["limits"]["max_total_jpy"]})

    async def lookup(self, intent):
        try:
            return json.loads(await self._request("/order/" + intent["intent_id"]))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise


class Trading:
    def __init__(self, store, executor=None):
        self.store, self.executor = store, executor

    def prepare(self, decision_id, listing_id, max_total_jpy, *, key, execution_mode="fixture", expires_in=600):
        request = {"decision_id": decision_id, "listing_id": listing_id, "max_total_jpy": max_total_jpy,
                   "execution_mode": execution_mode, "expires_in": expires_in}
        with self.store.transaction():
            replay = self.store.replay("prepare", key, request)
            if replay:
                return self.store.get_intent(replay["intent_id"])
            if execution_mode != "fixture" or self.executor is None:
                raise AgentError("FEATURE_UNAVAILABLE", "Only an explicitly configured test-marketplace executor is available.")
            money(max_total_jpy, "max_total_jpy")
            if type(expires_in) is not int or not 1 <= expires_in <= 3600:
                raise AgentError("INVALID_INPUT", "Intent lifetime must be 1–3600 seconds.")
            decision = self.store.read("decisions", decision_id)
            candidate = next((c for c in decision["purchase_candidates"] if c["listing_ref"]["observation_id"] == listing_id), None)
            if decision["recommendation"] != "buy" or candidate is None:
                raise AgentError("INVALID_INPUT", "Select a purchase candidate from this persisted decision.")
            offer = candidate["observation"]
            if offer["source_mode"] != "fixture" or not offer["source_url"].startswith("fixture://"):
                raise AgentError("FEATURE_UNAVAILABLE", "Real research observations cannot be submitted to the fixture as real trades.")
            if max_total_jpy < candidate["acquisition_total_jpy"] or max_total_jpy > decision["calculations"]["available_allowance_jpy"]:
                raise AgentError("INVALID_INPUT", "The ceiling must cover the observed total and respect the available allowance.")
            payload = {k: offer[k] for k in ("card", "price_jpy", "shipping_jpy", "purchase_fee_jpy", "condition", "availability", "quantity")}
            intent_id = new_id("intent")
            limits = {"currency": "JPY", "max_total_jpy": max_total_jpy,
                      "budget_jpy": decision["input_snapshot"]["goal"]["budget_jpy"],
                      "reserve_jpy": decision["input_snapshot"]["goal"]["reserve_jpy"]}
            listing_ref = dict(candidate["listing_ref"], market_listing_id=urlsplit(offer["source_url"]).netloc)
            binding = {"action": "buy", "execution_mode": "fixture", "marketplace": "test_marketplace",
                       "listing_ref": listing_ref, "payload": payload, "limits": limits}
            intent = {"schema_version": "1.0", "intent_id": intent_id, "idempotency_key": key, **binding,
                "payload_hash": digest(binding), "decision_ref": decision_id, "draft_ref": None,
                "evidence_refs": [candidate["listing_ref"]], "created_at": utcnow(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(),
                "policy_version": "fixture-only-1.0", "authorization_ref": None,
                "reservation_refs": [], "state": "prepared", "revision": 1, "outcome": None}
            self.store.db.execute("INSERT INTO main_intents VALUES(?,?)", (intent_id, encoded(intent)))
            self.store.remember("prepare", key, request, {"intent_id": intent_id})
            return intent

    def authorize(self, intent_id, payload_hash, *, revision, key):
        request = {"intent_id": intent_id, "payload_hash": payload_hash}
        with self.store.transaction():
            replay = self.store.replay("authorize", key, request)
            intent = self.store.get_intent(intent_id)
            if replay:
                return intent
            self.store.require_revision(intent, revision)
            if intent["state"] != "prepared" or payload_hash != intent["payload_hash"]:
                raise AgentError("INVALID_STATE", "Review and authorize the exact prepared payload hash.")
            if parse_time(intent["expires_at"]) <= datetime.now(timezone.utc):
                raise AgentError("INTENT_EXPIRED", "Prepare a new intent; this one expired.")
            auth = {"authorization_id": new_id("authorization"), "intent_id": intent_id,
                    "payload_hash": payload_hash, "authorized_at": utcnow(), "source": "explicit_local_user_action"}
            self.store.artifact("snapshots", auth["authorization_id"], auth)
            intent.update(state="authorized", authorization_ref=auth["authorization_id"], revision=intent["revision"] + 1)
            self.store.put_intent(intent)
            self.store.remember("authorize", key, request, {"intent_id": intent_id})
            return intent

    def _finish(self, intent_id, status, *, order=None, message=None):
        with self.store.transaction():
            intent = self.store.get_intent(intent_id)
            if intent["state"] == "confirmed":
                return intent
            outcome = {"status": status, "observed_at": utcnow(), "receipt_ref": order["receipt"] if order else None,
                       "actual_total_jpy": order["total"] if order else None,
                       "evidence_refs": ["fixture-order:" + intent_id] if order else [],
                       "error": None if message is None else {"code": "FIXTURE_" + status.upper(), "message": message,
                           "retryable": False, "field_errors": [], "resource_url": None},
                       "next_step": "confirm physical receipt separately" if status == "confirmed" else "reconcile this intent" if status == "unknown" else "review and prepare a new intent",
                       "ownership_state": "purchase_pending_receipt" if status == "confirmed" else None,
                       "execution_mode": "fixture", "transport": "local test-marketplace HTTP; no browser invoked"}
            intent.update(state=status, outcome=outcome, revision=intent["revision"] + 1)
            self.store.put_intent(intent)
            reservation_state = "confirmed" if status == "confirmed" else "unknown" if status == "unknown" else "released"
            self.store.db.execute("UPDATE main_fixture_reservations SET state=? WHERE intent_id=?", (reservation_state, intent_id))
            if order:
                self.store.artifact("snapshots", "receipt-" + digest(intent_id), {"mode": "fixture", "order": order})
            return intent

    @staticmethod
    def _verify_order(intent, order):
        return (isinstance(order, dict) and order.get("intent") == intent["intent_id"]
            and order.get("listing") == intent["listing_ref"]["market_listing_id"]
            and type(order.get("total")) is int and 0 <= order["total"] <= intent["limits"]["max_total_jpy"]
            and order["total"] == sum(intent["payload"][k] for k in ("price_jpy", "shipping_jpy", "purchase_fee_jpy"))
            and isinstance(order.get("receipt"), str) and order["receipt"].startswith("TEST-"))

    async def execute(self, intent_id):
        if self.executor is None:
            raise AgentError("FEATURE_UNAVAILABLE", "Configure the team-owned test marketplace executor.")
        with self.store.transaction():
            intent = self.store.get_intent(intent_id)
            if intent["state"] == "confirmed":
                return intent
            if intent["state"] in {"submitting", "unknown", "reserved"}:
                raise AgentError("INVALID_STATE", "This action may be in progress; reconcile it instead of resubmitting.")
            if intent["state"] != "authorized" or not intent["authorization_ref"]:
                raise AgentError("AUTHORIZATION_REQUIRED", "Persist authorization for this exact payload first.")
            binding = {k: intent[k] for k in ("action", "execution_mode", "marketplace", "listing_ref", "payload", "limits")}
            if digest(binding) != intent["payload_hash"]:
                raise AgentError("INVALID_STATE", "The authorized payload changed.")
            auth = self.store.read("snapshots", intent["authorization_ref"])
            if auth["payload_hash"] != intent["payload_hash"] or auth["intent_id"] != intent_id:
                raise AgentError("AUTHORIZATION_REQUIRED", "Authorization does not cover this payload.")
            if parse_time(intent["expires_at"]) <= datetime.now(timezone.utc):
                raise AgentError("INTENT_EXPIRED", "Expired intents cannot submit a new action.")
            used = self.store.db.execute("SELECT COALESCE(SUM(total),0) FROM main_fixture_reservations WHERE state IN ('reserved','unknown','confirmed')").fetchone()[0]
            if used + intent["limits"]["max_total_jpy"] > intent["limits"]["budget_jpy"] - intent["limits"]["reserve_jpy"]:
                raise AgentError("INVALID_STATE", "Active reservations or earlier fixture purchases consume this allowance.")
            try:
                self.store.db.execute("INSERT INTO main_fixture_reservations VALUES(?,?,?,'reserved')",
                    (intent_id, intent["listing_ref"]["source_url"], intent["limits"]["max_total_jpy"]))
            except sqlite3.IntegrityError:
                raise AgentError("INVALID_STATE", "Another intent already reserves this listing.") from None
            intent.update(state="reserved", reservation_refs=[intent_id], revision=intent["revision"] + 1)
            self.store.put_intent(intent)
        try:
            fresh = await self.executor.preflight(intent)
            expected = intent["payload"]
            if any(fresh.get(k) != v for k, v in expected.items()) or fresh.get("availability") != "available":
                return self._finish(intent_id, "needs_user_action", message="The listing identity, condition, quantity or costs changed. No submission was made.")
            with self.store.transaction():
                intent = self.store.get_intent(intent_id)
                if parse_time(intent["expires_at"]) <= datetime.now(timezone.utc):
                    raise AgentError("INTENT_EXPIRED", "Intent expired during preflight.")
                intent.update(state="submitting", revision=intent["revision"] + 1)
                self.store.put_intent(intent)
        except Exception:
            return self._finish(intent_id, "failed", message="Fixture preflight failed before submission. Review the intent.")
        try:
            await self.executor.submit(intent)
            order = await self.executor.lookup(intent)
            if not self._verify_order(intent, order):
                raise ValueError("Independent order verification failed.")
            return self._finish(intent_id, "confirmed", order=order)
        except Exception:
            return self._finish(intent_id, "unknown", message="Submission may have succeeded. Reconcile before any further submission.")

    async def reconcile(self, intent_id):
        intent = self.store.get_intent(intent_id)
        if intent["state"] == "confirmed":
            return intent
        if self.executor is None:
            raise AgentError("FEATURE_UNAVAILABLE", "The original fixture marketplace must be available for reconciliation.")
        if intent["state"] not in {"unknown", "submitting", "reserved"}:
            raise AgentError("INVALID_STATE", "Only potentially submitted actions need reconciliation.")
        try:
            order = await self.executor.lookup(intent)
            if self._verify_order(intent, order):
                return self._finish(intent_id, "confirmed", order=order)
        except Exception:
            pass
        return self._finish(intent_id, "unknown", message="No verified outcome recovered; retain the reservation and do not resubmit.")
