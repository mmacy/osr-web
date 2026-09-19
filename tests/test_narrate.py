"""Unit tests for the event renderer (`server/narrate.py`).

These cover osr-web's presentation choices over osrlib's structured events: the
`Narrator` resolves entity ids to names and decides phrasing. A lightweight fake
session is enough — `_name` only walks the party, `combatant(...)`, and the
encounter.

Most tests here hand the renderer a `SimpleNamespace` standing in for an event,
which is deliberate — it keeps a test about phrasing from having to satisfy a
whole engine model. The cost is that a fake answers to any attribute name the
handler asks for, so it can never catch a handler reading a field the real event
does not have. [`TestHandlersReadRealFields`][tests.test_narrate.TestHandlersReadRealFields]
covers that gap for every handler at once, and the town-spoils tests below build
real osrlib events on purpose.
"""

import ast
import inspect
import textwrap
from inspect import signature
from types import SimpleNamespace

import osrlib.core.events as core_events
import osrlib.crawl.events as crawl_events
from osrlib.core.events import Event, SpellBookUpdatedEvent, SpellDisruptedEvent, Visibility
from osrlib.core.validation import Rejection
from osrlib.crawl.dungeon import Direction, PartyLocation
from osrlib.crawl.events import (
    AdventureCompletedEvent,
    AdventureXpAwardEvent,
    CharacterLeveledUpEvent,
    CurseRevealedEvent,
    DoorEvent,
    ItemConsumedEvent,
    ItemIdentifiedEvent,
    ItemUsedEvent,
    JournalEntryAddedEvent,
    LocationEnteredEvent,
    MonstersLeftBehindEvent,
    ObjectiveCompletedEvent,
    ObjectiveRevealedEvent,
    QuestActivatedEvent,
    QuestCompletedEvent,
    TreasureSoldEvent,
)
from osrlib.data import load_equipment

from server.content import load_adventure
from server.narrate import (
    _REJECTION_TEXT,
    _TRAP_CONSEQUENCE,
    MODULE_PROSE,
    Narrator,
    _found_phrase,
    rejection_text,
)
from server.narration import Passage, interleave


def event(event_type="", code="", **attrs):
    return SimpleNamespace(event_type=event_type, code=code, **attrs)


def make_narrator(members=(), combatants=None):
    """A `Narrator` over a fake session where `_name` resolves the given refs.

    `effective_equipment` is the engine's own resolver for authored item ids
    (shipped ∪ adventure-bundled), which is what the narrator names items
    through. A fake session bundling nothing hands over the shipped catalog,
    which is exactly what a real one would.
    """
    lookup = dict(combatants or {})
    session = SimpleNamespace(
        party=SimpleNamespace(members=list(members)),
        combatant=lambda ref: lookup.get(ref),
        encounter=None,
        effective_equipment=load_equipment(),
    )
    return Narrator(session)


def attack(code, **attrs):
    fields = {
        "attacker_id": "character-0001",
        "defender_id": "monster-0001",
        "attack_name": "sword",
        "roll": 15,
        "modifier": 2,
        "total": 17,
        "required": 13,
        "defender_ac": 7,
        "natural": None,
    }
    fields.update(attrs)
    return event("attack_rolled", code, **fields)


FLARG = SimpleNamespace(id="character-0001", name="Flarg")
KOBOLD = SimpleNamespace(name="Kobold", template=None)


def combat_narrator():
    return make_narrator(members=[FLARG], combatants={"monster-0001": KOBOLD})


class TestAttackRolled:
    def test_hit_has_no_needed_clause(self):
        entry = combat_narrator()._on_attack_rolled(attack("combat.attack.hit"))
        assert entry == {
            "kind": "mech",
            "text": "Flarg hits Kobold with sword: rolled 15+2 = 17.",
        }
        assert "need" not in entry["text"]

    def test_miss_ends_with_needed(self):
        entry = combat_narrator()._on_attack_rolled(attack("combat.attack.missed", roll=5, total=7))
        assert entry == {
            "kind": "mech",
            "text": "Flarg misses Kobold with sword: rolled 5+2 = 7 (needed 13).",
        }
        assert entry["text"].endswith("(needed 13).")

    def test_auto_hit_reads_helpless_and_touches_no_roll(self):
        entry = combat_narrator()._on_attack_rolled(
            attack(
                "combat.attack.auto_hit",
                attack_name="dagger",
                roll=None,
                total=None,
                required=None,
                defender_ac=None,
            )
        )
        assert entry == {
            "kind": "mech",
            "text": "Flarg strikes the helpless Kobold with dagger — no roll needed.",
        }

    def test_natural_twenty_tags_the_hit(self):
        entry = combat_narrator()._on_attack_rolled(
            attack("combat.attack.hit", roll=20, modifier=-1, total=19, natural=20)
        )
        assert entry["text"] == "Flarg hits Kobold with sword: rolled 20-1 = 19 (natural 20)."

    def test_natural_one_tags_the_miss_before_needed(self):
        entry = combat_narrator()._on_attack_rolled(
            attack("combat.attack.missed", roll=1, modifier=0, total=1, natural=1)
        )
        assert entry["text"] == "Flarg misses Kobold with sword: rolled 1+0 = 1 (natural 1) (needed 13)."

    def test_dispatched_by_event_type(self):
        # The generic `_render_event` fallback routes attacks to `_on_attack_rolled`.
        entry = combat_narrator()._render_event(attack("combat.attack.hit"))
        assert entry["text"] == "Flarg hits Kobold with sword: rolled 15+2 = 17."


def search(kind, *found, code="exploration.search.found"):
    """A `search_completed` event as the engine emits it, with its raw find tokens."""
    return event(
        "search_completed",
        code,
        character_id="character-0001",
        kind=kind,
        found=tuple(found),
    )


class TestSearchFinds:
    """The engine reports finds as `kind:detail` tokens — never player-facing text.

    `secret_door:{direction}` carries something the party perceives; `room_trap:{id}`
    and `construction:{id}` carry internal area and feature ids, which must not reach
    the transcript.
    """

    def test_secret_door_reads_with_its_direction(self):
        entry = combat_narrator()._render_event(search("secret_doors", "secret_door:east"))
        assert entry == {
            "kind": "treasure",
            "text": "Flarg searches for secret doors — and finds a secret door to the east!",
        }

    def test_room_trap_never_names_its_area(self):
        entry = combat_narrator()._render_event(search("room_traps", "room_trap:4"))
        assert entry["text"] == "Flarg searches for traps — and finds a hidden trap!"
        assert "room_trap" not in entry["text"]
        assert "4" not in entry["text"]

    def test_construction_never_names_its_feature(self):
        entry = combat_narrator()._render_event(search("construction", "construction:feature-2"))
        assert entry["text"] == ("Flarg searches for odd construction — and finds a stretch of odd construction!")
        assert "feature-2" not in entry["text"]

    def test_two_finds_read_as_a_sentence(self):
        entry = combat_narrator()._render_event(search("secret_doors", "secret_door:north", "secret_door:west"))
        assert entry["text"] == (
            "Flarg searches for secret doors — and finds a secret door to the north and a secret door to the west!"
        )

    def test_three_finds_take_a_serial_comma(self):
        entry = combat_narrator()._render_event(
            search(
                "secret_doors",
                "secret_door:north",
                "room_trap:4",
                "construction:feature-2",
            )
        )
        assert entry["text"] == (
            "Flarg searches for secret doors — and finds a secret door to the north, "
            "a hidden trap, and a stretch of odd construction!"
        )

    def test_repeated_phrases_collapse(self):
        entry = combat_narrator()._render_event(
            search("construction", "construction:feature-2", "construction:feature-9")
        )
        assert entry["text"] == ("Flarg searches for odd construction — and finds a stretch of odd construction!")

    def test_unknown_token_shape_degrades_instead_of_leaking(self):
        entry = combat_narrator()._render_event(search("secret_doors", "portcullis:winch-7"))
        assert entry["text"] == ("Flarg searches for secret doors — and finds something out of place!")
        assert "portcullis" not in entry["text"]
        assert "winch-7" not in entry["text"]

    def test_a_token_with_no_detail_at_all_degrades(self):
        entry = combat_narrator()._render_event(search("secret_doors", "mystery"))
        assert entry["text"].endswith("finds something out of place!")

    def test_secret_door_without_a_known_direction_drops_the_bearing(self):
        entry = combat_narrator()._render_event(search("secret_doors", "secret_door:widdershins"))
        assert entry["text"] == ("Flarg searches for secret doors — and finds a secret door!")
        assert "widdershins" not in entry["text"]

    def test_nothing_found_says_so(self):
        entry = combat_narrator()._render_event(search("room_traps", code="exploration.search.nothing"))
        assert entry == {
            "kind": "mech",
            "text": "Flarg searches for traps and finds nothing.",
        }

    def test_found_code_with_an_empty_tuple_falls_back_to_nothing(self):
        entry = combat_narrator()._render_event(search("secret_doors"))
        assert entry["text"] == "Flarg searches for secret doors and finds nothing."


