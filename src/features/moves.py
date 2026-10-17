"""Arm moves: ~90 named motions you can ask either arm for — "rotate your wrist 90 degrees", "turn left a
little", "move up 5 centimeters", "tilt forward", "nod", "high five", "draw a circle", "knock twice",
"open your gripper", "undo that" — and chains like "grab the fork and rotate 90 degrees".

Each request becomes a plan that is checked from start to finish before anything moves, with the arm's own
kinematics (src/kin.py), at every step of the path:
    * every joint stays inside its limits,
    * the fingertips (and the wrist) stay at least `clearance_mm` above the table,
    * the fingertips stay inside the arm's reach and out of the `keep_out` boxes (the desk edge, the
      other arm's base), and never wander more than `max_travel_mm` from where they started.
If the plan breaks a rule nothing moves, and it's said why ("the fingers would come too close to the
table — say “move up” first"). If our kinematics don't agree with where the arm says it is, only moves
that can't change the fingertips' height are allowed (turning at the base, twisting the wrist, the gripper).

Two kinds of moves:
    pose     the arm ends somewhere new and stays there (turn, rotate, tilt, move up/down/left/..., open);
             "undo that" goes back to where it was before the last one
    gesture  the arm does something and comes back to exactly where it started (nod, high five, draw)

Modifiers that work with any move:
    amount   "30 degrees", "ninety degrees", "5 centimeters", "2 inches", "a little" (x0.4), "a lot" (x2)
    speed    "slowly" (x0.5), "quickly" / "fast" (x1.6)
    repeats  "twice", "three times" (gestures)
    arm      "arm 18", "the other arm", "both arms" (gestures); otherwise the helper arm (vision.picker)
Chains   "<command> and <move>" / "<move> then <command>": run one after the other (any command Buddy
         knows, as long as at least one part is a move): "grab the fork and rotate 90 degrees".
"Stop" halts it like any command; the arm stays where it halted.

Directions are the arm's own: forward = reaching out, away from its base; back = toward its base;
left / right = across that, as seen from behind the arm looking out; up / down = up / down.

Optional config, in the router's attributes:
    "moves": {"clearance_mm": 50,          # fingertips (and wrist) never closer to the table than this
              "max_travel_mm": 350,        # never further than this from where the move started
              "speed_deg_s": 45,           # joint speed for pose moves (gestures: 1.8x, capped at 150)
              "keep_out": {"arm17-arm": [[xmin, xmax, ymin, ymax, zmin, zmax], ...]}}   # mm, arm base frame
DoCommand {"moves": "list"} -> every move with its phrases;  {"moves": "plan", "text": "..."} -> the plan only.
"""
import asyncio
import math
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from . import Feature
from .. import kin

# ---------------------------------------------------------------- words

_FILLER = {"please", "can", "could", "would", "will", "you", "your", "the", "a", "an", "it", "its", "to", "of",
           "my", "me", "now", "just", "for", "hey", "buddy", "arm", "by", "some", "again", "go", "do", "and",
           "degrees", "degree", "centimeters", "centimeter", "centimetres", "centimetre", "cm", "inches", "inch",
           "millimeters", "millimeter", "mm", "times", "time", "twice", "thrice", "once",
           "bit", "little", "slightly", "tiny", "touch", "lot", "far", "much", "big", "small",
           "slowly", "slow", "quickly", "quick", "fast", "faster", "slower", "gently", "other", "both", "arms",
           "too", "more", "then", "that", "this", "with"}
_SMALL = {"bit", "little", "slightly", "tiny", "touch", "small"}
_BIG = {"lot", "far", "much", "big"}
_SLOW = {"slowly", "slow", "slower", "gently"}
_FAST = {"quickly", "quick", "fast", "faster"}
_UNITS = {"degrees": "deg", "degree": "deg", "centimeters": "cm", "centimeter": "cm", "centimetres": "cm",
          "centimetre": "cm", "cm": "cm", "inches": "in", "inch": "in", "millimeters": "mm", "millimeter": "mm",
          "mm": "mm"}
_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _numbers(text: str) -> List[str]:
    """Words with spoken numbers turned into digits: "rotate ninety degrees" -> [..., "90", "degrees"],
    "a hundred and eighty" -> "180", "forty five" -> "45", "three sixty" -> "360"."""
    w = re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("’", "").replace("'", "").replace("-", " ")).split()
    out, i = [], 0
    while i < len(w):
        t = w[i]
        if t in _ONES or t in _TENS or (t in ("a", "one") and i + 1 < len(w) and w[i + 1] == "hundred"):
            n, j, used = 0, i, False
            if w[j] in ("a", "one") and j + 1 < len(w) and w[j + 1] == "hundred":
                n, j, used = 100, j + 2, True
                if j < len(w) and w[j] == "and":
                    j += 1
            elif w[j] in _ONES and j + 1 < len(w) and w[j + 1] == "hundred":
                n, j, used = _ONES[w[j]] * 100, j + 2, True
                if j < len(w) and w[j] == "and":
                    j += 1
            if j < len(w) and w[j] in _TENS:
                n, j, used = n + _TENS[w[j]], j + 1, True
                if j < len(w) and w[j] in _ONES and 0 < _ONES[w[j]] < 10:
                    n, j = n + _ONES[w[j]], j + 1
            elif j < len(w) and w[j] in _ONES and not (used and n % 100):
                v = _ONES[w[j]]
                if not used and j + 1 < len(w) and w[j + 1] in _TENS and v < 10:   # "three sixty"
                    n, j, used = v * 100 + _TENS[w[j + 1]], j + 2, True
                else:
                    n, j, used = n + v, j + 1, True
            if used:
                out.append(str(n)); i = j; continue
        out.append(t); i += 1
    return out


# ---------------------------------------------------------------- the moves

@dataclass
class Move:
    name: str
    phrases: List[str]
    desc: str
    steps: Callable[[float], List[tuple]]     # amount -> segments (see _plan)
    kind: str = "gesture"                      # "pose" stays there; "gesture" comes back
    unit: Optional[str] = None                 # "deg" or "cm": what an amount means
    default: float = 0.0
    lo: float = 0.0
    hi: float = 0.0
    opens: bool = False                        # opens the gripper (refused while holding something)
    group: str = ""
    words: List[set] = field(default_factory=list)


def J(*d):            # joint keyframe: degrees from where the move started, joints 1..6
    return ("j", [float(x) for x in d] + [0.0] * (6 - len(d)))


