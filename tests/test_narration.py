"""Unit tests for the LLM narrative layer (`server/narration.py`).

The engine tests use a `FakeProvider` with a gate so the worker's timing is
deterministic: block the first generation, pile beats behind it, then release.

`TestTheGuidanceWindows` breaks the file's synthetic habit deliberately: what a
window test is about is which authored block is in play at a real moment of a
real adventure, so those drive sessions over the bundled document with real
commands, the way `tests/test_authored_layer.py` drives the rest of the layer.
"""

import io
import json
import logging
import threading
import time
from inspect import signature
from pathlib import Path
from types import SimpleNamespace

import pytest
from osrlib.crawl.commands import (
    EnterDungeon,
    GrantItem,
    MoveParty,
    OpenDoor,
    PlaceParty,
    TravelToTown,
)
from osrlib.crawl.dungeon import Direction, PartyLocation
from osrlib.crawl.events import DoorEvent, LocationEnteredEvent

from server import library
from server.app import _games, _wire_app_logging, create_game
from server.llm import ProviderError, configure_narration
from server.narrate import _TRAP_NO_HARM as NO_HARM
from server.narrate import MODULE_PROSE
from server.narration import (
    BEATS,
    GUIDANCE_HEADER,
    BeatSnapshot,
    NarrationEngine,
    Passage,
    _partial_text,
    annotate_replacement,
    assemble_guidance,
    build_prompt,
    detect_beat,
    interleave,
    sanitize,
)


def event(event_type="", code="", **attrs):
    return SimpleNamespace(event_type=event_type, code=code, **attrs)


def entry(kind, text="..."):
    return {"kind": kind, "text": text}


def module_prose(text="..."):
    """The area-description entry, marked the way `Narrator` marks it.

    Both surfaces that act on module text — beat detection and replacement
    tagging — read the marker, never the entry's position, so a test that means
    "the document's room description" has to say so.
    """
    return {"kind": "prose", "text": text, MODULE_PROSE: True}


def emphasis(name):
    """The prompt emphasis of one beat rule, by name."""
    return next(rule.emphasis for rule in BEATS if rule.name == name)


class TestDetectBeat:
    def test_area_entered_on_first_visit_pair(self):
        entries = [entry("place", "Guard room"), module_prose("Dust hangs thick.")]
        rule = detect_beat([], entries, set())
        assert rule is not None and rule.name == "area_entered"

    def test_revisit_place_alone_is_not_a_beat(self):
        assert detect_beat([], [entry("place", "Guard room")], set()) is None

    def test_an_authored_beat_after_a_place_header_is_not_an_area_entry(self):
        # A repeatable trigger's journal line on an already-seen area renders as
        # exactly a place + prose pair. Matched as `area_entered`, the model
        # would be asked to stand in for the author's own words — and reload
        # would drop them server-side. The marker is what tells them apart.
        entries = [entry("place", "Guard room"), entry("prose", "The draught turns.")]
        assert detect_beat([], entries, set()) is None

    def test_encounter_started(self):
        events = [event("encounter_started", "encounter.started")]
        rule = detect_beat(events, [entry("danger", "Monsters!")], set())
        assert rule is not None and rule.name == "encounter_started"

    def test_battle_ended(self):
        events = [event("battle_ended", "battle.ended.victory")]
        rule = detect_beat(events, [entry("system", "Victory!")], set())
        assert rule is not None and rule.name == "battle_ended"

    def test_member_died(self):
        events = [event("death", "combat.death.died", target_id="character-0001")]
        rule = detect_beat(events, [entry("danger")], {"character-0001"})
        assert rule is not None and rule.name == "member_died"

    def test_monster_death_is_not_member_died(self):
        events = [event("death", "combat.death.died", target_id="monster-0001")]
        assert detect_beat(events, [entry("mech")], {"character-0001"}) is None

    def test_trap_sprung(self):
        events = [event("trap", "exploration.trap.sprung", character_id="c")]
        rule = detect_beat(events, [entry("danger")], set())
        assert rule is not None and rule.name == "trap_sprung"

    def test_a_harmless_trap_is_still_a_beat(self):
        """A dart that misses is a moment worth prose — but an honest one.

        The beat deliberately still fires when the trap did nothing: what the trap
        *did* reaches the model through the transcript, which now closes a harmless
        spring with `narrate._TRAP_NO_HARM` rather than leaving the outcome to be
        inferred from silence.
        """
        events = [event("trap", "exploration.trap.sprung", character_id="c")]
        entries = [entry("danger", "A trap springs on Flarg!"), entry("mech", NO_HARM)]
        rule = detect_beat(events, entries, set())
        assert rule is not None and rule.name == "trap_sprung"

    def test_treasure_with_items(self):
        events = [
            event(
                "item_acquired",
                "exploration.item.acquired",
                item_ids=("gem",),
                coins_gp_value=0,
            )
        ]
        rule = detect_beat(events, [entry("treasure")], set())
        assert rule is not None and rule.name == "treasure_found"

    def test_treasure_rich_coins(self):
        events = [
            event(
                "item_acquired",
                "exploration.item.acquired",
                item_ids=(),
                coins_gp_value=50,
            )
        ]
        rule = detect_beat(events, [entry("treasure")], set())
        assert rule is not None and rule.name == "treasure_found"

    def test_small_coin_take_is_not_a_beat(self):
        events = [
            event(
                "item_acquired",
                "exploration.item.acquired",
                item_ids=(),
                coins_gp_value=10,
            )
        ]
        assert detect_beat(events, [entry("treasure")], set()) is None

    def test_a_split_hoard_is_judged_whole(self):
        """A haul is one beat, however many members carry it out.

        `TakeTreasure` distributes a haul, so a 200 gp coin hoard is not one
        event over the bar: it is six shares of ~33 gp, each of them under it and
        none of them carrying an item. Judged per event this is silence — the
        storyteller stops narrating treasure entirely.
        """
        events = [
            event(
                "item_acquired",
                "exploration.item.acquired",
                item_ids=(),
                coins_gp_value=share,
            )
            for share in (34, 34, 33, 33, 33, 33)
        ]
        assert all(e.coins_gp_value < 50 for e in events)
        rule = detect_beat(events, [entry("treasure")], set())
        assert rule is not None and rule.name == "treasure_found"

    def test_a_split_hoard_under_the_bar_is_still_not_a_beat(self):
        # 30 gp between six is a purse, not a hoard — the threshold still means
        # something after the change.
        events = [
            event(
                "item_acquired",
                "exploration.item.acquired",
                item_ids=(),
                coins_gp_value=5,
            )
            for _ in range(6)
        ]
        assert detect_beat(events, [entry("treasure")], set()) is None

    def test_a_split_hoard_in_town_is_still_not_a_beat(self):
        # The in_dungeon gate survives the change: commerce also emits acquisitions.
        events = [
            event(
                "item_acquired",
                "exploration.item.acquired",
                item_ids=(),
                coins_gp_value=100,
            )
            for _ in range(6)
        ]
        assert detect_beat(events, [entry("treasure")], set(), in_dungeon=False) is None

    def test_one_share_carrying_an_item_still_fires(self):
        # Only one member ends up with the gem; the rest carry coin.
        events = [
            event(
                "item_acquired",
                "exploration.item.acquired",
                item_ids=("valuable-0001",) if index == 0 else (),
                coins_gp_value=1,
            )
            for index in range(6)
        ]
        rule = detect_beat(events, [entry("treasure")], set())
        assert rule is not None and rule.name == "treasure_found"

    def test_town_commerce_is_not_a_beat(self):
        events = [
            event(
                "item_acquired",
                "exploration.item.acquired",
                item_ids=("sword",),
                coins_gp_value=0,
            )
        ]
        assert detect_beat(events, [entry("treasure")], set(), in_dungeon=False) is None

    def test_game_over(self):
        events = [event("game_over", "session.game_over")]
        rule = detect_beat(events, [entry("danger")], set())
        assert rule is not None and rule.name == "game_over"

    def test_movement_fires_nothing(self):
        events = [event("party_moved", "exploration.move.moved")]
        assert detect_beat(events, [], set()) is None

    def test_multi_match_picks_the_top_row(self):
        # A trap springs and kills someone: member_died sits above trap_sprung.
        events = [
            event("trap", "exploration.trap.sprung", character_id="character-0001"),
            event("death", "combat.death.died", target_id="character-0001"),
        ]
        entries = [entry("danger", "A trap!"), entry("danger", "Slain!")]
        rule = detect_beat(events, entries, {"character-0001"})
        assert rule is not None and rule.name == "member_died"

    def test_leveled_up_fires_in_town(self):
        # The return-to-town award is where most levels land, so the beat
        # deliberately ignores the in_dungeon gate.
        events = [event("leveled_up", "session.level.gained", character_id="c")]
        rule = detect_beat(events, [entry("system")], set(), in_dungeon=False)
        assert rule is not None and rule.name == "leveled_up"

    def test_member_death_outranks_the_level(self):
        # A death and a level in one delta must read as elegy, not celebration.
        events = [
            event("death", "combat.death.died", target_id="character-0001"),
            event("leveled_up", "session.level.gained", character_id="character-0002"),
        ]
        entries = [entry("danger"), entry("system")]
        rule = detect_beat(events, entries, {"character-0001", "character-0002"})
        assert rule is not None and rule.name == "member_died"


