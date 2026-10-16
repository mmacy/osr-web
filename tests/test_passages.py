"""Passages in play: the cell context and a crossing driven through the game routes.

The fixture is a dungeon of two levels joined by a passage pair. On level 1 the party walks from
the entrance at (0, 0) onto (1, 0), whose east side is a passage to level 2's (1, 0). On level 2
the passage back stands on the west side of (1, 0), and stairs up stand on the same cell. The
document goes into the library through the upload path, and the game runs through `create_game`
and `execute_command`, as the browser drives it.
"""

import json

import pytest
from osrlib.crawl.adventure import Adventure, TownSpec
from osrlib.crawl.dungeon import Direction, DungeonSpec, Edge, EdgeKind, LevelSpec, TransitionSpec, WanderingSpec
from osrlib.versioning import stamp_document

from server import library
from server.app import _games, create_game, execute_command
from server.library import add_adventure

DUNGEON = "keep"


def passage_adventure() -> Adventure:
    level_1 = LevelSpec(
        number=1,
        width=3,
        height=1,
        entrance=(0, 0),
        edges={"1,0:west": Edge(kind=EdgeKind.OPEN)},
        transitions=(
            TransitionSpec(
                kind="passage",
                position=(1, 0),
                side=Direction.EAST,
                to_dungeon_id=DUNGEON,
                to_level_number=2,
                to_position=(1, 0),
                to_facing=Direction.EAST,
            ),
        ),
        wandering=WanderingSpec(chance_in_six=0),
    )
    level_2 = LevelSpec(
        number=2,
        width=3,
        height=1,
        edges={"2,0:west": Edge(kind=EdgeKind.OPEN)},
        transitions=(
            TransitionSpec(
                kind="passage",
                position=(1, 0),
                side=Direction.WEST,
                to_dungeon_id=DUNGEON,
                to_level_number=1,
                to_position=(1, 0),
                to_facing=Direction.WEST,
            ),
            TransitionSpec(
                kind="stairs_up",
                position=(1, 0),
                to_dungeon_id=DUNGEON,
                to_level_number=1,
                to_position=(0, 0),
                to_facing=Direction.EAST,
            ),
        ),
        wandering=WanderingSpec(chance_in_six=0),
    )
    return Adventure(
        name="The Passage Keep",
        town=TownSpec(name="Threshold", travel_turns={DUNGEON: 0}),
        dungeons=(DungeonSpec(id=DUNGEON, name="Keep", levels=(level_1, level_2)),),
    )


@pytest.fixture(autouse=True)
def _redirected_dirs(monkeypatch, tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(drop))
    monkeypatch.delenv("OSR_WEB_ADVENTURE", raising=False)
    monkeypatch.delenv("OSR_WEB_ADVENTURES", raising=False)
    monkeypatch.setenv("OSR_WEB_NARRATOR", "")
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


@pytest.fixture
def game_id():
    document = stamp_document("adventure", passage_adventure().model_dump(mode="json"))
    entry = add_adventure(json.dumps(document).encode("utf-8"))
    payload = create_game({"seed": 7, "adventure_id": entry.id})
    yield payload["game_id"]
    _games.pop(payload["game_id"], None)


def command(game_id: str, command_type: str, **body) -> dict:
    return execute_command(game_id, {"command_type": command_type, **body})


def where(payload: dict) -> tuple:
    location = payload["view"]["location"]
    return (location["level_number"], location["position"], location["facing"])


def test_a_cell_with_only_a_passage_offers_no_stairs(game_id):
    assert command(game_id, "enter_dungeon", dungeon_id=DUNGEON)["accepted"] is True
    moved = command(game_id, "move_party", direction="east")
    assert moved["accepted"] is True
    assert where(moved) == (1, [1, 0], "east")
    assert moved["cell"]["transition"] is None


def test_stairs_beside_a_passage_are_still_offered(game_id):
    command(game_id, "enter_dungeon", dungeon_id=DUNGEON)
    command(game_id, "move_party", direction="east")
    crossed = command(game_id, "move_party", direction="east")
    assert crossed["accepted"] is True
    assert where(crossed) == (2, [1, 0], "east")
    assert crossed["cell"]["transition"] == "stairs_up"


def test_a_game_crosses_a_passage_pair_both_ways(game_id):
    command(game_id, "enter_dungeon", dungeon_id=DUNGEON)
    command(game_id, "move_party", direction="east")
    there = command(game_id, "move_party", direction="east")
    assert there["accepted"] is True
    assert where(there) == (2, [1, 0], "east")
    back = command(game_id, "move_party", direction="west")
    assert back["accepted"] is True
    assert where(back) == (1, [1, 0], "west")
    assert back["cell"]["transition"] is None
