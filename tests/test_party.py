"""Unit tests for pregenerated-party creation and veteran import (`server/content.py`).

`server/content.py` drives osrlib's stepwise creation functions itself so the
optional house rules (4d6-drop-lowest ability scores, maximum first-level hit
points) bend the premade six the same way they bend the wizard's drafts. Draws
are seeded, so same-seed assertions are deterministic.

Import is the other half: a party carried out of one save into a new adventure
keeps its sheets and its money but leaves behind everything the old session
owned — ledger-owned conditions, stat modifiers, and anything bound to the
adventure it came out of.
"""

import pytest
from fastapi import HTTPException
from osrlib.core.abilities import AbilityScore
from osrlib.core.effects import ActiveCondition, ActiveModifier, Condition
from osrlib.core.ruleset import Ruleset
from osrlib.crawl.commands import GrantItem
from osrlib.data import load_ability_tables, load_classes
from osrlib.persistence import save_game

from server.app import _games, create_game
from server.content import build_party, new_session, party_from_save
from server.creation import HouseRules


def _expected_max_hp(class_id: str, con_score: int) -> int:
    definition = load_classes().get(class_id)
    die = definition.row(1).hit_dice.die
    modifier = load_ability_tables().hit_point_modifier(con_score)
    return max(1, die + modifier)


def _scores(party) -> list[dict]:
    return [dict(member.scores) for member in party.members]


class TestBuildPartyHouseRules:
    def test_six_members_by_the_book_by_default(self):
        party, _ = build_party(seed=7, ruleset=Ruleset())
        assert len(party.members) == 6
        for member in party.members:
            assert member.level == 1
            assert member.max_hp >= 1

    def test_every_member_wields_a_weapon(self):
        # Regression: the dwarf's best kit pairs a two-handed battle axe with a
        # shield. Equipping the shield first left the axe unwieldable, so the
        # dwarf marched in weaponless. The shield must go on last (or not at all
        # for a two-handed arm) so every member fields a weapon, on any seed.
        for seed in range(1, 25):
            party, _ = build_party(seed=seed, ruleset=Ruleset())
            for member in party.members:
                assert member.inventory.wielded, f"seed {seed}: {member.name} the {member.class_id} wields nothing"

    def test_dwarf_wields_its_axe_over_the_shield(self):
        # A two-handed weapon precludes a shield in OSE, so the pregenerated
        # dwarf keeps the battle axe in hand and leaves the shield packed.
        party, _ = build_party(seed=12345, ruleset=Ruleset())
        dwarf = next(member for member in party.members if member.class_id == "dwarf")
        assert [i.template.id for i in dwarf.inventory.wielded] == ["battle_axe"]
        assert dwarf.inventory.shield is None

    def test_max_hp_takes_the_top_of_every_hit_die(self):
        party, _ = build_party(seed=7, ruleset=Ruleset(), house_rules=HouseRules(max_hp=True))
        for member in party.members:
            con = member.scores[AbilityScore.CON]
            assert member.max_hp == _expected_max_hp(member.class_id, con)
            assert member.current_hp == member.max_hp

    def test_same_seed_and_rules_reproduce_the_party(self):
        rules = HouseRules(ability_method="4d6_drop_lowest", max_hp=True)
        first, _ = build_party(seed=11, ruleset=Ruleset(), house_rules=rules)
        second, _ = build_party(seed=11, ruleset=Ruleset(), house_rules=rules)
        assert _scores(first) == _scores(second)
        assert [m.max_hp for m in first.members] == [m.max_hp for m in second.members]

    def test_4d6_drop_lowest_changes_the_draws(self):
        by_the_book, _ = build_party(seed=11, ruleset=Ruleset())
        dropped, _ = build_party(
            seed=11,
            ruleset=Ruleset(),
            house_rules=HouseRules(ability_method="4d6_drop_lowest"),
        )
        assert _scores(by_the_book) != _scores(dropped)


