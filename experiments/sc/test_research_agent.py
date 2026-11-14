"""Offline checks for research evidence boundaries and the AgentField endpoint."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import research_agent as research


class ResearchTests(unittest.TestCase):
    def test_rejects_fabricated_quote_and_other_card_number(self) -> None:
        sources = [{"id": "s1", "title": "Mewtwo V 273/S-P", "url": "https://example.com/card",
                    "content": "Price 3,368 JPY. In stock."},
                   {"id": "s2", "title": "Mewtwo V 030/071", "url": "https://example.com/other",
                    "content": "Price 200 JPY. In stock."}]
        findings = research.Findings(offers=[
            research.Offer(source_id="s1", price=100, currency="JPY", condition="unknown",
                           availability="unknown", evidence_quote="Price 100 JPY."),
            research.Offer(source_id="s2", price=200, currency="JPY", condition="unknown",
                           availability="unknown", evidence_quote="Price 200 JPY. In stock."),
        ], limitations=[])
        self.assertEqual(research.validate_offers(findings, sources, "273/S-P"), [])

    def test_stock_cannot_be_inferred_from_price(self) -> None:
        sources = [{"id": "s1", "title": "Mewtwo V 273/S-P", "url": "https://example.com/card",
                    "content": "3,368 JPY"}]
        findings = research.Findings(offers=[research.Offer(
            source_id="s1", price=3368, currency="JPY", condition="A-",
            availability="in_stock", evidence_quote="3,368 JPY",
        )], limitations=[])
        result = research.validate_offers(findings, sources, "273/S-P")
        self.assertEqual(result[0]["availability"], "unknown")
        self.assertEqual(result[0]["verification"], "search_snippet_only")

    def test_agentfield_http_research_pipeline(self) -> None:
        plan = research.SearchPlan(queries=["Mewtwo V 273/S-P price"])
        findings = research.Findings(offers=[research.Offer(
            source_id="s1", price=3368, currency="JPY", condition="A-",
            availability="in_stock", evidence_quote="3,368 JPY. In stock.",
        )], limitations=["Search snapshots only."])
        with (
            patch.object(research, "gemini", AsyncMock(side_effect=[plan, findings])) as model,
            patch.object(research, "search_web", AsyncMock(return_value={"results": [{
                "title": "Mewtwo V 273/S-P", "url": "https://example.com/card",
                "content": "3,368 JPY. In stock.",
            }]})),
            TestClient(research.app) as client,
        ):
            response = client.post("/reasoners/research", json={
                "input": {"card_name": "Mewtwo V", "card_number": "273/S-P", "currency": "JPY"},
            })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # AgentField may envelope HTTP results; inspect the result rather than duplicating routing.
        report = body.get("result", body)
        self.assertEqual(report["offers"][0]["price"], 3368)
        self.assertIsNone(report["cheapest_verified"])
        self.assertEqual(model.await_count, 2)
        with open(report["log_path"], encoding="utf-8") as stream:
            events = [json.loads(line)["event"] for line in stream]
        self.assertIn("complete", events)


if __name__ == "__main__":
    unittest.main()
