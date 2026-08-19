"""Configuration loading, validation and cwd resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_plugin_relay.config import (
    DEFAULT_CHAIN_TURN_CAP,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    ConfigError,
    ParticipantConfig,
    RelayConfig,
    load_config,
    parse_chain,
    parse_participant,
    parse_participants,
    resolve_cwd,
)


def getter(settings: dict):
    return lambda key, default=None: settings.get(key, default)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_ship_claude_codex_and_mock():
    config = load_config(None)
    assert config.handles == ("claude", "codex", "mock")
    assert [p.adapter for p in config.participants] == ["claude", "codex", "mock"]
    assert [p.adapter_id for p in config.participants] == [
        "claude-code-stream-json",
        "codex-app-server",
        "mock",
    ]
    assert config.chain.enabled is False
    assert config.chain.turn_cap == DEFAULT_CHAIN_TURN_CAP
    assert config.tool_timeout_seconds == DEFAULT_TOOL_TIMEOUT_SECONDS
    assert config.plugin_id == "hermes-plugin-relay"


def test_lookup_by_id_and_handle():
    config = load_config(None)
    assert config.resolve("claude").id == "claude:default"
    assert config.resolve("claude:default").handle == "claude"
    assert config.resolve("@claude").handle == "claude"
    assert config.resolve("@CLAUDE").handle == "claude"
    assert config.resolve("nobody") is None
    assert config.resolve("") is None


def test_disabled_participants_are_hidden_from_the_roster():
    config = load_config(
        getter(
            {
                "participants": [
                    {"handle": "claude", "adapter": "claude"},
                    {"handle": "codex", "adapter": "codex", "enabled": False},
                ]
            }
        )
    )
    assert config.handles == ("claude",)
    assert len(config.participants) == 2
    assert config.by_handle("codex").enabled is False


# ---------------------------------------------------------------------------
# Participant parsing
# ---------------------------------------------------------------------------


def test_participant_defaults_are_derived():
    participant = parse_participant({"handle": "@Pi", "adapter": "mock"})
    assert participant.handle == "pi"
    assert participant.id == "mock:pi"
    assert participant.display_name == "Pi"
    assert participant.enabled is True
    assert participant.options == {}


def test_participant_display_name_from_handle_words():
    assert parse_participant({"handle": "prime-agent", "adapter": "mock"}).display_name == "Prime Agent"


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ({}, "missing 'handle'"),
        ({"handle": "claude"}, "missing 'adapter'"),
        ({"handle": "Bad Handle", "adapter": "mock"}, "invalid participant handle"),
        ({"handle": "-leading", "adapter": "mock"}, "invalid participant handle"),
        ({"handle": "hermes", "adapter": "mock"}, "reserved"),
        ("not a mapping", "must be a mapping"),
    ],
)
def test_invalid_participants_are_rejected(raw, fragment):
    with pytest.raises(ConfigError) as excinfo:
        parse_participant(raw)
    assert fragment in str(excinfo.value)


def test_invalid_entries_are_skipped_not_fatal(caplog):
    participants = parse_participants(
        [
            {"handle": "claude", "adapter": "claude"},
            {"handle": "hermes", "adapter": "mock"},
            {"nonsense": True},
            {"handle": "codex", "adapter": "codex"},
        ]
    )
    assert [p.handle for p in participants] == ["claude", "codex"]


def test_duplicate_handles_and_ids_keep_the_first():
    participants = parse_participants(
        [
            {"id": "a", "handle": "claude", "adapter": "claude"},
            {"id": "b", "handle": "claude", "adapter": "mock"},
            {"id": "a", "handle": "other", "adapter": "mock"},
        ]
    )
    assert [(p.id, p.handle) for p in participants] == [("a", "claude")]


def test_non_list_participants_falls_back_to_defaults():
    config = load_config(getter({"participants": {"claude": {}}}))
    assert config.handles == ("claude", "codex", "mock")


def test_options_are_passed_through():
    participant = parse_participant(
        {"handle": "mock", "adapter": "mock", "options": {"reply_template": "hi {text}"}}
    )
    assert participant.options == {"reply_template": "hi {text}"}


def test_unknown_adapter_still_parses_but_reports_its_own_id():
    participant = parse_participant({"handle": "future", "adapter": "not-real"})
    assert participant.adapter_id == "not-real"


# ---------------------------------------------------------------------------
# Chain + scalars
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,enabled,cap",
    [
        (None, False, DEFAULT_CHAIN_TURN_CAP),
        ({}, False, DEFAULT_CHAIN_TURN_CAP),
        ({"enabled": True}, True, DEFAULT_CHAIN_TURN_CAP),
        ({"enabled": True, "turn_cap": 5}, True, 5),
        ({"turn_cap": "nope"}, False, DEFAULT_CHAIN_TURN_CAP),
        ({"turn_cap": -3}, False, 0),
        ("garbage", False, DEFAULT_CHAIN_TURN_CAP),
    ],
)
def test_chain_parsing(raw, enabled, cap):
    chain = parse_chain(raw)
    assert chain.enabled is enabled
    assert chain.turn_cap == cap


@pytest.mark.parametrize(
    "raw,expected",
    [(60, 60.0), ("90", 90.0), (0, DEFAULT_TOOL_TIMEOUT_SECONDS),
     (-1, DEFAULT_TOOL_TIMEOUT_SECONDS), ("nope", DEFAULT_TOOL_TIMEOUT_SECONDS)],
)
def test_tool_timeout_parsing(raw, expected):
    assert load_config(getter({"tool_timeout_seconds": raw})).tool_timeout_seconds == expected


def test_a_raising_get_config_falls_back_to_defaults():
    def broken(key, default=None):
        raise RuntimeError("settings tree is broken")

    config = load_config(broken)
    assert config.handles == ("claude", "codex", "mock")
    assert config.chain.enabled is False


# ---------------------------------------------------------------------------
# cwd resolution
# ---------------------------------------------------------------------------


def test_cwd_precedence(tmp_path):
    participant_dir = tmp_path / "participant"
    plugin_dir = tmp_path / "plugin"
    session_dir = tmp_path / "session"
    for path in (participant_dir, plugin_dir, session_dir):
        path.mkdir()

    config = RelayConfig(default_cwd=str(plugin_dir))
    with_own = ParticipantConfig(
        id="p", handle="p", display_name="P", adapter="mock", cwd=str(participant_dir)
    )
    without = ParticipantConfig(id="q", handle="q", display_name="Q", adapter="mock")

    assert resolve_cwd(with_own, config, str(session_dir)) == str(participant_dir)
    assert resolve_cwd(without, config, str(session_dir)) == str(plugin_dir)
    assert resolve_cwd(without, RelayConfig(), str(session_dir)) == str(session_dir)
    assert resolve_cwd(without, RelayConfig(), None) == str(Path.home())


def test_missing_directories_fall_through(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    participant = ParticipantConfig(
        id="p", handle="p", display_name="P", adapter="mock", cwd=str(tmp_path / "gone")
    )
    config = RelayConfig(default_cwd=str(good))
    assert resolve_cwd(participant, config, None) == str(good)
