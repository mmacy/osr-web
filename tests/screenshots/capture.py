"""The capture primitive: one place for every rule a documentation shot obeys.

Every shot in `tests/screenshots/test_shots.py` goes through
[`capture`][tests.screenshots.capture.capture], which is the only code in the repo
allowed to write a `docs/images/*.png` or to touch `.screenshot-manifest`. It:

1. Freezes motion — CSS animations and transitions to zero, the text caret to
   transparent — so a shot never catches a half-played transition.
2. Waits for `document.fonts.ready`, because a canvas drawn before the webfonts
   land measures its text against the fallback face.
3. Settles every visible `<canvas>` (the 920x518 `#viewport`, and `#automap` when
   the map overlay is open) by hashing its pixels once per animation frame until
   the hash stops changing. A bare timeout would pass on a canvas that is still
   mid-draw and fail on a slow machine; a stable frame is the actual question.
4. Writes `docs/images/<slug>.png`.
5. **Appends** the slug to `.screenshot-manifest` the moment the file exists.

Step 5 is what makes the gate (`scripts/check_screenshots.py`) load-bearing: the
manifest records what actually ran this session, so a deleted or skipped shot is
caught even though its stale PNG is still sitting on disk. The manifest is
truncated exactly once per session, by the `manifest` fixture in `conftest.py` —
never here — so a run that dies halfway leaves a half-written manifest and the
gate notices.
"""

from pathlib import Path

from playwright.sync_api import Page

REPO_ROOT = Path(__file__).resolve().parents[2]
"""Repo root, derived from this file's location rather than the caller's cwd."""

IMAGES_DIR = REPO_ROOT / "docs" / "images"
"""Where captured shots are written, one `<slug>.png` per shot."""

MANIFEST = REPO_ROOT / ".screenshot-manifest"
"""The run manifest the gate reads: one slug per line, LF, trailing newline."""

CAPTURE_ADVENTURE = REPO_ROOT / "tests" / "assets" / "capture-adventure" / "adventure.json"
"""The original adventure document every capture plays (never the bundled retail one)."""

SHOT_SLUGS = (
    "adventure-library",
    "play-view",
    "character-sheet",
    "automap",
    "combat-round",
    "party-builder",
    "victory-screen",
)
"""The canonical shot set. `capture` refuses any slug that is not in here."""

MIN_PNG_BYTES = 10_000
"""Floor for a plausible shot. A dark 1440x900@2x UI capture runs into six figures;
anything under this is a blank or unsettled canvas, not a screenshot."""

_STILL_STYLE_ID = "osr-capture-still"

_STILL_CSS = """
*, *::before, *::after {
  animation-delay: 0s !important;
  animation-duration: 0s !important;
  animation-iteration-count: 1 !important;
  transition-delay: 0s !important;
  transition-duration: 0s !important;
  caret-color: transparent !important;
  scroll-behavior: auto !important;
}
"""

_FREEZE_MOTION_JS = """
(payload) => {
  const { id, css } = payload;
  if (document.getElementById(id)) return false;
  const style = document.createElement('style');
  style.id = id;
  style.textContent = css;
  document.head.appendChild(style);
  return true;
}
"""

_SETTLE_JS = """
async (payload) => {
  const { stableFrames, timeoutMs, sampleStride } = payload;
  await document.fonts.ready;

  const visible = [...document.querySelectorAll('canvas')].filter((canvas) => {
    const box = canvas.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && canvas.width > 0 && canvas.height > 0;
  });

  // FNV-1a over a strided sample of the pixel bytes: cheap enough to run every
  // frame, wide enough that a redraw anywhere in the canvas moves the digest.
  const digest = (canvas) => {
    const ctx = canvas.getContext('2d');
    if (!ctx) return '-';
    const bytes = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let hash = 2166136261;
    for (let i = 0; i < bytes.length; i += sampleStride) {
      hash ^= bytes[i];
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16);
  };
  const snapshot = () => visible.map((c) => `${c.id}:${digest(c)}`).join('|');
  const nextFrame = () => new Promise((done) => requestAnimationFrame(() => done()));

  const started = performance.now();
  let previous = snapshot();
  let steady = 0;
  let frames = 0;
  while (performance.now() - started < timeoutMs) {
    await nextFrame();
    frames += 1;
    const current = snapshot();
    steady = current === previous ? steady + 1 : 0;
    previous = current;
    if (steady >= stableFrames) {
      return {
        canvases: visible.map((c) => c.id),
        frames,
        ms: Math.round(performance.now() - started),
      };
    }
  }
  throw new Error(
    `canvas never held still for ${stableFrames} frames within ${timeoutMs}ms ` +
    `(canvases: ${visible.map((c) => c.id).join(', ') || 'none'})`
  );
}
"""


