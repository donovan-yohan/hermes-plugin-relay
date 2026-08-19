"""Manifest / registration gates.

These are the cheap failures the plugin doctor would otherwise catch late:
declared tools drifting from registered tools, version drift between
``plugin.yaml`` / ``__init__`` / ``config``, unknown manifest fields, and
registration that crashes when the Hermes core seam is missing.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
import yaml

from conftest import ROOT

import hermes_plugin_relay
from hermes_plugin_relay.config import PLUGIN_ID, PLUGIN_VERSION
from hermes_plugin_relay.tools import TOOL_NAMES, TOOLSET

# Mirrors hermes_cli/plugins.py::_KNOWN_MANIFEST_FIELDS. Anything outside this
# set makes the loader log a warning for a manifest_version >= 2 plugin.
KNOWN_MANIFEST_FIELDS = {
    "name", "version", "description", "author", "requires_env",
    "provides_tools", "provides_hooks", "kind", "hooks", "label",
    "optional_env", "platforms", "external_dependencies", "pip_dependencies",
    "provides_browser_providers", "provides_web_providers",
    "manifest_version", "api_version", "requires_plugins",
    "python_dependencies", "config_schema", "license", "homepage", "tags",
    "capabilities", "emits", "listens", "hermes", "depends",
}

# Mirrors hermes_cli/plugins.py::_CONFIG_SCHEMA_TYPES.
KNOWN_CONFIG_SCHEMA_TYPES = {
    "str", "string", "int", "integer", "float", "number",
    "bool", "boolean", "list", "array", "dict", "object",
}


@pytest.fixture(scope="module")
def manifest() -> Dict[str, Any]:
    with open(ROOT / "plugin.yaml", "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def dashboard_manifest() -> Dict[str, Any]:
    with open(ROOT / "dashboard" / "manifest.json", "r", encoding="utf-8") as handle:
        return json.load(handle)


class RecordingCtx:
    """Stands in for Hermes's PluginContext."""

    def __init__(self, settings: Dict[str, Any] = None, get_config_raises: bool = False):
        self.settings = settings or {}
        self.get_config_raises = get_config_raises
        self.registered: List[Dict[str, Any]] = []
        self.unload_callbacks: List[Any] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        if self.get_config_raises:
            raise RuntimeError("settings tree is broken")
        return self.settings.get(key, default)

    def register_tool(self, **kwargs: Any) -> None:
        self.registered.append(kwargs)

    def on_unload(self, callback: Any) -> None:
        self.unload_callbacks.append(callback)


# ---------------------------------------------------------------------------
# plugin.yaml
# ---------------------------------------------------------------------------


def test_manifest_identity(manifest):
    assert manifest["name"] == PLUGIN_ID == "hermes-plugin-relay"
    assert manifest["manifest_version"] == 2
    assert manifest["kind"] == "standalone"
    assert manifest["label"]
    assert manifest["description"].strip()


def test_version_is_declared_once_per_place_and_agrees(manifest):
    assert manifest["version"] == PLUGIN_VERSION == hermes_plugin_relay.__version__


def test_provides_tools_matches_registered_tools(manifest):
    assert manifest["provides_tools"] == list(TOOL_NAMES)
    assert all(isinstance(name, str) for name in manifest["provides_tools"])


def test_manifest_declares_no_hooks(manifest):
    """No hooks registered, so neither key may appear (doctor checks both)."""

    assert "provides_hooks" not in manifest
    assert "hooks" not in manifest


def test_manifest_has_no_unknown_fields(manifest):
    unknown = set(manifest) - KNOWN_MANIFEST_FIELDS
    assert unknown == set()


def test_config_schema_types_are_supported(manifest):
    schema = manifest["config_schema"]
    assert set(schema) == {"participants", "chain", "tool_timeout_seconds", "cwd"}
    for key, spec in schema.items():
        assert isinstance(spec, dict), key
        assert str(spec["type"]).lower() in KNOWN_CONFIG_SCHEMA_TYPES, key
        assert spec["description"].strip()


