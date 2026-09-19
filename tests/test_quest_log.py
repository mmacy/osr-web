"""End-to-end pins for the quest projection and the journal, over the barrow.

`tests/test_authored_layer.py` drives the bundled document and its own concluding
quest. This module drives `tests/assets/barrow-adventure.json` — *The Barrow of
the Ninth*, generated from osrlib's own fixture builder (recipe in
`tests/assets/README.md`) — for the shape *The Cold Vein* deliberately does not
author: a hidden objective, the reveal that surfaces it, and the beat that rides
the reveal.

What is pinned here is the shape and the content of what crosses the wire, which
is the whole contract the client's quests card and journal overlay read. The
client itself has no unit harness; it is proven by the walk in the phase's
definition of done.

Endpoints are called directly (no HTTP layer) in the repo's style, with the
library and saves directories redirected per `tests/test_paths.py`. The barrow is
three moves end to end, so the party walks: `PlaceParty` is for routes that would
otherwise be a test about walking.
"""

import json
from pathlib import Path

import pytest

from server import library
from server.app import (
    _games,
    create_game,
    execute_command,
    get_game,
    save,
)

BARROW = Path(__file__).resolve().parent / "assets" / "barrow-adventure.json"
DUNGEON = "barrow"

QUEST = "The Votive Idol"
OFFER = "Sister Halda wants the ninth idol back in the temple, rite and all."
SPEAKER = "Sister Halda"
MOUTH = "The barrow takes you in, and the daylight stops at the lintel."
PROGRESS = "The idol comes out of its niche as if it were waiting."
RITE_OFFER = "The slab wants words said over it before the idol may leave."
RITE_PROGRESS = "The rite is spoken, and the cold goes out of the air."
COMPLETION = "The barrow is quiet. The idol is yours to carry home."


@pytest.fixture(autouse=True)
def _redirected_dirs(monkeypatch, tmp_path):
    """Play the barrow, and keep every test off the repo's saves and library."""
    empty_drop = tmp_path / "drop"
    empty_drop.mkdir()
    monkeypatch.setenv("OSR_WEB_SAVES_DIR", str(tmp_path / "saves"))
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(empty_drop))
    monkeypatch.setenv("OSR_WEB_ADVENTURE", str(BARROW))
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


def to_the_shrine(game_id):
    """Walk from the entrance to the reliquary at (2, 0), the party already in."""
    command(game_id, "turn_party", facing="east")
    command(game_id, "move_party", direction="east")
    return command(game_id, "move_party", direction="east")


def lift_the_idol(game_id):
    return command(game_id, "take_treasure", feature_id="reliquary")


def take_the_idol(game_id):
    """Enter the barrow, walk to the shrine, and lift the idol out."""
    enter(game_id)
    to_the_shrine(game_id)
    return lift_the_idol(game_id)


def objectives(payload) -> list[dict]:
    return payload["view"]["quests"][0]["objectives"]


def journal_texts(payload) -> list[str]:
    return [entry["text"] for entry in payload["view"]["journal"]]


def win_the_barrow(game_id):
    """Take the idol, then write the flag the second objective waits on.

    `speak-the-rite` completes on `flag_set barrow.rite = spoken`, and no player
    command in the barrow writes it — so the referee does, the way this file's
    sibling places a party. The crypt walk that would *reveal* the objective is
    not needed: completing one reveals it.
    """
    take_the_idol(game_id)
    return command(game_id, "set_flag", key="barrow.rite", value="spoken")