def trap(code="exploration.trap.sprung", character_id="character-0001"):
    return event(
        "trap",
        code,
        visibility=Visibility.PLAYER,
        trap_ref="area:4",
        character_id=character_id,
    )


def save(passed=True, target="character-0001", category="breath"):
    """One `SavingThrowRolledEvent` as `osrlib.core.combat.saving_throw` emits it."""
    return event(
        "saving_throw_rolled",
        "combat.save.passed" if passed else "combat.save.failed",
        visibility=Visibility.PLAYER,
        target_id=target,
        category=category,
        roll=17 if passed else 1,
        modifier=0,
        required=15,
    )


def damage(target="character-0001", amount=3):
    return event(
        "damage_dealt",
        "combat.damage.dealt",
        visibility=Visibility.PLAYER,
        target_id=target,
        attacker_id=None,
        amount=amount,
        rolls=(amount,),
    )


def hit_points(target="character-0001"):
    """The referee-only report `deal_damage` puts after every damage event."""
    return event(
        "hit_points_reported",
        "combat.state.hit_points",
        visibility=Visibility.REFEREE,
        target_id=target,
        current=0,
        maximum=3,
    )


def condition_gained(target="character-0001", condition="blind"):
    return event(
        "condition_gained",
        "effects.condition.gained",
        visibility=Visibility.PLAYER,
        target_id=target,
        condition=condition,
    )


def effect_attached(kind="trap_blind"):
    return event(
        "effect_attached",
        "effects.effect.attached",
        visibility=Visibility.REFEREE,
        kind=kind,
    )


class TestSprungTrapOutcome:
    """A sprung trap that harms nobody has to say so.

    `osrlib.crawl.exploration._resolve_trap` has three paths that emit the sprung
    event and then nothing at all — a save that negates, an effect with no
    `damage_dice`, and halved damage that rounds to zero — and the transcript used
    to leave "unharmed" to be inferred from the silence that followed. The player
    read a trap firing on their fighter with no consequence; the LLM narrator,
    handed the same lines as `just_now`, wrote the consequence itself.
    """

    def test_a_negated_trap_says_no_harm(self):
        # The `on_save == "negates"` path: `_resolve_trap` continues past the
        # victim without rolling damage at all.
        assert [e["text"] for e in combat_narrator().render([trap(), save()])] == [
            "A trap springs on Flarg!",
            "Flarg passes a save versus breath: rolled 17+0, needing 15.",
            "The trap does no harm.",
        ]

    def test_a_trap_with_no_damage_dice_says_no_harm(self):
        # A `manual` (prose-only) trap: the sprung event is the entire resolution,
        # so before the fix this rendered as one line and nothing else.
        assert combat_narrator().render([trap()]) == [
            {"kind": "danger", "text": "A trap springs on Flarg!"},
            {"kind": "mech", "text": "The trap does no harm."},
        ]

    def test_halved_damage_that_rounds_to_zero_says_no_harm(self):
        # `total //= 2` on a 1 is 0, and `deal_damage` is only called for a
        # positive total — the same shape on the wire as a negated save.
        entries = combat_narrator().render([trap(), save(passed=True)])
        assert entries[-1] == {"kind": "mech", "text": "The trap does no harm."}

    def test_the_verdict_follows_the_save_that_earned_it(self):
        # The save is the evidence; announcing the verdict first would read as a
        # ruling before the roll.
        texts = [e["text"] for e in combat_narrator().render([trap(), save()])]
        assert texts.index("The trap does no harm.") > texts.index(
            "Flarg passes a save versus breath: rolled 17+0, needing 15."
        )

    def test_damage_gets_no_reassurance(self):
        entries = combat_narrator().render([trap(), save(passed=False), damage(), hit_points()])
        assert [e["text"] for e in entries] == [
            "A trap springs on Flarg!",
            "Flarg fails a save versus breath: rolled 1+0, needing 15.",
            "Flarg takes 3 damage.",
        ]
        assert not any("no harm" in e["text"] for e in entries)

    def test_a_condition_only_trap_gets_no_reassurance(self):
        # Blindness costs no hit points but is emphatically harm, and its first
        # event is the referee-only attachment — which is why the scan stops on
        # referee events too.
        entries = combat_narrator().render([trap(), effect_attached(), condition_gained()])
        assert not any("no harm" in e["text"] for e in entries)

    def test_a_kills_trap_gets_no_reassurance(self):
        # `kills` skips damage entirely: the resolution is the save, then the
        # death, with no `damage_dealt` between them.
        entries = combat_narrator().render(
            [
                trap(),
                save(passed=False, category="death"),
                condition_gained(condition="dead"),
                event(
                    "death",
                    "combat.death.died",
                    visibility=Visibility.PLAYER,
                    target_id="character-0001",
                ),
            ]
        )
        assert [e["text"] for e in entries][-1] == "Flarg is slain!"
        assert not any("no harm" in e["text"] for e in entries)

    def test_a_slide_trap_gets_no_reassurance(self):
        # A `transition` effect relocates the party and emits nothing else — the
        # trap plainly did something, so `location_entered` counts as a
        # consequence.
        entries = combat_narrator().render(
            [
                trap(),
                event(
                    "location_entered",
                    "exploration.location.entered",
                    visibility=Visibility.PLAYER,
                    location_kind="level",
                    location_id="delve",
                    level_number=2,
                    narrative=None,
                ),
            ]
        )
        assert not any("no harm" in e["text"] for e in entries)

    def test_a_party_trap_that_hurts_the_last_member_gets_no_reassurance(self):
        # `affects: "party"` interleaves save and consequence per victim, so the
        # scan has to walk the whole run of saves before judging.
        entries = combat_narrator().render(
            [
                trap(),
                save(target="character-0001"),
                save(target="character-0002"),
                save(passed=False, target="character-0003"),
                damage(target="character-0003"),
            ]
        )
        assert not any("no harm" in e["text"] for e in entries)

    def test_a_party_trap_everyone_survives_says_no_harm(self):
        entries = combat_narrator().render([trap(), save(target="character-0001"), save(target="character-0002")])
        assert entries[-1]["text"] == "The trap does no harm."

    def test_an_unrelated_event_after_the_trap_does_not_suppress_it(self):
        # The commonest harmless case in play: a trapped cache whose needle missed,
        # followed straight away by the haul it was guarding.
        entries = combat_narrator().render(
            [
                trap(),
                save(),
                event(
                    "item_acquired",
                    "exploration.item.acquired",
                    visibility=Visibility.PLAYER,
                    character_id="character-0001",
                    item_ids=("torch",),
                    coins_gp_value=0,
                ),
            ]
        )
        assert [e["text"] for e in entries][2] == "The trap does no harm."
        assert entries[3]["text"].startswith("Flarg takes")

    def test_the_other_trap_codes_are_untouched(self):
        assert combat_narrator().render([trap("exploration.trap.safe")]) == [
            {"kind": "mech", "text": "The trap fails to fire."}
        ]
        assert combat_narrator().render([trap("exploration.trap.found")]) == [
            {"kind": "mech", "text": "Flarg finds a trap!"}
        ]
        assert combat_narrator().render([trap("exploration.trap.removed")]) == [
            {"kind": "mech", "text": "Flarg disarms the trap."}
        ]

    def test_every_consequence_type_is_a_real_engine_event_type(self):
        """The whitelist is only as good as its spelling.

        `_TRAP_CONSEQUENCE` is matched against `event_type` strings, so one typo
        (or an engine rename) turns a harm signal into a silent no-op and prints
        "The trap does no harm." over a corpse. Every name in it must resolve to
        an event class osrlib actually declares.
        """
        import osrlib.core.events as core_events
        import osrlib.crawl.events as crawl_events

        declared = set()
        for module in (core_events, crawl_events):
            for value in vars(module).values():
                fields = getattr(value, "model_fields", None)
                if fields and "event_type" in fields:
                    declared.add(fields["event_type"].default)
        assert _TRAP_CONSEQUENCE <= declared, _TRAP_CONSEQUENCE - declared

    def test_the_verdict_survives_the_reload_interleave(self):
        """A reloaded transcript must read exactly like the live one.

        `GET /api/games/{id}` rebuilds the log through
        [`interleave`][server.narration.interleave], which renders the longest runs
        its splice points allow. A trap's resolution has to stay inside one run or
        the reassurance would appear only on one of the two paths.
        """
        events = [trap(), save()]
        live = combat_narrator().render(events)
        passages = [Passage(seq=1, anchor=2, text="A dart rings off the stone.")]
        reloaded = interleave(combat_narrator().render, events, passages)
        assert reloaded == live + [{"kind": "narrative", "text": "A dart rings off the stone."}]