def C(f=0.0, l=0.0, u=0.0):   # straight-line keyframe: mm from where it started (forward, left, up)
    return ("c", (float(f), float(l), float(u)))


def TILT(fwd=0.0, left=0.0):  # turn the hand (fingers swing forward / left), degrees, about the wrist
    return ("tilt", (float(fwd), float(left)))


W = lambda s: ("w", float(s))            # wait
G = lambda op: ("g", op)                 # gripper: open / close / half
START = ("start",)                       # back to where the move started
HOME = ("home",)                         # the arm's saved starting position
DOWN = ("down",)                         # turn the hand to point straight down, staying where it is
UNDO = ("undo",)                         # back to where it was before the last pose move


def rep(seq, n):
    return [s for _ in range(n) for s in seq]


def circle(r, n=16, plane="h", turns=1.0, grow=0.0):
    """Keyframes round a circle of radius r (mm) that starts and ends where the hand is (centre to its left,
    or above it for a standing circle). grow > 0: a spiral, the radius growing by `grow` mm on the way."""
    pts, total = [], int(round(n * turns))
    for i in range(1, total + 1):
        a = 2 * math.pi * i / n
        rr = r + grow * i / total
        u, v = rr * math.sin(a), r - rr * math.cos(a)
        pts.append(C(u, v, 0) if plane == "h" else C(0, u, v))
    return pts


def poly(pts_cm, scale):
    return [C(p[0] * scale * 10, p[1] * scale * 10, 0) for p in pts_cm]


def _star():
    out = []
    for i in range(1, 6):
        a = math.pi / 2 + i * 4 * math.pi / 5
        out.append((math.cos(a) - math.cos(math.pi / 2), math.sin(a) - math.sin(math.pi / 2)))
    return out


def _heart(n=20):
    out = []
    for i in range(1, n + 1):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        out.append((x / 16 * 1.0, (y - 5) / 16 * 1.0))
    return out


def _eight(n=24):
    return [(math.sin(2 * math.pi * i / n), math.sin(4 * math.pi * i / n) / 2) for i in range(1, n + 1)]


M = []


def mv(name, phrases, desc, steps, **kw):
    M.append(Move(name, phrases, desc, steps, **kw))


# --- turning the whole arm at its base
g = "turning at the base"
mv("turn_left", ["turn left", "turn to the left", "swivel left", "rotate base left", "turn base left", "swing left"],
   "the whole arm swivels left at its base", lambda a: [J(a)], kind="pose", unit="deg", default=20, lo=2, hi=45, group=g)
mv("turn_right", ["turn right", "turn to the right", "swivel right", "rotate base right", "turn base right", "swing right"],
   "the whole arm swivels right at its base", lambda a: [J(-a)], kind="pose", unit="deg", default=20, lo=2, hi=45, group=g)
mv("look_left", ["look left", "look to the left", "glance left"], "turns left for a moment, then back",
   lambda a: [J(a, 0, 0, 0, -10), W(1.0)], unit="deg", default=25, lo=5, hi=40, group=g)
mv("look_right", ["look right", "look to the right", "glance right"], "turns right for a moment, then back",
   lambda a: [J(-a, 0, 0, 0, -10), W(1.0)], unit="deg", default=25, lo=5, hi=40, group=g)
mv("look_around", ["look around", "look about", "have a look around"], "looks left, right, then back",
   lambda a: [J(a, 0, 0, 0, -10), W(0.6), J(-a, 0, 0, 0, -10), W(0.6)], unit="deg", default=25, lo=5, hi=40, group=g)
mv("scan", ["scan the desk", "scan the table", "scan", "sweep the desk", "survey the desk"],
   "slowly sweeps across the desk and back", lambda a: [J(a), J(-a), J(0)], unit="deg", default=25, lo=5, hi=40, group=g)
mv("point_left", ["point left", "point to the left"], "points left for a moment", lambda a: [J(a), W(1.5)],
   unit="deg", default=30, lo=5, hi=45, group=g)
mv("point_right", ["point right", "point to the right"], "points right for a moment", lambda a: [J(-a), W(1.5)],
   unit="deg", default=30, lo=5, hi=45, group=g)
mv("sway", ["sway", "rock", "rock back and forth", "sway side to side"], "sways side to side",
   lambda a: rep([J(a), J(-a)], 3), unit="deg", default=8, lo=2, hi=15, group=g)
mv("swing", ["swing", "swing around"], "swings and twists", lambda a: rep([J(a, 0, 0, 0, 0, 2 * a), J(-a, 0, 0, 0, 0, -2 * a)], 2),
   unit="deg", default=12, lo=3, hi=20, group=g)

# --- twisting the wrist
g = "twisting the wrist"
mv("rotate_left", ["rotate left", "rotate wrist left", "twist wrist left", "turn wrist left", "rotate counterclockwise",
                   "rotate wrist counterclockwise", "rotate anticlockwise", "twist left", "counterclockwise", "rotate counter clockwise"],
   "twists the wrist to the left (counterclockwise from above)", lambda a: [J(0, 0, 0, 0, 0, a)], kind="pose", unit="deg",
   default=45, lo=2, hi=270, group=g)
mv("rotate_right", ["rotate right", "rotate wrist right", "twist wrist right", "turn wrist right", "rotate clockwise",
                    "rotate wrist clockwise", "twist right", "clockwise"],
   "twists the wrist to the right (clockwise from above)", lambda a: [J(0, 0, 0, 0, 0, -a)], kind="pose", unit="deg",
   default=45, lo=2, hi=270, group=g)
mv("rotate", ["rotate", "rotate wrist", "rotate it", "turn it", "twist it", "twist wrist", "twist", "turn wrist"],
   "twists the wrist (counterclockwise; e.g. “rotate 90 degrees”)", lambda a: [J(0, 0, 0, 0, 0, a)], kind="pose", unit="deg",
   default=90, lo=2, hi=270, group=g)
mv("flip", ["flip", "flip it", "flip over", "flip wrist", "turn wrist around", "flip it over", "turn it around", "turn it over"],
   "turns the wrist half way round (180 degrees)", lambda a: [J(0, 0, 0, 0, 0, 180)], kind="pose", group=g)
mv("spin", ["spin", "spin wrist", "spin it", "do a spin", "twirl", "spin around", "twirl it"],
   "spins the wrist all the way round once", lambda a: [J(0, 0, 0, 0, 0, 120), J(0, 0, 0, 0, 0, 240), J(0, 0, 0, 0, 0, 360)],
   kind="pose", group=g)
