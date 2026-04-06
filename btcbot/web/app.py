"""FastAPI application with async lifespan — runs engine + dashboard."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import CONFIG
from ..engine import Engine
from ..storage.db import init_db
from .routes import router

_STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Startup
    await init_db()

    engine = Engine(paper_mode=CONFIG.paper_mode)
    app.state.engine = engine
    engine_task = asyncio.create_task(engine.run(), name="engine")

    try:
        yield
    finally:
        # Shutdown
        await engine.stop()
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    app = FastAPI(title="btcbot", lifespan=_lifespan)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(router)
    return app


app = create_app()
