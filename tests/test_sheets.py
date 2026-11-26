"""Unit tests for the character-sheet enrichment (`server/app.py::_sheets`).

The sheet rides beside the player projection and must carry only what a B/X
player reads off their own character record — derived from the session's
Character objects, never referee state.
"""

from osrlib.core.classes import level_title
from osrlib.core.events import Visibility
from osrlib.core.items import MagicItemInstance
from osrlib.crawl.commands import AwardXP, EquipItem, GrantItem, PrepareSpells, Rest, UnequipItem

from server.app import _sheets
from server.content import new_session

SAVE_KEYS = {"death", "wands", "paralysis", "breath", "spells"}
ABILITY_KEYS = {"str", "int", "wis", "dex", "con", "cha"}


class TestSheets:
    def setup_method(self):
        self.session = new_session(seed=42)
        self.sheets = _sheets(self.session)

    def test_one_sheet_per_member_keyed_by_id(self):
        ids = {member.id for member in self.session.party.members}
        assert set(self.sheets) == ids

    def test_scores_and_saves_are_complete(self):
        for sheet in self.sheets.values():
            assert set(sheet["scores"]) == ABILITY_KEYS
            assert set(sheet["saves"]) == SAVE_KEYS
            assert all(2 <= v <= 20 for v in sheet["saves"].values())

    def test_progression_fields_are_consistent(self):
        for sheet in self.sheets.values():
            assert sheet["next_level_xp"] is None or sheet["next_level_xp"] > sheet["xp"]
            assert 2 <= sheet["thac0"] <= 20
            assert sheet["movement_per_turn"] > 0
            assert sheet["languages"], "every character speaks at least one tongue"
            assert sheet["languages"][0].startswith("alignment_")

    def test_sheet_matches_the_character(self):
        member = self.session.party.members[0]
        sheet = self.sheets[member.id]
        assert sheet["xp"] == member.xp
        assert sheet["armour_class"] == member.armour_class
        assert sheet["thac0"] == member.thac0
        assert sheet["alignment"] == member.alignment.value

    def test_no_referee_state_rides_along(self):
        # The sheet must never carry the engine's hidden bookkeeping.
        forbidden = {"seed", "streams", "stat_modifiers", "effect_id"}
        for sheet in self.sheets.values():
            assert not (forbidden & set(sheet))


class TestSpellSlotsAndRested:
    """The prep panel's two sheet extras: slot counts and the banked-sleep flag.

    `spell_slots` restates the member's own progression row (`spell_slots[i]`
    is the slots at spell level `i + 1`, empty for non-casters); `rested` is
    true only when a sleep is banked that the member hasn't spent on
    preparation — the engine's own gate for `PrepareSpells`, said up front.
    """

    def setup_method(self):
        self.session = new_session(seed=42)

    def _by_class(self, class_id):
        return next(m for m in self.session.party.members if m.class_id == class_id)

    def test_non_casters_have_empty_slots(self):
        sheets = _sheets(self.session)
        for member in self.session.party.members:
            if member.class_id in ("fighter", "thief", "dwarf", "halfling"):
                assert sheets[member.id]["spell_slots"] == []

    def test_level_one_cleric_slots_are_all_zero(self):
        # B/X clerics pray for nothing at level 1; the row still exists.
        cleric = self._by_class("cleric")
        slots = _sheets(self.session)[cleric.id]["spell_slots"]
        assert slots and all(count == 0 for count in slots)

    def test_level_one_arcane_casters_have_one_first_level_slot(self):
        sheets = _sheets(self.session)
        for class_id in ("magic_user", "elf"):
            member = self._by_class(class_id)
            assert sheets[member.id]["spell_slots"][0] == 1

    def test_fresh_session_is_unrested(self):
        # A new game opens with spells prepared but no sleep banked
        # (sleep_count == 0): the first in-game preparation demands a night.
        for sheet in _sheets(self.session).values():
            assert sheet["rested"] is False

    def test_a_night_rest_readies_everyone(self):
        self.session.execute(Rest(kind="night"))
        for sheet in _sheets(self.session).values():
            assert sheet["rested"] is True

    def test_preparing_spends_the_sleep_for_that_member_only(self):
        self.session.execute(Rest(kind="night"))
        caster = self._by_class("magic_user")
        result = self.session.execute(
            PrepareSpells(
                character_id=caster.id,
                selections=({"spell_id": caster.spell_book[0], "reversed": False},),
            )
        )
        assert result.accepted
        sheets = _sheets(self.session)
        assert sheets[caster.id]["rested"] is False
        for member in self.session.party.members:
            if member.id != caster.id:
                assert sheets[member.id]["rested"] is True


