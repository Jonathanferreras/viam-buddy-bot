# Buddy Bot

A two-armed robot you talk to. Say **"Hey buddy, grab the fork and rotate it 90 degrees"**, **"clean up"**,
**"let's play"** or **"high five"**, and Buddy works out what you mean, finds things with its cameras and moves
its arms to do it.

Built at the Viam Hackathon 2026 on two UFactory xArm 850 arms, three Intel RealSense depth cameras and
[Viam](https://www.viam.com). Everything runs locally: speech recognition (Vosk), object naming (CLIP) and the
optional language model (Ollama).

- **Helper** (arm 17) fetches things: grabs, carries, puts back, tidies up the desk.
- **Player** (arm 18) plays games: the shell game and Jenga.
- A **status page** at <http://localhost:8765> shows what Buddy hears, what each arm is doing and what the
  cameras see, with buttons for everything you can also say.

---

## How it works

```
 microphone ──► Vosk speech-to-text ──► understanding ──────────► command ──► router / feature ──► arms, grippers
 (on the Mac)   "hey buddy" + command   Simple: phrases + rules            (checks reach, safety,
                                        Smart:  local LLM (Ollama)          manual mode, busy)
                                                     ▲
 cameras ──► depth + color detection ──► names (CLIP, taught labels) ──────┘   (what's on the desk, where)
             desk camera + wrist camera
```

The project is a **Viam module** (`hackathons:voice-router`, a generic service) that runs as a local module on
the Mac with the microphone (`jonathan-mac-voice`). The two arm machines (`armfarm17`, `armfarm18`) are
reached as remotes.

1. **Listening.** Vosk transcribes the microphone continuously against a fixed word list (every command word
   Buddy knows), which makes "hey buddy" and "stop" reliable. After the wake word you have a few seconds to
   say the command. Chimes mark "listening", "done" and "didn't catch that".
2. **Understanding.** Exact command phrases run straight away. Everything else goes either to the rule-based
   parser (Simple mode) or to a local language model (Smart mode, see below). "Stop" never waits for
   anything: it halts every arm at once.
3. **Seeing.** The desk camera and arm 17's wrist camera find things on the blue mat from depth and color.
   CLIP gives them names ("white fork", "red can"), and you can teach names on the status page.
4. **Acting.** Each command goes through the router's own checks (reach, gripper width, manual mode, one
   command at a time) and then moves the arms. Picks use straight-line moves worked out with the arm's
   kinematics (`src/kin.py`), and fall back to Viam's motion planner when needed.

---

## Things you can say

Start each one with **"Hey buddy"**. In Smart mode you can also just say what you want in your own words.