# ---------------------------------------------------------------------------
# dashboard/manifest.json
# ---------------------------------------------------------------------------


def test_dashboard_manifest_mounts_at_the_contract_prefix(dashboard_manifest, manifest):
    # The mount prefix is /api/plugins/<dashboard manifest name>/.
    assert dashboard_manifest["name"] == manifest["name"]
    assert dashboard_manifest["api"] == "plugin_api.py"
    assert (ROOT / "dashboard" / dashboard_manifest["api"]).is_file()
    assert dashboard_manifest["version"] == manifest["version"]
    # No dashboard tab: this plugin only contributes a backend.
    assert "tab" not in dashboard_manifest


# ---------------------------------------------------------------------------
# register(ctx)
# ---------------------------------------------------------------------------


def test_register_registers_declared_tools_and_teardown(manifest):
    ctx = RecordingCtx()
    hermes_plugin_relay.register(ctx)
    try:
        assert [entry["name"] for entry in ctx.registered] == manifest["provides_tools"]
        assert {entry["toolset"] for entry in ctx.registered} == {TOOLSET}
        assert len(ctx.unload_callbacks) == 1
    finally:
        for callback in ctx.unload_callbacks:
            callback()


def test_register_survives_a_missing_core_seam(seam_absent):
    """Contract section 11: registration must not crash without the seam."""

    ctx = RecordingCtx()
    hermes_plugin_relay.register(ctx)
    try:
        from hermes_plugin_relay.runtime.manager import get_manager

        manager = get_manager()
        assert manager.seam_available() is False
        roster = manager.roster()
        assert roster, "default participants should still be listed"
        assert {entry["status"] for entry in roster} == {"error"}
        assert {entry["reason"] for entry in roster} == {"hermes core seam missing"}
    finally:
        for callback in ctx.unload_callbacks:
            callback()


def test_register_survives_a_broken_settings_tree():
    ctx = RecordingCtx(get_config_raises=True)
    hermes_plugin_relay.register(ctx)
    try:
        assert [entry["name"] for entry in ctx.registered] == list(TOOL_NAMES)
    finally:
        for callback in ctx.unload_callbacks:
            callback()


def test_register_applies_settings():
    ctx = RecordingCtx(
        settings={
            "participants": [
                {"handle": "solo", "adapter": "mock", "display_name": "Solo"},
            ],
            "chain": {"enabled": True, "turn_cap": 4},
            "tool_timeout_seconds": 42,
        }
    )
    hermes_plugin_relay.register(ctx)
    try:
        from hermes_plugin_relay.runtime.manager import get_manager

        config = get_manager().config
        assert config.handles == ("solo",)
        assert config.chain.enabled is True
        assert config.chain.turn_cap == 4
        assert config.tool_timeout_seconds == 42.0
    finally:
        for callback in ctx.unload_callbacks:
            callback()


def test_reregister_shuts_down_the_previous_runtime():
    """A plugin reload must not leak the old loop thread or its children."""

    from hermes_plugin_relay.runtime.manager import get_manager

    first_ctx = RecordingCtx()
    hermes_plugin_relay.register(first_ctx)
    first = get_manager()
    first.start()  # force the loop thread up so the leak would be observable
    assert first._thread is not None and first._thread.is_alive()
    first_thread = first._thread

    second_ctx = RecordingCtx()
    hermes_plugin_relay.register(second_ctx)
    try:
        second = get_manager()
        assert second is not first
        first_thread.join(timeout=10)
        assert not first_thread.is_alive(), "previous runtime loop thread outlived reload"
    finally:
        for callback in second_ctx.unload_callbacks:
            callback()


def test_teardown_is_idempotent():
    ctx = RecordingCtx()
    hermes_plugin_relay.register(ctx)
    callback = ctx.unload_callbacks[0]
    callback()
    callback()  # must not raise


def test_root_init_has_no_top_level_relative_imports():
    """Keeps the entry point importable as a bare module (pytest, tooling)."""

    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    module_level = [
        line
        for line in source.splitlines()
        if line.startswith("from .") or line.startswith("import .")
    ]
    assert module_level == []
