#!/usr/bin/env python3
"""Cache-bust gate for the client.

`static/index.html` loads the client as `app.js?v=N`, and the browser caches the script by that
URL. A commit that edits `static/app.js` and leaves `N` alone therefore ships a change that no
returning player's browser loads, with no error anywhere: the page works, it just runs yesterday's
code. "Bump N whenever you edit app.js" is the first entry under Client gotchas in
`.claude/rules/client.md`, and until this gate existed nothing observed it.

The gate compares two git refs and states one thing:

> If `static/app.js` differs between the two refs, the `v=N` query in `static/index.html` must
> differ too.

The unit is the range, not the commit, because a pull request merges as one change: what reaches a
player's browser is the difference between the base branch and the merged result. So a bump made
anywhere in the range covers every client edit in it, and a client edited and then reverted needs no
bump at all.

Everything is read through git (`git diff` and `git show`) with `cwd` set to the repository under
test, so the verdict depends on the named refs alone and never on what the working tree happens to
hold. That is what lets CI run it over a pull request's real range.

What this gate does NOT prove:

- That the new `N` is *right*. Any different value passes, because any different value is a
  different URL, which is all the browser cares about.
- That the change to `app.js` is correct, or that it loads at all. That is the client linter's
  job (`npx oxlint`) and the reader's.
- Anything about `style.css` or `index.html` itself. Neither is loaded through a versioned URL,
  and the HTML is revalidated on every load.

Run it against whatever a pull request would merge into:

```sh
uv run python scripts/check_app_version.py --base origin/main
```

Exit status is 0 when the rule holds, 1 when it is broken, and 2 when git could not answer (an
unknown ref, or a directory that is not a repository).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
"""Repo root, derived from this file's location so the gate ignores the caller's cwd."""

INDEX_HTML = "static/index.html"
"""The page that loads the client, relative to the repository root, as git spells it."""

APP_JS = "static/app.js"
"""The client itself, relative to the repository root, as git spells it."""

_APP_VERSION = re.compile(r"app\.js\?v=(\d+)")
"""Matches the cache-bust query in `<script src="app.js?v=42"></script>`."""

_GIT_FAILED = 2
"""Exit status for "the gate could not run", kept distinct from 1, "the rule is broken"."""


class GitError(RuntimeError):
    """A git command the gate depends on did not succeed.

    Carries git's own message, so an unknown ref reports as the operator's mistake rather than as
    a traceback out of `subprocess`.
    """


def app_version(index_html: str) -> int | None:
    """Read the client's cache-bust number out of the page that loads it.

    Args:
        index_html: The full source of `static/index.html`.

    Returns:
        The `N` of the first `app.js?v=N` reference, or `None` when the page loads the client
        without a versioned query (or does not load it at all).

    Examples:
        ```python
        app_version('<script src="app.js?v=42"></script>')
        # 42
        ```
    """
    match = _APP_VERSION.search(index_html)
    return int(match.group(1)) if match else None


def _git(repo: Path, *args: str, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    """Run git in `repo` and return the finished process, raising `GitError` on an unexpected code.

    Args:
        repo: Repository to run in; passed as git's working directory.
        *args: The git arguments, without the leading `git`.
        ok_codes: Exit statuses that count as an answer rather than a failure. `git diff --quiet`
            reports its verdict as 0 or 1, so callers widen this rather than test the code twice.

    Returns:
        The completed process, with stdout and stderr captured as text.

    Raises:
        GitError: If git exited with a status outside `ok_codes`.
    """
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if result.returncode not in ok_codes:
        detail = result.stderr.strip() or result.stdout.strip() or f"git exited {result.returncode}"
        raise GitError(f"`git {' '.join(args)}` failed in {repo}: {detail}")
    return result


def _resolve(repo: Path, ref: str) -> str:
    """Resolve a ref to the commit it names, so an unknown one is reported before anything else.

    Args:
        repo: Repository the ref belongs to.
        ref: A branch, tag, or commit id.

    Returns:
        The full commit id.

    Raises:
        GitError: If git cannot resolve the ref. The message names the ref, because the usual cause
            is a shallow CI checkout that never fetched it.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "no such revision"
        raise GitError(
            f"cannot resolve the git ref {ref!r} in {repo}: {detail}\n"
            "  A shallow checkout is the usual cause: fetch the ref before comparing against it."
        )
    return result.stdout.strip()


def _index_at(repo: Path, ref: str) -> str | None:
    """Read `static/index.html` as of a ref, or `None` when the file does not exist there."""
    result = _git(repo, "show", f"{ref}:{INDEX_HTML}", ok_codes=(0, 128))
    return None if result.returncode else result.stdout


