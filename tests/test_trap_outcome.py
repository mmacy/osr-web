"""End-to-end pin for the sprung-trap verdict (`server/narrate.py`).

`tests/test_narrate.py` covers the renderer against synthetic events. These drive
the real engine instead, so the transcript is pinned to what
`osrlib.crawl.exploration._resolve_trap` actually emits rather than to what this
repo believes it emits — which is the half of the bug that a fake event stream
cannot catch.

Two of the engine's three no-damage paths are reachable from a shipped document:

- **the save negates** — the capture fixture's `lead-casket` needle trap (1d4,
  save vs death, `on_save: negates`) on level 2;
- **halved damage rounds to zero** — the same fixture's sump-gallery dart volley
  (1d2 darts of 1d3, save vs breath for half), where a passed save on the minimum
  roll floors to nothing.

The third — an effect with no `damage_dice` at all — needs a trap no shipped
document authors, so it is built here by copying the fixture in memory and
swapping its trap for a `manual` one. The fixture on disk is never touched.

Outcomes are die rolls, so each case is found by scanning seeds the way
`tests/test_battle_round.py` does.
"""

import copy
import json
from pathlib import Path

import pytest
from osrlib.core.events import Visibility
from osrlib.crawl.commands import EnterDungeon, MoveParty, PlaceParty, TakeTreasure
from osrlib.crawl.dungeon import Direction, PartyLocation

from server.content import new_session
from server.narrate import _TRAP_NO_HARM, Narrator
from server.narration import _entry_lines, detect_beat

FIXTURE = Path(__file__).parent / "assets" / "capture-adventure" / "adventure.json"
DUNGEON = "hollow-tithe"

# The sump gallery (level 1, area 4) begins at (10,0); the party walks in from
# (10,1). The lead casket (level 2, area 11) sits on cell (8,7).
SUMP_APPROACH = (1, (10, 1))
CASKET_CELL = (2, (8, 7))

_MANUAL_EFFECT = {
    "damage_dice": None,
    "volley_dice": None,
    "save": None,
    "kills": False,
    "condition": None,
    "condition_duration_dice": None,
    "condition_duration_amount": None,
    "condition_duration_unit": None,
    "fall_feet": None,
    "transition": None,
    "manual": "A gout of chill air blasts up through the grating.",
}


@pytest.fixture(scope="module")
def manual_trap_document(tmp_path_factory):
    """The fixture with the sump gallery's trap swapped for a damage-less one.

    `TrapEffect.manual` is the authoring surface for "the referee describes it" —
    no dice, no save, no condition, no fall. It is the purest form of the engine's
    `effect.damage_dice is None` path: the sprung event is the entire resolution.
    """
    document = json.loads(FIXTURE.read_text())
    level = document["payload"]["dungeons"][0]["levels"][0]
    area = next(a for a in level["areas"] if a["id"] == "4")
    area["trap"] = {
        "kind": "room",
        "trigger": "enter",
        "effect": copy.deepcopy(_MANUAL_EFFECT),
        "affects": "triggerer",
    }
    path = tmp_path_factory.mktemp("manual-trap") / "adventure.json"
    path.write_text(json.dumps(document))
    return path


def _place(session, level_number, position):
    session.execute(EnterDungeon(dungeon_id=DUNGEON))
    session.execute(
        PlaceParty(
            location=PartyLocation(
                kind="dungeon",
                dungeon_id=DUNGEON,
                level_number=level_number,
                position=position,
                facing=Direction.NORTH,
            )
        )
    )


def _spring(seed, command, level_number, position, document=FIXTURE):
    """Run one command that may spring a trap; return everything it produced."""
    session = new_session(seed=seed, adventure_path=document)
    _place(session, level_number, position)
    narrator = Narrator(session)
    before = {m.id: (m.current_hp, tuple(m.conditions)) for m in session.party.members}
    base = len(session.event_log)
    session.execute(command)
    delta = session.event_log[base:]
    entries = narrator.render(delta)
    after = {m.id: (m.current_hp, tuple(m.conditions)) for m in session.party.members}
    return session, delta, entries, before == after


