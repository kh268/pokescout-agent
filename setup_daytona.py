"""Create or reuse a Daytona desktop and operate its visible browser worker."""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from daytona import Daytona, SandboxState
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
ID_FILE = ROOT / ".daytona_sandbox_id"
REMOTE_DIR = "/home/daytona/card-worker"
WORKER_FILES = (
    "daytona_worker/__init__.py",
    "daytona_worker/browser_tools.py",
    "daytona_worker/browser_worker.py",
    "daytona_worker/browser_request.py",
    "daytona_worker/stop_browser_worker.py",
)


def checked_exec(sandbox, command: str, *, timeout: int = 300, cwd: str | None = None):
    response = sandbox.process.exec(command, cwd=cwd, timeout=timeout)
    if response.exit_code != 0:
        raise RuntimeError(f"Sandbox command failed ({response.exit_code}): {command}\n{response.result}")
    return response.result


def sandbox_id(args) -> str | None:
    if args.sandbox_id:
        return args.sandbox_id
    if ID_FILE.exists():
        return ID_FILE.read_text(encoding="utf-8").strip() or None
    return None


def wait_for_desktop(sandbox, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    while time.monotonic() < deadline:
        last_status = str(sandbox.computer_use.get_status().status)
        if last_status.lower() == "active":
            return
        time.sleep(2)
    raise TimeoutError(f"Desktop did not become active within {timeout}s (status: {last_status})")


def wait_for_worker(sandbox, timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = sandbox.process.exec(
            "python3 -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).read().decode())\"",
            timeout=5,
        )
        if result.exit_code == 0 and '"ok": true' in result.result.lower():
            return
        time.sleep(2)
    logs = sandbox.process.exec(f"tail -80 {REMOTE_DIR}/worker.log", timeout=10).result
    raise TimeoutError(f"Browser worker did not become ready within {timeout}s.\n{logs}")


def wait_for_worker_stop(sandbox, timeout: int = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = sandbox.process.exec(
            "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)\"",
            timeout=5,
        )
        if result.exit_code != 0:
            return
        time.sleep(1)
    raise TimeoutError("Previous browser worker did not stop")


def send_action(sandbox, payload: dict) -> dict:
    payload_path = f"/tmp/card-action-{uuid.uuid4().hex}.json"
    sandbox.fs.upload_file(json.dumps(payload).encode("utf-8"), payload_path)
    raw = checked_exec(sandbox, f"python3 -m daytona_worker.browser_request {payload_path}", timeout=60, cwd=REMOTE_DIR)
    response = json.loads(raw)
    if not response.get("ok"):
        raise RuntimeError(f"Browser action failed: {response}")
    return response["result"]


def setup(daytona: Daytona, args) -> None:
    existing_id = sandbox_id(args)
    sandbox = daytona.get(existing_id) if existing_id else daytona.create()
    if existing_id and sandbox.state != SandboxState.STARTED:
        daytona.start(sandbox)
    ID_FILE.write_text(sandbox.id + "\n", encoding="utf-8")
    print(f"Sandbox ID: {sandbox.id}", flush=True)

    sandbox.computer_use.start()
    wait_for_desktop(sandbox)
    print("Daytona desktop is active", flush=True)

    checked_exec(sandbox, f"mkdir -p {REMOTE_DIR}/daytona_worker", timeout=30)
    for filename in WORKER_FILES:
        sandbox.fs.upload_file(str(ROOT / filename), f"{REMOTE_DIR}/{filename}")

    checked_exec(sandbox, "python3 -m daytona_worker.stop_browser_worker", timeout=15, cwd=REMOTE_DIR)
    wait_for_worker_stop(sandbox)
    checked_exec(sandbox, "python3 -m pip install playwright", timeout=300)
    checked_exec(sandbox, "python3 -m playwright install --with-deps chromium", timeout=600)
    display = checked_exec(sandbox, "ls /tmp/.X11-unix/X*", timeout=10).strip().splitlines()[0]
    display_number = display.rsplit("X", 1)[-1]
    checked_exec(
        sandbox,
        f"DISPLAY=:{display_number} nohup python3 -u -m daytona_worker.browser_worker > worker.log 2>&1 < /dev/null &",
        timeout=30,
        cwd=REMOTE_DIR,
    )
    wait_for_worker(sandbox)

    local_result = send_action(sandbox, {"tool": "smoke_test", "arguments": {}})
    if "Browser Ready" not in local_result["text"]:
        raise RuntimeError(f"Visible browser self-test failed: {local_result}")
    print(json.dumps({"browser_self_test": "passed", "title": local_result["title"]}), flush=True)

    try:
        result = send_action(sandbox, {"tool": "open_page", "arguments": {"url": args.check_url}})
    except RuntimeError as exc:
        print(
            f"External page check failed for {args.check_url}: {exc}\n"
            "The local browser self-test passed. Check Daytona outbound network limits "
            "and whether this domain is allowed for your organization tier: "
            "https://www.daytona.io/docs/en/network-limits/",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(json.dumps({"external_page_check": "passed", "url": result["url"], "title": result["title"]}))
    print("Open this sandbox in the Daytona dashboard to view its Terminal and VNC/Desktop.")
    print("The browser worker remains running until you stop or delete the sandbox.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "status", "action", "stop", "delete"), nargs="?", default="setup")
    parser.add_argument("--sandbox-id", help="Use an existing Daytona sandbox ID")
    parser.add_argument("--payload-file", type=Path, help="JSON action file for the action command")
    parser.add_argument("--check-url", default="https://example.com", help="External page to check during setup")
    args = parser.parse_args()
    if not os.environ.get("DAYTONA_API_KEY"):
        parser.error("DAYTONA_API_KEY is required in the environment")
    if args.command == "action" and args.payload_file is None:
        parser.error("action requires --payload-file")

    daytona = Daytona()
    try:
        if args.command == "setup":
            setup(daytona, args)
        else:
            existing_id = sandbox_id(args)
            if not existing_id:
                raise ValueError("No sandbox ID found. Run setup or pass --sandbox-id.")
            sandbox = daytona.get(existing_id)
            if args.command == "status":
                print(json.dumps({"sandbox_id": sandbox.id, "desktop": str(sandbox.computer_use.get_status().status)}))
            elif args.command == "action":
                payload = json.loads(args.payload_file.read_text(encoding="utf-8"))
                print(json.dumps(send_action(sandbox, payload), ensure_ascii=False, indent=2))
            elif args.command == "stop":
                daytona.stop(sandbox)
                print(f"Stopped sandbox {sandbox.id}")
            elif args.command == "delete":
                daytona.delete(sandbox, wait=True)
                if ID_FILE.exists() and ID_FILE.read_text(encoding="utf-8").strip() == sandbox.id:
                    ID_FILE.unlink()
                print(f"Deleted sandbox {sandbox.id}")
    except Exception as exc:
        print(f"Daytona setup error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
