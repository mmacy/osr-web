"""Unit tests for the town-commerce enrichment (`server/app.py`).

Phase 2 of the UX plan serves the temple price list and the per-member
preparable spell lists from the server instead of hardcoding them in the
client. Everything here is rulebook-public or the player's own record:
`temple_services` mirrors the engine's `HEALING_SERVICES` table (asserted
against the table itself, never restated literals), and `spell_books` is each
member's preparable list — an arcane caster's own book, a divine caster's
class list truncated to the spell levels their progression row grants.
"""

from osrlib.crawl.commands import AwardXP, LearnSpell
from osrlib.crawl.exploration import HEALING_SERVICES
from osrlib.data import load_spells

from server.app import Game, _preparable, _spellbook, _state
from server.content import new_session


def _town_state() -> dict:
    game = Game(new_session(seed=42))
    game.narration = None
    return _state(game)


def _by_class(session, class_id):
    return next(m for m in session.party.members if m.class_id == class_id)


def _leveled_game() -> Game:
    """A game whose magic-user has just reached level 2 — one open book pick."""
    game = Game(new_session(seed=42))
    game.narration = None
    caster = _by_class(game.session, "magic_user")
    result = game.session.execute(AwardXP(character_id=caster.id, amount=2500))
    assert result.accepted
    return game


class TestTempleServices:
    def test_entries_mirror_the_engine_table(self):
        # The payload is HEALING_SERVICES said out loud: same service ids, the
        # table's own price, and the served spell's display name.
        services = _town_state()["temple_services"]
        catalog = load_spells()
        assert {entry["id"] for entry in services} == set(HEALING_SERVICES)
        for entry in services:
            spell_id, cost = HEALING_SERVICES[entry["id"]]
            assert entry["cost_gp"] == cost
            assert entry["name"] == catalog.get(spell_id).name

    def test_entry_key_set_is_exactly_the_contract(self):
        for entry in _town_state()["temple_services"]:
            assert set(entry) == {"id", "name", "cost_gp"}


class TestSpellbookEntries:
    def test_entry_key_set_is_exactly_the_contract(self):
        spellbook = _town_state()["spellbook"]
        assert spellbook, "the premade party always fields casters"
        for entry in spellbook.values():
            assert set(entry) == {"name", "mode", "target", "level"}

    def test_spellbook_covers_every_preparable_spell(self):
        # The prep panel reads names and levels out of the spellbook, so every
        # id in a member's preparable list must have an entry.
        state = _town_state()
        for spell_ids in state["spell_books"].values():
            for spell_id in spell_ids:
                assert spell_id in state["spellbook"]


class TestPreparable:
    def test_level_one_cleric_is_absent_from_spell_books(self):
        # A first-level cleric's row grants no slots, so there is nothing to
        # prepare and no list to send.
        session = new_session(seed=42)
        game = Game(session)
        game.narration = None
        cleric = _by_class(session, "cleric")
        assert cleric.id not in _state(game)["spell_books"]

    def test_level_two_cleric_gets_the_cleric_level_one_list(self):
        # At level 2 the row grants one first-level slot, and the divine list
        # is the rulebook's: every cleric level-1 spell, sorted by name.
        session = new_session(seed=42)
        cleric = _by_class(session, "cleric")
        cleric.level = 2
        expected = [
            spell.id
            for spell in sorted(
                load_spells().by_list("cleric", level=1),
                key=lambda spell: (spell.level, spell.name),
            )
        ]
        preparable = _preparable(cleric)
        assert preparable == expected
        assert len(preparable) == 8

    def test_arcane_preparable_is_the_member_own_book(self):
        session = new_session(seed=42)
        caster = _by_class(session, "magic_user")
        assert _preparable(caster) == list(caster.spell_book)

    def test_non_casters_prepare_nothing(self):
        session = new_session(seed=42)
        assert _preparable(_by_class(session, "fighter")) == []


class TestLearnable:
    """The per-member learnable-spell lists (`server/app.py::_learnable`).

    Everything here is rulebook-public or the player's own record: the
    candidates are the class spell list at each spell level with an open book
    pick, minus the spells the member's own book already holds.
    """

    def test_entry_key_set_is_exactly_the_contract(self):
        learnable = _state(_leveled_game())["learnable"]
        assert learnable, "a leveled magic-user has an open pick"
        for entries in learnable.values():
            for entry in entries:
                assert set(entry) == {"id", "name", "level", "intro"}

    def test_members_without_open_picks_are_absent(self):
        # A fresh party has no open pick anywhere: the arcane books are full
        # at level 1 and nobody else keeps a book.
        assert _town_state()["learnable"] == {}

    def test_a_leveled_magic_user_lists_the_remaining_level_one_spells(self):
        game = _leveled_game()
        caster = _by_class(game.session, "magic_user")
        entries = _state(game)["learnable"][caster.id]
        assert len(entries) == 11
        listed = {entry["id"] for entry in entries}
        full_list = {spell.id for spell in load_spells().by_list("magic_user", 1)}
        assert listed == full_list - set(caster.spell_book)

    def test_learn_spell_closes_the_pick_and_grows_spell_books(self):
        game = _leveled_game()
        caster = _by_class(game.session, "magic_user")
        pick = _state(game)["learnable"][caster.id][0]
        result = game.session.execute(LearnSpell(character_id=caster.id, spell_id=pick["id"]))
        assert result.accepted
        state = _state(game)
        assert caster.id not in state["learnable"]
        assert pick["id"] in caster.spell_book
        assert state["spell_books"][caster.id] == list(caster.spell_book)


class TestSpellbookLevels:
    def test_levels_match_the_catalog(self):
        state = _town_state()
        catalog = load_spells()
        for spell_id, entry in state["spellbook"].items():
            assert entry["level"] == catalog.get(spell_id).level


def test_spellbook_widens_over_memorized_only():
    # _spellbook covers the whole preparable union, not memorized copies alone;
    # an unprepared book spell would otherwise have no name in the prep panel.
    session = new_session(seed=42)
    caster = _by_class(session, "magic_user")
    book = _spellbook(session)
    for spell_id in caster.spell_book:
        assert spell_id in book
