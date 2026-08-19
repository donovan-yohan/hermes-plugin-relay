"""hermes-plugin-relay — external agent participants for Hermes.

Registers three Hermes tools (``agent_participants_list``, ``agent_message``,
``agent_interrupt``) and owns the participant runtime that streams Claude Code /
Codex / mock turns into the Hermes transcript through
``tui_gateway.participants``.

Registration is deliberately failure-tolerant (participant seam contract v1
section 11): with the core seam absent the plugin still loads, the roster
reports ``error`` with reason "hermes core seam missing", and the tools return a
visible error instead of crashing the session or the plugin doctor.

Every import here is deferred into :func:`register`. Two reasons: the plugin
entry point stays cheap for CLI/TUI processes that only defer-load it, and this
file stays importable as a bare module (pytest collects a package root's
``__init__.py`` with no parent package, where relative imports raise).
``tests/test_manifest.py`` pins :data:`__version__` to ``config.PLUGIN_VERSION``
and ``plugin.yaml``.
"""

from __future__ import annotations

import logging
from typing import Any

__version__ = "0.1.0"

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-plugin-relay"


def register(ctx: Any) -> None:
    """Plugin entry point — called by the Hermes plugin system."""

    from .config import load_config
    from .runtime.manager import RelayRuntimeManager, set_manager, shutdown_manager
    from .tools import register_tools

    try:
        config = load_config(getattr(ctx, "get_config", None))
    except Exception:  # noqa: BLE001 - a broken settings tree must not block load
        logger.warning("%s: falling back to default configuration", PLUGIN_ID, exc_info=True)
        config = load_config(None)

    # A reload calls register() again. Tear the previous manager down first or
    # its loop thread and any provider child processes outlive it.
    try:
        shutdown_manager()
    except Exception:  # noqa: BLE001
        logger.warning("%s: could not shut down the previous runtime", PLUGIN_ID, exc_info=True)

    # The asyncio worker thread starts lazily on the first dispatch, so plain
    # `hermes plugins doctor` / CLI loads stay inert.
    set_manager(RelayRuntimeManager(config))

    try:
        register_tools(ctx)
    except Exception:  # noqa: BLE001
        logger.warning("%s: failed to register tools", PLUGIN_ID, exc_info=True)

    def _teardown(*_args: Any, **_kwargs: Any) -> None:
        try:
            shutdown_manager()
        except Exception:  # noqa: BLE001
            logger.warning("%s: teardown failed", PLUGIN_ID, exc_info=True)

    try:
        ctx.on_unload(_teardown)
    except Exception:  # noqa: BLE001
        logger.warning("%s: could not register teardown", PLUGIN_ID, exc_info=True)


__all__ = ["PLUGIN_ID", "__version__", "register"]
