"""The seven documentation shots.

Each test drives the app the way a player would — the actions bar, `W`, `M`, a
double-click on a party row, the note field, the Resolve round button — asserts
that the screen really is showing what the slug claims, and only then calls
[`capture`][tests.screenshots.capture.capture]. Nothing is ever photographed
unconditionally: a green check here is a claim about the picture, not about a file
existing.

Run them with `uv run pytest tests/screenshots`. Bare `uv run pytest` does not
collect this package at all (`norecursedirs` in `pyproject.toml`), so the fast unit
suite needs neither Chromium nor a server.
"""

import re

from playwright.sync_api import Page, expect

from .capture import canvas_ink_ratio, capture
from .conftest import ADVENTURE_NAME, DUNGEON_NAME, CaptureServer
from .drive import (
    act,
    advance_beats,
    enter_dungeon,
    hold_slow_timers,
    light_torch,
    location,
    map_extent,
    note_for_cell,
    open_map,
    resolve_battle,
    seed_party_builder,
    step_forward,
    turn_around,
    turn_left,
    turn_right,
    write_note,
)

ENTRANCE_CELL = (3, 10)
"""Level 1's entrance, where `enter_dungeon` lands the party facing north."""

VAULT_CELL = (14, 6)
"""The tithe vault, one step through the secret door in the countinghouse's east wall."""

MAP_NOTES = (
    (VAULT_CELL, "Secret door — the tithe vault"),
    (ENTRANCE_CELL, "Landing — the way out"),
)
"""The player notes the automap shot writes, in the order it writes them. The
entrance goes last so the map footer is still showing it when the shutter fires."""

QUEST_NAME = "The Toll-Keep's Ledger"
"""The capture document's one quest, as the quests card spells its name."""

QUEST_OBJECTIVES = (
    ("✓", "Recover the toll-keep's ledger"),
    ("○", "Bring it back to Thistlereach"),
)
"""The quest's two objectives and the marks `buildQuestsCard` gives them once the
tithe box is emptied: the ledger is in a pack, and the walk home is still owed.
Both states in one published image, which is the point of authoring a quest into
the capture document at all."""

MIN_CANVAS_INK = 0.002
"""Floor for "this canvas drew something". Both canvases are sparse line art over a
flat ground, so the honest question is blank-versus-not: a canvas that never drew
scores exactly 0.0, while the play view measures around 0.008 and the automap
around 0.03."""

REVEALED_BEATS = 3
"""How many beats of the battle round the `combat-round` shot shows. The round is
frozen there — see `drive.hold_slow_timers` — so the roundbar is still up and the
transcript lands on the same beat every run."""

RAW_ENGINE_TOKENS = ("secret_door:", "room_trap:", "construction:")
"""Token prefixes the engine reports a search find with.

`osrlib.crawl.exploration._reveal` answers a successful search with
`secret_door:{direction}`, `room_trap:{area_id}` and `construction:{feature_id}`.
The last two carry internal ids. `server/narrate.py::_found_phrase` turns all three
into English, and these shots are where a regression would otherwise be published
as an image."""

MIN_TRANSCRIPT_ENTRIES = 40
"""What "a real run of play" means for the `play-view` shot, counted in transcript
entries. The route below lands 49 at seed 42; the floor is set below that so a
wording change in `server/narrate.py` does not fail the shot, while a route that
quietly stopped short still does."""

MIN_MAP_CELLS = 50
"""What "the party has been somewhere" means for the `automap` shot. The route
below draws 65 cells at seed 42 — every cell the party's torchlight has shown
it along the way, remembered as map memory, not just the
walked trail. The bound sits above the 34 cells footprint-only mapping would
draw, so a regression to footprint-only mapping fails here."""

MIN_MAP_WIDTH_FRACTION = 0.5
MIN_MAP_HEIGHT_FRACTION = 0.7
"""How much of the 1456x960 overlay canvas the drawn region has to span before the
shot is worth publishing. Twenty cells in one corner is not a picture of an
automap; the route below measures about 0.69 x 0.83."""

VICTORY_JOURNAL_ENTRIES = 4
"""What the tale comes to on the victory card: the reeve's offer at the landing, the
two objectives' progress beats, and the completion. Asserted exactly, because the
whole argument for putting the journal on the card is that the record is readable
there — a card showing three of four beats is a different picture."""

