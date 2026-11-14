"""Thin backend bridge to the already running Daytona browser worker."""

from daytona import Daytona

from pokescout_agents.config import Settings, require_key
from setup_daytona import ID_FILE, send_action


class DaytonaBrowserClient:
    def __init__(self, sandbox_id: str | None = None, settings: Settings | None = None):
        settings = settings or Settings()
        require_key(settings.daytona_api_key, "DAYTONA_API_KEY")
        self.sandbox_id = sandbox_id or (ID_FILE.read_text(encoding="utf-8").strip() if ID_FILE.exists() else None)
        if not self.sandbox_id:
            raise ValueError("No Daytona sandbox ID. Run setup_daytona.py setup first.")
        self.daytona = Daytona()

    def action(self, tool: str, arguments: dict) -> dict:
        sandbox = self.daytona.get(self.sandbox_id)
        return send_action(sandbox, {"tool": tool, "arguments": arguments})