def quest_activated(name="The Captain's Day-Book"):
    return event("quest_activated", "session.quest.activated", quest_id="q", name=name)


def objective_completed():
    return event("objective_completed", "session.quest.objective_completed", quest_id="q")


def quest_completed():
    return event("quest_completed", "session.quest.completed", quest_id="q")


def adventure_completed():
    return event("adventure_completed", "session.adventure.completed", quest_id="q")


def leveled_up(character_id="character-0002"):
    return event("leveled_up", "session.level.gained", character_id=character_id)


def haul(coins=200, item_ids=("valuable-0001",)):
    return event(
        "item_acquired",
        "exploration.item.acquired",
        item_ids=item_ids,
        coins_gp_value=coins,
    )


class TestDetectAuthoredBeats:
    """The four quest-layer rows, and the ordering their neighbours settled."""

    def test_quest_activated(self):
        entries = [entry("system", "New quest: …"), entry("prose", "Bring me …")]
        rule = detect_beat([quest_activated()], entries, set())
        assert rule is not None and rule.name == "quest_activated"

    def test_objective_completed(self):
        entries = [entry("prose", "The day-book comes up off the board.")]
        rule = detect_beat([objective_completed()], entries, set())
        assert rule is not None and rule.name == "objective_completed"

    def test_quest_completed_fires_in_town(self):
        # Homecomings end in town, so none of these four read `in_dungeon`.
        entries = [entry("system", "Quest complete: …")]
        rule = detect_beat([quest_completed()], entries, set(), in_dungeon=False)
        assert rule is not None and rule.name == "quest_completed"

    def test_adventure_completed_fires_in_town(self):
        entries = [entry("system", "The adventure is won …")]
        rule = detect_beat([adventure_completed()], entries, set(), in_dungeon=False)
        assert rule is not None and rule.name == "adventure_completed"

    def test_adventure_completed_outranks_its_own_quest_completion(self):
        # The pair that always arrives together: the engine emits the quest's
        # completion and the ending back to back, carrying the same beat.
        events = [quest_completed(), adventure_completed()]
        entries = [entry("system"), entry("prose"), entry("system")]
        rule = detect_beat(events, entries, set(), in_dungeon=False)
        assert rule is not None and rule.name == "adventure_completed"

    def test_adventure_completed_outranks_the_reward_level_up(self):
        # Rewards resolve after the ending event, so an `award_xp` reward can
        # level a member in the ending's own run.
        events = [quest_completed(), adventure_completed(), haul(100, ()), leveled_up()]
        entries = [entry("system"), entry("treasure"), entry("system")]
        rule = detect_beat(events, entries, set(), in_dungeon=False)
        assert rule is not None and rule.name == "adventure_completed"

    def test_the_ending_outranks_a_first_visit_area(self):
        # The one cost of putting the ending at the very top: a quest that
        # concludes on a first-visit area never annotates replacement, so the
        # raw module prose renders at once and the closing passage lands after
        # it — the same fail-open surface a dropped generation already has.
        events = [
            event("location_entered", "exploration.location.entered"),
            adventure_completed(),
        ]
        entries = [entry("place", "The cold face"), module_prose("Ice on the ore.")]
        rule = detect_beat(events, entries, set())
        assert rule is not None and rule.name == "adventure_completed"

    def test_a_first_visit_area_outranks_the_activation(self):
        # The bundled entry: the quest activates on `dungeon_entered` in the same
        # command that first reads the adit mouth. Replacement mode is structural
        # — the client is holding the marked prose behind a placeholder — so the
        # area beat has to win or raw prose renders while the passage narrates
        # paperwork. The offer still reaches the prompt through `just_now`.
        events = [
            event("location_entered", "exploration.location.entered"),
            quest_activated(),
        ]
        entries = [
            entry("place", "The adit mouth"),
            module_prose("A cold draught."),
            entry("system", "New quest: …"),
        ]
        rule = detect_beat(events, entries, set())
        assert rule is not None and rule.name == "area_entered"

    def test_member_death_outranks_the_quest_completion(self):
        events = [
            event("death", "combat.death.died", target_id="character-0001"),
            quest_completed(),
        ]
        entries = [entry("danger"), entry("system")]
        rule = detect_beat(events, entries, {"character-0001"})
        assert rule is not None and rule.name == "member_died"

    def test_quest_completed_outranks_the_reward_level_up(self):
        # A non-concluding quest's `award_xp` can level a member as it completes;
        # the level is the completion's side effect, not the story.
        events = [quest_completed(), leveled_up()]
        entries = [entry("system"), entry("system")]
        rule = detect_beat(events, entries, set(), in_dungeon=False)
        assert rule is not None and rule.name == "quest_completed"

    def test_objective_completed_outranks_the_haul_beneath_it(self):
        # The relic comes off a cache that also pays out: the moment is the party
        # finding what it came for, not the shine of the coins beside it.
        events = [haul(), objective_completed()]
        entries = [entry("treasure"), entry("prose")]
        rule = detect_beat(events, entries, set())
        assert rule is not None and rule.name == "objective_completed"

    def test_the_activation_outranks_the_loot_it_rode_in_on(self):
        events = [haul(), quest_activated()]
        entries = [entry("treasure"), entry("system")]
        rule = detect_beat(events, entries, set())
        assert rule is not None and rule.name == "quest_activated"

    def test_a_sprung_trap_still_outranks_the_activation(self):
        # A trapped threshold that wakes a quest reads as the trap; the offer
        # rides the prompt as fact.
        events = [
            event("trap", "exploration.trap.sprung", character_id="c"),
            quest_activated(),
        ]
        entries = [entry("danger"), entry("system")]
        rule = detect_beat(events, entries, set())
        assert rule is not None and rule.name == "trap_sprung"


