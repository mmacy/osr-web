"""Event narration: player-visible events rendered as transcript entries.

Each entry is `{"kind": ..., "text": ...}`. Kinds drive the front end's two
voices: `prose` is the referee reading room descriptions and the author's own
beats (serif), `mech` is the kernel reporting dice and state (mono), `place`
heads an area, and `danger`, `treasure`, and `system` are accent lines. Entity
ids never reach the player — names resolve here, where the session is at hand.
"""

import re
from types import SimpleNamespace

from osrlib.core.events import Visibility
from osrlib.data import load_magic_items, load_spells
from osrlib.messages import format_message

_DIRECTION_WORDS = {"north": "north", "south": "south", "east": "east", "west": "west"}

MODULE_PROSE = "module"
"""Marks the one `prose` entry that is an area's own description from the document.

`prose` is the voice of *every* authored beat now — a trigger's journal line, a
gate's success beat, a quest's offer — so "the prose entry after a place entry" no
longer identifies the module's room description. It used to, and
[`server.narration`][server.narration] inferred it that way for the two things
that must act on module text alone: detecting the `area_entered` beat, and tagging
the entry an LLM passage stands in for. A repeatable trigger's journal beat
rendered on an already-seen area is exactly a place + prose pair, and it was being
replaced — the author's words swapped out for the model's, and dropped outright on
reload.

The marker is player-safe: it labels text the player is already reading, and the
client ignores keys it does not know.
"""

_REJECTION_TEXT = {
    "exploration.move.blocked": "A wall blocks the way.",
    "exploration.move.cannot_move": "The party cannot move — someone is overloaded or unable to walk.",
    "exploration.door.no_door": "There is no door there.",
    "exploration.door.already_open": "The door already stands open.",
    "exploration.door.already_closed": "The door is already closed.",
    "exploration.door.locked": "The door is locked.",
    "exploration.door.stuck": "The door is stuck fast — someone will have to force it.",
    "exploration.door.not_stuck": "The door isn't stuck; it opens normally.",
    "exploration.door.wedged": "The door is wedged and cannot swing.",
    "exploration.door.no_spike": "No one carries an iron spike.",
    "exploration.door.gate_refused": "The door will not open for the party.",
    "exploration.action.requires_light": "It is too dark to do that.",
    "exploration.listen.already_tried": "They have already listened at this door.",
    "exploration.search.already_tried": "They have already searched here for that.",
    "exploration.lock.not_a_thief": "Only a thief can pick locks.",
    "exploration.lock.no_tools": "Picking a lock needs thieves' tools.",
    "exploration.lock.not_locked": "There is no lock left to pick.",
    "exploration.lock.locked_out": "That lock has defeated them — until they learn more.",
    "exploration.trap.not_a_thief": "Only a thief can do that.",
    "exploration.trap.not_found": "No trap has been found there.",
    "exploration.trap.already_resolved": "That trap has already been dealt with.",
    "exploration.party.bad_order": "The marching order must name every member exactly once.",
    "exploration.feature.unknown": "There is nothing like that here.",
    "exploration.feature.emptied": "It has already been emptied.",
    "exploration.stairs.none": "There are no stairs here.",
    "exploration.travel.not_at_entrance": "The party must return to the entrance to leave.",
    "exploration.transition.gate_refused": "The way is barred to the party.",
    "exploration.light.no_flame": "No open flame and no tinder box — nothing to light it with.",
    "exploration.light.not_burning": "Nothing is burning.",
    "exploration.light.not_a_source": "That is not a light source.",
    "exploration.item.not_carried": "They do not carry that.",
    "exploration.item.not_equipped": "That isn't equipped.",
    "exploration.give.same_member": "A character can't give to themselves.",
    "session.command.unknown_member": "No such party member.",
    "session.command.unknown_item": "No such item.",
    "items.equip.armour_forbidden": "Their class can't wear that armour.",
    "items.equip.armour_not_allowed": "They can't wear that.",
    "items.equip.shield_forbidden": "Their class can't use a shield.",
    "items.equip.weapon_forbidden": "Their class can't wield that weapon.",
    "items.equip.weapon_not_allowed": "They can't wield that.",
    "items.equip.two_handed_with_shield": "A two-handed weapon leaves no hand for a shield.",
    "items.equip.not_equippable": "That can't be equipped.",
    "items.equip.not_usable": "They can't use that item.",
    "items.ring.hands_full": "They already wear two rings.",
    "items.curse.stuck": "A cursed item won't leave their hands.",
    "items.use.not_usable": "That item cannot be used like that.",
    "items.use.battle_only": "That power is for battle.",
    "items.use.target_required": "It needs a target.",
    "items.use.unknown_target": "No such target.",
    "items.device.inert": "Nothing happens.",
    "items.scroll.spent": "The scroll is spent.",
    "items.scroll.no_such_spell": "The scroll holds no such spell.",
    "items.scroll.wrong_caster": "That scroll is not written for them.",
    "items.purchase.insufficient_funds": "Not enough gold.",
    "items.purchase.not_stocked": "The trader doesn't carry that.",
    "town.sell.no_fixed_value": "Magic items have no fixed sale price.",
    "session.command.wrong_mode": "Not now.",
    "session.command.member_incapacitated": "They cannot act.",
    "encounter.parley.mid_pursuit": "No talking while being chased.",
    "encounter.turning.mid_pursuit": "No turning while being chased.",
    "encounter.evade.already_evading": "The party is already running.",
    "encounter.evade.nothing_to_drop": "Nothing of that kind to scatter.",
    "magic.turning.not_a_turner": "Only a cleric can turn the undead.",
    "magic.cast.not_memorized": "That spell is not prepared.",
    "magic.cast.target_count": "The spell has no valid target here.",
    "magic.cast.out_of_range": "The target is beyond the spell's reach.",
    "magic.cast.silenced_area": "Magical silence smothers the casting.",
    "magic.cast.caster_incapacitated": "The caster cannot act.",
    "magic.cast.caster_restrained": "The caster cannot move to cast.",
    "magic.cast.anti_magic_shell": "An anti-magic shell smothers the casting.",
    "magic.memorize.needs_sleep": "The caster must sleep before preparing spells again.",
    "magic.book.not_arcane": "Only arcane casters keep spell books.",
    "magic.book.unknown_spell": "No such spell.",
    "magic.book.wrong_list": "That spell is not on their class's list.",
    "magic.book.duplicate": "That spell is already inscribed.",
    "magic.book.capacity_exceeded": "The book has no room at that spell level until they gain more capacity.",
    "battle.declaration.not_in_front_rank": "Only the front rank can reach with melee weapons.",
    "battle.declaration.roster_mismatch": "Every able member needs exactly one action.",
    "combat.attack.out_of_reach": "The enemy is out of reach — close the distance first.",
    "combat.attack.out_of_range": "The target is out of range.",
}


