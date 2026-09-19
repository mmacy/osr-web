"""Unit tests for the adventure upload path (`POST /api/adventures`).

The scan (`server/library.py`) trusts what's already on the server's disk and
reads metadata only; the upload is the one route where bytes arrive over the
wire, so `add_adventure` must vet the whole document before writing, land the
original bytes in the drop directory, and collapse into an existing entry when
the library already holds the same file. Invalid content raises
`ContentValidationError`, which `app.py`'s handler turns into a 422.

Every test redirects the drop directory at a `tmp_path` with
`OSR_WEB_ADVENTURES_DIR` so nothing touches the repo's own `adventures/`, and
the module-level `_meta_cache` is cleared around each test the same way
`tests/test_paths.py` does.
"""

import asyncio
import json

import pytest
from fastapi import HTTPException
from osrlib.errors import ContentValidationError

from server import app as server_app
from server import library
from server.library import add_adventure, list_adventures

_BUNDLED_DOC = library._CONTENT_DIR / "adventure.json"


def _valid_document(name: str) -> bytes:
    """The bundled document re-stamped with a different adventure name.

    Renaming keeps the payload valid while changing the bytes, so the digest
    dedupe cannot mistake it for the bundled entry.
    """
    document = json.loads(_BUNDLED_DOC.read_bytes())
    document["payload"]["name"] = name
    return json.dumps(document).encode("utf-8")


@pytest.fixture(autouse=True)
def _isolated_drop_dir(monkeypatch, tmp_path):
    """Run every test against an empty temp drop directory and a fresh cache."""
    for name in ("OSR_WEB_ADVENTURE", "OSR_WEB_ADVENTURES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OSR_WEB_ADVENTURES_DIR", str(tmp_path))
    library._meta_cache.clear()
    yield
    library._meta_cache.clear()


class TestAddAdventure:
    def test_valid_upload_lands_in_the_drop_directory(self, tmp_path):
        raw = _valid_document("Tomb of the Uploaded")
        entry = add_adventure(raw)
        assert entry.id == "tomb-of-the-uploaded"
        assert entry.name == "Tomb of the Uploaded"
        written = tmp_path / "tomb-of-the-uploaded.json"
        assert entry.path == written.resolve()
        # The bytes written are the bytes received — formatting and all.
        assert written.read_bytes() == raw
        assert entry.id in [e.id for e in list_adventures()]

    def test_duplicate_bytes_return_the_existing_entry(self, tmp_path):
        raw = _valid_document("Tomb of the Uploaded")
        first = add_adventure(raw)
        second = add_adventure(raw)
        assert second.id == first.id
        assert len(list(tmp_path.iterdir())) == 1

    def test_bytes_the_library_already_holds_write_nothing(self, tmp_path):
        # Uploading the bundled document itself collapses into the bundled entry.
        entry = add_adventure(_BUNDLED_DOC.read_bytes())
        assert entry.path == _BUNDLED_DOC.resolve()
        assert list(tmp_path.iterdir()) == []

    def test_same_name_different_bytes_get_a_suffixed_file(self, tmp_path):
        document = json.loads(_valid_document("Tomb of the Uploaded"))
        document["payload"]["description"] = "The same tomb, revised."
        first = add_adventure(_valid_document("Tomb of the Uploaded"))
        second = add_adventure(json.dumps(document).encode("utf-8"))
        # The collision file sorts *after* the original in the scan, so the
        # first upload keeps its slug id and the second takes the `-2` suffix.
        assert first.id == "tomb-of-the-uploaded"
        assert second.id == "tomb-of-the-uploaded-2"
        assert (tmp_path / "tomb-of-the-uploaded.json").is_file()
        assert (tmp_path / "tomb-of-the-uploaded_2.json").is_file()

    def test_not_json_is_rejected(self, tmp_path):
        with pytest.raises(ContentValidationError, match="not valid JSON"):
            add_adventure(b"not json at all {")
        assert list(tmp_path.iterdir()) == []

    def test_wrong_kind_is_rejected(self, tmp_path):
        document = json.loads(_BUNDLED_DOC.read_bytes())
        document["kind"] = "save"
        with pytest.raises(ContentValidationError, match="expected a 'adventure'"):
            add_adventure(json.dumps(document).encode("utf-8"))
        assert list(tmp_path.iterdir()) == []

    def test_broken_payload_is_rejected_with_the_first_problem(self, tmp_path):
        document = json.loads(_valid_document("Tomb of the Broken"))
        document["payload"]["dungeons"] = "not a list"
        with pytest.raises(ContentValidationError, match="not a playable adventure"):
            add_adventure(json.dumps(document).encode("utf-8"))
        assert list(tmp_path.iterdir()) == []

    def test_a_dangling_authored_reference_is_rejected(self, tmp_path):
        # Model-valid and still broken: `item_ids` is a list of strings, so only
        # `validate_adventure`'s cross-reference walk catches a typo in one. An
        # upload that landed would load clean and quietly hand the party nothing.
        document = json.loads(_valid_document("Tomb of the Dangling"))
        level = document["payload"]["dungeons"][0]["levels"][0]
        area = next(entry for entry in level["areas"] if entry["features"])
        area["features"][0]["item_ids"].append("no-such-item")
        with pytest.raises(ContentValidationError, match="no-such-item"):
            add_adventure(json.dumps(document).encode("utf-8"))
        assert list(tmp_path.iterdir()) == []

    def test_a_very_long_name_gets_a_capped_filename(self, tmp_path):
        # `Adventure.name` is an unconstrained str, so a name that slugs past the
        # filesystem's ~255-byte limit passes the vet; the filename is capped or
        # the write dies on ENAMETOOLONG (`path.exists()` is False for it).
        entry = add_adventure(_valid_document("The " + "Very " * 60 + "Deep Tomb"))
        assert entry.name.startswith("The Very")
        written = list(tmp_path.iterdir())
        assert len(written) == 1
        assert len(written[0].name) <= 110


class _RawRequest:
    """The one method of a request the upload route touches."""

    def __init__(self, raw: bytes):
        self._raw = raw

    async def body(self) -> bytes:
        return self._raw


class TestUploadRoute:
    def test_response_carries_the_new_id_and_the_refreshed_library(self):
        raw = _valid_document("Tomb of the Uploaded")
        payload = asyncio.run(server_app.upload_adventure(_RawRequest(raw)))
        assert payload["adventure_id"] == "tomb-of-the-uploaded"
        listed = {a["id"] for a in payload["adventures"]}
        assert "tomb-of-the-uploaded" in listed
        # The bundled document stays the library's first entry and its default.
        assert payload["default_id"] != "tomb-of-the-uploaded"

    def test_an_oversized_body_is_a_422_before_any_parsing(self, tmp_path):
        raw = b"x" * (server_app._MAX_ADVENTURE_BYTES + 1)
        with pytest.raises(HTTPException) as error:
            asyncio.run(server_app.upload_adventure(_RawRequest(raw)))
        assert error.value.status_code == 422
        assert "too large" in error.value.detail
        assert list(tmp_path.iterdir()) == []
