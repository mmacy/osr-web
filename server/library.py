"""The adventure library: every osr-forge adventure the server can host.

Sources, scanned in order:

1. The bundled document (`content/adventure.json`, or `OSR_WEB_ADVENTURE` when set).
2. The drop directory — the repo `adventures/` directory, or `OSR_WEB_ADVENTURES_DIR`
   when set. Drop a stamped adventure JSON, an osr-forge output directory (anything
   holding an `adventure.json`), or a symlink to either.
3. `OSR_WEB_ADVENTURES` — a colon-separated list of extra files or directories,
   scanned the same way.

`OSR_WEB_ADVENTURES_DIR` (source 2) and `OSR_WEB_ADVENTURES` (source 3) are
different knobs: the first *replaces* the drop directory, the second *appends*
extra sources and leaves the drop directory in place.

Entries are deduplicated by file content (the bundled document and a forge run of
the same adventure dropped into `adventures/` collapse to one entry), given stable
slug ids derived from the adventure's name, and cached by path and mtime so the
scan stays cheap.

The scan reads metadata only and never validates a document — a file someone put
on the server's own disk is trusted to be what it says. The one exception is
[`add_adventure`][server.library.add_adventure], the upload path behind
`POST /api/adventures`: bytes arriving over the wire are fully vetted before any
of them land in the drop directory.

Deletion ([`remove_adventure`][server.library.remove_adventure], behind
`DELETE /api/adventures/{id}`) is the drop directory's alone: only source 2 puts
a `drop_item` on an entry, so the bundled document and anything reached through
`OSR_WEB_ADVENTURES` can be listed and played but never deleted.
"""

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from osrlib.crawl.adventure import Adventure, validate_adventure
from osrlib.data import load_equipment, load_monsters
from osrlib.errors import ContentValidationError
from osrlib.versioning import check_document
from pydantic import ValidationError

_CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
# The default drop directory; read it through `adventures_dir()`, which applies
# the OSR_WEB_ADVENTURES_DIR override.
_ADVENTURES_DIR = Path(__file__).resolve().parent.parent / "adventures"

_meta_cache: dict[Path, tuple[float, AdventureEntry | None]] = {}


@dataclass(frozen=True)
class AdventureEntry:
    """One playable adventure: its library id, display card, and source path.

    Beyond the id and the display card, an entry carries the player-safe facts
    the library pane shows before any game exists — the hooks, the town's name,
    and how many enterable dungeons the document holds. All of it is text a
    player could read off the adventure's own cover; nothing referee-only
    (keys, maps, treasure) is parsed.

    `path` is the document itself, resolved through any symlink. `drop_item` is
    the thing *in the drop directory* that put the entry in the library — the
    `.json` file, or the directory (often a symlink to a forge run) holding it —
    and it is the only path deletion is ever allowed to touch. An entry from the
    bundled document or from `OSR_WEB_ADVENTURES` has no `drop_item` and cannot
    be deleted.
    """

    id: str
    name: str
    description: str
    path: Path
    digest: str
    hooks: tuple[str, ...] = ()
    town_name: str = ""
    dungeon_count: int = 0
    drop_item: Path | None = None

    @property
    def removable(self) -> bool:
        """Whether `DELETE /api/adventures/{id}` may delete this entry."""
        return self.drop_item is not None

    @property
    def source_kind(self) -> str:
        """What deleting this entry would actually remove, in one word.

        The client says it out loud in the confirmation, because the three cases
        differ enormously: a `link` deletion leaves the forge run it points at
        untouched, a `folder` deletion takes the directory and everything in it,
        and a `file` deletion takes one document. `bundled` and `external`
        entries aren't deletable at all.
        """
        if self.drop_item is None:
            return "external" if self.path != _bundled_path().resolve() else "bundled"
        if self.drop_item.is_symlink():
            return "link"
        return "folder" if self.drop_item.is_dir() else "file"


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "adventure"


def _hooks(payload: dict) -> tuple[str, ...]:
    return tuple(str(hook) for hook in payload.get("hooks") or () if hook)


def _town_name(payload: dict) -> str:
    town = payload.get("town")
    return str(town.get("name") or "") if isinstance(town, dict) else ""


def _dungeon_count(payload: dict) -> int:
    """How many of the document's dungeons the party can actually enter.

    Mirrors the state payload's `dungeons` filter: a dungeon with no entrance on
    any level never gets an enter button, so it doesn't count on the card either.
    """
    count = 0
    for dungeon in payload.get("dungeons") or ():
        if not isinstance(dungeon, dict):
            continue
        levels = dungeon.get("levels") or ()
        if any(isinstance(level, dict) and level.get("entrance") is not None for level in levels):
            count += 1
    return count


