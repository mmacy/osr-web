"""The scripted opening route, proven by CI rather than by a person walking it (dev-loop phase 5).

`AGENTS.md` promises a reproducible first fight: seed 42, `enter_dungeon` into the Cold Vein, three
`move_party north`, and four kobolds attack. The assertions here are structural: where the party
stands, which mode the session is in, and who is in the encounter. Nothing pins a die result, so
an engine tuning that changes damage or initiative changes nothing here.
"""

import pytest

from server import library
from server.app import _games, create_game, execute_command

DUNGEON = "cold-vein"
ENTRANCE = [2, 15]
DRESSING_FLOOR = [2, 12]


@pytest.fixture(autouse=True)
def _redirected_dirs(monkeypatch, tmp_path):
    empty_drop = tmp_path / "drop"
    empty_drop.mkdir()
    monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(empty_drop))
    monkeypatch.delenv("OSR_WEB_ADVENTURE", raising=False)
    monkeypatch.delenv("OSR_WEB_ADVENTURES", raising=False)
    monkeypatch.setenv("OSR_WEB_NARRATOR", "")
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


@pytest.fixture
def game_id():
    payload = create_game({"seed": 42})
    yield payload["game_id"]
    _games.pop(payload["game_id"], None)


def command(game_id: str, command_type: str, **body) -> dict:
    return execute_command(game_id, {"command_type": command_type, **body})


def test_the_opening_route_reaches_the_pinned_first_fight(game_id):
    entered = command(game_id, "enter_dungeon", dungeon_id=DUNGEON)
    assert entered["accepted"] is True
    location = entered["view"]["location"]
    assert location["kind"] == "dungeon"
    assert location["dungeon_id"] == DUNGEON
    assert location["position"] == ENTRANCE
    assert location["facing"] == "north"
    assert entered["view"]["mode"] == "exploring"

    moves = [command(game_id, "move_party", direction="north") for _ in range(3)]
    assert [move["accepted"] for move in moves] == [True, True, True]
    assert [move["view"]["mode"] for move in moves[:2]] == ["exploring", "exploring"], "no wandering check on the way"

    third = moves[2]["view"]
    assert third["mode"] == "battle"
    assert third["location"]["position"] == DRESSING_FLOOR

    encounter = third["encounter"]
    assert encounter["in_battle"] is True
    assert encounter["stance"] == "attacks"
    assert [(group["label"], group["count"]) for group in encounter["groups"]] == [("Kobold", 4)]
