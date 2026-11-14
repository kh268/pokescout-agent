"""Send one JSON action to the browser worker from inside its sandbox."""

import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main() -> None:
    path = Path(sys.argv[1])
    try:
        request = Request(
            "http://127.0.0.1:8765/action",
            data=path.read_bytes(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=50) as response:
                print(response.read().decode("utf-8"))
        except HTTPError as exc:
            print(exc.read().decode("utf-8"))
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
