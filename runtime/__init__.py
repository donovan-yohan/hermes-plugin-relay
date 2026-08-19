"""Runtime for hermes-plugin-relay: one dispatch path, one router, one store.

Deliberately empty of re-exports. ``adapters.base`` imports
``runtime.events`` for the normalized event vocabulary, so anything this package
initializer pulled in eagerly (``runtime.manager``, which imports ``adapters``)
would form an import cycle. Import the submodules directly::

    from .runtime.events import MessageDelta, TurnCompleted
    from .runtime.manager import get_manager
    from .runtime.router import plan_chain
"""

from __future__ import annotations

__all__: list = []
