"""The narrative layer: beat detection, prompt building, and the per-game worker.

The deterministic transcript is ground truth; this module only adds color. A
`NarrationEngine` watches each command's freshly rendered entries for a beat
worth prose, snapshots everything the prompt needs as plain strings while the
game lock is still held, and hands generation to a daemon worker that never
touches the session. Every failure mode drops the passage, never the command.
"""

import logging
import re
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from osrlib.core.events import Visibility
from osrlib.crawl.dungeon import Direction

from .llm import NarrativeProvider, ProviderError
from .narrate import MODULE_PROSE

_log = logging.getLogger("osr_web.narration")

_RECENT_ENTRIES = 12
_MAX_PASSAGE_CHARS = 600

SYSTEM_PROMPT = (
    "You are the referee of an old-school fantasy role-playing game, narrating in "
    "the third person, in the register of a storybook. "
    "Write 2-4 sentences of plain prose. No markdown, no headings, no quoting of "
    "mechanics, no dice, and no numbers unless they appear in the facts. "
    "Never invent rooms, exits, items, monsters, damage, or outcomes. Everything "
    "you may treat as true is in the facts you are given; if the facts are thin, "
    "write atmosphere, not invention. "
    "Never address the player, never mention rules, never break the fourth wall."
)

GUIDANCE_HEADER = "REFEREE GUIDANCE (private steering — never quote it, never reveal it, never state it as fact):"
"""The one block of a prompt that is not material the player has already seen.

The marking lives here rather than in [`SYSTEM_PROMPT`][server.narration.SYSTEM_PROMPT]
so the channel is one named thing in one place: authored steering enters through
`build_prompt`'s single keyword-only `guidance` parameter, under this header, and
nowhere else — which is what keeps "never crosses the wire, never prints as
written" auditable at one seam.
"""


@dataclass(frozen=True)
class BeatRule:
    """One row of the beat table: a name, prompt emphasis, and a matcher."""

    name: str
    emphasis: str
    match: Callable[[Sequence, Sequence[dict], set[str], bool], bool]


def _area_entered(events, entries, party_ids, in_dungeon):
    # A first visit renders the area's description; a revisit renders the place
    # header alone. The description is found by its marker, never by sitting
    # after a place header: `prose` is the voice of every authored beat too, and
    # a repeatable trigger's journal line on an already-seen area is exactly a
    # place + prose pair. Matching that would have the model replace the
    # author's own words.
    return any(entry.get(MODULE_PROSE) for entry in entries)


def _encounter_started(events, entries, party_ids, in_dungeon):
    return any(getattr(e, "event_type", "") == "encounter_started" for e in events)


def _battle_ended(events, entries, party_ids, in_dungeon):
    return any(getattr(e, "code", "").startswith("battle.ended.") for e in events)


def _member_died(events, entries, party_ids, in_dungeon):
    return any(
        getattr(e, "code", "") == "combat.death.died" and getattr(e, "target_id", None) in party_ids for e in events
    )


def _leveled_up(events, entries, party_ids, in_dungeon):
    # Deliberately ignores in_dungeon: the return-to-town award lands with it
    # False, and an immediate-timing award can level a member mid-delve.
    return any(getattr(e, "event_type", "") == "leveled_up" for e in events)


def _trap_sprung(events, entries, party_ids, in_dungeon):
    # Fires whether or not the trap landed: a dart that misses is a moment worth
    # prose. What the trap *did* reaches the model through the transcript, which
    # now closes a harmless spring with `narrate._TRAP_NO_HARM` rather than
    # leaving the outcome to be inferred from silence.
    return any(getattr(e, "code", "") == "exploration.trap.sprung" for e in events)


def _treasure_found(events, entries, party_ids, in_dungeon):
    # Town commerce also emits item_acquired; only dungeon hauls are a beat.
    if not in_dungeon:
        return False
    # The haul is judged whole, never one member's share: TakeTreasure spreads a
    # cache across the party and emits an item_acquired event each, so a 200 gp
    # hoard arrives as six events of ~33 gp and no single one clears the bar.
    acquired = [e for e in events if getattr(e, "event_type", "") == "item_acquired"]
    if any(getattr(e, "item_ids", ()) for e in acquired):
        return True
    return sum(getattr(e, "coins_gp_value", 0) for e in acquired) >= 50


