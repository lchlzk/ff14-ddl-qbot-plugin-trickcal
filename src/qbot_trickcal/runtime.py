"""Install the plugin website and its daily catalogue refresh lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import FastAPI
from nonebot import get_driver
from nonebot.log import logger

from bot_tools.storage import Store
from .trickcal_board import refresh_catalogue_if_due
from .trickcal_web import BoardWeb, install_trickcal_web


async def catalogue_worker(store: Store) -> None:
    while True:
        try:
            if await refresh_catalogue_if_due(store):
                logger.info("Trickcal board catalogue refreshed for the daily 18:00 slot")
        except Exception as exc:
            logger.warning("Trickcal daily catalogue refresh failed ({})", type(exc).__name__)
        await asyncio.sleep(60)


def install_web(app: FastAPI, store: Store) -> BoardWeb:
    """Called by the bot core only when this plugin has been loaded."""
    existing = getattr(app.state, "trickcal_web", None)
    if existing is not None:
        return existing
    web = install_trickcal_web(app, store)
    app.state.trickcal_web = web
    task: asyncio.Task | None = None
    driver = get_driver()

    @driver.on_startup
    async def start_catalogue_refresh() -> None:
        nonlocal task
        if task is None or task.done():
            task = asyncio.create_task(catalogue_worker(store))

    @driver.on_shutdown
    async def stop_catalogue_refresh() -> None:
        nonlocal task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            task = None

    return web