def _version_at(repo: Path, ref: str) -> int | None:
    """Read the cache-bust number as of a ref, treating a missing page as no version."""
    html = _index_at(repo, ref)
    return app_version(html) if html is not None else None


def _client_changed(repo: Path, base: str, head: str) -> bool:
    """Report whether `static/app.js` differs between two refs."""
    return bool(_git(repo, "diff", "--quiet", base, head, "--", APP_JS, ok_codes=(0, 1)).returncode)


def check_app_version(repo: Path, base: str, head: str = "HEAD") -> list[str]:
    """Check the cache-bust rule over a range and return one message per failure.

    A client that is byte-identical at the two refs needs no bump, however it was edited in
    between. Otherwise the `v=N` query at `head` must differ from the one at `base`.

    Args:
        repo: The repository to read both refs out of. Nothing is read from its working tree.
        base: The ref the change is measured against, such as `origin/main`.
        head: The ref holding the change. Defaults to the repository's current head.

    Returns:
        A list of human-readable failure messages, empty when the rule holds. A message names
        `static/index.html`, the version that stood still, and the fix.

    Raises:
        GitError: If either ref cannot be resolved, or git otherwise fails.
    """
    _resolve(repo, base)
    _resolve(repo, head)

    if not _client_changed(repo, base, head):
        return []

    before = _version_at(repo, base)
    after = _version_at(repo, head)

    if after is None:
        return [
            f"{APP_JS} differs between {base} and {head}, and at {head} nothing loads it as\n"
            "  app.js?v=N any more.\n"
            "  Without the query the browser caches the script under one URL for good, which is\n"
            "  the defect this gate exists to catch.\n"
            f'  Fix: reference it in {INDEX_HTML} as <script src="app.js?v=N"></script>, with an N\n'
            "  no browser has seen."
        ]

    if after == before:
        return [
            f"{APP_JS} differs between {base} and {head}, and {INDEX_HTML} still loads it as\n"
            f"  app.js?v={after}.\n"
            "  The browser caches app.js by that URL, so every player who has loaded the page\n"
            "  before keeps running the old script, with no error to show for it.\n"
            f"  Fix: bump the query in {INDEX_HTML} to app.js?v={after + 1}."
        ]

    return []


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build and run the argument parser."""
    parser = argparse.ArgumentParser(
        prog="check_app_version.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Check that a change to static/app.js came with a bump to the app.js?v=N query in\n"
            "static/index.html.\n"
            "\n"
            "What this gate proves:\n"
            "  When static/app.js differs between two git refs, the version query in\n"
            "  static/index.html differs too — so the browser, which caches the script by that\n"
            "  URL, is asked for a file it has never seen. The unit is the range, because a pull\n"
            "  request merges as one change.\n"
            "\n"
            "What this gate does NOT prove:\n"
            "  - That the new N is the right number. Any different value is a different URL,\n"
            "    which is all the cache reads.\n"
            "  - That the change to app.js is correct or even parses. That is the client\n"
            "    linter's job."
        ),
        epilog=(
            "Exit status: 0 when the rule holds, 1 when it is broken, 2 when git could not answer.\n"
            "Typical use: uv run python scripts/check_app_version.py --base origin/main"
        ),
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        metavar="PATH",
        help="Repository to read both refs out of (default: %(default)s).",
    )
    parser.add_argument(
        "--base",
        required=True,
        metavar="REF",
        help="The ref the change is measured against, such as origin/main.",
    )
    parser.add_argument(
        "--head",
        default="HEAD",
        metavar="REF",
        help="The ref holding the change (default: %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the gate.

    Args:
        argv: Command-line arguments, defaulting to `sys.argv[1:]`.

    Returns:
        0 when the rule holds, 1 when a change to the client shipped without a version bump, and
        2 when git could not answer — an unresolvable ref is an operator's mistake, not a verdict
        about the client.
    """
    args = _parse_args(argv)
    try:
        problems = check_app_version(args.repo, base=args.base, head=args.head)
    except GitError as error:
        print(f"App version gate could not run.\n\n{error}", file=sys.stderr)
        return _GIT_FAILED
    if problems:
        print("App version gate FAILED.\n", file=sys.stderr)
        print("\n\n".join(problems), file=sys.stderr)
        return 1
    print(
        f"App version gate passed over {args.base}..{args.head}: "
        f"{APP_JS} and the app.js?v=N query in {INDEX_HTML} agree."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
