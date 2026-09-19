"""End-to-end pins for osrlib's authored layer over the bundled document.

`tests/test_narrate.py` covers the renderer against synthetic events. These drive
the real engine through the real routes instead, so what is pinned is what the
engine actually emits and what the player actually reads — the half a fake event
stream cannot catch.

*The Cold Vein* carries the whole authored surface: two bundled items
(`verrow-token` and `captains-day-book`), a non-consuming gated door keyed to the
first (level 1, edge `11,3:north`), a non-repeatable `area_entered` trigger on
the entrance area (`adit-draught`), and the concluding quest `the-day-book`. The
toll form of a gate is not shipped, so the one test that needs it plays an
in-memory copy of the document with `consumes` flipped.

Endpoints are called directly (no HTTP layer) in the repo's style, with the
library and saves directories redirected per `tests/test_paths.py`. Referee
placement goes through `PlaceParty` on the session, per
`tests/test_trap_outcome.py` — walking there would be a test about walking, and
the relic is granted rather than taken for the same reason: `GrantItem` emits the
same `ItemAcquiredEvent` the cache's take emits, so the objective's clause cannot
tell the two apart, and walking to the cold face would be a test about the
climax fight. Proving the take is the milestone playthrough's job.
"""

import json
import logging
from pathlib import Path

import pytest
from osrlib.crawl.commands import PlaceParty
from osrlib.crawl.dungeon import Direction, PartyLocation
from osrlib.crawl.events import NoteRecordedEvent, TriggerFiredEvent
from osrlib.crawl.interpreter import Interpreter
from osrlib.errors import ContentValidationError

from server import library
from server.app import (
    _games,
    _log_referee,
    _save_document,
    create_game,
    execute_command,
    get_game,
    save,
)
from server.content import load_adventure, party_from_save

BUNDLED = Path(__file__).resolve().parent.parent / "content" / "adventure.json"
DUNGEON = "cold-vein"

REFUSAL = "The wicket is banded in lead and stamped with Verrow's knot. Nothing the party carries answers it."
SUCCESS = "Saint Verrow's token fits the stamp in the lead band, and the wicket swings open."
JOURNAL = "The adit breathes out cold — a steady draught from far below, like the mine is still working."
TOKEN = "Saint Verrow's token"

QUEST = "The Captain's Day-Book"
RELIC = "captains-day-book"
DAY_BOOK = "the captain's day-book"
OFFER = (
    "Bring me the mine captain's day-book and I'll pay you twice over for it. "
    "Cinderhope has a right to know what stopped the work."
)
SPEAKER = "Headman Orrin Slake"
RECOVERED = "The day-book comes up off the board, still open at the last line the captain wrote."
HOMECOMING = "The drove road brings the party down off the moor with the day-book still in the pack."
COMPLETION = (
    "Orrin Slake reads the last line twice and says nothing for a long moment. "
    "Then he counts out the price he named, twice over as promised, "
    "and Cinderhope has its answer at last."
)

# One distinctive phrase from each of the document's five `guidance` blocks —
# level 1, level 2, the quest, the gate, the trigger. Each appears nowhere else
# in the document, so finding one in a payload means the steering channel leaked.
GUIDANCE_TOKENS = (
    "workaday dread",
    "older than the lamps",
    "flinty",
    "old blessing",
    "taking notice",
)

# The corridor cell outside the lead wicket, the saint's own offering ledge, and
# the shelf inside the cupboard.
WICKET_APPROACH = (11, 2)
LEDGE_CELL = (8, 2)
PLATE_CELL = (10, 3)


@pytest.fixture(autouse=True)
def _redirected_dirs(monkeypatch, tmp_path):
    """Keep every test off the repo's real saves and adventure library."""
    empty_drop = tmp_path / "drop"
    empty_drop.mkdir()
    monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(empty_drop))
    monkeypatch.delenv("OSR_WEB_ADVENTURE", raising=False)
    monkeypatch.delenv("OSR_WEB_ADVENTURES", raising=False)
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


@pytest.fixture
def game_id():
    payload = create_game({"seed": 42})
    yield payload["game_id"]
    _games.pop(payload["game_id"], None)