class TestDeathWording:
    def test_non_member_is_killed(self):
        entry = combat_narrator()._render_event(event("death", "combat.death.died", target_id="monster-0001"))
        assert entry == {"kind": "mech", "text": "Kobold is killed."}

    def test_member_is_slain(self):
        entry = combat_narrator()._render_event(event("death", "combat.death.died", target_id="character-0001"))
        assert entry == {"kind": "danger", "text": "Flarg is slain!"}


MARCHING_ORDER = ("Corwin", "Brunhild", "Meriel", "Fox", "Aravel", "Zindal")

BERYL = SimpleNamespace(
    instance_id="valuable-0001",
    kind="gem",
    name="A river-green beryl the size of a thumbnail",
)


def haul_narrator(names=MARCHING_ORDER, valuables=(BERYL,)):
    """A `Narrator` over a party that can hold treasure.

    Members carry an inventory because `_item_name` falls back to walking the
    party for anything the equipment tables do not know — which is how a rolled
    gem or a piece of jewellery gets its authored name into the transcript.
    """
    members = [
        SimpleNamespace(
            id=f"character-{index + 1:04d}",
            name=name,
            inventory=SimpleNamespace(valuables=list(valuables), items=[]),
        )
        for index, name in enumerate(names)
    ]
    session = SimpleNamespace(
        party=SimpleNamespace(members=members),
        combatant=lambda ref: None,
        encounter=None,
        effective_equipment=load_equipment(),
    )
    return Narrator(session)


def acquired(member, item_ids=(), coins_gp_value=0):
    """One `ItemAcquiredEvent` as the engine emits it, by marching position."""
    return event(
        "item_acquired",
        "exploration.item.acquired",
        visibility=Visibility.PLAYER,
        character_id=f"character-{member + 1:04d}",
        item_ids=tuple(item_ids),
        coins_gp_value=coins_gp_value,
    )


def clock():
    """The referee-visible `time_advanced` the engine puts after every take."""
    return event(
        "time_advanced",
        "session.time.advanced",
        visibility=Visibility.REFEREE,
        n=1,
        unit="turn",
        rounds_total=60,
    )


