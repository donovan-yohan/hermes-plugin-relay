"""REST surface for hermes-plugin-relay (participant seam contract v1 section 6).

Mounted by the Hermes dashboard at ``/api/plugins/hermes-plugin-relay/``. HTTP
routes carry no auth dependency of their own: they sit behind the dashboard's
global auth middleware, the same arrangement the bundled kanban plugin uses.

Import note
-----------
The host loads this file as a *flat* module
(``spec_from_file_location("hermes_dashboard_plugin_hermes-plugin-relay", …)``),
so relative imports are impossible here. :func:`_plugin_package_name` finds the
already-imported plugin package instead of loading a second copy — two copies
would mean two runtime managers, and the REST surface would stop sharing state
with the Hermes tools.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter()

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_FALLBACK_PACKAGE = "hermes_plugin_relay"

def _plugin_package_name() -> str:
    """Return the import name of the already-loaded plugin package."""

    root = str(_PLUGIN_ROOT)
    for name, module in list(sys.modules.items()):
        if not name.startswith("hermes_plugins.") or name.count(".") != 1:
            continue
        for entry in getattr(module, "__path__", None) or []:
            try:
                if str(Path(entry).resolve()) == root:
                    return name
            except OSError:  # pragma: no cover - unreadable path entry
                continue

    if _FALLBACK_PACKAGE in sys.modules:
        return _FALLBACK_PACKAGE

    spec = importlib.util.spec_from_file_location(
        _FALLBACK_PACKAGE,
        _PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[root],
    )
    if spec is None or spec.loader is None:  # pragma: no cover - packaging error
        raise ImportError(f"cannot load hermes-plugin-relay package from {root}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _FALLBACK_PACKAGE
    module.__path__ = [root]  # type: ignore[attr-defined]
    sys.modules[_FALLBACK_PACKAGE] = module
    spec.loader.exec_module(module)
    return _FALLBACK_PACKAGE


_PKG = _plugin_package_name()
_manager_mod = importlib.import_module(f"{_PKG}.runtime.manager")

DispatchCapacityError = _manager_mod.DispatchCapacityError
DispatchValidationError = _manager_mod.DispatchValidationError
ParticipantNotFoundError = _manager_mod.ParticipantNotFoundError
RelayRuntimeError = _manager_mod.RelayRuntimeError
SeamUnavailableError = _manager_mod.SeamUnavailableError

#: Statuses the runtime's own error classes declare. Re-exported for tests.
SEAM_UNAVAILABLE_STATUS = SeamUnavailableError.http_status
CAPACITY_STATUS = DispatchCapacityError.http_status


def _manager() -> Any:
    return _manager_mod.get_manager()


def _fail(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"ok": False, "error": message})


def _require_str(body: Any, key: str, *, strip: bool = True) -> str:
    """Pull a required non-empty string. ``strip=False`` preserves the value."""

    value = body.get(key) if isinstance(body, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise DispatchValidationError(f"'{key}' is required and must be a non-empty string")
    return value.strip() if strip else value


def _error_response(action: str, exc: BaseException) -> JSONResponse:
    """Answer a failed request, choosing status AND body by classification.

    ``pre_acceptance`` is the discriminator, and it decides two things at once:

    * **Status.** A 4xx tells the Desktop composer middleware "nothing
      happened", which licenses it to pass the draft on to Hermes. Only an
      error that provably had no side effect may claim that, so anything not
      marked pre-acceptance is forced to 5xx — an unclassified failure is
      ambiguous, and downgrading it to 4xx would duplicate the human's message
      when the user row had in fact already been appended.
    * **Body.** A pre-acceptance error describes the REQUEST (bad field,
      unknown participant, seam absent, at capacity), so its message is safe
      and useful to the caller. Anything else describes what went wrong INSIDE
      the runtime and can carry filesystem paths or provider diagnostics — for
      example the joined adapter spawn errors the manager raises when no turn
      could be queued. Those go to the log only.
    """

    status = getattr(exc, "http_status", 500)
    if getattr(exc, "pre_acceptance", False):
        return _fail(status, str(exc))
    log.warning("relay: %s failed: %s", action, exc, exc_info=True)
    return _fail(status if status >= 500 else 500, f"{action} failed unexpectedly")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/participants")
def list_participants(session_id: Optional[str] = None) -> Any:
    """Honest participant roster.

    ``session_id`` is optional and only adds the session-scoped ``busy`` status.
    """

    try:
        participants: List[Dict[str, Any]] = _manager().roster(session_id)
    except Exception:  # noqa: BLE001 - roster must never 500 the dashboard
        # Detail goes to the log only: raw exception text can carry paths and
        # provider diagnostics that do not belong in an HTTP body.
        log.warning("relay: roster failed", exc_info=True)
        return _fail(500, "failed to build the participant roster")
    return {"participants": participants}


@router.post("/dispatch")
def dispatch(body: Any = Body(default=None)) -> Any:
    """Fan one human submit out to every mentioned participant, exactly once.

    ``dispatch_id`` is generated once per submit attempt by the client and
    reused verbatim on transport retry; duplicates return the same turn refs and
    perform no second user-row append and no second fanout.
    """

    if not isinstance(body, dict):
        return _fail(400, "request body must be a JSON object")

    try:
        session_id = _require_str(body, "session_id")
        dispatch_id = _require_str(body, "dispatch_id")
        # The human's text is persisted verbatim, so it must not be stripped.
        text = _require_str(body, "text", strip=False)
    except DispatchValidationError as exc:
        return _error_response("dispatch", exc)

    raw_mentions = body.get("mentions")
    if not isinstance(raw_mentions, list) or not raw_mentions:
        return _fail(400, "'mentions' is required and must be a non-empty list of handles")
    mentions = [m.strip() for m in raw_mentions if isinstance(m, str) and m.strip()]
    if not mentions:
        return _fail(400, "'mentions' must contain at least one handle")

    append_user_message = body.get("append_user_message", True)
    if not isinstance(append_user_message, bool):
        return _fail(400, "'append_user_message' must be a boolean")

    try:
        result = _manager().dispatch_group(
            session_id,
            dispatch_id,
            text,
            mentions,
            append_user_message=append_user_message,
        )
    except Exception as exc:  # noqa: BLE001
        return _error_response("dispatch", exc)
    # A committed partial result (some participants queued, some failed) is a
    # 200: the submit was accepted and retrying the same dispatch_id replays
    # this exact body. `ok` and `failed` carry the real outcome.
    return result


@router.post("/interrupt")
def interrupt(body: Any = Body(default=None)) -> Any:
    """Best-effort interrupt of a participant's in-flight turn."""

    if not isinstance(body, dict):
        return _fail(400, "request body must be a JSON object")

    try:
        session_id = _require_str(body, "session_id")
        participant_id = _require_str(body, "participant_id")
    except DispatchValidationError as exc:
        return _error_response("interrupt", exc)

    try:
        return _manager().interrupt(session_id, participant_id)
    except Exception as exc:  # noqa: BLE001
        return _error_response("interrupt", exc)


__all__ = ["CAPACITY_STATUS", "SEAM_UNAVAILABLE_STATUS", "router"]