def _read_entry(path: Path) -> AdventureEntry | None:
    """Parse one candidate file into an entry (id assigned later); None if not an adventure doc."""
    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _meta_cache.get(path)
    if cached is not None and cached[0] == stat.st_mtime:
        return cached[1]
    entry: AdventureEntry | None = None
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
        if isinstance(document, dict) and document.get("kind") == "adventure":
            payload = document.get("payload") or {}
            name = str(payload.get("name") or path.stem)
            entry = AdventureEntry(
                id="",
                name=name,
                description=str(payload.get("description") or ""),
                path=path,
                digest=hashlib.sha256(raw).hexdigest(),
                hooks=_hooks(payload),
                town_name=_town_name(payload),
                dungeon_count=_dungeon_count(payload),
            )
    except OSError, ValueError, TypeError:
        entry = None
    _meta_cache[path] = (stat.st_mtime, entry)
    return entry


def _candidate_files(source: Path) -> list[Path]:
    """Expand one source into candidate adventure files.

    A file stands for itself; a directory contributes its own `adventure.json`,
    every top-level `*.json`, and each child directory's `adventure.json` (the
    osr-forge output layout).
    """
    if source.is_file():
        return [source]
    if not source.is_dir():
        return []
    candidates: list[Path] = []
    own = source / "adventure.json"
    if own.is_file():
        candidates.append(own)
    for child in sorted(source.iterdir()):
        if child.is_file() and child.suffix == ".json" and child != own:
            candidates.append(child)
        elif child.is_dir() and (child / "adventure.json").is_file():
            candidates.append(child / "adventure.json")
    return candidates


def adventures_dir() -> Path:
    """The drop directory scanned for adventure documents.

    `OSR_WEB_ADVENTURES_DIR` **replaces** the repo `adventures/` directory with one
    of your own; the other sources (the bundled document, `OSR_WEB_ADVENTURES`) are
    untouched. Do not confuse it with the plural `OSR_WEB_ADVENTURES`, which
    **appends** a colon-separated list of extra files and directories to the scan and
    leaves the drop directory in place. Pointing this at an empty directory is how a
    test or a screenshot run keeps the local library out of the picker.

    The environment is read on every call, so a test can redirect the scan with
    `monkeypatch.setenv`.

    Returns:
        The override with `~` expanded, or the repo `adventures/` directory when
        `OSR_WEB_ADVENTURES_DIR` is unset or blank.
    """
    override = os.environ.get("OSR_WEB_ADVENTURES_DIR", "").strip()
    return Path(override).expanduser() if override else _ADVENTURES_DIR


def _bundled_path() -> Path:
    """The bundled document's path, `OSR_WEB_ADVENTURE` override applied."""
    return Path(os.environ.get("OSR_WEB_ADVENTURE", _CONTENT_DIR / "adventure.json"))


def _sources() -> list[tuple[Path, bool]]:
    """Every source to scan, each flagged for whether it is the drop directory.

    The flag is what makes an entry deletable, and it travels with the source
    rather than being inferred from the path afterwards: pointing
    `OSR_WEB_ADVENTURE` at a file that happens to sit *inside* the drop
    directory must still leave the default adventure undeletable.
    """
    sources = [(_bundled_path(), False), (adventures_dir(), True)]
    extra = os.environ.get("OSR_WEB_ADVENTURES", "")
    for part in extra.split(":"):
        part = part.strip()
        if part:
            sources.append((Path(part).expanduser(), False))
    return sources


def _drop_item(candidate: Path, directory: Path) -> Path | None:
    """The drop-directory entry that put `candidate` in the library.

    A candidate is either the drop directory's own child (`<dir>/x.json`, which
    stands for itself) or one level down (`<dir>/child/adventure.json`, where the
    child directory — usually a symlink to a forge run — is what was dropped).
    The paths compared are the ones `_candidate_files` built out of `directory`,
    never resolved ones: a symlinked forge run must delete as the link that sits
    in the drop directory, not as the run it points at.

    Returns:
        The path deletion may remove, or None if `candidate` did not come from
        inside `directory` after all.
    """
    parent = candidate.parent
    if parent == directory:
        return candidate
    if parent.parent == directory:
        return parent
    return None


def list_adventures() -> list[AdventureEntry]:
    """Scan every source and return the library, first entry the default."""
    entries: list[AdventureEntry] = []
    seen_digests: set[str] = set()
    seen_paths: set[Path] = set()
    used_ids: set[str] = set()
    directory = adventures_dir()
    for source, droppable in _sources():
        for candidate in _candidate_files(source):
            resolved = candidate.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            entry = _read_entry(resolved)
            if entry is None or entry.digest in seen_digests:
                continue
            seen_digests.add(entry.digest)
            base = _slug(entry.name)
            adventure_id = base
            counter = 2
            while adventure_id in used_ids:
                adventure_id = f"{base}-{counter}"
                counter += 1
            used_ids.add(adventure_id)
            entries.append(
                AdventureEntry(
                    id=adventure_id,
                    name=entry.name,
                    description=entry.description,
                    path=entry.path,
                    digest=entry.digest,
                    hooks=entry.hooks,
                    town_name=entry.town_name,
                    dungeon_count=entry.dungeon_count,
                    drop_item=_drop_item(candidate, directory) if droppable else None,
                )
            )
    return entries