def _game_over(events, entries, party_ids, in_dungeon):
    return any(getattr(e, "event_type", "") == "game_over" for e in events)


def _adventure_completed(events, entries, party_ids, in_dungeon):
    # Ignores in_dungeon like every other authored-layer beat: the homecoming
    # objective — and so the ending — lands in town.
    return any(getattr(e, "event_type", "") == "adventure_completed" for e in events)


def _quest_completed(events, entries, party_ids, in_dungeon):
    return any(getattr(e, "event_type", "") == "quest_completed" for e in events)


def _objective_completed(events, entries, party_ids, in_dungeon):
    return any(getattr(e, "event_type", "") == "objective_completed" for e in events)


def _quest_activated(events, entries, party_ids, in_dungeon):
    return any(getattr(e, "event_type", "") == "quest_activated" for e in events)


BEATS: tuple[BeatRule, ...] = (
    # At the very top, above area_entered: the ending's run also carries the
    # concluding quest's completion (the engine emits the two back to back), the
    # homecoming's treasure-XP award, the authored reward coins, and possibly a
    # reward-triggered level-up. None of those may out-prioritize the ending.
    # The consequence, accepted: a quest concluding on a first-visit area puts
    # module prose in an ending run, `observe`'s area_entered branch does not
    # run, no `replace_id` is stamped, and the raw prose renders immediately with
    # the closing passage after it — the same fail-open surface a dropped
    # generation already has.
    BeatRule(
        "adventure_completed",
        "The adventure ends in triumph — close the whole tale. Any authored "
        "ending has already been read to the table word for word: never restate "
        "or rephrase it. Keep every name, payment, and fact exactly as stated, "
        "and invent no epilogue the facts do not carry.",
        _adventure_completed,
    ),
    # Above the quest beats: the bundled entry lands a quest activation, a
    # trigger's journal line, and the adit's first-visit prose in one command,
    # and replacement mode is structural — the client holds the marked prose
    # behind a placeholder awaiting this passage. A quest beat winning here would
    # break replacement in exactly the showcase moment. The loser's lines are not
    # lost: `just_now` carries the whole delta as facts.
    BeatRule(
        "area_entered",
        "You are the referee reading this place aloud as the party arrives. The "
        "location text is your private module copy — it may hold secrets, traps, "
        "treasure, lurking creatures, or advice meant only for you. Describe what "
        "the party immediately perceives and nothing more; keep every secret "
        "unspoken; add no new map facts.",
        _area_entered,
    ),
    BeatRule(
        "encounter_started",
        "Dread and first impressions; keep the monster count and kind exactly as stated.",
        _encounter_started,
    ),
    BeatRule(
        "battle_ended",
        "Give the whole fight its shape — victory, flight, or defeat in tone.",
        _battle_ended,
    ),
    BeatRule(
        "member_died",
        "A short elegy; name the fallen, nothing more.",
        _member_died,
    ),
    # Below member_died, above leveled_up: the same elegy-over-celebration rule
    # in miniature — a non-concluding quest's `award_xp` reward can level a
    # member in the completing run, and the level is the completion's side
    # effect, not the story.
    BeatRule(
        "quest_completed",
        "A quest is done; give the moment its weight. Any authored completion "
        "line has already been told word for word: never restate or rephrase "
        "it — put the prose around it, and claim no reward the facts do not name.",
        _quest_completed,
    ),
    # Directly below quest_completed, and above treasure_found: the fetch
    # objective completes in the same delta as the haul it was lying in, and the
    # moment's meaning is the party finding the thing it came for, not the shine
    # of the cache beside it.
    BeatRule(
        "objective_completed",
        "The story advances a step; mark what was just accomplished. Keep the "
        "objective and its quest exactly as named. Any authored progress line "
        "has already been told word for word: weave around it, never over it.",
        _objective_completed,
    ),
    # Above trap_sprung but below the deaths and completions: a death and a level
    # in one delta must read as elegy, not celebration.
    BeatRule(
        "leveled_up",
        "A hard-won milestone; state the new level, title, and hit points "
        "exactly as given — grant no new powers, and promise nothing about "
        "what comes next.",
        _leveled_up,
    ),
    BeatRule(
        "trap_sprung",
        "Alarm and sudden danger; report the trap's outcome exactly as the facts "
        "state it and add no injury they do not name. A trap that harmed no one "
        "hurt no one — narrate the shock and the near miss, never a wound.",
        _trap_sprung,
    ),
    # Below trap_sprung, above treasure_found: sudden violence in the same delta
    # (a trapped threshold that wakes a quest) reads as the trap, but a quest
    # waking on an acquisition is that acquisition's meaning.
    BeatRule(
        "quest_activated",
        "A charge is laid on the party — give the moment the weight of an errand "
        "accepted, in the voice the facts give it. Any authored offer has already "
        "been delivered word for word: never repeat or reword it, and name no "
        "speaker, promise, price, or condition the facts do not state.",
        _quest_activated,
    ),
    BeatRule(
        "treasure_found",
        "Wonder; describe only the listed items.",
        _treasure_found,
    ),
    BeatRule(
        "game_over",
        "A closing paragraph for the campaign.",
        _game_over,
    ),
)


