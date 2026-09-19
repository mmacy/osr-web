"""Unit tests for the screenshot staleness gate (`scripts/check_screenshots.py`).

Every test builds a throwaway repo under `tmp_path` — README, `docs/images/`, and the run
manifest — so nothing here depends on the real README or on captures having been taken.

The scenario that matters most is [`test_deleted_capture_test_fails_though_disk_looks_fine`]
[tests.test_check_screenshots.TestDirectionThreeManifest.test_deleted_capture_test_fails_though_disk_looks_fine]:
PNG on disk, README reference present, slug absent from the manifest. A disk-based check calls
that green; the gate must not.
"""

from pathlib import Path

import pytest

from scripts.check_screenshots import (
    check_screenshots,
    main,
    read_manifest,
    readme_image_targets,
)

SLUGS = (
    "title-screen",
    "play-view",
    "character-sheet",
    "automap",
    "combat-round",
    "party-builder",
)


def build_repo(
    tmp_path: Path,
    *,
    readme_slugs: tuple[str, ...] = SLUGS,
    disk_slugs: tuple[str, ...] | None = None,
    manifest_slugs: tuple[str, ...] | None = None,
    write_manifest: bool = True,
    readme_extra: str = "",
) -> Path:
    """Lay out a synthetic repo and return its root.

    Args:
        tmp_path: The pytest temp directory to build in.
        readme_slugs: Slugs the README references as `docs/images/<slug>.png`.
        disk_slugs: Slugs written as PNG files; defaults to `readme_slugs`.
        manifest_slugs: Slugs recorded in the manifest; defaults to `readme_slugs`.
        write_manifest: When False, no manifest file is written at all.
        readme_extra: Extra markdown appended to the README.

    Returns:
        The root of the synthetic repo.
    """
    disk = readme_slugs if disk_slugs is None else disk_slugs
    recorded = readme_slugs if manifest_slugs is None else manifest_slugs

    images_dir = tmp_path / "docs" / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for slug in disk:
        (images_dir / f"{slug}.png").write_bytes(b"\x89PNG\r\n\x1a\n not a real image")

    body = ["# osr-web", ""]
    body += [f"![{slug.replace('-', ' ').capitalize()}](docs/images/{slug}.png)\n" for slug in readme_slugs]
    if readme_extra:
        body.append(readme_extra)
    (tmp_path / "README.md").write_text("\n".join(body), encoding="utf-8")

    if write_manifest:
        text = "".join(f"{slug}\n" for slug in recorded)
        (tmp_path / ".screenshot-manifest").write_text(text, encoding="utf-8")

    return tmp_path


def gate_args(root: Path) -> list[str]:
    """Build the CLI overrides that point the gate at a synthetic repo."""
    return [
        "--readme",
        str(root / "README.md"),
        "--manifest",
        str(root / ".screenshot-manifest"),
        "--images-dir",
        str(root / "docs" / "images"),
    ]