def add_adventure(raw: bytes) -> AdventureEntry:
    """Vet an uploaded adventure document and add it to the library.

    The upload is the one path where content reaches the server's disk from the
    client, so unlike the scan (metadata-only by design) it validates the whole
    document — the envelope via `check_document`, the payload via
    `Adventure.model_validate`, and every cross-reference the payload makes via
    [`validate_adventure`][osrlib.crawl.adventure.validate_adventure] — before
    writing a byte. The bytes written are the bytes received: the file lands in
    [`adventures_dir`][server.library.adventures_dir] exactly as the player's
    copy reads, so the library's dedupe-by-content keeps working against copies
    that arrive by other routes.

    A document whose bytes the library already holds (bundled, dropped, or listed
    via `OSR_WEB_ADVENTURES`) is returned as its existing entry with nothing
    written.

    Args:
        raw: The uploaded file's bytes, exactly as sent.

    Returns:
        The library entry now serving this document.

    Raises:
        ContentValidationError: If the bytes are not a valid stamped adventure
            document. (`SaveVersionError` from a too-new schema stamp propagates
            as itself.)
    """
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise ContentValidationError(f"not valid JSON: {error}") from error
    payload = check_document(document, "adventure")
    try:
        adventure = Adventure.model_validate(payload)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "payload"
        more = error.error_count() - 1
        detail = f"{where}: {first['msg']}"
        if more:
            detail += f" (and {more} more problem{'s' if more > 1 else ''})"
        raise ContentValidationError(f"not a playable adventure — {detail}") from error
    # A model-valid document can still name an item, area, or monster that does
    # not exist. Caught here, it is a 422 on the upload; uncaught, it is a game
    # that loads and quietly never fires what the author wrote.
    validate_adventure(adventure, load_monsters(), load_equipment())
    digest = hashlib.sha256(raw).hexdigest()
    for entry in list_adventures():
        if entry.digest == digest:
            return entry
    directory = adventures_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # The slug is capped because the library id comes from the payload name at
    # scan time, never from the filename — but a name that slugs past the
    # filesystem's ~255-byte limit would slip through `path.exists()` (False on
    # ENAMETOOLONG) and crash the write.
    base = _slug(str(payload.get("name") or "adventure"))[:100]
    path = directory / f"{base}.json"
    counter = 2
    while path.exists():
        # `_` sorts after `.`, so a collision file lands *after* the original in
        # the scan's sorted order and the original keeps its slug id (`-2` in a
        # filename would sort first and steal it).
        path = directory / f"{base}_{counter}.json"
        counter += 1
    # Write-then-rename so a concurrent scan (the GET runs in the threadpool)
    # never reads a half-written file into `_meta_cache`; the `.tmp` name is
    # invisible to the scan, which collects only `*.json`.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)
    for entry in list_adventures():
        if entry.digest == digest:
            return entry
    # Unreachable when the write succeeded: the file just written is in the scan.
    raise ContentValidationError("the uploaded adventure did not appear in the library")


def remove_adventure(entry: AdventureEntry) -> None:
    """Delete the drop-directory item that put `entry` in the library.

    What gets deleted is [`drop_item`][server.library.AdventureEntry] — the file
    or directory sitting *in* the drop directory — never the resolved document
    and never a path outside that directory. The three shapes it can take are
    deleted as what they are: a symlink is unlinked (the forge run it points at
    is left whole), a `.json` file is unlinked, and a dropped directory goes with
    everything in it. Callers must check
    [`removable`][server.library.AdventureEntry.removable] first; a protected
    entry raises rather than falling through to a delete.

    Args:
        entry: The library entry to remove, as returned by the scan.

    Raises:
        ValueError: If the entry has no drop item, or its drop item has wandered
            outside the drop directory since the scan.
        FileNotFoundError: If the drop item is already gone.
    """
    item = entry.drop_item
    if item is None:
        raise ValueError(f"{entry.name!r} is not in the drop directory")
    # The scan built this path out of `adventures_dir()`; re-check it against the
    # directory as it reads *now*, so a delete can never escape one.
    if item.parent != adventures_dir():
        raise ValueError(f"{entry.name!r} does not sit in the drop directory")
    if item.is_symlink() or item.is_file():
        item.unlink()
    elif item.is_dir():
        shutil.rmtree(item)
    else:
        raise FileNotFoundError(f"{entry.name!r} is already gone")
    # The cache is keyed by the resolved document path; drop the dead key rather
    # than trust a recreated file to differ in mtime.
    _meta_cache.pop(entry.path, None)


def adventure_entry(adventure_id: str | None) -> AdventureEntry | None:
    """Return the entry for `adventure_id`, or the default entry when None."""
    entries = list_adventures()
    if not entries:
        return None
    if adventure_id is None:
        return entries[0]
    for entry in entries:
        if entry.id == adventure_id:
            return entry
    return None
