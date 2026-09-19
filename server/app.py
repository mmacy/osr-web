"""The osr-web API: in-memory `GameSession` objects behind a small FastAPI JSON surface.

`GameSession` is not thread-safe, so every session lives beside its own lock,
held across every execute and view read. Only the player projection and
player-visible events cross the wire; the master seed stays server-side. An
in-fiction rejection is a 200 (`accepted: false`); malformed content is a 422.

Beyond the wire contract, the server enriches responses with player-safe
context the projection alone doesn't carry: English narration with names
resolved (`server.narrate`), the current area's name, visible treasure
features on the party's cell, and the party's light state.
"""

import json
import logging
import os
import re
import secrets
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from osrlib.core.abilities import AbilityScore
from osrlib.core.character import Character
from osrlib.core.classes import level_title, xp_modifier_pct
from osrlib.core.effects import Condition, has_condition
from osrlib.core.events import Visibility
from osrlib.core.items import MAX_LOAD_COINS, tracked_weight_coins
from osrlib.core.spells import caster_profile, open_book_capacity
from osrlib.core.validation import Rejection
from osrlib.crawl.commands import parse_command
from osrlib.crawl.dungeon import PartyLocation, Position, cell_ref
from osrlib.crawl.exploration import HEALING_SERVICES
from osrlib.crawl.interpreter import Interpreter
from osrlib.crawl.session import GameSession
from osrlib.data import load_ability_tables, load_spells
from osrlib.errors import ContentValidationError, SaveVersionError
from osrlib.persistence import save_game
from osrlib.versioning import SCHEMA_VERSION, engine_version

from .content import new_session, party_from_save, restore_session
from .creation import (
    ABILITY_METHODS,
    SEED_BOUND,
    HouseRules,
    PartyBuilder,
    creation_catalog,
    creation_rejection_text,
)
from .library import (
    add_adventure,
    adventure_entry,
    list_adventures,
    remove_adventure,
)
from .llm import configure_narration
from .narrate import Narrator, rejection_text
from .narration import NarrationEngine, interleave

app = FastAPI(
    title="osr-web",
    description="An old-school dungeon crawler through any adventure osr-forge has stamped.",
)

# The default saves directory; read it through `_saves_dir()`, which applies the
# OSR_WEB_SAVES_DIR override.
_SAVES_DIR = Path(__file__).resolve().parent.parent / "saves"
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _saves_dir() -> Path:
    """The directory save documents are written to and listed from.

    `OSR_WEB_SAVES_DIR` replaces the repo `saves/` directory with one of your own —
    a temp directory for a test or a screenshot run, so no local save (and no
    adventure name it carries) reaches the title screen. It has nothing to do with
    the adventure library's `OSR_WEB_ADVENTURES` / `OSR_WEB_ADVENTURES_DIR`.

    The environment is read on every call, so a test can redirect saves with
    `monkeypatch.setenv`. Every read and write goes through here, so the list and
    the write path can never disagree.

    Returns:
        The override with `~` expanded, or the repo `saves/` directory when
        `OSR_WEB_SAVES_DIR` is unset or blank.
    """
    override = os.environ.get("OSR_WEB_SAVES_DIR", "").strip()
    return Path(override).expanduser() if override else _SAVES_DIR


def _wire_app_logging() -> None:
    """Give this app's own log records somewhere to go.

    uvicorn configures only its own `uvicorn*` loggers and never touches the root
    logger, which keeps the default `WARNING` level and no handler at all. Without
    this wiring, an `INFO` record from `osr_web.*` reaches no handler, so a
    `.env` that turns narration on does so in complete silence, and a `WARNING`
    escapes only through `logging.lastResort`, printing bare text with no level
    prefix.

    Pinning the `osr_web` namespace at `INFO` and lending it uvicorn's own handler
    puts every line this app writes in the same stream, format, and colour as
    `Application startup complete`. Outside uvicorn — a test importing the module,
    some other ASGI server — it falls back to a plain stderr handler, which is what
    `lastResort` was already doing. Nothing is added when records are already going
    somewhere, and propagation stays on, so pytest's `caplog` still sees them all.
    """
    app_log = logging.getLogger("osr_web")
    app_log.setLevel(logging.INFO)
    if app_log.handlers or logging.getLogger().handlers:
        return
    # uvicorn's default config hangs its handler off `uvicorn` and lets
    # `uvicorn.error` propagate up to it; a custom --log-config may do either.
    borrowed = logging.getLogger("uvicorn").handlers or logging.getLogger("uvicorn.error").handlers
    app_log.handlers = list(borrowed) or [logging.StreamHandler()]


_wire_app_logging()

# .env fills in what the process environment doesn't set (see .env.example).
# Snapshot the narrator switch first: python-dotenv's default `override=False`
# leaves a variable that is present-but-empty alone and fills in one that is
# absent, so anything that appears across this call is a value the operator's
# shell never mentioned. Saying so in the startup line is the difference between
# reporting narration is on and explaining why it is.
_narrator_preset = "OSR_WEB_NARRATOR" in os.environ
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Read once at startup; None keeps the hot path a no-op (spec: llm-narrative).
_NARRATION = configure_narration(from_dotenv=not _narrator_preset and "OSR_WEB_NARRATOR" in os.environ)

_referee_log = logging.getLogger("osr_web.referee")
"""The referee's screen is the operator's terminal.

A child of the `osr_web` namespace `_wire_app_logging` pins at INFO and lends
uvicorn's handler, so these lines land in the launch stream beside everything
else the app says.
"""