mv("wiggle_wrist", ["wiggle wrist", "wiggle", "wiggle it", "twist back and forth", "jiggle wrist"],
   "wiggles the wrist back and forth", lambda a: rep([J(0, 0, 0, 0, 0, a), J(0, 0, 0, 0, 0, -a)], 2),
   unit="deg", default=30, lo=5, hi=60, group=g)
mv("straighten", ["straighten wrist", "straighten up", "straighten", "point down", "point straight down", "level wrist",
                  "face down", "level out"], "turns the hand to point straight down, staying where it is", lambda a: [DOWN],
   kind="pose", group=g)
mv("roll_left", ["roll left", "roll wrist left", "roll forearm left", "tip hand left"], "rolls the forearm to the left",
   lambda a: [J(0, 0, 0, a)], kind="pose", unit="deg", default=20, lo=2, hi=60, group=g)
mv("roll_right", ["roll right", "roll wrist right", "roll forearm right", "tip hand right"], "rolls the forearm to the right",
   lambda a: [J(0, 0, 0, -a)], kind="pose", unit="deg", default=20, lo=2, hi=60, group=g)

# --- tilting the hand
g = "tilting the hand"
mv("tilt_forward", ["tilt forward", "tilt wrist forward", "tilt up", "point forward", "lean forward", "tip forward", "point outward"],
   "tilts the hand so the fingers swing out, away from the base", lambda a: [TILT(a, 0)], kind="pose", unit="deg",
   default=20, lo=2, hi=60, group=g)
mv("tilt_back", ["tilt back", "tilt backward", "tilt wrist back", "lean back", "tip back", "tilt toward you"],
   "tilts the hand so the fingers swing back, toward the base", lambda a: [TILT(-a, 0)], kind="pose", unit="deg",
   default=20, lo=2, hi=60, group=g)
mv("tilt_left", ["tilt left", "tilt wrist left", "lean left", "tip left"], "tilts the hand so the fingers swing left",
   lambda a: [TILT(0, a)], kind="pose", unit="deg", default=20, lo=2, hi=60, group=g)
mv("tilt_right", ["tilt right", "tilt wrist right", "lean right", "tip right"], "tilts the hand so the fingers swing right",
   lambda a: [TILT(0, -a)], kind="pose", unit="deg", default=20, lo=2, hi=60, group=g)
mv("nod", ["nod", "nod yes", "say yes", "yes", "nod your head"], "nods yes", lambda a: rep([TILT(a, 0), TILT(-a, 0)], 2),
   unit="deg", default=12, lo=4, hi=25, group=g)
mv("shake_no", ["shake head", "say no", "no", "shake no", "shake your head", "nope"], "shakes its head no",
   lambda a: rep([J(5, 0, 0, 0, 0, a), J(-5, 0, 0, 0, 0, -a)], 2), unit="deg", default=25, lo=5, hi=45, group=g)
mv("maybe", ["maybe", "say maybe", "shrug", "i dont know", "not sure"], "shrugs: maybe",
   lambda a: rep([J(0, 0, 0, a), J(0, 0, 0, -a)], 2), unit="deg", default=15, lo=5, hi=30, group=g)
mv("wobble", ["wobble", "wobble around"], "wobbles its hand", lambda a: rep([TILT(a, a), TILT(-a, -a), TILT(a, -a), TILT(-a, a)], 1),
   unit="deg", default=10, lo=3, hi=20, group=g)

# --- moving the hand in a straight line
g = "moving the hand"
mv("move_forward", ["move forward", "reach forward", "reach out", "go forward", "forward", "extend", "move out", "reach further"],
   "moves the hand forward, away from the base", lambda a: [C(f=a * 10)], kind="pose", unit="cm", default=5, lo=0.5, hi=15, group=g)
mv("move_back", ["move back", "move backward", "move backwards", "pull back", "back up", "retract", "pull in", "move in",
                 "come back a bit"], "moves the hand back, toward the base", lambda a: [C(f=-a * 10)], kind="pose", unit="cm",
   default=5, lo=0.5, hi=15, group=g)
mv("move_left", ["move left", "slide left", "shift left", "go left", "scoot left", "move to the left", "slide to the left"],
   "moves the hand to the left", lambda a: [C(l=a * 10)], kind="pose", unit="cm", default=5, lo=0.5, hi=15, group=g)
mv("move_right", ["move right", "slide right", "shift right", "go right", "scoot right", "move to the right", "slide to the right"],
   "moves the hand to the right", lambda a: [C(l=-a * 10)], kind="pose", unit="cm", default=5, lo=0.5, hi=15, group=g)
mv("move_up", ["move up", "go up", "raise", "raise up", "lift up", "higher", "lift it up", "raise it", "lift it", "up",
               "hold it up", "raise your hand"], "moves the hand up", lambda a: [C(u=a * 10)], kind="pose", unit="cm",
   default=5, lo=0.5, hi=20, group=g)
mv("move_down", ["move down", "go down", "lower", "lower down", "lower it", "come down", "down", "lower your hand"],
   "moves the hand down (never closer than 5 cm to the table)", lambda a: [C(u=-a * 10)], kind="pose", unit="cm",
   default=5, lo=0.5, hi=20, group=g)
mv("present", ["present", "present it", "show it", "show me", "offer it", "hold it out", "show me it"],
   "holds its hand out forward and up, and stays", lambda a: [C(f=80, u=50)], kind="pose", group=g)
mv("undo", ["undo", "undo that", "go back to where you were", "back to where you were", "previous position", "undo last move"],
   "goes back to where it was before the last move", lambda a: [UNDO], kind="pose", group=g)
mv("wake_up", ["wake up", "get up", "rise and shine"], "goes back to its starting position",
   lambda a: [HOME], kind="pose", group=g)
mv("sleep", ["go to sleep", "sleep", "take a nap", "nap", "rest", "have a rest", "power down"],
   "droops down and rests (say “wake up” to bring it back)", lambda a: [C(u=-60), TILT(-20, 0)], kind="pose", group=g)

# --- the gripper
g = "the gripper"
mv("open_gripper", ["open gripper", "open hand", "open claw", "open fingers", "open up", "open", "open the gripper"],
   "opens the gripper (lets go of anything it holds)", lambda a: [G("open")], kind="pose", group=g)
