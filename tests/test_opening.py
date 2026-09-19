"""Unit tests for the opening flow's server side.

Three properties carry the rebuilt front door:

1. A new game opens at turn 0 with spells prepared and an empty transcript —
   the creation-time rest/prepare dance must not leak into play.
2. The adventure library carries the player-safe facts the browsing pane needs
   (hooks, town name, enterable-dungeon count), so reading costs no session.
3. The premade party can be staged on a party builder — rolled, rerolled,
   renamed — with no `GameSession` behind it, and a session can be abandoned
   with `DELETE /api/games/{id}`.

Endpoints are called directly (no HTTP layer) in the repo's style, with the
library and saves directories redirected per `tests/test_paths.py`.
"""

import pytest
from fastapi import HTTPException

from server import library
from server.app import (
    _MAX_GAMES,
    _builders,
    _games,
    _remember,
    _touch,
    adventures,
    create_game,
    create_party_builder,
    delete_game,
    get_game,
    get_party_builder,
    party_builder_step,
)

_PREMADE_CLASSES = ["fighter", "dwarf", "cleric", "thief", "elf", "magic_user"]


@pytest.fixture(autouse=True)
def _redirected_dirs(monkeypatch, tmp_path):
    empty_drop = tmp_path / "drop"
    empty_drop.mkdir()
    monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(empty_drop))
    monkeypatch.delenv("OSR_WEB_ADVENTURE", raising=False)
    monkeypatch.delenv("OSR_WEB_ADVENTURES", raising=False)
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


def _new_game(body):
    payload = create_game(body)
    _games.pop(payload["game_id"], None)
    return payload


class TestTurnZero:
    def test_a_new_game_opens_at_turn_zero_with_an_empty_transcript(self):
        payload = _new_game({"seed": 42})
        assert payload["view"]["clock_rounds"] == 0
        assert payload["log"] == []

    def test_casters_arrive_with_spells_prepared(self):
        payload = _new_game({"seed": 42})
        prepared = {
            member["class_id"]: [copy["spell_id"] for copy in member["memorized_spells"]]
            for member in payload["view"]["party"]
            if member["memorized_spells"]
        }
        assert prepared == {"elf": ["magic_missile"], "magic_user": ["sleep"]}

    def test_a_builder_party_also_opens_at_turn_zero(self):
        builder = create_party_builder({"premade": True})
        payload = _new_game({"party_builder_id": builder["builder_id"]})
        assert payload["view"]["clock_rounds"] == 0
        assert payload["log"] == []
        assert any(m["memorized_spells"] for m in payload["view"]["party"])


class TestAdventureLibraryDetail:
    def test_entries_carry_the_browsing_pane_facts(self):
        entry = adventures()["adventures"][0]
        assert set(entry.keys()) == {
            "id",
            "name",
            "description",
            "hooks",
            "town_name",
            "dungeon_count",
            # Where the entry sits on the server's disk, never a path: whether
            # the delete button appears, and what it would remove.
            "removable",
            "source_kind",
        }
        assert entry["name"] == "The Cold Vein"
        assert entry["town_name"] == "Cinderhope"
        assert entry["dungeon_count"] == 1
        assert len(entry["hooks"]) == 4
        assert all(isinstance(hook, str) for hook in entry["hooks"])


class TestPremadeStaging:
    def teardown_method(self):
        _builders.clear()

    def test_premade_fill_rolls_the_classic_six_with_no_session(self):
        before = dict(_games)
        payload = create_party_builder({"premade": True})
        assert [m["class_id"] for m in payload["members"]] == _PREMADE_CLASSES
        assert payload["draft"] is None
        assert dict(_games) == before  # staging a party creates no game

    def test_the_same_seed_stages_the_same_party(self):
        def strip(members):
            return [{key: m[key] for key in ("name", "class_id", "max_hp", "scores", "gold_gp")} for m in members]

        first = create_party_builder({"premade": True, "seed": 7})
        second = create_party_builder({"premade": True, "seed": 7})
        assert strip(first["members"]) == strip(second["members"])

    def test_reroll_rerolls_under_new_house_rules(self):
        payload = create_party_builder({"premade": True})
        rerolled = party_builder_step(payload["builder_id"], {"action": "premade", "max_hp": True})
        assert rerolled["accepted"] is True
        assert rerolled["house_rules"]["max_hp"] is True
        assert len(rerolled["members"]) == 6

    def test_rename_sticks_and_validates(self):
        payload = create_party_builder({"premade": True})
        builder_id = payload["builder_id"]
        renamed = party_builder_step(builder_id, {"action": "rename", "index": 0, "name": "Aldric"})
        assert renamed["accepted"] is True
        assert renamed["members"][0]["name"] == "Aldric"
        assert get_party_builder(builder_id)["members"][0]["name"] == "Aldric"
        for bad in (
            {"action": "rename", "index": 0, "name": "  "},
            {"action": "rename", "index": 0, "name": "x" * 41},
            {"action": "rename", "index": 99, "name": "Nobody"},
        ):
            assert party_builder_step(builder_id, bad)["accepted"] is False

    def test_a_draft_in_progress_blocks_the_fill(self):
        payload = create_party_builder(None)
        builder_id = payload["builder_id"]
        party_builder_step(builder_id, {"action": "roll_abilities"})
        blocked = party_builder_step(builder_id, {"action": "premade"})
        assert blocked["accepted"] is False
        assert blocked["rejections"][0]["code"] == "creation.draft.in_progress"

    def test_beginning_the_adventure_consumes_the_builder(self):
        builder_id = create_party_builder({"premade": True})["builder_id"]
        payload = create_game({"party_builder_id": builder_id})
        _games.pop(payload["game_id"], None)
        with pytest.raises(HTTPException) as excinfo:
            get_party_builder(builder_id)
        assert excinfo.value.status_code == 404


class TestDeleteGame:
    def test_delete_frees_the_session(self):
        payload = create_game({"seed": 42})
        game_id = payload["game_id"]
        assert delete_game(game_id) == {"game_id": game_id, "deleted": True}
        with pytest.raises(HTTPException) as excinfo:
            get_game(game_id)
        assert excinfo.value.status_code == 404

    def test_deleting_twice_is_404(self):
        game_id = create_game({"seed": 42})["game_id"]
        delete_game(game_id)
        with pytest.raises(HTTPException) as excinfo:
            delete_game(game_id)
        assert excinfo.value.status_code == 404


class TestSessionHygiene:
    def test_the_store_evicts_least_recently_used_past_the_cap(self):
        # The LRU is generic store hygiene; exercised here with stand-ins so
        # the test doesn't build _MAX_GAMES real sessions.
        store = {}
        for n in range(_MAX_GAMES):
            _remember(store, f"game-{n}", object(), _MAX_GAMES)
        _touch(store, "game-0")  # game-0 becomes most recently used
        _remember(store, "one-too-many", object(), _MAX_GAMES)
        assert "game-0" in store
        assert "game-1" not in store  # the stalest went, not the touched one
        assert len(store) == _MAX_GAMES