FORTUNE_LINE = re.compile(r"The party carries home [\d,]+ gp in coin and 1 treasure worth 50 gp\.")
"""The ending's one sentence about the haul, as `victoryFortune` composes it.

The river-green beryl out of the tithe box is the party's only valuable, so this
is the singular branch — "1 treasure", not "1 treasures", which is the trap the
first draft of the template fell into. The coin total is left free: it is the
party's rolled starting gold plus the tithe box's 90 gp and 240 sp plus 25 gp a
head from the reeve, deterministic at seed 42 but not a number worth pinning."""

BUILDER_SEED = 42
"""The party builder's own seed for the `party-builder` shot.

Deliberately the same number as the game's [`SEED`][tests.screenshots.conftest.SEED]
— one seed everywhere is easier to reason about — but a different thing entirely:
this one is builder-scoped, write-only, and never becomes a session's master seed."""

BUILDER_ROLLS = [
    ("Strength", "2 + 4 + 4", "10"),
    ("Intelligence", "6 + 4 + 2", "12"),
    ("Wisdom", "5 + 5 + 5", "15"),
    ("Dexterity", "5 + 5 + 5", "15"),
    ("Constitution", "3 + 3 + 5", "11"),
    ("Charisma", "5 + 1 + 5", "11"),
]
"""Every die the first character rolls at [`BUILDER_SEED`][tests.screenshots.test_shots.BUILDER_SEED],
in the order B/X rolls them. Asserted exactly: if the seed stopped reaching the
builder these would be six other numbers, and the shot would churn on every
recapture instead of failing here."""


def _panel_round(page: Page) -> int:
    """The round number the declaration panel's header is showing.

    Read off the screen rather than assumed, because it depends on whether the
    party was surprised: a surprise round is a resolved round, so the party's
    first declaration can be round two (`server/app.py::_current_battle_round`).

    Args:
        page: The page showing a battle with declarations open.

    Returns:
        The number in the panel header's `Round N`.
    """
    # `text_content`, not `inner_text`: the header is uppercased by CSS, and the
    # underlying text is what the rest of this file's expectations compare against.
    header = page.locator("#party .party-head").text_content() or ""
    match = re.search(r"Round (\d+)", header)
    assert match is not None, "the declaration panel is not showing a round number"
    return int(match.group(1))


def _assert_unnarrated(page: Page, server: CaptureServer, game_id: str) -> None:
    """Assert the transcript about to be photographed is the deterministic one.

    Narration off is checked twice over: the server's own `narration` extra, and
    the absence of any held-back prose. A transcript entry carrying `replace_id` is
    module text the client is holding as a `✦ …` placeholder while it waits for an
    LLM passage, so one in a capture would mean a screenshot of prose that changes
    on every recapture.

    Args:
        page: The page about to be captured.
        server: The capture server, asked for the authoritative game state.
        game_id: The game the page has adopted.
    """
    state = server.get(f"/api/games/{game_id}")
    assert state["narration"]["enabled"] is False, (
        f"narration is live ({state['narration']}) — the transcript would carry model prose"
    )
    held = [entry for entry in state["log"] if entry.get("replace_id") is not None]
    assert not held, f"{len(held)} transcript entries are held back awaiting an LLM passage: {held}"
    assert page.locator("#log .log-entry.pending").count() == 0


def _assert_no_raw_tokens(page: Page, server: CaptureServer, game_id: str) -> None:
    """Assert no engine token is anywhere in the transcript about to be photographed.

    Checked from both ends: the server's own rebuilt transcript (every entry the
    renderer produced, including the ones scrolled out of sight) and the text
    actually on screen. A raw `secret_door:east` or `room_trap:4` in either is an
    entity id leaking into player-facing prose — the thing
    `server/narrate.py::_found_phrase` exists to prevent, and the worst place to
    discover it is a committed PNG.

    Args:
        page: The page about to be captured.
        server: The capture server, asked for the authoritative transcript.
        game_id: The game the page has adopted.
    """
    entries = [entry.get("text", "") for entry in server.get(f"/api/games/{game_id}")["log"]]
    leaked = [text for text in entries if any(t in text for t in RAW_ENGINE_TOKENS)]
    assert not leaked, f"raw engine tokens in the server's transcript: {leaked}"
    on_screen = page.locator("#log").inner_text()
    for token in RAW_ENGINE_TOKENS:
        assert token not in on_screen, f"the raw engine token {token!r} is on screen and about to be photographed"


