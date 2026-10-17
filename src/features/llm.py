"""Smart mode: a local LLM (Ollama) works out what you meant and turns it into commands Buddy already knows.

Two ways of understanding speech, switchable at any time:
    "rules"  the router's own parsing (exact phrases, keyword rules, the other features) — as before
    "llm"    anything that isn't an exact command phrase goes to a local model through Ollama, with what the
             cameras see, what each arm holds and which arms are in manual mode. It answers with one or more
             commands in Buddy's own words ("grab the red can", "put it back", "clean up", "move the orange
             screwdriver to the top left corner", ...). Each one then goes through the normal parsing and
             runs exactly like a spoken command — same checks (reach, gripper width, manual mode, busy) —
             one after the other. If Ollama isn't running, the model isn't pulled, or it answers nonsense,
             the rules take over for that command.
"Stop" never goes near the model: the router handles it before any feature sees it.

In "llm" mode the speech recogniser also listens with an open vocabulary next to the usual fixed word
list (which still catches "hey buddy" and "stop"), so words the rules never knew get through to the model.

Switch it:  voice "smart mode" / "simple mode";  status page toggle (POST /action {"action": "nlu_mode",
            "mode": "llm" | "rules"});  DoCommand {"nlu_mode": "llm" | "rules" | "toggle" | "status"}.
            The choice is kept in nlu_settings.json, so it survives restarts.
Model:      DoCommand {"nlu_mode": {"model": "qwen2.5:7b"}} (also kept), or in the config.

Optional config, in the router's attributes:
    "llm": {"mode": "rules",                      # the starting mode when nothing has been chosen yet
            "model": "qwen2.5:7b",                # any Ollama chat model: `ollama pull qwen2.5:7b`
            "url": "http://127.0.0.1:11434",      # where Ollama listens
            "timeout_s": 25,                      # give up on the model after this (then: rules)
            "open_speech": true}                  # in llm mode, also transcribe with an open vocabulary
"""
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import Feature

_SETTINGS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                         "nlu_settings.json")
_DEFAULT_MODEL = "qwen2.5:7b"
_MAX_STEPS = 4
_TO_LLM = ("smart mode", "smart", "clever mode", "llm mode")
_TO_RULES = ("simple mode", "rules mode", "basic mode")

# The answer the model must give (Ollama structured output: it can only produce this shape).
_SCHEMA = {
    "type": "object",
    "properties": {
        "commands": {"type": "array", "items": {"type": "string"}, "maxItems": _MAX_STEPS},
        "reply": {"type": "string"},
    },
    "required": ["commands", "reply"],
}


def _m(router):
    """The router's own module (main runs as __main__, so it's looked up rather than imported)."""
    return sys.modules[type(router).__module__]


def _st(router) -> Dict[str, Any]:
    s = getattr(router, "_llm", None)
    if s is None:
        s = router._llm = {"mode": "rules", "model": _DEFAULT_MODEL, "url": "http://127.0.0.1:11434",
                           "timeout_s": 25.0, "open_speech": True, "health": {"ok": None, "why": "not checked yet"},
                           "last": None, "skip": False, "checked_at": 0.0, "warm": False}
    return s


def _load_settings() -> Dict[str, Any]:
    try:
        with open(_SETTINGS) as f:
            return json.load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_settings(s: Dict[str, Any]) -> None:
    tmp = _SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"mode": s["mode"], "model": s["model"]}, f, indent=2)
    os.replace(tmp, _SETTINGS)


# ---------------------------------------------------------------- talking to Ollama

def _http(url: str, body: Optional[Dict[str, Any]], timeout: float) -> Dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _has_model(names: List[str], want: str) -> bool:
    if want in names:
        return True
    if ":" not in want:                       # "qwen2.5" means "qwen2.5:latest" (or any tag)
        return any(n.split(":")[0] == want for n in names)
    return False


