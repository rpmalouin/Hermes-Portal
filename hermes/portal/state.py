"""The portal's one piece of writable state: favourites, and the theme.

Everything else in the portal is read-only, and this module is the reason the
project needs a write path at all: a star has to survive a restart.  It owns a
single small JSON file and touches nothing else.

Three properties are deliberate:

* **Nothing is created until something is written.**  Constructing
  :class:`PortalState` reads the file if it is there and never creates it, so a
  read-only deployment (or a read-only home directory) still works -- the stars
  simply do not persist, and the error is reported on the page rather than raised.
* **Writes are atomic.**  The new document goes to a temporary file in the same
  directory and is moved into place with :func:`os.replace`, so a crash mid-write
  cannot leave a half-written state file, and concurrent requests (the server is
  threaded) cannot interleave.  A lock serialises them within the process.
* **Reads are forgiving.**  A truncated or hand-edited file yields the defaults
  plus an error string, because a corrupt star list must never take the portal
  down.

The file is a plain document with a version, so a future phase can migrate it::

    {
      "version": 1,
      "updated_at": "2026-09-17T20:00:00+00:00",
      "favorites": [
        {"domain": "vault", "id": "Homelab/Homelab.md", "title": "Homelab",
         "added_at": "2026-09-17T19:58:11+00:00"}
      ]
    }
"""

from __future__ import annotations

import json
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path

from .sources import as_of

STATE_VERSION = 1
MAX_FAVORITES = 200
MAX_ID_CHARS = 512
MAX_TITLE_CHARS = 200
DEFAULT_STATE_NAME = "portal/state.json"


@dataclass(frozen=True)
class Favorite:
    """One starred record."""

    domain: str
    id: str
    title: str
    added_at: str

    @property
    def key(self) -> str:
        """Stable identity for the pair, as used by the UI."""
        return f"{self.domain}|{self.id}"


@dataclass(frozen=True)
class StateSnapshot:
    """What the state file said when it was read."""

    favorites: tuple[Favorite, ...] = ()
    path: Path | None = None
    exists: bool = False
    error: str = ""

    def keys(self) -> set[str]:
        """The set of ``domain|id`` keys that are starred."""
        return {favorite.key for favorite in self.favorites}


class StateError(ValueError):
    """A rejected write: the caller sent something the store will not keep."""


class StateWriteError(StateError):
    """The store could not be written (permissions, disk, read-only home).

    Separate from :class:`StateError` so the server can answer 500 rather than
    blaming the caller's request for the filesystem's problem.
    """


def _clean(value: object, limit: int, field: str) -> str:
    """Validate and trim one string field."""
    text = str(value or "").strip()
    if not text:
        raise StateError(f"{field} is required")
    if len(text) > limit:
        raise StateError(f"{field} is longer than {limit} characters")
    if any(ord(char) < 32 for char in text):
        raise StateError(f"{field} contains control characters")
    return text


def default_state_path(hermes_root: Path) -> Path:
    """The default state file: ``<hermes root>/portal/state.json``.

    It sits inside the Hermes home because that is the directory the agent
    already owns, and outside every source the portal reads: this module never
    opens ``state.db``, ``cron/``, a skill tree or a vault note for writing.
    """
    return Path(hermes_root) / DEFAULT_STATE_NAME