def _assert_quest_card(page: Page) -> None:
    """Assert the exploring rail's quests card is showing the half-done charge.

    The card is `buildQuestsCard`'s own DOM, read as it renders it: one `.panel`
    in `#context` carrying the objective list, the quest's name, and one `li` per
    objective whose `.quest-obj-mark` is `✓` or `○` and whose `done` class is the
    completed one's only other tell.

    Args:
        page: A page exploring with the ledger taken and the walk home still owed.
    """
    card = page.locator("#context .panel").filter(has=page.locator(".quest-objectives"))
    expect(card).to_have_count(1)
    expect(card.locator("h3")).to_have_text("Quests")
    expect(card.locator(".quest-name")).to_have_text(QUEST_NAME)
    expect(card.locator(".quest-speaker")).to_have_text("— the reeve of Thistlereach")

    objectives = card.locator(".quest-objectives li")
    expect(objectives).to_have_count(len(QUEST_OBJECTIVES))
    for index, (mark, name) in enumerate(QUEST_OBJECTIVES):
        row = objectives.nth(index)
        expect(row.locator(".quest-obj-mark")).to_have_text(mark)
        expect(row).to_contain_text(name)
    expect(objectives.nth(0)).to_have_class(re.compile(r"\bdone\b"))
    assert "done" not in (objectives.nth(1).get_attribute("class") or ""), (
        "the homecoming objective is showing as complete before the party is home"
    )


def _delve_to_the_tithe_vault(page: Page) -> None:
    """Walk the capture adventure's verified route as far as the tithe vault.

    One fixed sequence, shared by the two dungeon shots that need a game with some
    play behind it. It has to stay fixed: the engine's RNG streams advance per
    command, so an extra action anywhere in here moves every roll after it — and
    two of those rolls are load-bearing. The tinder catches on the second strike,
    and the elf's one allowed attempt at the countinghouse wall only lands because
    the thief's listen at the door and sweep for traps have each drawn from the
    exploration stream ahead of it: at seed 42 the next two draws off that stream
    are misses and the third is the hit. Each character gets a single attempt per
    cell per kind, and the Search button always hands the job to the elf, so there
    is no second try to fall back on. `tests/assets/README.md` has the
    measurements.

    Every step goes through the player's own controls: `W` to walk, `D` to turn,
    and the actions bar for Listen, Open door, Find traps, Search and Take
    treasure. The waypoint assertions are what keep a swallowed keystroke from
    turning into a quietly wrong screenshot.

    Args:
        page: A booted page showing the play view in town.
    """
    enter_dungeon(page, DUNGEON_NAME)
    light_torch(page)
    assert location(page)["position"] == list(ENTRANCE_CELL)

    # Three steps north cross into the oil cellar, whose keyed encounter pins
    # `stance: "attacks"` — the party is in a battle the moment the move returns.
    step_forward(page, 3)
    expect(page.locator("#hud-mode")).to_have_text("battle")
    resolve_battle(page)
    expect(page.locator("#hud-mode")).to_have_text("exploring")

    step_forward(page)
    turn_right(page)
    step_forward(page, 5)
    assert location(page) == {
        "position": [8, 6],
        "facing": "east",
        "area": None,
    }, "the corridor east of the oil cellar"

    act(page, "Listen")
    act(page, "Open door")
    step_forward(page, 5)
    assert location(page)["position"] == [13, 6]
    expect(page.locator("#nameplate")).to_have_text("The countinghouse floor")

    act(page, "Find traps")
    act(page, "Search")
    expect(page.locator("#log")).to_contain_text("finds a secret door to the east")
    # The button only exists once the search has revealed the door.
    act(page, "Open door")
    step_forward(page)
    expect(page.locator("#nameplate")).to_have_text("The tithe vault")

    act(page, "Take treasure")
    expect(page.locator("#log")).to_contain_text("in coin")


