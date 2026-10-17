"""
voice-router (Buddy Bot): a Viam generic service that turns "Hey buddy, <command>" into DoCommand calls.

Pipeline on the machine (listen_mode "wake", the default):
    system-audio microphone (raw_mic / level_mic)  ->  voice-router (this)
The router transcribes the microphone locally with Vosk and watches for the wake word
("hey buddy"). Once it hears it, it plays a chime and takes the next few seconds as the
command; if nothing clear comes in time it plays the "off" chime and goes back to waiting
for the wake word. A finished command plays the "done" chime. Any error is logged and
the router goes back to waiting for the wake word. A mute switch on the status page (or
DoCommand {"mic": false}) turns the wake word off; "stop" is still heard while an arm moves.

Other modes: "wake_word" uses the external wake-word filter (self.mic) to decide when a
command is being said; "toggle" is tap-to-talk from the status page.

Config attributes:
{
  "mic": "wake-mic",                         # audio_in (the wake-word filter)   [required]
  "vosk_model": "vosk-model-small-en-us-0.15",  # model name (auto-download) or local path
  "match_threshold": 80,                     # 0-100 fuzzy score needed to accept a match
  "listen_mode": "wake",                     # "wake" (default) | "wake_word" (external filter) | "toggle"
  "wake_word": "hey buddy",                  # what wakes Buddy up in "wake" mode
  "command_window_s": 5,                     # seconds to say the command after the wake word
  "sounds": true,                            # play the wake / timeout / done chimes (sounds/*.mp3)
  "use_grammar": true,                       # restrict Vosk to the command phrases (more accurate)
  "live_camera": "a17-depth-cam",            # optional: camera shown live on the status page
  "live_fps": 6,                             #   frames per second while a page is watching
  "live_depth_range_mm": [300, 1500],        #   optional fixed depth color range (default: auto)
  "record": {                               # optional: film a camera while a voice command moves an arm
    "camera": "a17-cam", "fps": 10,         #   saved to <project>/recordings/<time>_<command>.mp4
    "idle_s": 10, "max_s": 600              #   ends once no arm has moved for idle_s seconds
  },
  "record_on_motion": [                      # optional: film a camera whenever an arm moves (any cause)
    {"camera": "cam", "arm": "arm", "label": "Arm 18 camera", "dir": "arm-18", "idle_s": 10, "fps": 10},
    {"camera": "depth-cam", "arms": ["arm", "a17-arm"], "label": "Table camera", "dir": "table"}
  ],                                         #   saved to <project>/recordings/<dir>/<time>_....mp4
  "commands": [                              # [required]
    {
      "phrases": ["play shell game", "start shell game", "shell game"],
      "resource": "shell-game",              # any resource on this machine (or remote:name)
      "command": {"start": true}             # payload passed to its DoCommand
    },
    {
      "phrases": ["wave arm seventeen", "wave arm 17", "wave"],
      "resource": "arm",                     # an arm component (here from the armfarm17 remote)
      "action": "wave",                      # built-in action instead of DoCommand
      "joint": 4,                            # 0-based joint index to wiggle (4 = wrist pitch)
      "amplitude_deg": 15,
      "times": 3,
      "speed_deg_s": 150,                    # wave speed, for this wave only (the arm's own speed is untouched)
      "accel_deg_s2": 1000                   #   and acceleration; xArm limits: 3-180 deg/s, <= 1145 deg/s^2
    }
  ]
}

Each command uses either "command" (DoCommand payload) or "action" (built-in: "wave").

DoCommand (for testing without speaking):
  {"simulate": "play shell game"}  -> routes the text as if it were heard
  {"status": true}                 -> listener state, last transcript, last match
"""

import asyncio
import contextlib
import json
import math
import os
import re
import time
from collections import deque
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

from typing_extensions import Self
from viam.components.arm import Arm, JointPositions
from viam.components.audio_in import AudioIn
from viam.logging import getLogger
from viam.module.module import Module
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.services.generic import Generic
from viam.utils import ValueTypes, struct_to_dict

from viam.proto.common import Pose

from . import identify, kin, recorder, sounds, status_page, vision

LOGGER = getLogger(__name__)
SAMPLE_RATE = 16000
_ACTIVE_TASK: Optional[asyncio.Task] = None  # the one running listener in this process
_LEVEL_TASK: Optional[asyncio.Task] = None   # the one running sound-level reader
_LIVE_TASKS: List[asyncio.Task] = []         # the running live-camera readers (one per camera)
_MIC = {"on": None}   # mute switch; None until the first config picks the default (on in "wake" mode, off in "toggle")
_ARMS_TASK: Optional[asyncio.Task] = None    # the one running arm-state poller (status page)
_REC: Optional["recorder.Recorder"] = None   # records a camera while voice commands move the arms
_MOTION_RECS: Dict[str, "recorder.MotionRecorder"] = {}   # camera -> films it whenever its arm moves
# Taught wave poses live next to the code (~/Dev/voice-router/wave_poses.json) so they survive restarts.
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSES_FILE = os.path.join(_HERE, "wave_poses.json")
CALIB_FILE = os.path.join(_HERE, "camera_calibration.json")
MODEL_DIR = os.path.join(_HERE, "models", "clip")
_IDENTIFIER: Optional["identify.Identifier"] = None   # one object-naming model per process


def _identifier() -> "identify.Identifier":
    global _IDENTIFIER
    if _IDENTIFIER is None:
        _IDENTIFIER = identify.Identifier(MODEL_DIR, LOGGER)
    return _IDENTIFIER


class NotFound(Exception):
    """The camera looked and the requested block isn't there."""


