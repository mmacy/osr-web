"""Unit tests for adventure deletion (`DELETE /api/adventures/{id}`).

Deletion is the one route that removes content from the server's own disk, so
its fence matters more than its convenience: only the drop directory is ever
written to, and only the item that put an entry in the library is removed. The
bundled document and anything reached through `OSR_WEB_ADVENTURES` are files the
operator pointed the server at — they list, they play, and a delete against them
is a 422.

The three shapes a drop-directory item takes are deleted as what they are, which
is what these tests pin hardest: a symlinked forge run unlinks and leaves the run
whole, a dropped directory goes with everything in it, and a `.json` file goes
alone.

Every test redirects the drop directory at a `tmp_path` with
`OSR_WEB_ADVENTURES_DIR` and clears `library._meta_cache` around itself, the same
way `tests/test_library_upload.py` and `tests/test_paths.py` do.
"""

import json

import pytest
from fastapi import HTTPException

from server import library
from server.app import delete_adventure
from server.library import _CONTENT_DIR, list_adventures

_BUNDLED_DOC = _CONTENT_DIR / "adventure.json"


def _document(name: str) -> bytes:
    """The bundled document re-stamped with a different adventure name."""
    document = json.loads(_BUNDLED_DOC.read_bytes())
    document["payload"]["name"] = name
    return json.dumps(document).encode("utf-8")


def _entry(adventure_id: str):
    return next((e for e in list_adventures() if e.id == adventure_id), None)


