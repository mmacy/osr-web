"""Acceptance tests for the cache-bust gate (`scripts/check_app_version.py`; dev-loop phase 4).

`static/index.html` loads the client as `app.js?v=N`, and the browser caches `app.js` by that URL,
so a change to `app.js` that leaves `N` alone ships a change nobody's browser loads. The gate turns
the rule "bump N whenever you edit app.js" into a check between two git refs.

Every test builds a throwaway git repository under `tmp_path`, so nothing here reads the real
client or the real history.
"""

import subprocess
from pathlib import Path


def _gate():
    """Import the gate inside each test so a missing script fails the test, not collection."""
    from scripts import check_app_version as module

    return module


INDEX = "static/index.html"
APP = "static/app.js"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=gate", "-c", "user.email=gate@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _index_html(version: int | None) -> str:
    script = f'<script src="app.js?v={version}"></script>' if version is not None else '<script src="app.js"></script>'
    return f"<!doctype html>\n<html><body>\n{script}\n</body></html>\n"


def build_repo(tmp_path: Path, *, version: int = 1, app_js: str = "console.log('one');\n") -> Path:
    """Initialise a repository with one commit holding the client files, and return its root."""
    repo = tmp_path / "repo"
    (repo / "static").mkdir(parents=True)
    (repo / INDEX).write_text(_index_html(version), encoding="utf-8")
    (repo / APP).write_text(app_js, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "tag", "base")
    return repo


def commit(repo: Path, *, version: int | None = None, app_js: str | None = None, message: str = "change") -> None:
    """Commit a change to the client files on top of the current head."""
    if version is not None or app_js is None:
        (repo / INDEX).write_text(_index_html(version if version is not None else 1), encoding="utf-8")
    if app_js is not None:
        (repo / APP).write_text(app_js, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)


class TestAppVersionParsing:
    def test_reads_the_version_from_the_script_tag(self):
        assert _gate().app_version(_index_html(42)) == 42

    def test_no_versioned_reference_is_none(self):
        assert _gate().app_version(_index_html(None)) is None
        assert _gate().app_version("<html></html>") is None


class TestCheckAppVersion:
    def test_untouched_client_passes(self, tmp_path):
        repo = build_repo(tmp_path)
        (repo / "README.md").write_text("docs only\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "docs")
        assert _gate().check_app_version(repo, base="base") == []

    def test_changed_app_with_bumped_version_passes(self, tmp_path):
        repo = build_repo(tmp_path)
        commit(repo, version=2, app_js="console.log('two');\n")
        assert _gate().check_app_version(repo, base="base") == []

    def test_changed_app_with_same_version_fails_naming_the_fix(self, tmp_path):
        repo = build_repo(tmp_path, version=7)
        commit(repo, app_js="console.log('two');\n")
        problems = _gate().check_app_version(repo, base="base")
        assert len(problems) == 1
        assert INDEX in problems[0]
        assert "v=7" in problems[0]

    def test_changed_app_with_reference_removed_fails(self, tmp_path):
        repo = build_repo(tmp_path)
        (repo / INDEX).write_text(_index_html(None), encoding="utf-8")
        (repo / APP).write_text("console.log('two');\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "drop the version")
        problems = _gate().check_app_version(repo, base="base")
        assert len(problems) == 1
        assert INDEX in problems[0]

    def test_head_defaults_to_the_working_head_and_can_be_named(self, tmp_path):
        repo = build_repo(tmp_path)
        commit(repo, version=2, app_js="console.log('two');\n")
        _git(repo, "tag", "bumped")
        commit(repo, app_js="console.log('three');\n", message="forgot the bump")
        assert _gate().check_app_version(repo, base="base", head="bumped") == []
        assert _gate().check_app_version(repo, base="bumped") != []
        # Over the whole range the bump covers the later edit: a pull request merges as one
        # change, so the version that reaches main differs from the one browsers have cached.
        assert _gate().check_app_version(repo, base="base") == []

    def test_edit_reverted_within_the_range_passes(self, tmp_path):
        repo = build_repo(tmp_path)
        commit(repo, app_js="console.log('two');\n", message="edit")
        commit(repo, app_js="console.log('one');\n", message="revert")
        assert _gate().check_app_version(repo, base="base") == []


class TestMain:
    def test_exit_status_follows_the_verdict(self, tmp_path, capsys):
        repo = build_repo(tmp_path, version=3)
        commit(repo, app_js="console.log('two');\n")
        assert _gate().main(["--repo", str(repo), "--base", "base"]) == 1
        assert INDEX in capsys.readouterr().err
        commit(repo, version=4)
        assert _gate().main(["--repo", str(repo), "--base", "base"]) == 0

    def test_unknown_base_ref_is_a_reported_failure_not_a_traceback(self, tmp_path, capsys):
        repo = build_repo(tmp_path)
        assert _gate().main(["--repo", str(repo), "--base", "no-such-ref"]) != 0
        assert "no-such-ref" in capsys.readouterr().err
