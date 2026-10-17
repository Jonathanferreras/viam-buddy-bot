"""
Short UI sounds for the voice flow, played on the computer running the module.

    wake_on   "Hey buddy" was heard: Buddy is listening for a command
    wake_off  no clear command came in time (or it wasn't understood / failed): back to the wake word
    done      the command finished

Files live in <project>/sounds/<name>.mp3. Playback is fire-and-forget through the system player
(afplay on macOS; ffplay, mpg123 or paplay on Linux), so a missing file or player never slows the
listener down -- it's logged once and skipped.
"""

import os
import shutil
import subprocess
import sys
import threading
from typing import Dict, Optional

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUNDS_DIR = os.path.join(_HERE, "sounds")
FILES: Dict[str, str] = {"wake_on": "wake-on.mp3", "wake_off": "wake-off.mp3", "done": "done.mp3"}

_player: Optional[list] = None
_warned: set = set()
_lock = threading.Lock()


def _find_player() -> Optional[list]:
    """The command line that plays one mp3 and exits, or None if nothing suitable is installed."""
    if sys.platform == "darwin" and shutil.which("afplay"):
        return ["afplay"]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
    if shutil.which("mpg123"):
        return ["mpg123", "-q"]
    if shutil.which("paplay"):   # PulseAudio can't decode mp3 on its own; works for wav files
        return ["paplay"]
    return None


def play(name: str, logger=None) -> bool:
    """Start playing the sound called `name` (see FILES). Returns True if a player was launched."""
    global _player
    path = os.path.join(SOUNDS_DIR, FILES.get(name, f"{name}.mp3"))
    with _lock:
        if _player is None:
            _player = _find_player() or []
        player = _player
    if not player or not os.path.isfile(path):
        why = "no audio player found (afplay/ffplay/mpg123)" if not player else f"missing {path}"
        if logger and why not in _warned:
            _warned.add(why)
            logger.warning(f"sound '{name}' not played: {why}")
        return False
    try:
        subprocess.Popen(player + [path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:  # never let a sound break the listener
        if logger and name not in _warned:
            _warned.add(name)
            logger.warning(f"sound '{name}' failed: {e}")
        return False