def texts(payload) -> list[str]:
    return [entry["text"] for entry in payload["log"]]


def command(game_id, command_type, **body):
    return execute_command(game_id, {"command_type": command_type, **body})


def enter(game_id):
    return command(game_id, "enter_dungeon", dungeon_id=DUNGEON)


def place(game_id, position, *, level_number=1, facing=Direction.NORTH):
    """Put the party on a cell by referee fiat, the way a referee would."""
    _games[game_id].session.execute(
        PlaceParty(
            location=PartyLocation(
                kind="dungeon",
                dungeon_id=DUNGEON,
                level_number=level_number,
                position=position,
                facing=facing,
            )
        )
    )


def carries_token(game_id) -> bool:
    return any(
        member.inventory.carried_item("verrow-token") is not None for member in _games[game_id].session.party.members
    )


def journal_texts(payload) -> list[str]:
    return [entry["text"] for entry in payload["view"]["journal"]]


def objectives(payload) -> list[dict]:
    return payload["view"]["quests"][0]["objectives"]


def grant_the_day_book(game_id):
    """Put the relic in the first member's pack by referee fiat.

    `GrantItem` emits the same `ItemAcquiredEvent` the cold face's cache emits on
    a take, so the objective's clause cannot tell the two routes apart — and this
    one does not have to walk past two zombies to get there.
    """
    holder = _games[game_id].session.party.members[0].id
    return command(game_id, "grant_item", character_id=holder, item_id=RELIC)


def cold_face():
    """Area 18 of level 2, the climax — the relic's home."""
    level = load_adventure().dungeon(DUNGEON).level(2)
    return next(area for area in level.areas if area.id == "18")


@pytest.fixture
def repeatable_game(monkeypatch, tmp_path):
    """A game over a copy of the bundled document whose trigger is repeatable.

    The shipped trigger is once-only, so its fired-mark hides a doubled
    registration: the second interpreter matches, sees the mark, and stays quiet.
    Only a repeatable trigger can count registrations by their effects.
    """
    document = json.loads(BUNDLED.read_text(encoding="utf-8"))
    document["payload"]["triggers"][0]["repeatable"] = True
    path = tmp_path / "repeatable-adventure.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("OSR_WEB_ADVENTURE", str(path))
    library._meta_cache.clear()
    payload = create_game({"seed": 42})
    yield payload["game_id"]
    _games.pop(payload["game_id"], None)


class TestTheTriggerFires:
    """One registration, one firing, and the fired-mark that makes it once-only."""

    def test_entering_the_mine_writes_exactly_one_journal_line(self, game_id):
        payload = enter(game_id)
        assert texts(payload).count(JOURNAL) == 1
        journal = payload["view"]["journal"]
        # The quest's offer is written on the way in too, and ahead of the
        # draught: the `dungeon_entered` activation resolves before the entrance
        # area's `area_entered` trigger.
        assert [entry["text"] for entry in journal] == [OFFER, JOURNAL]
        assert _games[game_id].session.fired_triggers == ["adit-draught"]

    def test_the_session_carries_exactly_one_interpreter(self, game_id):
        # `register_listener` never dedupes, and `Game.__init__` is the only
        # place that calls it. Counting the listeners is the direct statement;
        # the repeatable-trigger test below is the one that can *feel* a second.
        interpreters = [listener for listener in _games[game_id].session.listeners if isinstance(listener, Interpreter)]
        assert len(interpreters) == 1

    def test_a_repeatable_trigger_fires_once_per_entry_not_twice(self, repeatable_game):
        # A second registration would append two journal entries per crossing.
        # The shipped trigger cannot show this: its fired-mark silences the
        # duplicate, so the whole suite stays green with the call doubled.
        payload = enter(repeatable_game)
        assert texts(payload).count(JOURNAL) == 1
        # Two on the way in: the quest's offer, then the draught.
        assert len(payload["view"]["journal"]) == 2
        for expected in (3, 4):
            place(repeatable_game, (2, 13), facing=Direction.SOUTH)
            payload = command(repeatable_game, "move_party", direction="south")
            assert texts(payload).count(JOURNAL) == 1
            assert len(payload["view"]["journal"]) == expected

    def test_the_journal_beat_reads_as_the_referees_prose(self, game_id):
        payload = enter(game_id)
        entry = next(item for item in payload["log"] if item["text"] == JOURNAL)
        assert entry["kind"] == "prose"

    def test_re_entering_the_area_adds_nothing(self, game_id):
        enter(game_id)
        place(game_id, (2, 13), facing=Direction.SOUTH)
        payload = command(game_id, "move_party", direction="south")
        assert payload["accepted"]
        assert JOURNAL not in texts(payload)
        assert len(payload["view"]["journal"]) == 2

    def test_the_fired_beat_reaches_the_server_log_and_nowhere_else(self, game_id, caplog):
        # The referee's screen is the operator's terminal: the `fired` beat rides
        # a referee-visibility event, so this is its only consumer.
        with caplog.at_level(logging.INFO, logger="osr_web.referee"):
            payload = enter(game_id)
        messages = [record.getMessage() for record in caplog.records]
        assert any(message.startswith("trigger fired: adit-draught") for message in messages)
        assert any("the vein knows someone is in" in message for message in messages)
        assert "adit-draught" not in json.dumps(payload)


