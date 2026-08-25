"""Test bootstrap for the standalone plugin package."""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "hermes_plugin_relay"


def _bootstrap_package() -> types.ModuleType:
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PACKAGE
    module.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_bootstrap_package()


class RecordingTransport:
    def __init__(self, handler: Callable[..., Any]) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def request(self, *, method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> Any:
        call = {"method": method, "url": url, "headers": dict(headers), "body": body}
        self.calls.append(call)
        return self.handler(**call)


@pytest.fixture(autouse=True)
def reset_proxy_singleton():
    from hermes_plugin_relay.relay_proxy import (
        reset_actor_lane_for_tests,
        reset_relay_proxy_for_tests,
    )

    reset_relay_proxy_for_tests()
    reset_actor_lane_for_tests()
    yield
    reset_relay_proxy_for_tests()
    reset_actor_lane_for_tests()