class TestDistributedHaul:
    """`TakeTreasure` spreads a haul and emits one acquisition per member.

    Rendered one event each, a six-way split of a coin hoard is four identical
    lines of noise. The renderer collapses a run into one line: the coin is the
    party's, and only the members holding something worth naming are named.
    """

    def test_a_six_way_split_reads_as_one_line(self):
        # The capture adventure's tithe box: 90 gp + 240 sp, a silver dagger, a
        # vial of holy water and a beryl, across six members.
        events = [
            acquired(0, ("valuable-0001",), 19),
            acquired(1, (), 19),
            acquired(2, (), 19),
            acquired(3, ("silver_dagger", "holy_water"), 19),
            acquired(4, (), 19),
            acquired(5, (), 19),
        ]
        assert haul_narrator().render(events) == [
            {
                "kind": "treasure",
                "text": (
                    "The party splits 114 gp in coin. "
                    "Corwin takes A river-green beryl the size of a thumbnail; "
                    "Fox takes Silver dagger, Holy water (vial)."
                ),
            }
        ]

    def test_the_members_who_took_only_coin_are_not_named(self):
        entries = haul_narrator().render(
            [acquired(0, ("silver_dagger",), 19)] + [acquired(n, (), 19) for n in (1, 2, 3, 4, 5)]
        )
        text = entries[0]["text"]
        for name in ("Brunhild", "Meriel", "Fox", "Aravel", "Zindal"):
            assert name not in text
        assert "Corwin takes Silver dagger" in text

    def test_a_recipient_take_stays_a_plain_sentence(self):
        # `recipient_id` puts the whole haul on one character: one event, and the
        # collapsed form must not swallow it.
        entries = haul_narrator().render([acquired(1, ("silver_dagger",), 1561)])
        assert entries == [
            {
                "kind": "treasure",
                "text": "Brunhild takes Silver dagger and 1561 gp in coin.",
            }
        ]

    def test_a_party_of_one_stays_a_plain_sentence(self):
        entries = haul_narrator(names=("Corwin",)).render([acquired(0, (), 114)])
        assert entries == [{"kind": "treasure", "text": "Corwin takes 114 gp in coin."}]

    def test_a_coin_only_haul_is_the_coin_sentence_alone(self):
        entries = haul_narrator().render([acquired(n, (), 33) for n in range(6)])
        assert entries == [{"kind": "treasure", "text": "The party splits 198 gp in coin."}]

    def test_the_coin_figure_is_the_sum_of_the_shares(self):
        # 400 gp + 200 sp is 420 gp in the box, but each share floors its own
        # value to whole gold: four members hold 70 gp and two hold 69. The
        # events are all the renderer has, and 418 is what they add up to, the
        # same arithmetic six separate lines would otherwise leave to the reader.
        events = [acquired(n, (), 70) for n in range(4)] + [acquired(n, (), 69) for n in (4, 5)]
        assert haul_narrator().render(events)[0]["text"] == "The party splits 418 gp in coin."

    def test_shares_that_all_floor_to_nothing_never_read_as_nothing(self):
        # The bundled adventure's crib jar: 30 sp and 60 cp, worth 3 gp, split so
        # finely that every share's `coins_gp_value` is zero. Rendered one event
        # each that was four lines of "Brunhild takes nothing." — which is false.
        events = [
            acquired(0, ("tinder_box", "wine")),
            acquired(1),
            acquired(2),
            acquired(3),
            acquired(4),
        ]
        entries = haul_narrator().render(events)
        assert entries == [
            {
                "kind": "treasure",
                "text": ("The party splits the loose coin. Corwin takes Tinder box (flint & steel), Wine (2 pints)."),
            }
        ]
        assert "nothing" not in entries[0]["text"]

    def test_two_takes_in_a_row_stay_two_lines(self):
        # The turn's clock event sits between two takes, which is what keeps the
        # runs apart in a full-log render.
        events = [
            acquired(0, (), 19),
            acquired(1, (), 19),
            clock(),
            acquired(0, (), 40),
            acquired(1, (), 40),
        ]
        assert [entry["text"] for entry in haul_narrator().render(events)] == [
            "The party splits 38 gp in coin.",
            "The party splits 80 gp in coin.",
        ]

    def test_back_to_back_shop_purchases_stay_one_line_each(self):
        # The town panel posts one `purchase_equipment` per item and nothing
        # separates two of them in the log. Purchases name their goods and carry
        # no coin, so they never look like a split pot.
        events = [acquired(0, ("torch",)), acquired(1, ("rope",))]
        assert [entry["text"] for entry in haul_narrator().render(events)] == [
            "Corwin takes Torches (6).",
            "Brunhild takes Rope (50’).",
        ]

    def test_an_items_only_haul_across_two_members_stays_two_lines(self):
        # Deliberate: with no coin anywhere, a haul is indistinguishable from two
        # purchases, and one line per notable item is already what those lines are.
        events = [acquired(0, ("silver_dagger",)), acquired(3, ("holy_water",))]
        assert [entry["text"] for entry in haul_narrator().render(events)] == [
            "Corwin takes Silver dagger.",
            "Fox takes Holy water (vial).",
        ]

    def test_the_haul_line_keeps_its_place_among_the_other_events(self):
        events = [
            event(
                "door",
                "exploration.door.opened",
                visibility=Visibility.PLAYER,
                direction="north",
                character_id=None,
                narrative=None,
            ),
            acquired(0, (), 19),
            acquired(1, (), 19),
            event(
                "battle_started",
                "battle.started",
                visibility=Visibility.PLAYER,
            ),
        ]
        assert [entry["text"] for entry in haul_narrator().render(events)] == [
            "The door to the north swings open.",
            "The party splits 38 gp in coin.",
            "Battle is joined!",
        ]

    def test_the_collapse_survives_the_reload_interleave(self):
        """A reloaded transcript must read exactly like the live one.

        `GET /api/games/{id}` rebuilds the log through
        [`interleave`][server.narration.interleave] when narration is on. It used
        to hand the renderer one event at a time, which would have split every
        haul back into one line per member on reload.
        """
        events = [acquired(n, (), 19) for n in range(6)]
        live = haul_narrator().render(events)
        passages = [Passage(seq=1, anchor=6, text="Coin glints in the torchlight.")]
        reloaded = interleave(haul_narrator().render, events, passages)
        assert reloaded == live + [{"kind": "narrative", "text": "Coin glints in the torchlight."}]


class TestLeftBehind:
    """`exploration.item.left_behind`: the haul the party could not carry.

    A `system` line, not a `treasure` one — amber is the accent for what the party
    gained, and this is the opposite. Nothing is destroyed, so it reads as
    something still lying there.
    """

    @staticmethod
    def left_behind(item_ids=(), coins_gp_value=0):
        return event(
            "items_left_behind",
            "exploration.item.left_behind",
            visibility=Visibility.PLAYER,
            item_ids=tuple(item_ids),
            coins_gp_value=coins_gp_value,
        )

    def test_coins_only(self):
        assert haul_narrator().render([self.left_behind(coins_gp_value=439)]) == [
            {
                "kind": "system",
                "text": "The party cannot carry it all — 439 gp in coin stays where it lies.",
            }
        ]

    def test_items_and_coins(self):
        entry = haul_narrator().render([self.left_behind(("plate_mail", "torch", "torch"), 40)])[0]
        assert entry["text"] == (
            "The party cannot carry it all — Plate mail, Torches (6) ×2 and 40 gp in coin stays where it lies."
        )

    def test_it_never_falls_through_to_the_engine_message(self):
        # Without a handler this rendered grey `mech` text straight from
        # `osrlib.messages.format_message`, item ids and all.
        entry = haul_narrator().render([self.left_behind(("silver_dagger",))])[0]
        assert entry["kind"] == "system"
        assert "silver_dagger" not in entry["text"]
        assert entry["text"].startswith("The party cannot carry it all —")


class TestTownSpoils:
    """What the party gets for coming back alive.

    Both events here are built from the real osrlib classes rather than a fake:
    the defect these tests pin was a handler reading field names the event never
    declared, and a `SimpleNamespace` would have answered to those names happily.

    The engine was right on both counts the whole time — treasure XP was awarded
    and the gold did reach the purse. Only the transcript was wrong, which is the
    only place a player can see either.
    """

    def test_the_tally_names_both_pools_and_the_share(self):
        entry = haul_narrator()._render_event(
            AdventureXpAwardEvent(
                monster_xp=122,
                treasure_xp=442,
                share=94,
                survivors=tuple(f"character-{n + 1:04d}" for n in range(6)),
            )
        )
        assert entry == {
            "kind": "treasure",
            "text": ("The adventure's spoils are tallied: 122 XP from monsters and 442 XP from treasure, 94 each."),
        }

    def test_a_haul_that_earned_nothing_says_so_rather_than_going_quiet(self):
        # A party that fought its way out empty-handed. A line that read the
        # same either way would make a 442 XP haul look like a 0 XP one.
        entry = haul_narrator()._render_event(
            AdventureXpAwardEvent(monster_xp=122, treasure_xp=0, share=20, survivors=("character-0001",))
        )
        assert "0 XP from treasure" in entry["text"]

    def test_a_sale_reports_what_the_purse_actually_gained(self):
        entry = haul_narrator()._render_event(
            TreasureSoldEvent(
                character_id="character-0001",
                instance_ids=("valuable-0001", "valuable-0002"),
                gp_value=520,
            )
        )
        assert entry == {
            "kind": "treasure",
            "text": "Corwin sells treasure for 520 gp.",
        }

    def test_the_sale_line_names_the_seller_not_their_id(self):
        entry = haul_narrator()._render_event(
            TreasureSoldEvent(
                character_id="character-0004",
                instance_ids=("valuable-0001",),
                gp_value=9,
            )
        )
        assert entry["text"] == "Fox sells treasure for 9 gp."
        assert "character-0004" not in entry["text"]