class TestTheRefereeChannel:
    """Both branches of the referee log, and the forgery they have to survive.

    Trigger ids, fired beats, and note text all come out of an adventure
    document, and a document can arrive over the upload route. A newline in any
    of them would write whole lines of its own on the operator's terminal — the
    one screen this channel is trusted on.
    """

    def test_a_firing_logs_at_info_with_its_beat(self, caplog):
        with caplog.at_level(logging.INFO, logger="osr_web.referee"):
            _log_referee([TriggerFiredEvent(trigger_id="adit-draught", narrative="The draught turns.")])
        record = caplog.records[-1]
        assert record.levelno == logging.INFO
        assert record.getMessage() == "trigger fired: adit-draught — The draught turns."

    def test_a_firing_with_no_beat_logs_the_id_alone(self, caplog):
        with caplog.at_level(logging.INFO, logger="osr_web.referee"):
            _log_referee([TriggerFiredEvent(trigger_id="adit-draught")])
        assert caplog.records[-1].getMessage() == "trigger fired: adit-draught"

    def test_a_note_logs_at_warning(self, caplog):
        # Drops and truncations are exceptional; they earn the louder level.
        with caplog.at_level(logging.INFO, logger="osr_web.referee"):
            _log_referee([NoteRecordedEvent(text="quest the-vein: reward 0 dropped")])
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert record.getMessage() == ("referee note: quest the-vein: reward 0 dropped")

    def test_a_newline_in_authored_text_cannot_forge_a_line(self, caplog):
        forged = "harmless\nWARNING: the server is on fire"
        with caplog.at_level(logging.INFO, logger="osr_web.referee"):
            _log_referee(
                [
                    TriggerFiredEvent(trigger_id="a\nb", narrative=forged),
                    NoteRecordedEvent(text=forged),
                ]
            )
        for record in caplog.records[-2:]:
            message = record.getMessage()
            assert "\n" not in message
            assert message.count("WARNING") <= 1
        assert caplog.records[-2].getMessage() == ("trigger fired: a b — harmless WARNING: the server is on fire")
        assert caplog.records[-1].getMessage() == ("referee note: harmless WARNING: the server is on fire")


