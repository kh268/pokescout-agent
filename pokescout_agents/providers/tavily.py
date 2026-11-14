"""Tavily Search and Extract client for external reference sources."""

from datetime import datetime, timezone
from hashlib import sha256

import httpx

from pokescout_agents.config import Settings, require_key
from pokescout_agents.providers.common import checked_json
from pokescout_agents.schemas import ResearchSource


class TavilyClient:
    BASE_URL = "https://api.tavily.com"

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.settings = settings or Settings()
        self.api_key = require_key(self.settings.tavily_api_key, "TAVILY_API_KEY")
        self.http = httpx.Client(timeout=30, transport=transport)
        self.last_request_id: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def search(self, query: str, max_results: int = 5) -> list[ResearchSource]:
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")
        response = self.http.post(
            f"{self.BASE_URL}/search",
            headers=self.headers,
            json={"query": query, "search_depth": "basic", "max_results": max_results,
                  "include_answer": False, "include_published_date": True},
        )
        body = checked_json(response, "Tavily")
        self.last_request_id = body.get("request_id")
        retrieved = datetime.now(timezone.utc)
        return [ResearchSource(
            source_id=f"tavily-{sha256(item['url'].encode()).hexdigest()[:12]}", url=item["url"], title=item.get("title"),
            content=item.get("content", ""), retrieved_at=retrieved,
            published_at=item.get("published_date"),
        ) for item in body.get("results", [])]

    def extract(self, urls: list[str]) -> list[ResearchSource]:
        if not 1 <= len(urls) <= 5:
            raise ValueError("extract accepts 1 to 5 URLs")
        response = self.http.post(
            f"{self.BASE_URL}/extract", headers=self.headers,
            json={"urls": urls, "extract_depth": "basic", "include_published_date": True},
        )
        body = checked_json(response, "Tavily")
        self.last_request_id = body.get("request_id")
        retrieved = datetime.now(timezone.utc)
        return [ResearchSource(
            source_id=f"extract-{sha256(item['url'].encode()).hexdigest()[:12]}", url=item["url"],
            content=item.get("raw_content", ""), retrieved_at=retrieved,
            published_at=item.get("published_date"),
        ) for item in body.get("results", [])]