async def _check(router, warm: bool = False) -> Dict[str, Any]:
    """Is Ollama running, and is the model there? Optionally load the model now, so the first command
    doesn't wait for it."""
    s = _st(router)
    try:
        tags = await asyncio.to_thread(_http, s["url"].rstrip("/") + "/api/tags", None, 3.0)
        names = [m.get("name", "") for m in tags.get("models", [])]
        if not _has_model(names, s["model"]):
            h = {"ok": False, "why": f"the model '{s['model']}' isn't pulled — run: ollama pull {s['model']}",
                 "models": names}
        else:
            h = {"ok": True, "why": "", "models": names}
    except Exception as e:
        h = {"ok": False, "why": f"Ollama isn't reachable at {s['url']} ({type(e).__name__}) — is it running?"}
    s["health"], s["checked_at"] = h, time.time()
    if h["ok"] and warm and not s["warm"]:
        try:   # an empty generate loads the model into memory and keeps it there for a while
            await asyncio.to_thread(_http, s["url"].rstrip("/") + "/api/generate",     # same context size as _ask, or
                                    {"model": s["model"], "prompt": "", "keep_alive": "30m",   # it loads again
                                     "options": {"num_ctx": 8192}}, 120.0)
            s["warm"] = True
        except Exception:
            pass
    return h