class TestTheGatedDoor:
    """The wicket refuses in the author's words and opens on the author's key."""

    def test_the_refusal_is_the_authored_sentence_and_nothing_else(self, game_id):
        enter(game_id)
        place(game_id, WICKET_APPROACH, facing=Direction.SOUTH)
        payload = command(game_id, "open_door", direction="south")
        assert payload["accepted"] is False
        rejection = payload["rejections"][0]
        assert rejection["code"] == "exploration.door.gate_refused"
        # Verbatim, and unprefixed: the params carry no `character`/`caster`, so
        # app.py's speaker prefix never fires on a gate refusal.
        assert rejection["message"] == REFUSAL
        assert "gate_refused" not in rejection["message"]
        assert ":" not in rejection["message"]

    def test_a_refused_gate_costs_nothing(self, game_id):
        enter(game_id)
        place(game_id, WICKET_APPROACH, facing=Direction.SOUTH)
        before = _games[game_id].session.clock.rounds
        payload = command(game_id, "open_door", direction="south")
        assert payload["log"] == []
        assert _games[game_id].session.clock.rounds == before

    def test_the_key_reads_by_name_when_it_is_found(self, game_id):
        enter(game_id)
        place(game_id, LEDGE_CELL)
        payload = command(game_id, "take_treasure", feature_id="offering-ledge")
        assert payload["accepted"]
        assert any(TOKEN in text for text in texts(payload))
        assert "verrow-token" not in json.dumps(payload["log"])
        assert carries_token(game_id)

    def test_the_key_opens_the_wicket_and_the_beat_follows_the_door(self, game_id):
        enter(game_id)
        place(game_id, LEDGE_CELL)
        command(game_id, "take_treasure", feature_id="offering-ledge")
        place(game_id, WICKET_APPROACH, facing=Direction.SOUTH)
        payload = command(game_id, "open_door", direction="south")
        assert payload["accepted"]
        assert payload["log"] == [
            {"kind": "mech", "text": "The door to the south swings open."},
            {"kind": "prose", "text": SUCCESS},
        ]

    def test_the_gate_is_no_toll_and_the_cupboard_is_behind_it(self, game_id):
        enter(game_id)
        place(game_id, LEDGE_CELL)
        command(game_id, "take_treasure", feature_id="offering-ledge")
        place(game_id, WICKET_APPROACH, facing=Direction.SOUTH)
        command(game_id, "open_door", direction="south")
        payload = command(game_id, "move_party", direction="south")
        assert payload["accepted"]
        assert payload["view"]["location"]["position"] == [11, 3]
        assert {"kind": "place", "text": "The saint's cupboard"} in payload["log"]
        # Non-consuming: the token stays a keepsake, so the door reopens later.
        assert carries_token(game_id)

    def test_the_cupboard_gives_up_its_plate(self, game_id):
        enter(game_id)
        place(game_id, PLATE_CELL)
        payload = command(game_id, "take_treasure", feature_id="verrow-plate")
        assert payload["accepted"]
        assert any("pilgrim's dish" in text for text in texts(payload))


@pytest.fixture
def toll_game(monkeypatch, tmp_path):
    """A game over a copy of the bundled document whose gate charges a toll.

    The shipped document authors no toll — the token is a keepsake — but the
    consuming form is a first-class gate shape and the transcript has a line for
    it, so it is exercised against a copy rather than left unplayed.
    """
    document = json.loads(BUNDLED.read_text(encoding="utf-8"))
    level = document["payload"]["dungeons"][0]["levels"][0]
    gate = level["edges"]["11,3:north"]["door"]["requires"]
    gate["condition"]["consumes"] = True
    path = tmp_path / "toll-adventure.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("OSR_WEB_ADVENTURE", str(path))
    library._meta_cache.clear()
    payload = create_game({"seed": 42})
    yield payload["game_id"]
    _games.pop(payload["game_id"], None)


class TestTheTollForm:
    """`consumes: true` takes the key at the threshold, per success."""

    def test_opening_takes_the_key_and_says_so(self, toll_game):
        enter(toll_game)
        place(toll_game, LEDGE_CELL)
        command(toll_game, "take_treasure", feature_id="offering-ledge")
        place(toll_game, WICKET_APPROACH, facing=Direction.SOUTH)
        payload = command(toll_game, "open_door", direction="south")
        assert payload["accepted"]
        assert any(f"gives up {TOKEN}." in text for text in texts(payload))
        assert SUCCESS in texts(payload)
        assert not carries_token(toll_game)

    def test_the_door_refuses_again_once_the_toll_is_spent(self, toll_game):
        enter(toll_game)
        place(toll_game, LEDGE_CELL)
        command(toll_game, "take_treasure", feature_id="offering-ledge")
        place(toll_game, WICKET_APPROACH, facing=Direction.SOUTH)
        command(toll_game, "open_door", direction="south")
        command(toll_game, "close_door", direction="south")
        payload = command(toll_game, "open_door", direction="south")
        assert payload["accepted"] is False
        assert payload["rejections"][0]["message"] == REFUSAL


