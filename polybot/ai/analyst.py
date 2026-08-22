"""Claude-powered market analyst.

Analysis is elicited as a single, strictly-schema'd tool call
(`submit_analysis`) rather than free text, so the output is always
machine-parseable. The system prompt is marked cache_control=ephemeral since
it's identical across every market evaluated in a scan cycle -- only the
user message (the specific market data) changes, so caching it can save a
large fraction of input-token cost on multi-market cycles.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import anthropic

from ..config import AIConfig
from .prompts import SYSTEM_PROMPT, build_market_user_prompt
from ..engine.models import MarketSnapshot, TradeDecision

logger = logging.getLogger(__name__)

ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "Submit your structured probability estimate and trade recommendation for this market.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["BUY_YES", "BUY_NO", "HOLD", "NO_TRADE"],
                "description": "Your recommended action.",
            },
            "fair_value_probability": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Your best estimate of true P(YES).",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "How confident you are in fair_value_probability, independent of how far it is from the market price.",
            },
            "suggested_size_usd": {
                "type": "number",
                "minimum": 0,
                "description": "Rough conviction-weighted position size in dollars; 0 for HOLD/NO_TRADE. The risk layer decides the real size.",
            },
            "time_horizon_days": {
                "type": "number",
                "minimum": 0,
                "description": "Days you expect it to take for the market price to move toward your estimate (or for the market to resolve).",
            },
            "reasoning": {
                "type": "string",
                "description": "Concise (a few sentences) explanation of your reasoning, referencing base rates, evidence, and market efficiency.",
            },
            "risk_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short tags for anything that should reduce trust in this call, e.g. 'ambiguous_resolution', 'thin_liquidity', 'stale_information', 'high_volatility_subject'.",
            },
            "key_uncertainties": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The main things you don't know that could change this estimate.",
            },
        },
        "required": [
            "action",
            "fair_value_probability",
            "confidence",
            "suggested_size_usd",
            "time_horizon_days",
            "reasoning",
            "risk_flags",
            "key_uncertainties",
        ],
        "additionalProperties": False,
    },
}


class ClaudeAnalyst:
    def __init__(self, api_key: Optional[str], config: AIConfig):
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.config = config

    def analyze(self, market: MarketSnapshot) -> TradeDecision:
        user_prompt = build_market_user_prompt(self._market_block(market))

        response = self.client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.effort},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[ANALYSIS_TOOL],
            tool_choice={"type": "tool", "name": "submit_analysis"},
            messages=[{"role": "user", "content": user_prompt}],
        )

        tool_use = next(b for b in response.content if b.type == "tool_use")
        logger.debug(
            "Claude analysis for %s: cache_read=%s cache_write=%s input=%s output=%s",
            market.slug,
            getattr(response.usage, "cache_read_input_tokens", None),
            getattr(response.usage, "cache_creation_input_tokens", None),
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        data = tool_use.input
        if isinstance(data, str):  # defensive: SDK normally gives a dict already
            data = json.loads(data)
        return TradeDecision(**data)

    @staticmethod
    def _market_block(m: MarketSnapshot) -> str:
        days = m.days_to_resolution
        days_str = f"{days:.2f}" if days is not None else "unknown"
        desc = (m.description or "").strip()
        if len(desc) > 1500:
            desc = desc[:1500] + "... [truncated]"

        return f"""\
Question: {m.question}
Resolution details: {desc or "(none provided)"}
Tags: {", ".join(m.tags) or "(none)"}

Current market pricing:
  YES price: {m.yes_price:.4f}   NO price: {m.no_price:.4f}
  Best bid/ask (YES): {m.best_bid_yes} / {m.best_ask_yes}
  Bid-ask spread: {m.spread}
  24h volume: ${m.volume_24hr:,.0f}   Liquidity: ${m.liquidity:,.0f}
  Days until resolution: {days_str}
  Negative-risk (multi-outcome group) market: {m.neg_risk}"""
