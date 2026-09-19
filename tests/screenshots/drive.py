"""Driving the real UI for capture: clicks, keys, and the two clocks we control.

The shots are meant to be pictures of user-reachable states, so everything here
goes through the same surface a player uses — the actions bar, `W`, `M`,
double-click, the note field. Three exceptions exist, none of which reaches into
the app's state — two are about *time*, one is about randomness — and all live
here so the tests read as user actions:

- [`wait_idle`][tests.screenshots.drive.wait_idle] reads `S.busy` / `S.pacing`. A
  readiness probe, not a state change: while a command is in flight the client
  swallows further input, and moving to the next step before it lands would drop
  a keystroke and silently shift the route. Nothing in the DOM reports "the
  client is back at rest", so this is the only honest signal available.
- [`hold_slow_timers`][tests.screenshots.drive.hold_slow_timers],
  [`advance_beats`][tests.screenshots.drive.advance_beats] and
  [`release_slow_timers`][tests.screenshots.drive.release_slow_timers] replace
  `setTimeout` with a hand-cranked queue and hand it back afterwards. The battle
  round reveals one beat every 700ms and then hides `#roundbar`, so a real-clock
  capture would be a race against the pacing timer and the transcript would land
  on a different beat every run. The same crank also lets a route that only wants
  to *get past* a fight skip the pacing outright instead of idling through a
  minute of wall time. This is the same category of environment control as
  stubbing `Math.random` — a fake clock, not a reach into `PACE`.
- [`seed_party_builder`][tests.screenshots.drive.seed_party_builder] adds a seed
  to the one request the client sends without one. `POST /api/party-builders`
  accepts a builder-scoped `seed` that replays every draw, but `startBuilder()`
  in `static/app.js` posts `house_rules` alone, so a wizard opened by clicking
  rolls fresh dice on every run. That is the same problem the game seed has, and
  it gets the same shape of answer: the seed arrives from outside the client
  (there through `localStorage['osrweb_game']`, here through the request body),
  and every click after it is the player's own.
"""

from playwright.sync_api import Page, expect

FORWARD_KEY = "w"
"""The player's forward key (equivalently the `▲` button in the actions bar)."""

TURN_LEFT_KEY = "a"
"""Turns the party 90° anticlockwise (equivalently the `◀` button)."""

TURN_RIGHT_KEY = "d"
"""Turns the party 90° clockwise (equivalently the `▶` button)."""

TURN_AROUND_KEY = "s"
"""Turns the party about face (equivalently the `▼` button)."""

MAP_KEY = "m"
"""Summons the automap overlay."""

LIGHT_KEY = "l"
"""Lights a torch (equivalently the amber `Light torch (n)` button in the actions bar)."""

_MAX_BATTLE_ROUNDS = 12
"""A stop so a broken run fails instead of spinning. At seed 42 the oil cellar's
three giant rats go down in one round — *sleep* lands and the front rank finishes
the helpless survivors."""

_MAX_TORCH_ATTEMPTS = 20
"""Tinder box lighting is 2-in-6 per attempt and each attempt costs a round, so a
run of failures is correct behaviour rather than a bug. At seed 42 the capture
route lights on the second attempt; the cap only exists so a genuinely broken run
fails instead of spinning."""

_SLOW_TIMER_MS = 500
"""Timers at or above this delay are held by the fake clock. The paced battle round
schedules its next beat at 700ms; the only other long timer in `app.js` is the
toast dismissal at 3200ms, and holding that is harmless."""

_HOLD_TIMERS_JS = """
(threshold) => {
  if (window.__captureClock) return false;
  const real = window.setTimeout.bind(window);
  const held = [];
  window.__captureClock = { held, real };
  window.setTimeout = (fn, delay, ...args) => {
    if (typeof fn === 'function' && typeof delay === 'number' && delay >= threshold) {
      held.push(() => fn(...args));
      return -1;
    }
    return real(fn, delay, ...args);
  };
  return true;
}
"""

_ADVANCE_JS = """
(count) => {
  const clock = window.__captureClock;
  if (!clock) throw new Error('the capture clock was never installed');
  let fired = 0;
  for (let i = 0; i < count; i += 1) {
    const held = clock.held.shift();
    if (!held) break;
    held();
    fired += 1;
  }
  return { fired, pending: clock.held.length };
}
"""

