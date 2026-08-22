"""Tests ClaudeAnalyst's request construction and response parsing against a
mocked anthropic client -- no network access or API key required. This is
the main insurance against the Anthropic SDK call shape (tools + forced
tool_choice + adaptive thinking + prompt caching) being wrong, since it's
the one integration this test suite can't exercise live.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from polybot.ai.analyst import ClaudeAnalyst
from polybot.config import AIConfig
from polybot.engine.models import Action


def make_tool_use_response(**overrides):
    payload = dict(
        action="BUY_YES",
        fair_value_probability=0.62,
        confidence=0.78,
        suggested_size_usd=50.0,
        time_horizon_days=4,
        reasoning="Base rate plus specific evidence suggests the market is underpricing YES.",
        risk_flags=["thin_liquidity"],
        key_uncertainties=["Resolution source could be delayed"],
    )
    payload.update(overrides)

    tool_use_block = SimpleNamespace(type="tool_use", name="submit_analysis", id="toolu_1", input=payload)
    text_block_maybe_thinking = SimpleNamespace(type="thinking", thinking="")
    usage = SimpleNamespace(input_tokens=500, output_tokens=120, cache_read_input_tokens=400, cache_creation_input_tokens=0)
    return SimpleNamespace(content=[text_block_maybe_thinking, tool_use_block], usage=usage)


def test_analyze_builds_expected_request_and_parses_decision(sample_market):
    analyst = ClaudeAnalyst(api_key="sk-test", config=AIConfig(model="claude-opus-5", effort="medium"))
    analyst.client = MagicMock()
    analyst.client.messages.create.return_value = make_tool_use_response()

    decision = analyst.analyze(sample_market)

    assert decision.action == Action.BUY_YES
    assert decision.fair_value_probability == 0.62
    assert decision.confidence == 0.78
    assert decision.risk_flags == ["thin_liquidity"]

    _, kwargs = analyst.client.messages.create.call_args
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_analysis"}
    assert kwargs["tools"][0]["name"] == "submit_analysis"
    assert kwargs["tools"][0]["strict"] is True
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "medium"}
    # System prompt must carry a cache breakpoint so repeated cycles reuse it.
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Market-specific data (the volatile part) belongs in the user message, not system.
    assert sample_market.question in kwargs["messages"][0]["content"]


def test_analyze_handles_json_string_tool_input(sample_market):
    """Defensive path: some SDK versions may hand back tool input as a raw
    JSON string rather than an already-parsed dict."""
    import json

    analyst = ClaudeAnalyst(api_key="sk-test", config=AIConfig())
    analyst.client = MagicMock()
    resp = make_tool_use_response()
    tool_block = next(b for b in resp.content if b.type == "tool_use")
    tool_block.input = json.dumps(tool_block.input)
    analyst.client.messages.create.return_value = resp

    decision = analyst.analyze(sample_market)
    assert decision.action == Action.BUY_YES
