"""The battle menu's defensive moves (`static/app.js`).

osrlib's `BattleDeclaration.move` follows the SRD: a fighting withdrawal backs the party off at
half its encounter rate, and a retreat runs at the full rate and ends the battle once every member
declares it. `withdraw` corresponds to no rule, the engine never acted on it, and osrlib is
removing it, so a menu that sends it moves nobody today and is refused outright next release. The
menu offers the two real moves and never the dead one.
"""

from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")


def test_falling_back_declares_a_fighting_withdrawal():
    assert '{ action: "move", move: "fighting_withdrawal" }' in SOURCE


def test_a_retreat_is_offered():
    assert '{ action: "move", move: "retreat" }' in SOURCE


def test_no_declaration_names_withdraw():
    assert 'move: "withdraw"' not in SOURCE


class TestAnUnidentifiedArmIsOffered:
    """`wieldedWeapons()` offers every wielded arm whose masked entry carries `qualities`, identified
    or not. osrlib states an unidentified arm's base-weapon facts whenever its display names the base
    weapon, so the engine accepts a swing with it and identifies it on the first attack roll."""

    def test_the_menu_does_not_require_identification(self):
        assert "w.identified && w.qualities" not in SOURCE
        assert "if (w.qualities)" in SOURCE