_RELEASE_JS = """
() => {
  const clock = window.__captureClock;
  if (!clock) return { restored: false, pending: 0 };
  window.setTimeout = clock.real;
  const pending = clock.held.length;
  delete window.__captureClock;
  return { restored: true, pending };
}
"""

_SEED_BUILDER_JS = """
(seed) => {
  window.__captureBuilderSeed = seed;
  if (window.__captureBuilderFetch) return false;
  const real = window.fetch.bind(window);
  window.__captureBuilderFetch = real;
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.endsWith('/api/party-builders') && init && init.method === 'POST') {
      const body = JSON.parse(init.body || '{}');
      body.seed = window.__captureBuilderSeed;
      init = { ...init, body: JSON.stringify(body) };
    }
    return real(input, init);
  };
  return true;
}
"""

_LOCATION_JS = """
() => ({
  position: S.view.location.position,
  facing: S.view.location.facing,
  area: S.cell ? S.cell.area_name : null,
})
"""

_MAP_EXTENT_JS = """
() => {
  const canvas = document.getElementById('automap');
  const transform = MAP.t;
  if (!transform) throw new Error('the automap has not drawn yet');
  const loc = S.view.location;
  const level = S.view.explored.find(
    (l) => l.dungeon_id === loc.dungeon_id && l.level_number === loc.level_number
  );
  const xs = level.cells.map(([x]) => x);
  const ys = level.cells.map(([, y]) => y);
  const width = (Math.max(...xs) + 1 - Math.min(...xs)) * transform.size;
  const height = (Math.max(...ys) + 1 - Math.min(...ys)) * transform.size;
  return {
    cells: level.cells.length,
    width_fraction: width / canvas.width,
    height_fraction: height / canvas.height,
  };
}
"""

_CELL_POINT_JS = """
(cell) => {
  const canvas = document.getElementById('automap');
  const transform = MAP.t;
  if (!transform) throw new Error('the automap has not drawn yet');
  const box = canvas.getBoundingClientRect();
  const scale = box.width / canvas.width;
  return {
    x: box.left + (transform.ox + (cell[0] + 0.5) * transform.size) * scale,
    y: box.top + (transform.oy + (cell[1] + 0.5) * transform.size) * scale,
  };
}
"""

_NOTE_FOR_CELL_JS = """
(cell) => {
  const all = JSON.parse(localStorage.getItem('osrweb_notes') || '{}');
  const level = all[`${S.view.adventure_name}|${S.view.location.dungeon_id}|${S.view.location.level_number}`] || {};
  return level[`${cell[0]},${cell[1]}`] || null;
}
"""


def seed_party_builder(page: Page, seed: int) -> None:
    """Give the wizard a fixed seed before the player opens it.

    `POST /api/party-builders` takes an optional builder-scoped `seed` that replays
    the wizard's own draws — abilities, hit points, gold — for a given sequence of
    steps, which is what lets a rolled character be photographed at all. The client
    never sends one: `startBuilder()` posts `house_rules` alone. So the seed is
    added to that one request on its way out, exactly as the game's seed is handed
    to the page through `localStorage`, and every click from the title screen
    onwards is still the player's own.

    The patch matches only `POST /api/party-builders`; the catalog fetch and the
    per-step `POST /api/party-builders/{id}/step` calls pass through untouched.
    Idempotent — calling it again just changes the seed.

    Args:
        page: A booted page, before the wizard is opened.
        seed: The builder's seed. Never a game session's master seed: this one is
            write-only, is never echoed in a payload, and drives only the wizard.
    """
    page.evaluate(_SEED_BUILDER_JS, seed)


def wait_idle(page: Page, timeout: float = 15_000) -> None:
    """Wait until the client has finished the command in flight and is taking input again.

    Args:
        page: The page under capture.
        timeout: Milliseconds to wait before failing.
    """
    page.wait_for_function("!S.busy && !S.pacing", timeout=timeout)


def hud_mode(page: Page) -> str:
    """Read the HUD's mode chip — the client's own report of the engine mode."""
    return page.locator("#hud-mode").inner_text().strip()


