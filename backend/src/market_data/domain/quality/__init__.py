"""Data-quality validation: a list of small, composable rules."""

from market_data.domain.quality.report import QualityValidator
from market_data.domain.quality.rules import RuleContext, all_rules

__all__ = ["QualityValidator", "RuleContext", "all_rules"]