mv("close_gripper", ["close gripper", "close hand", "close claw", "close fingers", "make a fist", "close", "squeeze", "grip",
                     "close the gripper"], "closes the gripper", lambda a: [G("close")], kind="pose", group=g)
mv("half_open", ["half open", "open halfway", "open half way", "open part way", "half open gripper"],
   "opens the gripper half way", lambda a: [G("half")], kind="pose", group=g)
mv("clap", ["clap", "clap hands", "snap", "chomp", "snap fingers", "click", "clap your hands"], "snaps the gripper shut and open",
   lambda a: rep([G("close"), G("open")], 3), opens=True, group=g)
mv("pinch", ["pinch", "pinch it"], "pinches: closes and opens once", lambda a: [G("close"), W(0.4), G("open")], opens=True, group=g)

# --- gestures
g = "gestures"
mv("high_five", ["high five", "give me five", "give me a high five", "up top"], "raises an open hand for a high five",
   lambda a: [C(u=80), G("open"), W(1.5)], opens=True, group=g)
mv("fist_bump", ["fist bump", "bump", "give me a fist bump", "bump it"], "closes its hand and bumps forward",
   lambda a: [G("close"), C(f=60), W(0.3)], group=g)
mv("salute", ["salute", "give a salute"], "salutes", lambda a: [TILT(25, 0), J(0, 0, 0, 20), W(1.2)], group=g)
mv("bow", ["bow", "take a bow", "bow down"], "takes a bow", lambda a: [C(u=-50), TILT(15, 0), W(1.0)], group=g)
mv("stretch", ["stretch", "stretch out", "have a stretch"], "stretches up", lambda a: [C(u=110), TILT(15, 0), W(1.0)], group=g)
mv("shiver", ["shiver", "shake", "tremble", "shake it", "jiggle", "shudder", "jitter"], "shivers",
   lambda a: rep([J(0, 0, 0, 0, 0, a), J(0, 0, 0, 0, 0, -a)], 5), unit="deg", default=6, lo=2, hi=12, group=g)
mv("celebrate", ["celebrate", "hooray", "yay", "victory"], "celebrates: up, then a happy twist",
   lambda a: [C(u=60)] + rep([J(0, 0, 0, 0, 0, 50), J(0, 0, 0, 0, 0, -50)], 2), group=g)
mv("knock", ["knock", "knock knock", "tap", "tap the desk", "tap the table", "knock on the table"], "knocks: taps down and up",
   lambda a: rep([C(u=-30), C(u=0)], 3), group=g)
mv("drum", ["drum", "drum roll", "drum on the table"], "a quick drum roll", lambda a: rep([C(u=-15), C(u=0)], 6), group=g)
mv("think", ["think", "thinking", "ponder", "think about it", "let me think"], "strikes a thinking pose",
   lambda a: [J(0, 0, 0, 15), TILT(-15, 0), W(2.0)], group=g)
mv("bounce", ["bounce", "jump", "hop", "bounce up and down"], "bounces up and down",
   lambda a: rep([C(u=a * 10), C(u=0)], 3), unit="cm", default=4, lo=1, hi=8, group=g)
mv("peekaboo", ["peek a boo", "play peek a boo"], "hides, then pops back with a snap",
   lambda a: [C(f=-80, u=-40), W(1.2), C(), G("close"), G("open")], opens=True, group=g)
mv("hide", ["hide", "go hide", "hide away"], "pulls back and ducks for a moment",
   lambda a: [C(f=-80, u=-40), TILT(-20, 0), W(2.0)], group=g)
mv("peek", ["peek", "sneak a peek", "take a peek"], "peeks around", lambda a: [J(15, 0, 0, 0, -15), W(1.0)], group=g)
mv("point_forward", ["point", "point ahead", "point straight ahead", "point out there"], "points forward for a moment",
   lambda a: [C(f=80), W(1.5)], group=g)
mv("shake_hands", ["shake hands", "handshake", "shake my hand", "lets shake hands"], "reaches out and shakes hands",
   lambda a: [C(f=50)] + rep([C(f=50, u=20), C(f=50, u=-20)], 3), group=g)
mv("count", ["count to three", "count", "count three"], "counts to three with nods",
   lambda a: rep([TILT(12, 0), TILT(0, 0), W(0.5)], 3), group=g)
mv("bye", ["bye", "goodbye", "bye bye", "see you", "see you later", "wave goodbye"], "a bye-bye wiggle",
   lambda a: rep([J(0, 0, 0, 0, 0, 35), J(0, 0, 0, 0, 0, -35)], 3), group=g)
mv("sneeze", ["sneeze", "bless you"], "a robot sneeze", lambda a: [C(f=-30), W(0.4), C(f=40), W(0.2)], group=g)
mv("yawn", ["yawn", "be sleepy", "sleepy"], "a big slow yawn", lambda a: [C(u=60), TILT(20, 0), W(1.0)], group=g)
mv("sigh", ["sigh", "be tired", "tired"], "a sigh: sinks a little, then back", lambda a: [C(u=-30), W(1.0)], group=g)
mv("excited", ["be excited", "excited", "get excited"], "bounces with excitement", lambda a: rep([C(u=40), C(u=0)], 4), group=g)
mv("shy", ["be shy", "shy", "act shy"], "turns away shyly", lambda a: [J(-15, 0, 0, 0, 15), C(f=-30), W(1.5)], group=g)
mv("sad", ["be sad", "sad", "act sad", "look sad"], "droops sadly", lambda a: [C(u=-50), TILT(-25, 0), W(1.5)], group=g)
mv("happy", ["be happy", "happy", "act happy", "look happy"], "a happy wiggle",
   lambda a: [C(u=40)] + rep([J(0, 0, 0, 0, 0, 30), J(0, 0, 0, 0, 0, -30)], 2), group=g)
mv("angry", ["be angry", "angry", "act angry", "mad", "get mad"], "an angry shake",
   lambda a: [C(f=30)] + rep([J(0, 0, 0, 0, 0, 10), J(0, 0, 0, 0, 0, -10)], 4), group=g)
mv("surprised", ["be surprised", "surprised", "whoa", "wow", "act surprised"], "jumps back in surprise",
   lambda a: [C(f=-60, u=30), G("open"), W(1.0)], opens=True, group=g)