def run_gate(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the gate against a synthetic repo.

    Returns:
        The exit status and everything the gate printed on stdout and stderr.
    """
    status = main(gate_args(root))
    captured = capsys.readouterr()
    return status, captured.out + captured.err


class TestReadmeParsing:
    def test_plain_image_reference(self):
        assert readme_image_targets("![Automap](docs/images/automap.png)") == ["docs/images/automap.png"]

    def test_link_title_and_angle_brackets(self):
        text = '![A](docs/images/a.png "Title")\n![B](<docs/images/b.png>)'
        assert readme_image_targets(text) == ["docs/images/a.png", "docs/images/b.png"]

    def test_non_image_links_are_ignored(self):
        text = "[Not an image](docs/images/nope.png) and [docs](AGENTS.md)"
        assert readme_image_targets(text) == []

    def test_duplicates_collapse_in_document_order(self):
        text = "![B](docs/images/b.png)\n![A](docs/images/a.png)\n![B again](docs/images/b.png)"
        assert readme_image_targets(text) == ["docs/images/b.png", "docs/images/a.png"]


class TestAllGreen:
    def test_passes(self, tmp_path, capsys):
        root = build_repo(tmp_path)
        status, output = run_gate(root, capsys)
        assert status == 0, output
        assert "passed" in output

    def test_manifest_order_and_blank_lines_do_not_matter(self, tmp_path, capsys):
        root = build_repo(tmp_path, manifest_slugs=tuple(reversed(SLUGS)))
        (root / ".screenshot-manifest").write_text(
            "\n".join(["", *reversed(SLUGS), "", "  play-view  ", ""]),
            encoding="utf-8",
        )
        status, output = run_gate(root, capsys)
        assert status == 0, output

    def test_external_image_urls_are_not_checked_on_disk(self, tmp_path, capsys):
        root = build_repo(tmp_path, readme_extra="![Badge](https://example.invalid/badge.svg)\n")
        status, output = run_gate(root, capsys)
        assert status == 0, output


class TestZeroReferencesGuard:
    """The README is supposed to have screenshots; zero references is not a clean state."""

    def test_all_empty_state_fails(self, tmp_path, capsys):
        """Empty README, empty docs/images/, empty manifest: every direction holds vacuously."""
        root = build_repo(tmp_path, readme_slugs=(), disk_slugs=(), manifest_slugs=())
        assert list((root / "docs" / "images").iterdir()) == []
        assert (root / ".screenshot-manifest").read_text(encoding="utf-8") == ""

        status, output = run_gate(root, capsys)
        assert status != 0, "a README stripped of every screenshot must not pass"
        assert "references no screenshots" in output
        assert "Direction 1" not in output
        assert "Direction 2" not in output
        assert "Direction 3" not in output

    def test_a_single_reference_is_enough_to_clear_the_guard(self, tmp_path, capsys):
        root = build_repo(tmp_path, readme_slugs=("title-screen",))
        status, output = run_gate(root, capsys)
        assert status == 0, output
        assert "references no screenshots" not in output

    def test_guard_does_not_fire_when_the_readme_has_references(self, tmp_path, capsys):
        """The empty-manifest case must keep reporting as direction 3, not as the guard."""
        root = build_repo(tmp_path, manifest_slugs=())
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "Direction 3" in output
        assert "references no screenshots" not in output

    def test_guard_names_no_slug_count_or_slug_list(self, tmp_path):
        """Adding or dropping a shot must never mean editing the gate."""
        root = build_repo(tmp_path, readme_slugs=(), disk_slugs=(), manifest_slugs=())
        problems = check_screenshots(
            readme=root / "README.md",
            manifest=root / ".screenshot-manifest",
            images_dir=root / "docs" / "images",
        )
        assert len(problems) == 1
        guard = problems[0]
        assert "references no screenshots" in guard
        for slug in SLUGS:
            assert slug not in guard
        assert "six" not in guard.lower()


class TestDirectionOneMissingFile:
    def test_readme_reference_without_a_file_fails(self, tmp_path, capsys):
        root = build_repo(tmp_path, disk_slugs=tuple(s for s in SLUGS if s != "automap"))
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "Direction 1" in output
        assert "docs/images/automap.png" in output

    def test_returns_a_direction_one_problem(self, tmp_path):
        root = build_repo(tmp_path, disk_slugs=())
        problems = check_screenshots(
            readme=root / "README.md",
            manifest=root / ".screenshot-manifest",
            images_dir=root / "docs" / "images",
        )
        assert len(problems) == 1
        assert problems[0].startswith("Direction 1")
        for slug in SLUGS:
            assert f"docs/images/{slug}.png" in problems[0]


class TestDirectionTwoOrphans:
    def test_orphan_png_fails(self, tmp_path, capsys):
        root = build_repo(tmp_path, disk_slugs=SLUGS + ("abandoned-shot",))
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "Direction 2" in output
        assert "abandoned-shot" in output

    def test_referenced_shots_are_not_reported_as_orphans(self, tmp_path, capsys):
        root = build_repo(tmp_path, disk_slugs=SLUGS + ("abandoned-shot",))
        _, output = run_gate(root, capsys)
        orphan_block = next(block for block in output.split("\n\n") if "Direction 2" in block)
        assert "title-screen" not in orphan_block

    def test_absent_screenshot_directory_fails(self, tmp_path, capsys):
        """An images dir that is not there must not collapse into a silent pass."""
        root = build_repo(tmp_path, readme_slugs=(), manifest_slugs=())
        for png in (root / "docs" / "images").iterdir():
            png.unlink()
        (root / "docs" / "images").rmdir()
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "Direction 2" in output
        assert "screenshot directory does not exist" in output


class TestDirectionThreeManifest:
    def test_manifest_missing_a_referenced_slug_fails(self, tmp_path, capsys):
        root = build_repo(tmp_path, manifest_slugs=tuple(s for s in SLUGS if s != "combat-round"))
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "Direction 3" in output
        assert "combat-round" in output

    def test_manifest_with_an_unreferenced_slug_fails(self, tmp_path, capsys):
        root = build_repo(tmp_path, manifest_slugs=SLUGS + ("ghost-shot",))
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "Direction 3" in output
        assert "ghost-shot" in output
        assert "never referenced by README.md" in output

    def test_missing_manifest_file_fails(self, tmp_path, capsys):
        """The regression that matters most: no manifest means capture never ran."""
        root = build_repo(tmp_path, write_manifest=False)
        assert not (root / ".screenshot-manifest").exists()
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "Direction 3" in output
        assert ".screenshot-manifest" in output
        assert "capture run never happened" in output
        for slug in SLUGS:
            assert slug in output

    def test_missing_manifest_fails_even_with_a_readme_that_references_nothing(self, tmp_path, capsys):
        root = build_repo(tmp_path, readme_slugs=(), write_manifest=False)
        status, output = run_gate(root, capsys)
        assert status != 0, "a missing manifest must never be treated as 'nothing to check'"
        assert "Direction 3" in output

    def test_empty_manifest_fails(self, tmp_path, capsys):
        root = build_repo(tmp_path, manifest_slugs=())
        assert (root / ".screenshot-manifest").read_text(encoding="utf-8") == ""
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "Direction 3" in output
        for slug in SLUGS:
            assert slug in output

    def test_deleted_capture_test_fails_though_disk_looks_fine(self, tmp_path, capsys):
        """PNG on disk + README reference present + slug absent from the manifest.

        This is the exact shape of a deleted or skipped capture test, and the reason direction 3
        exists: a naive disk-based check finds the file exactly where it expects it and passes.
        """
        root = build_repo(tmp_path, manifest_slugs=tuple(s for s in SLUGS if s != "automap"))

        # What a naive disk-based check would see: file present, reference present. All green.
        assert (root / "docs" / "images" / "automap.png").is_file()
        assert "docs/images/automap.png" in (root / "README.md").read_text(encoding="utf-8")

        status, output = run_gate(root, capsys)
        assert status != 0, "the stale-screenshot case must fail, not pass"
        assert "Direction 1" not in output, "direction 1 is satisfied here — only direction 3 catches this"
        assert "Direction 2" not in output, "direction 2 is satisfied here — only direction 3 catches this"
        assert "Direction 3" in output
        assert "automap" in output
        assert "deleted or skipped" in output


class TestManifestReader:
    def test_reads_slugs_in_append_order(self, tmp_path):
        path = tmp_path / ".screenshot-manifest"
        path.write_text("title-screen\nplay-view\n", encoding="utf-8")
        assert read_manifest(path) == ["title-screen", "play-view"]

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_manifest(tmp_path / ".screenshot-manifest")


class TestMisc:
    def test_missing_readme_is_reported(self, tmp_path, capsys):
        root = build_repo(tmp_path)
        (root / "README.md").unlink()
        status, output = run_gate(root, capsys)
        assert status != 0
        assert "README not found" in output

    def test_help_explains_the_non_goal(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0
        text = " ".join(capsys.readouterr().out.split())  # the help text hard-wraps
        assert "no pixel diffing" in text
        assert "A missing manifest is a failure, never a pass" in text

    def test_failure_output_disclaims_image_inspection(self, tmp_path, capsys):
        root = build_repo(tmp_path, write_manifest=False)
        _, output = run_gate(root, capsys)
        assert "never inspects image content" in output