class TestLevelTitleAndSpellPicks:
    """The level-up flow's two sheet extras: the class-table title and open picks.

    `level_title` is the class table's title at the member's level (rulebook
    data); `spell_picks` restates the member's own progression row against
    their own spell book — `spell_picks[i]` is the open book slots at spell
    level `i + 1`, empty for everyone but arcane casters.
    """

    def setup_method(self):
        self.session = new_session(seed=42)

    def _by_class(self, class_id):
        return next(m for m in self.session.party.members if m.class_id == class_id)

    def test_level_title_matches_the_class_table(self):
        sheets = _sheets(self.session)
        for member in self.session.party.members:
            expected = level_title(member.definition, member.level)
            assert sheets[member.id]["level_title"] == expected
        assert sheets[self._by_class("magic_user").id]["level_title"] == "Medium"

    def test_fresh_arcane_book_has_no_open_picks(self):
        # A level-1 magic-user's book already holds their one first-level
        # spell; the row's other five spell levels grant nothing yet.
        caster = self._by_class("magic_user")
        assert _sheets(self.session)[caster.id]["spell_picks"] == [0, 0, 0, 0, 0, 0]

    def test_a_level_gain_opens_a_pick(self):
        # Level 2 grants a second first-level slot the book has not filled.
        caster = self._by_class("magic_user")
        result = self.session.execute(AwardXP(character_id=caster.id, amount=2500))
        assert result.accepted
        assert caster.level == 2
        assert _sheets(self.session)[caster.id]["spell_picks"][0] == 1

    def test_divine_and_non_casters_have_no_picks(self):
        sheets = _sheets(self.session)
        for class_id in ("cleric", "fighter", "thief"):
            member = self._by_class(class_id)
            assert sheets[member.id]["spell_picks"] == []


class TestArmourClassAsThePlayersKnowIt:
    """The sheet's armour class is the player view's, so it never tells more than the engine lets the players know.

    osrlib counts a worn cursed shield or suit of armour that is unidentified, and whose curse isn't revealed, as
    the +1 item it tests as. `MemberView.armour_class` has that number, and the live `Character` has the real one.
    """

    def setup_method(self):
        self.session = new_session(seed=42)

    def _wear_a_cursed_shield(self):
        fighter = next(m for m in self.session.party.members if m.class_id == "fighter")
        if fighter.inventory.shield is not None:
            worn = fighter.inventory.shield
            item_id = worn.instance_id if isinstance(worn, MagicItemInstance) else worn.template.id
            assert self.session.execute(UnequipItem(character_id=fighter.id, item_id=item_id)).accepted
        assert self.session.execute(GrantItem(character_id=fighter.id, item_id="cursed_shield_minus_2")).accepted
        cursed = next(
            entry
            for entry in fighter.inventory.items
            if isinstance(entry, MagicItemInstance) and entry.template_id == "cursed_shield_minus_2"
        )
        assert self.session.execute(EquipItem(character_id=fighter.id, item_id=cursed.instance_id)).accepted
        return fighter

    def test_an_unrevealed_cursed_shield_reads_as_the_players_know_it(self):
        fighter = self._wear_a_cursed_shield()
        view = next(m for m in self.session.view(Visibility.PLAYER).party if m.id == fighter.id)
        # The precondition: the engine's two numbers differ, so the test can tell which one the sheet sends.
        assert view.armour_class != fighter.armour_class

        sheet = _sheets(self.session)[fighter.id]

        assert (sheet["armour_class"], sheet["armour_class_ascending"]) == (
            view.armour_class,
            view.armour_class_ascending,
        )

    def test_every_sheet_agrees_with_the_player_view(self):
        view = {m.id: m for m in self.session.view(Visibility.PLAYER).party}
        for member_id, sheet in _sheets(self.session).items():
            assert sheet["armour_class"] == view[member_id].armour_class
            assert sheet["armour_class_ascending"] == view[member_id].armour_class_ascending