class TestTheQuestReachesThePlayer:
    """The card's whole contract: one active quest, in the speaker's voice."""

    def test_no_quest_is_active_in_town(self, game_id):
        # The quest activates on `dungeon_entered`, so the card has nothing to
        # render until the party goes in — which is what the client's "absent
        # entirely, nothing reserving height" rule is for.
        payload = get_game(game_id)
        assert payload["view"]["mode"] == "town"
        assert payload["view"]["quests"] == []

    def test_entering_the_barrow_activates_the_quest(self, game_id):
        payload = enter(game_id)
        assert [quest["id"] for quest in payload["view"]["quests"]] == ["the-idol"]
        quest = payload["view"]["quests"][0]
        assert quest["name"] == QUEST
        assert quest["narrative"] == OFFER
        assert quest["speaker"] == SPEAKER

    def test_the_hidden_objective_is_absent_not_masked(self, game_id):
        # Absent, with no placeholder of any kind: a hidden objective's id is the
        # one secret this projection could plausibly leak.
        payload = enter(game_id)
        assert [entry["id"] for entry in objectives(payload)] == ["recover-idol"]
        assert "speak-the-rite" not in json.dumps(payload)

    def test_an_objective_is_exactly_id_name_and_state(self, game_id):
        # Pinned as a set, the way `tests/test_saves.py` pins a summary's keys: a
        # field appearing here is a field the card would have to decide about.
        payload = enter(game_id)
        for entry in objectives(payload):
            assert set(entry) == {"id", "name", "state"}
            assert entry["state"] in {"incomplete", "complete"}

    def test_the_objective_carries_its_authored_name(self, game_id):
        payload = enter(game_id)
        assert objectives(payload)[0]["name"] == "Recover the idol"


class TestObjectivesTick:
    """Incomplete → complete, and hidden → revealed, through real commands."""

    def test_taking_the_idol_completes_the_first_objective(self, game_id):
        payload = take_the_idol(game_id)
        assert payload["accepted"]
        assert objectives(payload) == [{"id": "recover-idol", "name": "Recover the idol", "state": "complete"}]

    def test_the_progress_beat_lands_in_the_transcript_and_the_journal(self, game_id):
        payload = take_the_idol(game_id)
        assert {"kind": "prose", "text": PROGRESS} in payload["log"]
        assert PROGRESS in [entry["text"] for entry in payload["view"]["journal"]]

    def test_entering_the_crypt_reveals_the_hidden_objective(self, game_id):
        take_the_idol(game_id)
        payload = command(game_id, "move_party", direction="east")
        assert payload["view"]["location"]["position"] == [3, 0]
        assert objectives(payload) == [
            {"id": "recover-idol", "name": "Recover the idol", "state": "complete"},
            {"id": "speak-the-rite", "name": "Speak the rite", "state": "incomplete"},
        ]
        assert {"kind": "prose", "text": RITE_OFFER} in payload["log"]

    def test_the_quest_stays_active_and_alone(self, game_id):
        # `view.quests` is the active set: nothing completes here, because no
        # player command writes the flag `speak-the-rite` waits on.
        take_the_idol(game_id)
        payload = command(game_id, "move_party", direction="east")
        assert len(payload["view"]["quests"]) == 1
        assert payload["view"]["mode"] == "exploring"


class TestTheJournal:
    """Append-only, in order, and stamped — the record the overlay renders."""

    def test_every_entry_is_exactly_text_and_rounds(self, game_id):
        payload = take_the_idol(game_id)
        assert payload["view"]["journal"]
        for entry in payload["view"]["journal"]:
            assert set(entry) == {"text", "rounds"}

    def test_the_record_grows_by_appending(self, game_id):
        opening = enter(game_id)["view"]["journal"]
        # The trigger's beat, then the quest's offer — both written on the way in.
        assert [entry["text"] for entry in opening] == [MOUTH, OFFER]
        to_the_shrine(game_id)
        taken = lift_the_idol(game_id)["view"]["journal"]
        assert taken[: len(opening)] == opening
        assert [entry["text"] for entry in taken[len(opening) :]] == [PROGRESS]

    def test_the_stamps_are_the_clock_the_hud_reads(self, game_id):
        # One dungeon turn is 60 rounds, and `enter_dungeon` charges the town's
        # authored travel time first — the barrow authors one turn, so the
        # arrival beats are stamped turn 1, never turn 0. Two moves later the
        # idol comes up on turn 2.
        assert [entry["rounds"] for entry in enter(game_id)["view"]["journal"]] == [
            60,
            60,
        ]
        to_the_shrine(game_id)
        journal = lift_the_idol(game_id)["view"]["journal"]
        assert [entry["rounds"] for entry in journal] == [60, 60, 120]


