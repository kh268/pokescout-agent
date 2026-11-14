"""Shared research contract and HTTP client. Owned by the research-agent workstream.

The implementation lives in experiments/sc until its integration is approved.
No budget, collection, buy/sell decisions, or trading actions belong here.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Literal, NotRequired, TypedDict


class ConfirmedTarget(TypedDict):
    name_ja: str
    number: str
    rarity: NotRequired[str]
    language: NotRequired[str]


class SourceLimits(TypedDict, total=False):
    allowed_domains: list[str]
    max_searches: int
    max_inspections: int
    results_per_search: int
    timeout_seconds: int


class ResearchRequest(TypedDict):
    confirmed_target: ConfirmedTarget
    source_limits: NotRequired[SourceLimits]
    currency: NotRequired[Literal["JPY"]]
    raw_only: NotRequired[bool]
    accepted_conditions: NotRequired[list[str]]
    price_basis: NotRequired[Literal["item_price", "delivered_price"]]
    objective: NotRequired[str]


class Evidence(TypedDict):
    source_url: str
    observed_at: str
    kind: Literal["indexed_snippet", "extracted_page", "direct_page"]
    content: str
    sha256: str


class ListingObservation(TypedDict):
    listing_id: str
    url: str
    title: str
    price: float | None
    currency: str | None
    shipping: float | None
    total_price: float | None
    condition: str
    availability: Literal["available", "sold", "unknown"]
    observed_at: str
    verification: Literal["indexed_snippet", "extracted_page", "verified_listing"]
    evidence: list[Evidence]
    field_evidence: dict[str, str]
    qualifies: bool
    exclusion_reasons: list[str]


class CitedFinding(TypedDict):
    text: str
    source_urls: list[str]


class ResearchFailure(TypedDict):
    stage: str
    source_url: str | None
    message: str


class LowestObserved(TypedDict):
    listing_id: str
    url: str
    price: float
    currency: str
    price_basis: Literal["item_price", "delivered_price"]
    verification: str


class ResearchReport(TypedDict):
    schema_version: Literal["1.0"]
    run_id: str
    status: Literal["complete", "partial", "failed"]
    request: ResearchRequest
    started_at: str
    completed_at: str
    listings: list[ListingObservation]
    comparable_offers: list[ListingObservation]
    lowest_qualifying_observed_price: LowestObserved | None
    findings: list[CitedFinding]
    uncertainty: list[str]
    coverage: dict[str, object]
    failures: list[ResearchFailure]
    suggested_next_steps: list[str]
    log_path: str


class ResearchUnavailable(RuntimeError):
    """The research service could not supply a report; no fake fallback is used."""


async def research_market(request: ResearchRequest) -> ResearchReport:
    """Invoke the experimental AgentField reasoner without changing the main pipeline.

    Set RESEARCH_AGENT_URL to the agent origin (default http://127.0.0.1:18081).
    Returns observed evidence, never a buy/sell recommendation. Network errors
    raise ResearchUnavailable; source/model failures are returned in the report.
    """
    origin = os.getenv("RESEARCH_AGENT_URL", "http://127.0.0.1:18081").rstrip("/")
    timeout = request.get("source_limits", {}).get("timeout_seconds", 120) + 15

    def invoke() -> ResearchReport:
        # Direct node endpoints take function arguments; only the control plane uses input.
        payload = json.dumps({"request": request}).encode("utf-8")
        call = urllib.request.Request(
            origin + "/reasoners/research_market", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(call, timeout=timeout) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ResearchUnavailable("Research service unavailable; check RESEARCH_AGENT_URL and service logs.") from exc
        report = data.get("result", data) if isinstance(data, dict) else None
        if not isinstance(report, dict) or report.get("schema_version") != "1.0":
            raise ResearchUnavailable("Research service returned an unsupported report contract.")
        return report

    return await asyncio.to_thread(invoke)
