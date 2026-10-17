"""
Records a camera while a voice command has the robot moving, and saves an .mp4 per command.

    start(...)  is called when a command starts. Frames are grabbed from the camera right away.
    The arms are watched (is_moving + joint changes) the whole time; the clip ends once no arm has
    moved for `idle_s` seconds (and the command itself has finished). If no arm moved at all, the
    frames are thrown away, so a "grab" that found nothing doesn't leave an empty clip behind.
    Another command while a clip is running just extends that clip.

    Arm state comes from `probe` when one is set (the router's shared poller), so recorders don't each
    flood the arms with their own requests. A check that fails or is stale counts as "don't know",
    which keeps the clip rolling rather than ending it: a slow network must not cut a video short.

Clips land in <project>/recordings/YYYY-MM-DD_HH-MM-SS_<command>.mp4 with a .json beside it.
Encoding uses ffmpeg: Homebrew's if it's installed, else the one bundled with the imageio-ffmpeg
package (pip-installed into the module's venv the first time). Without either, the JPEG frames are
kept in a folder instead so nothing is lost.
"""
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
try:
    from viam.logging import getLogger   # so these lines reach the Viam app's logs
except Exception:  # pragma: no cover
    from logging import getLogger
from typing import Any, Callable, Dict, List, Optional

LOGGER = getLogger(__name__)

_FFMPEG: Optional[str] = None
_FFMPEG_LOCK = threading.Lock()


def _works(path: Optional[str]) -> bool:
    """Does this ffmpeg actually run? (Homebrew's can break when a library it links to is upgraded.)"""
    if not path or not os.path.exists(path):
        return False
    try:
        return subprocess.run([path, "-hide_banner", "-version"], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def find_ffmpeg() -> Optional[str]:
    """Path to a working ffmpeg: one on the system, else the one bundled with imageio-ffmpeg
    (pip-installed into the module's venv the first time). Cached."""
    global _FFMPEG
    with _FFMPEG_LOCK:
        if _FFMPEG and os.path.exists(_FFMPEG):
            return _FFMPEG
        broken = []
        for p in dict.fromkeys(x for x in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if x):
            if _works(p):
                _FFMPEG = p
                return p
            if os.path.exists(p):
                broken.append(p)
        if broken:
            LOGGER.info(f"recordings: {', '.join(broken)} doesn't run; using the ffmpeg from imageio-ffmpeg instead")
        for attempt in range(2):
            try:
                import imageio_ffmpeg  # type: ignore
                p = imageio_ffmpeg.get_ffmpeg_exe()
                if _works(p):
                    _FFMPEG = p
                    return p
                break
            except Exception:
                if attempt:
                    break
                LOGGER.info("recordings: installing ffmpeg (imageio-ffmpeg) into the module's venv...")
                try:
                    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "imageio-ffmpeg"],
                                   check=True, timeout=600, capture_output=True)
                except Exception as e:
                    LOGGER.warning(f"recordings: couldn't install imageio-ffmpeg: {e}")
                    break
        return None


