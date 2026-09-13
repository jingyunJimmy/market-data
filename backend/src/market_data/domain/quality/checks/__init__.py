"""The built-in quality checks. ``rules.all_rules()`` names the ones that run."""

from market_data.domain.quality.checks import duplicates, gaps, outliers, values

__all__ = ["duplicates", "gaps", "outliers", "values"]
