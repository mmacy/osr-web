"""Unit tests for the save routes' app-owned logic (`server/app.py`).

Saves are legible objects: summaries carry a name (custom or
derived), a place, and a turn beside the roster, and saves can be renamed and
deleted. These tests pin that surface — including the wire contract, in test
form: a summary exposes exactly the allowed keys and never the document.

Everything runs against redirected directories (`OSR_WEB_SAVES_DIR`,
`OSR_WEB_ADVENTURES_DIR`) per `tests/test_paths.py`, and endpoints are called
directly (no HTTP layer) in the repo's style.
"""

import json
import os

import pytest
from fastapi import HTTPException

from server import library
from server.app import (
    _games,
    create_game,
    delete_all_saves,
    delete_save,
    execute_command,
    rename_save,
    save,
    saves,
)

# The whole wire contract for one save summary: every key a player saw on
# screen when they saved, and nothing else — the document never crosses.
_SUMMARY_KEYS = {
    "save_id",
    "name",
    "adventure_name",
    "saved_at",
    "turn",
    "place",
    "party",
}
_MEMBER_KEYS = {"name", "class_id", "level", "dead"}


@pytest.fixture(autouse=True)
def _redirected_dirs(monkeypatch, tmp_path):
    """Keep every test off the repo's real saves and adventure library."""
    empty_drop = tmp_path / "drop"
    empty_drop.mkdir()
    monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(empty_drop))
    monkeypatch.delenv("OSR_WEB_ADVENTURE", raising=False)
    monkeypatch.delenv("OSR_WEB_ADVENTURES", raising=False)
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


@pytest.fixture
def game_id():
    payload = create_game({"seed": 42})
    yield payload["game_id"]
    _games.pop(payload["game_id"], None)


class TestSaveSummaries:
    def test_summary_exposes_exactly_the_allowed_keys(self, game_id):
        save(game_id)
        summary = saves()["saves"][0]
        assert set(summary.keys()) == _SUMMARY_KEYS
        for member in summary["party"]:
            assert set(member.keys()) == _MEMBER_KEYS

    def test_unnamed_save_gets_a_derived_name_with_place_and_turn(self, game_id):
        result = save(game_id)
        summary = saves()["saves"][0]
        assert summary["name"] == "The Cold Vein — turn 0"
        assert result["name"] == summary["name"]
        assert summary["turn"] == 0
        assert summary["place"] == "Cinderhope"

    def test_dungeon_save_names_the_dungeon_and_level(self, game_id):
        execute_command(game_id, {"command_type": "enter_dungeon", "dungeon_id": "cold-vein"})
        save(game_id)
        summary = saves()["saves"][0]
        assert summary["place"] == "The Cold Vein, level 1"
        assert summary["name"].startswith("The Cold Vein — level 1, turn ")

    def test_custom_name_is_stored_and_listed(self, game_id):
        result = save(game_id, {"name": "  Before the vault  "})
        assert result["name"] == "Before the vault"
        assert saves()["saves"][0]["name"] == "Before the vault"

    def test_blank_name_falls_back_to_derived(self, game_id):
        result = save(game_id, {"name": "   "})
        assert result["name"] == "The Cold Vein — turn 0"

    def test_overlong_name_is_422(self, game_id):
        with pytest.raises(HTTPException) as excinfo:
            save(game_id, {"name": "x" * 61})
        assert excinfo.value.status_code == 422

    def test_non_string_name_is_422(self, game_id):
        with pytest.raises(HTTPException) as excinfo:
            save(game_id, {"name": 7})
        assert excinfo.value.status_code == 422


class TestRenameSave:
    def test_rename_persists_and_survives_the_engine_contract(self, game_id):
        save_id = save(game_id)["save_id"]
        result = rename_save(save_id, {"name": "The vault run"})
        assert result == {"save_id": save_id, "name": "The vault run"}
        assert saves()["saves"][0]["name"] == "The vault run"
        # The name rides as an extra envelope key old engines ignore; the
        # renamed file must still restore.
        restored = create_game({"save_id": save_id})
        try:
            assert len(restored["view"]["party"]) == 6
        finally:
            _games.pop(restored["game_id"], None)

    def test_rename_keeps_the_list_order(self, game_id, tmp_path):
        first = save(game_id)["save_id"]
        second = save(game_id)["save_id"]
        # Age the first save so the ordering is unambiguous, then rename it:
        # a rename is not a save, so it must not jump to the top.
        path = tmp_path / "saves" / f"{first}.json"
        old = path.stat().st_mtime - 3600
        os.utime(path, (old, old))
        rename_save(first, {"name": "Older, renamed"})
        assert [s["save_id"] for s in saves()["saves"]] == [second, first]

    def test_unknown_save_is_404(self):
        with pytest.raises(HTTPException) as excinfo:
            rename_save("feedbeef", {"name": "Nothing"})
        assert excinfo.value.status_code == 404

    def test_empty_name_is_422(self, game_id):
        save_id = save(game_id)["save_id"]
        with pytest.raises(HTTPException) as excinfo:
            rename_save(save_id, {"name": "   "})
        assert excinfo.value.status_code == 422


class TestDeleteSave:
    def test_delete_removes_the_file_and_the_listing(self, game_id, tmp_path):
        save_id = save(game_id)["save_id"]
        result = delete_save(save_id)
        assert result == {"save_id": save_id, "deleted": True}
        assert not (tmp_path / "saves" / f"{save_id}.json").exists()
        assert saves() == {"saves": []}

    def test_unknown_save_is_404(self):
        with pytest.raises(HTTPException) as excinfo:
            delete_save("feedbeef")
        assert excinfo.value.status_code == 404

    def test_path_shaped_ids_are_fenced_off(self, game_id, tmp_path):
        # An id is a filename stem, never a path: nothing outside the saves
        # directory can be named, let alone deleted.
        save(game_id)
        outside = tmp_path / "precious.json"
        outside.write_text("{}", encoding="utf-8")
        for hostile in ("..", "../precious", "a/b", "a\\b", ".hidden"):
            with pytest.raises(HTTPException) as excinfo:
                delete_save(hostile)
            assert excinfo.value.status_code == 404
        assert outside.exists()


class TestDeleteAllSaves:
    def test_every_save_goes_and_the_listing_comes_back_empty(self, game_id):
        first = save(game_id)["save_id"]
        second = save(game_id)["save_id"]
        assert first != second

        payload = delete_all_saves()

        assert payload["deleted"] == 2
        assert payload["saves"] == []
        assert saves() == {"saves": []}

    def test_an_empty_directory_deletes_nothing(self):
        assert delete_all_saves() == {"deleted": 0, "saves": []}

    def test_only_the_files_the_list_shows_are_touched(self, game_id, tmp_path):
        save(game_id)
        directory = tmp_path / "saves"
        keeper = directory / "notes.txt"
        keeper.write_text("not a save", encoding="utf-8")

        payload = delete_all_saves()

        assert payload["deleted"] == 1
        assert keeper.is_file()
        assert list(directory.iterdir()) == [keeper]


class TestUnreadableSaves:
    def test_a_broken_file_does_not_break_the_listing(self, game_id, tmp_path):
        save_id = save(game_id)["save_id"]
        directory = tmp_path / "saves"
        (directory / "broken.json").write_text("not json", encoding="utf-8")
        (directory / "hollow.json").write_text(json.dumps({}), encoding="utf-8")
        listed = [s["save_id"] for s in saves()["saves"]]
        assert listed == [save_id]