class TestTrapEmphasis:
    """The trap beat's emphasis has to pin the model the way its siblings do.

    Every other beat names the facts the model may not exceed — "keep the monster
    count and kind exactly as stated", "name the fallen, nothing more", "describe
    only the listed items". The trap beat asked for "Sudden violence" and named no
    constraint at all, so a trap that harmed nobody was narrated as one that
    maimed the fighter.
    """

    def test_it_no_longer_demands_violence(self):
        assert "violence" not in emphasis("trap_sprung").lower()

    def test_it_forbids_unstated_injury(self):
        text = emphasis("trap_sprung").lower()
        assert "injury" in text
        assert "harmed no one" in text

    def test_it_pins_the_model_to_the_stated_outcome(self):
        # The shape every sibling shares: a tone, then a limit on what may be said.
        text = emphasis("trap_sprung").lower()
        assert "exactly as the facts state it" in text

    def test_the_sibling_beats_keep_their_constraints(self):
        # This fix must not be paid for out of the other beats.
        assert "exactly as stated" in emphasis("encounter_started")
        assert "nothing more" in emphasis("member_died")
        assert "only the listed items" in emphasis("treasure_found")
        assert "keep every secret" in emphasis("area_entered")

    def test_the_outcome_reaches_the_prompt_through_the_transcript(self):
        """The fix is in the facts, not in a special case for this beat.

        `just_now` is the rendered transcript, so the deterministic line carries
        the outcome into the prompt without `build_prompt` learning anything new —
        which is what keeps its strings-only signature intact.
        """
        prompt = build_prompt(
            adventure="The Cold Vein",
            party="Flarg the fighter (level 1, unhurt)",
            location="The captain's office",
            recent="",
            moments=[
                (
                    "trap_sprung",
                    f"[danger] A trap springs on Flarg!\n[mech] {NO_HARM}",
                )
            ],
            emphasis=emphasis("trap_sprung"),
        )
        assert NO_HARM in prompt
        assert "unhurt" in prompt


class TestAuthoredBeatEmphasis:
    """The authored rows all forbid restating a line the table already heard.

    Their shared limit is the verbatim discipline: an authored beat renders in
    the transcript word for word and reaches the prompt as fact, so a model that
    paraphrases it says the same thing twice, differently. "Any authored" is
    deliberate — `narrative` is `None` on all four events when the document
    authors nothing, and an emphasis asserting a line that is not there would be
    the trap beat's defect inverted.
    """

    def test_every_authored_beat_forbids_restating_the_line(self):
        for name in (
            "adventure_completed",
            "quest_completed",
            "objective_completed",
            "quest_activated",
        ):
            assert "word for word" in emphasis(name), name

    def test_each_carries_its_own_limit(self):
        assert "invent no epilogue" in emphasis("adventure_completed")
        assert "claim no reward" in emphasis("quest_completed")
        assert "exactly as named" in emphasis("objective_completed")
        assert "name no speaker" in emphasis("quest_activated")

    def test_the_activation_names_no_speaker(self):
        # `QuestActivatedEvent` carries no speaker and the transcript renders the
        # offer unattributed: the authored `speaker` reaches the player on the
        # quests card and the model only as guidance. An emphasis inviting "the
        # speaker's voice" would ask for steering to be stated as fact.
        assert "name no speaker" in emphasis("quest_activated")
        assert "speaker's voice" not in emphasis("quest_activated")


