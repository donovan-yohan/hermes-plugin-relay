"""Provider session-id store: durability, bounds, and Hermes-home resolution."""

from __future__ import annotations

import json
import sys
import types

from hermes_plugin_relay.runtime.persistence import (
    MAX_TRACKED_SESSIONS,
    STORE_FILENAME,
    ProviderSessionStore,
    resolve_plugin_data_dir,
)


def test_round_trip_and_isolation(tmp_path):
    store = ProviderSessionStore(tmp_path / STORE_FILENAME)
    assert store.get("s1", "claude:default") is None

    store.set("s1", "claude:default", "provider-1")
    store.set("s1", "codex:default", "thread-1")
    store.set("s2", "claude:default", "provider-2")

    assert store.get("s1", "claude:default") == "provider-1"
    assert store.get("s1", "codex:default") == "thread-1"
    assert store.get("s2", "claude:default") == "provider-2"
    assert store.get("s2", "codex:default") is None


def test_values_survive_a_reload(tmp_path):
    path = tmp_path / STORE_FILENAME
    ProviderSessionStore(path).set("s1", "claude:default", "provider-1")
    assert ProviderSessionStore(path).get("s1", "claude:default") == "provider-1"


def test_file_is_valid_json_with_a_version(tmp_path):
    path = tmp_path / STORE_FILENAME
    ProviderSessionStore(path).set("s1", "claude:default", "provider-1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["sessions"]["s1"]["participants"]["claude:default"] == "provider-1"
    assert not list(path.parent.glob("*.tmp"))


def test_empty_values_are_ignored(tmp_path):
    store = ProviderSessionStore(tmp_path / STORE_FILENAME)
    store.set("s1", "claude:default", "")
    assert store.get("s1", "claude:default") is None
    assert not (tmp_path / STORE_FILENAME).exists()


def test_forget_removes_one_participant_then_the_session(tmp_path):
    path = tmp_path / STORE_FILENAME
    store = ProviderSessionStore(path)
    store.set("s1", "claude:default", "a")
    store.set("s1", "codex:default", "b")

    store.forget("s1", "claude:default")
    assert store.get("s1", "claude:default") is None
    assert store.get("s1", "codex:default") == "b"

    store.forget("s1", "codex:default")
    assert store.snapshot() == {}
    store.forget("missing-session")  # must not raise


def test_store_is_bounded(tmp_path):
    store = ProviderSessionStore(tmp_path / STORE_FILENAME)
    for index in range(MAX_TRACKED_SESSIONS + 25):
        store.set(f"s{index:04d}", "claude:default", f"provider-{index}")
    snapshot = store.snapshot()
    assert len(snapshot) == MAX_TRACKED_SESSIONS
    # Newest survive, oldest were pruned.
    assert f"s{MAX_TRACKED_SESSIONS + 24:04d}" in snapshot
    assert "s0000" not in snapshot


def test_corrupt_file_degrades_gracefully(tmp_path):
    path = tmp_path / STORE_FILENAME
    path.write_text("{ not json", encoding="utf-8")
    store = ProviderSessionStore(path)
    assert store.get("s1", "claude:default") is None
    store.set("s1", "claude:default", "recovered")
    assert ProviderSessionStore(path).get("s1", "claude:default") == "recovered"


def test_memory_only_store_without_a_path():
    store = ProviderSessionStore(None)
    store.set("s1", "claude:default", "provider-1")
    assert store.get("s1", "claude:default") == "provider-1"
    assert store.path is None


def test_unwritable_path_does_not_raise(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    store = ProviderSessionStore(blocker / "nested" / STORE_FILENAME)
    store.set("s1", "claude:default", "provider-1")
    assert store.get("s1", "claude:default") == "provider-1"


# ---------------------------------------------------------------------------
# Data-dir resolution
# ---------------------------------------------------------------------------


def test_data_dir_uses_hermes_home_env(temp_hermes_home):
    data_dir = resolve_plugin_data_dir()
    assert data_dir == temp_hermes_home / "plugin-data" / "hermes-plugin-relay"
    assert data_dir.is_dir()


def test_data_dir_prefers_hermes_plugin_storage(monkeypatch, tmp_path):
    target = tmp_path / "from-plugin-storage"
    target.mkdir()
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.__path__ = []
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: target
    monkeypatch.setitem(sys.modules, "plugins", plugins_pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    monkeypatch.setattr(plugins_pkg, "plugin_storage", storage, raising=False)

    assert resolve_plugin_data_dir() == target


def test_data_dir_is_none_without_any_hermes_context(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(sys.modules, "plugins", None)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    assert resolve_plugin_data_dir() is None


def test_default_store_never_touches_the_real_home(temp_hermes_home):
    store = ProviderSessionStore.default()
    assert store.path is not None
    assert str(temp_hermes_home) in str(store.path)
    assert store.path.name == STORE_FILENAME


def test_default_store_is_memory_only_without_hermes(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(sys.modules, "plugins", None)
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    store = ProviderSessionStore.default()
    assert store.path is None
    store.set("s1", "p", "v")
    assert store.get("s1", "p") == "v"