def _load_poses() -> Dict[str, Any]:
    try:
        with open(POSES_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        LOGGER.warning(f"could not read {POSES_FILE}: {e}")
        return {}


def _save_poses(poses: Dict[str, Any]) -> None:
    tmp = POSES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(poses, f, indent=2)
    os.replace(tmp, POSES_FILE)


MAX_GRIP_MM = 80   # the xArm gripper opens to ~85 mm; anything wider can't be picked up


def _grab_arm_specs(vis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The arms that can grab: 'vision.arms' (a list), or the single 'vision.arm'/'vision.gripper'."""
    if vis.get("arms"):
        return [dict(a) for a in vis["arms"]]
    if vis.get("arm") or vis.get("gripper"):
        return [{"arm": vis.get("arm"), "gripper": vis.get("gripper"), "name": vis.get("name")}]
    return []


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


# ---- understanding free-form commands ("pick up the red ball", "go back to the start") ----
# The first intent whose words appear wins, so "stop" beats everything and "put it down" isn't a grab.
_INTENTS = [
    ("stop", ["stop", "halt", "freeze", "abort", "cancel"]),
    ("release", ["let go", "letting go", "release", "drop", "open the gripper", "open gripper", "open your hand",
                 "put it down", "put down", "set it down"]),
    ("home", ["reset", "go home", "go back", "back to the start", "back to start", "starting position",
              "start position", "return"]),
    ("wave", ["wave", "say hi", "say hello"]),
    ("grab", ["pick up", "pick", "grab", "get", "take", "fetch", "bring", "lift", "hand me", "grip"]),
]
_COLORS = ["red", "green", "yellow", "blue", "white", "black", "gray", "grey", "orange", "purple", "pink", "brown"]
# what people call things -> the names the camera's object naming uses
_NOUNS = {
    "ball": {"ball"}, "sphere": {"ball"}, "cup": {"cup", "mug"}, "mug": {"mug", "cup"}, "glass": {"cup"},
    "block": {"block", "box"}, "cube": {"block", "box"}, "brick": {"block"}, "box": {"box", "block"},
    "phone": {"phone"}, "bottle": {"bottle"}, "can": {"can"}, "cable": {"cable"}, "cord": {"cable"},
    "charger": {"charger"}, "pen": {"pen"}, "marker": {"pen"}, "scissors": {"scissors"}, "tape": {"tape"},
    "remote": {"remote"}, "keys": {"keys"}, "tool": {"tool", "screwdriver"},
    "screwdriver": {"screwdriver"}, "driver": {"screwdriver"}, "fruit": {"fruit"}, "apple": {"fruit"},
    "banana": {"fruit"}, "bowl": {"bowl"}, "mouse": {"mouse"}, "notebook": {"notebook"}, "book": {"notebook"},
    "glasses": {"glasses"}, "headphones": {"headphones"},
    "object": {"object"},   # the camera's name for something it couldn't identify ("White object 2")
}
_NUMBERS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6}
# extra words the speech recognizer may hear (it only knows the words it's given), in any order
_NLU_WORDS = sorted({w for _, ws in _INTENTS for p in ws for w in p.split()} | set(_COLORS) | set(_NOUNS)
                    | set(_ORDINALS) | {"two", "three", "four", "five", "six", "number"}
                    | {"the", "a", "an", "arm", "please", "can", "you", "me", "up", "it", "that", "this", "to",
                       "your", "and", "now", "go", "thing", "one", "seventeen", "eighteen", "hey", "buddy"})


def _number_duplicates(objs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """When a camera sees several things with the same name, number them left to right as they appear
    in its picture: each gets "n" (1-based), "count", and "tag" -- the name to show and say ("White cup 2").
    A one-of-a-kind thing's tag is just its name."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for o in objs:
        groups.setdefault((o.get("label") or "").lower(), []).append(o)
    out = []
    for o in objs:
        same = groups[(o.get("label") or "").lower()]
        if len(same) < 2:
            out.append({**o, "n": 1, "count": 1, "tag": o.get("label")})
            continue
        ordered = sorted(same, key=lambda x: ((x.get("uv") or [0, 0])[0], x.get("id", 0)))
        n = ordered.index(o) + 1
        out.append({**o, "n": n, "count": len(same), "tag": f"{o.get('label')} {n}"})
    return out


def _friendly_error(e: BaseException) -> str:
    """Motion-planner failures come back as long gRPC strings; say what they mean."""
    import re
    s = str(e) or type(e).__name__
    m = re.search(r"violation between ([\w:.-]+) and ([\w:.-]+) geometries", s)
    if m:
        a, b = (x.split(":")[-1] for x in m.groups())
        return f"the motion planner refused the move: the {a} would hit the {b}"
    if "IK solution" in s or "ik solution" in s.lower():
        return "the motion planner couldn't find a way for the arm to reach that spot"
    return s


# Where each arm's wrist camera sits on the arm's end flange: the frame the xarm850-realsense fragment
# gives its "cam" (mm, orientation vector in degrees). Override per camera with "mount" in live_cameras.
WRIST_CAM_MOUNT = {"x": 83.0, "y": -14.0, "z": 18.0,
                   "o_x": -0.030391, "o_y": -0.003538, "o_z": 0.999532, "theta": -97.731173}


def _ov_matrix(ox: float, oy: float, oz: float, theta_deg: float):
    """Rotation matrix for a Viam orientation vector: Rz(lon) @ Ry(lat) @ Rz(theta), the ZYZ
    convention the RDK uses (lat = angle from +z, lon = heading of the vector)."""
    import numpy as np
    n = math.sqrt(ox * ox + oy * oy + oz * oz) or 1.0
    ox, oy, oz = ox / n, oy / n, oz / n
    lat = math.acos(max(-1.0, min(1.0, oz)))
    lon = math.atan2(oy, ox) if 1 - abs(oz) > 1e-4 else 0.0
    th = math.radians(theta_deg)

    def rz(a):
        c, s_ = math.cos(a), math.sin(a)
        return np.array([[c, -s_, 0.0], [s_, c, 0.0], [0.0, 0.0, 1.0]])

    c, s_ = math.cos(lat), math.sin(lat)
    ry = np.array([[c, 0.0, s_], [0.0, 1.0, 0.0], [-s_, 0.0, c]])
    return rz(lon) @ ry @ rz(th)


def _live_cam_specs(attrs: Dict[str, Any], vis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The cameras shown on the status page: "live_cameras" (a list of {camera, label, arm}),
    else the single "live_camera" (or the vision camera). "arm" marks a camera mounted on that arm."""
    specs = []
    for c in attrs.get("live_cameras") or []:
        c = {"camera": c} if isinstance(c, str) else dict(c)
        if c.get("camera"):
            specs.append(c)
    if not specs:
        name = attrs.get("live_camera") or vis.get("camera")
        if name:
            specs.append({"camera": name})
    out, seen = [], set()
    for c in specs:
        if c["camera"] not in seen:
            seen.add(c["camera"])
            c["label"] = c.get("label") or ("Camera" if not out else c["camera"])
            out.append(c)
    return out


def _find_arm(router: "VoiceRouter", name: str) -> Arm:
    """An arm the router already uses (in a command, the arms panel or for grabbing), by resource name."""
    for c in router.commands:
        if c["resource_name"] == name and isinstance(c.get("resource"), Arm):
            return c["resource"]  # type: ignore
    info = getattr(router, "_arms", {}).get(name)
    if info and info.get("arm") is not None:
        return info["arm"]
    g = getattr(router, "grabbers", {}).get(name)
    if g and g.get("arm") is not None:
        return g["arm"]
    raise ValueError(f"'{name}' isn't an arm this router uses")


def _find_dep(deps: Mapping[ResourceName, ResourceBase], name: str) -> Optional[ResourceBase]:
    """Look up a dependency by its short name, whatever its API type."""
    for rn, res in deps.items():
        full = f"{rn.remote_path[0]}:{rn.name}" if getattr(rn, "remote_path", None) else rn.name
        if rn.name == name or full == name:
            return res
    return None


class VoiceRouter(Generic, EasyResource):
    # Change "hackathons" to your org's public namespace if you upload this to the registry.
    MODEL: ClassVar[Model] = Model(ModelFamily("hackathons", "voice-router"), "router")

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Tuple[Sequence[str], Sequence[str]]:
        attrs = struct_to_dict(config.attributes)
        mic = attrs.get("mic")
        if not mic:
            raise ValueError("'mic' (the wake-word-filter audio_in) is required")
        commands = attrs.get("commands") or []
        if not commands:
            raise ValueError("'commands' must list at least one command")
        deps = [mic]
        vis = attrs.get("vision") or {}
        uses_vision = any(c.get("action") in ("grab", "release") for c in commands)
        if uses_vision or vis.get("camera"):
            if not vis.get("camera"):
                raise ValueError("grab/release commands need 'vision.camera'")
            if vis["camera"] not in deps:
                deps.append(vis["camera"])
            specs = _grab_arm_specs(vis)
            if not specs:
                raise ValueError("'vision' needs 'arms' (or 'arm' and 'gripper')")
            for i, a in enumerate(specs):
                for key in ("arm", "gripper"):
                    if not a.get(key):
                        raise ValueError(f"vision.arms[{i}] needs '{key}'")
                    if a[key] not in deps:
                        deps.append(a[key])
        for i, c in enumerate(commands):
            if not c.get("phrases"):
                raise ValueError(f"commands[{i}] needs 'phrases'")
            if c.get("action") in ("grab", "release"):
                if c.get("action") == "grab" and not c.get("color"):
                    raise ValueError(f"commands[{i}] (grab) needs 'color'")
                targets = []
            elif c.get("action") == "stop":
                targets = c.get("resources") or []
                if not targets:
                    raise ValueError(f"commands[{i}] (stop) needs 'resources': the arms to halt")
            elif c.get("resource"):
                targets = [c["resource"]]
            else:
                raise ValueError(f"commands[{i}] needs 'resource'")
            for t in targets:
                if t not in deps:
                    deps.append(t)
        # Optional: the raw microphone, read only to drive the status page's sound-level animation.
        optional = [attrs["level_mic"]] if attrs.get("level_mic") else []
        # Optional: a camera to show live on the status page.
        if attrs.get("live_camera") and attrs["live_camera"] not in deps + optional:
            optional.append(attrs["live_camera"])
        for c in attrs.get("live_cameras") or []:
            c = {"camera": c} if isinstance(c, str) else c
            for name in (c.get("camera"), c.get("arm")):
                if name and name not in deps + optional:
                    optional.append(name)
        rec_cam = (attrs.get("record") or {}).get("camera")
        if rec_cam and rec_cam not in deps + optional:
            optional.append(rec_cam)
        for i, mr in enumerate(attrs.get("record_on_motion") or []):
            arms = mr.get("arms") or ([mr["arm"]] if mr.get("arm") else [])
            if not mr.get("camera") or not arms:
                raise ValueError(f"record_on_motion[{i}] needs 'camera' and 'arm' (or 'arms')")
            for name in [mr["camera"], *arms]:
                if name not in deps + optional:
                    optional.append(name)
        return deps, optional

    @classmethod
    def new(cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]) -> Self:
        self = super().new(config, dependencies)
        attrs = struct_to_dict(config.attributes)

        self.mic: AudioIn = _find_dep(dependencies, attrs["mic"])  # type: ignore
        # "wake" (default): always listening on the raw mic for the wake word ("hey buddy"); once heard, the
        #   next few seconds are taken as the command. The status page's mic button mutes it.
        # "toggle": tap the mic on the status page, then just say the command.
        # "wake_word": the external wake-word filter (self.mic) decides when a command is being said.
        raw_name = attrs.get("raw_mic") or attrs.get("level_mic")
        self.raw_mic: Optional[AudioIn] = _find_dep(dependencies, raw_name) if raw_name else None  # type: ignore
        self.listen_mode = str(attrs.get("listen_mode", "wake"))
        if self.listen_mode in ("wake", "toggle") and self.raw_mic is None:
            LOGGER.warning(f"listen_mode '{self.listen_mode}' needs 'raw_mic' (or 'level_mic'); falling back to the wake-word filter")
            self.listen_mode = "wake_word"
        self.wake_word = " ".join(str(attrs.get("wake_word", "hey buddy")).lower().split()) or "hey buddy"
        self.command_window_s = max(1.0, float(attrs.get("command_window_s", 5)))
        self.sounds_on = bool(attrs.get("sounds", True))
        if _MIC["on"] is None:
            _MIC["on"] = self.listen_mode != "toggle"
        self.match_threshold = float(attrs.get("match_threshold", 80))
        self.use_grammar = bool(attrs.get("use_grammar", True))
        self.model_ref = attrs.get("vosk_model", "vosk-model-small-en-us-0.15")

        self.commands: List[Dict[str, Any]] = []
        for c in attrs["commands"]:
            stop_names = c.get("resources") or []
            self.commands.append({
                "phrases": [p.lower().strip() for p in c["phrases"]],
                "resource_name": c.get("resource") or ", ".join(stop_names),
                "resource": _find_dep(dependencies, c["resource"]) if c.get("resource") else None,
                "stop_targets": [(n, _find_dep(dependencies, n)) for n in stop_names],
                "command": c.get("command", {}),
                "action": c.get("action"),
                "joint": int(c.get("joint", 4)),
                "amplitude_deg": float(c.get("amplitude_deg", 15)),
                "times": int(c.get("times", 3)),
                # The wiggle runs at this speed whatever the arm is set to; it's passed per move, so the
                # arm's own speed setting is never changed (nothing to restore, even after "stop").
                "speed_deg_s": max(3.0, min(180.0, float(c.get("speed_deg_s", 150)))),
                "accel_deg_s2": max(10.0, min(1145.0, float(c.get("accel_deg_s2", 1000)))),
                # Stay this far inside the joint's limits. xArm850 wrist (joint 4) is ±124°.
                "joint_limit_deg": float(c.get("joint_limit_deg", 118)),
                "label": c.get("label"),
                "name": c.get("name") or c.get("resource") or "",   # friendly name, e.g. "arm 18"
                "wave_pose": c.get("wave_pose"),  # optional joint list; overrides a taught pose
                "color": (c.get("color") or "").lower() or None,    # grab: which block
            })

        # ---- vision / grabbing (optional) ----
        vis = attrs.get("vision") or {}
        self.vision: Optional[Dict[str, Any]] = None
        self.grabbers: Dict[str, Dict[str, Any]] = {}   # arm resource name -> arm, gripper, calibration, holding
        self.picker: Optional[str] = None
        if vis.get("camera"):
            specs = _grab_arm_specs(vis)
            primary = specs[0]
            colors = dict(vision.DEFAULT_COLORS)
            for name, spec in (vis.get("colors") or {}).items():   # override or add colors
                colors[name.lower()] = {**colors.get(name.lower(), {"draw": (255, 255, 255)}), **spec}
            self.vision = {
                "camera_name": vis["camera"], "camera": _find_dep(dependencies, vis["camera"]),
                "arm_name": primary["arm"], "arm": _find_dep(dependencies, primary["arm"]),
                "gripper_name": primary["gripper"], "gripper": _find_dep(dependencies, primary["gripper"]),
                "name": primary.get("name") or primary["arm"],
                "colors": colors,
                "on_mat_min": 0.35 if vis.get("require_on_mat", True) else 0.0,
                "approach_mm": float(vis.get("approach_mm", 120)),
                "lift_mm": float(vis.get("lift_mm", 150)),
                "safe_z_mm": vis.get("safe_z_mm"),          # travel height; default derived from calibration
                "reach_min_mm": float(vis.get("reach_min_mm", 150)),
                "reach_max_mm": float(vis.get("reach_max_mm", 750)),
                # grabbing from a wrist camera. finger_mm: the real flange-to-fingertip length, used to aim.
                # planner_finger_mm: the same length as the motion planner models it (the gripper frame sits
                # 150 mm out and its "claws" box reaches 50 mm past that); the flange is never sent lower
                # than that model allows over the table, so the planner never refuses the move. table_z_mm:
                # the table top in the arms' base frame (the workcell's table obstacle: z -123, 200 thick).
                "finger_mm": float(vis.get("finger_mm", 172)),
                "planner_finger_mm": float(vis.get("planner_finger_mm", 200)),
                "table_z_mm": float(vis.get("table_z_mm", -23)),
                "intrinsics": None,
            }
            for i, a in enumerate(specs):
                # Each arm has its own camera calibration. The first arm keeps the original file name.
                slug = "".join(ch if ch.isalnum() else "-" for ch in a["arm"])
                path = CALIB_FILE if i == 0 else os.path.join(_HERE, f"camera_calibration.{slug}.json")
                self.grabbers[a["arm"]] = {
                    "key": a["arm"], "name": a.get("name") or a["arm"],
                    "arm": _find_dep(dependencies, a["arm"]),
                    "gripper_name": a["gripper"], "gripper": _find_dep(dependencies, a["gripper"]),
                    "calibration": vision.Calibration(path), "holding": None,
                    # how hard the gripper squeezes, 1-100 % (UFactory G2 gripper; None = its own setting)
                    "grip_force": max(1, min(100, int(a["grip_force_pct"]))) if a.get("grip_force_pct") else None,
                    # joint acceleration while picking something up (deg/s^2, xArm max 1145), then back to normal
                    "pick_accel": max(10.0, min(1145.0, float(a["pick_accel_deg_s2"]))) if a.get("pick_accel_deg_s2") else None,
                    "normal_accel": float(a.get("normal_accel_deg_s2", 381.67)),   # the xArm driver's default
                }
            # "picker": the one arm that picks things up (by voice or by clicking). It finds things with its
            # own wrist camera when it has one. Without a picker, the closest calibrated arm goes.
            self.picker = vis.get("picker") if vis.get("picker") in self.grabbers else None
            if vis.get("picker") and not self.picker:
                LOGGER.warning(f"vision.picker '{vis['picker']}' isn't one of vision.arms; any arm may grab")
            for c in self.commands:
                if c["action"] in ("grab", "release"):
                    c["resource_name"] = self.picker or ", ".join(self.grabbers)
                    c["name"] = c["name"] or (self.grabbers[self.picker]["name"] if self.picker else "the closest arm")
        self._last_look: Optional[Dict[str, Any]] = None

        self._busy = asyncio.Lock()  # one motion/command at a time
        # Commands run in the background so the router keeps listening (and can hear "stop").
        self._action_task: Optional[asyncio.Task] = None
        self._voice_task: Optional[asyncio.Task] = None   # the running action, if it was started by voice
        self.state = {"listening": False, "last_transcript": None, "last_match": None, "last_error": None}
        # For the status page: phase is one of starting, waiting, hearing, processing,
        # running, done, nomatch, error, offline. Result phases fall back to "waiting" after a few seconds.
        self._phase, self._phase_until, self._current = "starting", 0.0, None
        self._listen_until = 0.0   # "wake" mode: the command window closes at this time
        self._history: deque = deque(maxlen=15)
        # Counters the status page watches to light up the wake word and "Command" the moment they happen.
        self._events: Dict[str, Any] = {"wake_seq": 0, "cmd_seq": 0, "cmd_text": None,
                                        "cmd_matched": None, "cmd_label": None, "cmd_typed": False}
        self._loop = asyncio.get_event_loop()
        self._poses = _load_poses()
        status_page.capture_logs(LOGGER)
        status_page.set_state_source(self._snapshot)
        status_page.set_action_handler(self._handle_page_action)
        status_page.start(int(attrs.get("status_port", 8765)), LOGGER)

        # viam-server may rebuild this resource on a config change without closing the old
        # instance first; make sure only one listener ever runs so a command can't fire twice.
        global _ACTIVE_TASK
        if _ACTIVE_TASK is not None and not _ACTIVE_TASK.done():
            _ACTIVE_TASK.cancel()
        self._task = asyncio.get_event_loop().create_task(self._listen_forever())
        _ACTIVE_TASK = self._task

        global _LEVEL_TASK
        if _LEVEL_TASK is not None and not _LEVEL_TASK.done():
            _LEVEL_TASK.cancel()
        self.level_mic = _find_dep(dependencies, attrs["level_mic"]) if attrs.get("level_mic") else None
        _LEVEL_TASK = asyncio.get_event_loop().create_task(self._levels_forever()) if self.level_mic else None
        self._level_task = _LEVEL_TASK

        # Live cameras on the status page: "live_cameras", else "live_camera", else the vision camera.
        global _LIVE_TASKS
        for t in _LIVE_TASKS:
            if not t.done():
                t.cancel()
        _LIVE_TASKS = []
        self.live_cams: List[Dict[str, Any]] = []
        for i, sp in enumerate(_live_cam_specs(attrs, vis if self.vision else {})):
            arm_name = sp.get("arm")
            self.live_cams.append({
                **sp, "index": i, "cam": _find_dep(dependencies, sp["camera"]),
                "arm_res": _find_dep(dependencies, arm_name) if arm_name else None,
                "arm_label": (self.grabbers.get(arm_name) or {}).get("name") or next(
                    (c["name"] for c in self.commands if c["action"] == "wave" and c["resource_name"] == arm_name),
                    arm_name) if arm_name else None,
                "mount": {**WRIST_CAM_MOUNT, **(sp.get("mount") or {})},
            })
        self.live_cam_name = self.live_cams[0]["camera"] if self.live_cams else None
        self.live_cam = self.live_cams[0]["cam"] if self.live_cams else None
        self.live_fps = float(attrs.get("live_fps", 6))
        rng = attrs.get("live_depth_range_mm")
        self.live_range = (float(rng[0]), float(rng[1])) if rng and len(rng) == 2 else None
        self._live_last: Dict[str, Dict[str, Any]] = {}   # camera -> latest frame, intrinsics, arm pose
        self._det_n: Dict[str, int] = {}                     # camera -> detections made so far
        status_page.set_live_cams([c["camera"] for c in self.live_cams])
        for c in self.live_cams:
            status_page.set_live_status(c["camera"], camera=c["camera"], label=c["label"], arm=c["arm_label"],
                                        ok=False, paused=False, fps=0, error=None, range_mm=None, has_color=None,
                                        size=None, table=None, objects=[], detect_error=None)
            _LIVE_TASKS.append(asyncio.get_event_loop().create_task(self._live_forever(c)))
        self._live_tasks = list(_LIVE_TASKS)

        # Arm state for the status page: every arm the commands use, polled while a page is open.
        self._arms = self._collect_arms(dependencies)
        self._arm_state: Dict[str, Dict[str, Any]] = {}
        self._action_arms: List[str] = []   # arms the running voice command is driving
        global _ARMS_TASK
        if _ARMS_TASK is not None and not _ARMS_TASK.done():
            _ARMS_TASK.cancel()
        _ARMS_TASK = asyncio.get_event_loop().create_task(self._arms_forever()) if self._arms else None
        self._arms_task = _ARMS_TASK
        self._setup_recorder(attrs.get("record") or {}, dependencies)
        self._setup_motion_recorders(attrs.get("record_on_motion") or [], dependencies)
        return self

    # ---------- recording a camera while voice commands move the arms ----------

    def _setup_recorder(self, cfg: Dict[str, Any], deps: Mapping[ResourceName, ResourceBase]) -> None:
        global _REC
        recorder.LOGGER = LOGGER   # so its lines show up in the Viam app's logs
        self.rec_dir = os.path.join(_HERE, str(cfg.get("dir") or "recordings"))
        status_page.set_recordings_dir(self.rec_dir)
        if not cfg.get("camera") or cfg.get("enabled") is False:
            if _REC is not None:
                _REC.configure(None, "", {}, lambda: False)
            return
        if _REC is None:
            _REC = recorder.Recorder(self.rec_dir)
        _REC.out_dir = self.rec_dir
        _REC.fps = max(1.0, min(30.0, float(cfg.get("fps", 10))))
        _REC.idle_s = max(1.0, float(cfg.get("idle_s", 10)))
        _REC.max_s = max(_REC.idle_s + 5, float(cfg.get("max_s", 600)))
        _REC.keep_still = bool(cfg.get("keep_still", False))   # keep clips where no arm moved
        _REC.label = str(cfg.get("label") or next((c.get("label") for c in getattr(self, "live_cams", [])
                                                   if c["camera"] == cfg["camera"] and c.get("label")), cfg["camera"]))
        cam = _find_dep(deps, cfg["camera"])
        if cam is None:
            LOGGER.warning(f"recording: camera '{cfg['camera']}' isn't available; voice commands won't be recorded")
        arms = {rn: a["arm"] for rn, a in self._arms.items() if a.get("arm") is not None}
        _REC.configure(cam, cfg["camera"], arms,
                       lambda: self._action_task is not None and not self._action_task.done())

    def _setup_motion_recorders(self, specs: List[Dict[str, Any]], deps: Mapping[ResourceName, ResourceBase]) -> None:
        """Film a camera whenever an arm moves, whatever moved it (not just voice commands)."""
        global _MOTION_RECS
        base = getattr(self, "rec_dir", os.path.join(_HERE, "recordings"))
        wanted = {}
        for spec in specs:
            cam_name = spec["camera"]
            arm_names = list(spec.get("arms") or [spec["arm"]])
            label = str(spec.get("label") or f"{cam_name} camera")
            sub = re.sub(r"[^A-Za-z0-9_-]+", "-", str(spec.get("dir") or cam_name)).strip("-") or cam_name
            mr = _MOTION_RECS.get(cam_name)
            if mr is None:
                mr = recorder.MotionRecorder(recorder.Recorder(os.path.join(base, sub)))
            rec = mr.rec
            rec.out_dir, rec.label, rec.url_prefix = os.path.join(base, sub), label, sub + "/"
            rec.fps = max(1.0, min(30.0, float(spec.get("fps", 10))))
            rec.idle_s = max(1.0, float(spec.get("idle_s", 10)))
            rec.max_s = max(rec.idle_s + 5, float(spec.get("max_s", 600)))
            cam = _find_dep(deps, cam_name)
            arms = {a: _find_dep(deps, a) for a in arm_names}
            missing = [n for n, r in [(cam_name, cam), *arms.items()] if r is None]
            if missing:
                LOGGER.warning(f"recording: {', '.join(missing)} isn't available; {label} "
                               f"won't be filmed when {' or '.join(arm_names)} moves")
            labels = {a: (self._arms.get(a) or {}).get("name") or a for a in arm_names}
            mr.configure(cam, cam_name, arms, labels)
            wanted[cam_name] = mr
        for name, mr in _MOTION_RECS.items():
            if name not in wanted:
                mr.close()
        _MOTION_RECS = wanted

    def _rec_statuses(self) -> List[Dict[str, Any]]:
        recs = [_REC] if _REC is not None and _REC.camera is not None else []
        recs += [mr.rec for mr in _MOTION_RECS.values() if mr.rec.camera is not None]
        return [{**r.status(), "trigger": "voice" if r is _REC else "motion"} for r in recs]

    def _rec_start(self, label: str, text: str, arms: Optional[List[str]] = None) -> None:
        """A command is starting: begin (or extend) the clips that film it, named after the command.
        Motion recorders join in when the command moves one of their arms (they'd start on the motion
        anyway, but then the clip would only be called "arm 17 moving")."""
        try:
            if _REC is not None:
                _REC.start(label, text)
            for mr in _MOTION_RECS.values():
                if mr.rec.camera is not None and arms and set(arms) & set(mr.arms):
                    mr.rec.start(label, text)
        except Exception as e:
            LOGGER.warning(f"recording: couldn't start: {e}")

    # ---------- arm state for the status page ----------

    def _collect_arms(self, deps: Mapping[ResourceName, ResourceBase]) -> Dict[str, Dict[str, Any]]:
        """resource name -> {name, arm}, in a stable order, for every arm a command can move."""
        arms: Dict[str, Dict[str, Any]] = {}
        names = {c["resource_name"]: c["name"] for c in self.commands if c["action"] == "wave"}
        for g in self.grabbers.values():
            names.setdefault(g["key"], g["name"])
        order = list(names) + [n for c in self.commands for n, _ in c["stop_targets"]]
        for rn in order:
            if rn in arms:
                continue
            res = _find_dep(deps, rn)
            if res is not None and not isinstance(res, Arm):
                continue
            arms[rn] = {"name": names.get(rn) or rn, "arm": res}
        return dict(sorted(arms.items(), key=lambda kv: kv[1]["name"]))

    def _arms_for(self, cmd: Dict[str, Any]) -> List[str]:
        if cmd["action"] == "wave":
            return [cmd["resource_name"]]
        if cmd["action"] == "release":
            held = [k for k, g in self.grabbers.items() if g["holding"]]
            return held or list(self.grabbers)[:1]
        if cmd["action"] in ("pick", "home", "find_grab"):
            return [cmd["resource_name"]]
        if cmd["action"] == "grab" and self.picker:
            return [self.picker]
        return []   # grab: the arm is chosen once the camera has looked (see _pick)

    async def _poll_arm(self, rn: str, info: Dict[str, Any]) -> None:
        st = self._arm_state.setdefault(rn, {})
        if info["arm"] is None:
            st.update(ok=False, error="not available (is its machine online?)")
            return
        try:
            moving, joints = await asyncio.wait_for(
                asyncio.gather(info["arm"].is_moving(), info["arm"].get_joint_positions()), timeout=3)
            st.update(ok=True, error=None, moving=bool(moving),
                      joints=[round(float(v), 1) for v in joints.values], at=time.time())
            g = getattr(self, "grabbers", {}).get(rn)
            busy = self._action_task is not None and not self._action_task.done()
            if g is not None and not busy and time.time() - g.get("grip_checked", 0) > 1.0:
                await self._sync_holding(g, confirm=3)
        except Exception as e:
            st.update(ok=False, error=str(e) or type(e).__name__)

    async def _arms_forever(self):
        """Ask each arm whether it's moving (and where its joints are) ~3x/second, only while a page is open."""
        while True:
            if not status_page.page_open():
                await asyncio.sleep(0.3)
                continue
            t0 = time.time()
            await asyncio.gather(*[self._poll_arm(rn, a) for rn, a in self._arms.items()], return_exceptions=True)
            await asyncio.sleep(max(0.1, 0.33 - (time.time() - t0)))

    def _arms_snapshot(self) -> List[Dict[str, Any]]:
        running = self._action_task is not None and not self._action_task.done()
        out = []
        for rn, info in getattr(self, "_arms", {}).items():   # the page can ask before new() finishes
            st = self._arm_state.get(rn, {})
            fresh = st.get("ok") and time.time() - st.get("at", 0) < 3
            busy = running and rn in getattr(self, "_action_arms", [])
            out.append({
                "resource": rn, "name": info["name"],
                "ok": bool(fresh), "error": st.get("error") if not st.get("ok") else (None if fresh else "no recent reading"),
                "moving": bool(st.get("moving")) if fresh else None,
                "joints": st.get("joints") if fresh else None,
                "action": (self._current or "Working") if busy else None,
                "holding": (getattr(self, "grabbers", {}).get(rn) or {}).get("holding"),
                "can_reset": bool(getattr(self, "_poses", {}).get("home:" + rn)),
                "has_gripper": ((getattr(self, "grabbers", {}).get(rn) or {}).get("gripper")) is not None,
            })
        return out

    # ---------- live camera for the status page ----------

    @staticmethod
    def _detect_live(out: Dict[str, Any], intr: Dict[str, float], colors: Dict[str, Any]) -> Dict[str, Any]:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(out["color"])).convert("RGB") if out["color"] else None
        return vision.detect_objects(img, out["depth_mm"], intr, colors)

    async def _live_forever(self, c: Dict[str, Any]):
        """Grab frames from one camera only while a status page is watching it; colorize depth, find
        what's on the table, and hand it all to the page. A wrist camera also records where its arm
        was for each frame, so a thing seen in it can be located relative to the arm's base."""
        name, cam, arm = c["camera"], c["cam"], c["arm_res"]
        if cam is None:
            status_page.set_live_status(name, error=f"'{name}' isn't available "
                                                    "(is its machine online and in this machine's remotes?)")
            return
        period = 1.0 / max(0.5, min(15.0, self.live_fps))
        rng, stamps, failing = None, deque(maxlen=12), False
        intr, intr_err = None, None
        tracker = vision.ObjectTracker()
        tracker.next_id = c["index"] * 1_000_000 + 1   # ids unique across cameras (naming and grabbing use them)
        namer = _identifier()      # names things with CLIP ("white cup"); loads in the background
        colors = self.vision["colors"] if self.vision else vision.DEFAULT_COLORS
        while True:
            if not status_page.live_wanted(name):
                status_page.set_live_status(name, paused=True)
                stamps.clear()
                await asyncio.sleep(0.2)
                continue
            t0 = time.time()
            try:
                if arm is not None:   # the picture and where the arm was, as close together as we can
                    res, pose = await asyncio.gather(asyncio.wait_for(cam.get_images(), timeout=6),
                                                     asyncio.wait_for(arm.get_end_position(), timeout=6),
                                                     return_exceptions=True)
                    if isinstance(res, BaseException):
                        raise res
                    pose = None if isinstance(pose, BaseException) else pose
                else:
                    res, pose = await asyncio.wait_for(cam.get_images(), timeout=6), None
                named = res[0] if isinstance(res, tuple) else res   # SDK returns (images, metadata)
                out = await asyncio.to_thread(vision.render_live, named, rng, self.live_range)
                rng = out["range_mm"]
                status_page.set_live_frame(out["color"], out["depth"], out["depth_mm"], name)
                # what's on the table (needs depth and the lens intrinsics)
                if intr is None and intr_err is None:
                    try:
                        p = (await cam.get_properties()).intrinsic_parameters
                        if not p.focal_x_px:
                            raise ValueError("the camera doesn't report lens intrinsics")
                        intr = {"fx": p.focal_x_px, "fy": p.focal_y_px, "cx": p.center_x_px, "cy": p.center_y_px}
                    except Exception as e:
                        intr_err = str(e) or type(e).__name__
                self._live_last[name] = {"out": out, "intr": intr, "pose": pose, "t": time.time()}
                if c["index"] == 0:
                    self._last_live = (out, intr)
                if intr is not None and out["depth_mm"] is not None:
                    try:
                        found = await asyncio.to_thread(self._detect_live, out, intr, colors)
                        objs = namer.label(tracker.update(found["objects"]), out["color"])
                        status_page.set_live_status(name, table=found["table"], objects=objs, detect_error=None,
                                                    naming=namer.status())
                        self._det_n[name] = self._det_n.get(name, 0) + 1
                    except Exception as e:
                        status_page.set_live_status(name, objects=[], detect_error=str(e) or type(e).__name__)
                elif intr_err:
                    status_page.set_live_status(name, objects=[], detect_error=intr_err)
                stamps.append(time.time())
                span = stamps[-1] - stamps[0]
                status_page.set_live_status(
                    name, ok=True, paused=False, error=None,
                    fps=round((len(stamps) - 1) / span, 1) if span > 0 else 0,
                    range_mm=[round(rng[0]), round(rng[1])] if rng else None,
                    has_color=out["color"] is not None, size=out["size"])
                if failing:
                    LOGGER.info(f"live camera {name} is back")
                failing = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = str(e) or type(e).__name__
                status_page.set_live_status(name, ok=False, error=msg)
                if not failing:
                    LOGGER.warning(f"live camera {name}: {msg}; retrying every 2s")
                failing = True
                await asyncio.sleep(2)
                continue
            await asyncio.sleep(max(0.0, period - (time.time() - t0)))

    # ---------- sound level for the status page ----------

    async def _levels_forever(self):
        """Read the raw mic (a second, read-only stream) and publish a 0..1 loudness ~20x/second.
        Purely cosmetic: if anything goes wrong the page just falls back to its normal animation."""
        import math
        from array import array

        floor_db = -60.0  # adaptive noise floor so room hum doesn't keep the orb puffed up
        while True:
            try:
                stream = await self.level_mic.get_audio("pcm16", 0, 0)
                status_page.set_level_ok(True)
                async for chunk in stream:
                    data = chunk.audio.audio_data
                    if len(data) < 4:
                        continue
                    samples = array("h")
                    samples.frombytes(data[: len(data) - len(data) % 2])
                    # split each chunk into ~50 ms windows so the page gets smooth updates
                    win = 800  # 50 ms at 16 kHz
                    for i in range(0, len(samples), win):
                        part = samples[i:i + win]
                        if not part:
                            continue
                        rms = math.sqrt(sum(s * s for s in part) / len(part))
                        db = 20 * math.log10(rms / 32768 + 1e-9)
                        # floor follows quiet quickly-ish, loud very slowly
                        floor_db += (db - floor_db) * (0.05 if db < floor_db else 0.002)
                        floor_db = max(-75.0, min(floor_db, -30.0))
                        level = (db - (floor_db + 6)) / 30.0   # ~6 dB above floor -> 0, +36 dB -> 1
                        status_page.push_level(max(0.0, min(1.0, level)))
            except asyncio.CancelledError:
                status_page.set_level_ok(False)
                raise
            except Exception as e:
                status_page.set_level_ok(False)
                LOGGER.warning(f"sound-level feed from the mic stopped ({e}); retrying in 3s")
                await asyncio.sleep(3)

    # ---------- status page ----------

    def _set_phase(self, phase: str, hold_s: float = 0.0, current: Optional[str] = None):
        self._phase, self._current = phase, current
        self._phase_until = time.time() + hold_s if hold_s else 0.0

    def _idle_phase(self) -> str:
        if not self.state["listening"]:
            return "offline"
        return "muted" if self.listen_mode in ("toggle", "wake") and not _MIC["on"] else "waiting"

    def _set_mic(self, on: bool) -> Dict[str, Any]:
        _MIC["on"] = bool(on)
        LOGGER.info("microphone " + ("on" if on else ("muted" if self.listen_mode == "wake" else "off")))
        if not on:
            self._listen_until = 0.0
        if self._phase in ("waiting", "muted", "hearing"):
            self._set_phase(self._idle_phase())
        return {"mic": _MIC["on"]}

    def _mic_state(self) -> Dict[str, Any]:
        mutable = self.listen_mode in ("toggle", "wake")
        return {"mode": self.listen_mode, "on": bool(_MIC["on"]) if mutable else True,
                "wake_word": self.wake_word, "window_s": self.command_window_s,
                "listen_until": self._listen_until if self._phase == "hearing" and self._listen_until else None,
                "stop_only": mutable and not _MIC["on"] and self._voice_busy()}

    def _play(self, name: str) -> None:
        """One of the UI chimes (sounds.py); silent when sounds are off in the config."""
        if self.sounds_on:
            sounds.play(name, LOGGER)

    def _voice_busy(self) -> bool:
        """A command started by voice is still running (so "stop" is still listened for)."""
        t = self._action_task
        return t is not None and not t.done() and t is self._voice_task

    def _snapshot(self) -> Dict[str, Any]:
        phase = self._phase
        if self._phase_until and time.time() > self._phase_until:
            phase = self._idle_phase()
        elif phase in ("waiting", "muted"):
            phase = self._idle_phase()
        return {
            **self.state,
            "phase": phase,
            "mic": self._mic_state(),
            "current": self._current,
            "commands": [c["phrases"][0] for c in self.commands],
            "command_list": [{"label": self._label(c), "phrases": c["phrases"]} for c in self.commands],
            "history": list(reversed(self._history)),
            "events": dict(self._events),
            "sound_level_feed": status_page._level_ok,
            "sound_level": round(status_page._level, 3),
            "wave_arms": [
                {"resource": c["resource_name"], "name": c["name"],
                 "pose_source": "config" if c["wave_pose"] else ("saved" if c["resource_name"] in self._poses else None),
                 "saved_at": (self._poses.get(c["resource_name"]) or {}).get("saved_at")}
                for c in self._wave_cmds()
            ],
            "live": self._live_with_grab_info(self.live_cams[0]) if getattr(self, "live_cams", None) else None,
            "cams": [self._live_with_grab_info(c) for c in getattr(self, "live_cams", [])],
            "arms": self._arms_snapshot(),
            "rec": _REC.status() if _REC is not None and _REC.camera is not None else None,
            "recs": self._rec_statuses(),
            "vision": None if not self.vision else {
                "colors": [c["color"] for c in self.commands if c["action"] == "grab"] or list(self.vision["colors"]),
                "arms": [{"key": g["key"], "name": g["name"], "calibration": g["calibration"].summary(),
                          "handoff_saved_at": (self._poses.get(self._handoff_key(g)) or {}).get("saved_at"),
                          "holding": g["holding"]} for g in self.grabbers.values()],
                "last_look": self._last_look,
            },
        }

    @staticmethod
    def _handoff_key(g: Dict[str, Any]) -> str:
        return "handoff:" + g["key"]

    def _live_with_grab_info(self, c: Dict[str, Any]) -> Dict[str, Any]:
        """One camera's live status, with each object marked with the arm that would grab it (or why none can)."""
        lv = {**status_page.live_status(c["camera"]), "name": c["camera"], "label": c["label"], "arm": c["arm_label"]}
        objs = _number_duplicates(lv.get("objects") or [])
        if not objs or not self.grabbers:
            return {**lv, "objects": objs}
        out = []
        for o in objs:
            try:
                out.append({**o, "grab": self._object_grab_info(o, c)})
            except Exception as e:
                out.append({**o, "grab": {"why": str(e)}})
        return {**lv, "objects": out}

    def _wave_cmds(self) -> List[Dict[str, Any]]:
        seen, out = set(), []
        for c in self.commands:
            if c["action"] == "wave" and c["resource_name"] not in seen:
                seen.add(c["resource_name"])
                out.append(c)
        return out

    # ---------- wave poses (teach-in) ----------

    def _wave_pose_for(self, cmd: Dict[str, Any]) -> Optional[List[float]]:
        if cmd["wave_pose"]:
            return [float(v) for v in cmd["wave_pose"]]
        saved = self._poses.get(cmd["resource_name"])
        return [float(v) for v in saved["joints"]] if saved else None

    async def _save_wave_pose(self, resource_name: str) -> Dict[str, Any]:
        cmd = next((c for c in self._wave_cmds() if c["resource_name"] == resource_name), None)
        if cmd is None:
            raise ValueError(f"no wave command uses '{resource_name}'")
        if self._action_task is not None and not self._action_task.done():
            raise ValueError("an arm is moving; wait until it finishes before saving a pose")
        joints = list((await cmd["resource"].get_joint_positions()).values)
        self._poses[resource_name] = {"joints": joints, "saved_at": time.time()}
        _save_poses(self._poses)
        LOGGER.info(f"saved wave pose for {resource_name}: {[round(v, 1) for v in joints]}")
        return {"saved": resource_name, "joints": joints}

    def _clear_wave_pose(self, resource_name: str) -> Dict[str, Any]:
        existed = self._poses.pop(resource_name, None) is not None
        _save_poses(self._poses)
        return {"cleared": resource_name, "existed": existed}

    def _handle_page_action(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Called from the status page's HTTP thread; runs the work on the module's event loop."""
        action, arm = body.get("action"), str(body.get("arm", ""))
        if action == "mic":
            on = (not _MIC["on"]) if body.get("on") is None else bool(body.get("on"))
            async def flip():
                return self._set_mic(on)
            return asyncio.run_coroutine_threadsafe(flip(), self._loop).result(timeout=5)
        if action == "stop":
            return asyncio.run_coroutine_threadsafe(self._stop_from_page(), self._loop).result(timeout=15)
        if action == "reset":
            return asyncio.run_coroutine_threadsafe(self._reset_from_page(arm), self._loop).result(timeout=10)
        if action == "save_wave_pose":
            fut = asyncio.run_coroutine_threadsafe(self._save_wave_pose(arm), self._loop)
            return fut.result(timeout=15)
        if action == "clear_wave_pose":
            async def clear():
                return self._clear_wave_pose(arm)
            return asyncio.run_coroutine_threadsafe(clear(), self._loop).result(timeout=5)
        vision_actions = {
            "look": lambda: self._look(str(body.get("color") or "") or None, record=True),
            "calib_record": lambda: self._calib_record(str(body.get("color") or "green"), arm),
            "calib_undo": lambda: self._calib_undo(arm),
            "calib_clear": lambda: self._calib_clear(arm),
            "save_handoff_pose": lambda: self._save_handoff_pose(arm),
            "clear_handoff_pose": lambda: self._clear_handoff_pose(arm),
            "grab_object": lambda: self._grab_object(int(body.get("id", -1)), str(body.get("cam") or "")),
            "release": lambda: self._release_from_page(arm),
        }
        if action in vision_actions:
            if not self.vision:
                raise ValueError("no camera configured (add a 'vision' section to the router config)")
            return asyncio.run_coroutine_threadsafe(vision_actions[action](), self._loop).result(timeout=30)
        raise ValueError(f"unknown action '{action}'")

    @staticmethod
    def _label(cmd: Dict[str, Any]) -> str:
        if cmd["label"]:
            return cmd["label"]
        if cmd["action"] == "grab":
            return f"Grabbing the {cmd['color']} block"
        if cmd["action"] == "release":
            return "Letting go"
        if cmd["action"] == "wave":
            return f"Waving {cmd['resource_name']}"
        if cmd["action"] == "stop":
            return "Stop"
        return f"Running {cmd['resource_name']}"

    # ---------- speech -> text ----------

    def _load_vosk(self):
        from vosk import KaldiRecognizer, Model as VoskModel, SetLogLevel

        SetLogLevel(-1)
        if os.path.isdir(str(self.model_ref)):
            model = VoskModel(model_path=self.model_ref)
        else:
            # Downloads once and caches (~40 MB for the small English model).
            model = VoskModel(model_name=self.model_ref)

        def make_recognizer(only: Optional[str] = None):
            """only="stop": a recognizer that can hear nothing but the stop phrases."""
            cmds = [c for c in self.commands if only is None or c["action"] == only]
            if self.use_grammar or only:
                vocab = sorted({p for c in cmds for p in c["phrases"]})
                if only is None:     # words for free-form commands ("pick up the red ball"), usable in any order
                    vocab.append(" ".join(_NLU_WORDS))
                    vocab.append(self.wake_word)   # "hey buddy", so the wake word can be recognised at all
                return KaldiRecognizer(model, SAMPLE_RATE, json.dumps(vocab + ["[unk]"]))
            return KaldiRecognizer(model, SAMPLE_RATE)

        return make_recognizer

    async def _toggle_listen(self, make_recognizer):
        """Tap-to-talk: with the mic switched on, every utterance is a command (no wake word).
        With it off nothing is recognised -- except "stop" while an arm is moving."""
        try:   # the wake-word filter isn't needed in this mode: stop it spending CPU
            await self.mic.do_command({"pause_detection": None})
        except Exception as e:
            LOGGER.debug(f"couldn't pause the wake-word filter: {e}")
        clean = lambda t: " ".join(t.replace("[unk]", " ").split())
        while True:
            try:
                stream = await self.raw_mic.get_audio("pcm16", 0, 0)
                self.state["listening"] = True
                if self._phase in ("starting", "offline", "waiting", "muted"):
                    self._set_phase(self._idle_phase())
                LOGGER.info("voice-router: tap-to-talk listener ready")
                rec, mode, speaking = None, None, False
                async for chunk in stream:
                    data = chunk.audio.audio_data
                    if not data:
                        continue
                    want = "all" if _MIC["on"] else ("stop" if self._voice_busy() else None)
                    if want != mode:            # switched on/off, or an arm started/stopped moving
                        mode, speaking = want, False
                        rec = None if want is None else make_recognizer(None if want == "all" else "stop")
                        if want is None and self._phase == "hearing":
                            self._set_phase(self._idle_phase())
                    if rec is None:
                        continue                # mic off: audio is dropped, nothing is recognised
                    if rec.AcceptWaveform(data):        # Vosk decided the utterance ended
                        text = clean(json.loads(rec.Result()).get("text", ""))
                        speaking = False
                        if text:
                            if mode == "all" or self._match(text)[0] is not None:
                                self._set_phase("processing")
                                res = await self._route(text)
                                if mode == "all" and res.get("matched"):
                                    self._set_mic(False)   # got the command: the mic switches itself off
                        elif self._phase == "hearing":
                            self._set_phase(self._idle_phase())
                    elif not speaking and clean(json.loads(rec.PartialResult()).get("partial", "")):
                        speaking = True                  # words are coming in: light up "heard you"
                        self._events["wake_seq"] += 1
                        if self._phase in ("waiting", "muted"):
                            self._set_phase("hearing")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state["listening"] = False
                self.state["last_error"] = str(e)
                self._set_phase("offline")
                LOGGER.warning(f"microphone stream ended ({e}); reconnecting in 2s")
                await asyncio.sleep(2)

    def _after_wake(self, text: str) -> Optional[str]:
        """If `text` starts with the wake word (allowing for small mishearings: "hey body", "a buddy"),
        return whatever follows it ("" if nothing); otherwise None."""
        from rapidfuzz import fuzz
        words = text.split()
        n = len(self.wake_word.split())
        if len(words) < n:
            return None
        for k in (n, n + 1, max(1, n - 1)):          # the wake word may come out as one word or three
            head = " ".join(words[:k])
            if fuzz.ratio(head, self.wake_word) >= 80:
                return " ".join(words[k:])
        return None

    async def _wake_listen(self, make_recognizer):
        """Buddy mode: transcribe the raw microphone and watch for the wake word ourselves.
        wake word heard -> chime, "hearing" for command_window_s -> command routed, or the window
        closes with the "off" chime and we're back to waiting for the wake word. Muted, nothing is
        recognised except "stop" while an arm is moving. Any error logs and resets to waiting."""
        try:   # the external wake-word filter isn't needed in this mode: stop it spending CPU
            await self.mic.do_command({"pause_detection": None})
        except Exception as e:
            LOGGER.debug(f"couldn't pause the wake-word filter: {e}")
        clean = lambda t: " ".join(t.replace("[unk]", " ").split())
        while True:
            try:
                stream = await self.raw_mic.get_audio("pcm16", 0, 0)
                self.state["listening"] = True
                if self._phase in ("starting", "offline", "waiting", "muted"):
                    self._set_phase(self._idle_phase())
                LOGGER.info(f"voice-router: listening for '{self.wake_word}'")
                rec, mode, woke_early = None, None, False
                async for chunk in stream:
                    data = chunk.audio.audio_data
                    if not data:
                        continue
                    now = time.time()
                    # the command window closed without a clear command
                    if self._listen_until and now > self._listen_until:
                        self._listen_until = 0.0
                        woke_early = False
                        rec = make_recognizer(None)
                        LOGGER.info(f"no command within {self.command_window_s:.0f}s of '{self.wake_word}'")
                        self._record(heard="(nothing)", matched=False)
                        self._note_command("", None, typed=False)
                        self._set_phase(self._idle_phase())
                        self._play("wake_off")
                    want = "all" if _MIC["on"] else ("stop" if self._voice_busy() else None)
                    if want != mode:            # muted/unmuted, or an arm started/stopped moving
                        mode, woke_early = want, False
                        rec = None if want is None else make_recognizer(None if want == "all" else "stop")
                    if rec is None:
                        continue                # muted: audio is dropped, nothing is recognised
                    if rec.AcceptWaveform(data):        # Vosk decided the utterance ended
                        text = clean(json.loads(rec.Result()).get("text", ""))
                        rec = make_recognizer(None if mode == "all" else "stop")
                        if not text:
                            continue
                        if mode == "stop":      # muted, arm moving: only "stop" counts
                            if self._match(text)[0] is not None:
                                self._set_phase("processing")
                                await self._route(text)
                            continue
                        if self._listen_until:  # inside the command window: this is the command
                            self._listen_until = 0.0
                            woke_early = False
                            self._set_phase("processing")
                            await self._route(text)
                            continue
                        rest = self._after_wake(text)
                        if rest is None:        # ordinary talk, or the early partial was wrong
                            if woke_early:
                                woke_early = False
                                self._set_phase(self._idle_phase())
                                self._play("wake_off")
                            continue
                        if not woke_early:
                            self._woke()
                        woke_early = False
                        if rest:                # "hey buddy grab the red ball" in one breath
                            self._set_phase("processing")
                            await self._route(rest)
                        else:                   # just "hey buddy": the command comes next
                            self._listen_until = time.time() + self.command_window_s
                            self._set_phase("hearing")
                    elif mode == "all" and not self._listen_until and not woke_early:
                        partial = clean(json.loads(rec.PartialResult()).get("partial", ""))
                        if partial and self._after_wake(partial) is not None:
                            woke_early = True    # chime as soon as the words show up, before the pause
                            self._woke()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state["listening"] = False
                self.state["last_error"] = str(e)
                self._listen_until = 0.0
                self._set_phase("offline")
                LOGGER.warning(f"microphone stream ended ({e}); reconnecting in 2s")
                await asyncio.sleep(2)

    def _woke(self) -> None:
        """The wake word was heard: light the page up and chime."""
        self._events["wake_seq"] += 1
        self._set_phase("hearing")
        LOGGER.info(f"heard '{self.wake_word}'")
        self._play("wake_on")

    async def _listen_forever(self):
        try:
            make_recognizer = await asyncio.to_thread(self._load_vosk)
        except Exception as e:
            self.state["last_error"] = f"vosk load failed: {e}"
            self._set_phase("error")
            LOGGER.error(self.state["last_error"])
            return

        if self.listen_mode == "toggle":
            await self._toggle_listen(make_recognizer)
            return
        if self.listen_mode == "wake":
            await self._wake_listen(make_recognizer)
            return

        while True:
            try:
                stream = await self.mic.get_audio("pcm16", 0, 0)
                self.state["listening"] = True
                if self._phase in ("starting", "offline"):
                    self._set_phase("waiting")
                LOGGER.info("voice-router listening for commands")
                rec = make_recognizer()
                in_segment = False
                async for chunk in stream:
                    data = chunk.audio.audio_data
                    if data:
                        if not in_segment:  # wake word just fired; command audio is arriving
                            in_segment = True
                            self._set_phase("hearing")
                            self._events["wake_seq"] += 1
                            self._play("wake_on")
                        rec.AcceptWaveform(data)
                        continue
                    # Empty chunk = end of the utterance after the wake word.
                    in_segment = False
                    self._set_phase("processing")
                    text = json.loads(rec.FinalResult()).get("text", "").replace("[unk]", "").strip()
                    rec = make_recognizer()
                    if text:
                        await self._route(text)
                    else:
                        self._record(heard="(nothing)", matched=False)
                        self._note_command("", None, typed=False)
                        self._set_phase("nomatch", hold_s=3)
                        self._play("wake_off")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state["listening"] = False
                self.state["last_error"] = str(e)
                self._set_phase("offline")
                LOGGER.warning(f"audio stream ended ({e}); reconnecting in 2s")
                await asyncio.sleep(2)

    # ---------- text -> command ----------

    def _match(self, text: str) -> Tuple[Optional[Dict[str, Any]], float]:
        from rapidfuzz import fuzz

        text = text.lower().strip()
        best, best_score = None, 0.0
        for c in self.commands:
            for p in c["phrases"]:
                score = 100.0 if p in text else fuzz.ratio(text, p)
                if score > best_score:
                    best, best_score = c, score
        return (best, best_score) if best_score >= self.match_threshold else (None, best_score)

    def _mk_cmd(self, **kw) -> Dict[str, Any]:
        """A command built on the fly (from understood speech), shaped like the configured ones."""
        base = {"phrases": [], "resource_name": "", "resource": None, "stop_targets": [], "command": {},
                "action": None, "joint": 4, "amplitude_deg": 15.0, "times": 3, "joint_limit_deg": 118.0,
                "label": None, "name": "", "wave_pose": None, "color": None, "speed_deg_s": 150.0,
                "accel_deg_s2": 1000.0}
        base.update(kw)
        return base

    def _arm_by_number(self, num: str) -> Optional[str]:
        """'17' -> the arm whose name has 17 in it (e.g. "arm 17" -> a17-arm)."""
        for rn, info in getattr(self, "_arms", {}).items():
            if re.search(rf"\b{num}\b", info["name"]) or re.search(rf"(^|\D){num}(\D|$)", rn):
                return rn
        return None

    def _understand(self, text: str) -> Optional[Dict[str, Any]]:
        """Free-form speech -> a command: what to do (grab / let go / reset / wave / stop), which arm,
        and for a grab, a description (color, kind) matched later against what the camera sees."""
        words = [_NUMBERS.get(w, w) for w in re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()]
        t = " " + " ".join(words) + " "
        intent = next((name for name, ws in _INTENTS if any(f" {w} " in t for w in ws)), None)
        if intent is None:
            return None
        nums = [w for w in words if w.isdigit()]
        arm = self._arm_by_number(nums[0]) if nums else None
        if intent == "stop":
            return next((c for c in self.commands if c["action"] == "stop"), None)
        if intent == "release":
            return next((c for c in self.commands if c["action"] == "release"), None) or self._mk_cmd(
                action="release", resource_name=self.picker or ", ".join(self.grabbers), label="Letting go")
        if intent in ("home", "wave"):
            rn = arm or self.picker or next(iter(getattr(self, "_arms", {})), None)
            if rn is None:
                return None
            found = next((c for c in self.commands if c["action"] == intent and c["resource_name"] == rn), None)
            if found or intent == "wave":
                return found
            info = self._arms.get(rn) or {}
            return self._mk_cmd(action="home", resource_name=rn, resource=info.get("arm"), name=info.get("name", rn),
                                label=f"Resetting {info.get('name', rn)}")
        # grab: "pick up the red ball", "get the cup", "grab the yellow one"
        if not self.picker:
            return None
        color = next((w for w in words if w in _COLORS), None)
        color = "gray" if color == "grey" else color
        noun = next((w for w in reversed(words) if w in _NOUNS), None)   # the last one: "can you get the cup"
        # which one, when there are several: "white cup 2", "cup number two", "the second cup"
        which = None
        if noun:
            i = max(i for i, w in enumerate(words) if w in _NOUNS)
            for j in range(i + 1, len(words)):
                if words[j].isdigit() and words[j - 1] != "arm":
                    which = int(words[j])
                    break
        if which is None:
            which = next((_ORDINALS[w] for w in words if w in _ORDINALS), None)
        what = " ".join(x for x in (color, noun) if x) or "thing"
        shown = f"{what} {which}" if which else what
        g = self.grabbers[self.picker]
        return self._mk_cmd(action="find_grab", resource_name=self.picker, name=g["name"], color=color, noun=noun,
                            which=which, what=shown,
                            label=f"Grabbing the {shown}" if what != "thing" else "Grabbing something")

    def _record(self, **entry):
        self._history.append({"time": time.time(), **entry})

    def _note_command(self, text: str, cmd: Optional[Dict[str, Any]], typed: bool) -> None:
        self._events.update(cmd_seq=self._events["cmd_seq"] + 1, cmd_text=text, cmd_matched=cmd is not None,
                            cmd_label=self._label(cmd) if cmd else None, cmd_typed=typed)

    async def _route(self, text: str, typed: bool = False) -> Dict[str, Any]:
        self.state["last_transcript"] = text
        cmd, score = self._match(text)
        if score < 100:              # not one of the exact phrases: work out what was meant
            meant = self._understand(text)
            if meant is not None:
                cmd, score = meant, 100.0
        self._note_command(text, cmd, typed)
        if cmd is None:
            LOGGER.info(f"heard '{text}' - no command matched (best score {score:.0f})")
            self.state["last_match"] = None
            self._record(heard=text, matched=False)
            self._set_phase("nomatch", hold_s=4)
            self._play("wake_off")
            return {"heard": text, "matched": False, "score": score}

        label = self._label(cmd)
        what = cmd["action"] or f"DoCommand({cmd['command']})"
        LOGGER.info(f"heard '{text}' -> {cmd['resource_name']} {what}")
        self.state["last_match"] = cmd["resource_name"]

        # "stop" always runs immediately, even (especially) while something else is moving.
        if cmd["action"] == "stop":
            return await self._stop(cmd, text, label)

        if self._action_task is not None and not self._action_task.done():
            self._record(heard=text, matched=True, skipped=True, label=label)
            return {"heard": text, "matched": True, "skipped": "previous command still running"}

        # Run in the background so the listener stays free to hear "stop".
        self._set_phase("running", current=label)
        self._action_arms = self._arms_for(cmd)
        self._action_task = asyncio.get_event_loop().create_task(self._run(cmd, text, label))
        self._voice_task = None if typed else self._action_task
        self._rec_start(label, text, self._action_arms)
        return {"heard": text, "matched": True, "resource": cmd["resource_name"], "started": label}

    async def _run(self, cmd: Dict[str, Any], text: str, label: str) -> None:
        try:
            async with self._busy:
                if cmd["action"] == "wave":
                    await self._wave(cmd)
                elif cmd["action"] == "grab":
                    await self._grab(cmd)
                elif cmd["action"] == "release":
                    await self._release(cmd.get("only"))
                elif cmd["action"] == "home":
                    await self._go_home(cmd)
                elif cmd["action"] == "find_grab":
                    await self._grab_described(cmd)
                elif cmd["action"] == "pick":
                    await (self._pick_at if cmd.get("pick_kind") == "wrist" else self._pick)(*cmd["pick"])
                elif cmd["action"]:
                    raise ValueError(f"unknown action '{cmd['action']}'")
                else:
                    await cmd["resource"].do_command(cmd["command"])
            self._record(heard=text, **cmd.get("rec", {}), matched=True, label=label)
            self._set_phase("done", hold_s=3,
                            current=self._current if cmd["action"] in ("grab", "release", "pick", "find_grab") else None)
            self._play("done")
        except asyncio.CancelledError:
            pass  # stopped on purpose; _stop records it
        except NotFound as e:
            LOGGER.info(str(e))
            self._record(heard=text, **cmd.get("rec", {}), matched=True, label="Not found", error=str(e), notfound=True)
            self._set_phase("notfound", hold_s=8, current=str(e))
            self._play("wake_off")
        except Exception as e:
            why = _friendly_error(e)
            who = cmd.get("name") or cmd["resource_name"]
            self.state["last_error"] = f"{_cap(who)} failed: {why}"
            LOGGER.error(self.state["last_error"] + (f"  ({e})" if why != str(e) else ""))
            self._record(heard=text, **cmd.get("rec", {}), matched=True, label=label, error=why)
            self._set_phase("error", hold_s=8)
            self._play("wake_off")

    async def _stop(self, cmd: Dict[str, Any], text: str, label: str) -> Dict[str, Any]:
        """Cancel whatever is running and halt every listed arm where it is."""
        interrupted = self._action_task is not None and not self._action_task.done()
        if interrupted:
            self._action_task.cancel()
        errors = []
        # Halt all arms (and the grabbing gripper) in parallel: cancelling our task alone
        # doesn't stop a move already sent.
        targets = [t for t in cmd["stop_targets"] if t[1] is not None]
        for g in self.grabbers.values():
            if g["gripper"] is not None:
                targets.append((g["gripper_name"], g["gripper"]))
        results = await asyncio.gather(*[res.stop() for _, res in targets], return_exceptions=True)
        for (name, _), r in zip(targets, results):
            if isinstance(r, Exception):
                errors.append(f"{name}: {r}")
        what = "Stopped" + (" mid-move" if interrupted else "")
        if errors:
            self.state["last_error"] = "; ".join(errors)
            LOGGER.error(f"stop: {self.state['last_error']}")
            self._record(heard=text, matched=True, label=what, error=self.state["last_error"])
            self._set_phase("error", hold_s=6)
        else:
            LOGGER.info(f"stop: halted {cmd['resource_name']}")
            self._record(heard=text, matched=True, label=what, stop=True)
            self._set_phase("stopped", hold_s=4)
        return {"heard": text, "matched": True, "stopped": cmd["resource_name"],
                "interrupted": interrupted, "errors": errors}

    # ---------- vision: looking, calibration, grabbing ----------
    # Every arm that can grab has its own camera calibration. A block (or an object clicked on the
    # status page) is grabbed by the calibrated arm closest to it that can reach it.

    def _grabber(self, key: str = "") -> Dict[str, Any]:
        if not self.grabbers:
            raise ValueError("no arms are set up for grabbing (add 'vision.arms' to the router config)")
        if not key:
            return next(iter(self.grabbers.values()))
        g = self.grabbers.get(key) or next((g for g in self.grabbers.values() if g["name"] == key), None)
        if g is None:
            raise ValueError(f"no grabbing arm called '{key}'")
        return g

    def _reach(self, g: Dict[str, Any], cam_mm: List[float]) -> Optional[Tuple[List[float], float]]:
        """Where arm `g` goes to grab something the camera sees at cam_mm, and how far that is from
        the arm's base (mm). None if the arm isn't calibrated or can't reach it."""
        cal = g["calibration"]
        if not cal.ready:
            return None
        a = cal.to_arm(cam_mm)
        r = math.hypot(a[0], a[1])
        v = self.vision
        return (a, r) if v["reach_min_mm"] <= r <= v["reach_max_mm"] else None

    def _closest_arm(self, cam_mm: List[float]) -> Tuple[Optional[Dict[str, Any]], Any]:
        """(arm, its target in arm coordinates) for the nearest calibrated arm that can reach cam_mm
        and isn't already holding something; or (None, the reason none can)."""
        calibrated = [g for g in self.grabbers.values() if g["calibration"].ready
                      and (not self.picker or g["key"] == self.picker)]
        if not calibrated:
            if self.picker:
                p = self.grabbers[self.picker]["name"]
                return None, f"{p} can only grab what its own camera sees: switch to {_cap(p)}"
            return None, "no arm is calibrated to the camera yet (see Setup)"
        options = []
        for g in calibrated:
            hit = self._reach(g, cam_mm)
            if hit:
                options.append((hit[1], g, hit[0]))
        if not options:
            return None, "it's out of reach of " + " and ".join(g["name"] for g in calibrated)
        options.sort(key=lambda o: o[0])
        free = [o for o in options if not o[1]["holding"]]
        if not free:
            g = options[0][1]
            return None, f"{g['name']} is already holding the {g['holding']} — let go first"
        return free[0][1], free[0][2]

    def _object_cam_mm(self, obj: Dict[str, Any], c: Optional[Dict[str, Any]] = None) -> Optional[List[float]]:
        """3D point (camera frame, mm) of an object from a live feed, worked out the same way
        find_blocks does: the blob's centre pixel at its median depth."""
        c = c or (self.live_cams[0] if self.live_cams else None)
        last = self._live_last.get(c["camera"]) if c else None
        intr = (last or {}).get("intr") or (self.vision or {}).get("intrinsics")
        if obj.get("top_cam"):
            return [float(x) for x in obj["top_cam"]]
        if not intr or not obj.get("dist_mm") or not obj.get("uv"):
            return None
        u, v = obj["uv"]
        z = float(obj["dist_mm"])
        return [(u - intr["cx"]) * z / intr["fx"], (v - intr["cy"]) * z / intr["fy"], z]

    def _wrist_point(self, obj: Dict[str, Any], c: Dict[str, Any]) -> Tuple[Optional[List[float]], str]:
        """An object seen by a wrist camera, in its arm's base frame (mm): camera -> flange (the camera's
        mounting) -> base (where the arm was when the picture was taken)."""
        import numpy as np
        cam = self._object_cam_mm(obj, c)
        if cam is None:
            return None, "no distance reading for it"
        pose = (self._live_last.get(c["camera"]) or {}).get("pose")
        if pose is None:
            return None, f"couldn't read where {c['arm_label']} is"
        return self._wrist_to_base(c, cam, pose), ""

    def _wrist_extras(self, obj: Dict[str, Any], c: Dict[str, Any]) -> Dict[str, Any]:
        """For the gripper: which way the thing lies (a horizontal unit vector in the arm's base frame,
        None if it's round or squarish) and how tall it is."""
        import numpy as np
        out: Dict[str, Any] = {"axis": None, "height": float((obj.get("size_mm") or [0, 0, 0])[2] or 0)}
        pose = (self._live_last.get(c["camera"]) or {}).get("pose")
        if obj.get("axis_cam") and pose is not None:
            m = c["mount"]
            d = (_ov_matrix(pose.o_x, pose.o_y, pose.o_z, pose.theta)
                 @ _ov_matrix(m["o_x"], m["o_y"], m["o_z"], m["theta"]) @ np.array(obj["axis_cam"], float))
            if math.hypot(d[0], d[1]) > 0.3:
                out["axis"] = [float(d[0]), float(d[1])]
        return out

    @staticmethod
    def _wrist_to_base(c: Dict[str, Any], cam_mm: List[float], pose) -> List[float]:
        """Camera-frame point (mm) -> the arm's base frame, given where the arm's flange was."""
        import numpy as np
        m = c["mount"]
        in_flange = _ov_matrix(m["o_x"], m["o_y"], m["o_z"], m["theta"]) @ np.array(cam_mm) + np.array([m["x"], m["y"], m["z"]])
        base = _ov_matrix(pose.o_x, pose.o_y, pose.o_z, pose.theta) @ in_flange + np.array([pose.x, pose.y, pose.z])
        return [float(x) for x in base]

    def _wrist_target_problem(self, g: Dict[str, Any], p: List[float]) -> Optional[str]:
        """Why arm `g` shouldn't go for a point found with its wrist camera (None = fine)."""
        v = self.vision
        if not (v["table_z_mm"] - 60 <= p[2] <= v["table_z_mm"] + 450):
            return (f"the camera's mounting doesn't add up: it puts this {p[2] - v['table_z_mm']:.0f} mm "
                    "from the table")
        if not v["reach_min_mm"] <= math.hypot(p[0], p[1]) <= v["reach_max_mm"]:
            return f"it's out of {g['name']}'s reach"
        if g["holding"]:
            return f"{g['name']} is already holding the {g['holding']} — let go first"
        return None

    def _picker_cam(self) -> Optional[Dict[str, Any]]:
        return next((c for c in getattr(self, "live_cams", []) if self.picker and c.get("arm") == self.picker
                     and c["cam"] is not None and c["arm_res"] is not None), None)

    def _in_action_task(self) -> bool:
        """True when called from the running action itself (False from the page's thread)."""
        try:
            return asyncio.current_task() is self._action_task
        except RuntimeError:
            return False

    def _object_grab_info(self, obj: Dict[str, Any], c: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        c = c or (self.live_cams[0] if self.live_cams else None)
        if not self.vision or c is None:
            return None
        size = obj.get("size_mm") or [0, 0, 0]
        across = obj.get("grip_mm") or min(size[0], size[1])   # the part the fingers close on (a cup's top)
        if c.get("arm"):            # a wrist camera: the arm it's mounted on does the grabbing
            if self.picker and c["arm"] != self.picker:
                p = self.grabbers[self.picker]["name"]
                return {"why": f"{p} does the picking up: switch to {_cap(p)}’s camera"}
            # while the camera's own arm moves, the picture and the arm's position aren't from the same
            # instant, so where things are can't be worked out (it can come out under the table)
            # (a voice grab looks from inside its own action, before the arm moves: that doesn't count)
            busy = (self._action_task is not None and not self._action_task.done() and c["arm"] in self._action_arms
                    and not self._in_action_task())
            if busy or (getattr(self, "_arm_state", {}).get(c["arm"]) or {}).get("moving"):
                return {"why": f"{c['arm_label']} is moving; wait until it stops"}
            g = self.grabbers.get(c["arm"])
            if g is None:
                return {"why": f"{c['arm_label']} isn't set up for grabbing (add it to vision.arms)"}
            if across > MAX_GRIP_MM:
                return {"why": f"where it would be gripped it's about {round(across / 10)} cm across, too wide for the gripper"}
            p, why = self._wrist_point(obj, c)
            if p is None:
                return {"why": why}
            why = self._wrist_target_problem(g, p)
            if why:
                return {"why": why}
            return {"arm": g["name"], "key": g["key"], "via": "wrist"}
        if c["camera"] != self.vision["camera_name"]:
            return None   # not the camera the arms are calibrated to
        cam = self._object_cam_mm(obj, c)
        if cam is None:
            return {"why": "no distance reading for it"}
        if across > MAX_GRIP_MM:
            return {"why": f"where it would be gripped it's about {round(across / 10)} cm across, too wide for the gripper"}
        g, res = self._closest_arm(cam)
        if g is None:
            return {"why": res}
        return {"arm": g["name"], "key": g["key"]}

    async def _intrinsics(self) -> Dict[str, float]:
        v = self.vision
        if v["intrinsics"] is None:
            p = (await v["camera"].get_properties()).intrinsic_parameters
            if not p.focal_x_px:
                raise ValueError(f"{v['camera_name']} doesn't report lens intrinsics")
            v["intrinsics"] = {"fx": p.focal_x_px, "fy": p.focal_y_px, "cx": p.center_x_px, "cy": p.center_y_px}
        return v["intrinsics"]

    async def _capture(self):
        res = await self.vision["camera"].get_images()
        named = res[0] if isinstance(res, tuple) else res   # SDK returns (images, metadata)
        return await asyncio.to_thread(vision.split_images, named)

    async def _look(self, color: Optional[str], record: bool = False, on_mat: bool = True) -> Dict[str, Any]:
        """Take one picture and find blocks (all colors, or just `color`). Moves nothing.
        Each block is matched to the closest calibrated arm that can reach it.
        Publishes an annotated snapshot to the status page."""
        v = self.vision
        intr = await self._intrinsics()
        img, depth = await self._capture()
        blocks = await asyncio.to_thread(vision.find_blocks, img, depth, intr, v["colors"], color,
                                         120, 2, v["on_mat_min"] if on_mat else 0.0)
        any_cal = any(g["calibration"].ready for g in self.grabbers.values())
        chosen, why = None, None
        for b in blocks:
            b["cam_mm"] = [round(x, 1) for x in b["cam_mm"]]
            if any_cal:
                g, res = self._closest_arm(b["cam_mm"])
                if g is not None:
                    b["arm_key"], b["arm_name"], b["arm_mm"] = g["key"], g["name"], [round(x, 1) for x in res]
                    chosen = chosen or b
                else:
                    why = why or res
        if not any_cal:
            chosen = blocks[0] if blocks else None
        what = f"{color} block" if color else "blocks"
        if chosen is None and blocks:
            banner = f"Saw the {what}, but {why}"
        elif chosen is None:
            banner = f"No {what} found on the table"
        else:
            banner = (f"Found the {chosen['color']} block"
                      + (f"; {chosen['arm_name']} is closest" if chosen.get("arm_name") else "")
                      + ("" if color else f" (+{len(blocks) - 1} more)"))
        jpeg = await asyncio.to_thread(vision.annotate, img, blocks, chosen, v["colors"], banner)
        status_page.set_snapshot(jpeg)
        result = {"found": chosen is not None, "banner": banner, "color": color, "chosen": chosen,
                  "blocks": [{k: b[k] for k in ("color", "pixels", "uv", "cam_mm", "arm_name", "arm_mm") if k in b}
                             for b in blocks],
                  "time": time.time()}
        if record:
            self._last_look = {k: result[k] for k in ("found", "banner", "time")}
        return result

    async def _calib_record(self, color: str, arm: str = "") -> Dict[str, Any]:
        """`arm` is holding a `color` block somewhere over the table: pair what the camera sees
        with where that arm is. Nothing moves."""
        g = self._grabber(arm)
        if self._action_task is not None and not self._action_task.done():
            raise ValueError("an arm is moving; wait until it stops before recording")
        if g["arm"] is None:
            raise ValueError(f"{g['name']} isn't available (is its machine online?)")
        look = await self._look(color, record=True, on_mat=False)
        if not look["blocks"]:
            raise ValueError(f"the camera can't see a {color} block — make sure it's in the gripper and in view")
        pose = await g["arm"].get_end_position()
        arm_pose = {"x": pose.x, "y": pose.y, "z": pose.z, "o_x": pose.o_x, "o_y": pose.o_y, "o_z": pose.o_z,
                    "theta": pose.theta}
        cal = g["calibration"]
        if cal.points:
            first = cal.points[0]["arm"]
            tilt = math.degrees(math.acos(max(-1.0, min(1.0, first["o_x"] * arm_pose["o_x"] + first["o_y"] * arm_pose["o_y"]
                                                        + first["o_z"] * arm_pose["o_z"]))))
            if tilt > 8 or abs(((arm_pose["theta"] - first["theta"] + 180) % 360) - 180) > 10:
                raise ValueError(f"keep {g['name']}'s gripper pointing the same way for every point "
                                 f"(it's {tilt:.0f}° off from the first point); re-orient it and try again")
        cam = look["blocks"][0]["cam_mm"]   # largest blob of that color
        cal.add(cam, arm_pose, color)
        LOGGER.info(f"{g['name']} calibration point {len(cal.points)}: cam {cam} -> arm "
                    f"{[round(arm_pose[k], 1) for k in ('x', 'y', 'z')]}")
        return {**cal.summary(), "arm": g["name"]}

    async def _calib_undo(self, arm: str = "") -> Dict[str, Any]:
        g = self._grabber(arm)
        g["calibration"].undo()
        return {**g["calibration"].summary(), "arm": g["name"]}

    async def _calib_clear(self, arm: str = "") -> Dict[str, Any]:
        g = self._grabber(arm)
        g["calibration"].clear()
        return {**g["calibration"].summary(), "arm": g["name"]}

    async def _save_handoff_pose(self, arm: str = "") -> Dict[str, Any]:
        g = self._grabber(arm)
        if self._action_task is not None and not self._action_task.done():
            raise ValueError("an arm is moving; wait until it finishes before saving a pose")
        joints = list((await g["arm"].get_joint_positions()).values)
        self._poses[self._handoff_key(g)] = {"joints": joints, "saved_at": time.time()}
        _save_poses(self._poses)
        return {"saved": "handoff", "arm": g["name"], "joints": joints}

    async def _clear_handoff_pose(self, arm: str = "") -> Dict[str, Any]:
        g = self._grabber(arm)
        existed = self._poses.pop(self._handoff_key(g), None) is not None
        _save_poses(self._poses)
        return {"cleared": "handoff", "arm": g["name"], "existed": existed}

    async def _grab(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Voice: "grab the <color> block". With a picker that has a wrist camera, it takes a fresh picture
        from where it is and picks the block up; otherwise the table camera looks and the closest
        calibrated arm goes."""
        color = cmd["color"]
        c = self._picker_cam()
        if c is not None:
            return await self._grab_described({**cmd, "color": color, "noun": "block", "what": f"{color} block"})
        if not any(g["calibration"].ready for g in self.grabbers.values()):
            warn = next((g["calibration"].warning for g in self.grabbers.values() if g["calibration"].warning), None)
            raise ValueError("the camera isn't calibrated to an arm yet — calibrate one on the status page first"
                             + (f" ({warn})" if warn else ""))
        self._set_phase("running", current=f"Looking for the {color} block")
        look = await self._look(color, record=True)
        if not look["found"]:
            raise NotFound(look["banner"])
        ch = look["chosen"]
        return await self._pick(self.grabbers[ch["arm_key"]], ch["cam_mm"], f"{color} block")

    async def _squeeze(self, g: Dict[str, Any]) -> bool:
        """Close the gripper on something; True if it's holding it. With grip_force set, the UFactory G2
        gripper closes with that much force (a gentler squeeze than its default)."""
        gripper = g["gripper"]
        force = g.get("grip_force")
        if force and not g.get("force_unsupported"):
            try:
                await gripper.do_command({"grab_with_torque": {"position": 0.0, "speed": 2000.0, "torque": float(force)}})
                await asyncio.sleep(0.2)
                hs = await gripper.is_holding_something()
                return bool(getattr(hs, "is_holding_something", False))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                g["force_unsupported"] = True
                LOGGER.warning(f"{_cap(g['name'])}'s gripper doesn't take a grip force ({_friendly_error(e)}); "
                               "using its normal grab")
        return await gripper.grab()

    @contextlib.asynccontextmanager
    async def _pick_motion(self, g: Dict[str, Any]):
        """While picking something up, the arm accelerates harder (pick_accel), then goes back to normal."""
        accel = g.get("pick_accel")
        if not accel or g["arm"] is None:
            yield
            return
        try:
            await g["arm"].do_command({"set_acceleration": accel})
            LOGGER.info(f"{_cap(g['name'])}: acceleration {accel:.0f} deg/s² while picking up")
        except Exception as e:
            LOGGER.warning(f"{_cap(g['name'])}: couldn't raise its acceleration ({_friendly_error(e)})")
            yield
            return
        try:
            yield
        finally:
            try:
                await asyncio.shield(g["arm"].do_command({"set_acceleration": g["normal_accel"]}))
            except BaseException as e:   # incl. a cancel ("stop"): the shielded reset still goes through
                LOGGER.warning(f"{_cap(g['name'])}: resetting its acceleration: {_friendly_error(e) if isinstance(e, Exception) else 'cancelled'}")

    async def _sync_holding(self, g: Dict[str, Any], confirm: int = 1) -> None:
        """Believe the gripper over what we remember: it may have been opened by hand or from the Viam app
        (or closed on something) since our last grab. confirm: how many readings in a row must disagree
        before we change our mind (the background check uses 3, as some grippers' sensors flicker)."""
        if g.get("gripper") is None:
            return
        try:
            hs = await asyncio.wait_for(g["gripper"].is_holding_something(), timeout=3)
        except Exception as e:
            LOGGER.debug(f"{g['name']}: couldn't ask the gripper whether it's holding something: {e}")
            return
        g["grip_checked"] = time.time()
        holding = bool(getattr(hs, "is_holding_something", False))
        if bool(g["holding"]) == holding:
            g["_hold_votes"] = 0
            return
        g["_hold_votes"] = g.get("_hold_votes", 0) + 1
        if g["_hold_votes"] < confirm:
            return
        g["_hold_votes"] = 0
        if g["holding"] and not holding:
            was = "" if g["holding"] == "something" else f": no longer holding the {g['holding']}"
            LOGGER.info(f"{_cap(g['name'])}'s gripper is empty now{was}")
            g["holding"] = None
        elif holding and not g["holding"]:
            LOGGER.info(f"{_cap(g['name'])}'s gripper is holding something")
            g["holding"] = "something"

    async def _fresh_objects(self, c: Dict[str, Any], frames: int = 2, timeout: float = 8.0) -> List[Dict[str, Any]]:
        """What a live camera sees, from detections made after now (the camera is woken up if no page is
        watching it; two frames, because a thing is only listed once it's been seen twice)."""
        name = c["camera"]
        start, t0 = self._det_n.get(name, 0), time.time()
        while time.time() - t0 < timeout:
            status_page.want_live(name)
            if self._det_n.get(name, 0) >= start + frames:
                break
            await asyncio.sleep(0.2)
        return list(status_page.live_status(name).get("objects") or [])

    def _pick_object(self, objs: List[Dict[str, Any]], c: Dict[str, Any], color: Optional[str],
                     noun: Optional[str], which: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """The seen object that best fits "red ball": the color must match; the kind should (a thing the
        camera hasn't named yet still counts, one it named as something else counts less). With `which`,
        only the object numbered that way among its namesakes ("white cup 2") counts."""
        nouns = _NOUNS.get(noun, {noun}) if noun else set()
        objs = _number_duplicates(objs)
        scored, refused = [], None
        if which is not None:
            fitting = [o for o in objs if (not color or color == (o.get("block") or o.get("color") or "").lower()
                                              or color in (o.get("label") or "").lower().split())
                       and (not nouns or (o.get("kind") or "").lower() in nouns
                            or any(n in (o.get("label") or "").lower().split() for n in nouns))]
            if fitting and not any(o.get("n") == which for o in fitting):
                most = max(o.get("count", 1) for o in fitting)
                return None, f"only see {most} of those, not a number {which}"
            objs = [o for o in objs if o.get("n") == which]
        for o in objs:
            label = (o.get("label") or "").lower()
            kind = (o.get("kind") or "").lower()
            ocol = (o.get("block") or o.get("color") or "").lower()
            lw = label.split()
            score = 0
            if color:
                if color != ocol and color not in lw:
                    continue
                score += 3
            if nouns:
                if kind in nouns or any(n in lw for n in nouns):
                    score += 4
                elif kind or not label.endswith(" object"):
                    score -= 2
            if score <= 0:
                continue
            info = self._object_grab_info(o, c) or {}
            if "why" in info:
                refused = refused or (label, info["why"])
                continue
            scored.append((score, -(o.get("dist_mm") or 9999), o))
        if scored:
            return max(scored, key=lambda x: (x[0], x[1]))[2], None
        if refused:
            return None, f"saw the {refused[0]}, but {refused[1]}"
        return None, None

    async def _grab_described(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """"Pick up the red ball": the picker looks with its own camera, finds the best match among the
        things it sees (by color and name) and picks it up."""
        c = self._picker_cam()
        if c is None:
            raise ValueError("no arm with its own camera is set to pick things up (vision.picker)")
        g = self.grabbers[c["arm"]]
        name = _cap(g["name"])
        color, noun, which = cmd.get("color"), cmd.get("noun"), cmd.get("which")
        if not color and not noun:
            raise NotFound("Tell me what to grab, like “grab the red ball”")
        await self._sync_holding(g)
        if g["holding"]:
            raise ValueError(f"{name} is already holding the {g['holding']} — say “let go” first")
        what = cmd.get("what") or " ".join(x for x in (color, noun) if x)
        self._action_arms = [g["key"]]
        self._set_phase("running", current=f"{name} is looking for the {what}")
        if which is not None:
            # "white cup 2": the numbers are the ones the page shows, counted left to right in the desk
            # camera's picture, so look there (and let its calibration pick the arm), not through the wrist
            desk = next((d for d in self.live_cams if not d.get("arm") and d["cam"] is not None), None)
            if desk is not None:
                objs = await self._fresh_objects(desk)
                best, why = self._pick_object(objs, desk, color, noun, which)
                if best is None:
                    raise NotFound(_cap(why or f"I can't see a {what} on the desk"))
                tag = (best.get("tag") or best["label"])
                LOGGER.info(f"“{what}” -> the {tag.lower()} in {desk['label']}")
                return await self._grab_seen(best, desk, tag[:1].lower() + tag[1:])
        objs = await self._fresh_objects(c)
        best, why = self._pick_object(objs, c, color, noun, which)
        if best is None:
            if color and noun in (None, "block") and not why:   # nothing named: a plain color search
                return await self._grab_with_wrist(c, color)
            raise NotFound(_cap(why or f"{name} can't see a {what} from where it is"))
        p, why = self._wrist_point(best, c)
        if p is None:
            raise ValueError(why)
        tag = best.get("tag") or best["label"]
        label = tag[:1].lower() + tag[1:]
        LOGGER.info(f"“{what}” -> the {label} that {g['name']} sees")
        return await self._pick_at(g, p, label, float((best.get("size_mm") or [0, 0, 0])[2] or 0),
                                   self._wrist_extras(best, c))

    async def _grab_seen(self, obj: Dict[str, Any], c: Dict[str, Any], what: str) -> Dict[str, Any]:
        """Pick up `obj` as camera `c` sees it: from the desk camera the closest calibrated arm goes,
        from a wrist camera the arm it's mounted on. Runs inside the current action."""
        info = self._object_grab_info(obj, c)
        if not info:
            raise ValueError("grabbing isn't set up for this camera")
        if "why" in info:
            raise NotFound(_cap(f"saw the {what}, but {info['why']}"))
        g = self.grabbers[info["key"]]
        await self._sync_holding(g)
        if g["holding"]:
            raise ValueError(f"{_cap(g['name'])} is already holding the {g['holding']} — say “let go” first")
        self._action_arms = [g["key"]]
        self._set_phase("running", current=f"{_cap(g['name'])} is reaching for the {what}")
        if info.get("via") == "wrist":
            p, why = self._wrist_point(obj, c)
            if p is None:
                raise ValueError(why)
            return await self._pick_at(g, p, what, float((obj.get("size_mm") or [0, 0, 0])[2] or 0),
                                       self._wrist_extras(obj, c))
        return await self._pick(g, self._object_cam_mm(obj, c), what)

    async def _grab_with_wrist(self, c: Dict[str, Any], color: str) -> Dict[str, Any]:
        g = self.grabbers[c["arm"]]
        name = _cap(g["name"])
        await self._sync_holding(g)
        if g["holding"]:
            raise ValueError(f"{name} is already holding the {g['holding']} — say “let go” first")
        self._action_arms = [g["key"]]
        self._set_phase("running", current=f"{name} is looking for the {color} block")
        cam, arm = c["cam"], c["arm_res"]
        res, pose = await asyncio.gather(asyncio.wait_for(cam.get_images(), timeout=8),
                                         asyncio.wait_for(arm.get_end_position(), timeout=8))
        named = res[0] if isinstance(res, tuple) else res
        img, depth = await asyncio.to_thread(vision.split_images, named)
        intr = (self._live_last.get(c["camera"]) or {}).get("intr")
        if not intr:
            p = (await cam.get_properties()).intrinsic_parameters
            intr = {"fx": p.focal_x_px, "fy": p.focal_y_px, "cx": p.center_x_px, "cy": p.center_y_px}
        blocks = await asyncio.to_thread(vision.find_blocks, img, depth, intr, self.vision["colors"], color,
                                         120, 2, self.vision["on_mat_min"])
        why = f"{name} can't see a {color} block from where it is"
        for b in blocks:                                    # largest first; take the first one it can reach
            p = self._wrist_to_base(c, b["cam_mm"], pose)
            problem = self._wrist_target_problem(g, p)
            if problem is None:
                return await self._pick_at(g, p, f"{color} block", 25.0)
            why = f"saw the {color} block, but {problem}"
        raise NotFound(_cap(why))

    async def _grab_object(self, oid: int, cam_name: str = "") -> Dict[str, Any]:
        """Status page: an object was clicked in one of the cameras and "Grab" pressed. From the table
        camera the closest calibrated arm goes; from a wrist camera, the arm that camera is on.
        Runs in the background like a voice command, so "Hey buddy, stop" halts it."""
        found = None
        for c in self.live_cams:
            if cam_name and c["camera"] != cam_name:
                continue
            obj = next((o for o in _number_duplicates(status_page.live_status(c["camera"]).get("objects") or [])
                        if o.get("id") == oid), None)
            if obj is not None:
                found = (c, obj)
                break
        if found is None:
            raise ValueError("that object isn't on the table any more")
        c, obj = found
        if self._action_task is not None and not self._action_task.done():
            raise ValueError("an arm is busy; wait for it to finish (or say “Hey buddy, stop”)")
        await asyncio.gather(*[self._sync_holding(g) for g in self.grabbers.values()])
        info = self._object_grab_info(obj, c)
        if not info:
            raise ValueError("grabbing isn't set up for this camera")
        if "why" in info:
            raise ValueError(info["why"])
        g = self.grabbers[info["key"]]
        tag = obj.get("tag") or obj["label"]
        what = tag[:1].lower() + tag[1:]   # "red block", "white cup 2"
        if info.get("via") == "wrist":
            p, _ = self._wrist_point(obj, c)
            height = float((obj.get("size_mm") or [0, 0, 0])[2] or 0)
            pick, kind = (g, p, what, height, self._wrist_extras(obj, c)), "wrist"
        else:
            pick, kind = (g, self._object_cam_mm(obj, c), what), "table"
        label = f"Grabbed with {g['name']}"
        cmd = {"action": "pick", "pick": pick, "pick_kind": kind, "resource_name": g["key"], "name": g["name"],
               "label": label, "rec": {"clicked": True}}
        text = f"Clicked the {what}" + (f" in {c['label']}" if len(self.live_cams) > 1 else "")
        LOGGER.info(f"status page: {text} -> {g['name']}")
        self.state["last_match"] = g["key"]
        self._set_phase("running", current=f"{_cap(g['name'])} is reaching for the {what}")
        self._action_arms = [g["key"]]
        self._action_task = asyncio.get_event_loop().create_task(self._run(cmd, text, label))
        self._rec_start(f"{_cap(g['name'])} grabbing the {what}", text, self._action_arms)
        return {"started": f"{_cap(g['name'])} is going for the {what}", "arm": g["name"]}

    async def _stop_from_page(self) -> Dict[str, Any]:
        """The page's Stop button: halt every arm (and gripper) right away, whatever is running."""
        cmd = {"action": "stop", "resource_name": "every arm",
               "stop_targets": [(rn, info["arm"]) for rn, info in self._arms.items() if info.get("arm") is not None]}
        LOGGER.info("Stop clicked on the status page")
        return await self._stop(cmd, "Clicked Stop", "Stop")

    async def _reset_from_page(self, rn: str) -> Dict[str, Any]:
        """The page's Reset button: send one arm back to its saved starting position."""
        if self._action_task is not None and not self._action_task.done():
            raise ValueError("an arm is busy; wait for it to finish (or press Stop)")
        info = self._arms.get(rn)
        if info is None or info.get("arm") is None:
            raise ValueError(f"no arm called '{rn}' (is its machine online?)")
        if not self._poses.get("home:" + rn):
            raise ValueError(f"no starting position is saved for {info['name']} yet")
        label = f"Resetting {info['name']}"
        cmd = self._mk_cmd(action="home", resource_name=rn, resource=info["arm"], name=info["name"], label=label)
        cmd["rec"] = {"clicked": True}
        self._set_phase("running", current=label)
        self._action_arms = [rn]
        self._action_task = asyncio.get_event_loop().create_task(self._run(cmd, "Clicked Reset", label))
        self._rec_start(label, "Clicked Reset", self._action_arms)
        return {"started": label}

    async def _release_from_page(self, arm: str = "") -> Dict[str, Any]:
        if self._action_task is not None and not self._action_task.done():
            raise ValueError("an arm is busy; wait for it to finish")
        cmd = next((c for c in self.commands if c["action"] == "release"), None) or \
            {"action": "release", "resource_name": ", ".join(self.grabbers), "name": "", "label": "Letting go"}
        cmd = {**cmd, "rec": {"clicked": True}}
        if arm:                                   # one arm's Release button: open just that gripper
            if arm not in self.grabbers:
                raise ValueError(f"'{arm}' has no gripper set up (add it to vision.arms)")
            cmd["only"] = arm
        self._set_phase("running", current="Letting go")
        self._action_arms = [arm] if arm else self._arms_for(cmd)
        self._action_task = asyncio.get_event_loop().create_task(self._run(cmd, "Clicked Let go", "Let go"))
        self._rec_start("Letting go", "Clicked Let go", self._action_arms)
        return {"started": "Letting go"}

    async def _pick_steps(self, g: Dict[str, Any], cam_mm: List[float], what: str) -> Dict[str, Any]:
        """Arm `g` picks up the thing the camera sees at cam_mm, then brings it to that arm's
        hand-off spot (if taught) or holds it up where it was."""
        v, cal = self.vision, g["calibration"]
        arm, gripper, name = g["arm"], g["gripper"], _cap(g["name"])
        if arm is None or gripper is None:
            raise ValueError(f"{name} or its gripper isn't available (is its machine online?)")
        if g["holding"]:
            raise ValueError(f"{name} is already holding the {g['holding']} — say “let go” first")
        if not cal.ready:
            raise ValueError(f"{name} isn't calibrated to the camera yet")
        self._action_arms = [g["key"]]
        target = cal.to_arm(cam_mm)

        # sanity limits from the calibration: never reach far below the lowest point we recorded
        z_lo, z_hi = cal.arm_z_range()
        if target[2] < z_lo - 40:
            raise ValueError(f"the {what} looks {z_lo - target[2]:.0f} mm below {name}'s lowest calibration "
                             "point — not moving there; record a calibration point with a block resting just above the table")
        o = cal.orientation()
        safe_z = float(v["safe_z_mm"]) if v["safe_z_mm"] is not None else max(z_hi, target[2] + v["approach_mm"])

        def pose(x, y, z, orient=o):
            return Pose(x=x, y=y, z=z, o_x=orient["o_x"], o_y=orient["o_y"], o_z=orient["o_z"], theta=orient["theta"])

        # open, rise to a safe height where we are, travel above the target, come straight down
        self._set_phase("running", current=f"{name} is reaching for the {what}")
        await gripper.open()
        here = await arm.get_end_position()
        if here.z < safe_z - 5:
            await arm.move_to_position(Pose(x=here.x, y=here.y, z=safe_z, o_x=here.o_x, o_y=here.o_y,
                                            o_z=here.o_z, theta=here.theta))
        await arm.move_to_position(pose(target[0], target[1], max(safe_z, target[2] + v["approach_mm"])))
        await arm.move_to_position(pose(target[0], target[1], target[2]))

        # grab and check we actually caught it
        self._set_phase("running", current=f"{name} is grabbing the {what}")
        caught = await self._squeeze(g)
        await arm.move_to_position(pose(target[0], target[1], target[2] + v["lift_mm"]))
        if not caught:
            await gripper.open()
            raise ValueError(f"{name} missed the {what}: the gripper closed on nothing "
                             "(calibration may be off; check the status page snapshot)")
        g["holding"] = what

        # bring it to this arm's hand-off spot if one is taught, otherwise hold it up here
        handoff = self._poses.get(self._handoff_key(g))
        if handoff:
            self._set_phase("running", current=f"{name} is bringing you the {what}")
            await arm.move_to_joint_positions(JointPositions(values=[float(j) for j in handoff["joints"]]))
        self._current = f"{name} is holding the {what} — say “let go”"
        return {"grabbed": what, "arm": g["name"], "at": target}

    async def _pick_at(self, g: Dict[str, Any], top: List[float], what: str, height_mm: float,
                       extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return await self._grab_then_reset(g, self._pick_at_steps(g, top, what, height_mm, extras))

    async def _ik_ready(self, g: Dict[str, Any]) -> Optional[List[float]]:
        """The arm's joints, if our own kinematics agree with where the arm says it is (then short
        straight moves can be sent as joint positions, skipping the motion planner). None otherwise."""
        if g.get("ik_ok") is False:
            return None
        arm = g["arm"]
        jp, here = await asyncio.gather(arm.get_joint_positions(), arm.get_end_position())
        q = [float(v) for v in jp.values]
        if len(q) != 6:
            g["ik_ok"] = False
            return None
        T = kin.fk(q)
        off = math.dist(T[:3, 3], (here.x, here.y, here.z))
        tilt = float(T[:3, 2] @ [here.o_x, here.o_y, here.o_z])
        if off > 3 or tilt < 0.999:
            g["ik_ok"] = False
            LOGGER.warning(f"{_cap(g['name'])}: its kinematics don't match ({off:.1f} mm off); "
                           "using the motion planner for every move")
            return None
        g["ik_ok"] = True
        return q

    async def _move_line(self, arm, path: List[List[float]]) -> None:
        await arm.move_through_joint_positions([JointPositions(values=w) for w in path])

    async def _pick(self, g: Dict[str, Any], cam_mm: List[float], what: str) -> Dict[str, Any]:
        return await self._grab_then_reset(g, self._pick_steps(g, cam_mm, what))

    async def _return_home(self, g: Dict[str, Any]) -> bool:
        """Send arm `g` to its saved starting position; False if none is saved."""
        if not self._poses.get("home:" + g["key"]) or g["arm"] is None:
            return False
        await self._go_home({"resource": g["arm"], "resource_name": g["key"], "name": g["name"]})
        return True

    async def _grab_then_reset(self, g: Dict[str, Any], steps) -> Dict[str, Any]:
        """Run a grab, then send the arm back to its starting position (if one is saved): holding what it
        picked up, or with the gripper open if the grab failed. A taught hand-off spot takes precedence
        after a success. "Stop" (a cancel) leaves the arm where it halted."""
        async with self._pick_motion(g):
            return await self._grab_then_reset_inner(g, steps)

    async def _grab_then_reset_inner(self, g: Dict[str, Any], steps) -> Dict[str, Any]:
        try:
            res = await steps
        except (asyncio.CancelledError, NotFound):
            raise
        except Exception as e:
            LOGGER.info(f"{_cap(g['name'])}: grab didn't work ({_friendly_error(e)}); going back to the start")
            try:
                if g["gripper"] is not None and not g["holding"]:
                    await g["gripper"].open()
                await self._return_home(g)
            except asyncio.CancelledError:
                raise
            except Exception as e2:
                LOGGER.error(f"{_cap(g['name'])} couldn't go back to its starting position: {_friendly_error(e2)}")
            raise
        if not self._poses.get(self._handoff_key(g)) and g["holding"]:
            held = g["holding"]
            if await self._return_home(g):
                self._current = f"{_cap(g['name'])} is holding the {held} — say “let go”"
        return res

    async def _pick_at_steps(self, g: Dict[str, Any], top: List[float], what: str, height_mm: float,
                             extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Arm `g` picks up something whose top centre is at `top` (its own base frame, mm), found with
        its wrist camera. The gripper points straight down, so the flange sits finger_mm above the
        fingertips. The fingertips never go lower than 8 mm above the table top."""
        v = self.vision
        arm, gripper, name = g["arm"], g["gripper"], _cap(g["name"])
        if arm is None or gripper is None:
            raise ValueError(f"{name} or its gripper isn't available (is its machine online?)")
        await self._sync_holding(g)
        if g["holding"]:
            raise ValueError(f"{name} is already holding the {g['holding']} — say “let go” first")
        self._action_arms = [g["key"]]
        tcp, floor = v["finger_mm"], v["table_z_mm"] + 8
        lowest_flange = v["table_z_mm"] + 6 + v["planner_finger_mm"]   # the planner's gripper model clears the table
        # fingertips partway down its side (most of a short thing, the top 3 cm of a tall one), never below the floor
        grip_z = max(top[2] - min(max(height_mm * 0.6, 10.0), 30.0), floor)
        x, y = top[0], top[1]
        # don't trust the configured table height alone: the camera measured the surface next to it
        if height_mm > 3:
            floor = max(floor, top[2] - height_mm + 8)
            grip_z = max(grip_z, floor)
        fast = await self._pick_straight(g, x, y, grip_z, what, height_mm, (extras or {}).get("axis"))
        if fast is not None:
            return fast
        here = await arm.get_end_position()
        theta = here.theta if here.o_z < -0.8 else 0.0   # keep the wrist's current twist if already pointing down

        def flange(z_grip):
            return Pose(x=x, y=y, z=max(z_grip + tcp, lowest_flange), o_x=0.0, o_y=0.0, o_z=-1.0, theta=theta)

        reach = max(grip_z + tcp, lowest_flange) - tcp     # where the fingertips will really end up
        LOGGER.info(f"{name}: {what} top at z={top[2]:.0f} mm (about {height_mm:.0f} mm tall); fingertips to "
                    f"z={reach:.0f}" + (f" (aimed for {grip_z:.0f}; the planner won't go lower)" if reach > grip_z + 1 else ""))
        if reach > top[2] - 5:
            raise ValueError(f"the {what} is too short to reach: the motion planner's gripper model won't let the fingers "
                             f"lower than {reach - top[2]:.0f} mm above its top")

        above = grip_z + v["approach_mm"]
        self._set_phase("running", current=f"{name} is reaching for the {what}")
        await gripper.open()
        if here.z < above + tcp - 5:   # rise straight up first if we're lower than the approach height
            await arm.move_to_position(Pose(x=here.x, y=here.y, z=above + tcp, o_x=here.o_x, o_y=here.o_y,
                                            o_z=here.o_z, theta=here.theta))
        await arm.move_to_position(flange(above))
        await arm.move_to_position(flange(grip_z))

        self._set_phase("running", current=f"{name} is grabbing the {what}")
        caught = await self._squeeze(g)
        await arm.move_to_position(flange(grip_z + v["lift_mm"]))
        if not caught:
            await gripper.open()
            raise ValueError(f"{name} missed the {what}: the gripper closed on nothing")
        g["holding"] = what

        handoff = self._poses.get(self._handoff_key(g))
        if handoff:
            self._set_phase("running", current=f"{name} is bringing you the {what}")
            await arm.move_to_joint_positions(JointPositions(values=[float(j) for j in handoff["joints"]]))
        self._current = f"{name} is holding the {what} — say “let go”"
        return {"grabbed": what, "arm": g["name"], "at": [x, y, grip_z]}

    async def _pick_straight(self, g: Dict[str, Any], x: float, y: float, grip_z: float, what: str,
                             height_mm: float, axis: Optional[List[float]]) -> Optional[Dict[str, Any]]:
        """Pick up something at (x, y) with straight-line moves worked out here (no motion planner):
        over it, turning the wrist so the fingers close across its narrow side, straight down, grab,
        straight up. The gripper opens on the way over. None (nothing moved) if a path can't be found."""
        import numpy as np
        v = self.vision
        arm, gripper, name = g["arm"], g["gripper"], _cap(g["name"])
        q0 = await self._ik_ready(g)
        if q0 is None:
            return None
        tcp = v["finger_mm"]
        cur = kin.fk(q0)
        xa = np.array([cur[0, 0], cur[1, 0], 0.0])
        if np.linalg.norm(xa) < 0.3:
            xa = np.array([1.0, 0.0, 0.0])
        xa /= np.linalg.norm(xa)
        if axis:   # fingers close along the flange's y: lay its x along the thing's long side
            want = np.array([axis[0], axis[1], 0.0])
            want /= np.linalg.norm(want)
            xa = want if want @ xa >= 0 else -want   # the shorter way round
        above_z = grip_z + v["approach_mm"] + tcp
        T_above = kin.pose_down(x, y, above_z, xa)
        T_grip = kin.pose_down(x, y, grip_z + tcp, xa)
        T_lift = kin.pose_down(x, y, grip_z + v["lift_mm"] + tcp, xa)

        def plan():
            pts = []
            q = q0
            if cur[2, 2] > -0.9 or math.hypot(cur[0, 3] - x, cur[1, 3] - y) > 450:
                return None, None, None      # not pointing down nearby: the planner does the travel
            if cur[2, 3] < above_z - 5:       # low: rise straight up first
                rise = kin.line(q, np.vstack([np.column_stack([cur[:3, :3], [cur[0, 3], cur[1, 3], above_z]]),
                                              [0, 0, 0, 1]]))
                if rise is None:
                    return None, None, None
                pts += rise
                q = rise[-1]
            travel = kin.line(q, T_above)
            if travel is None:
                return None, None, None
            pts += travel
            down = kin.line(travel[-1], T_grip, step_mm=10)
            if down is None:
                return None, None, None
            up = kin.line(down[-1], T_lift)
            return pts, down, up

        travel, down, up = await asyncio.to_thread(plan)
        if up is None:
            LOGGER.info(f"{name}: no straight path to the {what}; using the motion planner")
            return None
        turn = math.degrees(math.atan2(xa[1], xa[0]))
        LOGGER.info(f"{name}: {what} at ({x:.0f}, {y:.0f}), about {height_mm:.0f} mm tall; fingertips to z={grip_z:.0f}, "
                    f"wrist at {turn:.0f}°" + (" (across its long side)" if axis else "") + " — straight moves, no planner")
        self._set_phase("running", current=f"{name} is reaching for the {what}")
        await asyncio.gather(gripper.open(), self._move_line(arm, travel))
        await self._move_line(arm, down)
        self._set_phase("running", current=f"{name} is grabbing the {what}")
        caught = await self._squeeze(g)
        await self._move_line(arm, up)
        if not caught:
            await gripper.open()
            raise ValueError(f"{name} missed the {what}: the gripper closed on nothing")
        g["holding"] = what
        handoff = self._poses.get(self._handoff_key(g))
        if handoff:
            self._set_phase("running", current=f"{name} is bringing you the {what}")
            await arm.move_to_joint_positions(JointPositions(values=[float(j) for j in handoff["joints"]]))
        self._current = f"{name} is holding the {what} — say “let go”"
        return {"grabbed": what, "arm": g["name"], "at": [x, y, grip_z], "straight": True}

    async def _release(self, only: Optional[str] = None) -> Dict[str, Any]:
        holders = [g for g in self.grabbers.values() if g["holding"] and (not only or g["key"] == only)]
        fallback = ([self.grabbers[only]] if only else
                    [self.grabbers[self.picker]] if self.picker else list(self.grabbers.values())[:1])
        targets = [g for g in (holders or fallback) if g["gripper"] is not None]
        self._action_arms = [g["key"] for g in targets]
        await asyncio.gather(*[g["gripper"].open() for g in targets])
        held = [g["holding"] for g in holders]
        for g in holders:
            g["holding"] = None
        self._current = ("Let go of the " + " and the ".join(held)) if held else "Gripper opened"
        return {"released": held}

    # ---------- starting position ("reset arm seventeen") ----------

    async def _save_home(self, resource_name: str) -> Dict[str, Any]:
        """Remember an arm's current joint positions as its starting position (kept in wave_poses.json)."""
        arm = _find_arm(self, resource_name)
        if self._action_task is not None and not self._action_task.done():
            raise ValueError("an arm is moving; wait until it finishes before saving a position")
        joints = list((await arm.get_joint_positions()).values)
        self._poses["home:" + resource_name] = {"joints": joints, "saved_at": time.time()}
        _save_poses(self._poses)
        LOGGER.info(f"saved starting position for {resource_name}: {[round(v, 1) for v in joints]}")
        return {"saved": resource_name, "joints": joints}

    async def _go_home(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Move an arm back to its saved starting position. If it's down low (near the table) it first
        rises straight up, with the motion planner checking for collisions, then goes there joint by joint."""
        arm: Arm = cmd["resource"]  # type: ignore
        name = _cap(cmd["name"] or cmd["resource_name"])
        saved = self._poses.get("home:" + cmd["resource_name"])
        if not saved:
            raise ValueError(f"no starting position is saved for {name} yet")
        target = [float(j) for j in saved["joints"]]
        if len(target) != len(list((await arm.get_joint_positions()).values)):
            raise ValueError(f"the saved starting position for {name} doesn't match the arm; save it again")
        here = await arm.get_end_position()
        safe_z = float(cmd.get("safe_z_mm") or 300)
        if here.z < safe_z:
            self._set_phase("running", current=f"Lifting {name}")
            await arm.move_to_position(Pose(x=here.x, y=here.y, z=safe_z, o_x=here.o_x, o_y=here.o_y,
                                            o_z=here.o_z, theta=here.theta))
        self._set_phase("running", current=f"Resetting {name}")
        await arm.move_to_joint_positions(JointPositions(values=target))
        return {"reset": cmd["resource_name"]}

    async def _wave(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Remember where the arm is, move to its wave pose (if one is taught), wave,
        then go back to where it started. Without a wave pose it waves in place.
        If stopped, the arm stays where it halted (no automatic return)."""
        arm: Arm = cmd["resource"]  # type: ignore
        name = cmd["name"]
        home = list((await arm.get_joint_positions()).values)
        pose = self._wave_pose_for(cmd)
        if pose is not None and len(pose) != len(home):
            raise ValueError(f"saved wave pose for {name} has {len(pose)} joints, arm has {len(home)}; re-save it")
        center = pose if pose is not None else home

        j, amp, lim = cmd["joint"], cmd["amplitude_deg"], cmd["joint_limit_deg"]
        if not 0 <= j < len(center):
            raise ValueError(f"joint {j} out of range for a {len(center)}-joint arm")
        # Keep the wave inside the joint's safe range: near a limit, wave less on that side.
        targets = [min(center[j] + amp, lim), max(center[j] - amp, -lim)]
        if max(abs(t - center[j]) for t in targets) < 3:
            raise ValueError(f"joint {j} is at {center[j]:.0f}° in the wave pose, too close to its "
                             f"±{lim:.0f}° limit to wave; save a wave pose with that joint nearer the middle")

        # Raising to the wave pose and returning use the arm's normal speed; the wiggle itself goes fast.
        fast = {"speed_d": cmd["speed_deg_s"], "acceleration_d": cmd["accel_deg_s2"]}

        async def go(joints, quick=False):
            await arm.move_to_joint_positions(JointPositions(values=list(joints)), extra=fast if quick else None)

        try:
            if pose is not None:
                self._set_phase("running", current=f"Raising {name}")
                await go(pose)
            self._set_phase("running", current=self._label(cmd))
            for _ in range(cmd["times"]):
                for target in targets:
                    if abs(target - center[j]) < 1:
                        continue  # no room on this side
                    p = list(center)
                    p[j] = target
                    await go(p, quick=True)
            await go(center, quick=True)
            if pose is not None:
                self._set_phase("running", current=f"Returning {name}")
                await go(home)
        except asyncio.CancelledError:
            raise  # "stop" was said: don't move again
        except Exception:
            # Something failed partway: try to put the arm back where it started, then report.
            try:
                await go(home)
            except Exception as back_err:
                LOGGER.error(f"{name}: could not return to start after error: {back_err}")
            raise
        return {"waved": True, "from_pose": pose is not None, "joint": j, "times": cmd["times"]}

    async def do_command(
        self, command: Mapping[str, ValueTypes], *, timeout: Optional[float] = None, **kwargs
    ) -> Mapping[str, ValueTypes]:
        if "simulate" in command:
            return await self._route(str(command["simulate"]), typed=True)
        if "status" in command:
            snap = self._snapshot()
            snap.pop("history", None)
            return snap
        if "recordings" in command:   # {"recordings": true} -> the saved clips, newest first
            return {"dir": getattr(self, "rec_dir", ""), "status": _REC.status() if _REC else None,
                    "recordings": recorder.list_recordings(getattr(self, "rec_dir", "")),
                    "on_motion": [{"status": mr.rec.status(), "dir": mr.rec.out_dir,
                                   "recordings": recorder.list_recordings(mr.rec.out_dir)}
                                  for mr in _MOTION_RECS.values()]}
        if "record_stop" in command:   # end the running clip now (it is still saved)
            if _REC is not None:
                await _REC.stop_now()
            return {"stopping": bool(_REC and _REC.recording)}
        if "mic" in command:   # {"mic": true|false|"toggle"}
            v = command["mic"]
            return self._set_mic((not _MIC["on"]) if v == "toggle" else bool(v))
        if "save_live_frame" in command:   # debugging aid: dump the latest live frame next to the module
            out, intr = getattr(self, "_last_live", (None, None))
            if not out or out["depth_mm"] is None:
                return {"error": "no live frame yet (open the status page)"}
            import numpy as np
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                f"debug_frame_{command['save_live_frame']}.npz")
            np.savez_compressed(path, depth=out["depth_mm"], color=np.frombuffer(out["color"] or b"", np.uint8),
                                intr=np.array([intr[k] for k in ("fx", "fy", "cx", "cy")] if intr else []))
            return {"saved": path}
        if "save_home" in command:          # {"save_home": "a17-arm"}: its current position is its start
            return await self._save_home(str(command["save_home"]))
        if "clear_home" in command:
            existed = self._poses.pop("home:" + str(command["clear_home"]), None) is not None
            _save_poses(self._poses)
            return {"cleared": str(command["clear_home"]), "existed": existed}
        if "save_wave_pose" in command:
            return await self._save_wave_pose(str(command["save_wave_pose"]))
        if "clear_wave_pose" in command:
            return self._clear_wave_pose(str(command["clear_wave_pose"]))
        if self.vision:
            if "look" in command:   # take a picture and find blocks; never moves anything
                color = command["look"] if isinstance(command["look"], str) and command["look"] else None
                res = await self._look(color, record=True)
                res.pop("chosen", None)
                return res
            if "calibration" in command:
                return {g["name"]: g["calibration"].summary() for g in self.grabbers.values()}
            if "calib_record" in command:
                return await self._calib_record(str(command["calib_record"] or "green"), str(command.get("arm") or ""))
        return {"error": "supported commands: simulate, status, save_wave_pose, clear_wave_pose, look, "
                         "calibration, calib_record"}

    async def close(self):
        for t in (getattr(self, "_task", None), getattr(self, "_action_task", None),
                  getattr(self, "_level_task", None), getattr(self, "_arms_task", None),
                  *getattr(self, "_live_tasks", [])):
            if t is not None and not t.done():
                t.cancel()


if __name__ == "__main__":
    asyncio.run(Module.run_from_registry())
