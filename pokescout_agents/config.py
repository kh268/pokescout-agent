"""Environment-backed configuration. Credentials stay on the local machine."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL") or "gemini-3.8-flash")
    nosana_api_key: str = field(default_factory=lambda: os.getenv("NOSANA_API_KEY", ""))
    nosana_model: str = field(default_factory=lambda: os.getenv("NOSANA_MODEL", "qwen/qwen3.8-27b"))
    nosana_base_url: str = field(default_factory=lambda: os.getenv("NOSANA_BASE_URL", "https://inference.nosana.com/v1"))
    tavily_api_key: str = field(default_factory=lambda: os.getenv("TAVILY_API_KEY", ""))
    daytona_api_key: str = field(default_factory=lambda: os.getenv("DAYTONA_API_KEY", ""))


def require_key(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} is missing. Set it in .env or the environment.")
    return value
