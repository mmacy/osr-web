"""The wire contract as an observer (dev-loop phase 5).

`AGENTS.md` says only the player projection crosses the wire, and that a new response field is
added only after asking whether it leaks referee state. Until now nothing checked. These tests pin
the key set of every payload the game routes return, so adding a field means editing the frozen
sets below on purpose, in the same change, with the wire-contract question answered in review.

The sets pin this app's payload envelope, not osrlib's `PlayerView`: the `view` block is the
engine's own player projection and is the engine's to grow.
"""

import pytest

from server import library
from server.app import _games, _state, create_game, execute_command

STATE_KEYS = frozenset(
    {
        "view",
        "cell",
        "sheets",
        "spellbook",
        "spell_books",
        "learnable",
        "temple_services",
        "hooks",
        "dungeons",
        "narration",
    }
)
"""The state block every game route returns: the projection plus the player-safe extras."""

CREATE_GAME_KEYS = STATE_KEYS | {"game_id", "log", "engine_version", "schema_version"}
COMMAND_KEYS = STATE_KEYS | {"accepted", "rejections", "log"}

REFEREE_KEYS = frozenset({"seed", "master_seed", "rng", "rng_streams", "referee"})
"""Names that belong to the referee's side of the table and must never appear anywhere in a payload."""


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
def opened():
    payload = create_game({"seed": 42})
    yield payload
    _games.pop(payload["game_id"], None)


def _keys_in(tree, found: set[str]) -> set[str]:
    if isinstance(tree, dict):
        for key, value in tree.items():
            found.add(key)
            _keys_in(value, found)
    elif isinstance(tree, list):
        for value in tree:
            _keys_in(value, found)
    return found


def test_state_block_is_exactly_the_pinned_key_set(opened):
    assert set(_state(_games[opened["game_id"]])) == STATE_KEYS


def test_create_game_payload_is_exactly_the_pinned_key_set(opened):
    assert set(opened) == CREATE_GAME_KEYS


def test_command_payload_is_exactly_the_pinned_key_set(opened):
    payload = execute_command(opened["game_id"], {"command_type": "enter_dungeon", "dungeon_id": "cold-vein"})
    assert set(payload) == COMMAND_KEYS


def test_no_referee_name_appears_anywhere_in_a_payload(opened):
    payload = execute_command(opened["game_id"], {"command_type": "enter_dungeon", "dungeon_id": "cold-vein"})
    assert _keys_in(opened, set()) & REFEREE_KEYS == set()
    assert _keys_in(payload, set()) & REFEREE_KEYS == set()