class TestCreateGameHouseRules:
    """The `POST /api/games` handler, called directly (no HTTP layer)."""

    def test_premade_party_honors_max_hp(self):
        payload = create_game({"seed": 7, "house_rules": {"max_hp": True}})
        try:
            for member in payload["view"]["party"]:
                con = payload["sheets"][member["id"]]["scores"]["con"]
                assert member["max_hp"] == _expected_max_hp(member["class_id"], con)
        finally:
            _games.pop(payload["game_id"], None)

    def test_no_house_rules_still_rolls_a_party(self):
        payload = create_game({"seed": 7})
        try:
            assert len(payload["view"]["party"]) == 6
        finally:
            _games.pop(payload["game_id"], None)

    def test_unknown_ability_method_is_422(self):
        with pytest.raises(HTTPException) as excinfo:
            create_game({"seed": 7, "house_rules": {"ability_method": "2d6"}})
        assert excinfo.value.status_code == 422


def _template_ids(inventory) -> list[str]:
    return [getattr(instance, "template_id", None) or instance.template.id for instance in inventory.all_instances()]


@pytest.fixture
def veteran_save():
    """A save of *The Cold Vein* whose party carries the adventure's own key.

    The token goes into a pack and into an equipped slot, because `has_item`
    reads the whole carried surface and the strip has to match it.
    """
    session = new_session(seed=42)
    holder, wielder = session.party.members[0], session.party.members[1]
    session.execute(GrantItem(character_id=holder.id, item_id="verrow-token"))
    session.execute(GrantItem(character_id=wielder.id, item_id="verrow-token"))
    carried = wielder.inventory.carried_item("verrow-token")
    wielder.inventory.items.remove(carried)
    wielder.inventory.wielded = [*wielder.inventory.wielded, carried]
    holder.conditions = (
        ActiveCondition(condition=Condition.BLIND, effect_id="effect-0001"),
        ActiveCondition(condition=Condition.DEAD, effect_id=None),
    )
    holder.stat_modifiers = (ActiveModifier(kind="ac_bonus", value=2, effect_id="effect-0001"),)
    assert holder.inventory.carried_item("verrow-token") is not None
    assert wielder.inventory.carried_item("verrow-token") is not None
    return session, save_game(session)


class TestPartyImport:
    """A veteran walks into the next adventure with their gear, not with its plot.

    `has_item` is stateless and party-wide — living or dead, worn slots included
    — so a bundled key that survived the import would pre-satisfy the next
    adventure's gates, or its own adventure's on a re-run.
    """

    def test_the_bundled_key_never_crosses(self, veteran_save):
        session, document = veteran_save
        assert sum(_template_ids(member.inventory).count("verrow-token") for member in session.party.members) == 2, (
            "the fixture must actually carry the key for this to prove anything"
        )
        party = party_from_save(document)
        for member in party.members:
            assert "verrow-token" not in _template_ids(member.inventory)
            assert member.inventory.carried_item("verrow-token") is None

    def test_shipped_gear_and_money_survive_the_strip(self, veteran_save):
        session, document = veteran_save
        party = party_from_save(document)
        for before, after in zip(session.party.members, party.members, strict=True):
            expected = [item_id for item_id in _template_ids(before.inventory) if item_id != "verrow-token"]
            assert _template_ids(after.inventory) == expected
            assert after.inventory.purse == before.inventory.purse
            assert [v.instance_id for v in after.inventory.valuables] == [
                v.instance_id for v in before.inventory.valuables
            ]
            assert after.inventory.wielded, f"{after.name} came home weaponless"

    def test_the_existing_condition_and_modifier_strip_still_holds(self, veteran_save):
        _, document = veteran_save
        party = party_from_save(document)
        survivor = party.members[0]
        assert [active.condition for active in survivor.conditions] == [Condition.DEAD]
        assert survivor.stat_modifiers == ()