class TestBuildPrompt:
    def test_contains_the_facts_blocks(self):
        prompt = build_prompt(
            adventure="The Cold Vein — an abandoned silver mine",
            party="Alice the fighter (level 1, unhurt)",
            location="Entrance: A cold hall.",
            recent="[mech] The party rests.",
            moments=[("area_entered", "[place] Entrance\n[prose] A cold hall.")],
            emphasis="Sensory detail.",
        )
        assert "ADVENTURE: The Cold Vein — an abandoned silver mine" in prompt
        assert "PARTY: Alice the fighter (level 1, unhurt)" in prompt
        assert "LOCATION: Entrance: A cold hall." in prompt
        assert "[mech] The party rests." in prompt
        assert "JUST NOW (area_entered):" in prompt
        assert prompt.endswith("Narrate this moment. Sensory detail.")

    def test_signature_takes_strings_only(self):
        # What this proves now that the guidance channel exists: no session and
        # no event object reaches the builder — the steering included, which
        # arrives as a plain string assembled by `observe`, the one place allowed
        # to read the adventure document. Player safety for everything else is
        # still structural (the builder cannot leak what it never sees); for the
        # one marked block it is structural-plus-instructed.
        allowed = {str, list[tuple[str, str]]}
        for parameter in signature(build_prompt).parameters.values():
            assert parameter.annotation in allowed

    def test_guidance_renders_under_its_header_before_the_instruction(self):
        prompt = build_prompt(
            adventure="The Cold Vein",
            party="Alice the fighter (level 1, unhurt)",
            location="Entrance: A cold hall.",
            recent="",
            moments=[("area_entered", "[place] Entrance")],
            emphasis="Sensory detail.",
            guidance="Keep the deeper mine a rumor.\nThe cold is older than the lamps.",
        )
        lines = prompt.splitlines()
        assert GUIDANCE_HEADER in lines
        assert lines.index("JUST NOW (area_entered):") < lines.index(GUIDANCE_HEADER)
        assert lines.index(GUIDANCE_HEADER) < lines.index("Narrate this moment. Sensory detail.")
        assert "Keep the deeper mine a rumor." in lines
        assert "The cold is older than the lamps." in lines

    def test_an_empty_channel_renders_nothing_at_all(self):
        # The unauthored-document property: an adventure that authors no
        # guidance builds the prompt it built before the channel existed.
        parts = {
            "adventure": "The Cold Vein",
            "party": "Alice the fighter (level 1, unhurt)",
            "location": "Entrance: A cold hall.",
            "recent": "",
            "moments": [("area_entered", "[place] Entrance")],
            "emphasis": "Sensory detail.",
        }
        assert build_prompt(**parts, guidance="") == build_prompt(**parts)
        assert "REFEREE GUIDANCE" not in build_prompt(**parts, guidance="")


class TestSanitize:
    def test_strips_markdown_and_wrapping_quotes(self):
        assert sanitize('  "The **hall** _glows_ faintly."  ') == "The hall glows faintly."

    def test_collapses_newlines(self):
        assert sanitize("One.\n\nTwo.") == "One. Two."

    def test_caps_at_a_sentence_boundary(self):
        text = "A sentence that runs a while here. " * 60
        cleaned = sanitize(text)
        assert len(cleaned) <= 600
        assert cleaned.endswith(".")

    def test_empty_after_cleaning_drops(self):
        assert sanitize(" ** ** \n") == ""

    def test_strips_reasoning_scratchpad(self):
        assert sanitize("<think>hmm, dark</think>The hall is dark.") == "The hall is dark."


class FakeProvider:
    """Records prompts; the gate makes the worker's timing deterministic.

    Streams the whole passage as one chunk, which keeps the existing
    enter-then-release timing (gate cleared before generation, released once
    the worker is inside) identical to the pre-streaming provider.
    """

    name = "fake"

    def __init__(self, error=False):
        self.prompts = []
        self.error = error
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def generate_stream(self, system, prompt, *, timeout):
        self.entered.set()
        assert self.gate.wait(timeout=5)
        self.prompts.append(prompt)
        if self.error:
            raise ProviderError("fake failure")
        yield f"Passage {len(self.prompts)}."

    def generate(self, system, prompt, *, timeout):
        return "".join(self.generate_stream(system, prompt, timeout=timeout))

    def health_check(self):
        return None


class ChunkedProvider:
    """Streams a passage delta-by-delta, one gate per chunk.

    `reached[i]` fires as the worker arrives at chunk `i`; `gates[i]` releases
    it. `fail_at` raises a `ProviderError` in place of yielding that chunk,
    modeling a stream that dies partway through.
    """

    name = "chunked"

    def __init__(self, chunks, fail_at=None):
        self.chunks = chunks
        self.fail_at = fail_at
        self.gates = [threading.Event() for _ in chunks]
        self.reached = [threading.Event() for _ in chunks]
        self.prompts = []

    def generate_stream(self, system, prompt, *, timeout):
        self.prompts.append(prompt)
        for index, chunk in enumerate(self.chunks):
            self.reached[index].set()
            assert self.gates[index].wait(timeout=5)
            if self.fail_at == index:
                raise ProviderError("mid-stream failure")
            yield chunk

    def generate(self, system, prompt, *, timeout):
        return "".join(self.generate_stream(system, prompt, timeout=timeout))

    def health_check(self):
        return None