def _one_line(value) -> str:
    """One log field's worth of text, collapsed onto a single line.

    Trigger ids, authored beats, and note text all originate in an adventure
    document, and an uploaded document is content the server did not write. A
    newline in any of them forges whole lines on the operator's terminal — the
    one screen the referee's channel is trusted on — so every interpolated value
    is flattened before it reaches a record.
    """
    return " ".join(str(value).split())


def _log_referee(events) -> None:
    """Route the interpreter's referee-visible events to the server log.

    The authored `fired` beat of a trigger, a dropped consequence, a reward that
    could not land, a cascade cut short — all of it rides referee visibility, so
    the narrator discards it and the wire never carries it. This is its only
    consumer.

    Args:
        events: One command's event-log delta, referee events included.
    """
    for event in events:
        event_type = getattr(event, "event_type", "")
        if event_type == "trigger_fired":
            narrative = _one_line(getattr(event, "narrative", None) or "")
            suffix = f" — {narrative}" if narrative else ""
            _referee_log.info("trigger fired: %s%s", _one_line(event.trigger_id), suffix)
        elif event_type == "note_recorded":
            _referee_log.warning("referee note: %s", _one_line(event.text))


class Game:
    """One running game: the session, its lock, its narrator, and LLM color."""

    def __init__(self, session: GameSession):
        """Wrap `session`, registering the interpreter and starting the narrator and lock."""
        self.session = session
        # The interpreter is registered here and nowhere else: every session
        # route — premade, builder party, veteran import, save restore —
        # converges on this constructor, listeners are code a save never
        # carries, and registering a second time would fire every trigger twice
        # (`register_listener` never dedupes). Forgetting it is silent: gates
        # still work, but no trigger fires and no quest ever advances.
        session.register_listener(Interpreter(session))
        self.lock = threading.Lock()
        self.narrator = Narrator(session)
        self.narration = NarrationEngine(_NARRATION.provider, _NARRATION.timeout) if _NARRATION is not None else None


_store_lock = threading.Lock()
_games: dict[str, Game] = {}
_builders: dict[str, PartyBuilder] = {}

# Sessions and builders are in-memory and would grow without bound
# (every title-screen browse would leak one). Creation is lazy, but
# an abandoned session still lingers until the client deletes it, so both
# stores keep least-recently-used order (touched on access, evicted on insert)
# as hygiene. The caps are far above what one player can reach.
_MAX_GAMES = 24
_MAX_BUILDERS = 24


def _remember(store: dict, key: str, value, cap: int) -> None:
    """Insert into an LRU store under `_store_lock`, evicting the stalest past `cap`."""
    with _store_lock:
        store[key] = value
        while len(store) > cap:
            store.pop(next(iter(store)))


def _touch(store: dict, key: str):
    """Fetch from an LRU store, moving the hit to most-recently-used."""
    with _store_lock:
        value = store.pop(key, None)
        if value is not None:
            store[key] = value
    return value


@app.exception_handler(ContentValidationError)
def _content_validation_error(request: Request, error: ContentValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(error)})


@app.exception_handler(SaveVersionError)
def _save_version_error(request: Request, error: SaveVersionError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(error)})


def _game_or_404(game_id: str) -> Game:
    game = _touch(_games, game_id)
    if game is None:
        raise HTTPException(status_code=404, detail=f"unknown game id {game_id!r}")
    return game


def _members_by_id(session: GameSession) -> dict[str, Character]:
    """The party keyed by member id, which is how every per-member payload is keyed.

    A `Character` carries no id until a session seats it — osrlib's allocator
    fills one in for every member of a running party — so this is the one place
    that states the fact, and the payload builders below key on a plain string.
    """
    return {member.id: member for member in session.party.members if member.id is not None}


def _dungeon_cell(location: PartyLocation) -> tuple[str, int, Position] | None:
    """Where the party stands as dungeon, level, and cell, or `None` when it is in town.

    `PartyLocation` fills all four dungeon fields together or none of them, and
    its own validator enforces that, so this unpacks the three the callers need
    once instead of each reading them back off an optional.
    """
    if location.kind != "dungeon":
        return None
    if location.dungeon_id is None or location.level_number is None or location.position is None:
        return None
    return location.dungeon_id, location.level_number, location.position


def _preparable(member) -> list[str]:
    """Spell ids this member may prepare, in the order the prep UI lists them.

    Leak review: an arcane caster's list is their own spell book — the player's
    own record. A divine caster has no book; their list is the class spell list
    (rulebook-public, matched by `CasterProfile.spell_list`), truncated to the
    spell levels their own progression row grants slots for and sorted by
    (level, name). Non-casters prepare nothing. No referee state is read.
    """
    profile = caster_profile(member.definition)
    if profile is None:
        return []
    if profile.kind == "arcane":
        return list(member.spell_book)
    slots = member.definition.row(member.level).spell_slots
    catalog = load_spells()
    templates = [
        spell
        for level, count in enumerate(slots, start=1)
        if count > 0
        for spell in catalog.by_list(profile.spell_list, level)
    ]
    return [spell.id for spell in sorted(templates, key=lambda s: (s.level, s.name))]


