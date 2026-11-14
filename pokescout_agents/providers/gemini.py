"""Gemini image identification client using the documented REST API."""

import base64
import json
import mimetypes
from pathlib import Path

import httpx

from pokescout_agents.config import Settings, require_key
from pokescout_agents.providers.common import ProviderError, checked_json
from pokescout_agents.schemas import CardIdentityCandidate


IDENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        key: {"type": ["string", "null"]}
        for key in ("name", "set_name", "set_code", "card_number", "language", "variant", "grading_company", "grade")
    } | {"unresolved_fields": {"type": "array", "items": {"type": "string"}}},
    "required": ["name", "set_name", "set_code", "card_number", "language", "variant", "grading_company", "grade", "unresolved_fields"],
}


class GeminiClient:
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None):
        self.settings = settings or Settings()
        self.api_key = require_key(self.settings.gemini_api_key, "GEMINI_API_KEY")
        self.http = httpx.Client(timeout=45, transport=transport)
        self.last_usage: dict | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key}

    def get_model(self) -> dict:
        response = self.http.get(f"{self.BASE_URL}/models/{self.settings.gemini_model}", headers=self.headers)
        return checked_json(response, "Gemini")

    def identify_card(self, image_path: str | Path) -> CardIdentityCandidate:
        path = Path(image_path)
        mime = mimetypes.guess_type(path.name)[0]
        if mime not in {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}:
            raise ValueError("Unsupported card image format")
        data = path.read_bytes()
        if not data or len(data) > 10_000_000:
            raise ValueError("Card image must be nonempty and at most 10 MB")
        payload = {
            "contents": [{"role": "user", "parts": [
                {"text": "Read this Pokémon card image. Extract only visible card identity and grading information. "
                         "Use null for unknown fields. Do not infer a grade for a raw card. "
                         "List unresolved fields explicitly."},
                {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}},
            ]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": IDENTITY_SCHEMA},
        }
        response = self.http.post(
            f"{self.BASE_URL}/models/{self.settings.gemini_model}:generateContent",
            headers=self.headers,
            json=payload,
        )
        body = checked_json(response, "Gemini")
        self.last_usage = body.get("usageMetadata")
        try:
            content = body["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("Gemini", "model did not return valid identity JSON") from exc
        return CardIdentityCandidate.model_validate(result)
