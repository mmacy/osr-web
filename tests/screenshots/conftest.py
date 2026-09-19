"""Fixtures for the documentation-screenshot harness.

Three things have to be true before a single pixel is written, and they are all set
up here:

**The server must be capture-safe.** `OSR_WEB_ADVENTURE` alone is not enough. The
adventure picker and the saves picker are separate scans with their own roots, and
on a developer's checkout they render nine adventures (several of them retail
modules) and sixteen saves named after retail modules straight into the title
screen's two `<select>` elements. So the capture server runs with
`OSR_WEB_ADVENTURES_DIR` and `OSR_WEB_SAVES_DIR` pointed at empty temp
directories, `HOME` redirected to a temp directory so no developer path can leak
into a published image, and an ephemeral port so it can never collide with (or be
mistaken for) the owner's own server. Then
[`_guard_capture_safety`][tests.screenshots.conftest._guard_capture_safety] asks
the running server what it is actually serving and aborts the session if the answer
is anything but one adventure and zero saves.

**Narration must be off.** Deleting `OSR_WEB_NARRATOR` from the child environment
does *not* do this: `server/app.py` calls `load_dotenv()` at import with the default
`override=False`, which fills in variables that are *absent* and leaves alone
variables that are *present but empty*. On a checkout whose `.env` names a live
provider, an unset variable therefore turns narration **on**, and on CI (no `.env`)
it would stay off — a defect that reproduces only on developer machines and bakes
model-generated prose into committed PNGs. The whole `OSR_WEB_NARRATOR*` family is
therefore set to the empty string, and the guard then verifies the result against
the server's own `narration` report rather than trusting the environment.

**The session must be reproducible.** The client never sends a seed, so the game is
created server-side with `seed: 42` and adopted by the browser through
`localStorage['osrweb_game']`, which is what `boot()` restores from. `Math.random`
is stubbed in an init script because the title screen picks its hook at random. The
browser viewport and device scale factor are fixed so image dimensions do not vary
by machine.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.sync_api import Page

from .capture import CAPTURE_ADVENTURE, MANIFEST, REPO_ROOT

ADVENTURE_ID = "the-hollow-tithe"
"""Library slug the capture document lands under."""

ADVENTURE_NAME = "The Hollow Tithe"
"""Display name the title screen and the enter button spell out."""

DUNGEON_NAME = "The Hollow Tithe"
"""Name on the town's enter button."""

SEED = 42
"""The one seed every capture uses; two seed-42 games roll identical parties."""

VIEWPORT = {"width": 1440, "height": 900}
"""Fixed browser viewport, so shot dimensions do not vary by machine."""

DEVICE_SCALE_FACTOR = 2
"""Retina capture: the PNGs come out at twice `VIEWPORT`."""

_STARTUP_TIMEOUT_S = 60.0