def detect_beat(
    events: Sequence,
    entries: Sequence[dict],
    party_ids: set[str],
    in_dungeon: bool = True,
) -> BeatRule | None:
    """At most one beat per command: the first matching table row wins.

    Args:
        events: The player-visible engine events of one command's log delta.
        entries: The transcript entries rendered from those events.
        party_ids: The party's member ids, so a member's death can be told
            apart from a monster's.
        in_dungeon: False in town, which gates the `treasure_found` beat
            (commerce also emits `item_acquired`).

    Returns:
        The highest-priority matching rule, or None when nothing warrants prose.
    """
    for rule in BEATS:
        if rule.match(events, entries, party_ids, in_dungeon):
            return rule
    return None


def build_prompt(
    *,
    adventure: str,
    party: str,
    location: str,
    recent: str,
    moments: list[tuple[str, str]],
    emphasis: str,
    guidance: str = "",
) -> str:
    """Assemble the user prompt from snapshotted strings.

    Takes strings only, never the session or events — the player-safety
    property is structural: this function cannot leak what it is never handed.

    `guidance` is the one exception to the player-visible rule, and it is a
    named one: the authored referee steering
    [`assemble_guidance`][server.narration.assemble_guidance] reads off the
    adventure document, rendered under
    [`GUIDANCE_HEADER`][server.narration.GUIDANCE_HEADER] nearest the closing
    instruction, where a small local model actually obeys it. It arrives as a
    plain string like everything else. Empty is the default and renders nothing
    at all, so an adventure authoring no guidance builds a prompt byte-identical
    to one built before the channel existed.
    """
    lines = [
        f"ADVENTURE: {adventure}",
        f"PARTY: {party}",
        f"LOCATION: {location}",
        "RECENT EVENTS:",
        recent if recent else "(the tale is just beginning)",
    ]
    for name, block in moments:
        lines.append(f"JUST NOW ({name}):")
        lines.append(block)
    if guidance:
        lines.append(GUIDANCE_HEADER)
        lines.append(guidance)
    lines.append("")
    lines.append(f"Narrate this moment. {emphasis}")
    return "\n".join(lines)


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_MARKDOWN_CHARS = re.compile(r"[*_#`]+")
_QUOTE_CHARS = "\"'“”‘’"


def sanitize(text: str) -> str:
    """Clean one completion into a single transcript-ready passage, or ''."""
    text = _THINK_BLOCK.sub("", text)  # reasoning models may leak their scratchpad
    text = re.sub(r"\s+", " ", text).strip()
    text = _MARKDOWN_CHARS.sub("", text).strip()
    while len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        text = text[1:-1].strip()
    if len(text) > _MAX_PASSAGE_CHARS:
        clipped = text[:_MAX_PASSAGE_CHARS]
        boundary = max(clipped.rfind("."), clipped.rfind("!"), clipped.rfind("?"))
        text = (clipped[: boundary + 1] if boundary > 0 else clipped).strip()
    return text


