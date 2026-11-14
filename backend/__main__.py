"""Explicit local launcher; no credentials are loaded implicitly on import."""
import argparse
import json
import os
from pathlib import Path

import uvicorn

from .app import create_app
from .worker import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--research-url")
    parser.add_argument("--fixtures", type=Path, help="Explicitly enable synthetic fixture decisions/purchases.")
    parser.add_argument("--allow-origin", action="append", default=[])
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        if not args.env_file.is_file():
            parser.error("The environment file does not exist.")
        load_dotenv(args.env_file, override=False)
    if not os.getenv("GEMINI_API_KEY") and os.getenv("GOOGLE_API_KEY"):
        os.environ["GEMINI_API_KEY"] = os.environ["GOOGLE_API_KEY"]
    if args.research_url:
        os.environ["RESEARCH_AGENT_URL"] = args.research_url
    os.environ.setdefault("ACTIVITY_PUBLISH_URL", f"http://127.0.0.1:{args.port}/internal/v1/activity/events")
    settings = Settings(data_dir=args.data_dir, identify_enabled=bool(os.getenv("GEMINI_API_KEY")),
                        research_enabled=bool(os.getenv("RESEARCH_AGENT_URL")),
                        fixture_listings=json.loads(args.fixtures.read_text(encoding="utf-8")) if args.fixtures else [],
                        allowed_origins=args.allow_origin)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