class TestSaveAndRestore:
    """`fired_triggers` and `journal` are engine-owned state and round-trip whole."""

    def test_the_journal_and_the_fired_mark_survive_a_restore(self, game_id):
        enter(game_id)
        save_id = save(game_id)["save_id"]
        restored = create_game({"save_id": save_id})
        try:
            assert [entry["text"] for entry in restored["view"]["journal"]] == [
                OFFER,
                JOURNAL,
            ]
            assert _games[restored["game_id"]].session.fired_triggers == ["adit-draught"]
            # The restore path registers the interpreter again — listeners are
            # code, and a save carries data — but the fired mark keeps it quiet.
            place(restored["game_id"], (2, 13), facing=Direction.SOUTH)
            payload = command(restored["game_id"], "move_party", direction="south")
            assert JOURNAL not in texts(payload)
            assert len(payload["view"]["journal"]) == 2
        finally:
            _games.pop(restored["game_id"], None)


class TestAreaProseFollowsTheEvent:
    """A whole-log re-render resolves each area against its own event.

    The old `_area` asked the session where the party stands, so after any
    relocation every historical area event resolved against the destination —
    on a level that has no area `1` at all, the adit mouth simply vanished from
    the transcript.
    """

    def test_the_opening_room_survives_a_relocation_to_another_level(self, game_id):
        enter(game_id)
        place(game_id, (2, 12), level_number=2)
        payload = get_game(game_id)
        assert {"kind": "place", "text": "The adit mouth"} in payload["log"]


class TestValidationAtLoad:
    """An authored typo fails at load, not silently in play."""

    def test_the_bundled_document_passes(self):
        assert load_adventure().name == "The Cold Vein"

    def test_a_dangling_item_reference_raises(self, tmp_path):
        # Model-valid — a cache's `item_ids` is a list of strings — and dangling.
        document = json.loads(BUNDLED.read_text(encoding="utf-8"))
        level = document["payload"]["dungeons"][0]["levels"][0]
        area = next(entry for entry in level["areas"] if entry["id"] == "9")
        area["features"][0]["item_ids"].append("no-such-item")
        path = tmp_path / "doctored.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ContentValidationError) as excinfo:
            load_adventure(path)
        assert "no-such-item" in str(excinfo.value)


class TestTheScriptedRouteHolds:
    """AGENTS.md's route to the first fight, with the interpreter registered."""

    def test_three_moves_north_still_land_the_kobold_fight(self, game_id):
        enter(game_id)
        for _ in range(3):
            payload = command(game_id, "move_party", direction="north")
            assert payload["accepted"]
        assert payload["view"]["location"]["position"] == [2, 12]
        assert payload["view"]["mode"] == "battle"


class TestThePlayerProjection:
    """The journal and the quest log cross; the wiring behind them never does."""

    def test_the_payload_carries_the_journal_and_the_quest_log(self, game_id):
        payload = enter(game_id)
        assert [quest["id"] for quest in payload["view"]["quests"]] == ["the-day-book"]
        assert set(payload["view"]["quests"][0]) == {
            "id",
            "name",
            "narrative",
            "speaker",
            "objectives",
        }
        assert all(set(entry) == {"text", "rounds"} for entry in payload["view"]["journal"])

    def test_no_referee_state_rides_along(self, game_id):
        # Swept over the whole serialized payload of a session that has fired
        # the trigger: the referee event *types* as well as the state they carry,
        # because a projection that started shipping them would ship them by
        # name. The gate's key id and the trigger's `fired` beat are secrets too.
        payload = enter(game_id)
        assert "fired_triggers" not in payload["view"]
        blob = json.dumps(payload)
        for forbidden in (
            "trigger_fired",
            "note_recorded",
            "session.trigger.fired",
            "session.note.recorded",
            "adit-draught",
            "verrow-token",
            # The relic's id is the quest's wiring until somebody picks it up:
            # the objective crosses by its authored name, never by what it
            # matches on. (`the-day-book` is `QuestView.id` and belongs here.)
            "captains-day-book",
            "the vein knows someone is in",
            # The narrator's steering: read off the document server-side, handed
            # to a prompt, and never a response field. The whole channel is a
            # secret, not just the trigger's.
            *GUIDANCE_TOKENS,
        ):
            assert forbidden not in blob, forbidden

    def test_the_reloaded_payload_is_swept_too(self, game_id):
        # `GET /api/games/{id}` rebuilds the transcript from the whole log,
        # referee events included, so it is its own opportunity to leak.
        enter(game_id)
        blob = json.dumps(get_game(game_id))
        for forbidden in ("trigger_fired", "note_recorded", "adit-draught"):
            assert forbidden not in blob, forbidden
        for token in GUIDANCE_TOKENS:
            assert token not in blob, token


