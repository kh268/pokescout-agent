"""Tests for model boundaries and deterministic purchase rules."""

import json
import unittest

import httpx

from pokescout_agents.config import Settings
from pokescout_agents.decision import evaluate_listing
from pokescout_agents.providers.nosana import NosanaClient
from pokescout_agents.providers.tavily import TavilyClient
from pokescout_agents.schemas import ListingEvidence, ListingObservation, SaleFacts, TargetCard


def listing(**changes) -> ListingObservation:
    data = {
        "card_name": "Pikachu", "set_code": "SVP", "card_number": "001",
        "language": "Japanese", "variant": None, "grading_company": None, "grade": None,
        "price_jpy": 4000, "shipping_jpy": 300, "seller_condition": "near mint",
        "defects": [], "available": True,
    }
    data.update(changes)
    return ListingObservation(
        marketplace="fixture", listing_id="listing-1", url="https://example.com/item/1",
        title="Pikachu", description="Synthetic listing", extracted=ListingEvidence(**data),
    )


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.target = TargetCard(name="Pikachu", set_code="SVP", card_number="001", max_total_jpy=5000)

    def test_eligible_total_and_score(self):
        decision = evaluate_listing(self.target, listing(), remaining_budget_jpy=5000)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.total_jpy, 4300)
        self.assertIsNotNone(decision.score)

    def test_unknown_shipping_blocks_purchase(self):
        decision = evaluate_listing(self.target, listing(shipping_jpy=None), remaining_budget_jpy=5000)
        self.assertFalse(decision.eligible)
        self.assertIn("ACQUISITION_TOTAL_UNKNOWN", decision.reasons)

    def test_wrong_card_and_duplicate_both_reported(self):
        decision = evaluate_listing(self.target, listing(set_code="SVA"), remaining_budget_jpy=5000,
                                    existing_listing_ids={"listing-1"})
        self.assertIn("WRONG_PRINTING", decision.reasons)
        self.assertIn("DUPLICATE_OR_ALREADY_RESERVED", decision.reasons)

    def test_defect_blocks_by_default(self):
        decision = evaluate_listing(self.target, listing(defects=["crease"]), remaining_budget_jpy=5000)
        self.assertIn("CONDITION_BELOW_REQUIREMENT", decision.reasons)


class NosanaBoundaryTests(unittest.TestCase):
    def test_extract_preserves_evidence(self):
        response = {
            "choices": [{"message": {"content": json.dumps({
                "card_name": "Pikachu", "price_jpy": 4000, "shipping_jpy": None,
                "seller_condition": "near mint", "defects": ["whitening"],
                "available": True, "evidence": {"seller_condition": "Near mint"},
            })}}]
        }
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
        client = NosanaClient(Settings(nosana_api_key="test-key"), transport=transport)
        extracted = client.extract_listing("Pikachu", "Near mint. Whitening. Price 4000 yen.")
        self.assertIsNone(extracted.shipping_jpy)
        self.assertEqual(extracted.defects, ["whitening"])

    def test_rejects_invented_evidence(self):
        response = {"choices": [{"message": {"content": json.dumps({"evidence": {"condition": "perfect"}})}}]}
        client = NosanaClient(Settings(nosana_api_key="test-key"),
                              transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
        with self.assertRaisesRegex(RuntimeError, "unsupported evidence"):
            client.extract_listing("Pikachu", "Near mint")

    def test_sale_draft_keeps_translated_defect(self):
        response = {"choices": [{"message": {"content": json.dumps({
            "title": "ピカチュウ SVP 001", "description": "裏面に白かけがあります。",
            "defect_disclosures": {"whitening on back": "裏面に白かけ"},
            "claim_map": {"ピカチュウ": ["name"]}, "unresolved_questions": [],
        })}}]}
        client = NosanaClient(Settings(nosana_api_key="test-key"),
                              transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
        facts = SaleFacts(copy_id="copy-1", name="Pikachu", set_code="SVP", card_number="001",
                          language="Japanese", defects=["whitening on back"], price_jpy=5000,
                          shipping_terms="seller pays")
        self.assertIn("白かけ", client.draft_sale(facts).description)

    def test_credit_exhaustion_is_explicit(self):
        client = NosanaClient(Settings(nosana_api_key="test-key"),
                              transport=httpx.MockTransport(lambda request: httpx.Response(402, json={})))
        with self.assertRaisesRegex(RuntimeError, "credit or quota limit"):
            client.complete_json("system", "user")


class TavilyBoundaryTests(unittest.TestCase):
    def test_search_keeps_source_and_date(self):
        response = {"results": [{"url": "https://example.com/article", "title": "Card release",
                                "content": "Public announcement", "published_date": "2026-11-10"}]}
        client = TavilyClient(Settings(tavily_api_key="test-key"),
                              transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
        source = client.search("Pikachu release")[0]
        self.assertEqual(source.published_at, "2026-11-10")
        self.assertEqual(source.content, "Public announcement")


if __name__ == "__main__":
    unittest.main()