def _learnable(session: GameSession) -> dict:
    """Per-member spells an arcane caster's book has room to take.

    Only members with at least one open pick get a key. Per spell level with an
    open count, the candidates are the class spell list at that level minus the
    spells the book already holds, sorted by (level, name); each entry carries
    `id`, `name`, `level`, and `intro`.

    Leak review: a class's spell list is rulebook-public data any B/X player
    owns, and the book is the player's own record — the same two sources
    [`_preparable`][server.app._preparable] reads. The open-pick counts restate
    the member's own progression row against that book. No referee state is
    read.
    """
    catalog = load_spells()
    learnable: dict[str, list[dict]] = {}
    for member_id, member in _members_by_id(session).items():
        definition = member.definition
        picks = open_book_capacity(member, definition, catalog)
        profile = caster_profile(definition)
        if profile is None or not any(picks):
            continue
        held = set(member.spell_book)
        candidates = sorted(
            (
                spell
                for level, count in enumerate(picks, start=1)
                if count > 0
                for spell in catalog.by_list(profile.spell_list, level)
                if spell.id not in held
            ),
            key=lambda spell: (spell.level, spell.name),
        )
        if candidates:
            learnable[member_id] = [
                {
                    "id": spell.id,
                    "name": spell.name,
                    "level": spell.level,
                    "intro": spell.intro,
                }
                for spell in candidates
            ]
    return learnable


def _spellbook(session: GameSession) -> dict:
    """Castable and preparable spells across the party: name, mode, target, level."""
    preferred_modes = {"sleep": "hd_budget"}
    book: dict[str, dict] = {}
    spell_ids = {copy.spell_id for member in session.party.members for copy in member.memorized_spells}
    for member in session.party.members:
        spell_ids.update(_preparable(member))
    catalog = load_spells()
    for spell_id in spell_ids:
        try:
            spell = catalog.get(spell_id)
        except ValueError:
            continue
        modes = [mode for mode in spell.modes if not mode.manual] or list(spell.modes)
        mode = next((m for m in modes if m.key == preferred_modes.get(spell_id)), modes[0])
        targeting = mode.targeting
        target = "none"
        if targeting is not None:
            targeting_mode = targeting.mode.value
            if targeting_mode == "self":
                target = "self"
            elif mode.effect is not None and mode.effect.kind == "heal":
                target = "member"
            else:
                target = "group"
        book[spell_id] = {
            "name": spell.name,
            "mode": mode.key,
            "target": target,
            "level": spell.level,
        }
    return book


def _sheets(session: GameSession) -> dict:
    """Per-member character-sheet data the projection doesn't carry.

    Everything here is printed on the player's own B/X character sheet —
    scores, saves, AC, THAC0, movement, XP, languages — derived from the
    session's Character objects. Nothing referee-only is touched.

    `spell_slots` is the member's own progression row (rulebook data); `rested`
    says whether a night's sleep is banked for spell preparation — a fact the
    player lived through, derived from the sleep ledger, never revealing it.
    `level_title` is the class table's title at the member's level (rulebook
    data, `None` past the printed list); `spell_picks` restates that row
    against the member's own spell book — per-spell-level open book slots,
    same index convention as `spell_slots`, empty for everyone but arcane
    casters.
    """
    tables = load_ability_tables()
    catalog = load_spells()
    sheets: dict[str, dict] = {}
    for member_id, member in _members_by_id(session).items():
        definition = member.definition
        next_row = definition.row(member.level + 1) if member.level < definition.max_level else None
        sheets[member_id] = {
            "race": member.race,
            "alignment": member.alignment.value,
            "class_name": definition.name,
            "xp": member.xp,
            "next_level_xp": next_row.xp if next_row is not None else None,
            "xp_bonus_pct": xp_modifier_pct(definition, member.scores),
            "scores": {ability.value: score for ability, score in member.scores.items()},
            "modifiers": {
                "melee": member.melee_modifier,
                "missile": member.missile_modifier,
                "armour_class": tables.ac_modifier(member.scores[AbilityScore.DEX]),
                "hit_points": member.hit_point_modifier,
                "magic_saves": member.magic_save_modifier,
                "reactions": member.npc_reaction_modifier,
                "open_doors": member.open_doors_chance,
            },
            "saves": member.saves.model_dump(),
            "thac0": member.thac0,
            "attack_bonus": member.attack_bonus,
            "armour_class": member.armour_class,
            "armour_class_ascending": member.armour_class_ascending,
            "movement_per_turn": member.movement_rate(session.ruleset),
            "encumbrance": {
                "mode": session.ruleset.encumbrance.value,
                "carried_coins": tracked_weight_coins(member.inventory, session.ruleset.encumbrance),
                "max_load_coins": MAX_LOAD_COINS,
            },
            "languages": list(member.languages),
            "level_title": level_title(definition, member.level),
            "spell_slots": list(definition.row(member.level).spell_slots),
            "spell_picks": list(open_book_capacity(member, definition, catalog)),
            "rested": session.sleep_count > 0 and session.last_prepared_sleep.get(member_id, 0) < session.sleep_count,
        }
    return sheets