class TestAdvancementLines:
    """The level-up flow's transcript lines, built from the real events.

    Real osrlib classes on purpose, like `TestTownSpoils`: these handlers bind
    to their models by naming convention alone, and a `SimpleNamespace` would
    answer happily to a field the event never declares.
    """

    def test_a_level_with_a_title_names_it(self):
        entry = haul_narrator()._render_event(
            CharacterLeveledUpEvent(
                character_id="character-0001",
                level_before=1,
                level_after=2,
                hp_gained=4,
                hp_roll=3,
                con_applied=True,
                title="Seer",
            )
        )
        assert entry == {
            "kind": "system",
            "text": "Corwin is now level 2 — Seer (+4 hp).",
        }

    def test_a_level_past_the_title_list_drops_the_clause(self):
        # The SRD's title lists run only through name level; past it the
        # event's title is None, which must never print as the word "None".
        entry = haul_narrator()._render_event(
            CharacterLeveledUpEvent(
                character_id="character-0002",
                level_before=14,
                level_after=15,
                hp_gained=1,
                hp_roll=None,
                con_applied=False,
                title=None,
            )
        )
        assert entry == {
            "kind": "system",
            "text": "Brunhild is now level 15 (+1 hp).",
        }
        assert "None" not in entry["text"]

    def test_an_inscribed_spell_reads_by_name(self):
        entry = haul_narrator()._render_event(SpellBookUpdatedEvent(caster_id="character-0001", spell_id="sleep"))
        assert entry == {
            "kind": "system",
            "text": "Corwin inscribes Sleep in their spell book.",
        }

    def test_every_book_rejection_reads_as_words(self):
        # `rejection_text` degrades to the raw code for anything unmapped, so
        # a missing entry would print `magic.book.duplicate` at the player.
        codes = (
            "magic.book.not_arcane",
            "magic.book.unknown_spell",
            "magic.book.wrong_list",
            "magic.book.duplicate",
            "magic.book.capacity_exceeded",
        )
        for code in codes:
            text = rejection_text(SimpleNamespace(code=code))
            assert text != code, code
            assert text[0].isupper() and text.endswith("."), code


class TestItemUse:
    """Magic-item use, identification, and curses — built from the real events.

    Real osrlib classes on purpose, like `TestTownSpoils`: these handlers bind
    to their models by naming convention alone, and a `SimpleNamespace` would
    answer happily to a field the event never declares. The instance behind a
    drunk potion leaves the inventory before render, so the names here come
    from the log's own `item_identified` line (cached by the narrator) or
    degrade to the item's kind — never to a raw instance id.
    """

    def test_a_drunk_potion_identifies_then_names_itself(self):
        # The engine's order: identification is first-meaningful-use, so the
        # identify event precedes the drink in the same run.
        entries = haul_narrator().render(
            [
                ItemIdentifiedEvent(instance_id="magic-item-0001", template_id="potion_of_healing"),
                ItemUsedEvent(
                    code="items.potion.drunk",
                    character_id="character-0001",
                    instance_id="magic-item-0001",
                ),
            ]
        )
        assert entries == [
            {"kind": "treasure", "text": "It is a Potion of Healing."},
            {"kind": "mech", "text": "Corwin drinks Potion of Healing."},
        ]

    def test_an_already_identified_potion_stays_masked_to_its_kind(self):
        # A narrator that never saw the identify line (the potion was known
        # before this render) still must not print the instance id.
        entries = haul_narrator().render(
            [
                ItemUsedEvent(
                    code="items.potion.drunk",
                    character_id="character-0001",
                    instance_id="magic-item-0001",
                )
            ]
        )
        assert entries == [{"kind": "mech", "text": "Corwin drinks a potion."}]
        assert "magic-item-0001" not in entries[0]["text"]

    def test_a_read_scroll_and_an_activated_device_take_their_own_verbs(self):
        narrator = haul_narrator()
        read = narrator._render_event(
            ItemUsedEvent(
                code="items.scroll.read",
                character_id="character-0002",
                instance_id="magic-item-0002",
            )
        )
        assert read == {"kind": "mech", "text": "Brunhild reads a scroll."}
        activated = narrator._render_event(
            ItemUsedEvent(
                code="items.device.activated",
                character_id="character-0002",
                instance_id="magic-item-0003",
            )
        )
        assert activated == {"kind": "mech", "text": "Brunhild activates an item."}

    def test_mixed_potions_and_cursed_scrolls_read_as_danger(self):
        narrator = haul_narrator()
        mixed = narrator._render_event(
            ItemUsedEvent(
                code="items.potion.mixed",
                character_id="character-0001",
                instance_id="magic-item-0001",
            )
        )
        assert mixed["kind"] == "danger"
        assert "Corwin" in mixed["text"]
        cursed = narrator._render_event(
            ItemUsedEvent(
                code="items.scroll.cursed",
                character_id="character-0001",
                instance_id="magic-item-0002",
            )
        )
        assert cursed == {
            "kind": "danger",
            "text": "The scroll Corwin reads is cursed!",
        }

    def test_identification_says_it_out_loud(self):
        entry = haul_narrator()._render_event(
            ItemIdentifiedEvent(instance_id="magic-item-0004", template_id="ring_of_weakness")
        )
        assert entry == {"kind": "treasure", "text": "It is a Ring of Weakness."}

    def test_a_curse_reveals_by_name_not_id(self):
        entry = haul_narrator()._render_event(
            CurseRevealedEvent(
                character_id="character-0004",
                instance_id="magic-item-0005",
                template_id="sword_minus_1_cursed",
            )
        )
        assert entry == {
            "kind": "danger",
            "text": "A curse — Fox cannot be rid of the Sword -1, Cursed.",
        }
        assert "magic-item" not in entry["text"]

    def test_a_treasure_trap_search_reads_as_words(self):
        # The inspect command's finds-nothing path emits a `search_completed`
        # with kind `treasure_traps`, which must not print as a raw token.
        entry = combat_narrator()._render_event(search("treasure_traps", code="exploration.search.nothing"))
        assert entry == {
            "kind": "mech",
            "text": "Flarg searches for treasure traps and finds nothing.",
        }


class TestMonstersLeftBehind:
    """The line that explains a mid-rout split: runners named, abandoned counted.

    Built on the real event class so the construction itself pins the field
    names (`source_group_id`, `count`), not just the AST scan below.
    """

    def test_names_the_runners_and_counts_the_abandoned(self):
        narrator = make_narrator()
        narrator._labels["group-0001"] = "Kobold"
        entry = narrator._on_monsters_left_behind(
            MonstersLeftBehindEvent(group_id="group-0002", source_group_id="group-0001", count=2)
        )
        assert entry == {
            "kind": "mech",
            "text": "Kobold abandon 2 helpless fellows where they lie.",
        }

    def test_one_abandoned_reads_singular(self):
        narrator = make_narrator()
        narrator._labels["group-0001"] = "Kobold"
        entry = narrator._on_monsters_left_behind(
            MonstersLeftBehindEvent(group_id="group-0002", source_group_id="group-0001", count=1)
        )
        assert entry["text"] == "Kobold abandon 1 helpless fellow where they lie."


REFUSAL = "The wicket is banded in lead. Nothing the party carries answers it."
GATE_CODES = ("exploration.door.gate_refused", "exploration.transition.gate_refused")


