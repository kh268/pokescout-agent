"""Offline checks for the real graph wiring, using scripted model/tool replies."""

import io
import os
import unittest
from collections.abc import Sequence
from contextlib import redirect_stdout
from typing import Self
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import agent


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Sequence[object], **kwargs: object) -> Self:
        return self


class AgentSmokeTests(unittest.TestCase):
    def test_model_search_model_loop(self) -> None:
        model = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{
                "name": "tavily_search",
                "args": {"query": "Tavily documentation"},
                "id": "offline-search-1",
                "type": "tool_call",
            }]),
            AIMessage(content="Tavily documentation: https://docs.tavily.com/"),
        ])
        payload = {"results": [{"title": "Tavily Docs", "url": "https://docs.tavily.com/"}]}
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"TAVILY_API_KEY": "offline", "OPENAI_API_KEY": "offline"}, clear=True),
            patch("agent.build_model", return_value=model),
            patch.object(agent.TavilySearch, "_run", return_value=payload) as search,
            redirect_stdout(output),
        ):
            agent.run_agent("Find Tavily documentation")
        search.assert_called_once()
        self.assertIn("PASS: ReAct agent completed with 1 successful Tavily call(s).", output.getvalue())
        self.assertIn("https://docs.tavily.com/", output.getvalue())

    def test_answer_without_search_is_not_a_success(self) -> None:
        model = ScriptedModel(responses=[AIMessage(content="An unverified answer")])
        with (
            patch.dict(os.environ, {"TAVILY_API_KEY": "offline", "OPENAI_API_KEY": "offline"}, clear=True),
            patch("agent.build_model", return_value=model),
            self.assertRaisesRegex(RuntimeError, "without calling Tavily"),
        ):
            agent.run_agent("Find Tavily documentation")

    def test_missing_key_is_reported_before_request(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "TAVILY_API_KEY"):
            agent.search_only("Tavily documentation")


if __name__ == "__main__":
    unittest.main()