def location(page: Page) -> dict:
    """Where the party stands, as the client itself has it.

    A readiness/waypoint probe of the same family as
    [`wait_idle`][tests.screenshots.drive.wait_idle]: it reads the client's own
    state and changes nothing. The route asserts its waypoints with it, so a
    swallowed keystroke fails at the step that dropped it rather than five steps
    later in a screenshot nobody looks at.

    Returns:
        `{"position": [x, y], "facing": str, "area": str | None}`.
    """
    return page.evaluate(_LOCATION_JS)


def act(page: Page, label: str) -> None:
    """Click one button in the actions bar by its own label, then wait for rest.

    The bar is rebuilt from the engine state on every render, so a button that is
    not there is a real statement about the game — `Open door` only exists while
    the party faces a door it knows about, and the secret door on the
    countinghouse's east wall does not offer one until somebody finds it.

    Args:
        page: A page in the exploring mode.
        label: The button's visible label, for example `Search` or `Take treasure`.
    """
    page.locator("#actions").get_by_role("button", name=label).click()
    wait_idle(page)


def enter_dungeon(page: Page, dungeon_name: str) -> None:
    """Click the town's enter button and wait for the party to be exploring.

    Args:
        page: A booted page showing the play view in town.
        dungeon_name: The dungeon's display name, as the button spells it.
    """
    page.locator("#actions").get_by_role("button", name=f"Enter {dungeon_name}").click()
    expect(page.locator("#hud-mode")).to_have_text("exploring")
    wait_idle(page)


def light_torch(page: Page) -> int:
    """Strike the tinder box until a torch catches, the way a player does.

    Without light the party sees only the space it stands on: the wireframe viewport
    reads "It is dark. Light a torch." and the automap knows three cells. Every
    dungeon shot lights up first so the pictures show the game rather than the dark.
    The attempts are part of the fixed command prefix, so the route downstream stays
    reproducible.

    Args:
        page: A page in the exploring mode with torches in the party's packs.

    Returns:
        How many attempts it took.

    Raises:
        AssertionError: If no torch catches within
            [`_MAX_TORCH_ATTEMPTS`][tests.screenshots.drive._MAX_TORCH_ATTEMPTS].
    """
    for attempt in range(1, _MAX_TORCH_ATTEMPTS + 1):
        if page.locator("#hud-light.lit").count():
            return attempt - 1
        page.keyboard.press(LIGHT_KEY)
        wait_idle(page)
    assert page.locator("#hud-light.lit").count(), (
        f"no torch caught in {_MAX_TORCH_ATTEMPTS} attempts — the party would be photographed in the dark"
    )
    return _MAX_TORCH_ATTEMPTS


def step_forward(page: Page, steps: int = 1) -> None:
    """Press forward, one settled step at a time.

    Each step waits for the client to come back to rest before the next keystroke,
    because the app drops input while a command is in flight — a swallowed key
    would silently shorten the route and move every roll after it.

    Args:
        page: A page in the exploring mode.
        steps: How many spaces to walk.
    """
    for _ in range(steps):
        page.keyboard.press(FORWARD_KEY)
        wait_idle(page)


def turn_left(page: Page) -> None:
    """Turn the party 90° anticlockwise, the way `A` (or the `◀` button) does."""
    page.keyboard.press(TURN_LEFT_KEY)
    wait_idle(page)


def turn_right(page: Page) -> None:
    """Turn the party 90° clockwise, the way `D` (or the `▶` button) does."""
    page.keyboard.press(TURN_RIGHT_KEY)
    wait_idle(page)


def turn_around(page: Page) -> None:
    """Turn the party about face, the way `S` (or the `▼` button) does."""
    page.keyboard.press(TURN_AROUND_KEY)
    wait_idle(page)