_FORMATION_SPLIT_TEXT = {
    "retreat": "The whole party has to retreat together.",
    "fighting_withdrawal": "The whole party has to make the fighting withdrawal together.",
}
"""`battle.declaration.formation_split`'s `params["move"]`, each as its own line.

The engine refuses a `retreat` or a `fighting_withdrawal` that not every
declarer chose (osrlib #123), one rejection per defensive move the round
split on, so each line names the one move its rejection is about.
"""

_FORMATION_SPLIT_FALLBACK = "The whole party has to make that move together."
"""What an unrecognised `params["move"]` degrades to.

The engine's set of split-sensitive moves can grow, and a raw move name
printed at the player would be engine vocabulary, not a sentence.
"""


def rejection_text(rejection) -> str:
    """One rejection as a friendly line; an authored gate refusal renders verbatim.

    `params["refusal"]` rides `exploration.door.gate_refused` and
    `exploration.transition.gate_refused` only when the author wrote one — the
    engine omits the key for an empty beat — and the authored words are the
    referee's line, rendered as written. The mapped text is the unauthored
    fallback and never competes with it.

    `battle.declaration.formation_split` names its move from
    `params["move"]` rather than through the mapped table, since the same
    code covers two different moves and the line must name only the one that
    was split.

    Args:
        rejection: One [`Rejection`][osrlib.core.validation.Rejection] from a
            command result.

    Returns:
        The line the player reads.
    """
    params = getattr(rejection, "params", None) or {}
    refusal = params.get("refusal")
    if isinstance(refusal, str) and refusal:
        return refusal
    if rejection.code == "battle.declaration.formation_split":
        raw_move = params.get("move")
        if isinstance(raw_move, str) and raw_move in _FORMATION_SPLIT_TEXT:
            return _FORMATION_SPLIT_TEXT[raw_move]
        return _FORMATION_SPLIT_FALLBACK
    mapped = _REJECTION_TEXT.get(rejection.code)
    return mapped if mapped is not None else rejection.code


_SEARCH_FIND_WORDS = {
    "room_trap": "a hidden trap",
    "construction": "a stretch of odd construction",
}
"""Phrases for the search tokens whose detail is an internal id.

`room_trap:{area_id}` and `construction:{feature_id}` both carry an id the player
never sees, so they render as what the search turned up and nothing more. Secret
doors are handled separately: their token carries a direction, which the party
really does perceive.
"""

_UNKNOWN_FIND_WORDS = "something out of place"
"""What an unrecognised search token degrades to.

The engine's token vocabulary can grow. Falling back to a raw `kind:detail` string
would print engine internals into the transcript, so an unknown shape reads as a
vague find instead.
"""


def _found_phrase(token: str) -> str:
    """One `exploration.search.found` token as a player-facing phrase.

    The engine reports a find as `secret_door:{direction}`, `room_trap:{area_id}`, or
    `construction:{feature_id}` (`osrlib.crawl.exploration._reveal`).

    A `room_trap` token names only the trap's area, which is referee state, so the
    phrase carries no bearing. osrlib 1.5.0 widened the search to the searched
    cell's door edges, so the find can be a trap in the room beyond a door — but
    [`SearchCompletedEvent`][osrlib.crawl.events.SearchCompletedEvent] carries no
    position, and the party's *current* position is not the searched one on any
    whole-log re-render (a page reload, a rename, a restore). There is no truthful
    render-time reconstruction of the bearing; stating it is the engine's job
    (mmacy/osrlib-python#67), and until the token carries it this renders what the
    event states and nothing more. Deliberately session-free: this function cannot
    consult a location even by accident.

    Args:
        token: One entry from a search event's `found` tuple.

    Returns:
        A phrase that reads as English — never the token, never an entity id.

    Examples:
        ```python
        _found_phrase("secret_door:east")  # 'a secret door to the east'
        _found_phrase("room_trap:4")       # 'a hidden trap'
        ```
    """
    kind, _, detail = token.partition(":")
    if kind == "secret_door":
        direction = _DIRECTION_WORDS.get(detail)
        return f"a secret door to the {direction}" if direction else "a secret door"
    return _SEARCH_FIND_WORDS.get(kind, _UNKNOWN_FIND_WORDS)


def _and_list(phrases: list[str]) -> str:
    """Join phrases the way a sentence does: `a`, `a and b`, `a, b, and c`."""
    if len(phrases) <= 2:
        return " and ".join(phrases)
    return f"{', '.join(phrases[:-1])}, and {phrases[-1]}"


def _acquisition_run(events, start: int) -> list:
    """The unbroken run of player-visible `item_acquired` events at `start`.

    Args:
        events: One render call's events, in log order.
        start: Where to begin; an event of any other type gives an empty run.

    Returns:
        The events from `start` up to the first one that is not a player-visible
        acquisition — empty when `start` itself is not one.
    """
    end = start
    while (
        end < len(events)
        and getattr(events[end], "event_type", "") == "item_acquired"
        and getattr(events[end], "visibility", None) is Visibility.PLAYER
    ):
        end += 1
    return events[start:end]


