#!/usr/bin/env python3
"""Opt-in smoke against the real Claude Code CLI.

    RELAY_SMOKE=1 python scripts/smoke_claude.py

Spawns ``claude`` through this plugin's own adapter in a throwaway directory,
sends one harmless prompt, prints the streamed deltas and the terminal status,
and exits non-zero on anything but a completed turn. No permission-bypass flags,
no writes outside the temp directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _smoke_common import bootstrap_package, run_smoke  # noqa: E402


def main() -> int:
    bootstrap_package()
    from hermes_plugin_relay.adapters.claude import ClaudeAdapter  # noqa: PLC0415

    return run_smoke(
        "claude",
        lambda _workdir: ClaudeAdapter(participant_id="claude:smoke"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