def snapshot(name, anchor, just_now="[mech] something happened", replace_event=None, guidance=()):
    return BeatSnapshot(
        beat=name,
        emphasis="Emphasis.",
        anchor=anchor,
        adventure="A",
        party="P",
        location="L",
        recent="",
        just_now=just_now,
        replace_event=replace_event,
        guidance=guidance,
    )


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestNarrationEngine:
    def test_coalesces_queued_beats_into_one_passage(self):
        provider = FakeProvider()
        provider.gate.clear()
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 3, "[prose] a hall"))
        assert provider.entered.wait(2)  # worker is inside generate for beat 1
        engine.enqueue(snapshot("encounter_started", 5, "[danger] monsters"))
        engine.enqueue(snapshot("battle_ended", 9, "[system] victory"))
        assert engine.state()["pending"] == 3
        provider.gate.set()
        assert wait_until(lambda: engine.state()["pending"] == 0)
        assert len(provider.prompts) == 2
        merged = provider.prompts[1]
        assert "JUST NOW (encounter_started):" in merged
        assert "JUST NOW (battle_ended):" in merged
        tail = engine.tail(0)
        assert [item["seq"] for item in tail["entries"]] == [1, 2]
        # the merged passage anchors at the newest coalesced beat
        assert [item["anchor"] for item in tail["entries"]] == [3, 9]

    def test_full_queue_drops_the_oldest_beat(self):
        provider = FakeProvider()
        provider.gate.clear()
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 1, "generating"))
        assert provider.entered.wait(2)
        for index in range(5):  # capacity 4: the first of these five drops
            engine.enqueue(snapshot("encounter_started", 10 + index, f"queued-{index}"))
        assert engine.state()["pending"] == 5  # 1 generating + 4 queued
        provider.gate.set()
        assert wait_until(lambda: engine.state()["pending"] == 0)
        merged = provider.prompts[1]
        assert "queued-0" not in merged
        assert all(f"queued-{index}" in merged for index in range(1, 5))

    def test_three_consecutive_failures_disable(self):
        provider = FakeProvider(error=True)
        engine = NarrationEngine(provider, timeout=5)
        for index in range(3):
            engine.enqueue(snapshot("area_entered", index + 1))
            assert wait_until(lambda: engine.state()["pending"] == 0)
        assert wait_until(lambda: engine.state()["enabled"] is False)
        engine.enqueue(snapshot("area_entered", 9))  # ignored once disabled
        assert engine.state() == {"enabled": False, "pending": 0, "seq": 0}

    def test_success_resets_the_failure_streak(self):
        provider = FakeProvider(error=True)
        engine = NarrationEngine(provider, timeout=5)
        for index in range(2):
            engine.enqueue(snapshot("area_entered", index + 1))
            assert wait_until(lambda: engine.state()["pending"] == 0)
        provider.error = False
        engine.enqueue(snapshot("area_entered", 3))
        assert wait_until(lambda: engine.state()["seq"] == 1)
        assert engine.state()["enabled"] is True

    def test_passage_carries_replaced_events(self):
        provider = FakeProvider()
        provider.gate.clear()
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 4, "[prose] hall", replace_event=2))
        assert provider.entered.wait(2)
        engine.enqueue(snapshot("area_entered", 9, "[prose] crypt", replace_event=7))
        engine.enqueue(snapshot("encounter_started", 12, "[danger] monsters"))
        provider.gate.set()
        assert wait_until(lambda: engine.state()["pending"] == 0)
        tail = engine.tail(0)
        # first passage replaces its own prose; the coalesced one replaces the
        # crypt's prose and the encounter contributes no replacement
        assert [item["replaces"] for item in tail["entries"]] == [[2], [7]]

    def test_coalescing_merges_guidance_in_beat_order_and_dedupes(self):
        # Two beats race in behind a generation and merge into one prompt. The
        # level's block rides both snapshots and must not be said twice; each
        # beat's own steering must survive the merge, in beat order.
        level = "The upper workings were abandoned mid-shift."
        trigger = "The draught is the mine's first word."
        quest = "The errand is a debt the town is owed."
        provider = FakeProvider()
        provider.gate.clear()
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 1))
        assert provider.entered.wait(2)
        engine.enqueue(snapshot("trap_sprung", 4, guidance=(level, trigger)))
        engine.enqueue(snapshot("quest_activated", 7, guidance=(level, quest)))
        provider.gate.set()
        assert wait_until(lambda: engine.state()["pending"] == 0)
        merged = provider.prompts[1]
        block = merged.split(GUIDANCE_HEADER + "\n")[1].split("\n\n")[0]
        assert block.splitlines() == [level, trigger, quest]

    def test_an_unsteered_batch_carries_no_guidance_block(self):
        provider = FakeProvider()
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 2))
        assert wait_until(lambda: engine.state()["seq"] == 1)
        assert GUIDANCE_HEADER not in provider.prompts[0]

    def test_nothing_in_the_channel_crosses_the_wire(self):
        # The surfaces, not the model: a canned provider proves that neither the
        # state block nor the poll tail can carry steering, whatever the model
        # writes. (The passage text itself is the model's, and the emphasis plus
        # the never-quote header are what govern that.)
        secret = "never-quote-this-steering"
        provider = FakeProvider()
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 2, guidance=(secret,)))
        assert wait_until(lambda: engine.state()["seq"] == 1)
        engine.enqueue(snapshot("quest_activated", 5, guidance=(secret,)))
        assert wait_until(lambda: engine.state()["seq"] == 2)
        assert secret in provider.prompts[0]  # it did reach the model
        assert secret not in json.dumps(engine.state())
        assert secret not in json.dumps(engine.tail(0))

    def test_tail_after_filters_by_seq(self):
        provider = FakeProvider()
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 2))
        assert wait_until(lambda: engine.state()["seq"] == 1)
        engine.enqueue(snapshot("encounter_started", 4))
        assert wait_until(lambda: engine.state()["seq"] == 2)
        tail = engine.tail(1)
        assert [item["seq"] for item in tail["entries"]] == [2]
        assert tail["entries"][0]["kind"] == "narrative"


class TestPartialText:
    def test_strips_trailing_space_as_it_grows(self):
        assert _partial_text("Dust hangs ") == "Dust hangs"

    def test_does_not_trim_to_a_sentence_boundary(self):
        # A growing buffer keeps its incomplete tail, unlike sanitize().
        assert _partial_text("The hall is dark. A cold dr") == "The hall is dark. A cold dr"

    def test_hides_an_unclosed_reasoning_block(self):
        assert _partial_text("The hall. <think>where next") == "The hall."

    def test_removes_a_closed_reasoning_block(self):
        assert _partial_text("<think>dark</think>The hall is dark") == "The hall is dark"

    def test_strips_a_leading_quote_but_not_a_trailing_one(self):
        assert _partial_text('"The hall glows fain') == "The hall glows fain"


class TestStreaming:
    def test_partial_grows_then_finalizes_in_place(self):
        provider = ChunkedProvider(["Dust ", "hangs ", "thick."])
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 4, replace_event=2))
        assert provider.reached[0].wait(2)
        provider.gates[0].set()
        assert wait_until(lambda: (engine.tail(0)["partial"] or {}).get("text") == "Dust")
        partial = engine.tail(0)["partial"]
        # The partial shares its seq with the entry it will become and carries
        # the same anchor and replacement targets.
        assert partial["seq"] == 1
        assert partial["anchor"] == 4
        assert partial["replaces"] == [2]
        assert partial["done"] is False
        provider.gates[1].set()
        provider.gates[2].set()
        assert wait_until(lambda: engine.state()["seq"] == 1)
        tail = engine.tail(0)
        assert tail["partial"] is None
        assert [entry["text"] for entry in tail["entries"]] == ["Dust hangs thick."]
        assert tail["entries"][0]["seq"] == 1
        assert tail["entries"][0]["replaces"] == [2]

    def test_partial_hidden_once_the_client_has_caught_up(self):
        provider = ChunkedProvider(["A ", "hall."])
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 3))
        assert provider.reached[0].wait(2)
        provider.gates[0].set()
        assert wait_until(lambda: engine.tail(0)["partial"] is not None)
        # A client already past this seq is not re-shown the in-flight partial.
        assert engine.tail(1)["partial"] is None
        provider.gates[1].set()
        assert wait_until(lambda: engine.state()["seq"] == 1)

    def test_midstream_failure_drops_the_partial_and_keeps_the_seq(self):
        provider = ChunkedProvider(["Half ", "written"], fail_at=1)
        engine = NarrationEngine(provider, timeout=5)
        engine.enqueue(snapshot("area_entered", 4, replace_event=2))
        assert provider.reached[0].wait(2)
        provider.gates[0].set()
        assert wait_until(lambda: (engine.tail(0)["partial"] or {}).get("text") == "Half")
        provider.gates[1].set()  # this chunk raises instead of yielding
        assert wait_until(lambda: engine.tail(0)["partial"] is None)
        tail = engine.tail(0)
        assert tail["entries"] == []  # nothing finalized
        assert engine.state()["seq"] == 0  # a failed beat never burns a seq
        assert engine.state()["enabled"] is True  # one failure is not fatal