def _free_port() -> int:
    """Ask the OS for an unused TCP port and hand it straight back.

    Binding port 0 and releasing it is the portable way to get a port nobody is
    using; the capture server claims it a moment later. Never a fixed port — port
    8620 is the repo owner's own server and must never be captured against.

    Returns:
        A port number that was free at the moment of asking.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(url: str, body: dict | None = None, timeout: float = 60.0) -> dict:
    """Do one JSON request against the capture server.

    Args:
        url: Absolute URL.
        body: JSON body for a POST; None makes it a GET.
        timeout: Socket timeout in seconds.

    Returns:
        The decoded JSON response.
    """
    if body is None:
        request = urllib.request.Request(url)
    else:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


@dataclass(frozen=True)
class CaptureServer:
    """A running, capture-safe osr-web server and the temp roots it was given."""

    base_url: str
    adventures_dir: Path
    saves_dir: Path
    home_dir: Path
    log_path: Path

    def get(self, path: str) -> dict:
        """GET one API path (for example `/api/adventures`) and decode the JSON."""
        return _request(f"{self.base_url}{path}")

    def post(self, path: str, body: dict | None = None) -> dict:
        """POST JSON to one API path and decode the JSON response."""
        return _request(f"{self.base_url}{path}", body if body is not None else {})

    def new_seeded_game(self) -> dict:
        """Create a fresh seed-42 game on the capture adventure.

        The browser client never sends a seed of its own, so every reproducible
        session starts here and is adopted by the page through `localStorage`.

        Returns:
            The full `POST /api/games` payload, including `game_id` and `narration`.
        """
        return self.post("/api/games", {"seed": SEED, "adventure_id": ADVENTURE_ID})


def _capture_environment(adventures_dir: Path, saves_dir: Path, home_dir: Path) -> dict[str, str]:
    """Build the child environment a capture server runs under.

    Args:
        adventures_dir: An empty directory replacing the repo `adventures/` drop directory.
        saves_dir: An empty directory replacing the repo `saves/` directory.
        home_dir: A temp `HOME`, so no developer path can be rendered into an image.

    Returns:
        The environment mapping to hand to the subprocess.
    """
    env = dict(os.environ)
    env["OSR_WEB_ADVENTURE"] = str(CAPTURE_ADVENTURE)
    env["OSR_WEB_ADVENTURES_DIR"] = str(adventures_dir)
    env["OSR_WEB_SAVES_DIR"] = str(saves_dir)
    # The plural one appends extra sources rather than replacing the drop directory;
    # blank it so an exported value cannot smuggle a local module into the picker.
    env["OSR_WEB_ADVENTURES"] = ""
    # Empty, never deleted: `server/app.py` runs load_dotenv(override=False), which
    # fills in absent variables from the repo `.env` and leaves present-but-empty
    # ones alone. Deleting these would switch narration ON wherever `.env` names a
    # provider, and bake nondeterministic model prose into the captures.
    for name in (
        "OSR_WEB_NARRATOR",
        "OSR_WEB_NARRATOR_MODEL",
        "OSR_WEB_NARRATOR_URL",
        "OSR_WEB_NARRATOR_TIMEOUT",
        "OSR_WEB_NARRATOR_API_KEY",
    ):
        env[name] = ""
    env["HOME"] = str(home_dir)
    return env


def _await_ready(base_url: str, process: subprocess.Popen, log_path: Path) -> None:
    """Poll the API until the server answers, or fail with its log.

    Polling the real endpoint (rather than sleeping) is what makes the harness
    usable on a slow machine and quick on a fast one.

    Args:
        base_url: Root URL of the server being started.
        process: The server process, watched for an early exit.
        log_path: Where the server's output is being collected.

    Raises:
        RuntimeError: If the process dies or never answers in time.
    """
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"the capture server exited with code {process.returncode} during startup.\n{_tail(log_path)}"
            )
        try:
            _request(f"{base_url}/api/adventures", timeout=2.0)
            return
        except urllib.error.URLError, OSError, TimeoutError:
            time.sleep(0.1)
    raise RuntimeError(
        f"the capture server never answered on {base_url} within {_STARTUP_TIMEOUT_S:.0f}s.\n{_tail(log_path)}"
    )


def _tail(log_path: Path, lines: int = 40) -> str:
    """Render the last few lines of the server log for a failure message."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no server log)"
    return "server log tail:\n" + "\n".join(text.splitlines()[-lines:])


def _guard_capture_safety(server: CaptureServer) -> dict:
    """Refuse to capture anything unless the server is serving exactly the capture set.

    Three questions, all answered by the running server rather than by the
    environment we *meant* to set:

    1. Does the library hold exactly one adventure, and is it the capture document?
       Anything else means the title screen's adventure `<select>` would photograph
       the developer's local library — which on this checkout is mostly retail
       modules.
    2. Are there zero saves? `GET /api/saves` feeds the "Resume a saved game"
       picker with each save's adventure name.
    3. Is narration off? An environment variable is an intention; the `narration`
       extra on a game payload is evidence.

    Args:
        server: The started capture server.

    Returns:
        A small evidence dict, useful in the run's own output.

    Raises:
        Exit: Ends the whole pytest session rather than let one shot be taken.
    """
    adventures = server.get("/api/adventures")["adventures"]
    names = [entry["name"] for entry in adventures]
    if names != [ADVENTURE_NAME]:
        pytest.exit(
            "CAPTURE ABORTED — the server is not library-isolated.\n"
            f"  GET /api/adventures returned {len(adventures)} adventure(s): {names}\n"
            f"  Expected exactly one: [{ADVENTURE_NAME!r}]\n"
            "  Capturing now would photograph this machine's local adventure library into\n"
            "  the title screen's picker and publish it in the README. Check that\n"
            "  OSR_WEB_ADVENTURES_DIR and OSR_WEB_ADVENTURE reached the server process.",
            returncode=1,
        )

    saves = server.get("/api/saves")["saves"]
    if saves:
        labels = [save.get("adventure_name") for save in saves]
        pytest.exit(
            "CAPTURE ABORTED — the server is not saves-isolated.\n"
            f"  GET /api/saves returned {len(saves)} save(s), adventures: {labels}\n"
            "  Expected zero. Each save's adventure name is rendered into the title\n"
            "  screen's 'Resume a saved game' picker, so capturing now would publish\n"
            "  this machine's save list. Check that OSR_WEB_SAVES_DIR reached the server.",
            returncode=1,
        )

    narration = server.new_seeded_game().get("narration", {})
    if narration.get("enabled"):
        pytest.exit(
            "CAPTURE ABORTED — the server reports narration is live.\n"
            f"  A game payload came back with narration={narration}\n"
            "  Capturing now would bake LLM-generated prose into the images, and model\n"
            "  output churns on every recapture. Note that *deleting* OSR_WEB_NARRATOR is\n"
            "  not enough: server/app.py runs load_dotenv(override=False), so an absent\n"
            "  variable is filled in from the repo .env. It must be set to the empty string.",
            returncode=1,
        )

    return {
        "adventures": names,
        "saves": len(saves),
        "narration_enabled": bool(narration.get("enabled")),
    }