mv("confused", ["be confused", "confused", "huh", "act confused"], "tilts its head, confused",
   lambda a: rep([J(0, 0, 0, 20), J(0, 0, 0, -20)], 2), group=g)
mv("dizzy", ["be dizzy", "dizzy", "get dizzy"], "wobbles in dizzy circles", lambda a: circle(30, 12) + circle(30, 12), group=g)
mv("dance_hand", ["hand dance", "dance your hand", "groove your hand"], "a little hand dance",
   lambda a: rep([C(l=30, u=20), C(l=-30, u=20), C()], 2), group=g)

# --- drawing in the air (flat, at the hand's height, unless said otherwise)
g = "drawing in the air"
mv("draw_circle", ["draw a circle", "circle", "make a circle", "trace a circle", "go in a circle"], "draws a circle",
   lambda a: circle(a * 10, 16), unit="cm", default=4, lo=1, hi=8, group=g)
mv("draw_circle_air", ["draw a circle in the air", "vertical circle", "circle in the air", "draw a standing circle"],
   "draws a standing circle", lambda a: circle(a * 10, 16, plane="v"), unit="cm", default=4, lo=1, hi=8, group=g)
mv("draw_square", ["draw a square", "square", "make a square", "trace a square"], "draws a square",
   lambda a: poly([(1, 0), (1, 1), (0, 1), (0, 0)], a), unit="cm", default=6, lo=1, hi=10, group=g)
mv("draw_triangle", ["draw a triangle", "triangle", "make a triangle"], "draws a triangle",
   lambda a: poly([(1, 0), (0.5, 0.87), (0, 0)], a), unit="cm", default=6, lo=1, hi=10, group=g)
mv("draw_star", ["draw a star", "star", "make a star"], "draws a five-pointed star", lambda a: poly(_star(), a / 2),
   unit="cm", default=6, lo=2, hi=10, group=g)
mv("draw_heart", ["draw a heart", "heart", "make a heart"], "draws a heart", lambda a: poly(_heart(), a / 2),
   unit="cm", default=6, lo=2, hi=10, group=g)
mv("draw_eight", ["draw a figure eight", "figure eight", "figure 8", "draw an eight", "infinity", "draw infinity"],
   "draws a figure eight", lambda a: poly(_eight(), a / 2), unit="cm", default=6, lo=2, hi=10, group=g)
mv("draw_zigzag", ["draw a zigzag", "zigzag", "zig zag"], "draws a zigzag",
   lambda a: poly([(0.25, 0.3), (0.5, 0), (0.75, 0.3), (1, 0), (0, 0)], a), unit="cm", default=8, lo=2, hi=12, group=g)
mv("draw_spiral", ["draw a spiral", "spiral", "make a spiral"], "draws a spiral", lambda a: circle(a * 3, 12, turns=2, grow=a * 7),
   unit="cm", default=4, lo=1, hi=7, group=g)
mv("draw_wave", ["draw a wave", "squiggle", "draw a squiggle", "wavy line"], "draws a wavy line",
   lambda a: poly([(0.25, 0.25), (0.5, 0), (0.75, -0.25), (1, 0), (0, 0)], a), unit="cm", default=8, lo=2, hi=12, group=g)
mv("draw_line", ["draw a line", "line", "straight line"], "draws a straight line and back", lambda a: poly([(0, 1), (0, 0)], a),
   unit="cm", default=8, lo=2, hi=12, group=g)
mv("draw_x", ["draw an x", "draw a cross", "cross", "make an x"], "draws an X",
   lambda a: poly([(1, 1), (0.5, 0.5), (1, 0), (0, 1), (0.5, 0.5), (0, 0)], a), unit="cm", default=6, lo=2, hi=10, group=g)
mv("draw_check", ["draw a check mark", "check mark", "draw a tick", "tick"], "draws a check mark",
   lambda a: poly([(-0.3, -0.3), (0.7, 0.4), (0, 0)], a), unit="cm", default=6, lo=2, hi=10, group=g)

_BY_NAME = {m.name: m for m in M}
for m in M:
    m.words = [set(w for w in _numbers(p) if w not in _FILLER) or set(_numbers(p)) for p in m.phrases]


# ---------------------------------------------------------------- reading a request

def _m(router):
    return sys.modules[type(router).__module__]


def _cfg(router) -> Dict[str, Any]:
    return getattr(router, "_moves_cfg", None) or {}


def _arm_keys(router) -> List[str]:
    return list((getattr(router, "_arms", None) or {}).keys())


def _pick_arms(router, words: List[str]) -> List[str]:
    keys = _arm_keys(router)
    if not keys:
        return []
    picker = getattr(router, "picker", None)
    default = picker if picker in keys else keys[0]
    if "both" in words and len(keys) > 1:
        return keys
    for i, w in enumerate(words[:-1]):
        if w == "arm" and words[i + 1].isdigit():
            rn = router._arm_by_number(words[i + 1])
            if rn:
                return [rn]
    if "other" in words and len(keys) > 1:
        return [next(k for k in keys if k != default)]
    return [default]


def _match_move(content: List[str]) -> Optional[Tuple[Move, int]]:
    """The move whose phrase covers what was said: every word of the phrase is there, and everything
    else said is filler, a number, an amount or speed word. The longest phrase wins."""
    cs = set(w for w in content if not w.isdigit() and w not in _FILLER)
    best = None
    for m in M:
        for ws in m.words:
            if ws and ws <= set(content) and cs <= ws:
                if best is None or len(ws) > best[1]:
                    best = (m, len(ws))
    return best


def _amount(m: Move, words: List[str]) -> Tuple[float, str]:
    """(amount in the move's unit, a note if it had to be limited)."""
    if not m.unit:
        return 0.0, ""
    val, note = None, ""
    for i, w in enumerate(words):
        if w.isdigit():
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if nxt in ("times", "time"):
                continue
            if i > 0 and words[i - 1] == "arm":
                continue
            unit = _UNITS.get(nxt)
            v = float(w)
            if m.unit == "cm":
                v = v * 2.54 if unit == "in" else v / 10 if unit == "mm" else v
            elif unit in ("cm", "in", "mm"):
                continue
            val = v
            break
    if val is None:
        val = m.default * (0.4 if set(words) & _SMALL else 2.0 if set(words) & _BIG else 1.0)
    clamped = max(m.lo, min(m.hi, val))
    if abs(clamped - val) > 1e-6:
        u = "degrees" if m.unit == "deg" else "cm"
        note = f"limited to {clamped:g} {u} (asked for {val:g})"
    return clamped, note


