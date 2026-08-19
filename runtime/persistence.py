"""Durable provider session/thread ids, keyed by (hermes session, participant).

Resuming a Claude Code or Codex conversation needs the provider's own id. That
is the only state this plugin persists, and it lives in the Hermes plugin data
dir (``<hermes home>/plugin-data/hermes-plugin-relay/``) so profile switches and
plugin upgrades behave.

No credentials are ever written here.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import PLUGIN_ID

logger = logging.getLogger(__name__)

STORE_FILENAME = "provider_sessions.json"
STORE_VERSION = 1

#: Bound the file so a long-lived install cannot grow it without limit.
MAX_TRACKED_SESSIONS = 500


def resolve_plugin_data_dir(create: bool = True) -> Optional[Path]:
    """Locate this plugin's durable data dir, or ``None`` outside Hermes.

    Never guesses ``~/.hermes``: the active profile decides the Hermes home, so
    the answer has to come from Hermes itself (or an explicit ``HERMES_HOME``).
    """

    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        return Path(plugin_data_dir(PLUGIN_ID))
    except Exception:  # noqa: BLE001 - not running inside Hermes
        pass

    root: Optional[Path] = None
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        root = Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        env_home = os.environ.get("HERMES_HOME")
        if env_home:
            root = Path(env_home)

    if root is None:
        return None

    data_dir = root / "plugin-data" / PLUGIN_ID
    if create:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("%s: cannot create plugin data dir %s: %s", PLUGIN_ID, data_dir, exc)
            return None
    return data_dir


class ProviderSessionStore:
    """JSON-backed map of ``(session_id, participant_id) -> provider session id``.

    Degrades to memory-only (with a warning) when no data dir is resolvable, so
    the plugin still works in a bare CLI or test process.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    @classmethod
    def default(cls) -> "ProviderSessionStore":
        data_dir = resolve_plugin_data_dir()
        if data_dir is None:
            logger.warning(
                "%s: no Hermes data dir resolvable; provider session ids are memory-only",
                PLUGIN_ID,
            )
            return cls(None)
        return cls(data_dir / STORE_FILENAME)

    @property
    def path(self) -> Optional[Path]:
        return self._path

    # -- public API -------------------------------------------------------------

    def get(self, session_id: str, participant_id: str) -> Optional[str]:
        with self._lock:
            self._ensure_loaded()
            entry = self._data.get(session_id)
            if not isinstance(entry, dict):
                return None
            value = entry.get("participants", {}).get(participant_id)
            return value if isinstance(value, str) and value else None

    def set(self, session_id: str, participant_id: str, provider_session_id: str) -> None:
        if not provider_session_id:
            return
        with self._lock:
            self._ensure_loaded()
            entry = self._data.setdefault(session_id, {"participants": {}})
            participants = entry.setdefault("participants", {})
            if participants.get(participant_id) == provider_session_id:
                entry["updated_at"] = time.time()
                return
            participants[participant_id] = provider_session_id
            entry["updated_at"] = time.time()
            self._prune_locked()
            self._flush_locked()

    def forget(self, session_id: str, participant_id: Optional[str] = None) -> None:
        with self._lock:
            self._ensure_loaded()
            entry = self._data.get(session_id)
            if entry is None:
                return
            if participant_id is None:
                self._data.pop(session_id, None)
            else:
                entry.get("participants", {}).pop(participant_id, None)
                if not entry.get("participants"):
                    self._data.pop(session_id, None)
            self._flush_locked()

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            return json.loads(json.dumps(self._data))

    # -- io ---------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("%s: unreadable provider session store: %s", PLUGIN_ID, exc)
            return
        if not isinstance(payload, dict):
            return
        sessions = payload.get("sessions")
        if isinstance(sessions, dict):
            self._data = {
                key: value for key, value in sessions.items() if isinstance(value, dict)
            }

    def _prune_locked(self) -> None:
        if len(self._data) <= MAX_TRACKED_SESSIONS:
            return
        ordered = sorted(
            self._data.items(), key=lambda item: item[1].get("updated_at", 0.0)
        )
        for key, _ in ordered[: len(self._data) - MAX_TRACKED_SESSIONS]:
            self._data.pop(key, None)

    def _flush_locked(self) -> None:
        if self._path is None:
            return
        payload = {"version": STORE_VERSION, "sessions": self._data}
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.warning("%s: failed writing provider session store: %s", PLUGIN_ID, exc)
            try:
                tmp_path.unlink()
            except OSError:
                pass


__all__ = [
    "MAX_TRACKED_SESSIONS",
    "ProviderSessionStore",
    "STORE_FILENAME",
    "STORE_VERSION",
    "resolve_plugin_data_dir",
]