class PortalState:
    """Read and write the portal's favourites.

    The instance is cheap and thread-safe; the server builds one per process.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Create a store; *path* ``None`` means nothing is persisted."""
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()

    # -- reading ---------------------------------------------------------

    def read(self) -> StateSnapshot:
        """Return the current state, never raising."""
        if self.path is None:
            return StateSnapshot()
        if not self.path.is_file():
            return StateSnapshot(path=self.path, exists=False)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return StateSnapshot(
                path=self.path,
                exists=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(raw, dict):
            return StateSnapshot(
                path=self.path,
                exists=True,
                error="state file is not a JSON object",
            )
        favorites: list[Favorite] = []
        for entry in raw.get("favorites") or []:
            if not isinstance(entry, dict):
                continue
            domain = str(entry.get("domain", "")).strip()
            record_id = str(entry.get("id", "")).strip()
            if not domain or not record_id:
                continue
            favorites.append(
                Favorite(
                    domain=domain,
                    id=record_id,
                    title=str(entry.get("title", "")).strip() or record_id,
                    added_at=str(entry.get("added_at", "")).strip(),
                )
            )
        return StateSnapshot(
            favorites=tuple(favorites),
            path=self.path,
            exists=True,
        )

    # -- writing ---------------------------------------------------------

    def toggle(
        self,
        domain: str,
        record_id: str,
        title: str = "",
        *,
        add: bool | None = None,
    ) -> StateSnapshot:
        """Star or unstar one record and return the state afterwards.

        Args:
            domain: Domain key; callers validate it against the registry first.
            record_id: The record's id in that domain.
            title: Display title to remember; defaults to the id.
            add: ``True`` to star, ``False`` to unstar, ``None`` to flip.

        Raises:
            StateError: The input is malformed, the list is full, or the write
                failed.  The caller turns that into a 4xx/5xx response.
        """
        domain = _clean(domain, 64, "domain")
        record_id = _clean(record_id, MAX_ID_CHARS, "id")
        title = _clean(title or record_id, MAX_TITLE_CHARS, "title")
        if self.path is None:
            raise StateError(
                "no state file is configured, so favourites cannot persist"
            )

        with self._lock:
            current = self.read()
            existing = {favorite.key: favorite for favorite in current.favorites}
            key = f"{domain}|{record_id}"
            if add is None:
                add = key not in existing
            if add and key not in existing:
                if len(existing) >= MAX_FAVORITES:
                    raise StateError(
                        f"the favourites list is full ({MAX_FAVORITES} entries); "
                        "unstar something first"
                    )
                existing[key] = Favorite(
                    domain=domain,
                    id=record_id,
                    title=title,
                    added_at=as_of(),
                )
            elif not add:
                existing.pop(key, None)

            ordered = tuple(
                sorted(existing.values(), key=lambda favorite: favorite.added_at)
            )
            self._write(ordered)
            return StateSnapshot(favorites=ordered, path=self.path, exists=True)

    def _write(self, favorites: tuple[Favorite, ...]) -> None:
        """Write the state atomically."""
        if self.path is None:
            raise StateError("no state file is configured")
        payload = {
            "version": STATE_VERSION,
            "updated_at": as_of(),
            "favorites": [
                {
                    "domain": favorite.domain,
                    "id": favorite.id,
                    "title": favorite.title,
                    "added_at": favorite.added_at,
                }
                for favorite in favorites
            ],
        }
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".state-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise StateWriteError(f"cannot write {self.path}: {exc}") from exc


def favorite_from_payload(payload: object) -> tuple[str, str, str, bool | None]:
    """Extract ``(domain, id, title, add)`` from a posted JSON body.

    Raises:
        StateError: The body is not an object or the action is not understood.
    """
    if not isinstance(payload, dict):
        raise StateError("body must be a JSON object")
    unresolved = object()
    action: object = payload.get("action", unresolved)
    if action is unresolved:
        add: bool | None = None
    elif isinstance(action, bool):
        add = action
    elif str(action).strip().lower() in {"add", "star", "on", "true"}:
        add = True
    elif str(action).strip().lower() in {"remove", "unstar", "off", "false", "delete"}:
        add = False
    elif str(action).strip().lower() in {"toggle", "flip", ""}:
        add = None
    else:
        raise StateError(f"unknown action {action!r}: use add, remove or toggle")
    return (
        str(payload.get("domain", "")),
        str(payload.get("id", "")),
        str(payload.get("title", "")),
        add,
    )


def snapshot_with(snapshot: StateSnapshot, **changes: object) -> StateSnapshot:
    """Return a copy of *snapshot* with fields replaced (for tests and callers)."""
    return replace(snapshot, **changes)  # type: ignore[arg-type]