class TestInterleave:
    @staticmethod
    def render(events):
        return [{"kind": "mech", "text": text} for text in events]

    def test_passages_splice_at_their_anchors(self):
        events = ["e0", "e1", "e2", "e3"]
        passages = [
            Passage(seq=1, anchor=2, text="First."),
            Passage(seq=2, anchor=4, text="Second."),
        ]
        log = interleave(self.render, events, passages)
        assert [item["text"] for item in log] == [
            "e0",
            "e1",
            "First.",
            "e2",
            "e3",
            "Second.",
        ]
        assert [item["kind"] for item in log] == [
            "mech",
            "mech",
            "narrative",
            "mech",
            "mech",
            "narrative",
        ]

    def test_anchor_beyond_the_log_clamps(self):
        events = ["e0", "e1"]
        passages = [Passage(seq=1, anchor=99, text="Late.")]
        log = interleave(self.render, events, passages)
        assert [item["text"] for item in log] == ["e0", "e1", "Late."]

    def test_no_passages_is_a_plain_render(self):
        assert interleave(self.render, ["e0"], []) == [{"kind": "mech", "text": "e0"}]

    @staticmethod
    def render_areas(events):
        out = []
        for text in events:
            if text.startswith("area"):
                out.append({"kind": "place", "text": f"{text}-name"})
                out.append(module_prose(f"{text}-desc"))
            else:
                out.append({"kind": "mech", "text": text})
        return out

    def test_replacement_passage_stands_in_for_the_prose(self):
        events = ["e0", "area1", "e2", "e3"]
        passages = [Passage(seq=1, anchor=4, text="The hall.", replaces=(1,))]
        log = interleave(self.render_areas, events, passages)
        assert [(item["kind"], item["text"]) for item in log] == [
            ("mech", "e0"),
            ("place", "area1-name"),  # the area header survives
            ("narrative", "The hall."),  # the passage sits where the prose was
            ("mech", "e2"),
            ("mech", "e3"),
        ]

    def test_coalesced_replacement_drops_every_replaced_prose(self):
        events = ["area0", "e1", "area2", "e3"]
        passages = [
            Passage(seq=1, anchor=4, text="Both rooms.", replaces=(0, 2)),
        ]
        log = interleave(self.render_areas, events, passages)
        assert [(item["kind"], item["text"]) for item in log] == [
            ("place", "area0-name"),
            ("mech", "e1"),
            ("place", "area2-name"),
            ("narrative", "Both rooms."),
            ("mech", "e3"),
        ]

    def test_unreplaced_prose_still_renders(self):
        # A beat whose generation failed leaves no passage: raw prose reveals.
        events = ["area0", "e1"]
        log = interleave(self.render_areas, events, [])
        assert [item["kind"] for item in log] == ["place", "prose", "mech"]

    @staticmethod
    def render_runs(events):
        """A stand-in for `Narrator.render`'s haul collapse.

        The real renderer folds a run of `item_acquired` events into one line, so
        it only produces the live transcript's wording when the run reaches it
        whole. This stub reports how many `take` events arrived together, which is
        what the assertions below are actually about.
        """
        entries = []
        index = 0
        while index < len(events):
            if events[index].startswith("take"):
                run = index
                while run < len(events) and events[run].startswith("take"):
                    run += 1
                entries.append({"kind": "treasure", "text": f"split x{run - index}"})
                index = run
                continue
            if events[index].startswith("area"):
                entries.append({"kind": "place", "text": f"{events[index]}-name"})
                entries.append(module_prose(f"{events[index]}-desc"))
            else:
                entries.append({"kind": "mech", "text": events[index]})
            index += 1
        return entries

    def test_a_haul_run_reaches_the_renderer_whole(self):
        # Rendering one event at a time would report six splits of one share each.
        events = ["e0", "take1", "take2", "take3", "take4", "take5", "take6", "e7"]
        passages = [Passage(seq=1, anchor=8, text="Coin glints.")]
        log = interleave(self.render_runs, events, passages)
        assert [item["text"] for item in log] == [
            "e0",
            "split x6",
            "e7",
            "Coin glints.",
        ]

    def test_a_replaced_event_is_still_rendered_alone(self):
        # Batching must not widen the prose filter: only the replaced event's own
        # prose is dropped, and the haul beside it still collapses.
        events = ["area0", "take1", "take2", "e3", "area4"]
        passages = [Passage(seq=1, anchor=5, text="The vault.", replaces=(0,))]
        log = interleave(self.render_runs, events, passages)
        assert [(item["kind"], item["text"]) for item in log] == [
            ("place", "area0-name"),  # its prose is the passage's to replace
            ("narrative", "The vault."),
            ("treasure", "split x2"),
            ("mech", "e3"),
            ("place", "area4-name"),
            ("prose", "area4-desc"),  # not replaced, so it still reveals
        ]


class TestAnnotateReplacement:
    def test_tags_the_first_visit_prose(self):
        events = [
            event("party_moved", "exploration.move.moved"),
            event("location_entered", "exploration.area.entered", location_kind="area"),
        ]
        entries = [entry("place", "Gateyard"), module_prose("An open yard.")]
        index = annotate_replacement(events, entries, base_index=140)
        assert index == 141
        assert entries[1]["replace_id"] == 141
        assert "replace_id" not in entries[0]

    def test_no_area_event_tags_nothing(self):
        events = [event("party_moved", "exploration.move.moved")]
        entries = [entry("mech", "The party rests.")]
        assert annotate_replacement(events, entries, base_index=0) is None
        assert "replace_id" not in entries[0]

    def test_an_unmarked_prose_entry_is_never_tagged(self):
        # The area event is there — a revisit — but the prose beside it is an
        # authored beat, not the module's description. Tagged, it would be held
        # back behind a placeholder and dropped on reload.
        events = [event("location_entered", "exploration.area.entered", location_kind="area")]
        entries = [entry("place", "Gateyard"), entry("prose", "The draught turns.")]
        assert annotate_replacement(events, entries, base_index=0) is None
        assert all("replace_id" not in item for item in entries)


BUNDLED = Path(__file__).resolve().parent.parent / "content" / "adventure.json"
DUNGEON = "cold-vein"
RELIC = "captains-day-book"

