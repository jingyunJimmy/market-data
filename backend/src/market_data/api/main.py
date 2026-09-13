from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from market_data import __version__
from market_data.api.deps import get_repo
from market_data.api.routes import analytics, contracts, insights, quality


def _configure_logging() -> None:
    """Print the app's own INFO logs in the server terminal, beside uvicorn's.

    uvicorn configures only its own loggers, so without a handler here the
    ``market_data.*`` INFO records -- insights progress among them -- go nowhere.
    """
    app_logger = logging.getLogger("market_data")
    if app_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(levelname)-9s %(asctime)s %(name)s - %(message)s", datefmt="%H:%M:%S")
    )
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    get_repo()  # open + initialise the DuckDB store on startup
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Futures Market Data",
        version=__version__,
        summary="Analytics and data-quality reporting over ingested futures market data.",
        lifespan=_lifespan,
    )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    for module in (contracts, analytics, quality, insights):
        app.include_router(module.router)
    return app


app = create_app()


def serve() -> None:
    """Console-script entry point (``market-data-serve``): run the API with uvicorn.

    uvicorn is imported here rather than at module scope so that importing the
    app -- which the tests and any ASGI server do -- costs nothing extra.
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Futures Market Data API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on source changes")
    args = parser.parse_args()

    # Passed as an import string, not the object: uvicorn needs that to re-import
    # the app in a worker process when --reload is on.
    uvicorn.run("market_data.api.main:app", host=args.host, port=args.port, reload=args.reload)