def _partial_text(raw: str) -> str:
    """Best-effort clean of an in-flight buffer for live display.

    Unlike [`sanitize`][server.narration.sanitize] this never trims to a
    sentence boundary or strips a closing quote — the passage is still growing,
    so those would make the visible text flicker. It only removes what would
    look wrong on screen right now: finished reasoning blocks, an *unclosed*
    reasoning block (the model is still thinking — reveal nothing yet), markdown,
    a leading quote, and anything past the hard length cap. The stored passage
    always re-runs the full `sanitize` on finalize.
    """
    text = _THINK_BLOCK.sub("", raw)
    open_think = text.rfind("<think>")
    if open_think != -1:
        text = text[:open_think]
    text = re.sub(r"\s+", " ", text)
    text = _MARKDOWN_CHARS.sub("", text).lstrip(_QUOTE_CHARS).strip()
    return text[:_MAX_PASSAGE_CHARS]


@dataclass(frozen=True)
class BeatSnapshot:
    """Everything one beat's prompt needs, captured as plain strings.

    `replace_event` is set for area_entered beats: the event-log index of the
    `location_entered` event whose module prose the passage will stand in for.

    `guidance` is the authored steering in play at this beat, one string per
    carrier block in assembly order. A tuple rather than a joined string because
    coalescing merges batches block by block.
    """

    beat: str
    emphasis: str
    anchor: int
    adventure: str
    party: str
    location: str
    recent: str
    just_now: str
    replace_event: int | None = None
    guidance: tuple[str, ...] = ()


@dataclass(frozen=True)
class Passage:
    """One generated passage, belonging after event_log[:anchor].

    `replaces` lists the event-log indexes of `location_entered` events whose
    rendered `prose` entries this passage stands in for (replacement mode).
    """

    seq: int
    anchor: int
    text: str
    replaces: tuple[int, ...] = ()


def annotate_replacement(events: Sequence, entries: Sequence[dict], base_index: int) -> int | None:
    """Tag one command's first-visit area prose for client-side replacement.

    Stamps the entry carrying the module's own room description — the one the
    renderer marked [`MODULE_PROSE`][server.narrate.MODULE_PROSE] — with a
    `replace_id`, the event-log index of the `location_entered` event it was
    rendered from, so the live client can hold the module text back until the
    passage lands (or reveal it if generation fails).

    The marker is what identifies it. Adjacency to a place header does not: an
    authored beat renders as `prose` too, so a repeatable trigger's journal line
    on an already-seen area is a place + prose pair the model would have been
    asked to stand in for — replacing the author's words, and dropping them
    outright on reload.

    Args:
        events: The engine events of one command's log delta.
        entries: The transcript entries rendered from those events; the
            marked entry is mutated in place.
        base_index: The full event log's length before this delta, so offsets
            within `events` translate to absolute log indexes.

    Returns:
        The tagged event's absolute log index, or None when the delta carries
        no first-visit area prose.
    """
    event_index = next(
        (
            base_index + offset
            for offset, event in enumerate(events)
            if getattr(event, "event_type", "") == "location_entered" and getattr(event, "location_kind", "") == "area"
        ),
        None,
    )
    if event_index is None:
        return None
    for entry in entries:
        if entry.get(MODULE_PROSE):
            entry["replace_id"] = event_index
            return event_index
    return None


def _entry_lines(entries) -> list[str]:
    return [f"[{entry['kind']}] {entry['text']}" for entry in entries]


def _condition_phrase(member) -> str:
    ratio = member.current_hp / max(1, member.max_hp)
    if ratio >= 1:
        words = ["unhurt"]
    elif ratio >= 0.5:
        words = ["wounded"]
    else:
        words = ["badly wounded"]
    words.extend(condition for condition in member.conditions if condition != "dead")
    return ", ".join(words)