def _press_on_to_the_north_passage(page: Page) -> None:
    """Carry the route back across the countinghouse and up its north passage.

    The automap draws the cells the party has walked plus whatever its torch
    reaches from where it stands, so where the party *stops* decides how much map
    there is to photograph. Stopping here spans the level corner to corner: the
    walked trail runs from the landing in the south-west to the vault in the
    east, and from the mouth of the passage the torch reaches back down into the
    countinghouse and forward into the sump gallery — glimpsed, not entered, so
    its dart trap never triggers.

    Args:
        page: A page standing in the tithe vault, treasure taken.
    """
    turn_around(page)
    step_forward(page, 3)
    turn_right(page)
    step_forward(page, 4)
    assert location(page) == {"position": [11, 2], "facing": "north", "area": None}


def _walk_home_from_the_vault(page: Page) -> None:
    """Carry the route back out of the toll-keep to the tollgate landing.

    The reverse of the delve, and its first leg is the automap shot's own
    (`turn_around`, then west along row 6). Two measured facts make it more than
    a rewind. The normal door on `9,6:west` swung shut behind the party the
    moment it stepped clear of both cells it joins, so getting back through it
    needs a second Open door — the secret door does not, because the party never
    stands clear of *its* two cells before passing back through it. And no turn
    elapses on the way out: explored cells accrue 10 odometer thirds apiece
    against 30 for new ground, so fifteen moves never reach the 180-third
    threshold. Nothing on the clock moves, which is why the level's one live
    wandering chance (0-in-6 authored, +1 for the noise of the oil-cellar
    battle) is never rolled and the torch is still burning at the landing.

    Args:
        page: A page standing in the tithe vault, treasure taken.
    """
    turn_around(page)
    step_forward(page, 5)
    assert location(page) == {
        "position": [9, 6],
        "facing": "west",
        "area": "The countinghouse floor",
    }, "back at the countinghouse's own side of the normal door"

    act(page, "Open door")
    step_forward(page, 6)
    assert location(page)["position"] == [3, 6]
    turn_left(page)
    step_forward(page, 4)
    assert location(page) == {
        "position": list(ENTRANCE_CELL),
        "facing": "south",
        "area": "Tollgate landing",
    }, "home at the landing, where the way out is"


def test_adventure_library(shot_page: Page) -> None:
    """The adventure library pane: the capture document, browsed, nothing created.

    The route is the player's own: the front door's New adventure button into
    step one of the flow. Browsing is client-side state, so the shot
    is asserted to show the capture adventure alone — name in the list,
    description and hook in the detail pane, the player-safe facts line — with
    zero games POSTed by any of it (the page's own adopted session predates the
    door being opened).
    """
    page = shot_page
    # boot() hid the overlay when it restored the seeded session; open the door
    # over that game, exactly as Menu → New adventure would.
    page.evaluate("openTitle()")
    expect(page.locator("#overlay")).to_be_visible()
    expect(page.locator("#overlay .overlay-title")).to_have_text("OSR Web")
    # Zero saves keeps Continue and Load game off the door entirely.
    expect(page.locator("#door-continue")).to_have_count(0)
    expect(page.locator("#door-load")).to_have_count(0)

    page.locator("#door-new").click()

    expect(page.locator("#overlay .wiz-title")).to_have_text("Choose an adventure")
    items = page.locator("#overlay .lib-item")
    expect(items).to_have_count(1)
    expect(items.first).to_have_text(ADVENTURE_NAME)
    expect(items.first).to_have_class(re.compile(r"\bselected\b"))
    expect(page.locator("#overlay .lib-name")).to_have_text(ADVENTURE_NAME)
    expect(page.locator("#overlay .lib-desc")).to_contain_text("river toll-keep")
    expect(page.locator("#overlay .lib-hook")).to_contain_text("reeve of Thistlereach")
    expect(page.locator("#overlay .lib-facts")).to_contain_text("the town of Thistlereach · one dungeon")
    expect(page.locator("#overlay").get_by_role("button", name="Next — the party")).to_be_enabled()

    capture(page, "adventure-library")


