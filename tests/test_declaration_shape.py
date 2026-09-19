"""The battle declaration panel's wire contract (`encounter.declarers` and friends).

A battle round is all-or-nothing: one illegal declaration rejects the whole
party's turn, listing every rejection and advancing nothing. So the declaration
panel in `static/app.js` must offer only declarations the engine will accept,
and everything it needs in order to know that comes from four id tuples on the
player view's encounter — `declarers`, `front_rank`, `immobile`, and `reloading`
(osrlib 1.7.0). The client reads each defensively (`enc.front_rank || []`),
which means a projection that stopped including them would not raise: every melee
attack would quietly read as illegal and the panel would offer nothing but Hold.
These tests are what make that failure loud.

`front_rank` is the load-bearing one. The rank is as wide as the party's own
fighting space allows — two in a ten-foot passage, more in a room, measured by the
engine off the level's geometry — so "the first two living members" is wrong in a
room, and wrong in both directions: it denies a rear member the swing the engine
would allow, and it offers a back-rank member one the engine rejects.
"""

import pytest
from osrlib.core.effects import ActiveCondition, Condition
from osrlib.crawl.commands import EnterDungeon, MoveParty
from osrlib.crawl.dungeon import Direction

from server.app import Game, _games, _state
from server.content import new_session

_GAME_ID = "test-declaration-shape"


@pytest.fixture
def battle() -> Game:
    """The bundled adventure's scripted first fight: four kobolds, in a keyed area.

    `enter_dungeon` lands the party on the entrance at (2,15) facing north and
    three moves north reach (2,12), where the encounter is pinned to attack — so
    the session is in `battle` the instant the third move returns.
    """
    session = new_session(seed=42)
    session.execute(EnterDungeon(dungeon_id="cold-vein"))
    for _ in range(3):
        session.execute(MoveParty(direction=Direction.NORTH))
    assert session.battle is not None, "the scripted route no longer opens a battle"
    game = Game(session)
    game.narration = None
    _games[_GAME_ID] = game
    yield game
    _games.pop(_GAME_ID, None)


def _encounter(game: Game) -> dict:
    return _state(game)["view"]["encounter"]


def _living_ids(game: Game) -> list[str]:
    return [member.id for member in game.session.party.living_members()]


class TestTheDeclarationShapeCrossesTheWire:
    def test_all_four_tuples_reach_the_client(self, battle):
        encounter = _encounter(battle)
        for field in ("declarers", "front_rank", "immobile", "reloading"):
            assert field in encounter, f"the declaration panel reads encounter.{field}"

    def test_the_front_rank_is_the_room_and_not_the_first_two(self, battle):
        # The dressing floor is four cells by three — thirty feet of frontage,
        # six abreast, which is the whole party. The width belongs to the space,
        # so this is a fact about the bundled document's geometry as much as
        # about the engine, which is exactly why no client may guess at the width.
        assert _encounter(battle)["front_rank"] == _living_ids(battle)

    def test_every_able_member_declares_and_the_helpless_do_not(self, battle):
        assert _encounter(battle)["declarers"] == _living_ids(battle)
        asleep = battle.session.party.members[3]
        asleep.conditions = (ActiveCondition(condition=Condition.ASLEEP, effect_id="effect-9999"),)
        declarers = _encounter(battle)["declarers"]
        assert asleep.id not in declarers
        assert declarers == [member for member in _living_ids(battle) if member != asleep.id]

    def test_nobody_is_immobile_or_reloading_in_a_plain_fight(self, battle):
        # Both tuples are empty here and that is the point: the client disables a
        # control on membership, so a projection that named everyone would
        # disable the whole panel just as surely as one that named no one.
        encounter = _encounter(battle)
        assert encounter["immobile"] == []
        assert encounter["reloading"] == []


class TestAnIdentifiedArmSaysHowItReaches:
    """A magic dagger is a thrown weapon; the client cannot classify it without the facts."""

    def test_an_identified_arm_reports_qualities_and_ranges(self, battle):
        from osrlib.core.items import MagicItemInstance

        member = battle.session.party.members[0]
        member.inventory.wielded.append(
            MagicItemInstance(
                instance_id="magic-item-0001",
                template_id="dagger_plus_1",
                identified=True,
            )
        )
        wielded = _state(battle)["view"]["party"][0]["inventory"]["wielded"][-1]
        assert wielded["instance_id"] == "magic-item-0001"
        assert set(wielded["qualities"]) == {"melee", "missile"}
        assert wielded["missile_ranges"]["long"]["max_feet"] == 30

    def test_an_unidentified_arm_reports_its_base_weapon_facts(self, battle):
        # The masked entry names the base ("a dagger with a faint aura"), so it carries the
        # dagger's reach too; the enchantment, its bonus, and any curse stay hidden.
        from osrlib.core.items import MagicItemInstance

        member = battle.session.party.members[0]
        member.inventory.wielded.append(MagicItemInstance(instance_id="magic-item-0001", template_id="dagger_plus_1"))
        wielded = _state(battle)["view"]["party"][0]["inventory"]["wielded"][-1]
        assert wielded["identified"] is False
        assert set(wielded["qualities"]) == {"melee", "missile"}
        assert wielded["missile_ranges"]["long"]["max_feet"] == 30
        assert "template_id" not in wielded
