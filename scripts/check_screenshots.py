#!/usr/bin/env python3
"""Staleness gate for the README screenshots.

The gate holds three directions together, over a README that references at least one screenshot
(zero references is itself a failure — see below):

1. Every image `README.md` references exists on disk.
2. Every `docs/images/*.png` on disk is referenced by `README.md` (the orphan direction).
3. The slug set recorded in `.screenshot-manifest` equals the slug set `README.md` references.

Direction 3 is the load-bearing one. Comparing the images on disk against the README is inert:
the capture harness writes into the same directory the committed PNGs already live in, so deleting
or skipping a shot's test leaves the PNG, the README reference, and any disk-based check perfectly
green. The manifest is appended by the harness *as it runs*, so it records what actually executed
this session — which is why a missing manifest is a loud failure rather than "nothing to check".

For the same reason a README that references no screenshots at all fails: all three directions hold
vacuously over an empty set, so stripping the references and deleting the capture tests together
would otherwise sail through CI with the README documenting the app with no pictures in it.

Explicit non-goal: no pixel diffing and no image-content inspection of any kind. Fonts render
differently across machines, so pixel comparison fails on cosmetic noise until everyone learns to
ignore it. This gate proves structural correspondence only.

Run it from anywhere; paths default to the repo root, not the caller's working directory:

```sh
uv run python scripts/check_screenshots.py
```

Exit status is 0 when all three directions hold and non-zero otherwise.
"""

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
"""Repo root, derived from this file's location so the gate ignores the caller's cwd."""

DEFAULT_README = REPO_ROOT / "README.md"
DEFAULT_MANIFEST = REPO_ROOT / ".screenshot-manifest"
DEFAULT_IMAGES_DIR = REPO_ROOT / "docs" / "images"

CAPTURE_COMMAND = "uv run pytest tests/screenshots"
"""The command that (re)captures every shot and rewrites the manifest."""

_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)")
"""Matches `![alt](target)`, tolerating a link title and angle-bracketed targets."""

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"})


def readme_image_targets(text: str) -> list[str]:
    """Pull the image targets out of markdown source, in document order without duplicates.

    Only the markdown image form the contract mandates is recognized: `![alt](docs/images/x.png)`.
    HTML `<img>` tags and reference-style links are deliberately not parsed — the harness and the
    README are required to use the plain form so this gate stays a dozen lines of regex instead of
    a markdown parser.

    Args:
        text: The full markdown source of the README.

    Returns:
        Each image target exactly as written, deduplicated, in the order first seen.

    Examples:
        ```python
        readme_image_targets("![Title screen](docs/images/title-screen.png)")
        # ['docs/images/title-screen.png']
        ```
    """
    targets: list[str] = []
    for match in _MARKDOWN_IMAGE.finditer(text):
        target = match.group(1).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        if target and target not in targets:
            targets.append(target)
    return targets


def read_manifest(path: Path) -> list[str]:
    """Read the run manifest: one slug per line, blank lines ignored.

    Args:
        path: Path to `.screenshot-manifest`.

    Returns:
        The recorded slugs in the order the harness appended them, duplicates preserved.

    Raises:
        FileNotFoundError: If the manifest does not exist. Callers must report this loudly rather
            than swallow it — a missing manifest means capture never ran.
    """
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _is_external(target: str) -> bool:
    """Report whether a markdown target points somewhere the filesystem cannot answer for."""
    return "://" in target or target.startswith(("data:", "mailto:", "#"))


def _looks_like_image(target: str) -> bool:
    """Report whether a markdown target names a file with an image extension."""
    return Path(target).suffix.lower() in _IMAGE_SUFFIXES


def _bullets(items: list[str]) -> str:
    """Render a sorted bullet list for a failure message."""
    return "\n".join(f"    {item}" for item in sorted(items))


