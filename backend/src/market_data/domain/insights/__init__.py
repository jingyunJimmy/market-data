"""Intelligent insights: recurring quality patterns and the rules they suggest.

Evidence is computed, an LLM proposes conclusions, and anything malformed is
refused; what is kept is shown beside the evidence it cites. See ``models.py``
for the flow.
"""

from market_data.domain.insights.evidence import build_evidence_pack
from market_data.domain.insights.models import EvidencePack, InsightsReport
from market_data.domain.insights.verify import verify_patterns, verify_suggestions

__all__ = [
    "EvidencePack",
    "InsightsReport",
    "build_evidence_pack",
    "verify_patterns",
    "verify_suggestions",
]
