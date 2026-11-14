"""Contract, evidence, ranking, scope and partial-failure checks without API credits."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

import market_research as market
from research import ResearchReport


def source() -> dict:
    return {"id": "s1", "url": "https://jp.mercari.com/item/m12345678901",
            "title": "ミュウツーV 273/S-P", "evidence": [market.evidence(
                "https://jp.mercari.com/item/m12345678901", "indexed_snippet",
                "ミュウツーV 273/S-P 価格 3,000円 購入手続きへ 新品、未使用 送料込み",
            )]}


def draft(**updates: object) -> market.DraftListing:
    values = {"source_id": "s1", "title": "ミュウツーV 273/S-P", "price": 3000,
              "currency": "JPY", "shipping": 0, "condition": "新品、未使用", "availability": "available",
              "field_evidence": {"title": "ミュウツーV 273/S-P", "price": "3,000円",
                                 "availability": "購入手続きへ", "condition": "新品、未使用", "shipping": "送料込み"}}
    return market.DraftListing.model_validate({**values, **updates})


class MarketTests(unittest.TestCase):
    def test_scope_and_card_number_boundaries(self) -> None:
        for url in ("https://jp.mercari.com.evil.test/item/m123", "http://jp.mercari.com/item/m123",
                    "https://jp.mercari.com/search", "https://user@jp.mercari.com/item/m123"):
            with self.assertRaises(ValueError):
                market.canonical_url(url, list(market.ALLOWED))
        self.assertFalse(market.contains_number("273/S-P", "1273/S-P"))
        self.assertFalse(market.contains_number("273/S-P", "273/S-P2"))
        self.assertTrue(market.contains_number("273/S-P", "ミュウツーV〈273/S-P〉"))

    def test_fabricated_price_and_stock_stay_unknown(self) -> None:
        row = market.observation(source(), draft(price=100, field_evidence={"price": "100円", "availability": "在庫あり"}))
        self.assertIsNone(row["price"])
        self.assertEqual(row["availability"], "unknown")
        self.assertEqual(row["verification"], "indexed_snippet")

    def test_python_ranking_and_shipping_requirement(self) -> None:
        one = market.observation(source(), draft())
        two = {**one, "listing_id": "s2", "price": 2800, "shipping": None, "total_price": None,
               "url": "https://jp.mercari.com/item/m99999999999"}
        request = market.Request(confirmed_target={"name_ja": "ミュウツーV", "number": "273/S-P"})
        offers, lowest = market.compare([one, two], request)
        self.assertEqual(lowest["price"], 2800)
        request.price_basis = "delivered_price"
        offers, lowest = market.compare([one, two], request)
        self.assertEqual(lowest["price"], 3000)
        self.assertEqual(len(offers), 1)
        two["availability"] = "sold"
        request.price_basis = "item_price"
        self.assertEqual(market.compare([one, two], request)[1]["price"], 3000)

    def test_agentfield_endpoint_contract_and_budget(self) -> None:
        hit = source()
        plans = [market.Plan(queries=["ミュウツーV 273/S-P"], reason="initial"),
                 market.Plan(queries=["ミュウツーV 273/S-P 新品"], inspect_urls=[hit["url"]], reason="refine")]
        synthesis = market.Synthesis(listings=[draft()], findings=[], uncertainty=[], suggested_next_steps=[])
        request = {"confirmed_target": {"name_ja": "ミュウツーV", "number": "273/S-P"},
                   "source_limits": {"max_searches": 1, "max_inspections": 0}}
        with (patch.object(market.runtime, "gemini", AsyncMock(side_effect=plans + [synthesis])),
              patch.object(market, "tavily", AsyncMock(return_value={"results": [{
                  "url": hit["url"], "title": hit["title"], "content": hit["evidence"][0]["content"]}]})) as search,
              TestClient(market.app) as client):
            response = client.post("/reasoners/research_market", json={"request": request})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        report = body.get("result", body)
        TypeAdapter(ResearchReport).validate_python(report)
        self.assertEqual(search.await_count, 1)
        self.assertEqual(report["coverage"]["inspections_used"], 0)
        self.assertEqual(report["lowest_qualifying_observed_price"]["verification"], "indexed_snippet")

    def test_failed_synthesis_preserves_direct_observations(self) -> None:
        hit = source()
        primary = {"title": hit["title"], "price": 3000, "currency": "JPY", "shipping_jpy": 0,
                   "condition": "新品、未使用", "availability": "available", "observed_at": market.now(), "evidence": {}}
        async def inspect(item: dict) -> dict:
            item["evidence"].append(market.evidence(item["url"], "direct_page", "primary listing"))
            return primary
        with (patch.object(market.runtime, "gemini", AsyncMock(side_effect=[
                market.Plan(queries=["ミュウツーV 273/S-P"], reason="initial"),
                market.Plan(queries=[], inspect_urls=[hit["url"]], reason="enough"), RuntimeError("quota exhausted")])),
              patch.object(market, "tavily", AsyncMock(return_value={"results": [{
                  "url": hit["url"], "title": hit["title"], "content": "3,000円"}]})),
              patch.object(market, "inspect_source", inspect)):
            report = asyncio.run(market.research_market({"confirmed_target": {"name_ja": "ミュウツーV", "number": "273/S-P"}}))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["lowest_qualifying_observed_price"]["price"], 3000)
        self.assertTrue(report["failures"])
        self.assertEqual(report["listings"][0]["verification"], "verified_listing")


if __name__ == "__main__":
    unittest.main()