_TRAP_CONSEQUENCE = frozenset(
    {
        "damage_dealt",
        "damage_absorbed",
        "hit_points_reported",
        "condition_gained",
        "condition_removed",
        "death",
        "effect_attached",
        "effect_expired",
        "location_entered",
        "party_moved",
        "door",
    }
)
"""Every event type a sprung trap's resolution can emit that means it did something.

Measured against `osrlib.crawl.exploration._resolve_trap` across all nine shapes
`TrapEffect` allows: plain damage, a volley, a save for half, a save that negates,
`kills`, `fall_feet`, a `condition`, a `transition` slide, and `affects: party`.
The one event the resolution emits that is *not* a consequence is the victim's
`saving_throw_rolled` — the save is how the trap is answered, not something it did.

Kept deliberately wide. A type in this set only costs the reassurance line; a
consequence type missing from it would print a reassurance that is false, so
anything a trap's resolution can reach belongs here whether or not it is itself an
injury (`hit_points_reported` and `effect_attached` are referee-only and never
render, but they mark a run that harmed someone).
"""

_TRAP_NO_HARM = "The trap does no harm."
"""The verdict the engine states only by silence.

A trap whose save negated it, whose halved damage rounded to zero, or that carries
no damage dice at all emits its `exploration.trap.sprung` event and then simply
stops — no damage event, no condition, no relocation. The player was left to infer
"unharmed" from a trap firing and nothing following it, and the LLM narrator, handed
the same transcript, inferred injury instead. Said out loud it is
unambiguous to both, and it is the twin of `"The trap fails to fire."`.
"""


def _harmless_trap_run(events, start: int) -> int | None:
    """Where a sprung trap that harmed nobody ends, or None.

    A trap's resolution is the sprung event, then one
    [`SavingThrowRolledEvent`][osrlib.core.events.SavingThrowRolledEvent] per victim
    the effect allows a save, then whatever the trap actually did. So the first
    event past the saves settles it: a member of
    [`_TRAP_CONSEQUENCE`][server.narrate._TRAP_CONSEQUENCE] means the trap landed,
    and anything else — the next command's business, or nothing at all — means it
    did not. A party-wide trap interleaves save and consequence per victim, which
    this still reads correctly: the first victim to be harmed puts a consequence
    immediately after the run of saves seen so far.

    Args:
        events: One render call's events, in log order.
        start: Where to begin; anything but a sprung trap gives None.

    Returns:
        The index just past the trap's resolution, so the caller can render the
        run and append the reassurance — or None when the event is not a sprung
        trap, or when the trap harmed someone.

    Note:
        The scan can only see the events of one render call.
        [`interleave`][server.narration.interleave] splits a reloaded transcript at
        the event-log indexes whose module prose a passage replaces, so a trap whose
        *only* consequence is a same-level `transition` into an unvisited area —
        whose one consequence event is exactly such an index — could have its
        resolution cut in two on reload and read as harmless. Every other shape
        emits damage, a condition, or a level change first, and no shipped adventure
        document carries a transition trap at all.
    """
    if getattr(events[start], "code", "") != "exploration.trap.sprung":
        return None
    stop = start + 1
    while stop < len(events) and getattr(events[stop], "event_type", "") == "saving_throw_rolled":
        stop += 1
    if stop < len(events) and getattr(events[stop], "event_type", "") in _TRAP_CONSEQUENCE:
        return None
    return stop


def _is_wedge_spike(events, index: int) -> bool:
    """Whether the event at `index` is the spike a `WedgeDoor` consumed.

    `WedgeDoor` emits exactly `[ItemConsumedEvent, DoorEvent]` with the door's
    code `exploration.door.wedged`
    (`osrlib.crawl.exploration._handle_wedge_door`), and osr-web's door line
    already names the spike — "An iron spike wedges the door to the north."
    Rendering both reports one spike twice. The consumption event is new in
    osrlib 1.5.0, so this is a line the transcript grew for a verb that authors
    nothing.

    Keyed to the wedged *code* rather than to the `iron_spikes` item id, so a
    gate's toll paid on the way through a door still reports as a consumption.

    Args:
        events: One render call's events, in log order.
        index: Where to look.

    Returns:
        True when the event is a wedge's spike and the caller should skip it.

    Note:
        The pair is adjacent only within one render call.
        [`interleave`][server.narration.interleave] splits a reloaded transcript
        at the event-log indexes whose module prose a passage replaces, and those
        are area-entered indexes alone — never a point between a consumption and
        the door event that follows it.
    """
    return (
        getattr(events[index], "event_type", "") == "item_consumed"
        and index + 1 < len(events)
        and getattr(events[index + 1], "code", "") == "exploration.door.wedged"
    )


def _is_distribution(run: list) -> bool:
    """Whether a run of acquisitions is one haul spread across the party.

    `TakeTreasure` divides a haul and emits one
    [`ItemAcquiredEvent`][osrlib.crawl.events.ItemAcquiredEvent] per member who
    took something, so a distributed take is several acquisitions in a row. The
    other source of back-to-back acquisitions is the town shop — the client posts
    one `purchase_equipment` per item, and nothing separates two of them in the
    event log — so a run needs a signature that a shopping trip cannot forge.

    Coin is that signature. A take spreads the cache's coins over everyone, so at
    least one share either carries a whole-gold value or names no items at all,
    and an acquisition with neither items nor gold can only be a share of coppers
    and silver worth less than a gold piece (the engine emits nothing for a member
    who took nothing). Every purchase, by contrast, names its goods and carries no
    coin. A cache of pure gear that lands on two different members therefore stays
    two lines, which is no loss: those lines are already one per notable item.

    Args:
        run: A run of acquisitions from
            [`_acquisition_run`][server.narrate._acquisition_run].

    Returns:
        True when the run should render as one collapsed haul line.
    """
    return len(run) > 1 and any(event.coins_gp_value or not event.item_ids for event in run)


