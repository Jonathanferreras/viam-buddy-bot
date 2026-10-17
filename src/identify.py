"""
Names what the camera sees on the table ("white cup", "black phone") with CLIP.

The depth camera already separates each object from the table, so this only has to answer
"what is this?" for each object's cut-out. CLIP compares the cut-out with a list of short text
descriptions and picks the best match — no training, and the list is easy to extend.

Everything heavy happens on a background thread:
  * first run: pip-installs onnxruntime + tokenizers (small) and downloads a compact CLIP model
    (~155 MB, quantized ONNX from huggingface.co/Xenova/clip-vit-base-patch32) into
    ~/Dev/voice-router/models/clip
  * each tracked object is classified once when it appears, then rechecked every so often;
    a few looks are averaged so one bad crop doesn't flip the name.
If anything fails, objects simply keep their color-based names and status() says why.
"""

import io
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import taught   # names / verified settings taught on the status page

REPO = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
FILES = {"vision.onnx": "onnx/vision_model_quantized.onnx",
         "text.onnx": "onnx/text_model_quantized.onnx",
         "tokenizer.json": "tokenizer.json"}
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)

# name shown on the page -> ways to describe it to CLIP. "" = background (keep the generic name).
DEFAULT_VOCAB: Dict[str, List[str]] = {
    "cup": ["a plastic cup", "a paper cup", "an upside-down white plastic cup", "a disposable cup turned upside down",
            "a white cup seen from above", "a crushed paper coffee cup", "a white paper coffee cup with a green logo"],
    "mug": ["a coffee mug"],
    "phone": ["a smartphone", "a cell phone lying on a table", "the back of a smartphone with its camera lenses",
              "a phone in a protective case lying face down"],
    "bottle": ["a water bottle", "a plastic bottle"],
    "block": ["a small plastic toy block", "a small wooden block"],
    "cable": ["a cable", "a power cord", "a usb cable"],
    "charger": ["a phone charger", "a power adapter", "a power supply"],
    "mouse": ["a computer mouse"],
    "keyboard": ["a computer keyboard"],
    "laptop": ["a laptop"],
    "pen": ["a pen", "a marker"],
    "scissors": ["a pair of scissors"],
    "tape": ["a roll of tape"],
    "box": ["a cardboard box", "a small box"],
    "bowl": ["a bowl"],
    "can": ["a soda can", "a red Coca-Cola can", "an aluminum drink can", "a soda can lying on its side",
            "a beer can", "the top of a drink can seen from above"],
    "remote": ["a remote control"],
    "headphones": ["headphones", "an earbuds case"],
    "notebook": ["a notebook", "a book"],
    "glasses": ["a pair of glasses"],
    "keys": ["a set of keys"],
    "screwdriver": ["a screwdriver", "a small stubby screwdriver with a yellow and black handle",
                    "a screwdriver lying on a table seen from above", "a short screwdriver with a rubber grip"],
    "tool": ["a pair of pliers", "a hand tool", "a wrench"],
    "fork": ["a fork", "a plastic fork", "a disposable plastic fork", "a fork lying flat on a table seen from above"],
    "spoon": ["a spoon", "a plastic spoon", "a spoon lying flat on a table"],
    "knife": ["a plastic knife", "a butter knife", "a table knife lying flat"],
    "ball": ["a ball", "a small rubber ball", "a round toy ball", "a ball seen from above"],
    "fruit": ["an apple", "a banana", "an orange"],
    "robot part": ["a robot gripper", "part of a robot arm"],
    "hand": ["a human hand"],
    "": ["an empty table top", "a blue table mat", "a blurry background"],
}


# What each kind of thing can plausibly measure (mm), from the depth camera. A guess that breaks its
# rule is heavily discounted, e.g. a 7 cm tall "bottle" is really a cup.
#   tall: True/False = must / mustn't stand up off the table; w, h, l = (min, max); hw = min height/width
SHAPES: Dict[str, Dict[str, Any]] = {
    "cup":      {"tall": True,  "w": (45, 130), "h": (40, 170)},
    "mug":      {"tall": True,  "w": (60, 130), "h": (60, 140)},
    "bottle":   {"tall": True,  "w": (35, 110), "h": (120, 400), "hw": 1.5},
    "can":      {"w": (45, 135), "h": (40, 180)},          # standing, or lying on its side (then ~12 cm long)
    "phone":    {"tall": False, "l": (95, 210), "w": (45, 110)},
    "remote":   {"tall": False, "l": (100, 260), "w": (25, 70)},
    "block":    {"l": (12, 100)},
    "keys":     {"tall": False, "l": (30, 160)},
    "glasses":  {"l": (90, 170)},
    "pen":      {"tall": False, "l": (90, 200), "w": (5, 30)},
    "ball":     {"w": (20, 250), "h": (15, 250)},          # round: never a flat scrap
    "screwdriver": {"tall": False, "l": (50, 320), "w": (8, 50), "lw": 2.5},   # long and thin (not a block), lying flat
    "fork":     {"tall": False, "l": (120, 260), "w": (10, 50), "lw": 4},      # cutlery: long, flat, lying down
    "spoon":    {"tall": False, "l": (120, 260), "w": (10, 50), "lw": 4},
    "knife":    {"tall": False, "l": (120, 260), "w": (8, 40), "lw": 5},
    "mouse":    {"l": (70, 140), "w": (40, 90)},
    "keyboard": {"tall": False, "l": (250, 500)},
    "laptop":   {"l": (250, 450)},
    "hand":     {"l": (110, 300)},
    "tape":     {"l": (40, 130)},
}


