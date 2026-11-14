"""Backend-facing facade for the logical PokéScout modules."""

from pathlib import Path

from pokescout_agents.config import Settings
from pokescout_agents.decision import evaluate_listing
from pokescout_agents.providers.gemini import GeminiClient
from pokescout_agents.providers.nosana import NosanaClient
from pokescout_agents.providers.tavily import TavilyClient
from pokescout_agents.schemas import (
    CardIdentityCandidate, Evaluation, ListingEvidence, ListingObservation,
    ResearchSource, ResearchSummary, SaleDraft, SaleFacts, TargetCard,
)


class PokeScoutServices:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()

    def identify_card(self, image_path: str | Path) -> CardIdentityCandidate:
        return GeminiClient(self.settings).identify_card(image_path)

    def analyze_listing(self, title: str, description: str) -> ListingEvidence:
        return NosanaClient(self.settings).extract_listing(title, description)

    def research_sources(self, query: str, max_results: int = 5) -> list[ResearchSource]:
        return TavilyClient(self.settings).search(query, max_results)

    def summarize_research(self, sources: list[ResearchSource]) -> ResearchSummary:
        return NosanaClient(self.settings).summarize_research(sources)

    def evaluate_listing(self, target: TargetCard, listing: ListingObservation, **limits) -> Evaluation:
        return evaluate_listing(target, listing, **limits)

    def draft_sale(self, facts: SaleFacts) -> SaleDraft:
        return NosanaClient(self.settings).draft_sale(facts)
