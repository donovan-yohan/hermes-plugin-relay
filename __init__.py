"""hermes-plugin-relay — native Relay workspace for Hermes Desktop.

The standalone plugin owns no participant/provider runtime, tools, subprocesses,
or persisted state. ``desktop/plugin.js`` renders the workspace while
``dashboard/plugin_api.py`` keeps Relay network access and credentials backend-side.
"""

from __future__ import annotations

from typing import Any

PLUGIN_ID = "hermes-plugin-relay"
__version__ = "0.2.0.dev0"


def register(_ctx: Any) -> None:
    """Standalone dashboard plugins need no CLI tools or lifecycle hooks."""


__all__ = ["PLUGIN_ID", "__version__", "register"]