def _fits(name: str, geo: Optional[Dict[str, Any]]) -> bool:
    spec = SHAPES.get(name)
    if not spec or not geo or not geo.get("size_mm"):
        return True
    L, W, H = (float(x) for x in geo["size_mm"])
    tall = bool(geo.get("tall"))
    if "tall" in spec and spec["tall"] != tall:
        return False
    inr = lambda v, r: r[0] <= v <= r[1]
    if "l" in spec and not inr(L, spec["l"]):
        return False
    if "w" in spec and not inr(W, spec["w"]):
        return False
    if "h" in spec and not inr(H, spec["h"]):
        return False
    if "hw" in spec and H < spec["hw"] * max(W, 1.0):
        return False
    if "lw" in spec and L < spec["lw"] * max(W, 1.0):    # at least this many times longer than wide
        return False
    return True


class Identifier:
    def __init__(self, model_dir: str, logger, vocab: Optional[Dict[str, List[str]]] = None,
                 min_conf: float = 0.4, recheck_s: float = 20.0, max_looks: int = 4):
        self.dir, self.log = model_dir, logger
        self.vocab = vocab or DEFAULT_VOCAB
        self.min_conf, self.recheck_s, self.max_looks = min_conf, recheck_s, max_looks
        self.state, self.detail = "starting", ""
        self._jobs: "queue.Queue[Tuple[int, bytes, List[int]]]" = queue.Queue(maxsize=32)
        self._pending: set = set()
        self._results: Dict[int, Dict[str, Any]] = {}   # track id -> {"probs": np[classes], "looks": n, "t": time}
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="identify").start()

    # ---------------- public, called from the camera loop ----------------

    def status(self) -> Dict[str, Any]:
        return {"state": self.state, "detail": self.detail}

    def embedding(self, oid: int) -> Optional[np.ndarray]:
        """What tracked object `oid` looks like to CLIP (unit vector), once it has been looked at."""
        with self._lock:
            r = self._results.get(oid)
            return None if r is None or r.get("emb") is None else r["emb"].copy()

    def label(self, objects: List[Dict[str, Any]], frame_jpeg: Optional[bytes]) -> List[Dict[str, Any]]:
        """Return objects with model names applied where we know them; queue the rest."""
        now = time.time()
        live_ids = set()
        out = []
        for o in objects:
            oid = o.get("id")
            if oid is None:
                out.append(o)
                continue
            live_ids.add(oid)
            with self._lock:
                r = self._results.get(oid)
            if (self.state == "ready" and frame_jpeg and oid not in self._pending
                    and (r is None or (r["looks"] < self.max_looks and now - r["t"] > 1.5)
                         or now - r["t"] > self.recheck_s)):
                try:
                    self._jobs.put_nowait((oid, frame_jpeg, o["bbox"]))
                    self._pending.add(oid)
                except queue.Full:
                    pass
            o = dict(o)
            if r is not None:   # colored things get a real name too ("Red ball"); o["block"] keeps the color for voice
                probs = self._prior(r["probs"], o)
                top = np.argsort(probs)[::-1][:3]
                o["kind_top"] = [[self._names[i] or "background", round(float(probs[i]), 2)] for i in top]
                name, conf = self._best(probs)
                if name and conf >= self.min_conf:
                    color = (o.get("color") or "").strip()
                    o["label"] = f"{color.capitalize()} {name}" if color else name.capitalize()
                    o["kind"], o["kind_conf"] = name, round(conf, 2)
            o = taught.apply(o, r.get("emb") if r is not None else None)   # what you taught it wins
            out.append(o)
        taught.prune(live_ids)
        with self._lock:                                   # forget objects that left the table
            for oid in [k for k in self._results if k not in live_ids]:
                if now - self._results[oid]["t"] > 30:
                    del self._results[oid]
        return out

    # ---------------- background work ----------------

    def _best(self, probs: np.ndarray) -> Tuple[str, float]:
        i = int(np.argmax(probs))
        return self._names[i], float(probs[i])

    def _run(self):
        try:
            self._setup()
        except Exception as e:
            self.state, self.detail = "error", str(e) or type(e).__name__
            self.log.warning(f"object naming unavailable: {self.detail}")
            return
        self.state, self.detail = "ready", ""
        self.log.info("object naming ready (CLIP)")
        while True:
            oid, jpeg, bbox = self._jobs.get()
            try:
                probs, emb = self._classify(jpeg, bbox)
                with self._lock:
                    r = self._results.get(oid)
                    if r is None:
                        self._results[oid] = {"probs": probs, "emb": emb, "looks": 1, "t": time.time()}
                    else:   # running average over a few looks so one bad crop doesn't flip the name
                        n = min(r["looks"], self.max_looks - 1)
                        e = r.get("emb")
                        e = emb if e is None else (e * n + emb) / (n + 1)
                        r.update(probs=(r["probs"] * n + probs) / (n + 1), emb=e / np.linalg.norm(e),
                                 looks=r["looks"] + 1, t=time.time())
            except Exception as e:
                self.log.debug(f"classify failed for object {oid}: {e}")
            finally:
                self._pending.discard(oid)

    def _setup(self):
        self.state, self.detail = "installing", "installing onnxruntime and tokenizers"
        try:
            import onnxruntime  # noqa: F401
            import tokenizers   # noqa: F401
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "onnxruntime", "tokenizers"],
                           check=True, capture_output=True, timeout=900)
        import onnxruntime as ort
        from tokenizers import Tokenizer

        os.makedirs(self.dir, exist_ok=True)
        for local, remote in FILES.items():
            path = os.path.join(self.dir, local)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                continue
            self.state, self.detail = "downloading", f"downloading the model ({remote.split('/')[-1]})"
            tmp = path + ".part"
            with urllib.request.urlopen(REPO + remote, timeout=60) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, path)

        self.state, self.detail = "loading", "loading the model"
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2            # leave CPU for the speech recognizer
        self._vis = ort.InferenceSession(os.path.join(self.dir, "vision.onnx"), opts, providers=["CPUExecutionProvider"])
        text = ort.InferenceSession(os.path.join(self.dir, "text.onnx"), opts, providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(os.path.join(self.dir, "tokenizer.json"))

        # embed every description once; a kind's score is the sum over its descriptions
        self._names = list(self.vocab.keys())
        embeds, owner = [], []
        for ci, name in enumerate(self._names):
            for p in self.vocab[name]:
                embeds.append(self._embed_text(text, tok, f"a photo of {p}"))
                owner.append(ci)
        self._text = np.stack(embeds).astype(np.float32)
        self._owner = np.array(owner)
        self._vis_in = self._vis.get_inputs()[0].name
        self._vis_out = self._pick_output(self._vis, "image_embeds")

    @staticmethod
    def _pick_output(sess, prefer: str) -> str:
        names = [o.name for o in sess.get_outputs()]
        return prefer if prefer in names else next((n for n in names if "embed" in n), names[0])

    def _embed_text(self, sess, tok, s: str) -> np.ndarray:
        ids = tok.encode(s).ids
        feeds = {}
        for inp in sess.get_inputs():
            if inp.name == "input_ids":
                feeds[inp.name] = np.array([ids], dtype=np.int64)
            elif inp.name == "attention_mask":
                feeds[inp.name] = np.ones((1, len(ids)), dtype=np.int64)
            elif inp.name == "position_ids":
                feeds[inp.name] = np.arange(len(ids), dtype=np.int64)[None]
        out = sess.run([self._pick_output(sess, "text_embeds")], feeds)[0][0]
        if out.ndim > 1:     # got hidden states instead of the pooled embedding: take the end-of-text token
            out = out[int(np.argmax(ids))]
        return out / np.linalg.norm(out)

    def _classify(self, jpeg: bytes, bbox: List[int]) -> np.ndarray:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        x0, y0, x1, y1 = bbox
        w, h = x1 - x0, y1 - y0
        pad = 0.18 * max(w, h) + 6                      # a little context helps CLIP
        side = max(w, h) + 2 * pad                      # square crop around the object
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        box = [int(max(0, cx - side / 2)), int(max(0, cy - side / 2)),
               int(min(img.width, cx + side / 2)), int(min(img.height, cy + side / 2))]
        crop = img.crop(box).resize((224, 224), Image.BICUBIC)
        x = (np.asarray(crop, np.float32) / 255.0 - MEAN) / STD
        x = x.transpose(2, 0, 1)[None].astype(np.float32)
        e = self._vis.run([self._vis_out], {self._vis_in: x})[0][0]
        e = e / np.linalg.norm(e)
        logits = 100.0 * (self._text @ e)
        # each kind scores as its best-matching description, so a kind isn't favoured just for having more
        # descriptions (summing them made "cup", with five, beat a soda can with one)
        best = np.full(len(self._names), -np.inf, np.float32)
        np.maximum.at(best, self._owner, logits)
        p = np.exp(best - best.max())
        return p / p.sum(), e

    def _prior(self, probs: np.ndarray, geo: Optional[Dict[str, Any]]) -> np.ndarray:
        """Discount kinds the measured size/shape rules out, then renormalise."""
        w = np.array([1.0 if _fits(n, geo) else 0.08 for n in self._names])
        q = probs * w
        return q / q.sum() if q.sum() > 0 else probs
