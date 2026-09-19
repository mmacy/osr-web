"""Unit tests for the cell-context trap fields (`server/app.py::_cell_info`).

Phase 3 of the UX plan puts the thief's check-and-disarm play in the client,
which needs two player-safe facts per treasure feature: `trap_found` (the party
found a trap here and has not yet answered it) and `inspected_by` (who has
already spent their one inspect attempt). Both are the party's own history —
`found_traps` and `inspect_attempts` are written only by the party's own
commands — so an untrapped cache and a trapped-but-undiscovered one must stay
indistinguishable on the wire.

The fixture is the bundled adventure's one authored treasure trap: the
captain's strongbox at level 1 (18, 6), ref `cold-vein:1:captains-strongbox`.
"""

from osrlib.core.items import MagicItemInstance
from osrlib.crawl.commands import PlaceParty
from osrlib.crawl.dungeon import DropPile, GeneratedCache, PartyLocation, cell_ref

from server.app import _cell_info
from server.content import new_session

REF = "cold-vein:1:captains-strongbox"
FEATURE_KEYS = {"id", "description", "trap_found", "inspected_by"}


def office_session():
    """A fresh session standing on the strongbox cell in the captain's office."""
    session = new_session(seed=42)
    result = session.execute(
        PlaceParty(
            location=PartyLocation(
                kind="dungeon",
                dungeon_id="cold-vein",
                level_number=1,
                position=(18, 6),
                facing="north",
            )
        )
    )
    assert result.accepted
    return session


def strongbox(session) -> dict:
    features = _cell_info(session)["features"]
    assert [feature["id"] for feature in features] == ["captains-strongbox"]
    return features[0]


class TestAuthoredFeatureFields:
    def test_feature_key_set_is_exactly_the_contract(self):
        assert set(strongbox(office_session())) == FEATURE_KEYS

    def test_trapped_but_undiscovered_reads_as_untrapped(self):
        # The strongbox is authored trapped, but nobody has inspected it: the
        # wire must not know more than the party does.
        box = strongbox(office_session())
        assert box["trap_found"] is False
        assert box["inspected_by"] == []

    def test_a_found_trap_reads_true(self):
        session = office_session()
        session.dungeon_state.found_traps.append(REF)
        assert strongbox(session)["trap_found"] is True

    def test_a_removed_trap_reads_false_again(self):
        session = office_session()
        session.dungeon_state.found_traps.append(REF)
        session.dungeon_state.removed_traps.append(REF)
        assert strongbox(session)["trap_found"] is False

    def test_a_sprung_trap_reads_false_again(self):
        session = office_session()
        session.dungeon_state.found_traps.append(REF)
        session.dungeon_state.sprung_traps.append(REF)
        assert strongbox(session)["trap_found"] is False

    def test_inspect_attempts_surface_as_inspected_by(self):
        session = office_session()
        thief = next(m for m in session.party.members if m.class_id == "thief")
        session.dungeon_state.inspect_attempts[REF] = [thief.id]
        assert strongbox(session)["inspected_by"] == [thief.id]


class TestPileCount:
    def test_a_pile_of_left_behind_magic_is_not_empty(self):
        # A haul's magic items can be left behind like anything else, and the
        # party watched it happen — `ItemsLeftBehindEvent` is player-visible —
        # so the pile tally counts them and leaks nothing.
        session = office_session()
        here = cell_ref("cold-vein", 1, (18, 6))
        session.dungeon_state.piles[here] = DropPile(
            magic_items=[MagicItemInstance(instance_id="magic-item-9001", template_id="wand_of_fear")]
        )
        pile = _cell_info(session)["pile"]
        assert pile == {"coins_gp_value": 0, "count": 1}


class TestGeneratedCacheFields:
    def test_a_generated_cache_carries_both_fields(self):
        # Generated hoards are untrapped by engine rule (traps are authored
        # content only), so trap_found is constant false; inspect attempts key
        # by the cache id itself rather than a content ref.
        session = office_session()
        here = cell_ref("cold-vein", 1, (18, 6))
        session.dungeon_state.generated_caches["cache-0001"] = GeneratedCache(cell_ref=here)
        session.dungeon_state.inspect_attempts["cache-0001"] = ["character-0003"]
        features = {f["id"]: f for f in _cell_info(session)["features"]}
        cache = features["cache-0001"]
        assert set(cache) == FEATURE_KEYS
        assert cache["trap_found"] is False
        assert cache["inspected_by"] == ["character-0003"]
