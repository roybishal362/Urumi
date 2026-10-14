"""Shared test setup."""

from __future__ import annotations

import pytest

from harness import RUNNING


@pytest.fixture(autouse=True)
async def stop_leftover_servers():
    """Stop servers a failing test did not get to stop, so one failure cannot hang the run."""
    yield
    for server in list(RUNNING):
        await server.stop()