class TestTheDocumentCarriesTheQuest:
    """*The Cold Vein*'s concluding quest, as authored.

    The document is the thing under test here — the sessions below only play
    what this class pins. `TestValidationAtLoad` already proves the whole file
    passes `validate_adventure`; this is the shape inside that.
    """

    def test_the_stamp_names_the_engine_the_document_is_written_for(self):
        # The schema version is the half `check_document` enforces: stamped 3, a
        # pre-1.5.0 engine refuses the file out loud instead of stripping it to
        # an unwinnable shell. The engine version is documentation — nothing
        # reads it — which is exactly why it has to be kept honest by hand.
        # 1.6.0 is the first release that reads `ObjectiveSpec.name`, and 1.5.0
        # would load this file happily and show both objectives by their slugs.
        document = json.loads(BUNDLED.read_text(encoding="utf-8"))
        assert document["schema_version"] == 3
        assert document["engine_version"] == "1.6.0"

    def test_the_quest_is_the_only_one_and_it_ends_the_adventure(self):
        quests = load_adventure().quests
        assert [quest.id for quest in quests] == ["the-day-book"]
        quest = quests[0]
        assert quest.name == QUEST
        assert quest.completion == "all"
        assert quest.concludes_adventure is True
        assert quest.activation.pattern.pattern_type == "dungeon_entered"
        assert quest.activation.pattern.dungeon_id == DUNGEON
        assert quest.narrative.offer == OFFER
        assert quest.narrative.completion == COMPLETION
        assert quest.narrative.speaker == SPEAKER

    def test_both_objectives_are_visible_and_speak_only_on_progress(self):
        # Nothing is hidden, so `objective_revealed` never fires in the shipped
        # document and an `offer` beat on either objective would be dead weight
        # — the reveal surface is the barrow fixture's to exercise.
        quest = load_adventure().quests[0]
        recover, home = quest.objectives
        assert recover.id == "recover-day-book"
        assert recover.name == "Recover the captain's day-book"
        assert recover.when.pattern.pattern_type == "item_acquired"
        assert recover.when.pattern.item_id == RELIC
        assert recover.when.conditions == ()
        assert recover.narrative.progress == RECOVERED
        assert home.id == "bring-it-home"
        assert home.name == "Bring it home to Cinderhope"
        assert home.when.pattern.pattern_type == "town_entered"
        assert [condition.item_id for condition in home.when.conditions] == [RELIC]
        # A quest observes; it never takes. The engine rejects the toll form at
        # parse, and the document does not write one.
        assert all(not condition.consumes for condition in home.when.conditions)
        assert home.narrative.progress == HOMECOMING
        for objective in quest.objectives:
            assert not objective.hidden
            assert objective.reveal_when is None
            assert objective.narrative.offer == ""

    def test_the_rewards_are_authored_in_order_and_terminal_legal(self):
        # Both land *after* the session is already `victory`, so both have to be
        # legal there: coins and XP are, spawns and placements are not.
        coins, xp = load_adventure().quests[0].rewards
        assert coins.command_type == "grant_coins"
        assert coins.character_id == "@party"
        assert coins.coins.gp == 100
        assert coins.coins.total_coins == 100
        assert xp.command_type == "award_xp"
        assert xp.character_id == "@party"
        assert xp.amount == 200

    def test_the_document_bundles_both_of_its_own_items(self):
        adventure = load_adventure()
        assert [item.id for item in adventure.items] == ["verrow-token", RELIC]
        book = adventure.items[1]
        assert book.name == DAY_BOOK
        # Found, never bought: the shop stocks the shipped lists alone, and the
        # authored zero is the convention for that, not a price.
        assert book.cost_gp == 0

    def test_the_relic_lies_on_the_cold_face_behind_the_climax(self):
        area = cold_face()
        cache = next(feature for feature in area.features if feature.id == "captains-board")
        assert cache.kind == "treasure_cache"
        assert cache.item_ids == (RELIC,)
        assert cache.cell in area.cells
        # Guarded by the keyed fight, never by a lock or a gate: the route rule
        # holds for the one thing the adventure cannot be won without.
        assert [keyed.template_id for keyed in area.encounter.monsters] == ["zombie"]