@pytest.fixture(autouse=True)
def _isolated_drop_dir(monkeypatch, tmp_path):
    """Run every test against an empty temp drop directory and a fresh cache."""
    drop = tmp_path / "drop"
    drop.mkdir()
    for name in ("OSR_WEB_ADVENTURE", "OSR_WEB_ADVENTURES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(drop))
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


class TestWhatIsRemovable:
    def test_the_bundled_document_is_not_removable(self):
        bundled = list_adventures()[0]
        assert bundled.path == _BUNDLED_DOC.resolve()
        assert bundled.removable is False
        assert bundled.source_kind == "bundled"

    def test_a_dropped_file_is_removable(self, tmp_path):
        (tmp_path / "drop" / "tomb.json").write_bytes(_document("Tomb of the Dropped"))
        entry = _entry("tomb-of-the-dropped")
        assert entry.removable is True
        assert entry.source_kind == "file"

    def test_a_dropped_directory_is_removable_as_a_folder(self, tmp_path):
        run = tmp_path / "drop" / "run.forge"
        run.mkdir()
        (run / "adventure.json").write_bytes(_document("Tomb of the Forge"))
        entry = _entry("tomb-of-the-forge")
        assert entry.removable is True
        assert entry.source_kind == "folder"

    def test_a_symlinked_run_is_removable_as_a_link(self, tmp_path):
        run = tmp_path / "elsewhere" / "run.forge"
        run.mkdir(parents=True)
        (run / "adventure.json").write_bytes(_document("Tomb of the Linked"))
        (tmp_path / "drop" / "run.forge").symlink_to(run)
        entry = _entry("tomb-of-the-linked")
        assert entry.removable is True
        assert entry.source_kind == "link"

    def test_an_extra_source_is_listed_but_not_removable(self, tmp_path, monkeypatch):
        outside = tmp_path / "pointed-at"
        outside.mkdir()
        (outside / "tomb.json").write_bytes(_document("Tomb of the Pointed At"))
        monkeypatch.setenv("OSR_WEB_ADVENTURES", str(outside))
        entry = _entry("tomb-of-the-pointed-at")
        assert entry is not None
        assert entry.removable is False
        assert entry.source_kind == "external"

    def test_a_bundled_override_inside_the_drop_directory_stays_protected(self, tmp_path, monkeypatch):
        # Removability travels with the *source*, not the path: the default
        # adventure is undeletable even when it sits in the drop directory.
        path = tmp_path / "drop" / "bundled.json"
        path.write_bytes(_document("Tomb of the Default"))
        monkeypatch.setenv("OSR_WEB_ADVENTURE", str(path))
        library._meta_cache.clear()
        entries = list_adventures()
        assert entries[0].name == "Tomb of the Default"
        assert entries[0].removable is False
        assert path.exists()


class TestDeleteRoute:
    def test_deleting_a_file_removes_it_and_refreshes_the_library(self, tmp_path):
        path = tmp_path / "drop" / "tomb.json"
        path.write_bytes(_document("Tomb of the Dropped"))
        assert _entry("tomb-of-the-dropped") is not None

        payload = delete_adventure("tomb-of-the-dropped")

        assert payload["adventure_id"] == "tomb-of-the-dropped"
        assert payload["deleted"] is True
        assert not path.exists()
        listed = {a["id"] for a in payload["adventures"]}
        assert "tomb-of-the-dropped" not in listed
        assert payload["default_id"] == payload["adventures"][0]["id"]

    def test_deleting_a_dropped_folder_takes_everything_in_it(self, tmp_path):
        run = tmp_path / "drop" / "run.forge"
        run.mkdir()
        (run / "adventure.json").write_bytes(_document("Tomb of the Forge"))
        (run / "editor.json").write_text("{}", encoding="utf-8")

        delete_adventure("tomb-of-the-forge")

        assert not run.exists()
        assert _entry("tomb-of-the-forge") is None

    def test_deleting_a_symlinked_run_leaves_the_run_whole(self, tmp_path):
        run = tmp_path / "elsewhere" / "run.forge"
        run.mkdir(parents=True)
        document = run / "adventure.json"
        document.write_bytes(_document("Tomb of the Linked"))
        (run / "editor.json").write_text("{}", encoding="utf-8")
        link = tmp_path / "drop" / "run.forge"
        link.symlink_to(run)

        delete_adventure("tomb-of-the-linked")

        assert not link.exists()
        assert not link.is_symlink()
        # The whole point of unlinking rather than resolving: the forge run the
        # link pointed at is untouched.
        assert document.is_file()
        assert (run / "editor.json").is_file()
        assert _entry("tomb-of-the-linked") is None

    def test_deleting_a_symlinked_file_leaves_the_file(self, tmp_path):
        target = tmp_path / "elsewhere" / "tomb.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(_document("Tomb of the Linked File"))
        link = tmp_path / "drop" / "tomb.json"
        link.symlink_to(target)

        delete_adventure("tomb-of-the-linked-file")

        assert not link.is_symlink()
        assert target.is_file()

    def test_the_drop_directory_itself_survives_its_own_document(self, tmp_path):
        # `<drop>/adventure.json` is a candidate whose parent *is* the drop
        # directory: the file goes, the directory stays.
        drop = tmp_path / "drop"
        (drop / "adventure.json").write_bytes(_document("Tomb of the Drop Root"))

        delete_adventure("tomb-of-the-drop-root")

        assert drop.is_dir()
        assert not (drop / "adventure.json").exists()

    def test_deleting_the_bundled_document_is_a_422(self):
        bundled = list_adventures()[0]
        with pytest.raises(HTTPException) as excinfo:
            delete_adventure(bundled.id)
        assert excinfo.value.status_code == 422
        assert _BUNDLED_DOC.is_file()

    def test_deleting_an_extra_source_is_a_422(self, tmp_path, monkeypatch):
        outside = tmp_path / "pointed-at"
        outside.mkdir()
        path = outside / "tomb.json"
        path.write_bytes(_document("Tomb of the Pointed At"))
        monkeypatch.setenv("OSR_WEB_ADVENTURES", str(outside))

        with pytest.raises(HTTPException) as excinfo:
            delete_adventure("tomb-of-the-pointed-at")

        assert excinfo.value.status_code == 422
        assert path.is_file()

    def test_an_unknown_id_is_a_404(self):
        with pytest.raises(HTTPException) as excinfo:
            delete_adventure("no-such-adventure")
        assert excinfo.value.status_code == 404

    def test_deleting_the_last_dropped_adventure_leaves_the_bundled_default(self, tmp_path):
        (tmp_path / "drop" / "tomb.json").write_bytes(_document("Tomb of the Dropped"))
        payload = delete_adventure("tomb-of-the-dropped")
        assert len(payload["adventures"]) == 1
        assert payload["adventures"][0]["removable"] is False


class TestListingContract:
    def test_every_entry_reports_removability_and_kind_without_a_path(self, tmp_path):
        (tmp_path / "drop" / "tomb.json").write_bytes(_document("Tomb of the Dropped"))
        from server.app import adventures

        for entry in adventures()["adventures"]:
            assert set(entry) == {
                "id",
                "name",
                "description",
                "hooks",
                "town_name",
                "dungeon_count",
                "removable",
                "source_kind",
            }
            assert entry["source_kind"] in {
                "bundled",
                "external",
                "file",
                "folder",
                "link",
            }
