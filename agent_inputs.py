"""Strict goal inputs and server-generated, typed clarification questions."""
from __future__ import annotations

from cards import CARD_SCHEMA, identity_key, money, validate_card
from agent_store import AgentError, new_id

CONDITIONS = {"near_mint", "unused", "played", "damaged"}
GOAL_FIELDS = {"message", "intent", "targets", "copy_ids", "budget_jpy", "reserve_jpy",
               "allowed_conditions", "avoid_owned", "allow_defects"}


def object_fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise AgentError("INVALID_INPUT", "Missing or unexpected input fields.")


def card_input(value, confirmed=False):
    object_fields(value, CARD_SCHEMA["properties"])
    return validate_card(value, confirmed=confirmed)


def validate_goal(value):
    object_fields(value, GOAL_FIELDS, ("message", "intent"))
    goal = dict(value)
    if not isinstance(goal["message"], str) or not 1 <= len(goal["message"].strip()) <= 4000:
        raise AgentError("INVALID_INPUT", "Goal message must contain 1–4000 characters.")
    if goal["intent"] not in ("buy", "sell", "assess"):
        raise AgentError("INVALID_INPUT", "Choose buy, sell or assess explicitly.")
    goal.setdefault("targets", [])
    goal.setdefault("copy_ids", [])
    goal.setdefault("budget_jpy", None)
    goal.setdefault("reserve_jpy", 0)
    goal.setdefault("allowed_conditions", None)
    goal.setdefault("avoid_owned", True)
    goal.setdefault("allow_defects", False)
    for key in ("avoid_owned", "allow_defects"):
        if type(goal[key]) is not bool:
            raise AgentError("INVALID_INPUT", key + " must be boolean.")
    for key in ("budget_jpy", "reserve_jpy"):
        if key != "budget_jpy" or goal[key] is not None:
            money(goal[key], key)
    conditions = goal["allowed_conditions"]
    if conditions is not None and (not isinstance(conditions, list) or not conditions
                                   or any(not isinstance(c, str) or c not in CONDITIONS for c in conditions)):
        raise AgentError("INVALID_INPUT", "Choose an explicit nonempty allowed_conditions list.")
    if not isinstance(goal["targets"], list) or len(goal["targets"]) > 10:
        raise AgentError("INVALID_INPUT", "At most ten target cards are supported.")
    targets, seen = [], set()
    for target in goal["targets"]:
        object_fields(target, {"card", "quantity"}, ("card",))
        quantity = target.get("quantity", 1)
        if type(quantity) is not int or not 1 <= quantity <= 10:
            raise AgentError("INVALID_INPUT", "Target quantity must be 1–10.")
        card = card_input(target["card"])
        if all(card[k] for k in ("name", "set", "number", "language", "variant")):
            key = identity_key(card)
            if key in seen:
                raise AgentError("INVALID_INPUT", "Duplicate target printings must be combined into one quantity.")
            seen.add(key)
        targets.append({"card": card, "quantity": quantity})
    goal["targets"] = targets
    ids = goal["copy_ids"]
    if (not isinstance(ids, list) or len(ids) > 10
            or any(not isinstance(i, str) or not 1 <= len(i) <= 120 for i in ids) or len(set(ids)) != len(ids)):
        raise AgentError("INVALID_INPUT", "Supply at most ten distinct owned-copy IDs.")
    return goal


def question(field, prompt, value_type, choices=None):
    return {"question_id": new_id("question"), "prompt": prompt, "field": field,
            "value_type": value_type, "choices": choices, "required": True}


def goal_questions(goal, copies):
    questions = []
    if goal["intent"] in ("buy", "assess"):
        if not goal["targets"]:
            questions.append(question("targets.0.card", "Confirm the exact target printing and raw/slabbed cohort.", "card"))
        for index, target in enumerate(goal["targets"]):
            try:
                validate_card(target["card"], confirmed=True)
            except ValueError:
                questions.append(question(f"targets.{index}.card", "Resolve this card's missing identity or grading fields and uncertainties.", "card"))
        if goal["budget_jpy"] is None:
            questions.append(question("budget_jpy", "What is your JPY spending allowance? This is not a verified cash balance.", "integer"))
        if goal["allowed_conditions"] is None:
            questions.append(question("allowed_conditions", "Which raw-card conditions are acceptable?", "condition_list", sorted(CONDITIONS)))
    if goal["intent"] == "sell":
        by_id = {c["id"]: c for c in copies}
        if not goal["copy_ids"] or any(i not in by_id or not by_id[i]["allow_sale"] or by_id[i]["state"] != "owned" for i in goal["copy_ids"]):
            questions.append(question("copy_ids", "Select one owned copy already permitted for sale. Protected copies cannot be offered.", "string"))
    return questions


def apply_answers(goal, questions, answers):
    if not isinstance(answers, list):
        raise AgentError("INVALID_INPUT", "Answers must be an array.")
    by_id, supplied = {q["question_id"]: q for q in questions}, {}
    for answer in answers:
        object_fields(answer, {"question_id", "value"}, ("question_id", "value"))
        key = answer["question_id"]
        if not isinstance(key, str) or key not in by_id or key in supplied:
            raise AgentError("INVALID_STATE", "Unknown, stale or duplicate question ID.")
        supplied[key] = answer["value"]
    if set(supplied) != set(by_id):
        raise AgentError("INVALID_INPUT", "Answer every pending required question.")
    for key, q in by_id.items():
        value, field = supplied[key], q["field"]
        if q["value_type"] == "card":
            value = card_input(value, confirmed=True)
            index = int(field.split(".")[1])
            if index == len(goal["targets"]):
                goal["targets"].append({"card": value, "quantity": 1})
            else:
                goal["targets"][index]["card"] = value
        elif field == "budget_jpy":
            goal[field] = money(value, field)
        elif field == "allowed_conditions":
            goal[field] = value
        elif field == "copy_ids":
            if not isinstance(value, str):
                raise AgentError("INVALID_INPUT", "A copy ID must be a string.")
            goal[field] = [value]
        else:
            raise AgentError("INVALID_INPUT", "Unsupported clarification field.")
    return validate_goal(goal)
