"""LLM adapter for intelligent insights.

Infrastructure, like ``storage/``: it is the only package that talks to a
model. Nothing in ``domain/`` imports it.
"""

from market_data.llm.claude import ClaudeClient, InsightProviderError

__all__ = ["ClaudeClient", "InsightProviderError"]