def _cell_info(session: GameSession) -> dict:
    """Player-safe context for the party's current cell."""
    location = session.dungeon_state.location
    lit, bright = session.party_light()
    info: dict = {
        "area_name": None,
        "features": [],
        "pile": None,
        "transition": None,
        "lit": lit,
        "at_entrance": False,
    }
    where = _dungeon_cell(location)
    if where is None:
        return info
    dungeon_id, level_number, position = where
    try:
        level = session.adventure.dungeon(dungeon_id).level(level_number)
    except ValueError:
        return info
    area = level.area_at(position)
    state = session.dungeon_state
    if area is not None:
        info["area_name"] = area.name or f"Area {area.id}"
        emptied = set(state.emptied_caches)
        for feature in area.features:
            if feature.kind != "treasure_cache":
                continue
            if feature.cell is not None and tuple(feature.cell) != tuple(position):
                continue
            ref = f"{dungeon_id}:{level_number}:{feature.id}"
            if ref in emptied:
                continue
            # Leak review: `trap_found` is true only through the party's own
            # successful inspect — the engine writes `found_traps` on their
            # check and nowhere else — so an untrapped cache and a trapped but
            # undiscovered one are indistinguishable on the wire. A disarmed or
            # sprung trap reads false again: no trap is *set* any more.
            # `inspected_by` is the party's own attempt history (who has
            # already gone over this cache), never the referee's answer.
            info["features"].append(
                {
                    "id": feature.id,
                    "description": feature.description or "a treasure cache",
                    "trap_found": ref in state.found_traps
                    and ref not in state.removed_traps
                    and ref not in state.sprung_traps,
                    "inspected_by": list(state.inspect_attempts.get(ref, [])),
                }
            )
    here = cell_ref(dungeon_id, level_number, position)
    for cache_id, cache in state.generated_caches.items():
        if cache.cell_ref == here:
            # Generated hoards are untrapped by engine rule, so `trap_found`
            # is constant false; inspect attempts key by the cache id itself.
            info["features"].append(
                {
                    "id": cache_id,
                    "description": "treasure, free for the taking",
                    "trap_found": False,
                    "inspected_by": list(state.inspect_attempts.get(cache_id, [])),
                }
            )
    pile = session.dungeon_state.piles.get(here)
    if pile is not None:
        info["pile"] = {
            "coins_gp_value": pile.coins.value_gp,
            # Magic items count too: the party watched them being left behind
            # (`ItemsLeftBehindEvent` is player-visible), so the tally leaks
            # nothing — and a pile holding only a left-behind wand is not empty.
            "count": len(pile.items) + len(pile.valuables) + len(pile.magic_items),
        }
    for transition in level.transitions:
        if tuple(transition.position) == tuple(position):
            info["transition"] = transition.kind
    if level.entrance is not None:
        info["at_entrance"] = tuple(level.entrance) == tuple(position)
    return info


def _current_battle_round(view: dict) -> dict:
    """Restate `encounter.battle_round` as the round the party is declaring now.

    osrlib's `EncounterView.battle_round` is `session.battle.round`: the count of
    battle rounds already **resolved**, which is one behind the round the party is
    about to declare — and one behind the `— Round N —` header the transcript
    prints when that round resolves (`server.narrate.Narrator._on_battle_round`,
    reading the engine's own `battle_round` event). The client reads this field as
    the current round for the declaration panel and the roundbar, so the
    projection hands it the current round.

    Without a surprise round the gap is invisible: nothing has resolved, so the
    engine's zero renders as round one either way. A surprise round *is* a
    resolved round — the engine runs the monsters' free round immediately and
    advances the clock — so from then on the panel and the transcript disagreed
    by one, both on screen at once.

    Args:
        view: The dumped player projection, modified in place.

    Returns:
        The same mapping, for chaining.
    """
    encounter = view.get("encounter")
    if encounter is not None and encounter.get("battle_round") is not None:
        encounter["battle_round"] += 1
    return view


def _state(game: Game) -> dict:
    """The full client payload: projection, cell context, and the spellbook."""
    session = game.session
    view = session.view(Visibility.PLAYER)
    catalog = load_spells()
    return {
        "view": _current_battle_round(view.model_dump(mode="json")),
        "cell": _cell_info(session),
        "sheets": _sheets(session),
        "spellbook": _spellbook(session),
        "spell_books": {
            member.id: preparable for member in session.party.members if (preparable := _preparable(member))
        },
        "learnable": _learnable(session),
        # The temple's posted price list — the engine's HEALING_SERVICES table,
        # which a town would post on the door (player-safe rulebook data).
        "temple_services": [
            {"id": service, "name": catalog.get(spell_id).name, "cost_gp": cost}
            for service, (spell_id, cost) in HEALING_SERVICES.items()
        ],
        "hooks": list(session.adventure.hooks),
        "dungeons": [
            {"id": dungeon.id, "name": dungeon.name}
            for dungeon in session.adventure.dungeons
            if any(level.entrance is not None for level in dungeon.levels)
        ],
        "narration": game.narration.state()
        if game.narration is not None
        else {"enabled": False, "pending": 0, "seq": 0},
    }


def _fill_cast_targets(session: GameSession, payload: dict) -> dict:
    """Battle-cast convenience: resolve a group id into that group's monster ids.

    The player view names groups, not monsters, so the client sends
    `target_group_id`; group-targeting spells need entity ids, filled here.
    Healing casts name party members directly and pass through untouched.
    """
    if payload.get("command_type") != "resolve_battle_round" or session.encounter is None:
        return payload
    groups = {group.id: group for group in session.encounter.groups}
    catalog = load_spells()
    declarations = []
    for declaration in payload.get("declarations", []):
        declaration = dict(declaration)
        if declaration.get("action") == "cast" and not declaration.get("targets"):
            group = groups.get(declaration.get("target_group_id") or "")
            spell_id = declaration.get("spell_id") or ""
            if group is not None:
                living = [
                    monster_id
                    for monster_id in group.monster_ids
                    if (combatant := session.combatant(monster_id)) is not None
                    and not has_condition(combatant, Condition.DEAD)
                ]
                try:
                    mode = catalog.get(spell_id).mode(declaration.get("spell_mode") or "")
                    targeting_mode = mode.targeting.mode.value if mode.targeting else "single"
                except ValueError:
                    targeting_mode = "single"
                if targeting_mode in ("hd_budget", "area"):
                    declaration["targets"] = living
                elif living:
                    declaration["targets"] = living[:1]
        declarations.append(declaration)
    payload = dict(payload)
    payload["declarations"] = declarations
    return payload