def encode(frames_dir: str, times: List[float], mp4: str, fps: float) -> Optional[str]:
    """f000001.jpg... in frames_dir -> an H.264 .mp4 that plays in real time. Returns an error or None."""
    ff = find_ffmpeg()
    if not ff:
        return "no working ffmpeg"
    lst = os.path.join(frames_dir, "frames.txt")
    with open(lst, "w") as f:   # each frame shown for as long as it really lasted
        for i in range(len(times)):
            dur = (times[i + 1] - times[i]) if i + 1 < len(times) else 1.0 / fps
            f.write(f"file 'f{i + 1:06d}.jpg'\nduration {max(0.001, dur):.4f}\n")
        f.write(f"file 'f{len(times):06d}.jpg'\n")
    real_fps = (len(times) - 1) / max(1e-6, times[-1] - times[0])
    out_fps = str(int(round(min(30.0, max(fps, real_fps)))))
    head = [ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-r", out_fps]
    tail = ["-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4]   # plain yuv420p: QuickTime-friendly
    err = ""
    for codec in (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"],
                  ["-c:v", "h264_videotoolbox", "-b:v", "6M"]):          # the Mac's own encoder, as a backup
        r = subprocess.run(head + codec + tail, capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            return None
        err = (r.stderr or "").strip()[-300:]
    return err or "ffmpeg failed"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "command"


class Recorder:
    def __init__(self, out_dir: str, fps: float = 10.0, idle_s: float = 10.0, max_s: float = 600.0,
                 keep_still: bool = False, label: str = "", url_prefix: str = ""):
        self.out_dir = out_dir
        self.label = label              # shown on the status page, e.g. "Arm 18 camera"
        self.url_prefix = url_prefix    # where its clips are under /recordings/ ("" or "arm-18/")
        self.fps = max(1.0, min(30.0, float(fps)))
        self.idle_s = max(1.0, float(idle_s))
        self.max_s = max(self.idle_s + 5, float(max_s))
        self.keep_still = keep_still
        self.camera = None
        self.camera_name = ""
        self.arms: Dict[str, Any] = {}           # resource name -> Arm
        self.busy: Callable[[], bool] = lambda: False
        # rn -> (moving, joints) from a shared poller, or None if it has nothing recent; unset: ask the arm
        self.probe: Optional[Callable[[str], Optional[tuple]]] = None
        self.workers = 3          # frame requests kept in flight (each one is a round trip to the camera)
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[Dict[str, Any]] = None
        self.last: Optional[Dict[str, Any]] = None    # the last saved clip (for the status page)
        self.error: Optional[str] = None

    # ---- set up from the service's config (called on every reconfigure; a running clip carries on) ----
    def configure(self, camera, camera_name: str, arms: Dict[str, Any], busy: Callable[[], bool]) -> None:
        self.camera, self.camera_name, self.arms, self.busy = camera, camera_name, arms, busy
        if not hasattr(self, "_born"):
            self._born = time.time()      # frames older than this were left by an earlier run
        if self.last is None:
            saved = list_recordings(self.out_dir, limit=1)
            if saved:
                self.last = {**saved[0], "url": self.url_prefix + saved[0]["file"]}
        if camera is not None and not getattr(self, "_warmed", False):
            self._warmed = True   # find (or install) ffmpeg now, so the first clip doesn't wait for it
            threading.Thread(target=self._warm, daemon=True).start()

    def _warm(self) -> None:
        ff = find_ffmpeg()
        if ff:
            self._convert_leftovers()
            self._recover_partials()
            LOGGER.info(f"recordings: {self.label or self.camera_name} ({self.camera_name}) is saved "
                        f"into {self.out_dir} (ffmpeg: {ff})")
        else:
            LOGGER.warning("recordings: no ffmpeg found or installable; clips will be saved as JPEG frame folders")

    def _recover_partials(self) -> None:
        """Clips cut off by a restart (their frames left in .partial/) -> .mp4, timed by when each frame was
        saved. Only frames from before this process started are touched, never a clip being recorded now."""
        root = os.path.join(self.out_dir, ".partial")
        try:
            names = sorted(os.listdir(root))
        except FileNotFoundError:
            return
        born = getattr(self, "_born", time.time())
        for n in names:
            d = os.path.join(root, n)
            try:
                if not os.path.isdir(d):
                    continue
                frames, k = sorted(f for f in os.listdir(d) if re.fullmatch(r"f\d{6}\.jpg", f)), 0
                while k < len(frames) and frames[k] == f"f{k + 1:06d}.jpg":   # numbered from 1, no gaps
                    k += 1
                frames = frames[:k]
                times = [os.path.getmtime(os.path.join(d, f)) for f in frames]
                if times and max(times) >= born - 1:
                    continue                                 # written by this process: a live clip
                if len(frames) < 2:
                    shutil.rmtree(d, ignore_errors=True)
                    continue
                for i in range(1, len(times)):
                    times[i] = max(times[i], times[i - 1] + 0.001)
                parts = n.split("_", 2)
                label = parts[2].replace("-", " ").capitalize() if len(parts) == 3 else n
                mp4 = os.path.join(self.out_dir, n + ".mp4")
                meta = {"camera": self.camera_name, "commands": [{"label": label, "heard": "", "at": times[0]}],
                        "started": datetime.fromtimestamp(times[0]).isoformat(timespec="seconds"),
                        "seconds": round(times[-1] - times[0], 2), "frames": len(times),
                        "fps": round((len(times) - 1) / max(1e-6, times[-1] - times[0]), 2),
                        "idle_s": self.idle_s, "recovered": "cut off by a restart; frame timing from file times"}
                err = encode(d, times, mp4, self.fps)
                if err is None:
                    meta.update(file=os.path.basename(mp4), bytes=os.path.getsize(mp4))
                    _write_json(mp4[:-4] + ".json", meta)
                    shutil.rmtree(d, ignore_errors=True)
                    LOGGER.info(f"recordings: recovered {n}.mp4 ({len(times)} frames; it was cut off by a restart)")
                else:
                    LOGGER.warning(f"recordings: couldn't recover {n}: {err}")
            except Exception as e:
                LOGGER.warning(f"recordings: couldn't recover {n}: {e}")

    def _convert_leftovers(self) -> None:
        """Clips saved as frame folders (when ffmpeg wasn't working) -> .mp4, now that it is."""
        try:
            names = [n for n in os.listdir(self.out_dir) if n.endswith("_frames")]
        except FileNotFoundError:
            return
        for n in names:
            d = os.path.join(self.out_dir, n)
            try:
                with open(os.path.join(d, "recording.json")) as f:
                    meta = json.load(f)
                times = meta.pop("frame_times")
                mp4 = os.path.join(self.out_dir, n[:-len("_frames")] + ".mp4")
                if encode(d, times, mp4, self.fps) is None:
                    meta.update(file=os.path.basename(mp4), bytes=os.path.getsize(mp4))
                    _write_json(mp4[:-4] + ".json", meta)
                    shutil.rmtree(d, ignore_errors=True)
                    LOGGER.info(f"recordings: converted {n} to {os.path.basename(mp4)}")
            except Exception as e:
                LOGGER.warning(f"recordings: couldn't convert {n}: {e}")

    @property
    def recording(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> Dict[str, Any]:
        s = self._session
        return {
            "camera": self.camera_name, "label": self.label, "available": self.camera is not None, "idle_s": self.idle_s,
            "recording": self.recording and s is not None,
            "state": (s or {}).get("state"),
            "started": (s or {}).get("t0"), "secs": round(time.time() - s["t0"], 1) if s and self.recording else None,
            "frames": (s or {}).get("n", 0) if self.recording else None,
            "moved": bool((s or {}).get("moved")) if self.recording else None,
            "still_for": round(time.time() - s["last_motion"], 1) if s and self.recording and s.get("moved") else None,
            "commands": [c["label"] for c in (s or {}).get("commands", [])] if self.recording else [],
            "last": self.last, "error": self.error,
        }

    def start(self, label: str, heard: str, moved: bool = False) -> bool:
        """A command just started (or, with moved=True, an arm just started moving): begin a clip
        (or extend the running one)."""
        if self.camera is None:
            return False
        now = time.time()
        if self.recording and self._session is not None:
            self._session["commands"].append({"label": label, "heard": heard, "at": now})
            self._session["last_motion"] = max(self._session["last_motion"], now)
            return True
        stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d_%H-%M-%S")
        name = f"{stamp}_{_slug(label or heard)}"
        frames_dir = os.path.join(self.out_dir, ".partial", name)
        os.makedirs(frames_dir, exist_ok=True)
        self._session = {"name": name, "dir": frames_dir, "t0": now, "last_motion": now, "moved": moved,
                         "n": 0, "times": [], "commands": [{"label": label, "heard": heard, "at": now}],
                         "state": "recording", "joints": {}}
        self._task = asyncio.get_event_loop().create_task(self._run(self._session))
        LOGGER.info(f"recording {self.camera_name} for '{label}'")
        return True

    async def stop_now(self) -> None:
        """End the running clip right away (it's still saved)."""
        if self._session is not None:
            self._session["force_end"] = True

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    # ---- the clip ----
    async def _run(self, s: Dict[str, Any]) -> None:
        cap = asyncio.get_event_loop().create_task(self._capture(s))
        try:
            await self._watch(s)
        finally:
            s["stop"] = True
            try:
                await asyncio.wait_for(cap, timeout=5)
            except BaseException:
                cap.cancel()
        s["state"] = "saving"
        try:
            await self._finish(s)
        except Exception as e:
            self.error = f"couldn't save the recording: {e}"
            LOGGER.warning(self.error)
        finally:
            self._session = None if self._session is s else self._session

    def mark_moved(self) -> None:
        """The command this clip films did move an arm (said by the router once it finishes), so the clip is
        kept even if every motion check happened to fail."""
        if self._session is not None:
            self._session["moved"] = True
            self._session["last_motion"] = time.time()

    async def _arm_state(self, rn: str, arm) -> Optional[tuple]:
        """(moving, joint values) or None when it can't be told right now."""
        if self.probe is not None:
            return self.probe(rn)
        try:
            moving, joints = await asyncio.wait_for(
                asyncio.gather(arm.is_moving(), arm.get_joint_positions()), timeout=2)
        except Exception:
            return None
        return bool(moving), [float(v) for v in joints.values]

    async def _arm_moved(self, rn: str, arm, s: Dict[str, Any]) -> Optional[bool]:
        """True/False, or None when the arm couldn't be asked (that must not end a clip)."""
        st = await self._arm_state(rn, arm)
        if st is None:
            return None
        moving, vals = st
        prev = s["joints"].get(rn)
        s["joints"][rn] = vals
        changed = prev is not None and len(prev) == len(vals) and max(abs(a - b) for a, b in zip(prev, vals)) > 0.3
        return bool(moving) or changed

    async def _watch(self, s: Dict[str, Any]) -> None:
        """Wait until no arm has moved for idle_s (and the command is done), or the time cap."""
        while True:
            now = time.time()
            if self.arms:
                moved = await asyncio.gather(*[self._arm_moved(rn, a, s) for rn, a in self.arms.items() if a is not None])
                if any(m is True for m in moved):
                    s["moved"] = True
                    s["last_motion"] = time.time()
                elif any(m is None for m in moved):
                    s["last_motion"] = time.time()   # can't tell: keep rolling (max_s still caps it)
            busy = False
            try:
                busy = bool(self.busy())
            except Exception:
                pass
            if busy:   # the command is still going (it may be looking, or between moves): keep rolling
                s["last_motion"] = time.time()
            if s.get("force_end"):
                return
            if time.time() - s["last_motion"] >= self.idle_s and not busy:
                return
            if now - s["t0"] >= self.max_s:
                LOGGER.info(f"recording hit its {self.max_s:.0f}s limit; ending it")
                return
            await asyncio.sleep(0.25)

    async def _grab_jpeg(self) -> Optional[bytes]:
        cam = self.camera
        try:
            imgs, _ = await asyncio.wait_for(cam.get_images(filter_source_names=["color"]), timeout=3)
        except TypeError:
            imgs, _ = await asyncio.wait_for(cam.get_images(), timeout=3)
        img = next((i for i in imgs if "depth" not in (getattr(i, "name", "") or "").lower()
                    and "dep" not in str(getattr(i, "mime_type", "")).lower()), None)
        if img is None:
            return None
        data = getattr(img, "data", None)
        if data is None:
            return None
        if str(getattr(img, "mime_type", "")).endswith("jpeg") or data[:2] == b"\xff\xd8":
            return bytes(data)
        from PIL import Image   # png / raw rgb -> jpeg
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).convert("RGB").save(buf, "JPEG", quality=88)
        return buf.getvalue()

    async def _capture(self, s: Dict[str, Any]) -> None:
        """Grab frames at up to `fps`. A remote camera can take a few hundred ms per frame, so several
        requests are kept in flight (spaced 1/fps apart) to reach the frame rate anyway."""
        period = 1.0 / self.fps
        state = {"next": time.time(), "fails": 0}

        async def worker():
            while not s.get("stop"):
                now = time.time()
                slot = max(state["next"], now)
                state["next"] = slot + period
                if slot > now:
                    await asyncio.sleep(slot - now)
                if s.get("stop"):
                    return
                try:
                    jpeg = await self._grab_jpeg()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    state["fails"] += 1
                    if state["fails"] in (1, 20):
                        LOGGER.warning(f"recording: couldn't read {self.camera_name}: {e}")
                    await asyncio.sleep(0.5)
                    continue
                if not jpeg or s.get("stop"):
                    continue
                state["fails"] = 0
                s["n"] += 1                      # numbered in arrival order, with the arrival time
                s["times"].append(time.time())
                await asyncio.to_thread(_write, os.path.join(s["dir"], f"f{s['n']:06d}.jpg"), jpeg)

        workers = [asyncio.get_event_loop().create_task(worker()) for _ in range(max(1, int(self.workers)))]
        try:
            await asyncio.gather(*workers)
        finally:
            for w in workers:
                w.cancel()

    async def _finish(self, s: Dict[str, Any]) -> None:
        if not s["moved"] and not self.keep_still:
            LOGGER.info(f"recording '{s['name']}': no arm moved, so it wasn't kept")
            await asyncio.to_thread(shutil.rmtree, s["dir"], True)
            return
        if s["n"] < 2:
            self.error = f"no frames came from {self.camera_name}, so nothing was saved"
            LOGGER.warning(f"recording '{s['name']}': {self.error}")
            await asyncio.to_thread(shutil.rmtree, s["dir"], True)
            return
        info = await asyncio.to_thread(self._encode, s)
        info["url"] = self.url_prefix + info["file"] if info["file"].endswith(".mp4") else None
        self.last = info
        self.error = None
        LOGGER.info(f"saved recording {info['file']} ({info['seconds']:.0f}s, {info['frames']} frames)")

    def _encode(self, s: Dict[str, Any]) -> Dict[str, Any]:
        os.makedirs(self.out_dir, exist_ok=True)
        times = s["times"]
        meta = {
            "camera": self.camera_name, "commands": s["commands"],
            "started": datetime.fromtimestamp(s["t0"]).isoformat(timespec="seconds"),
            "seconds": round(times[-1] - times[0], 2), "frames": len(times),
            "fps": round((len(times) - 1) / max(1e-6, times[-1] - times[0]), 2),
            "idle_s": self.idle_s,
        }
        mp4 = os.path.join(self.out_dir, s["name"] + ".mp4")
        err = encode(s["dir"], times, mp4, self.fps)
        if err is None:
            shutil.rmtree(s["dir"], ignore_errors=True)
            meta["file"] = os.path.basename(mp4)
            meta["bytes"] = os.path.getsize(mp4)
            _write_json(os.path.join(self.out_dir, s["name"] + ".json"), meta)
            return meta
        LOGGER.warning(f"recording: couldn't make an .mp4 ({err}); keeping the JPEG frames instead")
        # fall back: keep the frames as a folder
        dest = os.path.join(self.out_dir, s["name"] + "_frames")
        shutil.move(s["dir"], dest)
        meta["file"] = os.path.basename(dest)
        meta["frame_times"] = times
        _write_json(os.path.join(dest, "recording.json"), meta)
        return meta


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def _write_json(path: str, obj: Any) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


def list_recordings(out_dir: str, limit: int = 20) -> List[Dict[str, Any]]:
    try:
        names = sorted((n for n in os.listdir(out_dir) if n.endswith(".mp4")), reverse=True)[:limit]
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        p = os.path.join(out_dir, n)
        meta = {}
        try:
            with open(p[:-4] + ".json") as f:
                meta = json.load(f)
        except Exception:
            pass
        out.append({"file": n, "bytes": os.path.getsize(p), "seconds": meta.get("seconds"),
                    "commands": [c.get("label") for c in meta.get("commands", [])], "started": meta.get("started")})
    return out


class MotionRecorder:
    """Films a camera whenever one of its arms starts moving, whatever moved it (voice, the Viam app,
    a script), until all of them have been still for idle_s. The arms are checked ~3x a second,
    page open or not."""

    def __init__(self, rec: Recorder):
        self.rec = rec
        self.arms: Dict[str, Any] = {}         # resource name -> Arm
        self.labels: Dict[str, str] = {}       # resource name -> "arm 18"
        self._task: Optional[asyncio.Task] = None

    def configure(self, camera, camera_name: str, arms: Dict[str, Any], labels: Dict[str, str],
                  busy: Optional[Callable[[], bool]] = None) -> None:
        self.arms = {k: a for k, a in arms.items() if a is not None}
        self.labels = labels
        self.rec.configure(camera, camera_name, self.arms, busy or (lambda: False))
        self.close()
        self._task = (asyncio.get_event_loop().create_task(self._watch())
                      if camera is not None and self.arms else None)

    def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _moving(self, rn: str, arm, prev: Dict[str, List[float]], fails: Dict[str, int]) -> bool:
        if self.rec.probe is not None:
            st = self.rec.probe(rn)
            if st is None:
                prev.pop(rn, None)
                return False
            moving, vals = st
            old = prev.get(rn)
            prev[rn] = vals
            return bool(moving) or (old is not None and len(old) == len(vals)
                                    and max(abs(a - b) for a, b in zip(old, vals)) > 0.3)
        try:
            moving, joints = await asyncio.wait_for(asyncio.gather(arm.is_moving(), arm.get_joint_positions()), timeout=3)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fails[rn] = fails.get(rn, 0) + 1
            if fails[rn] == 1 or fails[rn] % 100 == 0:
                LOGGER.warning(f"recording: couldn't check whether {self.labels.get(rn, rn)} is moving: {e}")
            prev.pop(rn, None)
            return False
        fails[rn] = 0
        vals = [float(v) for v in joints.values]
        old = prev.get(rn)
        prev[rn] = vals
        return bool(moving) or (old is not None and len(old) == len(vals)
                                and max(abs(a - b) for a, b in zip(old, vals)) > 0.3)

    async def _watch(self) -> None:
        prev: Dict[str, List[float]] = {}
        fails: Dict[str, int] = {}
        while True:
            if self.rec.recording:      # the clip watches the arms itself until they've been still for idle_s
                prev.clear()
                await asyncio.sleep(0.5)
                continue
            names = list(self.arms)
            moved = await asyncio.gather(*[self._moving(rn, self.arms[rn], prev, fails) for rn in names])
            who = [self.labels.get(rn, rn) for rn, m in zip(names, moved) if m]
            if who:
                self.rec.start(" and ".join(who) + " moving", "", moved=True)
            await asyncio.sleep(0.3 if not any(fails.values()) else 2)
