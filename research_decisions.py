"""Evidence-preserving adapter around the existing deterministic purchase planner.

Research v1 does not carry full printing, quantity, purchase fees or defects.
Those fields require an explicit, per-observation confirmation sidecar. A target
or a research 'qualifies' flag never supplies the missing listing facts.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from agent_inputs import CONDITIONS, card_input, object_fields
from agent_store import AgentError, digest, new_id, utcnow
from cards import identity_key, money
from decisions import cohort, plan


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("A timezone-aware observation timestamp is required.")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A timezone-aware observation timestamp is required.")
    return result


def integral_jpy(value):
    if value is None:
        return None
    if type(value) is float and math.isfinite(value) and value.is_integer():
        value = int(value)
    return money(value)


def observation_key(run_id, row):
    return "observation-" + digest([run_id, row["listing_id"], row["observed_at"]])


def canonical_listing(url, source_mode):
    parsed = urlsplit(url)
    if source_mode == "fixture" and parsed.scheme == "fixture" and parsed.netloc and not parsed.path:
        return url
    if (parsed.scheme != "https" or parsed.hostname not in {"jp.mercari.com", "item.fril.jp"}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Unsupported marketplace URL.")
    import re
    pattern = r"/item/m\d+" if parsed.hostname == "jp.mercari.com" else r"/[a-fA-F0-9]{20,64}"
    if not re.fullmatch(pattern, parsed.path.rstrip("/")):
        raise ValueError("Unsupported marketplace listing path.")
    return urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), "", ""))


def validate_report(report):
    if not isinstance(report, dict) or report.get("schema_version") != "1.0":
        raise AgentError("INVALID_RESPONSE", "Unsupported research report schema.")
    if report.get("status") not in {"complete", "partial", "failed"}:
        raise AgentError("INVALID_RESPONSE", "Invalid research status.")
    for field in ("run_id", "started_at", "completed_at"):
        if not isinstance(report.get(field), str):
            raise AgentError("INVALID_RESPONSE", "Research metadata is missing.")
    parse_time(report["started_at"])
    parse_time(report["completed_at"])
    if not isinstance(report.get("request"), dict):
        raise AgentError("INVALID_RESPONSE", "Research request snapshot is missing.")
    for field in ("listings", "findings", "uncertainty", "failures", "suggested_next_steps", "comparable_offers"):
        if not isinstance(report.get(field), list):
            raise AgentError("INVALID_RESPONSE", "Invalid research report arrays.")
    if len(report["listings"]) > 32:
        raise AgentError("INVALID_RESPONSE", "Research exceeded the bounded observation limit.")
    seen = set()
    for row in report["listings"]:
        if not isinstance(row, dict) or not all(isinstance(row.get(f), str) for f in ("listing_id", "url", "observed_at")):
            raise AgentError("INVALID_RESPONSE", "Invalid research observation reference.")
        key = observation_key(report["run_id"], row)
        if key in seen:
            raise AgentError("INVALID_RESPONSE", "Duplicate observation reference in research report.")
        seen.add(key)
        if not isinstance(row.get("evidence"), list) or not isinstance(row.get("field_evidence"), dict):
            raise AgentError("INVALID_RESPONSE", "Research observation evidence is missing.")
    return report


def confirmed_facts(run_id, row, value, *, fixture=False):
    object_fields(value, {"card", "quantity", "purchase_fee_jpy", "condition_record", "registry_status"}, ("card", "quantity", "purchase_fee_jpy"))
    card = card_input(value["card"], confirmed=True)
    quantity = value["quantity"]
    if type(quantity) is not int or not 1 <= quantity <= 100:
        raise AgentError("INVALID_INPUT", "Confirm the actual listing quantity.")
    fee = value["purchase_fee_jpy"]
    if fee is not None:
        money(fee, "purchase_fee_jpy")
    condition = value.get("condition_record")
    if condition is not None:
        object_fields(condition, {"condition_raw", "condition_normalized", "condition_basis"},
                      ("condition_raw", "condition_normalized", "condition_basis"))
        if condition["condition_raw"] != row.get("condition"):
            raise AgentError("INVALID_INPUT", "Retain the report's exact condition wording.")
        if condition["condition_normalized"] not in CONDITIONS | {"unknown"} or condition["condition_basis"] != "user_confirmed":
            raise AgentError("INVALID_INPUT", "Normalization requires explicit user confirmation.")
    registry = value.get("registry_status", "not_checked")
    if registry not in {"registry_match", "registry_mismatch", "lookup_unavailable", "not_checked"}:
        raise AgentError("INVALID_INPUT", "Invalid registry status.")
    return {"schema_version": "1.0", "fact_id": observation_key(run_id, row), "run_id": run_id,
            "listing_id": row["listing_id"], "observed_at": row["observed_at"], "card": card,
            "quantity": quantity, "purchase_fee_jpy": fee, "condition_record": condition,
            "registry_status": registry, "confirmed_at": utcnow(),
            "confirmation_basis": "synthetic_fixture" if fixture else "explicit_user_confirmation"}


def adapt_reports(reports, facts, source_modes, *, now=None, max_age_seconds=900,
                  pending_listing_ids=None):
    """Retain every rejection and observation ref; only pass validated rows to plan()."""
    now = now or datetime.now(timezone.utc)
    offers, rejected, evidence_refs, seen = [], [], [], {}
    # Newer sold/unknown/unverified observations invalidate older positive stock,
    # including when the newer observation itself cannot enter the planner.
    latest, conflicting = {}, set()
    for report in reports:
        for row in report["listings"]:
            try:
                url = canonical_listing(row["url"], source_modes[report["run_id"]])
                stamp = parse_time(row["observed_at"])
            except (TypeError, ValueError):
                continue
            fingerprint = digest({k: row.get(k) for k in ("price", "shipping", "currency", "availability", "condition", "verification")})
            entry = latest.get(url)
            if entry is None or stamp > entry[0]:
                latest[url] = (stamp, observation_key(report["run_id"], row), fingerprint)
                conflicting.discard(url)
            elif stamp == entry[0] and fingerprint != entry[2]:
                conflicting.add(url)
    for report in reports:
        run_id = report["run_id"]
        mode = source_modes[run_id]
        for row in report["listings"]:
            key = observation_key(run_id, row)
            ref = {"observation_id": key, "run_id": run_id, "listing_id": row["listing_id"],
                   "source_url": row["url"], "observed_at": row["observed_at"]}
            evidence_refs.append(ref)
            reasons = []
            try:
                canonical = canonical_listing(row["url"], mode)
            except (TypeError, ValueError):
                canonical = key
                reasons.append("SOURCE_NOT_ALLOWED")
            latest_observation = latest.get(canonical)
            if canonical in conflicting:
                reasons.append("CONFLICTING_OBSERVATIONS")
            elif latest_observation and latest_observation[1] != key:
                reasons.append("DUPLICATE_OBSERVATION")
            try:
                age = (now - parse_time(row["observed_at"])).total_seconds()
                if mode != "fixture" and not -5 <= age <= max_age_seconds:
                    reasons.append("EVIDENCE_STALE")
            except (ValueError, TypeError):
                reasons.append("OBSERVATION_TIME_UNKNOWN")
            if row.get("verification") != "verified_listing":
                reasons.append("SOURCE_NOT_VERIFIED")
            if mode != "fixture":
                direct = [e for e in row["evidence"] if e.get("kind") == "direct_page" and e.get("source_url") == row["url"]]
                if not direct:
                    reasons.append("DIRECT_EVIDENCE_MISSING")
                texts = "\n".join(e.get("content", "") for e in direct)
                if any(not isinstance(quote, str) or not quote or quote not in texts for quote in row["field_evidence"].values()):
                    reasons.append("FIELD_EVIDENCE_UNSUPPORTED")
            if row.get("availability") != "available":
                reasons.append("NOT_AVAILABLE")
            if row["listing_id"] in (pending_listing_ids or set()):
                reasons.append("DUPLICATE_OR_PENDING_PURCHASE")
            if row.get("currency") != "JPY":
                reasons.append("CURRENCY_MISMATCH")
            fact = facts.get(key)
            if fact is None:
                reasons.append("LISTING_FACTS_UNCONFIRMED")
            costs = {}
            for dest, src in (("price_jpy", "price"), ("shipping_jpy", "shipping")):
                try:
                    costs[dest] = integral_jpy(row.get(src))
                except ValueError:
                    costs[dest] = None
                    reasons.append("INVALID_JPY_AMOUNT")
            fee = fact["purchase_fee_jpy"] if fact else None
            if fee is None or any(v is None for v in costs.values()):
                reasons.append("ACQUISITION_TOTAL_UNKNOWN")
            if fact:
                condition_record = fact["condition_record"]
                # Canonical condition words are preserved as seller claims. Shop letters
                # and other raw wording need the user's explicit per-observation mapping.
                condition = condition_record["condition_normalized"] if condition_record else row.get("condition", "unknown")
                if condition not in CONDITIONS:
                    condition = "unknown"
                offer = {"id": key, "card": fact["card"], **costs, "purchase_fee_jpy": fee,
                         "condition": condition, "condition_record": condition_record,
                         "availability": row.get("availability"), "quantity": fact["quantity"],
                         "registry_status": fact["registry_status"], "source_url": row["url"],
                         "observed_at": row["observed_at"], "ref": ref, "source_mode": mode}
            else:
                offer = None
            if reasons:
                rejected.append({"candidate_ref": ref, "reasons": sorted(set(reasons))})
                continue
            # URL, not the experiment's per-run s1/s2 IDs, scopes physical offers.
            if canonical in seen:
                previous = seen[canonical]
                if parse_time(offer["observed_at"]) <= parse_time(previous["observed_at"]):
                    rejected.append({"candidate_ref": ref, "reasons": ["DUPLICATE_OBSERVATION"]})
                    continue
                offers.remove(previous)
                rejected.append({"candidate_ref": previous["ref"], "reasons": ["DUPLICATE_OBSERVATION"]})
            seen[canonical] = offer
            offers.append(offer)
    certs = {}
    for offer in offers:
        cert = offer["card"].get("cert_number")
        if cert:
            certs.setdefault((offer["card"].get("grader"), cert), []).append(offer)
    duplicates = {o["id"] for group in certs.values() if len(group) > 1 for o in group}
    for offer in list(offers):
        if offer["id"] in duplicates:
            offers.remove(offer)
            rejected.append({"candidate_ref": offer["ref"], "reasons": ["DUPLICATE_CERTIFICATE_REVIEW"]})
    # Preserve the existing planner's audited exhaustive-search bound.
    offers.sort(key=lambda o: (sum(o[k] for k in ("price_jpy", "shipping_jpy", "purchase_fee_jpy")), o["id"]))
    for offer in offers[16:]:
        rejected.append({"candidate_ref": offer["ref"], "reasons": ["PLANNER_CANDIDATE_LIMIT"]})
    return offers[:16], rejected, evidence_refs


def plan_decision(goal, collection, reports, facts, source_modes, *, max_age_seconds=900,
                  pending_listing_ids=None):
    """Decision/trading contract wrapper; makes no reservations or external trades."""
    offers, rejections, refs = adapt_reports(reports, facts, source_modes,
        max_age_seconds=max_age_seconds, pending_listing_ids=pending_listing_ids)
    decision_id = new_id("decision")
    result = {"schema_version": "1.0", "decision_id": decision_id, "created_at": utcnow(),
              "rule_version": "main-agent-1.0/planner-0.2", "goal_ref": "goal-" + digest(goal),
              "collection_ref": collection["snapshot_id"], "research_refs": [r["run_id"] for r in reports],
              "recommendation": "insufficient_evidence", "purchase_candidates": [], "sale_candidates": [],
              "calculations": {"currency": "JPY", "confirmed_cash_jpy": None,
                  "spending_allowance_jpy": goal["budget_jpy"], "reserve_jpy": goal["reserve_jpy"],
                  "active_reservations_jpy": collection["active_reservations_jpy"], "purchase_total_jpy": 0},
              "rejections": rejections, "rationale": [], "evidence_refs": refs, "requirements": [],
              "coverage": [{"run_id": r["run_id"], "status": r["status"], "coverage": r.get("coverage", {}),
                            "uncertainty": r["uncertainty"], "failures": r["failures"]} for r in reports],
              "input_snapshot": {"goal": goal, "collection": collection},
              "execution": "none", "source_modes": source_modes}
    incomplete_evidence = bool(rejections) or any(r["status"] != "complete" for r in reports)
    if goal["intent"] == "sell":
        # Selection of sale copies is supported; asking-price valuation isn't yet implemented.
        by_id = {c["id"]: c for c in collection["copies"]}
        result["sale_candidates"] = [{"copy_id": i, "asking_price_jpy": None,
            "required_defects": by_id[i]["card"]["defects"]} for i in goal["copy_ids"]
            if i in by_id and by_id[i]["allow_sale"] and by_id[i]["state"] == "owned"]
        result["requirements"] = ["ASKING_PRICE_REQUIRED", "SALE_VALUATION_UNAVAILABLE"]
        result["rationale"] = ["Eligible copies selected; choose an explicit asking price to request a review-only draft."]
        return result
    planner_goal = dict(goal, active_reservations_jpy=collection["active_reservations_jpy"],
                        source_mode="research_contract_adapter")
    planned = plan(planner_goal, offers, collection["copies"])
    result["planner_result"] = planned
    result["calculations"].update(available_cash_jpy=None,
        available_allowance_jpy=max(0, goal["budget_jpy"] - goal["reserve_jpy"] - collection["active_reservations_jpy"]),
        purchase_total_jpy=planned["cost_jpy"], remaining_allowance_jpy=planned["remaining_allowance_jpy"])
    by_id = {o["id"]: o for o in offers}
    for checked in planned["checked"]:
        offer = by_id[checked["id"]]
        if checked["id"] in planned["selected_ids"]:
            result["purchase_candidates"].append({"listing_ref": offer["ref"], "quantity": 1,
                "observed_price_jpy": offer["price_jpy"], "acquisition_total_jpy": checked["total_jpy"],
                "observation": offer})
        else:
            reasons = checked["reasons"] or (["OVER_BUDGET"] if checked["total_jpy"] > result["calculations"]["available_allowance_jpy"] else ["OTHER_FEASIBLE_ALLOCATION"])
            result["rejections"].append({"candidate_ref": offer["ref"], "reasons": reasons})
    owned_complete = all(sum(1 for c in collection["copies"] if c["state"] == "owned"
        and identity_key(c["card"]) == identity_key(t["card"]) and cohort(c["card"]) == cohort(t["card"])) >= t["quantity"]
        for t in goal["targets"]) if goal["avoid_owned"] else False
    if result["purchase_candidates"]:
        result["recommendation"] = "buy"
        result["requirements"] = ["FRESH_PREFLIGHT_AND_SEPARATE_AUTHORIZATION_REQUIRED"]
        result["rationale"] = ["Maximized requested units, then minimized total JPY among at most 16 eligible supplied offers.",
                                 "Research coverage is bounded; this is not a whole-market cheapest-price claim."]
    elif owned_complete:
        result["recommendation"] = "hold"
        result["rationale"] = ["The requested quantities are already owned and duplicate avoidance is enabled."]
    elif any(c["eligible"] for c in planned["checked"]) and not incomplete_evidence:
        result["recommendation"] = "hold"
        result["rationale"] = ["Inspected qualifying offers exceed the available spending allowance."]
    else:
        result["requirements"] = ["SUFFICIENT_VERIFIED_COMPATIBLE_EVIDENCE_REQUIRED"]
        result["rationale"] = ["No affordable selection is supported by sufficient fresh, compatible, complete-cost evidence."]
    return result