_INK_JS = """
(payload) => {
  const { canvasId, sampleStride } = payload;
  const canvas = document.getElementById(canvasId);
  if (!canvas) throw new Error(`no canvas #${canvasId}`);
  const ctx = canvas.getContext('2d');
  const bytes = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
  // The ground colour is whatever the canvas painted in its top-left corner.
  const ground = [bytes[0], bytes[1], bytes[2]];
  let inked = 0;
  let sampled = 0;
  for (let i = 0; i < bytes.length; i += 4 * sampleStride) {
    sampled += 1;
    if (
      Math.abs(bytes[i] - ground[0]) > 6 ||
      Math.abs(bytes[i + 1] - ground[1]) > 6 ||
      Math.abs(bytes[i + 2] - ground[2]) > 6
    ) {
      inked += 1;
    }
  }
  return sampled ? inked / sampled : 0;
}
"""


def canvas_ink_ratio(page: Page, canvas_id: str, *, sample_stride: int = 7) -> float:
    """Fraction of a canvas's sampled pixels that differ from its own ground colour.

    Evidence that a canvas actually drew something. A blank canvas — the classic
    failure when a shot fires before the app has rendered — comes back at 0.0.

    Args:
        page: The page holding the canvas.
        canvas_id: The canvas element's id, for example `viewport` or `automap`.
        sample_stride: Sample every Nth pixel.

    Returns:
        A ratio between 0.0 (uniformly blank) and 1.0.
    """
    return float(page.evaluate(_INK_JS, {"canvasId": canvas_id, "sampleStride": sample_stride}))


def freeze_motion(page: Page) -> None:
    """Pin the page still: no animation, no transition, no blinking caret.

    Idempotent — the style element is injected at most once per document.
    [`capture`][tests.screenshots.capture.capture] calls this itself, so a test
    never has to.

    Args:
        page: The page about to be photographed.
    """
    page.evaluate(_FREEZE_MOTION_JS, {"id": _STILL_STYLE_ID, "css": _STILL_CSS})


def settle_canvases(
    page: Page,
    *,
    stable_frames: int = 3,
    timeout_ms: int = 5_000,
    sample_stride: int = 401,
) -> dict:
    """Block until every visible canvas has drawn the same pixels several frames running.

    Fonts are awaited first: a canvas that draws text before the webfonts resolve
    paints the fallback face and then repaints, which is exactly the churn this
    guards against.

    Args:
        page: The page to settle.
        stable_frames: Consecutive identical frames required before the canvas counts
            as still.
        timeout_ms: Give up (loudly) after this long.
        sample_stride: Byte stride through the pixel buffer when hashing. Prime, so
            the sample does not align with the 4-byte RGBA pitch.

    Returns:
        A small report: the ids of the canvases watched, how many frames were
        inspected, and how long it took.

    Raises:
        Error: If no canvas ever holds still within `timeout_ms`.
    """
    return page.evaluate(
        _SETTLE_JS,
        {
            "stableFrames": stable_frames,
            "timeoutMs": timeout_ms,
            "sampleStride": sample_stride,
        },
    )


def record(slug: str) -> None:
    """Append one slug to the run manifest.

    Appending (rather than rewriting at the end) is the point: the manifest then
    describes the shots that actually ran, so a partial run leaves a partial
    manifest and the gate fails instead of waving the stale PNGs through.

    Args:
        slug: The shot slug just written to disk.
    """
    with MANIFEST.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{slug}\n")


def capture(page: Page, slug: str) -> Path:
    """Photograph the page as `docs/images/<slug>.png` and record the slug.

    The only sanctioned way to write a documentation shot. Callers assert the UI
    state they are claiming *before* calling this; nothing here inspects what is on
    screen beyond refusing an implausibly small file.

    Args:
        page: A page already driven into the state the slug claims.
        slug: One of [`SHOT_SLUGS`][tests.screenshots.capture.SHOT_SLUGS].

    Returns:
        The path written.

    Raises:
        ValueError: If `slug` is not part of the canonical shot set.
        AssertionError: If the PNG comes out too small to be a real capture.

    Examples:
        ```python
        expect(page.locator("#roundbar")).not_to_have_class(re.compile("hidden"))
        capture(page, "combat-round")
        ```
    """
    if slug not in SHOT_SLUGS:
        raise ValueError(f"{slug!r} is not one of the canonical shots: {', '.join(SHOT_SLUGS)}")

    freeze_motion(page)
    settle_canvases(page)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGES_DIR / f"{slug}.png"
    page.screenshot(path=path, animations="disabled", caret="hide", scale="device")

    size = path.stat().st_size
    assert size >= MIN_PNG_BYTES, (
        f"{path} is only {size} bytes — that is a blank or unsettled canvas, not a screenshot."
    )
    record(slug)
    return path
