# Shell game (vendored)

Copied from https://github.com/dramirez4/viam-shell-game at commit 9e8a4e5 (`run.py`, `robot/`, README).
`archive/` and `simulation/` were left out. The files are unchanged; the voice router runs
`run.py --execute --ball-slot <slot> --swaps <n>` as a child process (see `_shell_game` in `src/main.py`).

Credentials: `robot/.env` (gitignored) holds armfarm18's address and API key, as in `.env.example`.
To update, copy the upstream files over this folder again.

## Added for the buddy bot (not upstream)

- `play.py`: the voice/tap game: `start` (overview, scan, `robot/setup_ball.py`, scan, shuffle) and
  `reveal --run <folder> --slot <slot>` (`robot/reveal_cup.py`). Prints `STAGE <id> <text>` lines.
- `robot/setup_ball.py`: lifts the middle cup, waits for the red ball to settle under it (wrist
  camera), covers it, returns to the overview pose.
- `robot/reveal_cup.py`: lifts the cup the player picked, holds it up, puts it back.
- `robot/game_lib.py`: the moves and gripper checks those two share (same approach as `round.py`).
- `robot/round.py`: one change: `SHUFFLE_SKIP_REVEAL=1` skips the opening peek (the player just saw
  the ball go under the cup).
- `robot/putdown_cup.py` (`play.py putdown`): after a stop, lowers a cup arm 18 is still holding
  straight down, lets go, and returns to the overview pose.
- `robot/look_at_cups.py`: after the shuffle, back to the overview pose so the camera shows every cup for the guess; writes where each cup is in the picture (`guess_view.json`).
