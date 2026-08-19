#!/usr/bin/env python3
"""Opt-in smoke against the real Codex app-server.

    RELAY_SMOKE=1 python scripts/smoke_codex.py

Spawns ``codex app-server --listen stdio://`` through this plugin's own adapter
in a throwaway directory, sends one harmless prompt, prints the streamed deltas
and the terminal status, and exits non-zero on anything but a completed turn.
Reads ``~/.codex/auth.json`` only to check that a login exists; never writes to
it or to any other user config.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _smoke_common import bootstrap_package, run_smoke  # noqa: E402


def main() -> int:
    bootstrap_package()
    from hermes_plugin_relay.adapters.codex import CodexAdapter  # noqa: PLC0415

    return run_smoke(
        "codex",
        lambda _workdir: CodexAdapter(participant_id="codex:smoke"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