def test_play_view(shot_page: Page, capture_server: CaptureServer, seeded_game: dict) -> None:
    """The main play view, with a real run of play behind it.

    The transcript owns the main column, so the shot has to be taken from a game
    that has actually given it something to own: a fight won, a corridor walked,
    a door listened at and opened, a wall searched, a secret door found, and the
    tithe box emptied. The column is asserted to be genuinely overfull and pinned
    to its bottom — the state a player reads it in — rather than a third of the
    way down an empty pane.
    """
    page = shot_page
    expect(page.locator("#overlay")).to_be_hidden()

    _delve_to_the_tithe_vault(page)
    # Facing the way it came in: the vault is a three-pace cell, so east is a flat
    # wall a step away, while west looks back through the secret door it just
    # opened and down the countinghouse until the torchlight gives out.
    turn_around(page)
    assert location(page)["facing"] == "west"

    expect(page.locator("#hud-mode")).to_have_text("exploring")
    expect(page.locator("#hud-light")).to_have_class("hud-item lit")
    expect(page.locator("#brand-sub")).to_have_text("level 1")
    expect(page.locator("#nameplate")).to_have_text("The tithe vault")
    expect(page.locator("#party .member")).to_have_count(6)
    # The standing charge, in the rail beneath the viewport: the ledger came up
    # with the rest of the tithe box, so objective one is ticked and the walk
    # home is not.
    _assert_quest_card(page)

    entries = page.locator("#log .log-entry")
    assert entries.count() >= MIN_TRANSCRIPT_ENTRIES, (
        f"only {entries.count()} transcript entries — the shot is meant to show a run "
        "of play, not an entrance and a torch"
    )
    # The whole delve is in there: the entrance, the rats, the search, the take.
    expect(page.locator("#log")).to_contain_text("mooring rings")
    expect(page.locator("#log")).to_contain_text("Victory!")
    expect(page.locator("#log")).to_contain_text("finds a secret door to the east")
    expect(page.locator("#log")).to_contain_text("in coin")

    # The point of the shot: the column is full, and showing its bottom the way a
    # player left it. `logPinnedToBottom` in app.js allows 40px of slack.
    page.wait_for_function(
        "() => { const log = document.getElementById('log');"
        " return log.scrollHeight > log.clientHeight + 40"
        " && log.scrollHeight - log.scrollTop - log.clientHeight <= 40; }"
    )

    # The viewport is line art on a dark ground, so only a small share of its pixels
    # carry ink; a blank canvas comes back at exactly 0.0.
    assert canvas_ink_ratio(page, "viewport") > MIN_CANVAS_INK, "the wireframe viewport drew nothing"
    _assert_unnarrated(page, capture_server, seeded_game["game_id"])
    _assert_no_raw_tokens(page, capture_server, seeded_game["game_id"])

    capture(page, "play-view")


def test_character_sheet(shot_page: Page, seeded_game: dict) -> None:
    """The character sheet modal, opened the documented way: double-click a party row."""
    page = shot_page
    enter_dungeon(page, DUNGEON_NAME)
    light_torch(page)

    first = page.locator("#party .member").first
    name = first.locator(".member-name").inner_text().strip()
    assert name == seeded_game["view"]["party"][0]["name"]

    first.dblclick()

    expect(page.locator("#sheet-overlay")).to_be_visible()
    expect(page.locator("#sheet-panel .sheet-head .s-name")).to_have_text(name)
    # A real sheet, not the no-data fallback: abilities, saves, and the inventory.
    assert page.locator("#sheet-panel .ability").count() == 6
    expect(page.locator("#sheet-panel")).to_contain_text("Fighter")

    capture(page, "character-sheet")


def test_automap(shot_page: Page, capture_server: CaptureServer, seeded_game: dict) -> None:
    """The automap overlay over a level the party has actually crossed.

    The map is a summoned record of where the party has been, so it is worth a
    picture only once the party has been somewhere: the full delve to the vault,
    then back across the countinghouse and up its north passage, which spreads
    the drawn region across most of the overlay. Both notes are written through
    the map footer's own input, the entrance last so the footer is still showing
    it when the shutter fires.
    """
    page = shot_page
    _delve_to_the_tithe_vault(page)
    _press_on_to_the_north_passage(page)
    # The overlay leaves the transcript dimly legible down the right-hand side, so
    # the same delve's search line is in this picture too.
    _assert_no_raw_tokens(page, capture_server, seeded_game["game_id"])

    open_map(page)
    expect(page.locator("#map-overlay")).to_be_visible()
    expect(page.locator("#map-title")).to_have_text(f"{ADVENTURE_NAME} — level 1")
    assert canvas_ink_ratio(page, "automap") > MIN_CANVAS_INK, "the automap drew nothing"

    # The whole point of the shot: a map, not a postage stamp in a black pane.
    extent = map_extent(page)
    assert extent["cells"] >= MIN_MAP_CELLS, (
        f"only {extent['cells']} cells are drawn — the party has barely been anywhere"
    )
    assert extent["width_fraction"] >= MIN_MAP_WIDTH_FRACTION, extent
    assert extent["height_fraction"] >= MIN_MAP_HEIGHT_FRACTION, extent

    for cell, text in MAP_NOTES:
        write_note(page, cell, text)

    for cell, text in MAP_NOTES:
        assert note_for_cell(page, cell) == text
    last_cell, last_note = MAP_NOTES[-1]
    expect(page.locator("#map-note")).to_have_value(last_note)
    expect(page.locator("#map-note-hint")).to_have_text(f"cell {last_cell[0]},{last_cell[1]}")

    capture(page, "automap")


