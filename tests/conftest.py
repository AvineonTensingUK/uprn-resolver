"""Shared test fixtures."""

import importlib

import pytest

# Modules that import convert._free_gb by name (so patching convert alone is not enough).
_FREE_GB_USERS = ["convert", "epc", "inspire", "ni", "osm", "osm_buildings", "poi", "provenance", "ros", "streets"]


@pytest.fixture(autouse=True)
def _plenty_of_disk(monkeypatch, request):
    """Disk-space guards check the real host disk; tests use tiny tmp data, so report ample free space.

    Mark a test with ``@pytest.mark.real_disk`` to keep the real check."""
    if request.node.get_closest_marker("real_disk"):
        return
    for name in _FREE_GB_USERS:
        try:
            mod = importlib.import_module(f"uprn_resolver.{name}")
        except ImportError:
            continue
        if hasattr(mod, "_free_gb"):
            monkeypatch.setattr(mod, "_free_gb", lambda *_a, **_k: 1e6)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_disk: use the real free-disk check instead of the test stub")