class TestGateRefusals:
    """An authored gate refusal is the author's own sentence, rendered as written.

    `rejection_text` reads `params["refusal"]` before the code table. The engine
    puts the beat there on the two gate-refusing codes and omits the key entirely
    when the author wrote none, so the mapped fallback and the authored line can
    never compete. Built from the real `Rejection` model, because the params
    shape *is* the mechanism.
    """

    def test_both_gate_codes_render_the_authored_line_verbatim(self):
        for code in GATE_CODES:
            rejection = Rejection(code=code, params={"direction": "south", "refusal": REFUSAL})
            assert rejection_text(rejection) == REFUSAL, code

    def test_the_code_string_never_reaches_the_player(self):
        for code in GATE_CODES:
            text = rejection_text(Rejection(code=code, params={"refusal": REFUSAL}))
            assert "gate_refused" not in text, code
            assert code not in text, code

    def test_an_unauthored_gate_falls_back_to_the_mapped_words(self):
        # The engine omits the key rather than sending an empty beat.
        for code in GATE_CODES:
            text = rejection_text(Rejection(code=code, params={"direction": "north"}))
            assert text != code, code
            assert text[0].isupper() and text.endswith("."), code

    def test_an_empty_refusal_falls_back_too(self):
        # Belt and braces: an empty string is not a line the referee would say.
        for code in GATE_CODES:
            text = rejection_text(Rejection(code=code, params={"refusal": ""}))
            assert text != code and text != "", code

    def test_a_rejection_with_no_params_still_renders(self):
        # Every other caller passes a plain rejection; `params` defaults to {}.
        assert rejection_text(SimpleNamespace(code="exploration.door.locked")) == ("The door is locked.")

    def test_every_mapped_code_reads_as_words(self):
        # The whole table, not a hand-kept list of the codes this change added:
        # `rejection_text` degrades to the raw code for anything unmapped, so an
        # entry that is blank, lowercase, or punctuation-less prints engine
        # vocabulary at the player, and a hard-coded list only ever covers the
        # codes somebody remembered to add to it.
        assert _REJECTION_TEXT, "an empty table would make this vacuous"
        for code, text in _REJECTION_TEXT.items():
            assert rejection_text(SimpleNamespace(code=code)) == text, code
            assert text != code, code
            assert text[0].isupper(), code
            assert text.endswith("."), code

    def test_the_two_gate_fallbacks_read_exactly(self):
        # These two are the unauthored-gate wording specifically, so they are
        # pinned by string rather than only by shape.
        assert _REJECTION_TEXT["exploration.door.gate_refused"] == ("The door will not open for the party.")
        assert _REJECTION_TEXT["exploration.transition.gate_refused"] == ("The way is barred to the party.")


class TestAuthoredLayerLines:
    """The authored layer's new events, built from the real osrlib classes.

    Real classes on purpose, like `TestTownSpoils` and `TestItemUse`: these
    handlers bind to their models by naming convention alone, and a
    `SimpleNamespace` answers happily to a field the event never declares.
    """

    def test_a_journal_entry_is_the_authored_text_and_nothing_else(self):
        entry = make_narrator()._render_event(JournalEntryAddedEvent(text="The adit breathes out cold.", rounds=120))
        assert entry == {"kind": "prose", "text": "The adit breathes out cold."}
        assert "120" not in entry["text"]

    def test_an_activated_quest_states_the_fact_then_speaks_the_offer(self):
        entries = make_narrator()._render_event(
            QuestActivatedEvent(
                quest_id="the-vein",
                name="The pay-vein",
                narrative="The headman wants the captain's day-book.",
            )
        )
        assert entries == [
            {"kind": "system", "text": "New quest: The pay-vein."},
            {"kind": "prose", "text": "The headman wants the captain's day-book."},
        ]

    def test_an_unauthored_activation_is_the_fact_alone(self):
        entries = make_narrator()._render_event(QuestActivatedEvent(quest_id="the-vein", name="The pay-vein"))
        assert entries == [{"kind": "system", "text": "New quest: The pay-vein."}]

    def test_objective_beats_render_as_prose_and_name_no_ids(self):
        narrator = make_narrator()
        for model in (ObjectiveRevealedEvent, ObjectiveCompletedEvent):
            entry = narrator._render_event(
                model(
                    quest_id="the-vein",
                    quest_name="The pay-vein",
                    objective_id="find-day-book",
                    name="Find the day-book",
                    narrative="The day-book comes up out of the strongbox.",
                )
            )
            assert entry == {
                "kind": "prose",
                "text": "The day-book comes up out of the strongbox.",
            }

    def test_unauthored_objective_lines_name_the_objective_and_its_quest(self):
        # The engine fills `name` with the objective's authored name or its id,
        # so the label the event carries is the label to print — never the
        # quest alone, and never an id fished out of the wiring fields.
        narrator = make_narrator()
        texts = [
            narrator._render_event(
                model(
                    quest_id="the-vein",
                    quest_name="The pay-vein",
                    objective_id="find-day-book",
                    name="Find the day-book",
                )
            )["text"]
            for model in (ObjectiveRevealedEvent, ObjectiveCompletedEvent)
        ]
        assert texts == [
            "New objective: Find the day-book — The pay-vein.",
            "Objective complete: Find the day-book — The pay-vein.",
        ]

    def test_an_unnamed_objective_prints_the_label_the_engine_gave_it(self):
        # A document authoring no objective name gets `name=objective.id` from
        # the engine, and the slug is then the thing's own label. Nothing here
        # prettifies it, and nothing compares it to `objective_id` to decide.
        entry = make_narrator()._render_event(
            ObjectiveCompletedEvent(
                quest_id="the-vein",
                quest_name="The pay-vein",
                objective_id="find-day-book",
                name="find-day-book",
            )
        )
        assert entry == {
            "kind": "system",
            "text": "Objective complete: find-day-book — The pay-vein.",
        }

    def test_the_ending_speaks_its_beat_exactly_once(self):
        # osrlib emits both terminal events carrying the *same* completion beat,
        # and the journal records it once. The transcript must too.
        beat = "The almoner counts out the reward without looking up."
        entries = make_narrator().render(
            [
                QuestCompletedEvent(quest_id="the-vein", name="The pay-vein", narrative=beat),
                AdventureCompletedEvent(quest_id="the-vein", name="The pay-vein", narrative=beat),
            ]
        )
        assert entries == [
            {"kind": "system", "text": "Quest complete: The pay-vein."},
            {"kind": "prose", "text": beat},
            {"kind": "system", "text": "The adventure is won — The pay-vein is done."},
        ]
        assert [entry["text"] for entry in entries].count(beat) == 1

    def test_a_toll_names_the_item_it_took(self):
        entry = haul_narrator()._render_event(ItemConsumedEvent(character_id="character-0001", item_id="iron_spikes"))
        assert entry == {"kind": "mech", "text": "Corwin gives up Iron spikes (12)."}

    def test_an_unresolvable_toll_never_prints_an_instance_id(self):
        entry = haul_narrator()._render_event(
            ItemConsumedEvent(character_id="character-0001", item_id="magic-item-0007")
        )
        assert entry == {"kind": "mech", "text": "Corwin gives up an item."}
        assert "magic-item" not in entry["text"]

    def test_a_bundled_item_named_after_its_own_id_still_reads(self):
        # An adventure may bundle `{"id": "rope", "name": "rope"}`. Deciding the
        # lookup failed by comparing its answer to the id would throw that name
        # away and print "an item" for an item the catalog knew perfectly well.
        narrator = haul_narrator()
        narrator._equipment = SimpleNamespace(get=lambda item_id: SimpleNamespace(name=item_id))
        entry = narrator._render_event(ItemConsumedEvent(character_id="character-0001", item_id="rope"))
        assert entry == {"kind": "mech", "text": "Corwin gives up rope."}

    def test_an_identified_toll_takes_the_name_the_log_already_said(self):
        entries = haul_narrator().render(
            [
                ItemIdentifiedEvent(instance_id="magic-item-0007", template_id="potion_of_healing"),
                ItemConsumedEvent(character_id="character-0001", item_id="magic-item-0007"),
            ]
        )
        assert entries[-1] == {
            "kind": "mech",
            "text": "Corwin gives up Potion of Healing.",
        }

    def test_a_wedge_reports_one_line_not_two(self):
        # `WedgeDoor` emits the spike's consumption and the door event; the door
        # line already names the spike.
        entries = haul_narrator().render(
            [
                ItemConsumedEvent(character_id="character-0002", item_id="iron_spikes"),
                DoorEvent(code="exploration.door.wedged", x=2, y=3, direction="north"),
            ]
        )
        assert entries == [{"kind": "mech", "text": "An iron spike wedges the door to the north."}]

    def test_a_toll_before_an_opened_door_still_reports(self):
        # Keyed to the wedged code, not to the item: a gate's toll is paid at the
        # threshold and its event lands immediately before the door's own.
        entries = haul_narrator().render(
            [
                ItemConsumedEvent(character_id="character-0001", item_id="holy_water"),
                DoorEvent(code="exploration.door.opened", x=2, y=3, direction="north"),
            ]
        )
        assert entries == [
            {"kind": "mech", "text": "Corwin gives up Holy water (vial)."},
            {"kind": "mech", "text": "The door to the north swings open."},
        ]

    def test_a_doors_success_beat_follows_its_mechanical_line(self):
        beat = "Saint Verrow's token fits the stamp in the lead band."
        entries = make_narrator()._render_event(
            DoorEvent(
                code="exploration.door.opened",
                x=11,
                y=3,
                direction="south",
                narrative=beat,
            )
        )
        assert entries == [
            {"kind": "mech", "text": "The door to the south swings open."},
            {"kind": "prose", "text": beat},
        ]

    def test_an_arrivals_success_beat_rides_both_crossing_kinds(self):
        beat = "The stair accepts the token and lets the party through."
        narrator = make_narrator()
        narrator.session.adventure = SimpleNamespace(dungeon=lambda _: SimpleNamespace(name="The Cold Vein"))
        level = narrator._render_event(
            LocationEnteredEvent(
                location_kind="level",
                location_id="cold-vein",
                level_number=2,
                narrative=beat,
            )
        )
        assert level == [
            {"kind": "system", "text": "The party comes to level 2."},
            {"kind": "prose", "text": beat},
        ]
        dungeon = narrator._render_event(
            LocationEnteredEvent(
                location_kind="dungeon",
                location_id="cold-vein",
                level_number=1,
                narrative=beat,
            )
        )
        assert dungeon[-1] == {"kind": "prose", "text": beat}

    def test_a_level_arrival_never_claims_a_direction(self):
        # The bundled up-stair (level 2 → level 1) climbs, and the event states
        # only which level was reached. "Descends" was a guess and the up-stair
        # made it a lie; the means waits on mmacy/osrlib-python#68.
        narrator = make_narrator()
        entry = narrator._render_event(
            LocationEnteredEvent(location_kind="level", location_id="cold-vein", level_number=1)
        )
        assert entry == {"kind": "system", "text": "The party comes to level 1."}
        for word in ("descend", "climb", "up", "down"):
            assert word not in entry["text"].lower()