class TestTheDocumentSteersTheNarrator:
    """Every `guidance` slot is authored, and the sweeps' tokens track them.

    The sweeps are negative — a token found in a payload is a leak — which is
    only worth something while each token is really in the document. Reword a
    block and its token moves here first.
    """

    def test_every_carrier_authors_guidance(self):
        adventure = load_adventure()
        dungeon = adventure.dungeon(DUNGEON)
        gate = dungeon.level(1).edge((11, 3), Direction.NORTH).door.requires
        blocks = (
            dungeon.level(1).guidance,
            dungeon.level(2).guidance,
            adventure.quests[0].narrative.guidance,
            gate.narrative.guidance,
            adventure.triggers[0].narrative.guidance,
        )
        assert all(blocks)
        for token, block in zip(GUIDANCE_TOKENS, blocks, strict=True):
            assert token in block, token

    def test_the_objectives_author_none(self):
        # The quest's own block says what the errand is. An objective's window is
        # osr-web's own rule rather than osrlib's, so it is pinned against a
        # doctored copy in `tests/test_narration.py` instead of shipped here.
        for objective in load_adventure().quests[0].objectives:
            assert objective.narrative.guidance == ""


class TestTheQuestActivatesAtTheAdit:
    """The charge lands the moment the party first steps into the mine."""

    def test_no_quest_is_active_in_town(self, game_id):
        payload = get_game(game_id)
        assert payload["view"]["mode"] == "town"
        assert payload["view"]["quests"] == []

    def test_entering_the_mine_puts_the_whole_card_in_the_view(self, game_id):
        payload = enter(game_id)
        quest = payload["view"]["quests"][0]
        assert quest["id"] == "the-day-book"
        assert quest["name"] == QUEST
        assert quest["narrative"] == OFFER
        assert quest["speaker"] == SPEAKER
        assert objectives(payload) == [
            {
                "id": "recover-day-book",
                "name": "Recover the captain's day-book",
                "state": "incomplete",
            },
            {
                "id": "bring-it-home",
                "name": "Bring it home to Cinderhope",
                "state": "incomplete",
            },
        ]

    def test_the_offer_reaches_the_transcript_and_the_journal(self, game_id):
        payload = enter(game_id)
        assert {"kind": "system", "text": f"New quest: {QUEST}."} in payload["log"]
        assert {"kind": "prose", "text": OFFER} in payload["log"]
        assert journal_texts(payload).count(OFFER) == 1