def party_summary(view) -> str:
    """The living party as one line, from the player projection only."""
    parts = []
    for member in view.party:
        if member.current_hp <= 0 or "dead" in member.conditions:
            continue
        class_name = member.class_id.replace("_", "-")
        parts.append(f"{member.name} the {class_name} (level {member.level}, {_condition_phrase(member)})")
    return "; ".join(parts) if parts else "no one left standing"


def location_line(session) -> str:
    """The party's current place, described as the module wrote it."""
    location = session.dungeon_state.location
    if location.kind != "dungeon":
        name = getattr(getattr(session.adventure, "town", None), "name", "") or "town"
        return f"the town of {name}"
    try:
        level = session.adventure.dungeon(location.dungeon_id).level(location.level_number)
    except ValueError:
        return "somewhere beneath the earth"
    area = level.area_at(location.position)
    if area is None:
        return "an unmarked stretch of the dungeon"
    name = area.name or f"Area {area.id}"
    description = (area.description or "").strip()
    return f"{name}: {description}" if description else name


def _block_guidance(block) -> str:
    """One narrative block's steering, or `""` when there is none to read.

    Total by design: the block may be `None` (unauthored carrier) and its
    `guidance` may be the empty default, and both mean the same thing here.
    """
    return (getattr(block, "guidance", "") or "").strip()


def _level_guidance(session) -> list[str]:
    """The occupied level's ambient slot; nothing in town."""
    location = session.dungeon_state.location
    if location.kind != "dungeon":
        return []
    try:
        level = session.adventure.dungeon(location.dungeon_id).level(location.level_number)
    except ValueError:
        return []
    text = (level.guidance or "").strip()
    return [text] if text else []


def _quest_guidance(session, events) -> list[str]:
    """Every quest in play, each followed by the objectives riding it."""
    completed_quests = {
        getattr(event, "quest_id", "") for event in events if getattr(event, "event_type", "") == "quest_completed"
    }
    completed_objectives = {
        (getattr(event, "quest_id", ""), getattr(event, "objective_id", ""))
        for event in events
        if getattr(event, "event_type", "") == "objective_completed"
    }
    blocks: list[str] = []
    for quest in session.adventure.quests:
        state = session.quests.get(quest.id)
        in_play = state is not None and state.status == "active"
        if not in_play and quest.id not in completed_quests:
            continue
        text = _block_guidance(quest.narrative)
        if text:
            blocks.append(text)
        for objective in quest.objectives:
            objective_state = None if state is None else state.objectives.get(objective.id)
            riding = objective_state is not None and objective_state.revealed and not objective_state.complete
            if not riding and (quest.id, objective.id) not in completed_objectives:
                continue
            text = _block_guidance(objective.narrative)
            if text:
                blocks.append(text)
    return blocks


def _trigger_guidance(session, events) -> list[str]:
    """The steering of every trigger that fired in this run, in event order.

    `trigger_fired` is referee-visible, which is exactly why it is read here and
    only here: the raw delta is inspected server-side, under the game lock, and
    what comes back is steering that never crosses the wire.
    """
    blocks: list[str] = []
    for event in events:
        if getattr(event, "event_type", "") != "trigger_fired":
            continue
        trigger_id = getattr(event, "trigger_id", "")
        spec = next(
            (trigger for trigger in session.adventure.triggers if trigger.id == trigger_id),
            None,
        )
        text = _block_guidance(getattr(spec, "narrative", None))
        if text:
            blocks.append(text)
    return blocks


