"""Shared harness for the opt-in real-provider smokes.

These scripts spawn a REAL provider CLI on the operator's machine, so they are
gated behind ``RELAY_SMOKE=1`` and constrained hard:

* no permission-bypass flags are ever passed (the adapters use
  ``--permission-mode default`` / plain ``codex app-server``);
* the child runs in a throwaway temp directory, removed on exit;
* nothing under the user's config or credentials is written;
* the child is always closed, including on Ctrl-C, via the adapter's own
  process-group teardown.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

ENV_FLAG = "RELAY_SMOKE"
DEFAULT_PROMPT = "Reply with exactly: pong"
DEFAULT_TIMEOUT_SECONDS = 180.0

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "hermes_plugin_relay"


def bootstrap_package():
    """Import the plugin as ``hermes_plugin_relay`` (the repo dir has a dash)."""

    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot import the plugin package from {ROOT}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PACKAGE
    module.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def _refuse(label: str) -> int:
    script = f"scripts/smoke_{label}.py"
    print(
        f"Refusing to run: this smoke spawns the real {label} CLI.\n"
        f"Enable it explicitly:\n\n"
        f"    {ENV_FLAG}=1 python {script}\n",
        file=sys.stderr,
    )
    return 2


def run_smoke(
    label: str,
    adapter_factory: Callable[[str], Any],
    *,
    prompt: str = DEFAULT_PROMPT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    if os.environ.get(ENV_FLAG) != "1":
        return _refuse(label)

    bootstrap_package()
    from hermes_plugin_relay.runtime.events import (  # noqa: PLC0415
        MessageDelta,
        SessionUpdated,
        TurnCompleted,
        TurnStarted,
    )
    from hermes_plugin_relay.adapters.base import TurnInput  # noqa: PLC0415

    workdir = tempfile.mkdtemp(prefix=f"relay-smoke-{label}-")
    adapter = adapter_factory(workdir)

    availability = type(adapter).availability()
    print(f"[{label}] availability: {availability.status}"
          + (f" ({availability.reason})" if availability.reason else ""))
    if availability.status != "ready":
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"[{label}] not ready; nothing was spawned.", file=sys.stderr)
        return 1

    print(f"[{label}] cwd: {workdir}")
    print(f"[{label}] prompt: {prompt!r}")

    terminal: list = []

    def on_event(event: Any) -> None:
        if isinstance(event, SessionUpdated):
            print(f"\n[{label}] provider session: {event.provider_session_id}")
        elif isinstance(event, TurnStarted):
            print(f"[{label}] turn started; streaming:\n", end="", flush=True)
        elif isinstance(event, MessageDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, TurnCompleted):
            terminal.append(event)

    adapter.subscribe(on_event)

    async def drive() -> None:
        await adapter.start_turn(
            TurnInput(
                text=prompt,
                cwd=workdir,
                participant_turn_id=f"pturn-{uuid.uuid4()}",
            )
        )
        deadline = asyncio.get_running_loop().time() + timeout
        while not terminal and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

    async def main() -> int:
        try:
            await drive()
        finally:
            await adapter.close()
        if not terminal:
            print(f"\n[{label}] TIMEOUT after {timeout}s", file=sys.stderr)
            return 1
        result = terminal[0]
        print(f"\n[{label}] status: {result.status}")
        if result.error:
            print(f"[{label}] error: {result.error}", file=sys.stderr)
        print(f"[{label}] final text: {result.text!r}")
        return 0 if result.status == "completed" else 1

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n[{label}] interrupted", file=sys.stderr)
        return 130
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"[{label}] removed {workdir}")


__all__ = ["DEFAULT_PROMPT", "ENV_FLAG", "bootstrap_package", "run_smoke"]
