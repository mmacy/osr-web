# Spec: passages

osrlib 2.0 adds the *passage*, a transition the party walks through instead of taking with the use-stairs command. A passage stands on one side of a cell. A `move_party` from that cell across that side lands the party on the passage's destination, which can be on another level. osr-forge writes passages where two levels' floors meet on a printed map, and where it lays out the two halves of a map apart. osrlib's own documentation of `TransitionSpec` and `MoveParty` is the authority on how a passage plays.

osr-web plays passages with one server change. The cell context the server sends the browser (`cell.transition`) names only a transition the party can take with `use_stairs`, so a cell where only a passage stands shows no "Use stairs" button. Stairs on the same cell as a passage are still offered. The browser needs nothing else: the first-person view draws a passage's side as an opening into darkness, because the player view reports that side as open, and the automap leaves a gap there.

## Phase 1: the passage in play

The server leaves passages out of `cell.transition`. Done when `tests/test_passages.py` passes: no stairs are offered on a cell where only a passage stands, stairs beside a passage are offered, and a game crosses a passage pair between two levels both ways with `move_party` commands.
