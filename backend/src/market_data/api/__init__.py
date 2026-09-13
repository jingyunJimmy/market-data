"""FastAPI application exposing the market-data services over REST."""

from market_data.api.main import app, create_app

__all__ = ["app", "create_app"]
