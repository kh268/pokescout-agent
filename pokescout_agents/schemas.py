"""Shared boundary types for identification, evidence, and decisions."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class CardIdentityCandidate(BaseModel):
    name: str | None = None
    set_name: str | None = None
    set_code: str | None = None
    card_number: str | None = None
    language: str | None = None
    variant: str | None = None
    grading_company: str | None = None
    grade: str | None = None
    unresolved_fields: list[str] = Field(default_factory=list)


class ListingEvidence(BaseModel):
    card_name: str | None = None
    set_code: str | None = None
    card_number: str | None = None
    language: str | None = None
    variant: str | None = None
    grading_company: str | None = None
    grade: str | None = None
    price_jpy: int | None = Field(default=None, ge=0)
    shipping_jpy: int | None = Field(default=None, ge=0)
    seller_condition: str | None = None
    defects: list[str] = Field(default_factory=list)
    available: bool | None = None
    evidence: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)


class ListingObservation(BaseModel):
    marketplace: str
    listing_id: str
    url: HttpUrl
    title: str
    description: str
    extracted: ListingEvidence
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TargetCard(BaseModel):
    name: str
    set_code: str
    card_number: str
    language: str = "Japanese"
    variant: str | None = None
    grading_company: str | None = None
    grade: str | None = None
    max_total_jpy: int = Field(gt=0)
    minimum_condition: Literal["any", "played", "lightly_played", "near_mint"] = "any"
    allowed_defects: list[str] = Field(default_factory=list)


class Evaluation(BaseModel):
    eligible: bool
    reasons: list[str]
    total_jpy: int | None
    score: float | None


class ResearchSource(BaseModel):
    source_id: str
    url: HttpUrl
    title: str | None = None
    content: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: str | None = None


class ResearchClaim(BaseModel):
    claim: str
    source_ids: list[str]


class ResearchSummary(BaseModel):
    claims: list[ResearchClaim]
    uncertainties: list[str]


class SaleFacts(BaseModel):
    copy_id: str
    name: str
    set_code: str
    card_number: str
    language: str
    variant: str | None = None
    grading_company: str | None = None
    grade: str | None = None
    defects: list[str] = Field(default_factory=list)
    price_jpy: int = Field(gt=0)
    shipping_terms: str


class SaleDraft(BaseModel):
    title: str
    description: str
    defect_disclosures: dict[str, str] = Field(default_factory=dict)
    claim_map: dict[str, list[str]] = Field(default_factory=dict)
    unresolved_questions: list[str] = Field(default_factory=list)
