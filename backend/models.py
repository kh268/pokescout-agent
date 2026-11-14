"""Strict public request models. Monetary amounts use integer JPY."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

Money = Annotated[int, Field(strict=True, ge=0, le=100_000_000)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Card(Model):
    name: str | None
    set: str | None
    number: str | None
    language: str | None
    variant: str | None
    year: str | None = None
    brand: str | None = None
    presentation: Literal["raw", "slabbed"] | None
    grader: str | None = None
    grade: str | None = None
    grade_text: str | None = None
    cert_number: str | None = None
    qualifiers: list[str] = Field(default_factory=list, max_length=30)
    defects: list[str] = Field(default_factory=list, max_length=30)
    uncertainties: list[str] = Field(default_factory=list, max_length=30)


class ConfirmCopy(Model):
    candidate_id: str
    confirmed_card: Card
    confirm_owned: Literal[True]
    allow_sale: StrictBool = False
    cost_jpy: Money | None = None


class Target(Model):
    card: Card
    quantity: Annotated[int, Field(strict=True, ge=1, le=10)] = 1


class Goal(Model):
    message: str = Field(min_length=1, max_length=4000)
    intent: Literal["buy", "sell", "assess"]
    targets: list[Target] = Field(default_factory=list, max_length=10)
    copy_ids: list[str] = Field(default_factory=list, max_length=10)
    budget_jpy: Money | None = None
    reserve_jpy: Money = 0
    allowed_conditions: list[Literal["near_mint", "unused", "played", "damaged"]] | None = None
    avoid_owned: StrictBool = True
    allow_defects: StrictBool = False


class Limits(Model):
    allowed_domains: list[Literal["jp.mercari.com", "item.fril.jp"]] = Field(
        default_factory=lambda: ["jp.mercari.com", "item.fril.jp"], min_length=1, max_length=2)
    max_searches: int = Field(default=3, ge=1, le=4, strict=True)
    max_inspections: int = Field(default=4, ge=1, le=6, strict=True)
    results_per_search: int = Field(default=5, ge=1, le=8, strict=True)
    timeout_seconds: int = Field(default=120, ge=1, le=180, strict=True)


class ConfirmedTarget(Model):
    name_ja: str = Field(min_length=1, max_length=300)
    number: str = Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9-]+$")
    rarity: str | None = None
    language: str = "Japanese"


class ResearchRequest(Model):
    confirmed_target: ConfirmedTarget
    source_limits: Limits = Field(default_factory=Limits)
    currency: Literal["JPY"] = "JPY"
    raw_only: StrictBool = True
    accepted_conditions: list[str] | None = None
    price_basis: Literal["item_price", "delivered_price"] = "item_price"
    objective: str | None = Field(default=None, max_length=4000)


class IdentifyInput(Model):
    upload_id: str


class ResearchInput(Model):
    request: ResearchRequest


class DecisionInput(Model):
    goal: Goal
    research_run_ids: list[str] = Field(default_factory=list, max_length=10)


class IdentifyJob(Model):
    type: Literal["identify"]
    input: IdentifyInput


class ResearchJob(Model):
    type: Literal["research"]
    input: ResearchInput


class DecisionJob(Model):
    type: Literal["decision"]
    input: DecisionInput


JobRequest = Annotated[IdentifyJob | ResearchJob | DecisionJob, Field(discriminator="type")]


class Answer(Model):
    question_id: str
    value: object


class Answers(Model):
    answers: list[Answer] = Field(min_length=1, max_length=20)


class DraftRequest(Model):
    copy_id: str
    asking_price_jpy: Money
    decision_id: str | None = None


class IntentRequest(Model):
    action: Literal["buy", "sell"]
    execution_mode: Literal["fixture", "live"]
    decision_id: str | None = None
    listing_id: str | None = None
    draft_id: str | None = None
    max_total_jpy: Money | None = None


class Authorization(Model):
    payload_hash: str
    confirm: Literal[True]


class Empty(Model):
    pass


class ListingCondition(Model):
    condition_raw: str
    condition_normalized: Literal["near_mint", "unused", "played", "damaged", "unknown"]
    condition_basis: Literal["user_confirmed"]


class ListingFacts(Model):
    card: Card
    quantity: Annotated[int, Field(strict=True, ge=1, le=100)]
    purchase_fee_jpy: Money | None
    condition_record: ListingCondition | None = None
    registry_status: Literal["registry_match", "registry_mismatch", "lookup_unavailable", "not_checked"] = "not_checked"


class ListingConfirmation(Model):
    listing_id: str = Field(min_length=1, max_length=160)
    observed_at: str = Field(min_length=1, max_length=80)
    confirmed: Literal[True]
    facts: ListingFacts