def _gate_guidance(session, events) -> list[str]:
    """A door gate's steering, on the run carrying its authored success beat.

    Door form only. A gated *transition*'s success beat rides the arrival's
    `LocationEnteredEvent`, which names where the party arrived and nothing about
    the crossing it took, so no honest mapping back to the transition spec
    exists — that waits on the engine naming the crossing
    (mmacy/osrlib-python#71), and no reverse search over the document's
    transitions gets built here.

    The relocation guard is the honesty argument for reading the session's own
    location: no trigger pattern matches a door event, so an open or force
    command cannot move the party *except* through a door trap's transition
    effect — which emits `location_entered`. A delta carrying one drops the gate
    block rather than resolving the edge against whichever level the party ended
    up on. A missed lookup at any step yields no block, never a fallback.
    """
    if any(getattr(event, "event_type", "") == "location_entered" for event in events):
        return []
    location = session.dungeon_state.location
    if location.kind != "dungeon":
        return []
    blocks: list[str] = []
    for event in events:
        if getattr(event, "event_type", "") != "door":
            continue
        if not getattr(event, "narrative", None):
            continue
        try:
            level = session.adventure.dungeon(location.dungeon_id).level(location.level_number)
            # `LevelSpec.edge` canonicalizes, so the cell the event names and the
            # cell on the far side resolve to the same authored entry.
            edge = level.edge((event.x, event.y), Direction(event.direction))
        except ValueError:
            continue
        door = getattr(edge, "door", None)
        gate = getattr(door, "requires", None)
        text = _block_guidance(getattr(gate, "narrative", None))
        if text:
            blocks.append(text)
    return blocks


def assemble_guidance(session, events) -> tuple[str, ...]:
    """The authored steering in play at one beat, ambient to specific.

    The one place in the app that reads `guidance` off the adventure document.
    Everything it returns is referee-only: it reaches a prompt through
    [`build_prompt`][server.narration.build_prompt]'s `guidance` parameter and no
    other surface, and it crosses the wire on none.

    Order within a snapshot is the place, then the standing charge, then the
    thing that just happened: level → quests (each quest's block, then its riding
    objectives', quests in document order and objectives in authored order) →
    triggers (in event order) → gate.

    The windows are osrlib's — save the objective's riding rule (revealed and
    not complete), which is this app's own. The level's is post-command occupancy — the honest
    reading of "while occupied": a run that crosses levels is steered by the
    level it lands on, never the one it left. A trigger's and a gate's are the
    firing and success runs themselves, delta-scoped by their own events, so
    their edges take care of themselves. The quest and objective carriers get the
    one edge rule: **they steer every run from the one that brings them into play
    through the one that takes them out, both inclusive.** Live state at snapshot
    time is post-command, so a completing quest reads `completed` in the very run
    that is its completion — and the engine's own matching stance is that events
    are judged by the moment they describe, never by current state. Hence the
    event checks beside the state ones. The activation edge needs no such case:
    the activating run's post-command status is already `active`.

    Args:
        session: The live session, read under the game lock the command path
            already holds.
        events: The raw log delta of one command — referee events included, which
            is what lets a fired trigger be attributed at all.

    Returns:
        The blocks in play, in assembly order. Empty means the adventure authors
        no guidance for this moment (or authors none at all).
    """
    return tuple(
        _level_guidance(session)
        + _quest_guidance(session, events)
        + _trigger_guidance(session, events)
        + _gate_guidance(session, events)
    )


def adventure_line(adventure) -> str:
    """The adventure's name and description as one line."""
    description = (getattr(adventure, "description", "") or "").strip()
    return f"{adventure.name} — {description}" if description else adventure.name


