"""Stop only this project's browser worker in the Daytona sandbox."""

import os
import signal
from pathlib import Path


WORKER_DIR = Path(__file__).resolve().parent.parent


def main() -> None:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
            working_directory = (entry / "cwd").resolve()
        except (OSError, PermissionError):
            continue
        if (b"browser_worker.py" in arguments or b"daytona_worker.browser_worker" in arguments) and working_directory == WORKER_DIR:
            os.kill(int(entry.name), signal.SIGTERM)
            print(f"Stopped browser worker PID {entry.name}")


if __name__ == "__main__":
    main()