@pytest.fixture(scope="session")
def capture_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CaptureServer]:
    """Run one capture-safe osr-web server for the whole capture session.

    Yields:
        The running [`CaptureServer`][tests.screenshots.conftest.CaptureServer].
    """
    root = tmp_path_factory.mktemp("capture-server")
    adventures_dir = root / "adventures"
    saves_dir = root / "saves"
    home_dir = root / "home"
    for directory in (adventures_dir, saves_dir, home_dir):
        directory.mkdir()
    log_path = root / "server.log"

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "server.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=_capture_environment(adventures_dir, saves_dir, home_dir),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        server = CaptureServer(
            base_url=base_url,
            adventures_dir=adventures_dir,
            saves_dir=saves_dir,
            home_dir=home_dir,
            log_path=log_path,
        )
        try:
            _await_ready(base_url, process, log_path)
            evidence = _guard_capture_safety(server)
            print(
                f"capture server ready on {base_url} — "
                f"adventures={evidence['adventures']}, saves={evidence['saves']}, "
                f"narration enabled={evidence['narration_enabled']}"
            )
            yield server
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


@pytest.fixture(scope="session", autouse=True)
def manifest() -> Iterator[Path]:
    """Truncate the run manifest once per capture session.

    Individual shots only ever append (see
    [`record`][tests.screenshots.capture.record]), so a run that dies halfway
    leaves a short manifest and the gate fails on the missing slugs instead of
    passing on the stale PNGs still sitting in `docs/images/`.

    Yields:
        The manifest path.
    """
    MANIFEST.write_text("", encoding="utf-8")
    yield MANIFEST


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """Fix everything about the browser that could move an image between machines.

    Args:
        browser_context_args: pytest-playwright's own defaults.

    Returns:
        The defaults plus a fixed viewport, device scale factor, colour scheme,
        locale, and timezone. `reduced_motion` is explicitly *no-preference*: the
        client skips its beat-paced battle round under a reduce preference, and the
        `combat-round` shot needs the roundbar the pacing puts on screen.
    """
    return {
        **browser_context_args,
        "viewport": dict(VIEWPORT),
        "device_scale_factor": DEVICE_SCALE_FACTOR,
        "color_scheme": "dark",
        "reduced_motion": "no-preference",
        "locale": "en-US",
        "timezone_id": "UTC",
    }


@pytest.fixture
def seeded_game(capture_server: CaptureServer) -> dict:
    """A fresh seed-42 game, created server-side, for one shot.

    Every shot starts from its own `POST /api/games` so the command prefix that
    follows is the only thing the engine's RNG streams have seen — the capture
    adventure's verified route is reproducible only from a fresh game.

    Returns:
        The `POST /api/games` payload.
    """
    return capture_server.new_seeded_game()


@pytest.fixture
def shot_page(page: Page, capture_server: CaptureServer, seeded_game: dict) -> Page:
    """A booted page that has adopted the seeded game, ready to be driven.

    Two init scripts land before any app code runs: `Math.random` is pinned so the
    title screen's random hook stops churning between runs, and
    `localStorage['osrweb_game']` is pre-seeded with the server-side game id, which
    is the exact key `boot()` restores from.

    Returns:
        The page, with `boot()` finished and the play view showing.
    """
    game_id = seeded_game["game_id"]
    page.add_init_script("Math.random = () => 0.5;")
    page.add_init_script(f"try {{ localStorage.setItem('osrweb_game', {json.dumps(game_id)}); }} catch (error) {{}}")
    page.goto(capture_server.base_url, wait_until="domcontentloaded")
    # boot() keeps the overlay hidden on the restore path; that is the signal that
    # the seeded session was adopted. The transcript is legitimately *empty* at
    # this point — a new game opens at turn 0 with no bookkeeping lines — so
    # the render is awaited through the state, not a log entry.
    page.wait_for_selector("#overlay.hidden", state="attached")
    page.wait_for_function("() => S.view !== null")
    return page