def _times(words: List[str]) -> int:
    if "twice" in words:
        return 2
    if "thrice" in words:
        return 3
    for i, w in enumerate(words[:-1]):
        if w.isdigit() and words[i + 1] in ("times", "time"):
            return max(1, min(5, int(w)))
    return 1


_BODY = {"hand", "hands", "wrist", "gripper", "claw", "arm"}


def read(router, text: str) -> Optional[Dict[str, Any]]:
    """`text` -> a move command, or None if it isn't one."""
    words = _numbers(text)
    hit = _match_move(words)
    if hit is None:     # "move the hand up", "raise the gripper": the body part is understood
        hit = _match_move([w for w in words if w not in _BODY])
    if hit is None:
        return None
    m = hit[0]
    amount, note = _amount(m, words)
    arms = _pick_arms(router, words)
    if not arms:
        return None
    speed = 0.5 if set(words) & _SLOW else 1.6 if set(words) & _FAST else 1.0
    names = [(router._arms.get(rn) or {}).get("name", rn) for rn in arms]
    amt = f" {amount:g} {'degrees' if m.unit == 'deg' else 'cm'}" if m.unit else ""
    nice = m.phrases[0]
    label = f"{_m(router)._cap(' and '.join(names))}: {nice}{amt}"
    return router._mk_cmd(action="move", move=m.name, amount=amount, note=note, speed=speed, times=_times(words),
                          arms=arms, resource_name=arms[0], name=" and ".join(names), label=label)


def _split(text: str) -> Optional[List[str]]:
    t = " " + " ".join(_numbers(text)) + " "
    parts = [p.strip() for p in re.split(r"\b(?:and then|and|then|after that)\b", t) if p.strip()]
    return parts if len(parts) > 1 else None


def understand(router, words: List[str], text: str) -> Optional[Dict[str, Any]]:
    if getattr(router, "_moves_busy_parse", False):
        return None
    whole = read(router, text)           # "rock back and forth" is one move, not a chain
    if whole is not None:
        return whole
    parts = _split(text)
    if parts is None:
        return None
    # a chain: at least one part must be a move; every part must be something Buddy knows
    steps, any_move = [], False
    router._moves_busy_parse = True
    llm = getattr(router, "_llm", None)          # each part goes through the rules, never the model
    was = llm.get("skip") if llm else None
    if llm:
        llm["skip"] = True
    try:
        for p in parts[:4]:
            c = read(router, p)
            if c is not None:
                any_move = True
            else:
                c, score = router._match(p)
                if score < 100:
                    c = router._understand(p)
            if c is None or c.get("action") in ("stop", "llm", "nlu_mode"):
                return None
            steps.append((p, c))
    finally:
        router._moves_busy_parse = False
        if llm:
            llm["skip"] = was
    if not any_move:
        return None
    label = " → ".join(router._label(c) for _, c in steps)
    return router._mk_cmd(action="move_chain", steps=steps, resource_name=steps[0][1].get("resource_name", ""),
                          name=steps[0][1].get("name", ""), label=label)


# ---------------------------------------------------------------- planning and checking

def _tip(T, finger):
    return T[:3, 3] + T[:3, 2] * finger


class _Ctx:
    def __init__(self, router, rn, q0, ok_kin):
        v = getattr(router, "vision", None) or {}
        cfg = _cfg(router)
        self.finger = float(v.get("finger_mm", 172))
        self.table_z = float(v.get("table_z_mm", -23))
        self.clear = float(cfg.get("clearance_mm", 50))
        self.travel = float(cfg.get("max_travel_mm", 350))
        self.rmin = float(v.get("reach_min_mm", 150)) - 30
        self.rmax = float(v.get("reach_max_mm", 750)) + 50
        ko = (cfg.get("keep_out") or {}).get(rn)
        if ko is None:
            ko = _DEFAULT_KEEP_OUT.get(rn, [])
        self.boxes = [list(map(float, b)) for b in ko]
        self.q0, self.ok_kin = q0, ok_kin
        self.T0 = kin.fk(q0)
        self.tip0 = _tip(self.T0, self.finger)
        r = np.array([self.T0[0, 3], self.T0[1, 3], 0.0])
        n = float(np.linalg.norm(r))
        self.fwd = r / n if n > 1 else np.array([1.0, 0.0, 0.0])
        self.left = np.cross([0.0, 0.0, 1.0], self.fwd)

    def problem(self, q) -> Optional[str]:
        for j, (lo, hi) in enumerate(kin.LIMITS):
            if not lo + 3 <= q[j] <= hi - 3:
                return f"joint {j + 1} would go past its limit"
        if not self.ok_kin:
            return None
        T = kin.fk(q)
        tip, wrist = _tip(T, self.finger), T[:3, 3]
        if tip[2] < self.table_z + self.clear or wrist[2] < self.table_z + self.clear:
            return "the fingers would come too close to the table"
        r = math.hypot(tip[0], tip[1])
        if r > self.rmax:
            return "it would reach out too far"
        if r < self.rmin:
            return "it would come too close to its own base"
        if float(np.linalg.norm(tip - self.tip0)) > self.travel:
            return "it would go too far from where it is"
        for b in self.boxes:
            for p in (tip, wrist):
                if b[0] - 40 <= p[0] <= b[1] + 40 and b[2] - 40 <= p[1] <= b[3] + 40 and b[4] - 40 <= p[2] <= b[5] + 40:
                    return "it would hit something next to it (the desk edge or the other arm)"
        return None


# The obstacles next to arm 17 in its machine's config (desk17-right-edge, arm18-base), as boxes in its frame.
_DEFAULT_KEEP_OUT = {"arm17-arm": [[-300, 800, -480, -380, -123, 977], [-172, 178, 803, 1153, 0, 700]]}


def _joint_path(ctx, qa, qb, why_out) -> Optional[List[List[float]]]:
    """Check a joint-space move from qa to qb (the arm interpolates joints evenly) at every few degrees."""
    n = max(1, int(math.ceil(max(abs(b - a) for a, b in zip(qa, qb)) / 3.0)))
    for i in range(1, n + 1):
        q = [a + (b - a) * i / n for a, b in zip(qa, qb)]
        p = ctx.problem(q)
        if p:
            why_out.append(p)
            return None
    return [list(qb)]