def bundled_narrator(monkeypatch, position=None):
    """A `Narrator` over the bundled document and a fake session.

    `OSR_WEB_ADVENTURE` is cleared: exported, `load_adventure()` would return
    somebody else's document and the cold-vein assertions below would either fail
    or — worse — pass over an adventure that happens to have no area `9` at all.

    With `position` given the session carries a live cold-vein location; without
    it, it carries **no `dungeon_state` attribute at all**, so anything that
    reaches for the party's whereabouts raises rather than quietly answering.
    """
    monkeypatch.delenv("OSR_WEB_ADVENTURE", raising=False)
    session = SimpleNamespace(
        party=SimpleNamespace(members=[]),
        combatant=lambda ref: None,
        encounter=None,
        effective_equipment=load_equipment(),
        adventure=load_adventure(),
    )
    if position is not None:
        session.dungeon_state = SimpleNamespace(
            location=PartyLocation(
                kind="dungeon",
                dungeon_id="cold-vein",
                level_number=1,
                position=position,
                facing=Direction.NORTH,
            )
        )
    return Narrator(session)


def area_event(**overrides):
    fields = {
        "location_kind": "area",
        "location_id": "9",
        "level_number": 1,
        "dungeon_id": "cold-vein",
    }
    fields.update(overrides)
    return LocationEnteredEvent(**fields)


class TestAreaResolutionFollowsTheEvent:
    """Area prose resolves off the event's own facts, with no fallback at all.

    Two halves. The structural one: a fake session with no `dungeon_state`, so a
    renderer that consulted the party's whereabouts would raise. The killing one:
    a session whose location is a perfectly good cold-vein area, fed an event
    that names none — a guarded fallback would answer from the location and
    render a room; the honest renderer renders nothing.
    `tests/test_authored_layer.py` drives a real `place_party` for the
    behavioural half.
    """

    def test_the_event_names_the_area(self, monkeypatch):
        entries = bundled_narrator(monkeypatch)._render_event(area_event())
        assert entries[0] == {"kind": "place", "text": "The niche of Saint Verrow"}
        assert entries[1]["kind"] == "prose"

    def test_an_event_without_a_dungeon_id_renders_nothing_at_all(self, monkeypatch):
        # A pre-1.5.0 save's persisted log: the field is additive, so it
        # deserializes as None and no migration ever stamps it. Not "the place
        # header without the prose" — nothing, and `seen_areas` stays empty, so
        # the first entry after the upgrade prints the description in full.
        narrator = bundled_narrator(monkeypatch)
        assert narrator._render_event(area_event(dungeon_id=None, level_number=None)) is None
        assert narrator.seen_areas == set()

    def test_a_live_location_never_fills_in_a_missing_dungeon_id(self, monkeypatch):
        # The killing pin. The party stands in area 9 of cold-vein level 1, and
        # the event names no dungeon: a `getattr`-guarded fallback would resolve
        # against the location and print the niche. There is no fallback.
        narrator = bundled_narrator(monkeypatch, position=(8, 2))
        assert narrator.session.dungeon_state.location.kind == "dungeon"
        assert narrator._render_event(area_event(dungeon_id=None, level_number=None)) is None
        assert narrator.seen_areas == set()

    def test_an_unknown_dungeon_or_level_renders_nothing(self, monkeypatch):
        narrator = bundled_narrator(monkeypatch)
        assert narrator._render_event(area_event(dungeon_id="elsewhere")) is None
        assert narrator._render_event(area_event(level_number=9)) is None

    def test_the_first_visit_marks_the_modules_own_prose(self, monkeypatch):
        # `prose` is the voice of every authored beat now, so the module's room
        # description carries a marker; `server/narration.py` replaces text by
        # that marker and never by adjacency to a place header.
        entries = bundled_narrator(monkeypatch)._render_event(area_event())
        assert entries[1][MODULE_PROSE] is True
        assert MODULE_PROSE not in entries[0]

    def test_the_narrator_names_items_through_the_engines_own_catalog(self):
        # `session.effective_equipment` is shipped ∪ adventure-bundled; the
        # shipped catalog alone printed a bundled item's raw id everywhere.
        catalog = load_equipment()
        session = SimpleNamespace(
            party=SimpleNamespace(members=[]),
            combatant=lambda ref: None,
            encounter=None,
            effective_equipment=catalog,
        )
        assert Narrator(session)._equipment is catalog