def test_combat_round(shot_page: Page, capture_server: CaptureServer, seeded_game: dict) -> None:
    """A live battle round: three moves north into the oil cellar's giant rats.

    At seed 42 the rats surprise the party, so the round on screen is round two —
    and the number is asserted three ways rather than hard-coded, because all
    three surfaces are in the published image at once: the declaration panel's
    header, the roundbar at the foot of the stage, and the transcript's own
    `— Round N —` line a few entries above.
    """
    page = shot_page
    enter_dungeon(page, DUNGEON_NAME)
    light_torch(page)
    # The verified route: the third step crosses into area 2, whose keyed encounter
    # pins `stance: "attacks"`, so the session lands in battle with no menu between.
    step_forward(page, 3)

    expect(page.locator("#hud-mode")).to_have_text("battle")
    expect(page.locator("#party .party-head")).to_contain_text("Round ")
    declared = _panel_round(page)
    expect(page.locator("#party .party-foot")).to_contain_text("Resolve round")
    _assert_unnarrated(page, capture_server, seeded_game["game_id"])

    # Hand-crank the pacing clock so the round freezes on a known beat with the
    # roundbar still up, instead of racing a 700ms timer that then hides it.
    hold_slow_timers(page)
    page.locator("#party .party-foot").get_by_role("button", name="Resolve round").click()

    expect(page.locator("#roundbar")).to_be_visible()
    beats = page.locator("#round-ticks i")
    expect(beats).not_to_have_count(0)
    # The first beat lands with the click; stop short of the last one, which ends the
    # pacing and hides the bar.
    advanced = advance_beats(page, min(REVEALED_BEATS - 1, beats.count() - 1))
    assert advanced["fired"] >= 1, "the paced round never scheduled another beat"

    roundbar = page.locator("#roundbar")
    expect(roundbar).to_be_visible()
    assert "hidden" not in (roundbar.get_attribute("class") or "")
    expect(page.locator("#round-label")).to_have_text(f"round {declared}")
    # The transcript header for the round now resolving. A reader of the image
    # sees it and the roundbar together, so they have to be the same number.
    expect(page.locator("#log .log-entry", has_text="— Round ").last).to_have_text(f"— Round {declared} —")
    expect(page.locator("#round-beat")).not_to_have_text("")
    expect(page.locator("#hud-mode")).to_have_text("battle")
    assert page.locator("#round-ticks i.done").count() == min(REVEALED_BEATS, beats.count())

    capture(page, "combat-round")