@app.get("/api/adventures")
def adventures() -> dict:
    """The adventure library: every forge document the server can host.

    Each entry carries the player-safe facts the library pane shows before any
    game exists — description, hooks, the town's name, and the count of
    enterable dungeons. Browsing the library must never need a session,
    so everything a player reads before committing rides here.

    `removable` and `source_kind` describe the entry's place on the server's own
    disk, not the adventure: whether the delete button appears at all, and which
    of three very different deletions it would perform. Neither carries a path.
    """
    entries = list_adventures()
    return {
        "adventures": [
            {
                "id": entry.id,
                "name": entry.name,
                "description": entry.description,
                "hooks": list(entry.hooks),
                "town_name": entry.town_name,
                "dungeon_count": entry.dungeon_count,
                "removable": entry.removable,
                "source_kind": entry.source_kind,
            }
            for entry in entries
        ],
        "default_id": entries[0].id if entries else None,
    }


# A generous ceiling for a forge document; mostly a guard against browsing to
# the wrong file entirely and buffering it whole into memory.
_MAX_ADVENTURE_BYTES = 20 * 1024 * 1024


@app.post("/api/adventures")
async def upload_adventure(request: Request) -> dict:
    """Add an uploaded adventure document to the library.

    The body is the stamped adventure JSON itself — the file's bytes, unaltered,
    so what gets vetted, deduplicated, and written to the drop directory is
    exactly what the player browsed to. Malformed content is a 422 through the
    validation handlers; a document the library already holds comes back as its
    existing entry with nothing written. The response is the refreshed library
    plus the entry's `adventure_id`, so the client can select it without a
    second fetch.

    Staying `async def` is load-bearing: `add_adventure` never awaits, so its
    check-then-write section runs whole on the event loop and concurrent uploads
    serialize instead of racing for a filename. Don't flip this to a threadpool
    `def` without giving the library a write lock.
    """
    raw = await request.body()
    if len(raw) > _MAX_ADVENTURE_BYTES:
        raise HTTPException(status_code=422, detail="adventure document too large (20 MB limit)")
    entry = add_adventure(raw)
    return {"adventure_id": entry.id, **adventures()}


@app.delete("/api/adventures/{adventure_id}")
def delete_adventure(adventure_id: str) -> dict:
    """Delete an adventure the drop directory holds.

    Only the drop directory is ever written to, in either direction: an entry
    the scan found through the bundled document or `OSR_WEB_ADVENTURES` is a
    file the operator pointed the server at deliberately, so it lists, plays,
    and stays — a 422 rather than a delete. What deletion removes is the drop
    directory's own item (`library.remove_adventure`): a `.json` file, or the
    directory that held it — commonly a symlink to a forge run, which unlinks
    without touching the run.

    The response is the refreshed library, same shape as the upload's, so the
    client repaints from one round trip.
    """
    entry = adventure_entry(adventure_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown adventure id {adventure_id!r}")
    if not entry.removable:
        raise HTTPException(
            status_code=422,
            detail=(f"“{entry.name}” is not in this server's adventures directory, so it can't be deleted from here."),
        )
    try:
        remove_adventure(entry)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"“{entry.name}” could not be deleted: {error}") from error
    return {"adventure_id": entry.id, "deleted": True, **adventures()}


# Save ids the server minted are eight hex digits, but files dropped into the
# saves directory by hand list too — the pattern only fences the id off from
# path tricks, not down to token_hex output.
_SAVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_SAVE_NAME = 60


def _save_path(save_id: str) -> Path:
    """The save's file path; an id that isn't a plain filename stem is a 404.

    Every route that touches a save file by id resolves it here, so no id can
    smuggle a path separator (or anything else the filesystem might interpret)
    into `_saves_dir()`.
    """
    if not _SAVE_ID_PATTERN.match(save_id):
        raise HTTPException(status_code=404, detail=f"unknown save id {save_id!r}")
    return _saves_dir() / f"{save_id}.json"


