"""Monsters are named the way osrlib's display names name them.

osrlib gives every monster template a `display_name` and a `display_name_plural`, and labels a
monster group with them. A catalog name is written for a rulebook's index, like "Centipede, Giant"
or "Veteran (2)", so the narrator never shows one. A display name is lowercase, so a line that
starts with a monster capitalizes it, and a monster inside a sentence gets "the".
"""

from types import SimpleNamespace

import pytest
from osrlib.core.monsters import MONSTER_SPAWN_STREAM, spawn_monster
from osrlib.core.rng import RngStreams
from osrlib.data import load_equipment, load_monsters

from server.narrate import Narrator

FLARG = SimpleNamespace(id="character-0001", name="Flarg")


def monster(template_id: str):
    stream = RngStreams(master_seed=3).get(MONSTER_SPAWN_STREAM)
    return spawn_monster(load_monsters().get(template_id), id="monster-0001", stream=stream)


def narrator(template_id: str) -> Narrator:
    lookup = {"monster-0001": monster(template_id)}
    session = SimpleNamespace(
        party=SimpleNamespace(members=[FLARG]),
        combatant=lookup.get,
        encounter=None,
        effective_equipment=load_equipment(),
    )
    return Narrator(session)


def attack(attacker_id: str, defender_id: str, attack_name: str):
    return SimpleNamespace(
        event_type="attack_rolled",
        code="combat.attack.hit",
        attacker_id=attacker_id,
        defender_id=defender_id,
        attack_name=attack_name,
        roll=15,
        modifier=2,
        total=17,
        required=13,
        defender_ac=7,
        natural=None,
    )


def started(count: int):
    return SimpleNamespace(
        event_type="encounter_started",
        code="encounter.started",
        monster_name="giant centipede",
        monster_name_plural="giant centipedes",
        count=count,
        distance_feet=60,
        party_surprised=False,
        monsters_surprised=False,
    )


def test_a_monster_inside_a_sentence_gets_the():
    entry = narrator("centipede_giant")._on_attack_rolled(attack("character-0001", "monster-0001", "sword"))
    assert entry["text"] == "Flarg hits the giant centipede with sword: rolled 15+2 = 17."


def test_a_monster_at_the_start_of_a_line_is_capitalized():
    entry = narrator("centipede_giant")._on_attack_rolled(attack("monster-0001", "character-0001", "bite"))
    assert entry["text"] == "The giant centipede hits Flarg with bite: rolled 15+2 = 17."


@pytest.mark.parametrize("template_id", ["veteran_2", "hellhound_5"])
def test_a_form_the_players_cant_see_is_never_named(template_id):
    entry = narrator(template_id)._on_attack_rolled(attack("character-0001", "monster-0001", "sword"))
    template = load_monsters().get(template_id)
    assert f"the {template.display_name} " in entry["text"]
    assert "(" not in entry["text"].split(":")[0]


@pytest.mark.parametrize(
    ("count", "text"),
    [(1, "Monsters! 1 giant centipede, 60 feet away."), (3, "Monsters! 3 giant centipedes, 60 feet away.")],
)
def test_the_encounter_line_counts_with_the_plural(count, text):
    entries = narrator("centipede_giant")._on_encounter_started(started(count))
    assert entries[0]["text"] == text
