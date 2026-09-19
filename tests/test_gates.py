"""Acceptance tests for the standing gate's configuration (`specs/dev-loop/spec.md`, phases 2 to 4).

These pin what the gate *is*: which tools are declared, how they are configured, and that CI runs
them. They do not run the tools. The verdicts (a clean format check, zero lint findings, zero type
errors, a clean client lint) come from the gate commands themselves, which every chunk's
done-criterion runs and which CI runs on every pull request.

The point of pinning configuration in a test is drift: a rule quietly dropped from the ruff
selection, a suppression key slipped into `[tool.pyright]`, or a CI step deleted would otherwise
leave the tree "green" while the gate meant less than the guide says it does.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
LOCKFILE = REPO_ROOT / "uv.lock"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
AGENTS = REPO_ROOT / "AGENTS.md"
CLIENT_RULE = REPO_ROOT / ".claude" / "rules" / "client.md"
OSRLIB_PYPROJECT = REPO_ROOT.parent / "osrlib-python" / "pyproject.toml"

_DEPENDENCY_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _osrlib_pyproject() -> dict:
    return tomllib.loads(OSRLIB_PYPROJECT.read_text(encoding="utf-8"))


def _dev_dependency_names() -> set[str]:
    names = set()
    for spec in _pyproject()["dependency-groups"]["dev"]:
        match = _DEPENDENCY_NAME.match(spec)
        assert match, spec
        names.add(match.group(1).lower())
    return names


def _ci_run_lines() -> list[str]:
    """Every `run:` command in the workflow, one string per step, block scalars joined."""
    lines = CI.read_text(encoding="utf-8").splitlines()
    commands: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("run:"):
            body = stripped[len("run:") :].strip()
            if body in {"|", ">", "|-", ">-"}:
                indent = len(lines[index]) - len(lines[index].lstrip())
                block: list[str] = []
                index += 1
                while index < len(lines) and (
                    not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip()) > indent
                ):
                    block.append(lines[index].strip())
                    index += 1
                commands.append(" ".join(part for part in block if part))
                continue
            commands.append(body)
        index += 1
    return commands


def _ci_runs(fragment: str) -> bool:
    return any(fragment in command for command in _ci_run_lines())


def _source_files() -> list[Path]:
    return sorted(path for folder in ("server", "scripts") for path in (REPO_ROOT / folder).rglob("*.py"))


class TestRuffGate:
    """Phase 2: ruff is declared, configured to match osrlib, and run by CI over a locked sync."""

    def test_ruff_is_a_dev_dependency_in_the_lockfile(self):
        assert "ruff" in _dev_dependency_names()
        assert 'name = "ruff"' in LOCKFILE.read_text(encoding="utf-8")

    def test_ruff_config_matches_osrlib(self):
        ours = _pyproject()["tool"]["ruff"]
        theirs = _osrlib_pyproject()["tool"]["ruff"]
        assert ours["line-length"] == theirs["line-length"]
        assert set(ours["lint"]["select"]) == set(theirs["lint"]["select"])
        assert ours["lint"]["pydocstyle"]["convention"] == theirs["lint"]["pydocstyle"]["convention"]
        assert ours["lint"]["per-file-ignores"] == {"tests/**": ["D"]}
        assert "ignore" not in ours["lint"], "no standing lint exceptions: a gate lands only on a clean tree"

    def test_ci_syncs_locked_and_runs_format_and_lint(self):
        assert _ci_runs("uv sync --locked")
        assert not any(command == "uv sync" for command in _ci_run_lines()), "an unlocked sync hides lockfile drift"
        assert _ci_runs("uv run ruff format --check")
        assert _ci_runs("uv run ruff check")

    def test_guide_describes_the_real_toolchain(self):
        text = AGENTS.read_text(encoding="utf-8")
        assert "uvx ruff" not in text
        assert "default line length" not in text
        assert "uv run ruff" in text


class TestPyrightGate:
    """Phase 3: pyright in basic mode over server/ and scripts/, no suppressions, run by CI."""

    def test_pyright_is_configured_like_osrlib(self):
        assert "pyright" in _dev_dependency_names()
        config = _pyproject()["tool"]["pyright"]
        assert set(config["include"]) >= {"server", "scripts"}
        assert config["typeCheckingMode"] == "basic"
        assert config["pythonVersion"] == _osrlib_pyproject()["tool"]["pyright"]["pythonVersion"]
        suppressions = [key for key in config if key.startswith("report")]
        assert suppressions == [], f"no per-rule suppressions: {suppressions}"

    def test_no_inline_ignores_in_the_sources(self):
        offenders = [
            f"{path.relative_to(REPO_ROOT)}:{number}"
            for path in _source_files()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if "type: ignore" in line or "pyright: ignore" in line
        ]
        assert offenders == []

    def test_ci_runs_pyright(self):
        assert _ci_runs("uv run pyright")


class TestClientGate:
    """Phase 4: an exact-version client linter that fails on warnings, plus the cache-bust check."""

    def test_ci_lints_the_client_at_an_exact_version_and_denies_warnings(self):
        lint_steps = [command for command in _ci_run_lines() if "oxlint" in command]
        assert len(lint_steps) == 1, lint_steps
        (command,) = lint_steps
        assert re.search(r"oxlint@\d+\.\d+\.\d+(\s|$)", command), "pin oxlint to an exact version"
        assert "--deny-warnings" in command
        assert "static" in command

    def test_no_client_toolchain_is_introduced(self):
        assert not (REPO_ROOT / "package.json").exists()
        assert not (REPO_ROOT / "package-lock.json").exists()
        assert not (REPO_ROOT / "node_modules").exists()

    def test_ci_runs_the_app_version_check(self):
        assert _ci_runs("scripts/check_app_version.py")

    def test_client_rule_names_the_gate(self):
        assert "check_app_version" in CLIENT_RULE.read_text(encoding="utf-8")
