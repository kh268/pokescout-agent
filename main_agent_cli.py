"""Local transport for the main agent; existing PokéScout commands remain available."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from contextlib import nullcontext
from pathlib import Path

from agent_inputs import object_fields
from agent_store import AgentError, new_id, utcnow
from agent_trading import FixtureExecutor
from main_agent import MainAgent
from research_decisions import observation_key


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def install_demo_evidence(agent, goal, listings):
    """Explicitly synthetic reports, exercising the same adapter as real reports."""
    runs = []
    for target in goal["targets"]:
        card = target["card"]
        # Include wrong-variant offers for the target to exercise rejections.
        source_rows = [r for r in listings if r["card"]["number"] == card["number"]]
        stamp, run_id = utcnow(), new_id("fixture")
        rows = []
        for source in source_rows:
            content = json.dumps(source, ensure_ascii=False, sort_keys=True)
            rows.append({"listing_id": source["id"], "url": source["source_url"], "title": source["card"]["name"],
                "price": source["price_jpy"], "shipping": source["shipping_jpy"], "currency": "JPY",
                "total_price": None if source["shipping_jpy"] is None else source["price_jpy"] + source["shipping_jpy"],
                "condition": source["condition"], "availability": source["availability"], "observed_at": stamp,
                "verification": "verified_listing", "evidence": [{"source_url": source["source_url"], "observed_at": stamp,
                    "kind": "direct_page", "content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}],
                "field_evidence": {}, "qualifies": source["availability"] == "available", "exclusion_reasons": []})
        report = {"schema_version": "1.0", "run_id": run_id, "status": "complete",
            "request": {"confirmed_target": {"name_ja": card["name"], "number": card["number"]}, "currency": "JPY", "raw_only": True},
            "started_at": stamp, "completed_at": stamp, "listings": rows, "comparable_offers": [r for r in rows if r["qualifies"]],
            "lowest_qualifying_observed_price": None, "findings": [], "uncertainty": ["Synthetic test data; no provider called."],
            "coverage": {"source_count": len(rows), "searches_used": 0, "inspections_used": 0}, "failures": [],
            "suggested_next_steps": [], "log_path": ""}
        agent.import_research(report, source_mode="fixture")
        for row, source in zip(rows, source_rows):
            agent.confirm_listing(run_id, row["listing_id"], row["observed_at"], {
                "card": source["card"], "quantity": source["quantity"], "purchase_fee_jpy": source["purchase_fee_jpy"]}, confirmed=True)
        runs.append(run_id)
    return runs


async def demo(data_dir):
    from fixture_market import TestMarket
    root = Path(__file__).resolve().parent / "examples" / "v2"
    goal, rows = load(root / "goal.json"), load(root / "listings.json")
    goal = {k: v for k, v in goal.items() if k != "source_mode"}
    goal.update(message="Acquire the two synthetic wishlist cards.", intent="buy", allowed_conditions=["near_mint"])
    with TestMarket(rows, Path(data_dir) / "fixture-market.sqlite3") as market:
        agent = MainAgent(data_dir, executor=FixtureExecutor(market.base))
        try:
            runs = install_demo_evidence(agent, goal, rows)
            owned = agent.store.collection.add(load(root / "owned-card.json"), cost=1200, allow_sale=True)
            job = agent.create_job("decision", {"goal": goal, "research_run_ids": runs}, key="demo-decision")
            job = await agent.run_job(job["job_id"])
            if job["state"] != "succeeded":
                raise AgentError("DEMO_FAILED", "The decision job failed; inspect its durable state.")
            decision = agent.store.read("decisions", job["result"]["id"])
            draft_job = agent.create_sale_draft(owned["id"], 1800, key="demo-draft")
            draft_job = await agent.run_job(draft_job["job_id"])
            # This CLI subcommand explicitly requests a synthetic test transaction.
            candidate = decision["purchase_candidates"][0]
            intent = agent.trading.prepare(decision["decision_id"], candidate["listing_ref"]["observation_id"],
                                           candidate["acquisition_total_jpy"], key="demo-prepare")
            intent = agent.trading.authorize(intent["intent_id"], intent["payload_hash"], revision=intent["revision"], key="demo-authorize")
            execution = agent.create_action_job(intent["intent_id"], revision=intent["revision"], key="demo-execute")
            execution = await agent.run_job(execution["job_id"])
            outcome = agent.store.get_intent(intent["intent_id"])
            report = {"mode": "fixture", "identification": "synthetic JSON; image provider not called",
                "decision_job": job, "decision": decision, "sale_draft_job": draft_job,
                "sale_draft": agent.store.read("drafts", draft_job["result"]["id"]), "execution_job": execution,
                "intent": outcome, "live_marketplace_transactions": 0,
                "providers": {p: "not called" for p in ("Gemini", "Tavily", "Nosana", "Daytona", "CodeAF")}}
            agent.store.save_bytes("demo-report.json", (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            if outcome["state"] != "confirmed":
                raise AgentError("DEMO_FAILED", "The fixture action was not independently confirmed; inspect the retained intent.")
            return {"mode": "fixture", "selected_listing_ids": [c["listing_ref"]["listing_id"] for c in decision["purchase_candidates"]],
                "total_jpy": decision["calculations"]["purchase_total_jpy"], "fixture_receipt": outcome["outcome"]["receipt_ref"],
                "ownership_state": outcome["outcome"]["ownership_state"], "live_marketplace_transactions": 0,
                "report": str(Path(data_dir).resolve() / "demo-report.json")}
        finally:
            agent.close()


def parser():
    cli = argparse.ArgumentParser(description="Durable local main-agent workflows; no live trading.")
    cli.add_argument("--data-dir", type=Path, help="Shared application directory; defaults to repository/data")
    cli.add_argument("--fixture-listings", type=Path, help="Explicit synthetic offers for the local TestMarket adapter")
    commands = cli.add_subparsers(dest="command", required=True)
    for command in ("capabilities", "jobs", "work", "recover", "demo"):
        commands.add_parser(command)
    upload = commands.add_parser("upload"); upload.add_argument("images", nargs="+"); upload.add_argument("--roles", nargs="+"); upload.add_argument("--key", required=True)
    submit = commands.add_parser("submit"); submit.add_argument("--request", type=Path, required=True); submit.add_argument("--key", required=True); submit.add_argument("--run", action="store_true")
    show = commands.add_parser("show"); show.add_argument("kind", choices=["job", "candidate", "decision", "draft", "research", "intent"]); show.add_argument("id")
    answers = commands.add_parser("answers"); answers.add_argument("job_id"); answers.add_argument("--file", type=Path, required=True); answers.add_argument("--revision", type=int, required=True); answers.add_argument("--key", required=True)
    cancel = commands.add_parser("cancel"); cancel.add_argument("job_id"); cancel.add_argument("--revision", type=int, required=True); cancel.add_argument("--key", required=True)
    confirm = commands.add_parser("confirm-copy"); confirm.add_argument("--file", type=Path, required=True); confirm.add_argument("--key", required=True)
    research = commands.add_parser("import-research"); research.add_argument("file", type=Path); research.add_argument("--mode", choices=["replay", "fixture"], default="replay")
    facts = commands.add_parser("confirm-listing"); facts.add_argument("--file", type=Path, required=True)
    sale = commands.add_parser("draft"); sale.add_argument("copy_id"); sale.add_argument("--price-jpy", type=int, required=True); sale.add_argument("--key", required=True)
    prepare = commands.add_parser("prepare"); prepare.add_argument("decision_id"); prepare.add_argument("observation_id"); prepare.add_argument("--max-total-jpy", type=int, required=True); prepare.add_argument("--key", required=True)
    auth = commands.add_parser("authorize"); auth.add_argument("intent_id"); auth.add_argument("--payload-hash", required=True); auth.add_argument("--revision", type=int, required=True); auth.add_argument("--key", required=True)
    for command in ("execute", "reconcile"):
        action = commands.add_parser(command); action.add_argument("intent_id"); action.add_argument("--revision", type=int, required=True); action.add_argument("--key", required=True)
    return cli


async def dispatch(agent, args):
    command = args.command
    if command == "capabilities": return agent.capabilities()
    if command == "jobs": return agent.list_jobs()
    if command == "work": return await agent.work()
    if command == "recover": return agent.recover_interrupted()
    if command == "upload": return agent.save_upload(args.images, roles=args.roles, key=args.key)
    if command == "submit":
        request = load(args.request)
        object_fields(request, {"type", "input"}, ("type", "input"))
        job = agent.create_job(request["type"], request["input"], key=args.key)
        return await agent.run_job(job["job_id"]) if args.run else job
    if command == "show":
        if args.kind == "job": return agent.get_job(args.id)
        if args.kind == "intent": return agent.store.get_intent(args.id)
        if args.kind == "research": return agent.research_report(args.id)
        return agent.store.read({"candidate": "candidates", "decision": "decisions", "draft": "drafts"}[args.kind], args.id)
    if command == "answers": return agent.answers(args.job_id, load(args.file)["answers"], revision=args.revision, key=args.key)
    if command == "cancel": return agent.cancel(args.job_id, revision=args.revision, key=args.key)
    if command == "confirm-copy": return agent.confirm_copy(key=args.key, **load(args.file))
    if command == "import-research": return {"run_id": agent.import_research(load(args.file), source_mode=args.mode)}
    if command == "confirm-listing": return agent.confirm_listing(**load(args.file))
    if command == "draft": return agent.create_sale_draft(args.copy_id, args.price_jpy, key=args.key)
    if command == "prepare": return agent.trading.prepare(args.decision_id, args.observation_id, args.max_total_jpy, key=args.key)
    if command == "authorize": return agent.trading.authorize(args.intent_id, args.payload_hash, revision=args.revision, key=args.key)
    if command in {"execute", "reconcile"}:
        return agent.create_action_job(args.intent_id, revision=args.revision, key=args.key, reconcile=command == "reconcile")
    raise AgentError("INVALID_INPUT", "Unsupported command.")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            if args.data_dir is None or (args.data_dir.exists() and any(args.data_dir.iterdir())):
                raise AgentError("INVALID_INPUT", "Demo requires --data-dir pointing to a new or empty directory.")
            args.data_dir.mkdir(parents=True, exist_ok=True)
            result = asyncio.run(demo(args.data_dir))
        else:
            from fixture_market import TestMarket
            data_dir = args.data_dir or Path(__file__).resolve().parent / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            market_context = TestMarket(load(args.fixture_listings), data_dir / "fixture-market.sqlite3") if args.fixture_listings else nullcontext()
            with market_context as market:
                agent = MainAgent(data_dir, executor=FixtureExecutor(market.base) if market else None)
                try:
                    result = asyncio.run(dispatch(agent, args))
                finally:
                    agent.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        jobs = result if isinstance(result, list) else [result]
        return 2 if any(isinstance(j, dict) and j.get("state") == "failed" for j in jobs) else 0
    except (AgentError, ValueError, OSError, TypeError) as error:
        safe = error if isinstance(error, AgentError) else AgentError("INVALID_INPUT", "Check input fields, file paths and local runtime permissions.")
        print(json.dumps({"error": safe.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