class TestTheSpine:
    """Recover it, carry it home: the two objectives and the ending they buy."""

    def test_acquiring_the_relic_completes_the_first_objective(self, game_id):
        enter(game_id)
        payload = grant_the_day_book(game_id)
        assert payload["accepted"]
        assert [entry["state"] for entry in objectives(payload)] == [
            "complete",
            "incomplete",
        ]
        assert {"kind": "prose", "text": RECOVERED} in payload["log"]
        assert journal_texts(payload).count(RECOVERED) == 1
        # Named, never printed as its bundled id: `Narrator._equipment` is the
        # session's effective catalog.
        assert any(DAY_BOOK in text for text in texts(payload))
        assert RELIC not in json.dumps(payload["log"])

    def test_carrying_it_home_wins_the_adventure_and_says_so_once(self, game_id):
        enter(game_id)
        grant_the_day_book(game_id)
        payload = command(game_id, "travel_to_town")
        assert payload["accepted"]
        assert payload["view"]["mode"] == "victory"
        # A completed quest leaves the projection; the victory card is built
        # around that and never tries to read it.
        assert payload["view"]["quests"] == []
        lines = texts(payload)
        assert lines.count(f"Quest complete: {QUEST}.") == 1
        assert lines.count(f"The adventure is won — {QUEST} is done.") == 1
        # The two terminal events carry the same authored beat; it renders on
        # `quest_completed` alone, and the journal records it once.
        assert lines.count(COMPLETION) == 1
        assert journal_texts(payload)[-1] == COMPLETION
        assert journal_texts(payload).count(COMPLETION) == 1

    def test_the_homecoming_beat_precedes_the_ending(self, game_id):
        enter(game_id)
        grant_the_day_book(game_id)
        payload = command(game_id, "travel_to_town")
        lines = texts(payload)
        assert lines.index(HOMECOMING) < lines.index(f"Quest complete: {QUEST}.")
        assert journal_texts(payload) == [
            OFFER,
            JOURNAL,
            RECOVERED,
            HOMECOMING,
            COMPLETION,
        ]

    def test_the_rewards_land_on_every_member_after_the_ending(self, game_id):
        enter(game_id)
        grant_the_day_book(game_id)
        session = _games[game_id].session
        before = {member.id: (member.inventory.purse.gp, member.xp) for member in session.party.members}
        mark = len(session.event_log)
        command(game_id, "travel_to_town")
        delta = session.event_log[mark:]
        ending = next(index for index, event in enumerate(delta) if event.event_type == "adventure_completed")
        coins = {event.character_id: event for event in delta[ending:] if event.event_type == "item_acquired"}
        awards = {event.character_id: event for event in delta[ending:] if event.event_type == "xp_awarded"}
        assert set(coins) == set(before)
        assert set(awards) == set(before)
        for member in session.party.members:
            purse, xp = before[member.id]
            assert coins[member.id].coins_gp_value == 100
            assert member.inventory.purse.gp == purse + 100
            # The authored award is a flat 200 per head; the prime-requisite
            # modifier rides on top, so read what the engine reported rather
            # than recomputing it here.
            assert awards[member.id].award == 200
            assert member.xp == xp + awards[member.id].modified_award


class TestTheHomecomingIsEdgeTriggered:
    """A clause that fails to match waits for the next event, and never retries."""

    def test_coming_home_empty_handed_leaves_the_quest_running(self, game_id):
        enter(game_id)
        payload = command(game_id, "travel_to_town")
        assert payload["accepted"]
        assert payload["view"]["mode"] == "town"
        assert [entry["state"] for entry in objectives(payload)] == [
            "incomplete",
            "incomplete",
        ]
        assert HOMECOMING not in texts(payload)

    def test_an_active_quest_does_not_offer_itself_again(self, game_id):
        enter(game_id)
        command(game_id, "travel_to_town")
        payload = enter(game_id)
        assert f"New quest: {QUEST}." not in texts(payload)
        assert journal_texts(payload).count(OFFER) == 1

    def test_the_next_arrival_carrying_it_is_the_one_that_counts(self, game_id):
        enter(game_id)
        command(game_id, "travel_to_town")
        # `town_entered` has already fired once and matched nothing. Nothing
        # re-checks the condition; the objective simply waits for the *next*
        # arrival, which is what makes the empty-handed trip survivable.
        enter(game_id)
        grant_the_day_book(game_id)
        payload = command(game_id, "travel_to_town")
        assert payload["view"]["mode"] == "victory"
        assert HOMECOMING in texts(payload)
        assert texts(payload).count(f"Quest complete: {QUEST}.") == 1


class TestTheRelicStaysWithItsAdventure:
    """A veteran walks into the next game without the thing that won this one.

    The pointed case of the general import strip in `tests/test_party.py`: the
    homecoming objective is a `has_item` condition, and `has_item` is stateless
    and party-wide, so a day-book that survived the import would let a re-run of
    *The Cold Vein* be won by walking to town.
    """

    def test_an_imported_veteran_carries_no_day_book(self, game_id):
        enter(game_id)
        grant_the_day_book(game_id)
        session = _games[game_id].session
        assert any(member.inventory.carried_item(RELIC) is not None for member in session.party.members), (
            "the fixture must actually carry the relic for this to prove anything"
        )
        document = _save_document(save(game_id)["save_id"])
        party = party_from_save(document)
        for member in party.members:
            assert member.inventory.carried_item(RELIC) is None
