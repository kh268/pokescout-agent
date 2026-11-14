"""Check provider configuration without sending card data or spending credits by default."""

import argparse
import json
import struct
import tempfile
import zlib
from pathlib import Path

from pokescout_agents.config import Settings
from pokescout_agents.providers.gemini import GeminiClient
from pokescout_agents.providers.nosana import NosanaClient


def blank_png() -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))

    width = height = 32
    pixels = b"".join(b"\0" + b"\xff\xff\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-nosana", action="store_true", help="Send one small Qwen chat request using Nosana credits")
    parser.add_argument("--smoke-gemini-image", action="store_true", help="Send one blank image to Gemini to check structured extraction")
    args = parser.parse_args()
    settings = Settings()
    results: dict[str, object] = {}
    try:
        nosana = NosanaClient(settings)
        nosana.require_model()
        results["nosana"] = {"status": "available", "model": settings.nosana_model}
        if args.smoke_nosana:
            reply = nosana.complete_json("Return only a JSON object with ok set to true.", "Reply now.", max_tokens=80)
            results["nosana_chat"] = {"status": "passed" if reply.get("ok") is True else "unexpected_response", "usage": nosana.last_usage}
    except Exception as exc:
        results["nosana"] = {"status": "error", "error": str(exc)}
    try:
        gemini = GeminiClient(settings)
        model = gemini.get_model()
        results["gemini"] = {"status": "available", "model": model.get("name")}
        if args.smoke_gemini_image:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "blank.png"
                path.write_bytes(blank_png())
                identity = gemini.identify_card(path)
            results["gemini_image"] = {"status": "passed", "unresolved_fields": identity.unresolved_fields,
                                       "usage": gemini.last_usage}
    except Exception as exc:
        results["gemini"] = {"status": "error", "error": str(exc)}
    results["tavily"] = {"status": "configured_not_tested" if settings.tavily_api_key else "missing_key"}
    print(json.dumps(results, indent=2))
    required = all(results[key]["status"] == "available" for key in ("nosana", "gemini"))
    optional = all(results[key]["status"] == "passed" for key in ("nosana_chat", "gemini_image") if key in results)
    return 0 if required and optional else 1


if __name__ == "__main__":
    raise SystemExit(main())