def test_party_builder(shot_page: Page) -> None:
    """The roll-your-own creation wizard, stopped on a character's ability rolls.

    The route is the opening flow's own: front door → New adventure → the
    library's Next → staging → Build a custom party → Roll a character — and the
    wizard is doing the thing it exists to do: 3d6 down the line with the dice
    still on the table, each total beside what it actually buys. That is the
    screen worth publishing; the roster it opens on is an empty list.

    It is photographable at all because `POST /api/party-builders` takes a
    builder-scoped seed (`server/creation.py`), handed to the request by
    [`seed_party_builder`][tests.screenshots.drive.seed_party_builder]. The patch
    seeds the staging screen's premade builder too, but the two builders draw
    from their own streams, so the wizard's first character still rolls exactly
    [`BUILDER_ROLLS`][tests.screenshots.test_shots.BUILDER_ROLLS] — asserted
    before the shutter fires, so a seed that stopped arriving fails here rather
    than quietly churning the image on every recapture.
    """
    page = shot_page
    seed_party_builder(page, BUILDER_SEED)
    page.evaluate("openTitle()")
    expect(page.locator("#overlay")).to_be_visible()

    page.locator("#door-new").click()
    expect(page.locator("#overlay .wiz-title")).to_have_text("Choose an adventure")
    page.locator("#overlay").get_by_role("button", name="Next — the party").click()
    expect(page.locator("#overlay .wiz-title")).to_have_text("The party")
    # The staged premade roster is rolled server-side before the wizard opens.
    expect(page.locator("#overlay .r-name-input")).to_have_count(6)

    page.locator("#st-build").click()

    expect(page.locator("#overlay-card.wizard")).to_be_visible()
    expect(page.locator("#overlay .overlay-eyebrow")).to_contain_text("roll up your party")
    expect(page.locator("#overlay .wiz-title")).to_have_text("The party")
    expect(page.locator("#overlay .house-rules")).to_be_visible()

    page.locator("#wiz-roll").click()

    expect(page.locator("#overlay .wiz-title")).to_have_text("Ability scores")
    expect(page.locator("#overlay .wiz-sub")).to_have_text("3d6 down the line — STR, INT, WIS, DEX, CON, CHA")
    rolled = page.locator("#overlay .ability-table tr").evaluate_all(
        "rows => rows.map((row) => ["
        " row.querySelector('.a-name').textContent.trim(),"
        " row.querySelector('.dice').textContent.trim(),"
        " row.querySelector('.score').textContent.trim()])"
    )
    assert [tuple(row) for row in rolled] == BUILDER_ROLLS, (
        f"the wizard rolled {rolled} — the builder seed did not reach the server, so "
        "this image would change on every recapture"
    )
    # The column that makes the screen worth a picture: what a score buys.
    expect(page.locator("#overlay .ability-table tr").nth(3)).to_contain_text("AC +1, missile +1")
    expect(page.locator("#overlay").get_by_role("button", name="Continue")).to_be_visible()

    capture(page, "party-builder")


def test_victory_screen(shot_page: Page, capture_server: CaptureServer, seeded_game: dict) -> None:
    """The ending the delve is for: the ledger carried home to Thistlereach.

    The whole route in one test, because the screen is only reachable at the end
    of one — the reeve's charge lands at the tollgate landing, the ledger comes out of
    the tithe box with the rest of the tithe, and the second objective is the
    walk back. Leaving for town from the landing completes it, the quest
    concludes the adventure, and the session goes terminal, which is what puts
    the overlay up.

    Everything the card claims is asserted before the shutter: the eyebrow and
    the title, the fortune in its singular form, a roster of six, and the whole
    journal — the tale, told, on the card where `J` cannot reach.
    """
    page = shot_page
    _delve_to_the_tithe_vault(page)
    _assert_quest_card(page)
    _walk_home_from_the_vault(page)

    # `Leave for town` renders only on the entrance cell (`cell.at_entrance`),
    # so the button being there *is* the arrival assertion.
    expect(page.locator("#actions").get_by_role("button", name="Leave for town")).to_be_visible()
    _assert_unnarrated(page, capture_server, seeded_game["game_id"])

    act(page, "Leave for town")

    assert page.evaluate("S.view.mode") == "victory"
    overlay = page.locator("#overlay")
    expect(overlay).to_be_visible()
    expect(overlay.locator(".overlay-eyebrow")).to_have_text("the adventure is won")
    expect(overlay.locator(".overlay-title")).to_have_text("The tale is told")
    expect(overlay.locator(".overlay-sub")).to_contain_text(ADVENTURE_NAME)

    fortune = (overlay.locator(".victory-fortune").text_content() or "").strip()
    assert FORTUNE_LINE.fullmatch(fortune), (
        f"the fortune line reads {fortune!r}, which is not the haul this route brings home"
    )
    expect(overlay.locator(".roster-row")).to_have_count(6)
    expect(overlay.get_by_role("button", name="Save the party")).to_be_visible()

    expect(overlay.locator(".victory-journal-label")).to_have_text("The journal")
    entries = overlay.locator(".victory-journal .j-entry")
    expect(entries).to_have_count(VICTORY_JOURNAL_ENTRIES)
    expect(entries.first).to_contain_text("toll-keep's ledger")
    expect(entries.last).to_contain_text("closes the book on the tithe")

    capture(page, "victory-screen")
