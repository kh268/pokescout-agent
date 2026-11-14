"""Shared HTTP handling with bounded requests and redacted errors."""

import httpx


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, status_code: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status_code = status_code


def checked_json(response: httpx.Response, provider: str) -> dict:
    if response.status_code >= 400:
        if response.status_code in (401, 403):
            message = "authentication or access denied"
        elif response.status_code in (402, 429):
            message = "credit or quota limit reached"
        elif response.status_code == 404:
            message = "endpoint or model unavailable"
        else:
            message = f"request failed with HTTP {response.status_code}"
        raise ProviderError(provider, message, response.status_code)
    try:
        result = response.json()
    except ValueError as exc:
        raise ProviderError(provider, "response was not JSON") from exc
    if not isinstance(result, dict):
        raise ProviderError(provider, "response was not a JSON object")
    return result