def check_screenshots(readme: Path, manifest: Path, images_dir: Path) -> list[str]:
    """Check the three directions and return one message per failure.

    Relative image targets are resolved against the README's own directory, which is the repo root
    in normal use and a `tmp_path` fixture under test.

    Args:
        readme: Path to the README whose image references are authoritative.
        manifest: Path to the run manifest the capture harness appends to.
        images_dir: Directory the capture harness writes `<slug>.png` files into.

    Returns:
        A list of human-readable failure messages, empty when all three directions hold. Each
        message names the direction it belongs to, the offending slugs or paths, and the fix.
    """
    problems: list[str] = []

    if not readme.is_file():
        return [f"README not found at {readme} — the gate has nothing to check against."]

    base = readme.resolve().parent
    images_dir = images_dir.resolve()
    text = readme.read_text(encoding="utf-8")

    referenced: dict[Path, str] = {}
    missing_on_disk: list[str] = []
    for target in readme_image_targets(text):
        if _is_external(target) or not _looks_like_image(target):
            continue
        path = (base / target).resolve()
        referenced.setdefault(path, target)
        if not path.is_file():
            missing_on_disk.append(target)

    readme_slugs = {path.stem for path in referenced if path.parent == images_dir and path.suffix.lower() == ".png"}

    # Precondition: the README is supposed to have screenshots. Zero references is the shape of
    # the feature quietly evaporating — capture tests deleted and README references stripped —
    # under which all three directions below hold vacuously and a green run proves nothing. Only
    # zero trips this; the expected slugs are never enumerated here, so adding or dropping one
    # shot never means editing the gate.
    if not readme_slugs:
        problems.append(
            f"Precondition — {readme.name} references no screenshots at all "
            f"(nothing under {images_dir}):\n"
            "  Zero references is a failure state, not a clean one. This gate exists because the\n"
            "  README is supposed to show the app; no references means the shots — and probably\n"
            "  their capture tests — were dropped, and every direction below then holds vacuously.\n"
            f"  Fix: run the capture harness ({CAPTURE_COMMAND}) and reference each shot from\n"
            "  README.md as ![Alt text](docs/images/<slug>.png)."
        )

    # Direction 1: every referenced image exists on disk.
    if missing_on_disk:
        problems.append(
            "Direction 1 — README.md references images that are not on disk:\n"
            f"{_bullets(missing_on_disk)}\n"
            f"  Fix: capture them ({CAPTURE_COMMAND}) or correct the path in README.md."
        )

    # Direction 2: every PNG on disk is referenced by the README (the orphan direction).
    # A screenshot directory that is not there at all is a failure in its own right: without it
    # the orphan direction has nothing to walk and the slug set collapses to empty, which would
    # let a misdirected --images-dir (or a repo missing docs/images/) pass silently.
    if not images_dir.is_dir():
        problems.append(
            f"Direction 2 — the screenshot directory does not exist: {images_dir}\n"
            "  With no directory to walk there are no shots to compare against, so a pass here\n"
            "  would mean nothing.\n"
            f"  Fix: run the capture harness ({CAPTURE_COMMAND}), or point --images-dir at the "
            "right directory."
        )
    on_disk = sorted(images_dir.glob("*.png")) if images_dir.is_dir() else []
    orphans = [
        str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
        for path in on_disk
        if path.resolve() not in referenced
    ]
    if orphans:
        problems.append(
            "Direction 2 — images on disk that README.md never references (orphans):\n"
            f"{_bullets(orphans)}\n"
            "  Fix: reference each one from README.md as ![Alt text](docs/images/<slug>.png), "
            "or delete the file."
        )

    # Direction 3: the manifest records exactly the slugs the README references.
    try:
        manifest_slugs = set(read_manifest(manifest))
    except FileNotFoundError:
        expected = ", ".join(sorted(readme_slugs)) or "(none — README.md references no screenshots)"
        problems.append(
            f"Direction 3 — the run manifest is missing: {manifest}\n"
            "  The capture harness appends one slug per line the moment each shot is written, so no\n"
            "  manifest means the capture run never happened (or never finished). This cannot be\n"
            "  waved through as 'nothing to check': the committed PNGs are still sitting in\n"
            "  docs/images/, so every disk-based check would pass while the screenshots rot.\n"
            f"  README.md expects these slugs: {expected}\n"
            f"  Fix: run the capture harness ({CAPTURE_COMMAND})."
        )
    else:
        uncaptured = readme_slugs - manifest_slugs
        unreferenced = manifest_slugs - readme_slugs
        if uncaptured or unreferenced:
            lines = [f"Direction 3 — {manifest.name} does not match the slugs README.md references:"]
            if uncaptured:
                lines.append("  Referenced by README.md but not captured this run (test deleted or skipped?):")
                lines.append(_bullets(sorted(uncaptured)))
            if unreferenced:
                lines.append("  Captured this run but never referenced by README.md:")
                lines.append(_bullets(sorted(unreferenced)))
            lines.append(
                f"  Fix: recapture ({CAPTURE_COMMAND}) after adding the missing shot's test, "
                "or update README.md to match the shots the harness actually takes."
            )
            problems.append("\n".join(lines))

    return problems


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and run the argument parser."""
    parser = argparse.ArgumentParser(
        prog="check_screenshots.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Check that the README screenshots, the PNGs on disk, and the capture run manifest all\n"
            "agree with each other.\n"
            "\n"
            "What this gate proves:\n"
            "  0. README.md references at least one screenshot. Zero references is a failure, not\n"
            "     a clean pass: the directions below all hold vacuously over an empty set, so\n"
            "     deleting the capture tests and stripping the README references together would\n"
            "     otherwise go green.\n"
            "  1. Every image README.md references exists on disk.\n"
            "  2. Every docs/images/*.png on disk is referenced by README.md (no orphans).\n"
            "  3. The slugs recorded in .screenshot-manifest are exactly the slugs README.md\n"
            "     references. The harness appends to the manifest as it runs, so this is the only\n"
            "     direction that notices a capture test that was deleted or skipped: the stale PNG\n"
            "     stays on disk and a disk-only check stays green. A missing manifest is a failure,\n"
            "     never a pass.\n"
            "\n"
            "What this gate does NOT prove:\n"
            "  - That a screenshot shows the right thing, or anything at all. There is no pixel\n"
            "    diffing and no image-content inspection: fonts render differently across machines,\n"
            "    so pixel comparison fails on cosmetic noise until people learn to ignore it.\n"
            "  - That the captured UI is correct. It proves the shots were taken this run and that\n"
            "    the README, the directory, and the manifest describe the same set of shots."
        ),
        epilog=f"Exit status: 0 when all three directions hold, 1 otherwise.\nRecapture with: {CAPTURE_COMMAND}",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=DEFAULT_README,
        metavar="PATH",
        help="README whose image references are authoritative (default: %(default)s).",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        metavar="PATH",
        help="Run manifest the capture harness appends slugs to (default: %(default)s).",
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=DEFAULT_IMAGES_DIR,
        metavar="PATH",
        help="Directory holding <slug>.png captures (default: %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the gate.

    Args:
        argv: Command-line arguments, defaulting to `sys.argv[1:]`.

    Returns:
        0 when every direction holds, 1 when any fails.
    """
    args = _parse_args(argv)
    problems = check_screenshots(readme=args.readme, manifest=args.manifest, images_dir=args.images_dir)
    if problems:
        print("Screenshot gate FAILED.\n", file=sys.stderr)
        print("\n\n".join(problems), file=sys.stderr)
        print(
            "\nThe gate checks structure only (references, files, manifest) — it never inspects image content.",
            file=sys.stderr,
        )
        return 1
    print(f"Screenshot gate passed: {args.readme.name}, {args.images_dir}, and {args.manifest.name} agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
