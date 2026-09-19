"""Unit tests for the party rename endpoint (`server/app.py::rename_member`).

The endpoint is called directly (no HTTP layer), with the game registered in the
module's in-memory store the same way `create_game` would leave it. Renaming is
app-owned logic: name validation, the member lookup, and the transcript retell
under the new name.
"""

import pytest
from fastapi import HTTPException
from osrlib.crawl.commands import PrepareSpells, Rest

from server.app import Game, _games, rename_member
from server.content import new_session

_GAME_ID = "test-rename"


def _register_game() -> Game:
    game = Game(new_session(seed=42))
    _games[_GAME_ID] = game
    return game


class TestRenameMember:
    def teardown_method(self):
        _games.pop(_GAME_ID, None)

    def test_renames_the_member_and_returns_fresh_state(self):
        game = _register_game()
        member = game.session.party.members[0]
        payload = rename_member(_GAME_ID, {"member_id": member.id, "name": "Aldric"})
        assert payload["accepted"] is True
        assert payload["rejections"] == []
        assert member.name == "Aldric"
        names = [m["name"] for m in payload["view"]["party"]]
        assert "Aldric" in names

    def test_transcript_retells_under_the_new_name(self):
        # A new game's transcript is empty by design (turn 0), so put
        # an event that names the member on the log first: the magic-user rests
        # and prepares a spell. The retold log must then use the new name.
        game = _register_game()
        member = next(m for m in game.session.party.members if m.class_id == "magic_user")
        game.session.execute(Rest(kind="night"))
        game.session.execute(
            PrepareSpells(
                character_id=member.id,
                selections=({"spell_id": member.spell_book[0], "reversed": False},),
            )
        )
        old_name = member.name
        payload = rename_member(_GAME_ID, {"member_id": member.id, "name": "Nyx"})
        text = " ".join(entry["text"] for entry in payload["log"])
        assert old_name not in text
        assert "Nyx" in text

    def test_surrounding_whitespace_is_trimmed(self):
        game = _register_game()
        member = game.session.party.members[0]
        payload = rename_member(_GAME_ID, {"member_id": member.id, "name": "  Wren  "})
        assert payload["accepted"] is True
        assert member.name == "Wren"

    def test_empty_name_rejected(self):
        game = _register_game()
        member = game.session.party.members[0]
        old_name = member.name
        payload = rename_member(_GAME_ID, {"member_id": member.id, "name": "   "})
        assert payload["accepted"] is False
        assert payload["rejections"][0]["code"] == "creation.name.invalid"
        assert payload["rejections"][0]["message"]  # a friendly, non-empty line
        assert member.name == old_name

    def test_overlong_name_rejected(self):
        game = _register_game()
        member = game.session.party.members[0]
        payload = rename_member(_GAME_ID, {"member_id": member.id, "name": "x" * 41})
        assert payload["accepted"] is False
        assert payload["rejections"][0]["code"] == "creation.name.invalid"

    def test_unknown_member_is_404(self):
        _register_game()
        with pytest.raises(HTTPException) as excinfo:
            rename_member(_GAME_ID, {"member_id": "character-9999", "name": "Aldric"})
        assert excinfo.value.status_code == 404

    def test_unknown_game_is_404(self):
        with pytest.raises(HTTPException) as excinfo:
            rename_member("no-such-game", {"member_id": "character-0001", "name": "A"})
        assert excinfo.value.status_code == 404
