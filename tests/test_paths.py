"""Unit tests for the directory overrides that redirect the server's disk scans.

`OSR_WEB_ADVENTURES_DIR` replaces the adventure drop directory
(`server/library.py`) and `OSR_WEB_SAVES_DIR` replaces the saves directory
(`server/app.py`). Both are read on every call, so these tests redirect them with
`monkeypatch.setenv` and a `tmp_path`. With neither set, every path must behave
exactly as it did before the overrides existed — that is the contract these tests
are really guarding, alongside the property a screenshot run depends on: an empty
drop directory leaves nothing but the bundled document in the picker.

The library's `_meta_cache` is module-level and keyed by path and mtime, so it is
cleared around every test; a temp file rewritten inside one mtime tick would
otherwise read back as its predecessor.
"""

import json
from pathlib import Path

import pytest

from server import app, library
from server.app import _games, _saves_dir, create_game, save, saves
from server.library import adventures_dir, list_adventures

_BUNDLED_DOC = library._CONTENT_DIR / "adventure.json"


def _write_adventure(directory: Path, name: str) -> Path:
    """Write a minimal stamped adventure document into `directory`.

    The library reads metadata only (`kind`, `payload.name`, `payload.description`)
    and never validates the document, so a stub is enough to prove a source is
    scanned.

    Args:
        directory: Directory to write `adventure.json` into.
        name: Adventure name, which becomes the entry's display name and slug id.

    Returns:
        The path written.
    """
    path = directory / "adventure.json"
    path.write_text(
        json.dumps(
            {
                "kind": "adventure",
                "payload": {"name": name, "description": f"{name}, for testing."},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _clean_scan(monkeypatch):
    """Run every test with no overrides set and no stale library metadata."""
    for name in (
        "OSR_WEB_ADVENTURE",
        "OSR_WEB_ADVENTURES",
        "OSR_WEB_ADVENTURES_DIR",
        "OSR_WEB_SAVES_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


class TestAdventuresDirOverride:
    def test_empty_override_leaves_only_the_bundled_document(self, monkeypatch, tmp_path):
        # The property a screenshot run depends on: with the drop directory
        # pointed somewhere empty, the picker holds the bundled document alone —
        # no local forge run can leak into a published image.
        monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(tmp_path))
        entries = list_adventures()
        assert len(entries) == 1
        assert entries[0].path == _BUNDLED_DOC.resolve()

    def test_override_directory_is_scanned(self, monkeypatch, tmp_path):
        _write_adventure(tmp_path, "Tomb of the Test Fixture")
        monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(tmp_path))
        entries = list_adventures()
        assert [entry.name for entry in entries[1:]] == ["Tomb of the Test Fixture"]
        assert entries[1].id == "tomb-of-the-test-fixture"
        assert entries[1].description == "Tomb of the Test Fixture, for testing."

    def test_override_replaces_the_repo_drop_directory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(tmp_path))
        assert adventures_dir() == tmp_path
        sources = library._sources()
        assert (tmp_path, True) in sources
        assert library._ADVENTURES_DIR not in [source for source, _ in sources]

    def test_unset_scans_the_repo_drop_directory(self):
        # The flag beside each source is what deletion keys off: only the drop
        # directory carries True, so only its own items can ever be removed.
        assert adventures_dir() == library._ADVENTURES_DIR
        assert library._sources() == [
            (_BUNDLED_DOC, False),
            (library._ADVENTURES_DIR, True),
        ]

    def test_blank_override_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", "   ")
        assert adventures_dir() == library._ADVENTURES_DIR

    def test_override_expands_a_tilde(self, monkeypatch):
        monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", "~/forge-runs")
        assert adventures_dir() == Path.home() / "forge-runs"

    def test_the_plural_variable_still_appends_beside_the_override(self, monkeypatch, tmp_path):
        # The two knobs do different jobs: `_DIR` replaces the drop directory,
        # the plural appends extra sources.
        drop = tmp_path / "drop"
        drop.mkdir()
        extra = tmp_path / "extra"
        extra.mkdir()
        _write_adventure(drop, "Halls of the Replaced Drop")
        _write_adventure(extra, "Caverns of the Appended Path")
        monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(drop))
        monkeypatch.setenv("OSR_WEB_ADVENTURES", str(extra))
        names = [entry.name for entry in list_adventures()]
        assert names[1:] == [
            "Halls of the Replaced Drop",
            "Caverns of the Appended Path",
        ]


class TestSavesDirOverride:
    def test_empty_override_lists_no_saves(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path))
        assert saves() == {"saves": []}

    def test_missing_override_directory_lists_no_saves(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path / "never-created"))
        assert saves() == {"saves": []}

    def test_write_list_and_restore_all_use_the_override(self, monkeypatch, tmp_path):
        # The split-brain guard: saving, listing, and restoring must agree on one
        # directory, and the repo's own saves must stay out of the list.
        directory = tmp_path / "saves"
        empty_drop = tmp_path / "drop"
        empty_drop.mkdir()
        monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(directory))
        monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(empty_drop))
        payload = create_game({"seed": 42})
        try:
            save_id = save(payload["game_id"])["save_id"]
        finally:
            _games.pop(payload["game_id"], None)
        assert (directory / f"{save_id}.json").is_file()  # created on the write path
        assert not (app._SAVES_DIR / f"{save_id}.json").exists()
        summaries = saves()["saves"]
        assert [summary["save_id"] for summary in summaries] == [save_id]
        assert len(summaries[0]["party"]) == 6
        restored = create_game({"save_id": save_id})
        try:
            assert len(restored["view"]["party"]) == 6
        finally:
            _games.pop(restored["game_id"], None)

    def test_unset_uses_the_repo_saves_directory(self):
        assert _saves_dir() == app._SAVES_DIR

    def test_blank_override_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("OSR_WEB_SAVES_DIR", "   ")
        assert _saves_dir() == app._SAVES_DIR

    def test_override_expands_a_tilde(self, monkeypatch):
        monkeypatch.setenv("OSR_WEB_SAVES_DIR", "~/tales")
        assert _saves_dir() == Path.home() / "tales"
