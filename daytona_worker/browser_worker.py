"""Loopback-only browser worker. One HTTP request uses the browser at a time."""

import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

from .browser_tools import BrowserTools


HOST = "127.0.0.1"
PORT = 8765
MAX_REQUEST_BYTES = 16384
ALLOWED = {
    "open_page": {"url": str},
    "smoke_test": {},
    "read_page": {},
    "click": {"role": str, "name": str},
    "fill": {"label": str, "value": str},
    "screenshot": {},
}


def validate_action(payload: object) -> tuple[str, dict]:
    if not isinstance(payload, dict) or set(payload) != {"tool", "arguments"}:
        raise ValueError("Expected tool and arguments")
    tool = payload["tool"]
    arguments = payload["arguments"]
    if not isinstance(tool, str) or tool not in ALLOWED:
        raise ValueError("Unsupported browser tool")
    schema = ALLOWED[tool]
    if not isinstance(arguments, dict) or set(arguments) != set(schema):
        raise ValueError("Invalid tool arguments")
    if any(not isinstance(arguments[key], expected) for key, expected in schema.items()):
        raise ValueError("Invalid argument type")
    return tool, arguments


class Handler(BaseHTTPRequestHandler):
    browser: BrowserTools

    def respond(self, status: int, data: dict) -> None:
        # ASCII JSON survives the Daytona process output transport for Japanese text.
        body = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.respond(200, {"ok": True})
        else:
            self.respond(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/action":
            self.respond(404, {"error": "Not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise ValueError("Request body size is invalid")
            tool, arguments = validate_action(json.loads(self.rfile.read(size)))
            result = getattr(self.browser, tool)(**arguments)
            logging.info("browser_tool=%s ok=true url=%s", tool, result.get("url"))
            self.respond(200, {"ok": True, "result": result})
        except (ValueError, json.JSONDecodeError) as exc:
            self.respond(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logging.exception("Browser action failed")
            try:
                screenshot = self.browser.screenshot()["path"]
            except Exception:
                screenshot = None
            self.respond(500, {"ok": False, "error": str(exc), "screenshot": screenshot})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    browser = BrowserTools()
    Handler.browser = browser
    server = HTTPServer((HOST, PORT), Handler)
    logging.info("Browser worker listening on %s:%s", HOST, PORT)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        browser.close()


if __name__ == "__main__":
    main()