# The five authored blocks of *The Cold Vein*, transcribed. Pinned here rather
# than read off the document so a deleted or reworded slot fails loudly instead
# of quietly making these tests agree with whatever is in the file.
LEVEL_1 = (
    "The upper workings were abandoned mid-shift and never looted: tools set "
    "down, not dropped. The register is workaday dread — cold economics, "
    "absence, the surface still faintly present in draughts and daylight. Keep "
    "the deeper mine a rumor, and never explain what stopped the work."
)
LEVEL_2 = (
    "Below the winze the mine stops being a workplace. Cold is the fact of this "
    "level — felt on skin, iron, and standing water — and the dark is older than "
    "the lamps. Let the silence press, speak of the cold as a presence, and "
    "never name its cause."
)
QUEST = (
    "The errand is a debt the town is owed, not a treasure hunt: the day-book is "
    "an answer, and Cinderhope has waited two generations for it. Keep Orrin "
    "Slake flinty and fair — he pays what he promises and says less than he knows."
)
GATE = (
    "The wicket and what lies behind it are the parish's, not the mine's: lead, "
    "wax, and an old blessing still holding. Let a little reverence in — the "
    "saint's protection is quiet and real — without ever making the saint act."
)
TRIGGER = (
    "The draught is the mine's first word: steady, cold, wrong for a dead "
    "working. Let it read as the place taking notice without ever saying so."
)
# The document authors no objective guidance — an objective's window is
# osr-web's own rule, so it is exercised against a doctored copy.
OBJECTIVE = "The board is the shift written up; the book is the last thing on it."

OFFER = (
    "Bring me the mine captain's day-book and I'll pay you twice over for it. "
    "Cinderhope has a right to know what stopped the work."
)
GATE_SUCCESS = "Saint Verrow's token fits the stamp in the lead band, and the wicket swings open."

WICKET_APPROACH = (11, 2)  # the corridor cell outside the lead wicket
LEDGE_CELL = (8, 2)  # the saint's offering ledge, where the token lies
CRIB_DOOR_CELL = (7, 11)  # south of the plain door into the crib


@pytest.fixture
def hermetic_library(monkeypatch, tmp_path):
    """Keep these tests off the developer's own library and saves."""
    drop = tmp_path / "drop"
    drop.mkdir()
    monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(drop))
    monkeypatch.delenv("OSR_WEB_ADVENTURE", raising=False)
    monkeypatch.delenv("OSR_WEB_ADVENTURES", raising=False)
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


def _started_game():
    payload = create_game({"seed": 42})
    return payload["game_id"], _games[payload["game_id"]]


@pytest.fixture
def cold_vein(hermetic_library):
    """A real game over the bundled document, interpreter and all."""
    game_id, game = _started_game()
    yield game
    _games.pop(game_id, None)


@pytest.fixture
def objective_game(hermetic_library, monkeypatch, tmp_path):
    """A game over a copy of the document whose first objective steers.

    The shipped objectives author no guidance — the quest's own block says what
    the errand is — so the objective window is pinned against a doctored copy,
    the way `tests/test_authored_layer.py` exercises the toll form of the gate.
    """
    document = json.loads(BUNDLED.read_text(encoding="utf-8"))
    objective = document["payload"]["quests"][0]["objectives"][0]
    objective["narrative"]["guidance"] = OBJECTIVE
    path = tmp_path / "objective-guidance-adventure.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("OSR_WEB_ADVENTURE", str(path))
    library._meta_cache.clear()
    game_id, game = _started_game()
    yield game
    _games.pop(game_id, None)


def run(session, command):
    """Execute one command and return the log delta, exactly as app.py reads it."""
    mark = len(session.event_log)
    session.execute(command)
    return session.event_log[mark:]


def place(session, position, *, level_number=1, facing=Direction.NORTH):
    """Put the party on a cell by referee fiat, the way a referee would."""
    return run(
        session,
        PlaceParty(
            location=PartyLocation(
                kind="dungeon",
                dungeon_id=DUNGEON,
                level_number=level_number,
                position=position,
                facing=facing,
            )
        ),
    )


class TestTheGuidanceWindows:
    """Which authored block steers which run, against the shipped document.

    Every carrier *The Cold Vein* authors is exercised at the seam guidance is
    actually read: `assemble_guidance`, handed a real session and the real log
    delta of a real command.
    """

    def test_in_town_nothing_is_in_play(self, cold_vein):
        # The "assembles to nothing" case, on a document that authors all five
        # blocks: no level (town), the quest not yet activated, no trigger fired,
        # no gate opened.
        session = cold_vein.session
        assert session.dungeon_state.location.kind == "town"
        assert assemble_guidance(session, []) == ()

    def test_the_entry_run_carries_level_quest_and_trigger_and_nothing_else(self, cold_vein):
        session = cold_vein.session
        delta = run(session, EnterDungeon(dungeon_id=DUNGEON))
        # Ambient to specific: the place, the standing charge, the thing that
        # just happened. The gate is untouched and both objectives author
        # nothing, so this is the whole channel at the mine's mouth.
        assert assemble_guidance(session, delta) == (LEVEL_1, QUEST, TRIGGER)

    def test_the_next_run_has_dropped_the_trigger(self, cold_vein):
        # Trigger steering lives and dies with its firing run; the level and the
        # standing quest are still in play a step later.
        session = cold_vein.session
        run(session, EnterDungeon(dungeon_id=DUNGEON))
        delta = run(session, MoveParty(direction=Direction.NORTH))
        assert assemble_guidance(session, delta) == (LEVEL_1, QUEST)

    def test_the_quest_steers_through_its_completing_run_and_no_further(self, cold_vein):
        session = cold_vein.session
        run(session, EnterDungeon(dungeon_id=DUNGEON))
        holder = session.party.members[0].id
        run(session, GrantItem(character_id=holder, item_id=RELIC))
        delta = run(session, TravelToTown())
        assert session.mode == "victory"
        # The inclusive completing edge: live state already reads `completed`,
        # and the run being narrated *is* the completion — which is exactly the
        # prompt the author's steering belongs in. No level block: town.
        assert assemble_guidance(session, delta) == (QUEST,)
        assert assemble_guidance(session, []) == ()

    def test_a_completed_objective_stops_riding(self, objective_game):
        session = objective_game.session
        delta = run(session, EnterDungeon(dungeon_id=DUNGEON))
        # Revealed and incomplete: it rides its quest, in authored order.
        assert assemble_guidance(session, delta) == (LEVEL_1, QUEST, OBJECTIVE, TRIGGER)
        holder = session.party.members[0].id
        delta = run(session, GrantItem(character_id=holder, item_id=RELIC))
        assert assemble_guidance(session, delta) == (LEVEL_1, QUEST, OBJECTIVE)
        assert assemble_guidance(session, []) == (LEVEL_1, QUEST)

    def test_the_gate_steers_its_success_run_alone(self, cold_vein):
        session = cold_vein.session
        run(session, EnterDungeon(dungeon_id=DUNGEON))
        holder = session.party.members[0].id
        run(session, GrantItem(character_id=holder, item_id="verrow-token"))
        place(session, WICKET_APPROACH, facing=Direction.SOUTH)
        delta = run(session, OpenDoor(direction=Direction.SOUTH))
        assert any(getattr(e, "narrative", None) == GATE_SUCCESS for e in delta)
        assert assemble_guidance(session, delta) == (LEVEL_1, QUEST, GATE)

        # A plain door is a door with no gate behind it: nothing to steer with.
        place(session, CRIB_DOOR_CELL, facing=Direction.NORTH)
        delta = run(session, OpenDoor(direction=Direction.NORTH))
        assert any(getattr(e, "event_type", "") == "door" for e in delta)
        assert assemble_guidance(session, delta) == (LEVEL_1, QUEST)

        # The relocation guard. No trigger pattern matches a door event, so the
        # only way an open can move the party is a door trap's transition
        # effect — which emits `location_entered`. With one in the delta the
        # session's location is no longer the door's, so the block is dropped
        # rather than resolved against the wrong level.
        opened = DoorEvent(
            code="exploration.door.opened",
            x=WICKET_APPROACH[0],
            y=WICKET_APPROACH[1],
            direction=Direction.SOUTH,
            narrative=GATE_SUCCESS,
        )
        moved = LocationEnteredEvent(location_kind="level", location_id=DUNGEON, level_number=2)
        assert GATE in assemble_guidance(session, [opened])
        assert GATE not in assemble_guidance(session, [opened, moved])

    def test_guidance_reaches_the_prompt_and_the_beat_text_arrives_untouched(self, cold_vein):
        # The whole channel, end to end: `observe` called the way app.py calls
        # it, over the real entry run.
        session = cold_vein.session
        provider = FakeProvider()
        engine = NarrationEngine(provider, timeout=5)
        mark = len(session.event_log)
        session.execute(EnterDungeon(dungeon_id=DUNGEON))
        delta = session.event_log[mark:]
        entries = cold_vein.narrator.render(delta)
        engine.observe(session, delta, entries)
        assert wait_until(lambda: engine.state()["seq"] == 1)

        prompt = provider.prompts[0]
        moment = prompt.split("JUST NOW (area_entered):\n")[1]
        assert f"[prose] {OFFER}" in moment  # the authored beat, byte-exact
        block = prompt.split(GUIDANCE_HEADER + "\n")[1].split("\n\n")[0]
        assert block.splitlines() == [LEVEL_1, QUEST, TRIGGER]
        # And none of it is in the transcript the player reads.
        rendered = json.dumps(entries)
        for steering in (LEVEL_1, QUEST, TRIGGER):
            assert steering not in rendered