def resolve_battle(page: Page) -> int:
    """Fight the battle out through the party panel's own Resolve round button.

    Each round is revealed one beat every 700ms; on the real clock a five-round
    fight would spend a minute of wall time doing nothing but waiting. So the
    fake clock is installed for the fight and cranked flat out — the same
    environment control [`hold_slow_timers`][tests.screenshots.drive.hold_slow_timers]
    exists for, used here to skip the pacing rather than to freeze it — and then
    released, so no held timer (a toast dismissal, say) can survive into a later
    shot.

    Args:
        page: A page whose HUD reads `battle`.

    Returns:
        How many rounds it took.

    Raises:
        AssertionError: If the fight has not ended within
            [`_MAX_BATTLE_ROUNDS`][tests.screenshots.drive._MAX_BATTLE_ROUNDS].
    """
    hold_slow_timers(page)
    rounds = 0
    while hud_mode(page) == "battle":
        assert rounds < _MAX_BATTLE_ROUNDS, (
            f"the battle was still running after {rounds} rounds — the route has drifted"
        )
        page.locator("#party .party-foot").get_by_role("button", name="Resolve round").click()
        # The roundbar appearing is the signal that the round has landed and is
        # revealing; cranking before that would find an empty timer queue and
        # leave the pacing to a clock nobody is winding.
        expect(page.locator("#roundbar")).to_be_visible()
        while advance_beats(page, 256)["fired"]:
            pass
        wait_idle(page)
        rounds += 1
    released = release_slow_timers(page)
    assert released["pending"] == 0, f"{released['pending']} paced beats were still held when the fight ended"
    return rounds


def open_map(page: Page) -> None:
    """Summon the automap with the `M` key and wait for the overlay."""
    page.keyboard.press(MAP_KEY)
    expect(page.locator("#map-overlay")).to_be_visible()


def map_extent(page: Page) -> dict:
    """How much of the automap canvas the drawn region actually covers.

    The automap draws the cells the party has walked plus whatever its light
    reaches from where it stands, scaled to fit — so "is there a map here or a
    postage stamp" is a question about the bounding box of those cells against
    the canvas, not about the file on disk.

    Args:
        page: A page with the map overlay open and drawn.

    Returns:
        `{"cells": int, "width_fraction": float, "height_fraction": float}`.
    """
    return page.evaluate(_MAP_EXTENT_JS)


def select_map_cell(page: Page, cell: tuple[int, int]) -> None:
    """Click one explored cell on the automap, the way a player picks a cell to annotate.

    The point is computed from the canvas's own drawing transform and clicked with
    the real mouse, so the app's canvas click handler does the inverse mapping.

    Args:
        page: A page with the map overlay open.
        cell: The `(x, y)` dungeon cell to click.
    """
    point = page.evaluate(_CELL_POINT_JS, list(cell))
    page.mouse.click(point["x"], point["y"])
    expect(page.locator("#map-note-hint")).to_have_text(f"cell {cell[0]},{cell[1]}")


def write_note(page: Page, cell: tuple[int, int], text: str) -> None:
    """Add a player note to one cell through the map footer's own input.

    Args:
        page: A page with the map overlay open.
        cell: The `(x, y)` cell to annotate.
        text: The note to write.
    """
    select_map_cell(page, cell)
    page.locator("#map-note").fill(text)
    page.locator("#map-note").press("Enter")


def note_for_cell(page: Page, cell: tuple[int, int]) -> str | None:
    """Read back what the client stored for a cell, from its own `osrweb_notes` store.

    Args:
        page: A page in a dungeon.
        cell: The `(x, y)` cell to look up.

    Returns:
        The stored note, or None when the cell carries none.
    """
    return page.evaluate(_NOTE_FOR_CELL_JS, list(cell))


def hold_slow_timers(page: Page) -> None:
    """Install the fake clock: long timers queue up instead of firing on their own.

    Call this before the action that starts the paced battle round. Idempotent.
    """
    page.evaluate(_HOLD_TIMERS_JS, _SLOW_TIMER_MS)


def advance_beats(page: Page, count: int) -> dict:
    """Crank the fake clock forward by `count` held timers.

    Each paced beat appends its transcript entry and schedules the next one, so
    firing `count` held callbacks reveals exactly `count` further beats.

    Args:
        page: A page whose fake clock is installed.
        count: How many held timers to fire.

    Returns:
        `{"fired": int, "pending": int}` — how many ran and how many are still held.
    """
    return page.evaluate(_ADVANCE_JS, count)


def release_slow_timers(page: Page) -> dict:
    """Give `setTimeout` back to the page and report anything left in the queue.

    Called once the paced round is over, so the rest of a route runs on the real
    clock and nothing the app schedules later can be silently swallowed.

    Returns:
        `{"restored": bool, "pending": int}` — whether a clock was installed, and
        how many callbacks were still held when it came out.
    """
    return page.evaluate(_RELEASE_JS)