def _ask(url: str, model: str, system: str, user: str, timeout: float) -> Dict[str, Any]:
    # "think": False — "thinking" models (qwen3, ...) otherwise reason at length before answering, far too
    # slow for a spoken command. A model (or an older Ollama) that won't take the option gets asked without it.
    body = {"model": model, "stream": False, "format": _SCHEMA, "keep_alive": "30m", "think": False,
            # num_ctx: Ollama keeps only the last 2-4k tokens of a prompt by default, which would cut off the
            # start of these instructions now that they list every arm move
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    try:
        out = _http(url.rstrip("/") + "/api/chat", body, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 400 or "think" not in (e.read().decode(errors="replace") or "").lower():
            raise
        body.pop("think")
        out = _http(url.rstrip("/") + "/api/chat", body, timeout)
    content = ((out.get("message") or {}).get("content") or "").strip()
    ans = json.loads(content)
    ns = 1e-9
    ans["_timing"] = {"load_s": round((out.get("load_duration") or 0) * ns, 2),
                      "prompt_tokens": out.get("prompt_eval_count"), "prompt_s": round((out.get("prompt_eval_duration") or 0) * ns, 2),
                      "answer_tokens": out.get("eval_count"), "answer_s": round((out.get("eval_duration") or 0) * ns, 2),
                      "thought": bool((out.get("message") or {}).get("thinking"))}
    return ans


# ---------------------------------------------------------------- what the model is told

def _scene(router) -> Dict[str, Any]:
    """What Buddy knows right now: things the cameras see, what each arm holds, manual mode, spots."""
    M = _m(router)
    sp = M.status_page
    seen: Dict[str, List[str]] = {}
    for c in getattr(router, "live_cams", []) or []:
        objs = sp.live_status(c["camera"]).get("objects") or []
        try:
            objs = M._number_duplicates(objs)
        except Exception:
            pass
        names = []
        for o in objs:
            n = (o.get("tag") or o.get("label") or "").strip()
            if n and n.lower() not in names:
                names.append(n.lower())
        where = f"{c.get('arm_label') or c.get('arm')}'s own camera" if c.get("arm") else "the desk camera"
        seen[where] = names
    arms = []
    for rn, info in (getattr(router, "_arms", {}) or {}).items():
        g = (getattr(router, "grabbers", {}) or {}).get(rn) or {}
        arms.append({"arm": info.get("name", rn), "holding": g.get("holding"),
                     "manual_mode": rn in (getattr(router, "_manual", {}) or {})})
    return {"seen": seen, "arms": arms, "picker": ((getattr(router, "grabbers", {}) or {}).get(router.picker) or {}).get("name")
            if getattr(router, "picker", None) else None}


def _catalogue(router) -> List[str]:
    """The commands Buddy understands, in its own words."""
    arms = [info.get("name", rn) for rn, info in (getattr(router, "_arms", {}) or {}).items()]
    nums = [re.sub(r"\D", "", a) or a for a in arms]
    picker = ((getattr(router, "grabbers", {}) or {}).get(getattr(router, "picker", None)) or {}).get("name", "the helper arm")
    out = [
        f"grab the <thing>        — {picker} picks it up (use a name from what the cameras see, e.g. \"grab the red can\", \"grab the white cup 2\")",
        "move the <thing> to the <place>  — pick it up and set it down there; places: the center, the left side, the right side, "
        "the top, the bottom, the top left corner, the top right corner, the bottom left corner, the bottom right corner",
        "put it back             — set what it's holding down where it picked it up",
        "let go                  — open the gripper right where it is (drops what it holds)",
        "clean up                — move every named thing on the desk to the side",
    ]
    for n in nums:
        out += [f"reset arm {n}            — arm {n} goes back to its starting position",
                f"wave arm {n}",
                f"manual mode {n} / manual mode off {n}  — let a person move arm {n} by hand / lock it again"]
    out += ["dance                   — both arms dance for a few seconds",
            "play shell game         — arm 18 hides a ball under one of three cups and shuffles them",
            "left cup / middle cup / right cup  — guess where the ball is (during the shell game)",
            "where is the ball       — show where the ball is",
            "set up jenga            — arm 18 builds a Jenga tower"]
    try:                                          # what the plug-in features add (src/features/moves.py: arm moves)
        from . import catalogue as _feature_lines
        out += _feature_lines(router)
    except Exception:
        pass
    extra = sorted({p for c in getattr(router, "commands", []) for p in c.get("phrases", [])
                    if c.get("action") != "stop"})
    if extra:
        out.append("also these exact phrases: " + "; ".join(f"\"{p}\"" for p in extra))
    return out


_SYSTEM = """You are the language understanding of Buddy, a robot with two arms on a desk. A person speaks to Buddy; \
you turn what they said into Buddy's own commands. Buddy only understands the commands listed below — write each \
one exactly in that form, filling in <thing> and <place>.

Commands:
{catalogue}

Rules:
- Answer only with JSON: {{"commands": [...], "reply": "..."}}.
- "commands": the commands to run, in order (at most {max_steps}). Usually just one.
- For <thing>, use a name from what the cameras see (below), exactly as written there, e.g. "red can", "white cup 2". \
If they describe something ("the soda", "that cup", "the tool"), pick the best match. If nothing fits, run nothing \
and say so in "reply".
- "it" / "that" usually means what an arm is holding, or the thing they just talked about.
- Keep the person's numbers and directions exactly ("90 degrees" stays "90 degrees", "left" stays "left"); add \
"a little" / "a lot" only if they said something like that. A quarter turn is 90 degrees, half a turn 180 degrees.
- Never write "stop" — Buddy handles stopping by itself.
- If they want something Buddy can't do, or it's chat, run nothing and answer briefly in "reply".
- "reply": one short, friendly sentence saying what Buddy will do (or why it can't).

Examples:
"could you tidy up the desk" -> {{"commands": ["clean up"], "reply": "Tidying up the desk."}}
"pass me the can" (cameras see "red can") -> {{"commands": ["grab the red can"], "reply": "Getting the red can."}}
"stick the screwdriver in the top left" -> {{"commands": ["move the orange screwdriver to the top left corner"], "reply": "Moving the screwdriver to the top left corner."}}
"actually never mind, put that back" -> {{"commands": ["put it back"], "reply": "Putting it back."}}
"say hi" -> {{"commands": ["wave arm 17"], "reply": "Hi!"}}
"grab the fork and rotate it 90 degrees" (cameras see "white fork") -> {{"commands": ["grab the white fork", "rotate 90 degrees"], "reply": "Getting the fork, then turning it."}}
"twist your hand a bit to the right, then nod twice" -> {{"commands": ["rotate wrist right a little", "nod twice"], "reply": "Twisting right, then nodding."}}
"what's the weather like" -> {{"commands": [], "reply": "I can't check the weather — I can move things on the desk."}}"""


def _prompt(router, text: str) -> Tuple[str, str]:
    sc = _scene(router)
    system = _SYSTEM.format(catalogue="\n".join("- " + c for c in _catalogue(router)), max_steps=_MAX_STEPS)
    names = []
    for ns in sc["seen"].values():
        for n in ns:
            if n not in names:
                names.append(n)
    seen = ", ".join(names) if names else "nothing (no camera is watching)"
    arms = "; ".join(f"{a['arm']} is " + (f"holding the {a['holding']}" if a["holding"] else "empty-handed")
                     + (" and in manual mode" if a["manual_mode"] else "") for a in sc["arms"])
    user = (f"Things Buddy can see right now (use exactly these names for <thing>): {seen}.\n"
            f"Arms: {arms}.\n\nThe person said: \"{text}\"")
    return system, user


# ---------------------------------------------------------------- understanding, then running

def _parse(router, phrase: str) -> Optional[Dict[str, Any]]:
    """One of the model's commands -> a router command, through the normal parsing (never the model again)."""
    s = _st(router)
    s["skip"] = True
    try:
        cmd, score = router._match(phrase)
        if score < 100 or re.search(r"\b(and|then)\b", phrase.lower()):
            meant = router._understand(phrase)
            if meant is not None:
                cmd = meant
        return cmd
    finally:
        s["skip"] = False


async def _dispatch(router, cmd: Dict[str, Any], text: str) -> None:
    """Run one command the way the router's own _run does."""
    from . import owns, run as frun
    M = _m(router)
    a = cmd["action"]
    if owns(a):
        await frun(router, cmd, text)
    elif a in getattr(M, "_SHELL_ACTIONS", ()):
        label, _good = await getattr(router, "_" + a)(cmd, text)
        router._current = label
    elif a == "wave":
        await router._wave(cmd)
    elif a == "grab":
        await router._grab(cmd)
    elif a == "release":
        await router._release(cmd.get("only"))
    elif a == "put_back":
        await router._put_back(cmd)
    elif a == "home":
        await router._go_home(cmd)
    elif a == "find_grab":
        await router._grab_described(cmd)
    elif a:
        raise ValueError(f"unknown action '{a}'")
    else:
        await cmd["resource"].do_command(cmd["command"])


async def _plan(router, text: str) -> Tuple[Optional[List[Tuple[str, Dict[str, Any]]]], str, str]:
    """Ask the model. (steps, reply, why) — steps None means "the model couldn't be used: use the rules"."""
    M = _m(router)
    s = _st(router)
    if not s["health"].get("ok") or time.time() - s["checked_at"] > 60:
        await _check(router)
    if not s["health"].get("ok"):
        return None, "", s["health"].get("why") or "the model isn't available"
    # a fresh look, so the model knows what's on the desk now
    cams = [c for c in (router._desk_cam(), router._picker_cam()) if c is not None]
    if cams:
        try:
            await asyncio.wait_for(asyncio.gather(*[router._fresh_objects(c, frames=1, timeout=2.5) for c in cams]), 3.0)
        except Exception:
            pass
    system, user = _prompt(router, text)
    t0 = time.time()
    try:
        ans = await asyncio.to_thread(_ask, s["url"], s["model"], system, user, float(s["timeout_s"]))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        s["health"] = {"ok": False, "why": f"the model didn't answer ({type(e).__name__}: {e})"}
        s["checked_at"] = time.time()
        return None, "", s["health"]["why"]
    ms = int((time.time() - t0) * 1000)
    raw = [str(c).strip() for c in (ans.get("commands") or []) if str(c).strip()][:_MAX_STEPS]
    reply = str(ans.get("reply") or "").strip()
    steps, dropped = [], []
    for phrase in raw:
        cmd = _parse(router, phrase)
        if cmd is None or cmd.get("action") in ("stop", "llm", "nlu_mode"):
            dropped.append(phrase)
            continue
        steps.append((phrase, cmd))
    s["last"] = {"heard": text, "commands": raw, "ran": [p for p, _ in steps], "dropped": dropped,
                 "reply": reply, "ms": ms, "model": s["model"], "at": time.time(), "timing": ans.get("_timing")}
    M.LOGGER.info(f"smart mode: '{text}' -> {raw or '(nothing)'}" + (f" — couldn't use {dropped}" if dropped else "")
                  + (f" — “{reply}”" if reply else "") + f" ({ms} ms, {s['model']})")
    return steps, reply, ""


async def run(router, cmd: Dict[str, Any], text: str) -> Dict[str, Any]:
    M = _m(router)
    s = _st(router)
    if cmd["action"] == "nlu_mode":
        res = await _set_mode(router, cmd["mode"])
        router._current = ("Smart mode: I'll work out what you mean" if cmd["mode"] == "llm"
                           else "Simple mode: the usual commands")
        if cmd["mode"] == "llm" and not s["health"].get("ok"):
            router._current += f" — but {s['health'].get('why')}"
        cmd["keep_current"] = True
        return res

    heard = cmd.get("text") or text
    router._set_phase("running", current="Thinking…")
    steps, reply, why = await _plan(router, heard)
    if steps is None:          # no model: the rules have a go, exactly as in simple mode
        M.LOGGER.warning(f"smart mode: {why}; using the usual rules for '{heard}'")
        rc = _parse(router, heard)
        if rc is None or rc.get("action") == "stop":
            raise M.NotFound(f"Didn't understand “{heard}” (and the smart mode isn't available: {why})")
        steps, reply = [(heard, rc)], ""
    if not steps:
        last = s.get("last") or {}
        if last.get("dropped"):     # it suggested things Buddy can't do
            raise M.NotFound(f"I don't know how to do “{heard}”")
        raise M.NotFound(reply or f"I don't know how to do “{heard}”")
    for i, (phrase, step) in enumerate(steps):
        label = router._label(step)
        router._action_arms = list(dict.fromkeys(router._arms_for(step)))
        router._set_phase("running", current=label if len(steps) == 1 else f"{label} ({i + 1} of {len(steps)})")
        step = {**step, "rec": cmd.get("rec", {})}
        await _dispatch(router, step, phrase)
    if reply and len(steps) > 1:
        router._current = reply
    elif not getattr(router, "_current", None):
        router._current = reply or router._label(steps[-1][1])
    cmd["keep_current"] = True
    return {"ran": [p for p, _ in steps], "reply": reply}


def understand(router, words: List[str], text: str) -> Optional[Dict[str, Any]]:
    s = _st(router)
    if s["skip"]:
        return None                            # parsing the model's own commands: rules only
    t = " " + " ".join(words) + " "
    if any(f" {p} " in t for p in _TO_RULES) and ("mode" in words or len(words) <= 3):
        return router._mk_cmd(action="nlu_mode", mode="rules", label="Switching to simple mode")
    if any(f" {p} " in t for p in _TO_LLM) and ("mode" in words or len(words) <= 3):
        return router._mk_cmd(action="nlu_mode", mode="llm", label="Switching to smart mode")
    if s["mode"] != "llm":
        return None
    # what the move parser reads completely (every word accounted for) needs no model: it's instant and keeps
    # the exact numbers ("grab the fork and rotate 90 degrees"); the moves feature takes it from here
    try:
        from .moves import understand as _moves_read
        if _moves_read(router, words, text) is not None:
            return None
    except Exception:
        pass
    if not s["health"].get("ok") and time.time() - s["checked_at"] < 30:
        return None                            # known to be down: don't make every command wait for it
    return router._mk_cmd(action="llm", text=text, label="Thinking…")


def arms_for(router, cmd: Dict[str, Any]) -> List[str]:
    return []                                  # known once the model has answered (set in run)


def label(cmd: Dict[str, Any]) -> str:
    return cmd.get("label") or ("Thinking…" if cmd["action"] == "llm" else "Switching mode")


# ---------------------------------------------------------------- open-vocabulary listening (llm mode)

class _Dual:
    """Two Vosk recognisers on the same audio: the router's fixed-word-list one decides when an utterance
    ends and catches the wake word (it's the reliable one for that), and an open-vocabulary one supplies
    the words of the command itself, which may be anything."""

    def __init__(self, router, fixed, make_open):
        self.r, self.g, self.o = router, fixed, make_open()
        self.buf: List[str] = []

    def AcceptWaveform(self, data) -> bool:
        if self.o.AcceptWaveform(data):        # the open one ended a phrase of its own: keep its words
            self.buf.append(json.loads(self.o.Result()).get("text", ""))
        return self.g.AcceptWaveform(data)

    def PartialResult(self):
        return self.g.PartialResult()

    def _open_text(self) -> str:
        words = " ".join(self.buf + [json.loads(self.o.FinalResult()).get("text", "")])
        self.buf = []
        return " ".join(words.replace("[unk]", " ").split())

    def _known(self, text: str) -> bool:
        """Is `text` a whole command Buddy knows exactly (a command phrase, or an arm move)? Then the fixed
        word list heard it right, and it beats the open recogniser's guess ("he would he wrote take your wrist")."""
        if not text:
            return False
        try:
            if self.r._match(text)[1] >= 100:
                return True
            from .moves import read
            return read(self.r, text) is not None
        except Exception:
            return False

    def _merge(self, fixed: str, open_: str) -> str:
        r = self.r
        fixed = " ".join(fixed.replace("[unk]", " ").split())
        if not open_:
            return fixed
        if getattr(r, "_listen_until", 0):    # inside the command window after a bare "hey buddy"
            return fixed if self._known(fixed) else open_
        rest_fixed = r._after_wake(fixed)
        if rest_fixed and self._known(rest_fixed):
            return fixed                       # "hey buddy rotate your wrist": the fixed list got it
        if rest_fixed is not None:             # the fixed list heard the wake word: the command is the open words
            rest = r._after_wake(open_)
            rest = open_ if rest is None else rest
            return f"{r.wake_word} {rest}".strip() if rest else fixed
        if r._after_wake(open_) is not None:   # only the open one heard it
            return open_
        return fixed                           # ordinary talk: ignored as before

    def Result(self):
        res = json.loads(self.g.Result())
        res["text"] = self._merge(res.get("text", ""), self._open_text())
        return json.dumps(res)

    def FinalResult(self):
        res = json.loads(self.g.FinalResult())
        o = self._open_text()
        res["text"] = o or res.get("text", "")
        return json.dumps(res)


def speech_hook(router, fixed_rec, make_open):
    """Called by the router each time it makes its main recogniser: in llm mode, pair it with an open one."""
    s = _st(router)
    if s["mode"] == "llm" and s["open_speech"]:
        return _Dual(router, fixed_rec, make_open)
    return None


# ---------------------------------------------------------------- switching, page, DoCommand, state

async def _set_mode(router, mode: str) -> Dict[str, Any]:
    M = _m(router)
    s = _st(router)
    if mode == "toggle":
        mode = "rules" if s["mode"] == "llm" else "llm"
    if mode not in ("llm", "rules"):
        raise ValueError("mode must be 'llm' or 'rules'")
    s["mode"] = mode
    _save_settings(s)
    M.LOGGER.info(f"speech understanding: {'smart mode (' + s['model'] + ' via Ollama)' if mode == 'llm' else 'simple mode (rules)'}")
    if mode == "llm":
        h = await _check(router)
        if h["ok"]:
            asyncio.get_event_loop().create_task(_check(router, warm=True))
        else:
            M.LOGGER.warning(f"smart mode: {h['why']} — the usual rules are used until it's fixed")
    return state(router)


async def _page_mode(router, body):
    return await _set_mode(router, str(body.get("mode") or "toggle"))


async def _do(router, command):
    v = command.get("nlu_mode")
    s = _st(router)
    if isinstance(v, dict):
        if v.get("model"):
            s["model"], s["warm"] = str(v["model"]), False
            _save_settings(s)
        if v.get("mode"):
            return await _set_mode(router, str(v["mode"]))
        await _check(router, warm=s["mode"] == "llm")
        return state(router)
    if v in ("llm", "rules", "toggle"):
        return await _set_mode(router, v)
    if v == "test" or "nlu_test" in command:     # {"nlu_test": "tidy up please"}: what would the model do? (runs nothing)
        text = str(command.get("nlu_test") or command.get("text") or "")
        steps, reply, why = await _plan(router, text)
        return {"heard": text, "would_run": [p for p, _ in steps] if steps is not None else None,
                "reply": reply, "why": why, "last": s.get("last"), "scene": _scene(router),
                "prompt_chars": sum(len(x) for x in _prompt(router, text))}
    await _check(router)
    return state(router)


def configure(router, attrs: Dict[str, Any]) -> None:
    s = _st(router)
    cfg = dict(attrs.get("llm") or {})
    saved = _load_settings()
    s["url"] = str(cfg.get("url") or s["url"])
    s["timeout_s"] = float(cfg.get("timeout_s", s["timeout_s"]))
    s["open_speech"] = bool(cfg.get("open_speech", True))
    s["model"] = str(saved.get("model") or cfg.get("model") or s["model"])
    mode = str(saved.get("mode") or cfg.get("mode") or s["mode"])
    s["mode"] = mode if mode in ("llm", "rules") else "rules"
    router._speech_hook = lambda fixed, make_open: speech_hook(router, fixed, make_open)
    try:   # check Ollama (and load the model, in smart mode) in the background
        asyncio.get_running_loop().create_task(_check(router, warm=s["mode"] == "llm"))
    except RuntimeError:
        pass                                    # no loop yet: the first command checks instead


def state(router) -> Dict[str, Any]:
    s = _st(router)
    return {"mode": s["mode"], "model": s["model"], "url": s["url"], "ok": s["health"].get("ok"),
            "why": s["health"].get("why"), "models": s["health"].get("models"), "last": s.get("last"),
            "open_speech": s["open_speech"]}


FEATURE = Feature(
    name="llm",
    actions=("llm", "nlu_mode"),
    words=("smart mode", "simple mode", "rules mode", "basic mode"),
    priority=100,                               # first say, so in smart mode the model hears everything
    configure=configure,
    understand=understand,
    run=run,
    arms_for=arms_for,
    label=label,
    page_actions={"nlu_mode": _page_mode},
    do_commands={"nlu_mode": _do, "nlu_test": _do},
    state=state,
)