class TestStartupVisibility:
    """The startup line has to actually reach a terminal.

    These test the mechanism, not the fact that logging was called: without the
    wiring the app's `INFO` line would be dropped on the floor because uvicorn
    configures only its own loggers, leaving root at `WARNING` with no handler.
    """

    def test_wiring_lifts_the_app_out_of_the_default_warning_floor(self):
        app_log = logging.getLogger("osr_web")
        previous = app_log.level
        try:
            app_log.setLevel(logging.NOTSET)  # what an unwired process looks like
            assert not logging.getLogger("osr_web.narration").isEnabledFor(logging.INFO)
            _wire_app_logging()
            assert logging.getLogger("osr_web.narration").isEnabledFor(logging.INFO)
        finally:
            app_log.setLevel(previous)

    def test_wiring_writes_through_uvicorns_handler(self):
        root = logging.getLogger()
        app_log = logging.getLogger("osr_web")
        uvicorn_log = logging.getLogger("uvicorn")
        saved = (root.handlers, app_log.handlers, app_log.level, uvicorn_log.handlers)
        stream = io.StringIO()
        try:
            root.handlers = []
            app_log.handlers = []
            uvicorn_log.handlers = [logging.StreamHandler(stream)]
            _wire_app_logging()
            logging.getLogger("osr_web.narration").info("narration enabled: ollama")
            assert "narration enabled: ollama" in stream.getvalue()
        finally:
            root.handlers, app_log.handlers, app_log.level, uvicorn_log.handlers = saved

    def test_wiring_falls_back_to_a_stream_when_nothing_is_configured(self):
        root = logging.getLogger()
        app_log = logging.getLogger("osr_web")
        uvicorn_log = logging.getLogger("uvicorn")
        saved = (root.handlers, app_log.handlers, app_log.level, uvicorn_log.handlers)
        try:
            root.handlers = []
            app_log.handlers = []
            uvicorn_log.handlers = []
            _wire_app_logging()
            assert app_log.handlers  # never silence, even off uvicorn
        finally:
            root.handlers, app_log.handlers, app_log.level, uvicorn_log.handlers = saved


class TestStartupReport:
    """One line per startup, saying which way narration went and why."""

    def test_blank_variable_reports_off(self, monkeypatch, caplog):
        monkeypatch.setenv("OSR_WEB_NARRATOR", "")
        with caplog.at_level(logging.INFO, logger="osr_web.narration"):
            assert configure_narration() is None
        assert "narration off: OSR_WEB_NARRATOR is empty" in caplog.text

    def test_absent_variable_reports_off(self, monkeypatch, caplog):
        monkeypatch.delenv("OSR_WEB_NARRATOR", raising=False)
        with caplog.at_level(logging.INFO, logger="osr_web.narration"):
            assert configure_narration() is None
        assert "narration off: OSR_WEB_NARRATOR is not set" in caplog.text

    def test_dotenv_provenance_is_named(self, monkeypatch, caplog):
        # The surprise worth explaining: a provider the shell never mentioned.
        monkeypatch.setenv("OSR_WEB_NARRATOR", "bogus")
        with caplog.at_level(logging.INFO, logger="osr_web.narration"):
            assert configure_narration(from_dotenv=True) is None
        assert "unknown provider 'bogus'" in caplog.text
        assert ".env" in caplog.text

    def test_shell_provenance_is_not_blamed_on_dotenv(self, monkeypatch, caplog):
        monkeypatch.setenv("OSR_WEB_NARRATOR", "bogus")
        with caplog.at_level(logging.INFO, logger="osr_web.narration"):
            assert configure_narration() is None
        assert ".env" not in caplog.text

    def test_provenance_does_not_change_the_outcome(self, monkeypatch):
        monkeypatch.setenv("OSR_WEB_NARRATOR", "")
        assert configure_narration(from_dotenv=True) is None
        monkeypatch.setenv("OSR_WEB_NARRATOR", "bogus")
        assert configure_narration(from_dotenv=True) is None