class NarrationEngine:
    """Per-game beat queue, daemon worker, and passage store.

    Lock choice: passages, queue, and counters live behind the engine's own
    lock rather than the game lock, so the poll endpoint and the worker never
    contend with command execution. `observe` is the only method that touches
    the session, and its caller already holds the game lock — the `_recent`
    transcript buffer is therefore game-lock-guarded, everything else here is
    engine-lock-guarded.
    """

    QUEUE_CAPACITY = 4
    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, provider: NarrativeProvider, timeout: float):
        """Start the engine against `provider`, enabled, idle, and with an empty queue."""
        self.provider = provider
        self.timeout = timeout
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queue: deque[BeatSnapshot] = deque()
        self._passages: list[Passage] = []
        # The generation in flight, revealed to the client as a growing passage
        # before it finalizes into `_passages`. `seq` is tentative — the value
        # the passage *will* take on success (highest finalized seq + 1) — so a
        # failed beat never consumes a seq. None between generations.
        self._partial: dict | None = None
        self._pending = 0
        self._seq = 0
        self._failures = 0
        self._enabled = True
        self._worker: threading.Thread | None = None
        self._recent: deque[str] = deque(maxlen=_RECENT_ENTRIES)

    @property
    def enabled(self) -> bool:
        """Whether the engine is still accepting beats, after any auto-disable on failure."""
        with self._lock:
            return self._enabled

    def observe(self, session, events, entries) -> None:
        """Detect a beat in one command's freshly rendered entries.

        Runs under the game lock; string snapshots only, no I/O. `events` is
        the log delta the entries were rendered from.
        """
        if not entries:
            return
        recent = "\n".join(self._recent)
        self._recent.extend(_entry_lines(entries))
        if not self.enabled:
            return
        visible = [event for event in events if getattr(event, "visibility", None) is Visibility.PLAYER]
        party_ids = {member.id for member in session.party.members}
        in_dungeon = session.dungeon_state.location.kind == "dungeon"
        rule = detect_beat(visible, entries, party_ids, in_dungeon)
        if rule is None:
            return
        replace_event = None
        if rule.name == "area_entered":
            base_index = len(session.event_log) - len(events)
            replace_event = annotate_replacement(events, entries, base_index)
        snapshot = BeatSnapshot(
            beat=rule.name,
            emphasis=rule.emphasis,
            anchor=len(session.event_log),
            adventure=adventure_line(session.adventure),
            party=party_summary(session.view(Visibility.PLAYER)),
            location=location_line(session),
            recent=recent,
            just_now="\n".join(_entry_lines(entries)),
            replace_event=replace_event,
            guidance=assemble_guidance(session, events),
        )
        self.enqueue(snapshot)

    def enqueue(self, snapshot: BeatSnapshot) -> None:
        """Queue one beat for generation, dropping the oldest when full."""
        with self._wake:
            if not self._enabled:
                return
            if len(self._queue) >= self.QUEUE_CAPACITY:
                self._queue.popleft()
                self._pending -= 1
            self._queue.append(snapshot)
            self._pending += 1
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="narration-worker", daemon=True)
                self._worker.start()
            self._wake.notify()

    def state(self) -> dict:
        """The player-safe narration block for the state payload."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "pending": self._pending,
                "seq": self._seq,
            }

    def tail(self, after: int) -> dict:
        """Finalized passages with seq > after, the live partial, and pending.

        The `partial` block (or None) carries the growing text of the beat
        currently generating; it shares a seq with the `entries` item it will
        become, so the client can settle the same element in place. Only
        finalized `entries` are ground truth — a partial that never finalizes
        (mid-stream failure) simply stops being reported.
        """
        with self._lock:
            entries = [
                {
                    "seq": passage.seq,
                    "anchor": passage.anchor,
                    "kind": "narrative",
                    "text": passage.text,
                    "replaces": list(passage.replaces),
                }
                for passage in self._passages
                if passage.seq > after
            ]
            partial = None
            if self._partial is not None and self._partial["seq"] > after:
                partial = {
                    "seq": self._partial["seq"],
                    "anchor": self._partial["anchor"],
                    "kind": "narrative",
                    "text": self._partial["text"],
                    "replaces": list(self._partial["replaces"]),
                    "done": False,
                }
            return {"entries": entries, "partial": partial, "pending": self._pending}

    def passages(self) -> list[Passage]:
        """A snapshot of stored passages, for the reload interleave."""
        with self._lock:
            return list(self._passages)

    def _run(self) -> None:
        while True:
            with self._wake:
                while self._enabled and not self._queue:
                    self._wake.wait()
                if not self._enabled:
                    return
                # Coalesce: a player racing ahead gets one merged passage,
                # not a backlog.
                batch = list(self._queue)
                self._queue.clear()
            newest = batch[-1]
            replaces = tuple(beat.replace_event for beat in batch if beat.replace_event is not None)
            # A coalesced beat's steering is neither dropped nor carried past its
            # window: the batch's blocks merge oldest-first, the same order the
            # JUST NOW moments take, and an exact repeat keeps its first
            # occurrence — the level and the standing quest ride every snapshot.
            merged: list[str] = []
            for beat in batch:
                for block in beat.guidance:
                    if block not in merged:
                        merged.append(block)
            prompt = build_prompt(
                adventure=newest.adventure,
                party=newest.party,
                location=newest.location,
                recent=newest.recent,
                moments=[(beat.beat, beat.just_now) for beat in batch],
                emphasis=newest.emphasis,
                guidance="\n".join(merged),
            )
            with self._wake:
                # Reserve the seq this passage will take on success. Held apart
                # from `_seq` so a failure leaves the counter untouched.
                self._partial = {
                    "seq": self._seq + 1,
                    "anchor": newest.anchor,
                    "replaces": replaces,
                    "text": "",
                }
            buffer: list[str] = []
            try:
                for delta in self.provider.generate_stream(SYSTEM_PROMPT, prompt, timeout=self.timeout):
                    buffer.append(delta)
                    display = _partial_text("".join(buffer))
                    with self._wake:
                        if self._partial is None:  # disabled out from under us
                            break
                        self._partial["text"] = display
            except ProviderError as error:
                _log.debug("narration beat dropped: %s", error)
                with self._wake:
                    self._partial = None
                    self._pending -= len(batch)
                    self._failures += 1
                    if self._failures >= self.MAX_CONSECUTIVE_FAILURES:
                        # The model has almost certainly gone away; silence is
                        # better than a stall on every beat.
                        self._enabled = False
                        self._pending = 0
                        self._queue.clear()
                        return
                continue
            text = sanitize("".join(buffer))
            with self._wake:
                self._failures = 0
                self._pending -= len(batch)
                self._partial = None
                if text:
                    self._seq += 1
                    self._passages.append(
                        Passage(
                            seq=self._seq,
                            anchor=newest.anchor,
                            text=text,
                            replaces=replaces,
                        )
                    )


def interleave(
    render: Callable[[Sequence], list[dict]],
    events: Sequence,
    passages: Sequence[Passage],
) -> list[dict]:
    """Rebuild a full transcript with passages spliced in at their anchors.

    A passage that replaces module prose splices in right after the place
    header it describes, and the replaced module-prose entries are dropped; a
    plain passage splices at its anchor. The drop is keyed to the
    [`MODULE_PROSE`][server.narrate.MODULE_PROSE] marker, so an authored beat
    rendered from the same event would survive — the passage stands in for the
    document's room description and for nothing else.

    Events are handed over in the longest runs the splice points allow, never one
    at a time: the renderer collapses a distributed treasure haul's per-member
    events into one line, and slicing that run apart would give a reloaded
    transcript more lines than the live one had. Only an event whose prose is
    being replaced is rendered alone, so the marker filter still applies to
    exactly that event and nothing else.

    Args:
        render: `Narrator.render` (or a stand-in with its signature); its
            seen-areas and label state accumulate correctly across sequential
            calls, so this is a loop around existing machinery.
        events: The session's full event log.
        passages: Stored passages in `seq` order; their `anchor` and
            `replaces` indexes refer to positions in `events`.

    Returns:
        The complete transcript, deterministic entries and passages in order.
    """
    replaced = {index for passage in passages for index in passage.replaces}
    entries: list[dict] = []
    position = 0

    def render_to(stop: int) -> None:
        nonlocal position
        stop = min(max(stop, position), len(events))
        while position < stop:
            if position in replaced:
                rendered = render(events[position : position + 1])
                entries.extend(e for e in rendered if not e.get(MODULE_PROSE))
                position += 1
                continue
            run = position + 1
            while run < stop and run not in replaced:
                run += 1
            entries.extend(render(events[position:run]))
            position = run

    for passage in passages:
        splice = passage.replaces[-1] + 1 if passage.replaces else passage.anchor
        render_to(splice)
        entries.append({"kind": "narrative", "text": passage.text})
    render_to(len(events))
    return entries
