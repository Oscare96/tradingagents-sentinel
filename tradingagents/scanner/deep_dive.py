"""Deep-dive layer: run top screen candidates through the full TradingAgents
multi-agent pipeline.

This is the "Oracle" equivalent: the screens produce a candidate list, and
each candidate gets the full treatment -- analysts, bull/bear debate, trader,
risk team, portfolio manager -- producing a rated decision with suggested
entry, stop-loss, and price target. Output is a research report for Oscar to
review manually; nothing here places trades.

Deep dives need a configured LLM provider. That can be a paid provider key
(OpenAI, Anthropic, ...) or the FreeLLMAPI self-hosted gateway, which routes
across 34 providers' free tiers (set TRADINGAGENTS_LLM_PROVIDER=freellmapi,
FREELLMAPI_API_KEY to its unified key, and the model to "auto"). When no key
is present the scanner runs in screen-only mode and says so plainly.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "bedrock": None,  # uses AWS credential chain instead
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    # FreeLLMAPI self-hosted gateway: unified key from its dashboard Keys page.
    "freellmapi": "FREELLMAPI_API_KEY",
}


def deep_dive_available() -> tuple[bool, str]:
    """Whether an LLM key is configured for the selected provider."""
    provider = os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "openai").lower()
    if provider not in _PROVIDER_KEY_ENV:
        return False, f"unknown provider '{provider}' -- check TRADINGAGENTS_LLM_PROVIDER"
    key_env = _PROVIDER_KEY_ENV[provider]
    if key_env is None:
        return True, f"provider '{provider}' needs no API-key env var"
    if os.environ.get(key_env):
        return True, f"{key_env} is set"
    return False, (
        f"{key_env} is not set -- deep-dive analysis is unavailable; "
        "running in screen-only mode"
    )


def analyze_tickers(tickers: list[str], trade_date: str) -> list[dict]:
    """Run each ticker through the TradingAgents graph; return decision dicts."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()
    config["debug"] = False
    graph = TradingAgentsGraph(debug=False, config=config)

    results = []
    for ticker in tickers:
        try:
            logger.info("Deep dive: %s", ticker)
            final_state, signal = graph.propagate(ticker, trade_date)
            decision = final_state.get("final_trade_decision", "") if final_state else ""
            results.append({
                "ticker": ticker,
                "signal": signal,
                "decision": decision,
                "ok": True,
            })
        except Exception as exc:
            logger.warning("Deep dive failed for %s: %s", ticker, exc)
            results.append({"ticker": ticker, "signal": "ERROR", "decision": str(exc), "ok": False})
    return results