class TestRoomTrapFinds:
    """A found room trap reads as a find and states no bearing.

    osrlib 1.5.0 widened a `room_traps` search to the searched cell's door edges,
    so a find can be a trap in the room beyond a door — but
    `SearchCompletedEvent` carries no position, and the party's *current*
    position is not the searched one on any whole-log re-render (a page reload, a
    rename, a restore). A renderer that reconstructed the bearing from the
    session was reproducibly wrong: a trap found underfoot re-rendered from the
    corridor as "a trap rigged to the south door", a bearing the engine cannot
    produce for an enter-trigger trap at all — and the walk had no way to honour
    `_reveal`'s own guards, so it could point at an undiscovered secret door.

    Stating the bearing is the engine's job (mmacy/osrlib-python#67). Until the
    token carries it, the renderer renders what the event states.
    """

    def test_a_room_trap_reads_as_a_find_and_names_no_door(self):
        phrase = _found_phrase("room_trap:5")
        assert phrase == "a hidden trap"
        assert "door" not in phrase
        assert "5" not in phrase

    def test_the_phrase_never_consults_the_session(self, monkeypatch):
        # Same token, two sessions: one with no `dungeon_state` at all (a
        # reach for it would be a loud AttributeError), one standing in the
        # corridor outside the powder magazine's door. Same words.
        without = bundled_narrator(monkeypatch)
        assert not hasattr(without.session, "dungeon_state")
        standing = bundled_narrator(monkeypatch, position=(13, 9))
        found = search("room_traps", "room_trap:5")
        assert without._render_event(found) == standing._render_event(found)
        assert "door" not in standing._render_event(found)["text"]

    def test_the_resolver_takes_no_session_at_all(self):
        # Structural: a module function cannot consult a location by accident.
        assert list(signature(_found_phrase).parameters) == ["token"]


class TestHandlersReadRealFields:
    """Every `_on_*` handler must read only fields its event class declares.

    `Narrator._render_event` dispatches on `event.event_type`, so a handler and an
    event model are bound by a naming convention and nothing else. Reading a field
    the event does not have is therefore not a type error anywhere — with a
    `getattr` default it is not even an `AttributeError`. It silently renders the
    default: `_on_treasure_sold` asked for `coins_gp_value` and `value_gp` where
    the event declares `gp_value`, so every sale in the game reported 0 gp, which
    is exactly what a worthless gem would report.
    """

    @staticmethod
    def event_classes() -> dict[str, type[Event]]:
        """Every declared event class, keyed by the `event_type` it dispatches on."""
        classes: dict[str, type[Event]] = {}
        for module in (core_events, crawl_events):
            for value in vars(module).values():
                if not (inspect.isclass(value) and issubclass(value, Event)):
                    continue
                field = value.model_fields.get("event_type")
                if field is not None and field.default:
                    classes[field.default] = value
        return classes

    @staticmethod
    def fields_read(handler) -> set[str]:
        """The attribute names a handler reads off its `event` argument.

        Catches both `event.name` and `getattr(event, "name", ...)` — the second
        being the shape that turns a rename into a silently wrong number.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "event":
                names.add(node.attr)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "event"
                and isinstance(node.args[1], ast.Constant)
            ):
                names.add(node.args[1].value)
        return names

    def test_every_dispatched_handler_reads_only_declared_fields(self):
        classes = self.event_classes()
        unknown: dict[str, set[str]] = {}
        for name in dir(Narrator):
            if not name.startswith("_on_"):
                continue
            model = classes.get(name[len("_on_") :])
            if model is None:
                continue  # `_on_haul` renders a run, not one event
            missing = self.fields_read(getattr(Narrator, name)) - set(model.model_fields)
            if missing:
                unknown[name] = missing
        assert not unknown, unknown

    def test_the_scan_would_have_caught_the_defect(self):
        """The guard is worthless if it cannot see the bug it was written for."""

        def _on_treasure_sold(self, event):  # the wrong field name the scan catches
            value = getattr(event, "coins_gp_value", None) or getattr(event, "value_gp", 0)
            return value

        missing = self.fields_read(_on_treasure_sold) - set(TreasureSoldEvent.model_fields)
        assert missing == {"coins_gp_value", "value_gp"}

    def test_every_handler_but_the_run_collapser_is_reachable(self):
        """A handler whose `event_type` no event carries is dead code.

        The convention cuts both ways: a misspelled handler name never runs and
        the event falls through to `osrlib.messages.format_message`, which prints
        raw entity ids into the transcript.
        """
        classes = self.event_classes()
        orphans = {
            name[len("_on_") :]
            for name in dir(Narrator)
            if name.startswith("_on_") and name[len("_on_") :] not in classes
        }
        assert orphans == {"haul"}


class TestFormationSplitRefusal:
    """`battle.declaration.formation_split` is the engine's refusal of a fighting withdrawal or a
    retreat that not every declarer made (osrlib #123). The client offers both moves per member with
    labels that say all must agree, so the refusal is the line the player reads when they pick one for
    a single member. It has to name the move in words and read as a sentence, never as the raw code."""

    @staticmethod
    def _refusal(move: str):
        return Rejection(
            code="battle.declaration.formation_split",
            params={"move": move, "declared": ("character-0001",), "others": ("character-0002", "character-0003")},
        )

    def test_a_split_retreat_reads_as_words_naming_the_move(self):
        text = rejection_text(self._refusal("retreat"))
        assert text != "battle.declaration.formation_split"
        assert text[0].isupper() and text.endswith(".")
        assert "retreat" in text.lower()
        assert "fighting withdrawal" not in text.lower()

    def test_a_split_fighting_withdrawal_names_that_move(self):
        text = rejection_text(self._refusal("fighting_withdrawal"))
        assert text[0].isupper() and text.endswith(".")
        assert "fighting withdrawal" in text.lower()
        assert "retreat" not in text.lower()


class TestSpellFailureLines:
    """A disrupted or fizzled spell is narrated with the spell's name and, for a fizzle, the reason.

    osrlib reports both on `SpellDisruptedEvent`: `magic.cast.disrupted` when a blow or a failed
    save stopped the caster, and `magic.cast.fizzled` (osrlib #129) when the magic phase judged the
    declaration again and refused it, with the rejection code in `reason`. The default template
    prints the spell's snake_case id and never the reason, so the narrator renders both itself:
    the caster's name, the spell's catalog name, and the reason as the rejection table already
    words it."""

    def test_a_fizzle_names_the_spell_and_the_reason(self):
        entries = haul_narrator().render(
            [
                SpellDisruptedEvent(
                    code="magic.cast.fizzled",
                    caster_id="character-0001",
                    spell_id="magic_missile",
                    reason="magic.cast.silenced_area",
                )
            ]
        )
        assert len(entries) == 1
        text = entries[0]["text"]
        assert "Corwin" in text
        assert "Magic Missile" in text
        assert "magic_missile" not in text
        assert "magic.cast" not in text
        assert rejection_text(SimpleNamespace(code="magic.cast.silenced_area")) in text
        assert text[0].isupper() and text.endswith(".")

    def test_a_fizzle_with_no_reason_still_reads_as_words(self):
        entries = haul_narrator().render(
            [SpellDisruptedEvent(code="magic.cast.fizzled", caster_id="character-0001", spell_id="sleep")]
        )
        text = entries[0]["text"]
        assert "Corwin" in text and "Sleep" in text
        assert "magic.cast" not in text
        assert text[0].isupper() and text.endswith(".")

    def test_a_disruption_names_the_spell(self):
        entries = haul_narrator().render([SpellDisruptedEvent(caster_id="character-0001", spell_id="magic_missile")])
        text = entries[0]["text"]
        assert "Corwin" in text and "Magic Missile" in text
        assert "magic_missile" not in text
        assert "disrupt" in text.lower()
        assert text[0].isupper() and text.endswith(".")