class TestNoQuestWiringCrosses:
    """The projection is the charge, never the machinery behind it."""

    WIRING = (
        "pattern",
        "reveal_when",
        "rewards",
        "concludes_adventure",
        "guidance",
        "activation",
        "fired_triggers",
    )

    def test_the_wiring_is_absent_before_the_hidden_objective_reveals(self, game_id):
        # Swept before the crypt, so the hidden objective's id is still a secret
        # and counts as the sweep's sharpest term. Deliberately *not* swept:
        # `votive_idol` rides inventory rows and `the-idol` is `QuestView.id`.
        blob = json.dumps(enter(game_id))
        for forbidden in (*self.WIRING, "speak-the-rite", "barrow.rite"):
            assert forbidden not in blob, forbidden

    def test_the_wiring_is_still_absent_once_it_is_all_revealed(self, game_id):
        take_the_idol(game_id)
        payload = command(game_id, "move_party", direction="east")
        blob = json.dumps(payload)
        for forbidden in (*self.WIRING, "barrow.rite"):
            assert forbidden not in blob, forbidden

    def test_the_reloaded_payload_is_swept_too(self, game_id):
        # `GET /api/games/{id}` rebuilds the transcript from the whole log and
        # re-dumps the view, so it is its own opportunity to leak.
        take_the_idol(game_id)
        blob = json.dumps(get_game(game_id))
        for forbidden in (*self.WIRING, "barrow.rite"):
            assert forbidden not in blob, forbidden

    def test_the_victory_payload_is_swept_too(self, game_id):
        # The ending is where the wiring is closest to the surface: rewards
        # resolve in this very command, and `concludes_adventure` is the thing
        # that made it happen. Neither may ride out on the payload the victory
        # screen is built from.
        blob = json.dumps(win_the_barrow(game_id))
        for forbidden in (*self.WIRING, "barrow.rite"):
            assert forbidden not in blob, forbidden


class TestSaveAndRestore:
    """Quest state and the journal are engine-owned and round-trip whole."""

    def test_a_restored_game_keeps_its_progress_and_its_record(self, game_id):
        journal = take_the_idol(game_id)["view"]["journal"]
        save_id = save(game_id)["save_id"]
        restored = create_game({"save_id": save_id})
        try:
            view = restored["view"]
            assert view["quests"][0]["objectives"] == [
                {"id": "recover-idol", "name": "Recover the idol", "state": "complete"}
            ]
            assert view["journal"] == journal
        finally:
            _games.pop(restored["game_id"], None)


class TestTheEnding:
    """The terminal payload the victory screen is built from.

    The screen itself has no unit harness — it is proven by the phase's walk —
    so what is pinned here is everything it reads: the mode that routes to it,
    the empty quest list it is designed around, and the journal it renders on
    the card.
    """

    def test_completing_the_concluding_quest_ends_the_session_in_victory(self, game_id):
        payload = win_the_barrow(game_id)
        assert payload["accepted"]
        assert payload["view"]["mode"] == "victory"
        # A completed quest leaves the projection: the card never reads it, and
        # `#context` has nothing left to render either.
        assert payload["view"]["quests"] == []

    def test_the_journal_is_whole_ordered_and_ends_with_the_completion_beat(self, game_id):
        payload = win_the_barrow(game_id)
        assert journal_texts(payload) == [
            MOUTH,
            OFFER,
            PROGRESS,
            RITE_PROGRESS,
            COMPLETION,
        ]

    def test_the_ending_is_said_once_across_the_two_terminal_events(self, game_id):
        # `quest_completed` and `adventure_completed` arrive back to back
        # carrying the *same* authored beat. The beat rides the first; the
        # second states the terminal fact and repeats nothing.
        lines = texts(win_the_barrow(game_id))
        assert lines.count(f"Quest complete: {QUEST}.") == 1
        assert lines.count(f"The adventure is won — {QUEST} is done.") == 1
        assert lines.count(COMPLETION) == 1


class TestTheVictorySaveRoundTrips:
    """The server-side half of the victory screen's Save the party button."""

    def test_a_save_taken_at_victory_restores_to_victory(self, game_id):
        journal = journal_texts(win_the_barrow(game_id))
        save_id = save(game_id)["save_id"]
        restored = create_game({"save_id": save_id})
        try:
            assert restored["view"]["mode"] == "victory"
            assert restored["view"]["quests"] == []
            assert journal_texts(restored) == journal
        finally:
            _games.pop(restored["game_id"], None)