| What | Say |
|---|---|
| **Grab** | "grab the red can", "pick up the white cup 2", "get the screwdriver", or just "the white cup" |
| **Carry** | "grab the screwdriver and put it in the bottom right corner", "move the fork to the center" |
| **Put back / let go** | "put it back" (returns it to where it was picked up), "let go" / "drop it" (opens the gripper where it is) |
| **Clean up** | "clean up": moves every named thing on the desk to the side, in a row |
| **Arm moves** | 85 of them, see [Arm moves](#arm-moves): "rotate your wrist 90 degrees", "move up a little", "nod twice", "draw a heart" |
| **Chains** | "grab the fork and rotate 90 degrees", "nod and then wave arm seventeen" |
| **Reset** | "reset arm seventeen", "go back" |
| **Fun** | "wave arm seventeen", "dance", "high five", "celebrate" |
| **Games** (arm 18) | "let's play" (shell game), then "left / middle / right cup" or tap the cup; "where is the ball"; "set up Jenga" |
| **Manual mode** | "manual mode" (move the arm by hand), "manual mode off"; add "18" for the other arm |
| **Understanding** | "smart mode" / "simple mode" |
| **Stop** | "stop", "halt", "freeze": always heard, even while an arm is moving |

---

## Smart mode (local LLM)

Smart mode sends anything that isn't an exact command to a local model through
[Ollama](https://ollama.com), together with what the cameras see, what each arm holds and which arms are in
manual mode. The model rewrites your request into one or more commands Buddy already knows, for example
*"the place is a mess, could you sort it out"* → `clean up`, or *"stick the green ball in the bottom right"* →
`move the green ball to the bottom right corner`. Those commands then go through the normal parser and safety
checks, exactly as if you'd said them.

- **Set up:** install Ollama and pull a model, e.g. `ollama pull qwen3.5:9b`, the default here: it gets these
  right in about 3–5 s. `gemma3:4b` is faster but makes things up.
- **Switch:** the **Simple / Smart** toggle at the top right of the voice panel, say "smart mode" / "simple
  mode", or DoCommand `{"nlu_mode": "llm" | "rules"}`. The choice and the model are kept in
  `nlu_settings.json`.
- **Model:** DoCommand `{"nlu_mode": {"model": "gemma3:4b"}}`.
- **Try it without moving anything:** DoCommand `{"nlu_test": "could you tidy up a bit"}` shows what it
  would run, its reply, the scene it was given and Ollama's timings.
- **It never stops the arms, and can only run commands Buddy already knows.** Anything else it suggests is
  dropped. If Ollama is down or slow, the usual rules take over (an orange dot on the toggle).
- **Exact commands skip the model:** anything the arm-move parser understands completely runs instantly with
  your exact numbers.
- **Listening:** in Smart mode a second, open-vocabulary recognizer listens alongside the fixed word list, so
  words Buddy has never heard of reach the model. When the fixed list heard a complete known command, that
  wins over the open recognizer's guess.

Config (router attributes): `"llm": {"mode": "rules", "model": "qwen3.5:9b", "url": "http://127.0.0.1:11434",
"timeout_s": 25, "open_speech": true}`.

---

## Arm moves

`src/features/moves.py` gives both arms 85 named motions. Each one is planned in full and checked before
anything moves.

| Group | Moves |
|---|---|
| Turning at the base | turn left / right, look left / right / around, scan the desk, point left / right, sway, swing |
| Twisting the wrist | rotate left / right, rotate (e.g. "rotate 90 degrees"), flip, spin, wiggle wrist, straighten / point down, roll left / right |
| Tilting the hand | tilt forward / back / left / right, nod, shake head no, shrug ("maybe"), wobble |
| Moving the hand | move forward / back / left / right / up / down, present it, sleep, wake up, undo that |
| The gripper | open, close, half open, clap, pinch |
| Gestures | high five, fist bump, salute, bow, stretch, shiver, celebrate, knock, drum, think, bounce, hide, peek-a-boo, peek, point, shake hands, count to three, bye, sneeze, yawn, sigh; moods: excited, shy, sad, happy, angry, surprised, confused, dizzy; hand dance |
| Drawing in the air | circle (flat or standing), square, triangle, star, heart, figure eight, zigzag, spiral, wave, line, X, check mark |

**Modifiers** work with any move:
- **amounts:** "30 degrees", "ninety degrees", "5 centimeters", "2 inches", "a little", "a lot"
- **speed:** "slowly", "quickly"
- **repeats:** "twice", "three times"
- **which arm:** "arm 18", "the other arm", "both arms"

Directions are the arm's own: forward means reaching away from its base, and left/right are as seen from
behind the arm. Pose moves stay where they end ("undo that" goes back); gestures return to where they
started.

**Safety:** every step of the path is checked first:
- joint limits
- fingers and wrist at least 5 cm above the table
- within reach
- clear of the desk edge and the other arm's base
- never more than 35 cm from where it started

If anything fails, nothing moves and Buddy says why. If the arm's kinematics don't match where it reports
itself, only base turns, wrist twists and the gripper are allowed. Tunable under `"moves"`
(`clearance_mm`, `max_travel_mm`, `speed_deg_s`, `keep_out`).

DoCommand `{"moves": "list"}` lists every move with its phrases; `{"moves": "plan", "text": "rotate ninety
degrees"}` plans one against the arm's real joints without moving.

---

## Seeing and grabbing

- **Detection** (`src/vision.py`) fits the table plane in the depth image and keeps what stands up from it on
  the blue mat.
  - **Merged objects:** touching things of clearly different colors (a fork lying against a block) are split
    apart. A cube's shaded side or a screwdriver's black ends stay part of the same thing unless one piece
    stands clearly taller.
  - **Dropped:** flat marks, things without a depth reading, and things beside the mat (phone, plug, power
    strip).
  - **Upright cans** seen from straight above are kept and get their color from their rim.
- **Naming** (`src/identify.py`) uses CLIP; numbered duplicates read "white cup 1", "white cup 2". On the
  status page you can rename a thing and mark it **verified** (what clean-up moves). Names are remembered by
  appearance (`src/taught.py`, `taught_labels.json`).
- **Wrist camera (arm 17):** what it sees is placed in the arm's base frame from the camera mount and the
  arm's pose, which is accurate.
- **Desk camera ↔ arm 17 link:** whenever both cameras see the same block, that's a calibration point (kept
  in `table_link.arm17-arm.json`). Once there are four, the desk camera can hand arm 17 things its own
  camera can't see.
  - **Height** comes from the desk camera's measurement of the object above the table.
  - **Before grabbing** something found by the desk camera, arm 17 checks it with its own camera (from
    where it is, or from right above the spot) and grabs where that says.
- **Grabbing** goes over the thing, turns the wrist across its narrow side, comes straight down, squeezes
  gently, checks it caught it, lifts and returns to the start position. Arm 17 remembers where it picked
  each thing up (kept across restarts), so "put it back" sets it down exactly there.

---

## Running it

The module runs as a **local module** on the machine with the microphone. `run.sh` creates a virtualenv on
first run, installs `requirements.txt` and starts `python -m src.main`.

1. In the Viam app, add a local module with executable path `<this folder>/run.sh`, and a generic service of
   model `hackathons:voice-router:router` (named `voice-router` here).
2. Configure it: the microphone, the commands, the cameras and the arms. Every attribute is documented at the
   top of `src/main.py` and in each feature's file in `src/features/`. The arms and cameras come from the
   arm machines as remotes; this machine uses the `arm17-` prefix for arm 17's.
3. Open <http://localhost:8765>.

**Status page:** a React + Vite app in `web/`; the module serves the built `web/dist`. See
[`web/README.md`](web/README.md):

```bash
cd web && npm install && npm run build   # or npm run dev for hot reload on :5173
```

**One-time setup on the status page or by voice:**
- **Start positions:** move an arm (Manual mode) and press **Save start**.
- **Clean-up spot:** arm 17 over it, press the **Clean-up spot** pin, or say "this is the clean up spot".
- **Hand-off spot, wave pose, calibration points:** the Setup panel.

Positions are kept in `wave_poses.json`.

### DoCommands (for testing without speaking)

| DoCommand | Does |
|---|---|
| `{"simulate": "grab the red can"}` | routes the text as if it were heard |
| `{"status": true}` | everything the status page shows |
| `{"mic": true \| false \| "toggle"}` | mute / unmute |
| `{"save_home": "arm17-arm"}`, `{"clear_home": …}` | an arm's start position |
| `{"save_live_frame": "name", "camera": "arm17-cam"}` | saves a raw color + depth frame (`debug_frame_name.npz`) for offline detector tests |
| `{"look": true}`, `{"calibration": true}`, `{"calib_record": "green"}` | block finding and camera calibration |
| `{"recordings": true}`, `{"record_stop": true}` | the per-command video clips in `recordings/` |
| `{"nlu_mode": …}`, `{"nlu_test": "…"}` | Smart mode (above) |
| `{"moves": "list" \| "plan", "text": …}` | arm moves (above) |
| `{"manual_mode": {"arm": "arm17-arm", "on": true}}` | hand-guided mode |
| `{"cleanup": true}`, `{"cleanup_spot": "save" \| "clear" \| "show"}` | clean-up |
| `{"dance": true}`, `{"carry_spot": …}`, `{"cup_taps": …}`, `{"taught_labels": true}` | the other features |

---

## Plug-in features

Everything beyond the core router lives in `src/features/`, one file per feature. Each defines
`FEATURE = Feature(...)` and is picked up automatically, so new features don't need edits to `main.py` and
several people can work on different features at once.

| Feature | File |
|---|---|
| Arm moves (85 motions, chains) | `moves.py` |
| Smart mode (Ollama) | `llm.py` |
| Clean up | `cleanup.py` |
| Carry ("put it in the corner") | `carry.py` |
| Manual (hand-guided) mode | `manual.py` |
| Dance | `dance.py` |
| Jenga | `jenga.py` + `jenga/` |
| Shell game: tap the cup | `cup_tap.py`, `_tapdetect.py` |
| Teach names / verified | `labels.py` + `src/taught.py` |
| "Just name it" grabs | `just_name.py` |

A feature can provide:
- `understand` (speech → command)
- `run` (do it)
- `arms_for`, `label`, `state` (shown on the status page)
- `page_actions`, `do_commands`, `words` (for the speech recognizer)
- `catalogue` (its commands, for Smart mode)
- `priority` (who gets first say)

See the docstring in `src/features/__init__.py`.

The shell game itself is in `shell_game/`, vendored from
[dramirez4/viam-shell-game](https://github.com/dramirez4/viam-shell-game) (see `shell_game/README.md`).

---

## Layout

```
run.sh, meta.json, requirements.txt    the Viam local module
src/main.py                             the router: listening, commands, arms, grabbing, cameras, status
src/vision.py                           detection, block finding, camera calibration
src/identify.py, src/taught.py          naming things (CLIP) and taught names
src/kin.py                              xArm 850 kinematics (FK/IK, straight-line paths)
src/grip.py, src/recorder.py, src/sounds.py, src/status_page.py
src/features/                           plug-in features (above)
web/                                    the status page (React + Vite)
shell_game/, jenga/                     the games arm 18 plays
sounds/                                 chimes
wave_poses.json                         start positions, wave poses, clean-up spot, where things were picked up
table_link.arm17-arm.json               desk camera ↔ arm 17 calibration points (learned automatically)
nlu_settings.json, taught_labels.json   Smart-mode choice, taught names
```

---

## Troubleshooting

- **A command does nothing.** The voice panel says what was heard and why; the Logs drawer (**L**) has the
  details. "I don't know where the side is yet" means the clean-up spot hasn't been taught.
- **The arm won't move after manual mode.** Press ✓ on its card (or say "manual mode off"). While in manual
  mode the arm refuses motion on purpose. "Stop" also ends it.
- **Grabs miss.** Check the detections in the Helper view; a grab the desk camera hands to arm 17 is checked
  with arm 17's camera first. For detector work, save a frame with `save_live_frame` and run
  `vision.detect_objects` on it.
- **Smart mode is slow or wrong.** `{"nlu_test": "…"}` shows the scene the model was given and its timings;
  try another model with `{"nlu_mode": {"model": "…"}}`, or switch back to Simple.
- **Changes to the code** take effect when the module restarts (Viam app → restart, or `RestartModule`).
