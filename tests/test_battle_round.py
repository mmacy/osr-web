"""Unit tests for the battle-round projection (`server/app.py::_current_battle_round`).

Two round counters share the battle screen: the declaration panel and the
roundbar render the player view's `encounter.battle_round`, while the transcript
prints the engine's `battle_round` event as `— Round N —`. osrlib's projection
carries `session.battle.round`, the count of rounds already *resolved*, which is
one behind the round the party is declaring. A surprise round is a resolved
round, so the two numbers drifted apart the moment the party was surprised —
both visible at once, one of them wrong.

These tests pin the payload's number to the transcript's on all three battle
openings: no surprise, the party surprised (the monsters take a free round), and
the monsters surprised (the party's free round is round one, monsters holding).
Surprise is a die roll, so each opening is found by scanning seeds the way
osrlib's own encounter tests do.
"""

import pytest
from osrlib.core.events import Visibility
from osrlib.core.tables import ReactionResult
from osrlib.crawl import encounter as encounter_module
from osrlib.crawl.commands import EnterDungeon

from server.app import Game, _current_battle_round, _games, _state, execute_command
from server.content import new_session

_GAME_ID = "test-battle-round"


def _dungeon_id(session) -> str:
    """The first dungeon a party can actually enter."""
    return next(
        dungeon.id
        for dungeon in session.adventure.dungeons
        if any(level.entrance is not None for level in dungeon.levels)
    )


def _meet_goblins(session, stance, **awareness) -> list:
    """Walk into the dungeon and start a spawned goblin encounter with a pinned stance."""
    session.execute(EnterDungeon(dungeon_id=_dungeon_id(session)))
    instances = session.spawn("goblin", 2)
    return encounter_module.start_encounter(
        session,
        groups=[("goblin", instances)],
        kind="spawned",
        distance_feet=60,
        pinned_stance=stance,
        **awareness,
    )


def _surprised(events, side: str) -> bool:
    """Did `side` roll surprised in this encounter's opening events?"""
    event = next((e for e in events if getattr(e, "side", "") == side), None)
    return bool(getattr(event, "surprised", False))


def _register(session) -> Game:
    """Register a game the way `create_game` would, with the LLM layer off.

    Narration only adds color beside the deterministic transcript, and it would
    reach for a live provider; the round header under test is the narrator's.
    """
    game = Game(session)
    game.narration = None
    _games[_GAME_ID] = game
    return game


def _battle_party_surprised() -> Game:
    """A battle the monsters opened with their free round: one round already resolved."""
    for seed in range(60):
        session = new_session(seed=seed)
        events = _meet_goblins(session, ReactionResult.HOSTILE, party_aware=False, monsters_aware=True)
        if _surprised(events, "party") and session.battle is not None:
            assert session.battle.round == 1, "the surprise round is a resolved round"
            return _register(session)
    raise AssertionError("no seed in sixty surprised the party")


def _battle_monsters_surprised() -> Game:
    """A battle opened on the party's free round: nothing resolved, monsters holding."""
    for seed in range(60):
        session = new_session(seed=seed)
        events = _meet_goblins(session, ReactionResult.ATTACKS, party_aware=True, monsters_aware=False)
        if _surprised(events, "monsters") and session.battle is not None:
            assert session.battle.monsters_hold_rounds == 1
            return _register(session)
    raise AssertionError("no seed in sixty surprised the monsters")


def _battle_no_surprise() -> Game:
    """A battle nobody was surprised in: the next round is the first round."""
    for seed in range(60):
        session = new_session(seed=seed)
        events = _meet_goblins(session, ReactionResult.ATTACKS, party_aware=True, monsters_aware=True)
        if session.battle is not None and not any(_surprised(events, side) for side in ("party", "monsters")):
            assert session.battle.round == 0
            return _register(session)
    raise AssertionError("no seed in sixty opened battle without surprise")


def _panel_round(game: Game) -> int:
    """The number the declaration panel and the roundbar show (`static/app.js`).

    Read raw, without the client's `battle_round || 1` fallback: that fallback is
    what hid the off-by-one whenever no round had resolved yet, so the payload
    itself has to carry the right number.
    """
    return _state(game)["view"]["encounter"]["battle_round"]


def _resolve(game: Game) -> str:
    """Resolve one round through the command endpoint; return the transcript's header."""
    declarations = [{"character_id": member.id, "action": "hold"} for member in game.session.party.living_members()]
    result = execute_command(
        _GAME_ID,
        {"command_type": "resolve_battle_round", "declarations": declarations},
    )
    assert result["accepted"], result["rejections"]
    return next(entry["text"] for entry in result["log"] if entry["text"].startswith("— Round"))


class _BattleCase:
    """Shared assertions: whatever the UI shows is what the transcript then prints."""

    def teardown_method(self):
        _games.pop(_GAME_ID, None)

    def test_the_ui_number_is_the_number_the_transcript_prints(self):
        game = self.open_battle()
        shown = _panel_round(game)
        assert _resolve(game) == f"— Round {shown} —"

    def test_the_two_stay_in_step_round_after_round(self):
        game = self.open_battle()
        for _ in range(3):
            if game.session.battle is None:  # the goblins fell or the party did
                break
            shown = _panel_round(game)
            assert _resolve(game) == f"— Round {shown} —"


class TestPartySurprised(_BattleCase):
    open_battle = staticmethod(_battle_party_surprised)

    def test_the_surprise_round_is_counted(self):
        # The monsters' free round happened, so the party declares round two —
        # which is exactly what the transcript already printed.
        game = self.open_battle()
        assert game.session.battle.round == 1
        assert _panel_round(game) == 2


class TestMonstersSurprised(_BattleCase):
    open_battle = staticmethod(_battle_monsters_surprised)

    def test_the_partys_free_round_is_round_one(self):
        # The party's free round is not resolved ahead of time: the monsters
        # simply hold through round one, so round one is still to come.
        game = self.open_battle()
        assert game.session.battle.round == 0
        assert _panel_round(game) == 1


class TestNoSurprise(_BattleCase):
    open_battle = staticmethod(_battle_no_surprise)

    def test_first_round_is_round_one(self):
        game = self.open_battle()
        assert _panel_round(game) == 1


class TestProjectionLeavesTheEngineAlone:
    def teardown_method(self):
        _games.pop(_GAME_ID, None)

    def test_the_engines_own_view_still_counts_resolved_rounds(self):
        # The rebase belongs to the payload; osrlib's projection keeps its
        # meaning so a reader comparing the two is not misled about the engine.
        game = _battle_party_surprised()
        assert _panel_round(game) == 2
        view = game.session.view(Visibility.PLAYER)
        assert view.encounter.battle_round == 1


class TestCurrentBattleRound:
    def test_a_battle_round_advances_to_the_round_being_declared(self):
        view = {"encounter": {"in_battle": True, "battle_round": 0}}
        assert _current_battle_round(view)["encounter"]["battle_round"] == 1

    def test_an_encounter_out_of_battle_is_untouched(self):
        view = {"encounter": {"in_battle": False, "battle_round": None}}
        assert _current_battle_round(view)["encounter"]["battle_round"] is None

    def test_no_encounter_is_untouched(self):
        assert _current_battle_round({"encounter": None}) == {"encounter": None}


@pytest.mark.parametrize("resolved", [0, 1, 5])
def test_the_ui_round_is_always_one_past_the_resolved_count(resolved):
    view = {"encounter": {"in_battle": True, "battle_round": resolved}}
    assert _current_battle_round(view)["encounter"]["battle_round"] == resolved + 1
