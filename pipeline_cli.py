"""Local command-line entry point for the persisted PokéScout workflow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from agent_store import AgentError
from orchestrator import Pipeline


def main(argv=None):
    load_dotenv(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="Start or replay an idempotent workflow")
    start.add_argument("--input", required=True, type=Path)
    start.add_argument("--idempotency-key")
    sub.add_parser("demo", help="Run the synthetic research-to-decision workflow")
    status = sub.add_parser("status", help="Read a persisted job")
    status.add_argument("job_id")
    answer = sub.add_parser("answer", help="Answer every pending typed question")
    answer.add_argument("job_id")
    answer.add_argument("--answers", required=True, type=Path)
    answer.add_argument("--revision", required=True, type=int)
    resume = sub.add_parser("resume", help="Resume a queued job after restart")
    resume.add_argument("job_id")
    result = sub.add_parser("result", help="Read the final decision or sale draft")
    result.add_argument("job_id")
    args = parser.parse_args(argv)
    pipeline = Pipeline(args.data_dir)
    try:
        if args.command == "start":
            request = json.loads(args.input.read_text(encoding="utf-8"))
            value = pipeline.start(request, idempotency_key=args.idempotency_key)
        elif args.command == "demo":
            root = Path(__file__).resolve().parent / "examples" / "v2"
            goal = json.loads((root / "goal.json").read_text(encoding="utf-8"))
            goal.pop("source_mode", None)
            goal.update(message="Evaluate exact synthetic card offers", intent="buy",
                        allowed_conditions=["near_mint", "unused"])
            listings = json.loads((root / "listings.json").read_text(encoding="utf-8"))
            value = pipeline.start({"goal": goal, "fixture_listings": listings})
        elif args.command == "status":
            value = pipeline.get(args.job_id)
        elif args.command == "answer":
            answers = json.loads(args.answers.read_text(encoding="utf-8"))
            value = pipeline.answer(args.job_id, answers, revision=args.revision)
        elif args.command == "resume":
            value = pipeline.run(args.job_id)
        else:
            value = pipeline.result(args.job_id)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0 if not isinstance(value, dict) or value.get("state") != "failed" else 2
    except (AgentError, ValueError, OSError, json.JSONDecodeError) as exc:
        if isinstance(exc, AgentError):
            error = exc.as_dict()
        else:
            error = {"code": "INVALID_INPUT", "message": str(exc)}
        print(json.dumps({"error": error}, ensure_ascii=False))
        return 2
    finally:
        pipeline.close()


if __name__ == "__main__":
    raise SystemExit(main())
