"""Deterministic listing eligibility and a simple disclosed ranking score."""

from pokescout_agents.schemas import Evaluation, ListingObservation, TargetCard


CONDITION_RANK = {"played": 1, "lightly_played": 2, "near_mint": 3}


def normalized(value: str | None) -> str | None:
    return " ".join(value.casefold().split()) if value is not None else None


def condition_rank(claim: str | None) -> int | None:
    if claim is None:
        return None
    token = normalized(claim)
    aliases = {"nm": 3, "near mint": 3, "near_mint": 3,
               "lp": 2, "lightly played": 2, "lightly_played": 2,
               "played": 1, "pl": 1}
    return aliases.get(token)


def evaluate_listing(
    target: TargetCard,
    listing: ListingObservation,
    *,
    remaining_budget_jpy: int,
    remaining_quantity: int = 1,
    existing_listing_ids: set[str] | None = None,
) -> Evaluation:
    facts = listing.extracted
    reasons: list[str] = []
    if normalized(facts.card_name) != normalized(target.name) or normalized(facts.set_code) != normalized(target.set_code) or normalized(facts.card_number) != normalized(target.card_number):
        reasons.append("WRONG_PRINTING")
    if normalized(facts.language) != normalized(target.language):
        reasons.append("WRONG_LANGUAGE")
    if normalized(facts.variant) != normalized(target.variant):
        reasons.append("WRONG_VARIANT")
    if normalized(facts.grading_company) != normalized(target.grading_company) or normalized(facts.grade) != normalized(target.grade):
        reasons.append("GRADE_OUTSIDE_REQUIREMENT")
    if facts.available is not True:
        reasons.append("NOT_AVAILABLE")
    if remaining_quantity < 1:
        reasons.append("QUANTITY_LIMIT")
    if listing.listing_id in (existing_listing_ids or set()):
        reasons.append("DUPLICATE_OR_ALREADY_RESERVED")

    required_rank = CONDITION_RANK.get(target.minimum_condition, 0)
    stated_rank = condition_rank(facts.seller_condition)
    if required_rank and stated_rank is None:
        reasons.append("CONDITION_UNKNOWN")
    elif required_rank and stated_rank < required_rank:
        reasons.append("CONDITION_BELOW_REQUIREMENT")
    if any(normalized(defect) not in {normalized(item) for item in target.allowed_defects} for defect in facts.defects):
        reasons.append("CONDITION_BELOW_REQUIREMENT")

    total = None
    if facts.price_jpy is None or facts.shipping_jpy is None:
        reasons.append("ACQUISITION_TOTAL_UNKNOWN")
    else:
        total = facts.price_jpy + facts.shipping_jpy
        if total > target.max_total_jpy or total > remaining_budget_jpy:
            reasons.append("OVER_BUDGET")

    reasons = list(dict.fromkeys(reasons))
    score = round(1 - total / target.max_total_jpy, 4) if not reasons and total is not None else None
    return Evaluation(eligible=not reasons, reasons=reasons, total_jpy=total, score=score)