def _save_document(save_id: str) -> dict:
    path = _save_path(save_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"unknown save id {save_id!r}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _save_name_or_422(value) -> str | None:
    """Vet a caller-supplied save name; empty means "no name" and None passes through."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="save name must be a string")
    name = value.strip()
    if len(name) > _MAX_SAVE_NAME:
        raise HTTPException(
            status_code=422,
            detail=f"save name must be at most {_MAX_SAVE_NAME} characters",
        )
    return name or None


def _save_place(payload: dict) -> str:
    """Where the saved party stands, in words a player would use.

    Town is the town's name; a dungeon is its name and level. Only the location
    pointer and the adventure's own display names are read — never the keyed
    content around them.
    """
    location = (payload.get("dungeon_state") or {}).get("location") or {}
    adventure = payload.get("adventure") or {}
    if location.get("kind") == "dungeon":
        dungeon_name = next(
            (
                str(dungeon.get("name") or "")
                for dungeon in adventure.get("dungeons") or ()
                if isinstance(dungeon, dict) and dungeon.get("id") == location.get("dungeon_id")
            ),
            "",
        )
        level = location.get("level_number")
        return f"{dungeon_name or 'The dungeon'}, level {level}"
    town = adventure.get("town") or {}
    return str(town.get("name") or "") or "town"


def _derived_save_name(payload: dict) -> str:
    """The fallback save name: adventure, place marker, and turn."""
    adventure_name = str((payload.get("adventure") or {}).get("name") or "An adventure")
    turn = int(payload.get("clock_rounds") or 0) // 60
    location = (payload.get("dungeon_state") or {}).get("location") or {}
    if location.get("kind") == "dungeon":
        return f"{adventure_name} — level {location.get('level_number')}, turn {turn}"
    return f"{adventure_name} — turn {turn}"


def _save_summary(save_id: str, document: dict, saved_at: int) -> dict:
    """One player-safe save summary: the label a player reads, never the document.

    The keys here are the whole wire contract for a save: name, adventure,
    place, turn, roster, and timestamps are all facts the player saw on screen
    when they saved. `tests/test_saves.py` pins the exact key set.
    """
    payload = document["payload"]
    members = payload["party"]["members"]
    custom_name = document.get("name")
    return {
        "save_id": save_id,
        "name": str(custom_name) if custom_name else _derived_save_name(payload),
        "adventure_name": payload["adventure"].get("name", ""),
        "saved_at": saved_at,
        "turn": int(payload.get("clock_rounds") or 0) // 60,
        "place": _save_place(payload),
        "party": [
            {
                "name": member["name"],
                "class_id": member["class_id"],
                "level": member["level"],
                "dead": any(active.get("condition") == "dead" for active in member.get("conditions", [])),
            }
            for member in members
        ],
    }


@app.get("/api/saves")
def saves() -> dict:
    """Player-safe save summaries: label, roster, place, and turn — never the document."""
    summaries = []
    directory = _saves_dir()
    if directory.is_dir():
        for path in sorted(
            directory.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                with path.open(encoding="utf-8") as handle:
                    document = json.load(handle)
                summaries.append(_save_summary(path.stem, document, int(path.stat().st_mtime)))
            except OSError, ValueError, KeyError, TypeError:
                continue  # an unreadable save simply doesn't list
    return {"saves": summaries}


@app.patch("/api/saves/{save_id}")
def rename_save(save_id: str, body: dict) -> dict:
    """Give a save a name of the player's own; the document is otherwise untouched.

    The name lives as an extra top-level key on the save file — the document
    envelope ignores unknown keys by contract (`osrlib.versioning`), so an old
    engine restores a renamed save unchanged. An empty name is a 422: clearing
    back to the derived label isn't an operation the UI offers.
    """
    name = _save_name_or_422(body.get("name"))
    if name is None:
        raise HTTPException(status_code=422, detail="save name must not be empty")
    path = _save_path(save_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"unknown save id {save_id!r}")
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    document["name"] = name
    saved_at = path.stat().st_mtime
    with path.open("w", encoding="utf-8") as handle:
        json.dump(document, handle)
    # A rename is not a save: keep the file's age so the list order holds.
    os.utime(path, (saved_at, saved_at))
    return {"save_id": save_id, "name": name}


@app.delete("/api/saves/{save_id}")
def delete_save(save_id: str) -> dict:
    """Delete one save file; unknown ids are a 404."""
    path = _save_path(save_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"unknown save id {save_id!r}")
    path.unlink()
    return {"save_id": save_id, "deleted": True}


@app.delete("/api/saves")
def delete_all_saves() -> dict:
    """Delete every save the list shows; the response is the list afterwards.

    The scope is exactly what `GET /api/saves` lists — the `*.json` files in
    `_saves_dir()` — so what the player was looking at is what goes, and nothing
    else in that directory is touched. A file that refuses to delete is skipped
    rather than failing the sweep: the refreshed summaries ride along, so a
    survivor reappears in the list instead of vanishing from a client that
    assumed success.
    """
    deleted = 0
    directory = _saves_dir()
    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                path.unlink()
                deleted += 1
            except OSError:
                continue
    return {"deleted": deleted, **saves()}


def _builder_or_404(builder_id: str) -> PartyBuilder:
    builder = _touch(_builders, builder_id)
    if builder is None:
        raise HTTPException(status_code=404, detail=f"unknown party builder id {builder_id!r}")
    return builder


@app.get("/api/creation/catalog")
def get_creation_catalog() -> dict:
    """Rulebook data for the creation wizard: classes, spells, languages, wares."""
    return creation_catalog()


def _builder_seed(body: dict) -> int | None:
    """Parse the optional builder seed; anything but a seedable integer is a 422.

    The seed is the caller's own knob on a builder the caller just asked for: it
    makes a wizard run reproducible (same seed, same steps, same draws) and has
    nothing to do with a game session's master seed, which is never accepted
    here, never derived from this, and never returned.

    Returns:
        The seed, or `None` when absent or explicitly null (fresh randomness).

    Raises:
        HTTPException: 422 if `seed` is not an integer in `[0, SEED_BOUND)`.
    """
    seed = body.get("seed")
    if seed is None:
        return None
    # bool is an int subclass, and out-of-range seeds would raise inside the
    # engine — both are malformed content, not an in-fiction rejection.
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < SEED_BOUND:
        raise HTTPException(
            status_code=422,
            detail=f"seed must be an integer in [0, 2**128), got {seed!r}",
        )
    return seed


@app.post("/api/party-builders")
def create_party_builder(body: dict | None = None) -> dict:
    """Open a roll-your-own party builder; creation draws stay server-side.

    An optional `house_rules` object (`ability_method`, `max_hp`) sets the table
    rules up front; they can also be changed later via the `house_rules` step,
    until the first character is rolled. An optional `seed` makes the builder's
    draws reproducible — the same seed driven through the same steps rolls the
    same abilities, hit points, and gold. It is builder-scoped and write-only:
    omit it and the builder rolls fresh randomness.

    `premade: true` fills the builder with the pregenerated six on creation —
    the staging screen's party, rolled and held with **no game session** behind
    it. The `premade` and `rename` steps then reroll the set and
    rename members until `POST /api/games {party_builder_id}` commits.
    """
    body = body or {}
    builder = PartyBuilder(seed=_builder_seed(body))
    rejections = []
    house_rules = body.get("house_rules")
    if isinstance(house_rules, dict):
        with builder.lock:
            rejections = builder.set_house_rules(
                ability_method=house_rules.get("ability_method"),
                max_hp=house_rules.get("max_hp"),
            )
    if body.get("premade"):
        with builder.lock:
            rejections = rejections + builder.fill_premade()
    builder_id = secrets.token_hex(8)
    _remember(_builders, builder_id, builder, _MAX_BUILDERS)
    with builder.lock:
        state = builder.state()
    return {
        "builder_id": builder_id,
        "accepted": not rejections,
        "rejections": [
            {"code": rejection.code, "message": creation_rejection_text(rejection)} for rejection in rejections
        ],
        **state,
    }


@app.get("/api/party-builders/{builder_id}")
def get_party_builder(builder_id: str) -> dict:
    """The builder's current roster and draft, for a client picking back up."""
    builder = _builder_or_404(builder_id)
    with builder.lock:
        state = builder.state()
    return {"builder_id": builder_id, **state}


_BUILDER_STEPS = {
    "house_rules": lambda builder, body: builder.set_house_rules(
        ability_method=body.get("ability_method"), max_hp=body.get("max_hp")
    ),
    "roll_abilities": lambda builder, body: builder.roll_abilities(),
    "discard": lambda builder, body: builder.discard_draft(),
    "choose_class": lambda builder, body: builder.choose_class(body.get("class_id")),
    "adjust": lambda builder, body: builder.adjust_scores(body.get("lowered") or {}, body.get("raised") or {}),
    "choose_spells": lambda builder, body: builder.choose_spells(body.get("spell_ids")),
    "choose_languages": lambda builder, body: builder.choose_languages(body.get("language_ids")),
    "roll_hit_points": lambda builder, body: builder.roll_hp(),
    "roll_gold": lambda builder, body: builder.roll_gold(),
    "buy": lambda builder, body: builder.buy(body.get("item_id"), body.get("lots", 1)),
    "buy_kit": lambda builder, body: builder.buy_kit(body.get("kit_id")),
    "return": lambda builder, body: builder.return_purchase(body.get("index")),
    "finalize": lambda builder, body: builder.finalize(body.get("name"), body.get("alignment")),
    "remove_member": lambda builder, body: builder.remove_member(body.get("index")),
    "premade": lambda builder, body: builder.fill_premade(
        ability_method=body.get("ability_method"), max_hp=body.get("max_hp")
    ),
    "rename": lambda builder, body: builder.rename_member(body.get("index"), body.get("name")),
}


@app.post("/api/party-builders/{builder_id}/step")
def party_builder_step(builder_id: str, body: dict) -> dict:
    """One creation step; a broken rule is a 200 with `accepted: false`."""
    builder = _builder_or_404(builder_id)
    action = body.get("action")
    step = _BUILDER_STEPS.get(action) if isinstance(action, str) else None
    if step is None:
        raise HTTPException(status_code=422, detail=f"unknown builder action {action!r}")
    with builder.lock:
        rejections = step(builder, body)
        state = builder.state()
    return {
        "accepted": not rejections,
        "rejections": [
            {"code": rejection.code, "message": creation_rejection_text(rejection)} for rejection in rejections
        ],
        "builder_id": builder_id,
        **state,
    }


def _house_rules(body: dict) -> HouseRules | None:
    """Parse an optional `house_rules` object; an unknown ability method is a 422."""
    rules = body.get("house_rules")
    if not isinstance(rules, dict):
        return None
    method = rules.get("ability_method")
    if method is not None and str(method) not in ABILITY_METHODS:
        raise HTTPException(status_code=422, detail=f"unknown ability-score method {method!r}")
    return HouseRules(
        ability_method=str(method) if method is not None else "3d6",
        max_hp=bool(rules.get("max_hp")),
    )


@app.post("/api/games")
def create_game(body: dict | None = None) -> dict:
    """Start a fresh game (optionally with a saved or hand-rolled party), or restore a save.

    An optional `house_rules` object (`ability_method`, `max_hp`) bends the
    pregenerated party's creation draws; it is ignored when the party comes
    from a save or a builder (those members keep their sheets).
    """
    body = body or {}
    save_id = body.get("save_id")
    if save_id is not None:
        session = restore_session(_save_document(save_id))
    else:
        requested_seed = body.get("seed")
        seed = requested_seed if isinstance(requested_seed, int) else secrets.randbits(63)
        adventure_id = body.get("adventure_id")
        entry = adventure_entry(adventure_id if isinstance(adventure_id, str) else None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown adventure id {adventure_id!r}")
        party = None
        party_builder_id = body.get("party_builder_id")
        party_save_id = body.get("party_save_id")
        if isinstance(party_builder_id, str) and party_builder_id:
            builder = _builder_or_404(party_builder_id)
            with builder.lock:
                party, rejections = builder.party()
            if rejections:
                raise HTTPException(status_code=422, detail=creation_rejection_text(rejections[0]))
        elif isinstance(party_save_id, str) and party_save_id:
            party = party_from_save(_save_document(party_save_id))
        session = new_session(
            seed,
            adventure_path=entry.path,
            party=party,
            house_rules=_house_rules(body),
        )
        if isinstance(party_builder_id, str) and party_builder_id:
            with _store_lock:
                _builders.pop(party_builder_id, None)
    game = Game(session)
    game_id = secrets.token_hex(8)
    _remember(_games, game_id, game, _MAX_GAMES)
    with game.lock:
        log = game.narrator.render(session.event_log)
        if game.narration is not None:
            game.narration.observe(session, session.event_log, log)
        state = _state(game)
    return {
        "game_id": game_id,
        "schema_version": SCHEMA_VERSION,
        "engine_version": engine_version(),
        "log": log,
        **state,
    }


@app.delete("/api/games/{game_id}")
def delete_game(game_id: str) -> dict:
    """Abandon a session: free its store entry; unknown ids are a 404.

    The one honest destructive verb: the client calls it from
    "Abandon adventure" and when beginning a new adventure replaces a live
    session. Nothing on disk is touched — saves survive their session.
    """
    with _store_lock:
        game = _games.pop(game_id, None)
    if game is None:
        raise HTTPException(status_code=404, detail=f"unknown game id {game_id!r}")
    return {"game_id": game_id, "deleted": True}


@app.get("/api/games/{game_id}")
def get_game(game_id: str) -> dict:
    """Rebuild the client payload, transcript included, for a page reload."""
    game = _game_or_404(game_id)
    with game.lock:
        game.narrator.seen_areas.clear()
        if game.narration is not None:
            log = interleave(
                game.narrator.render,
                game.session.event_log,
                game.narration.passages(),
            )
        else:
            log = game.narrator.render(game.session.event_log)
        state = _state(game)
    return {"game_id": game_id, "log": log, **state}


_MAX_MEMBER_NAME = 40


@app.post("/api/games/{game_id}/party/name")
def rename_member(game_id: str, body: dict) -> dict:
    """Rename one party member; the full transcript re-renders under the new name.

    The narrator resolves entity ids to names at render time, so rebuilding the
    log (exactly as `GET /api/games/{id}` does) retells every earlier event with
    the new name. A bad name is a 200 with `accepted: false`; an unknown member
    id is a 404.
    """
    game = _game_or_404(game_id)
    member_id = str(body.get("member_id") or "")
    name = str(body.get("name") or "").strip()
    if not name or len(name) > _MAX_MEMBER_NAME:
        rejection = Rejection(code="creation.name.invalid", params={"max": _MAX_MEMBER_NAME})
        return {
            "accepted": False,
            "rejections": [{"code": rejection.code, "message": creation_rejection_text(rejection)}],
        }
    with game.lock:
        member = next((m for m in game.session.party.members if m.id == member_id), None)
        if member is None:
            raise HTTPException(status_code=404, detail=f"unknown member id {member_id!r}")
        member.name = name
        game.narrator.seen_areas.clear()
        if game.narration is not None:
            log = interleave(
                game.narrator.render,
                game.session.event_log,
                game.narration.passages(),
            )
        else:
            log = game.narrator.render(game.session.event_log)
        state = _state(game)
    return {
        "accepted": True,
        "rejections": [],
        "game_id": game_id,
        "log": log,
        **state,
    }


@app.post("/api/games/{game_id}/command")
def execute_command(game_id: str, body: dict) -> dict:
    """Parse and execute one command under the game's lock."""
    game = _game_or_404(game_id)
    with game.lock:
        body = _fill_cast_targets(game.session, body)
        command = parse_command(body)
        if command is None:
            raise HTTPException(
                status_code=422,
                detail=f"unknown command type {body.get('command_type')!r}",
            )
        mark = len(game.session.event_log)
        result = game.session.execute(command)
        # As of osrlib 1.5.0, result.events folds in everything listener-issued —
        # nested commands logged, each event once, in log order — so this log-delta
        # read and result.events are equivalent. The delta read stays: it is the
        # same window observe() gets and needs no knowledge of the result shape.
        log = game.narrator.render(game.session.event_log[mark:])
        _log_referee(game.session.event_log[mark:])
        if game.narration is not None:
            game.narration.observe(game.session, game.session.event_log[mark:], log)
        rejections = []
        for rejection in result.rejections:
            message = rejection_text(rejection)
            who = rejection.params.get("character") or rejection.params.get("caster")
            if isinstance(who, str) and who:
                message = f"{game.narrator._name(who)}: {message}"
            rejections.append({"code": rejection.code, "message": message})
        state = _state(game)
    return {
        "accepted": result.accepted,
        "rejections": rejections,
        "log": log,
        **state,
    }


@app.get("/api/games/{game_id}/narration")
def narration_tail(game_id: str, after: int = 0) -> dict:
    """Stored passages past the client's high-water mark, plus the pending count."""
    game = _game_or_404(game_id)
    if game.narration is None:
        return {"entries": [], "partial": None, "pending": 0}
    return game.narration.tail(after)


@app.post("/api/games/{game_id}/save")
def save(game_id: str, body: dict | None = None) -> dict:
    """Snapshot to the server-side saves directory; only the id and label cross the wire.

    An optional `name` (stripped, at most `_MAX_SAVE_NAME` characters) labels
    the save; absent, summaries derive one from the adventure, place, and turn.
    The name rides as an extra top-level key the document envelope ignores by
    contract, so old saves and old engines are both untouched.
    """
    name = _save_name_or_422((body or {}).get("name"))
    game = _game_or_404(game_id)
    with game.lock:
        document = save_game(game.session)
    if name:
        document["name"] = name
    save_id = secrets.token_hex(4)
    directory = _saves_dir()
    directory.mkdir(exist_ok=True)
    with (directory / f"{save_id}.json").open("w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return {
        "save_id": save_id,
        "name": name or _derived_save_name(document["payload"]),
    }


app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