def _find(command, level_number, position, *, harmless, document=FIXTURE, limit=400):
    """The first seed whose trap springs and lands on the wanted side of harm."""
    for seed in range(limit):
        session, delta, entries, unchanged = _spring(seed, command, level_number, position, document)
        codes = [getattr(e, "code", "") for e in delta]
        if "exploration.trap.sprung" not in codes:
            continue
        if unchanged is harmless:
            return seed, session, delta, entries
    raise AssertionError(f"no seed in {limit} sprang a trap with harmless={harmless}")


def _texts(entries):
    return [entry["text"] for entry in entries]


class TestNoDamagePaths:
    """The three `_resolve_trap` paths that spring a trap and deal no damage.

    All three are correct engine behavior — the bug was that the transcript said
    nothing about it, so the player saw a trap fire on their fighter with no
    consequence and no explanation, and the narrator invented one.
    """

    def test_a_negated_save_says_no_harm(self):
        seed, session, delta, entries = _find(TakeTreasure(feature_id="lead-casket"), *CASKET_CELL, harmless=True)
        codes = [getattr(e, "code", "") for e in delta]
        assert "combat.save.passed" in codes
        assert "combat.damage.dealt" not in codes, "the engine dealt no damage"
        texts = _texts(entries)
        assert texts[0].startswith("A trap springs on")
        assert _TRAP_NO_HARM in texts
        assert all(m.current_hp == m.max_hp for m in session.party.members)

    def test_halved_damage_that_rounds_to_zero_says_no_harm(self):
        seed, session, delta, entries = _find(MoveParty(direction=Direction.NORTH), *SUMP_APPROACH, harmless=True)
        codes = [getattr(e, "code", "") for e in delta]
        assert "combat.save.passed" in codes
        assert "combat.damage.dealt" not in codes
        assert _TRAP_NO_HARM in _texts(entries)

    def test_a_trap_with_no_damage_dice_says_no_harm(self, manual_trap_document):
        seed, session, delta, entries = _find(
            MoveParty(direction=Direction.NORTH),
            *SUMP_APPROACH,
            harmless=True,
            document=manual_trap_document,
        )
        # The whole resolution: a referee spring roll and the sprung event.
        player = [e for e in delta if e.visibility is Visibility.PLAYER]
        assert [e.code for e in player if e.event_type == "trap"] == ["exploration.trap.sprung"]
        springs, verdict = _texts(entries)[-2:]
        assert springs.startswith("A trap springs on")
        assert verdict == _TRAP_NO_HARM


class TestTheDamagePath:
    """The control: when a trap does land, nothing reassuring is printed."""

    def test_damage_drops_hit_points_and_gets_no_reassurance(self):
        seed, session, delta, entries = _find(TakeTreasure(feature_id="lead-casket"), *CASKET_CELL, harmless=False)
        codes = [getattr(e, "code", "") for e in delta]
        assert "combat.save.failed" in codes
        damage = next(e for e in delta if e.event_type == "damage_dealt")
        assert damage.amount > 0
        victim = next(m for m in session.party.members if m.id == damage.target_id)
        assert victim.current_hp < victim.max_hp or "dead" in victim.conditions
        texts = _texts(entries)
        assert f"{victim.name} takes {damage.amount} damage." in texts
        assert _TRAP_NO_HARM not in texts


class TestWhatTheModelIsHanded:
    """`just_now` is the rendered transcript, so the fix reaches the prompt free.

    This is the whole point of putting the fix in `narrate.py` rather than in the
    prompt builder: `build_prompt` keeps its strings-only signature, and the model
    is handed the same sentence the player reads.
    """

    def test_the_beat_still_fires_and_carries_the_outcome(self):
        seed, session, delta, entries = _find(MoveParty(direction=Direction.NORTH), *SUMP_APPROACH, harmless=True)
        visible = [e for e in delta if e.visibility is Visibility.PLAYER]
        rule = detect_beat(visible, entries, {m.id for m in session.party.members}, True)
        assert rule is not None and rule.name == "trap_sprung"
        just_now = "\n".join(_entry_lines(entries))
        assert f"[mech] {_TRAP_NO_HARM}" in just_now
        assert "damage" not in just_now