class Narrator:
    """Renders player-visible events to transcript entries for one session."""

    def __init__(self, session):
        """Start rendering for `session`, with no areas seen and no labels learned yet."""
        self.session = session
        self.seen_areas: set[str] = set()
        # The engine's own resolver for authored ids: the shipped lists unioned
        # with whatever the adventure bundles. The shipped catalog alone printed
        # a bundled item's raw id everywhere it was named.
        self._equipment = session.effective_equipment
        self._labels: dict[str, str] = {}
        # instance id → true name, learned from identification events. A drunk
        # potion or read scroll leaves the inventory before render, so the
        # inventory walk in `_item_name` has nothing to find; the log's own
        # `item_identified` line is where the name was said, and it precedes
        # every use of it (identification is first-meaningful-use).
        self._item_labels: dict[str, str] = {}

    def _cache_labels(self) -> None:
        """Remember group and monster labels while the encounter still exists.

        The terminal battle round renders after the engine clears the encounter,
        so names learned during earlier commands must outlive it.
        """
        encounter = self.session.encounter
        if encounter is None:
            return
        for group in encounter.groups:
            self._labels[group.id] = group.label
            for monster_id in group.monster_ids:
                combatant = self.session.combatant(monster_id)
                name = getattr(getattr(combatant, "template", None), "name", None)
                if name:
                    self._labels[monster_id] = name

    def _name(self, ref: str) -> str:
        """A display name for any entity ref the events may carry."""
        for member in self.session.party.members:
            if member.id == ref:
                return member.name
        combatant = self.session.combatant(ref)
        if combatant is not None:
            name = getattr(combatant, "name", None) or getattr(getattr(combatant, "template", None), "name", None)
            if name:
                return name
        encounter = self.session.encounter
        if encounter is not None:
            for group in encounter.groups:
                if group.id == ref:
                    return group.label
        if ref in self._labels:
            return self._labels[ref]
        if ref.startswith("group-"):
            return "the monsters"
        return ref

    def _substitute_ids(self, text: str) -> str:
        return re.sub(
            r"\b(character|monster|npc|group)-\d+\b",
            lambda m: self._name(m.group(0)),
            text,
        )

    def _item_name(self, item_id: str) -> str:
        try:
            return self._equipment.get(item_id).name
        except ValueError:
            pass
        for member in self.session.party.members:
            for valuable in member.inventory.valuables:
                if valuable.instance_id == item_id:
                    return valuable.name or valuable.kind
            for instance in member.inventory.items:
                if getattr(instance, "instance_id", None) == item_id:
                    if getattr(instance, "identified", False):
                        from osrlib.core.items import magic_item_template

                        return magic_item_template(instance).name
                    return "an unidentified item"
        if item_id in self._item_labels:
            return self._item_labels[item_id]
        return item_id

    def _area(self, event):
        """The area an arrival event names, resolved off the event's own facts.

        Area ids are level-scoped, so the lookup needs the whole triple the event
        carries — never where the party stands at render time. Rendering happens
        after the command has finished, so a trigger consequence that relocates
        the party would otherwise resolve every area event in the delta against
        the destination, and a whole-log re-render (a rename) would resolve all
        of history against the final location.

        An event with no `dungeon_id` — a pre-1.5.0 save's persisted log, where
        the field did not exist — resolves to no area, and
        [`_on_location_entered`][server.narrate.Narrator._on_location_entered]
        then renders **nothing at all** for it: no place header, no prose, and
        `seen_areas` never learns the area, so the first entry after the upgrade
        re-prints the description in full. That is the honest reading: the
        renderer states what the data states, and there is no fallback to the
        session's current location, which is the very defect this resolution
        exists to fix.
        """
        if event.dungeon_id is None:
            return None
        try:
            level = self.session.adventure.dungeon(event.dungeon_id).level(event.level_number)
        except ValueError:
            return None
        return next((area for area in level.areas if area.id == event.location_id), None)

    def render(self, events) -> list[dict]:
        """Player-visible events to transcript entries, in order.

        Events render one at a time except where a run of them tells one story.
        A distributed haul is one: `TakeTreasure` emits an `item_acquired` event
        per member, and a run of them collapses into the single line
        [`_on_haul`][server.narrate.Narrator._on_haul] writes. A sprung trap is
        the other: its resolution renders normally, and a trap that harmed nobody
        then gets the closing line the engine leaves unsaid
        ([`_harmless_trap_run`][server.narrate._harmless_trap_run]). A wedged door
        is the third: the spike's consumption and the door event are one act, and
        the door line already names the spike
        ([`_is_wedge_spike`][server.narrate._is_wedge_spike]). Runs are found by
        adjacency in the raw event list, which is how the engine emits them —
        back to back — so two takes in a row stay two lines.
        """
        self._cache_labels()
        events = list(events)
        entries: list[dict] = []
        index = 0
        while index < len(events):
            run = _acquisition_run(events, index)
            if _is_distribution(run):
                entries.append(self._on_haul(run))
                index += len(run)
                continue
            if _is_wedge_spike(events, index):
                index += 1  # the door event that follows already names the spike
                continue
            stop = _harmless_trap_run(events, index)
            if stop is not None:
                # The saves are the evidence, so the verdict follows them.
                for event in events[index:stop]:
                    self._append_entry(entries, event)
                entries.append({"kind": "mech", "text": _TRAP_NO_HARM})
                index = stop
                continue
            self._append_entry(entries, events[index])
            index += 1
        return entries

    def _append_entry(self, entries: list[dict], event) -> None:
        """Render one event onto `entries`, skipping what the player never sees."""
        if event.visibility is not Visibility.PLAYER:
            return
        entry = self._render_event(event)
        if entry is None:
            return
        if isinstance(entry, list):
            entries.extend(entry)
        else:
            entries.append(entry)

    def _render_event(self, event):
        code = getattr(event, "code", "")
        if code == "effects.condition.gained" and getattr(event, "condition", "") == "dead":
            return None  # the combat.death.died line that follows says it better
        if code == "combat.death.died":
            who = self._name(getattr(event, "target_id", ""))
            is_member = any(member.id == getattr(event, "target_id", "") for member in self.session.party.members)
            if is_member:
                return {"kind": "danger", "text": f"{who} is slain!"}
            return {"kind": "mech", "text": f"{who} is killed."}
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            return handler(event)
        return {"kind": "mech", "text": self._substitute_ids(format_message(event))}

    def _on_party_moved(self, event):
        return None  # the viewport and automap tell this story

    def _on_location_entered(self, event):
        if event.location_kind == "area":
            area = self._area(event)
            if area is None:
                return None
            if area.id not in self.seen_areas:
                self.seen_areas.add(area.id)
                text = area.description.strip() or "Nothing of note."
                return [
                    {"kind": "place", "text": area.name or f"Area {area.id}"},
                    {"kind": "prose", "text": text, MODULE_PROSE: True},
                ]
            return {"kind": "place", "text": area.name or f"Area {area.id}"}
        if event.location_kind == "town":
            return {
                "kind": "system",
                "text": f"The party returns to {self.session.adventure.town.name}.",
            }
        # A gate's success beat rides the arrival that crossed a level or dungeon
        # boundary — the only arrivals a gated transition emits. A same-level
        # transition emits nothing at all, which is why a bundled gate is a door.
        if event.location_kind == "dungeon":
            return self._with_beat(
                {
                    "kind": "system",
                    "text": f"The party stands at the entrance of {self._dungeon_name(event.location_id)}.",
                },
                event.narrative,
            )
        if event.location_kind == "level":
            # "Descends" was a guess, and the bundled up-stair (level 2 → level 1)
            # made it a false one: the party climbed and the transcript said they
            # went down. A level arrival states which level, never by what means,
            # and the previous level is not something a whole-log re-render can
            # reconstruct honestly — so the wording is neutral until the engine
            # says which way the stair ran (mmacy/osrlib-python#68).
            return self._with_beat(
                {
                    "kind": "system",
                    "text": f"The party comes to level {event.level_number}.",
                },
                event.narrative,
            )
        return None

    @staticmethod
    def _with_beat(entry: dict, narrative):
        """One entry, followed by the author's beat when the event carries one.

        Authored text renders verbatim in the prose voice — the module speaking —
        beside the templated fact, never instead of it.
        """
        if not narrative:
            return entry
        return [entry, {"kind": "prose", "text": narrative}]

    def _dungeon_name(self, dungeon_id: str) -> str:
        try:
            return self.session.adventure.dungeon(dungeon_id).name
        except ValueError:
            return dungeon_id

    def _on_door(self, event):
        direction = _DIRECTION_WORDS.get(event.direction, event.direction)
        texts = {
            "exploration.door.opened": f"The door to the {direction} swings open.",
            "exploration.door.closed": f"The door to the {direction} closes.",
            "exploration.door.forced": (
                f"{self._name(event.character_id) if event.character_id else 'Someone'} "
                f"forces the door to the {direction} with a crash!"
            ),
            "exploration.door.stuck": f"The door to the {direction} will not budge.",
            "exploration.door.wedged": f"An iron spike wedges the door to the {direction}.",
            "exploration.door.swung_shut": "A door swings shut behind the party.",
            "exploration.door.unlocked": f"The lock clicks open on the door to the {direction}.",
        }
        return self._with_beat({"kind": "mech", "text": texts.get(event.code, event.code)}, event.narrative)

    def _on_listened(self, event):
        who = self._name(event.character_id)
        direction = _DIRECTION_WORDS.get(event.direction, event.direction)
        if event.code == "exploration.listen.heard":
            return {
                "kind": "danger",
                "text": f"{who} presses an ear to the {direction} door — something stirs beyond.",
            }
        return {
            "kind": "mech",
            "text": f"{who} listens at the {direction} door: silence.",
        }

    def _on_search_completed(self, event):
        who = self._name(event.character_id)
        kind_words = {
            "secret_doors": "secret doors",
            "room_traps": "traps",
            "treasure_traps": "treasure traps",
            "construction": "odd construction",
        }
        what = kind_words.get(event.kind, event.kind)
        if event.code == "exploration.search.found" and event.found:
            # Two tricks in one room would otherwise read the same phrase twice.
            phrases = list(dict.fromkeys(_found_phrase(token) for token in event.found))
            return {
                "kind": "treasure",
                "text": f"{who} searches for {what} — and finds {_and_list(phrases)}!",
            }
        return {"kind": "mech", "text": f"{who} searches for {what} and finds nothing."}

    def _on_trap(self, event):
        who = self._name(event.character_id) if event.character_id else "Someone"
        texts = {
            "exploration.trap.sprung": f"A trap springs on {who}!",
            "exploration.trap.found": f"{who} finds a trap!",
            "exploration.trap.removed": f"{who} disarms the trap.",
            "exploration.trap.safe": "The trap fails to fire.",
        }
        kind = "danger" if event.code == "exploration.trap.sprung" else "mech"
        return {"kind": kind, "text": texts.get(event.code, event.code)}

    def _item_list(self, item_ids) -> str:
        """Item ids as a readable list, coalescing repeats into `name ×N`."""
        counts: dict[str, int] = {}
        order: list[str] = []
        for item_id in item_ids:
            if item_id not in counts:
                order.append(item_id)
            counts[item_id] = counts.get(item_id, 0) + 1
        parts = []
        for item_id in order:
            name = self._item_name(item_id)
            parts.append(f"{name} ×{counts[item_id]}" if counts[item_id] > 1 else name)
        return ", ".join(parts)

    def _goods_and_coin(self, event) -> str:
        """The `items and coin` phrase shared by acquire/drop/give."""
        parts = []
        if event.item_ids:
            parts.append(self._item_list(event.item_ids))
        if event.coins_gp_value:
            parts.append(f"{event.coins_gp_value} gp in coin")
        return " and ".join(parts) if parts else "nothing"

    def _on_item_acquired(self, event):
        who = self._name(event.character_id)
        return {
            "kind": "treasure",
            "text": f"{who} takes {self._goods_and_coin(event)}.",
        }

    def _on_haul(self, events) -> dict:
        """One take's acquisitions as a single line, once it has been split.

        A haul that reached one character — a `recipient_id` take, a party of one,
        or a cache small enough that one pack swallowed it — never gets here:
        [`_is_distribution`][server.narrate._is_distribution] leaves it to
        [`_on_item_acquired`][server.narrate.Narrator._on_item_acquired] and its
        plain sentence. What gets here is a haul that six members split, which as
        six lines is four repetitions of the same coin figure. So the coin becomes
        the party's one clause and only the members holding something worth naming
        are named — gear, gems, jewellery, and magic all ride in `item_ids`, so
        "notable" needs no separate test.

        **The coin figure is the sum of the events, not the cache's face value.**
        Each share floors its own value to whole gold, so a pot that does not
        divide evenly reads a gold piece or two light: 400 gp and 200 sp across six
        members is 420 gp in the box and 418 gp in the shares. The events are all
        the narrator has — the cache is gone from the session by render time, and
        the purses hold money the party already had — and this is the same
        arithmetic the six separate lines added up to, now shown once. When every
        share floors to nothing (a jar of coppers), the count is dropped rather
        than printed as a zero, because the alternative reads as a lie.

        Args:
            events: A run of `item_acquired` events from one take, in marching
                order.

        Returns:
            One `treasure` entry.
        """
        coin = sum(event.coins_gp_value for event in events)
        sentences = []
        if coin:
            sentences.append(f"The party splits {coin} gp in coin.")
        elif any(not event.item_ids for event in events):
            # An acquisition naming no items at all is a share of small change:
            # the engine emits nothing for a member who took nothing.
            sentences.append("The party splits the loose coin.")
        clauses = [
            f"{self._name(event.character_id)} takes {self._item_list(event.item_ids)}"
            for event in events
            if event.item_ids
        ]
        if clauses:
            sentences.append(f"{'; '.join(clauses)}.")
        return {"kind": "treasure", "text": " ".join(sentences)}

    def _on_items_left_behind(self, event):
        """Treasure the party could not carry, left where it lay.

        A `system` line rather than a `treasure` one: amber is the accent for what
        the party gained, and this is the opposite — a plain note about the world,
        in the same voice as "The encounter is over." Nothing is destroyed, so the
        line reads as a thing still lying there rather than a loss.
        """
        return {
            "kind": "system",
            "text": (f"The party cannot carry it all — {self._goods_and_coin(event)} stays where it lies."),
        }

    def _on_items_dropped(self, event):
        who = self._name(event.character_id)
        return {"kind": "mech", "text": f"{who} drops {self._goods_and_coin(event)}."}

    def _on_items_given(self, event):
        who = self._name(event.character_id)
        recipient = self._name(event.recipient_id)
        return {
            "kind": "mech",
            "text": f"{who} hands {self._goods_and_coin(event)} to {recipient}.",
        }

    def _on_item_consumed(self, event):
        """A gate's toll paid — the item leaves the pack for good.

        A magic toll carries the session-scoped `instance_id` of an instance the
        engine consumed before render time, so the inventory walk in `_item_name`
        comes back empty: the name degrades through the identification cache
        (`_item_labels`) and, failing that, to "an item" — never to a raw id.

        The other issuer of this event is `WedgeDoor`'s iron spike, which
        [`render`][server.narrate.Narrator.render] drops in favour of the door
        line that already names the spike.

        The resolution is explicit rather than a comparison of `_item_name`'s
        answer against the id it was handed: an adventure may bundle an item
        whose display name *is* its id (`{"id": "rope", "name": "rope"}`), which
        resolves perfectly well and which the comparison would have thrown away
        to print "an item".
        """
        who = self._name(event.character_id)
        return {
            "kind": "mech",
            "text": f"{who} gives up {self._consumed_item_phrase(event.item_id)}.",
        }

    def _consumed_item_phrase(self, item_id: str) -> str:
        """What a consumed item is called, or "an item" when nothing knows it.

        Three places can answer, in order of authority: the effective equipment
        catalog (a mundane toll carries its catalog id); the party's own packs (a
        toll taken from a magic *stack* decrements it and leaves the instance
        carried); and the identification cache, which is the only witness left
        once a single-unit magic instance has been consumed. Failing all three,
        the phrase is vague — a session-scoped instance id is never a thing to
        print at a player.
        """
        try:
            return self._equipment.get(item_id).name
        except ValueError:
            pass
        for member in self.session.party.members:
            for instance in member.inventory.items:
                if getattr(instance, "instance_id", None) != item_id:
                    continue
                if getattr(instance, "identified", False):
                    from osrlib.core.items import magic_item_template

                    return magic_item_template(instance).name
                return "an unidentified item"
        return self._item_labels.get(item_id, "an item")

    def _used_item_phrase(self, event) -> str:
        """The used item's display name, degrading to its kind when unresolvable.

        Using a potion or scroll consumes the instance before render, so the
        inventory walk can come back empty. The identified-name cache covers an
        item the log named earlier; failing even that, the event's own code says
        what kind of thing it was, which is all the player knew anyway.
        """
        name = self._item_name(event.instance_id)
        if name != event.instance_id:
            return name
        kind, _, _ = event.code.removeprefix("items.").partition(".")
        return {"potion": "a potion", "scroll": "a scroll"}.get(kind, "an item")

    def _on_item_used(self, event):
        who = self._name(event.character_id)
        if event.code == "items.potion.mixed":
            return {
                "kind": "danger",
                "text": (f"The potions mix badly inside {who} — both effects are lost, and sickness takes hold."),
            }
        if event.code == "items.scroll.cursed":
            return {"kind": "danger", "text": f"The scroll {who} reads is cursed!"}
        verbs = {
            "items.potion.drunk": "drinks",
            "items.scroll.read": "reads",
            "items.device.activated": "activates",
        }
        verb = verbs.get(event.code, "uses")
        return {
            "kind": "mech",
            "text": f"{who} {verb} {self._used_item_phrase(event)}.",
        }

    def _magic_item_name(self, template_id: str) -> str:
        try:
            return load_magic_items().get(template_id).name
        except ValueError:
            return "something magical"

    def _on_item_identified(self, event):
        name = self._magic_item_name(event.template_id)
        self._item_labels[event.instance_id] = name
        article = "an" if name[:1].lower() in "aeiou" else "a"
        return {"kind": "treasure", "text": f"It is {article} {name}."}

    def _on_curse_revealed(self, event):
        who = self._name(event.character_id)
        name = self._magic_item_name(event.template_id)
        self._item_labels[event.instance_id] = name
        return {
            "kind": "danger",
            "text": f"A curse — {who} cannot be rid of the {name}.",
        }

    def _on_light(self, event):
        texts = {
            "exploration.light.lit": "A light flares up.",
            "exploration.light.failed": "The tinder fails to catch.",
            "exploration.light.extinguished": "The light is put out.",
            "exploration.light.expired": "The light gutters and dies.",
        }
        kind = "danger" if event.code == "exploration.light.expired" else "mech"
        return {
            "kind": kind,
            "text": texts.get(event.code, self._substitute_ids(format_message(event))),
        }

    def _on_rested(self, event):
        if event.code == "exploration.rest.interrupted":
            return {"kind": "danger", "text": "The rest is interrupted!"}
        return {"kind": "mech", "text": "The party rests."}

    def _on_encounter_started(self, event):
        entries = [
            {
                "kind": "danger",
                "text": f"Monsters! {event.count} × {event.monster_name}, {event.distance_feet} feet away.",
            }
        ]
        if event.party_surprised:
            entries.append({"kind": "danger", "text": "The party is caught by surprise!"})
        if event.monsters_surprised:
            entries.append({"kind": "system", "text": "The monsters have not noticed the party."})
        return entries

    def _on_stance_changed(self, event):
        stance_words = {
            "hostile": "They look hostile.",
            "attacks": "They attack!",
            "uncertain": "They seem uncertain of the party.",
            "indifferent": "They take little interest in the party.",
            "friendly": "They seem friendly.",
        }
        text = stance_words.get(event.stance, f"Their manner changes ({event.stance}).")
        kind = "danger" if event.stance in ("hostile", "attacks") else "mech"
        return {"kind": kind, "text": text}

    def _on_encounter_ended(self, event):
        return {"kind": "system", "text": f"The encounter is over ({event.outcome})."}

    def _on_battle_started(self, event):
        return {"kind": "danger", "text": "Battle is joined!"}

    def _on_battle_round(self, event):
        return {"kind": "system", "text": f"— Round {event.round} —"}

    def _on_battle_ended(self, event):
        texts = {
            "battle.ended.victory": "Victory! The field belongs to the party.",
            "battle.ended.fled": "The party breaks away from the fight.",
            "battle.ended.defeat": "The battle is lost.",
        }
        kind = "danger" if event.code == "battle.ended.defeat" else "system"
        return {"kind": kind, "text": texts.get(event.code, "The battle is over.")}

    def _on_monster_defeated(self, event):
        name = self._monster_name(event.template_id)
        outcomes = {"slain": "slain", "routed": "routed", "surrendered": "taken"}
        return {
            "kind": "mech",
            "text": f"{name} {outcomes.get(event.outcome, event.outcome)} ({event.xp} XP).",
        }

    def _on_monster_fled(self, event):
        label = self._name(event.group_id)
        if event.code == "battle.side.surrendered":
            return {
                "kind": "mech",
                "text": f"{label} throw down their arms and surrender!",
            }
        return {"kind": "mech", "text": f"{label} break and flee!"}

    def _on_monsters_left_behind(self, event):
        # A routing group left its immobilized (asleep, paralysed, webbed)
        # members where they lie, as their own group. Without this line the
        # sidebar sprouts a second group the log never explained — and the LLM
        # narrator, fed the same transcript, would invent its own reason.
        label = self._name(event.source_group_id)
        fellows = "fellow" if event.count == 1 else "fellows"
        return {
            "kind": "mech",
            "text": f"{label} abandon {event.count} helpless {fellows} where they lie.",
        }

    def _on_group_moved(self, event):
        return {
            "kind": "mech",
            "text": f"{self._name(event.group_id)} now {event.distance_feet} feet away.",
        }

    def _on_attack_rolled(self, event):
        attacker = self._name(event.attacker_id)
        defender = self._name(event.defender_id)
        if event.code == "combat.attack.auto_hit":
            return {
                "kind": "mech",
                "text": f"{attacker} strikes the helpless {defender} with {event.attack_name} — no roll needed.",
            }
        roll = f"rolled {event.roll}{event.modifier:+d} = {event.total}"
        if event.code == "combat.attack.hit":
            natural = " (natural 20)" if event.natural == 20 else ""
            return {
                "kind": "mech",
                "text": f"{attacker} hits {defender} with {event.attack_name}: {roll}{natural}.",
            }
        natural = " (natural 1)" if event.natural == 1 else ""
        return {
            "kind": "mech",
            "text": (
                f"{attacker} misses {defender} with {event.attack_name}: {roll}{natural} (needed {event.required})."
            ),
        }

    def _monster_name(self, template_id: str) -> str:
        try:
            from osrlib.data import load_monsters

            return load_monsters().get(template_id).name
        except ValueError:
            return template_id

    def _on_evasion(self, event):
        if event.code == "encounter.evasion.succeeded":
            return {"kind": "system", "text": "The party gets away clean."}
        return {"kind": "danger", "text": "They give chase!"}

    def _on_pursuit(self, event):
        texts = {
            "encounter.pursuit.round": f"The chase continues — the gap is {event.gap_feet} feet.",
            "encounter.pursuit.distracted": "The pursuers stop for the bait!",
            "encounter.pursuit.escaped": "The party escapes!",
            "encounter.pursuit.caught": "The pursuers are on the party's heels!",
        }
        kind = "system" if event.code == "encounter.pursuit.escaped" else "danger"
        return {
            "kind": kind,
            "text": texts.get(event.code, self._substitute_ids(format_message(event))),
        }

    def _on_xp_awarded(self, event):
        who = self._name(event.character_id)
        return {
            "kind": "mech",
            "text": f"{who} gains {event.modified_award} XP (level {event.level_after}).",
        }

    def _on_leveled_up(self, event):
        who = self._name(event.character_id)
        if event.title is None:
            # The SRD's title lists run only through name level.
            return {
                "kind": "system",
                "text": f"{who} is now level {event.level_after} (+{event.hp_gained} hp).",
            }
        return {
            "kind": "system",
            "text": f"{who} is now level {event.level_after} — {event.title} (+{event.hp_gained} hp).",
        }

    def _on_adventure_xp_award(self, event):
        """The end-of-adventure tally, broken out into its two pools.

        The award is one number per head, so the per-member lines that follow
        cannot say where it came from. Without the breakdown a party that hauled
        a fortune out of the dark read the same as one that fought its way out
        empty-handed, and treasure XP — most of a B/X party's advancement —
        looked like it was never awarded at all.
        """
        return {
            "kind": "treasure",
            "text": (
                f"The adventure's spoils are tallied: {event.monster_xp} XP from monsters "
                f"and {event.treasure_xp} XP from treasure, {event.share} each."
            ),
        }

    def _on_treasure_sold(self, event):
        return {
            "kind": "treasure",
            "text": f"{self._name(event.character_id)} sells treasure for {event.gp_value} gp.",
        }

    def _on_healing_purchased(self, event):
        return {
            "kind": "mech",
            "text": f"The temple tends to {self._name(getattr(event, 'character_id', ''))}.",
        }

    def _on_game_over(self, event):
        return {
            "kind": "danger",
            "text": "The party has fallen. The dark closes over them, and no one returns.",
        }

    def _on_journal_entry_added(self, event):
        # The authored text is the whole report. `rounds` is bookkeeping for the
        # journal surface, never a transcript line.
        return {"kind": "prose", "text": event.text}

    def _on_quest_activated(self, event):
        entries = [{"kind": "system", "text": f"New quest: {event.name}."}]
        if event.narrative:
            entries.append({"kind": "prose", "text": event.narrative})
        return entries

    def _on_objective_revealed(self, event):
        # The authored beat speaks when there is one; otherwise the system voice
        # states the fact, naming the objective and its quest off the event. The
        # engine fills `name` with the objective's authored name *or* its id, so
        # an unauthored objective's label is its slug — and comparing the label
        # to the id to find that out would only throw a good name away.
        if event.narrative:
            return {"kind": "prose", "text": event.narrative}
        return {
            "kind": "system",
            "text": f"New objective: {event.name} — {event.quest_name}.",
        }

    def _on_objective_completed(self, event):
        if event.narrative:
            return {"kind": "prose", "text": event.narrative}
        return {
            "kind": "system",
            "text": f"Objective complete: {event.name} — {event.quest_name}.",
        }

    def _on_quest_completed(self, event):
        # The completion beat renders here and only here. A concluding quest
        # emits this event and `adventure_completed` back to back carrying the
        # same authored line, and the journal records it once; repeating it in
        # the transcript would say the ending twice.
        entries = [{"kind": "system", "text": f"Quest complete: {event.name}."}]
        if event.narrative:
            entries.append({"kind": "prose", "text": event.narrative})
        return entries

    def _on_adventure_completed(self, event):
        # The terminal fact alone: the event names the concluding quest, and its
        # `narrative` is the beat `quest_completed` already spoke.
        return {
            "kind": "system",
            "text": f"The adventure is won — {event.name} is done.",
        }

    def _on_flag_set(self, event):
        return None

    def _on_time_advanced(self, event):
        return None

    def _spell_name(self, spell_id: str) -> str:
        try:
            return load_spells().get(spell_id).name
        except ValueError:
            return spell_id

    def _on_spell_declared(self, event):
        return {
            "kind": "mech",
            "text": f"{self._name(event.caster_id)} begins casting {self._spell_name(event.spell_id)}.",
        }

    def _on_spell_cast(self, event):
        who = self._name(event.caster_id)
        spell = self._spell_name(event.spell_id)
        if event.code == "magic.cast.no_effect":
            return {"kind": "mech", "text": f"{who} casts {spell} — it has no effect."}
        return {"kind": "mech", "text": f"{who} casts {spell}!"}

    def _on_spell_disrupted(self, event):
        who = self._name(event.caster_id)
        spell = self._spell_name(event.spell_id)
        if event.code == "magic.cast.fizzled":
            if event.reason:
                reason = rejection_text(SimpleNamespace(code=event.reason))
                return {
                    "kind": "mech",
                    "text": f"{who}'s casting of {spell} fizzles. {reason}",
                }
            return {"kind": "mech", "text": f"{who}'s casting of {spell} fizzles."}
        return {"kind": "mech", "text": f"{who}'s casting of {spell} is disrupted."}

    def _on_spells_memorized(self, event):
        names = ", ".join(self._spell_name(copy.spell_id) for copy in event.prepared)
        return {
            "kind": "mech",
            "text": f"{self._name(event.caster_id)} prepares: {names}.",
        }

    def _on_spell_book_updated(self, event):
        return {
            "kind": "system",
            "text": f"{self._name(event.caster_id)} inscribes {self._spell_name(event.spell_id)} in their spell book.",
        }