def _cart_target(ctx, off, R=None):
    T = ctx.T0.copy() if R is None else ctx.T0.copy()
    if R is not None:
        T[:3, :3] = R
    T[:3, 3] = ctx.T0[:3, 3] + ctx.fwd * off[0] + ctx.left * off[1] + np.array([0, 0, 1.0]) * off[2]
    return T


def _rot(axis, deg):
    axis = np.asarray(axis, float)
    axis = axis / np.linalg.norm(axis)
    a = math.radians(deg)
    K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
    return np.eye(3) + math.sin(a) * K + (1 - math.cos(a)) * K @ K


def _cart_path(ctx, q, T, why_out) -> Optional[List[List[float]]]:
    """Straight line from where q puts the hand to T, checked at every waypoint."""
    if not ctx.ok_kin:
        why_out.append("its kinematics don't match where it is, so it can only turn at the base or twist the wrist")
        return None
    wps = kin.line(q, T, step_mm=8.0, step_deg=4.0)
    if wps is None:
        why_out.append("it can't move its hand there in a straight line")
        return None
    for w in wps:
        p = ctx.problem(w)
        if p:
            why_out.append(p)
            return None
    return wps


def _plan(router, rn, m: Move, amount: float, times: int, q0, ok_kin) -> Tuple[Optional[List[tuple]], str, List[float]]:
    """(actions, why, end joints): actions are ("joints", [waypoints], gesture?), ("grip", op), ("wait", s), ("home",)."""
    ctx = _Ctx(router, rn, q0, ok_kin)
    segs = list(m.steps(amount))
    if m.kind == "gesture" and times > 1:
        segs = [s for _ in range(times) for s in segs + [START]]
    if m.kind == "gesture":
        segs = segs + [START]
    q, R, acts, why = list(q0), ctx.T0[:3, :3].copy(), [], []
    off = np.zeros(3)
    for s in segs:
        kind = s[0]
        if kind == "j":
            d = s[1]
            if not ok_kin and any(abs(d[j]) > 1e-9 for j in (1, 2, 3, 4)):
                return None, "its kinematics don't match where it is, so it can only turn at the base or twist the wrist", q0
            qb = [a + b for a, b in zip(q0, d)]
            if m.name == "spin" and not (kin.LIMITS[5][0] + 3 <= qb[5] <= kin.LIMITS[5][1] - 3):
                qb[5] = q0[5] - d[5]                   # spin the other way round if that one hits the limit
            path = _joint_path(ctx, q, qb, why)
            if path is None:
                return None, why[0], q0
            acts.append(("joints", path)); q = qb
            T = kin.fk(q); R = T[:3, :3].copy()
            off = np.array([(T[:3, 3] - ctx.T0[:3, 3]) @ ctx.fwd, (T[:3, 3] - ctx.T0[:3, 3]) @ ctx.left, T[2, 3] - ctx.T0[2, 3]])
        elif kind in ("c", "tilt", "down"):
            if kind == "c":
                off = np.array(s[1])
                Rt = R
            elif kind == "tilt":
                fwd_deg, left_deg = s[1]
                # fingers swing forward = turn about the "left" axis; swing left = turn about "back" axis
                Rt = _rot(ctx.left, -fwd_deg) @ _rot(ctx.fwd, left_deg) @ ctx.T0[:3, :3]
                # pivot at the wrist: the flange stays put
            else:   # point straight down, keeping the hand's heading
                cur = kin.fk(q)
                xa = np.array([cur[0, 0], cur[1, 0], 0.0])
                xa = xa / np.linalg.norm(xa) if np.linalg.norm(xa) > 0.3 else np.array([1.0, 0, 0])
                Rt = kin.pose_down(0, 0, 0, xa)[:3, :3]
            T = _cart_target(ctx, off, Rt)
            path = _cart_path(ctx, q, T, why)
            if path is None:
                return None, why[0], q0
            acts.append(("joints", path)); q = path[-1]; R = Rt
        elif kind == "w":
            acts.append(("wait", s[1]))
        elif kind == "g":
            acts.append(("grip", s[1]))
        elif kind == "start":
            path = _joint_path(ctx, q, q0, why)
            if path is None:
                return None, why[0], q0
            acts.append(("joints", path)); q = list(q0); R = ctx.T0[:3, :3].copy(); off = np.zeros(3)
        elif kind == "home":
            acts.append(("home",))
        elif kind == "undo":
            prev = (getattr(router, "_moves_prev", None) or {}).get(rn)
            if prev is None:
                return None, "there's no earlier position to go back to", q0
            path = _joint_path(ctx, q, prev, why)
            if path is None:
                return None, why[0], q0
            acts.append(("joints", path)); q = list(prev)
    return acts, "", q


# ---------------------------------------------------------------- running

async def _kin_ok(arm, q0) -> bool:
    try:
        here = await arm.get_end_position()
        return math.dist(kin.fk(q0)[:3, 3], (here.x, here.y, here.z)) <= 5
    except Exception:
        return False


async def _do_one(router, rn: str, m: Move, cmd: Dict[str, Any]) -> str:
    M_ = _m(router)
    info = router._arms.get(rn) or {}
    arm, name = info.get("arm"), M_._cap(info.get("name", rn))
    if arm is None:
        raise ValueError(f"{name} isn't connected")
    if rn in (getattr(router, "_manual", None) or {}):
        raise ValueError(f"{name} is in manual mode — say “manual mode off” first")
    g = (getattr(router, "grabbers", None) or {}).get(rn)
    if m.opens and g is not None and g.get("holding"):
        raise ValueError(f"{name} would drop the {g['holding']} — say “put it back” first")
    if any(s[0] == "g" for s in m.steps(cmd.get("amount", 0))) and (g is None or g.get("gripper") is None):
        raise ValueError(f"{name} has no gripper set up")
    q0 = [float(x) for x in (await arm.get_joint_positions()).values]
    if len(q0) != 6:
        raise ValueError(f"{name} isn't a 6-joint arm")
    ok = await _kin_ok(arm, q0)
    acts, why, q_end = await asyncio.to_thread(_plan, router, rn, m, float(cmd.get("amount") or 0),
                                               int(cmd.get("times") or 1), q0, ok)
    if acts is None:
        hint = " — say “move up” first" if "table" in why else ""
        raise ValueError(f"{name} won't {m.phrases[0]}: {why}{hint}")
    cfg = _cfg(router)
    base = float(cfg.get("speed_deg_s", 45))
    speed = (base if m.kind == "pose" else min(150.0, base * 1.8)) * float(cmd.get("speed") or 1.0)
    speed = max(8.0, min(150.0, speed))
    if m.kind == "pose" and m.name not in ("undo",):
        router.__dict__.setdefault("_moves_prev", {})[rn] = q0
    batch: List[List[float]] = []

    async def flush():
        if batch:
            await router._move_line(arm, list(batch), speed)
            batch.clear()

    for a in acts:
        if a[0] == "joints":
            batch.extend(a[1])
            continue
        await flush()
        if a[0] == "wait":
            await asyncio.sleep(a[1] / max(0.5, float(cmd.get("speed") or 1.0)))
        elif a[0] == "grip":
            await _grip(router, g, a[1])
        elif a[0] == "home":
            await router._go_home({"resource": arm, "resource_name": rn, "name": info.get("name", rn)})
    await flush()
    return name


