"""Shared test setup: no test reaches DexScreener.

`name_copies` searches DexScreener for every answer that carries a verdict, so
any endpoint test would otherwise make a real HTTP call. Tests that exercise
the search pass their own `fetch`.
"""

from __future__ import annotations

import pytest

import name_copies


@pytest.fixture(autouse=True)
def _no_dexscreener(monkeypatch):
    def offline(url):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(name_copies, "_fetch", offline)
    name_copies._cache.clear()
    yield