async def _grip(router, g, op: str) -> None:
    gr = g["gripper"]
    if op == "open":
        await gr.open()
        if g.get("holding"):
            g["holding"] = None
            if hasattr(router, "_remember_pick"):
                router._remember_pick(g, None)
    elif op == "close":
        caught = await router._squeeze(g) if hasattr(router, "_squeeze") else await gr.grab()
        if caught and not g.get("holding"):
            g["holding"] = "something"
    elif op == "half":
        await gr.do_command({"set": 425})


async def run(router, cmd: Dict[str, Any], text: str) -> Dict[str, Any]:
    M_ = _m(router)
    if cmd["action"] == "move_chain":
        from .llm import _dispatch                      # runs any command the way the router's _run does
        for i, (phrase, step) in enumerate(cmd["steps"]):
            router._action_arms = list(dict.fromkeys(router._arms_for(step) or step.get("arms") or []))
            router._set_phase("running", current=f"{router._label(step)} ({i + 1} of {len(cmd['steps'])})")
            await _dispatch(router, {**step, "rec": cmd.get("rec", {})}, phrase)
        router._current = f"Done: {cmd['label']}"
        cmd["keep_current"] = True
        return {"chain": [p for p, _ in cmd["steps"]]}
    m = _BY_NAME[cmd["move"]]
    arms = cmd.get("arms") or [cmd["resource_name"]]
    if m.kind == "pose" and len(arms) > 1:
        arms = arms[:1]                                  # "both arms" is for gestures
    router._action_arms = list(arms)
    router._set_phase("running", current=cmd["label"])
    if cmd.get("note"):
        M_.LOGGER.info(f"move {m.name}: {cmd['note']}")
    res = await asyncio.gather(*[_do_one(router, rn, m, cmd) for rn in arms], return_exceptions=True)
    for r in res:
        if isinstance(r, asyncio.CancelledError):
            raise r
    errs = [r for r in res if isinstance(r, Exception)]
    if errs and len(errs) == len(res):
        raise errs[0]
    done = [r for r in res if isinstance(r, str)]
    router._current = f"{' and '.join(done)}: {m.phrases[0]}" + (f" ({cmd['note']})" if cmd.get("note") else "") + \
        (f" — {'; '.join(M_._friendly_error(e) for e in errs)}" if errs else "")
    cmd["keep_current"] = True
    return {"move": m.name, "arms": done}


def arms_for(router, cmd: Dict[str, Any]) -> List[str]:
    if cmd["action"] == "move_chain":
        return []
    return list(cmd.get("arms") or [cmd.get("resource_name")])


def label(cmd: Dict[str, Any]) -> str:
    return cmd.get("label") or "Moving"


# ---------------------------------------------------------------- for the LLM, the page, DoCommand

def catalogue(router) -> List[str]:
    """One line per group of moves, for smart mode's list of commands."""
    out = ["arm moves — write the first phrase shown; add an amount (\"30 degrees\", \"5 centimeters\", \"a little\", "
           "\"a lot\"), a speed (\"slowly\", \"quickly\"), repeats (\"twice\") or the arm (\"arm 18\", \"both arms\") as needed:"]
    groups: Dict[str, List[str]] = {}
    for m in M:
        groups.setdefault(m.group, []).append(m.phrases[0])
    for gname, bits in groups.items():
        out.append(f"  {gname}: " + "; ".join(bits))
    return out


async def _do(router, command):
    v = command.get("moves")
    if v == "plan":
        c = understand(router, _numbers(str(command.get("text") or "")), str(command.get("text") or ""))
        if c is None:
            return {"understood": False}
        if c["action"] == "move_chain":
            return {"understood": True, "chain": [{"text": p, "action": s.get("action"), "move": s.get("move"),
                                                   "amount": s.get("amount")} for p, s in c["steps"]]}
        out = {"understood": True, "move": c["move"], "amount": c["amount"], "arms": c["arms"], "speed": c["speed"],
               "times": c["times"], "note": c["note"]}
        m = _BY_NAME[c["move"]]
        for rn in c["arms"]:
            arm = router._arms[rn]["arm"]
            q0 = [float(x) for x in (await arm.get_joint_positions()).values]
            acts, why, _ = _plan(router, rn, m, c["amount"], c["times"], q0, await _kin_ok(arm, q0))
            out.setdefault("plans", {})[rn] = {"ok": acts is not None, "why": why,
                                               "steps": len(acts or []), "waypoints": sum(len(a[1]) for a in acts or [] if a[0] == "joints")}
        return out
    return {"count": len(M), "moves": [{"name": m.name, "kind": m.kind, "group": m.group, "phrases": m.phrases,
                                        "does": m.desc, "unit": m.unit, "default": m.default} for m in M]}


def configure(router, attrs: Dict[str, Any]) -> None:
    router._moves_cfg = dict(attrs.get("moves") or {})


FEATURE = Feature(
    name="moves",
    actions=("move", "move_chain"),
    words=tuple(sorted({("don't" if w == "dont" else w) for m in M for p in m.phrases for w in p.split()} |
                       set(_ONES) | set(_TENS) | {"hundred", "degrees", "centimeters", "inches", "millimeters", "times",
                                                  "twice", "little", "bit", "lot", "slowly", "quickly", "both", "other"})),
    priority=50,                 # before the other features: a move must be whole ("move the cup ..." isn't one)
    configure=configure,
    understand=understand,
    run=run,
    arms_for=arms_for,
    label=label,
    do_commands={"moves": _do},
    catalogue=catalogue,
)
