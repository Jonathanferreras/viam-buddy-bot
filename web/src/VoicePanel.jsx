import { useEffect, useRef, useState } from "react";
import { Mic, MicOff, Check, Alert, Play, Stop } from "./icons.jsx";
import { isGameCommand, wakeWord } from "./roles.js";
import { post } from "./api.js";

// What the orb and the headline say in each phase the router reports. `W` is the wake word ("Hey buddy").
const PHASES = (W) => ({
  starting:   { tone: "idle",  icon: "mic",    t: "Starting up…",               s: "Loading the speech model" },
  waiting:    { tone: "wake",  icon: "mic",    t: `Say “${W}”`,                 s: "Ask me to grab something, or let’s play a game." },
  hearing:    { tone: "hear",  icon: "bars",   t: "I’m listening…",             s: "Go ahead — what do you need?" },
  processing: { tone: "think", icon: "bars",   t: "Understanding…",             s: "" },
  running:    { tone: "run",   icon: "bars",   t: "On it",                      s: `Say “${W}, stop” to halt` },
  done:       { tone: "run",   icon: "check",  t: "Done",                       s: "" },
  stopped:    { tone: "think", icon: "stop",   t: "Stopped",                    s: "Arms halted where they are" },
  nomatch:    { tone: "bad",   icon: "alert",  t: "Didn’t catch that",          s: `Try again: “${W}”, then the command` },
  notfound:   { tone: "bad",   icon: "alert",  t: "Couldn’t find it",           s: "Check it’s on the desk and in the camera’s view" },
  error:      { tone: "bad",   icon: "alert",  t: "Something went wrong",       s: "" },
  offline:    { tone: "bad",   icon: "mic",    t: "Microphone not connected",   s: "Retrying…" },
  muted:      { tone: "idle",  icon: "micoff", t: "Buddy is muted",             s: `Tap the mic and I’ll listen for “${W}” again` },
});
const TOGGLE_TEXT = {
  waiting: { t: "Listening", s: "Say a command, like “grab the red ball”" },
  hearing: { t: "I’m listening…", s: "Go ahead — I’ll act when you pause" },
  running: { s: "Say “stop” to halt" },
  muted: { t: "Mic is off", s: "Tap the mic, then say a command" },
};
const micMode = (st) => (st.mic && st.mic.mode) || "wake_word";
const canMute = (st) => ["toggle", "wake"].includes(micMode(st));

function micText(st, micOn, phase, key, dflt) {
  if (phase === "running" && canMute(st) && !micOn && key === "s")
    return st.mic && st.mic.stop_only ? "Muted — it still hears “stop” while the arm moves" : "Tap the mic and say “stop” to halt it";
  if (micMode(st) !== "toggle") return dflt;
  return (TOGGLE_TEXT[phase] && TOGGLE_TEXT[phase][key]) || dflt;
}

// The seconds Buddy waits for a command after the wake word, drawn as a draining bar.
function Countdown({ active, seconds }) {
  const [since, setSince] = useState(0);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!active) { setSince(0); return; }
    setSince(Date.now());
    const id = setInterval(() => tick((n) => n + 1), 100);
    return () => clearInterval(id);
  }, [active]);
  if (!active || !since) return null;
  const left = Math.max(0, 1 - (Date.now() - since) / (seconds * 1000));
  return (
    <div className="countdown" role="timer" aria-label={`${Math.ceil(left * seconds)} seconds to say a command`}>
      <i style={{ transform: `scaleX(${left.toFixed(3)})` }} />
    </div>
  );
}

// ---- the orb: color by phase, size and bars by the live mic level ----
const BAR_SHAPE = [0.55, 0.85, 1, 0.8, 0.6];
function Orb({ tone, icon, levels, toggle, on, onToggle }) {
  const ref = useRef(null);
  useEffect(() => {
    let lv = 0, raf = 0;
    const el = ref.current;
    const bars = el ? Array.from(el.querySelectorAll(".orb__bar")) : [];
    const frame = (t) => {
      const L = levels.current;
      const live = L.live && (!toggle || on);
      lv += ((live ? L.target : 0) - lv) * (L.target > lv ? 0.4 : 0.12);
      if (el) {
        el.style.setProperty("--lv", live ? lv.toFixed(3) : "0");
        bars.forEach((b, i) => {
          const wobble = 0.75 + 0.5 * Math.abs(Math.sin(t / 110 + i * 1.7));
          b.style.setProperty("--b", Math.max(0.18, Math.min(1, (0.15 + lv) * BAR_SHAPE[i] * wobble)).toFixed(3));
        });
      }
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [levels, toggle, on, icon]);

  const inner =
    icon === "bars" ? <span className="orb__bars" aria-hidden="true">{BAR_SHAPE.map((_, i) => <i key={i} className="orb__bar" />)}</span>
    : icon === "check" ? <Check size={48} />
    : icon === "alert" ? <Alert size={44} />
    : icon === "stop" ? <Stop size={40} />
    : icon === "micoff" ? <MicOff size={44} />
    : <Mic size={44} />;
  const label = toggle ? (on ? "Buddy is listening — tap to mute" : "Buddy is muted — tap to listen") : "Voice status";
  return (
    <div ref={ref} className={`orb orb--${tone}${toggle && !on ? " orb--off" : ""}`}>
      <span className="orb__ring orb__ring--2" aria-hidden="true" />
      <span className="orb__ring orb__ring--1" aria-hidden="true" />
      {toggle ? (
        <button type="button" className="orb__btn" role="switch" aria-checked={on} aria-label={label} title={on ? "Mute Buddy (M)" : "Unmute Buddy (M)"} onClick={onToggle}>{inner}</button>
      ) : (
        <span className="orb__btn" role="img" aria-label={label}>{inner}</span>
      )}
    </div>
  );
}

// ---- Hey Viam -> Command -> Action ----
function Steps({ st, flow, micOn, W }) {
  const toggle = micMode(st) === "toggle";
  const { wakeOn, showCmd, act, outcome, cmd } = flow;
  const bad = outcome === "error" || outcome === "notfound";
  const s3 = !act ? "" : outcome === "running" ? "on active" : bad ? "bad" : outcome === "stopped" ? "warn" : "on";
  const s3val = !act ? "" : outcome === "running" ? (st.current || cmd.label || "") : outcome === "notfound" ? "not found" : outcome === "error" ? "failed" : outcome === "stopped" ? "stopped" : "done";
  return (
    <ol className="steps" aria-label="How a command runs">
      <li className={`step${wakeOn ? " on" : ""}${wakeOn && !cmd ? " active" : ""}`}>
        <span className="step__dot">{wakeOn ? <Check size={14} /> : "1"}</span>
        <span className="step__lbl">{toggle ? "You spoke" : W}</span>
        <span className="step__val">{wakeOn ? (toggle ? "heard you" : "heard") : flow.typed ? "typed" : ""}</span>
      </li>
      <li className={`step${showCmd && cmd.matched ? " on" : ""}${showCmd && !cmd.matched ? " bad" : ""}${!showCmd && wakeOn ? " active" : ""}`}>
        <span className="step__dot">{showCmd ? (cmd.matched ? <Check size={14} /> : "!") : "2"}</span>
        <span className="step__lbl">Command</span>
        <span className="step__val">{showCmd ? (cmd.label || cmd.text || "didn’t catch it") : wakeOn ? "listening…" : ""}</span>
      </li>
      <li className={`step ${s3}`.trim()}>
        <span className="step__dot">{!act ? "3" : outcome === "running" ? <Play size={14} /> : bad ? "!" : outcome === "stopped" ? <Stop size={12} /> : <Check size={14} />}</span>
        <span className="step__lbl">Action</span>
        <span className="step__val">{s3val}</span>
      </li>
    </ol>
  );
}

// `say`: a few full phrases worth trying right now (shown while Buddy waits for the wake word).
// How Buddy understands what you say: Simple (the usual phrases and rules) or Smart (a local LLM through
// Ollama works out what you meant — src/features/llm.py). An orange dot: smart mode can't reach the model
// right now, so the rules are standing in.
function NluToggle({ st }) {
  const n = st.features && st.features.llm;
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  if (!n) return null;
  const smart = n.mode === "llm";
  const down = smart && n.ok === false;
  async function set(mode) {
    if (busy || mode === n.mode) return;
    setBusy(true); setErr("");
    try { await post({ action: "nlu_mode", mode }); }
    catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }
  const tip = err ? `Couldn’t switch: ${err}`
    : !smart ? "Simple mode: the usual command phrases. Switch to Smart to have an LLM work out what you mean (or say “smart mode”)."
    : down ? `Smart mode can’t use the model right now — ${n.why}. The usual rules are standing in.`
    : `Smart mode: ${n.model} (Ollama) works out what you mean. Say “simple mode” to switch back.`;
  return (
    <div className={`nlu${down ? " nlu--down" : ""}`} role="group" aria-label="How Buddy understands you" title={tip}>
      <button type="button" className={smart ? "" : "on"} aria-pressed={!smart} disabled={busy} onClick={() => set("rules")}>Simple</button>
      <button type="button" className={smart ? "on" : ""} aria-pressed={smart} disabled={busy} onClick={() => set("llm")}>{down && <i />}Smart</button>
    </div>
  );
}

export default function VoicePanel({ st, flow, levels, micOn, onToggleMic, say = [] }) {
  const W = wakeWord(st);
  const P = PHASES(W);
  const phase = P[st.phase] ? st.phase : "starting";
  const p = P[phase];
  const toggle = micMode(st) === "toggle", mutable = canMute(st);
  const game = phase === "running" && isGameCommand(st.current || (flow.cmd && flow.cmd.label), st);
  const tone = game ? "game" : p.tone;

  const transcript = flow.cmd && flow.cmd.text && !["muted", "offline", "starting"].includes(phase);
  const title = ["running", "notfound", "done"].includes(st.phase) && st.current ? st.current : micText(st, micOn, phase, "t", p.t);
  const sub = st.phase === "error" ? st.last_error || "" : micText(st, micOn, phase, "s", p.s);
  const windowS = (st.mic && st.mic.window_s) || 5;

  return (
    <section className={`voice${flow.flash ? " voice--flash" : ""}`} aria-label="Voice">
      <NluToggle st={st} />
      <Orb tone={tone} icon={p.icon} levels={levels} toggle={mutable} on={micOn} onToggle={onToggleMic} />
      <div className="voice__text">
        {transcript ? (
          <h1 className="voice__title voice__title--quote">
            {flow.typed || toggle ? "" : <span className="voice__wake">“{W}, </span>}
            {flow.typed || toggle ? `“${flow.cmd.text}”` : `${flow.cmd.text}”`}
          </h1>
        ) : (
          <h1 className="voice__title">{title}</h1>
        )}
        <p className="voice__sub">
          {toggle && !["muted", "starting", "offline"].includes(phase) && (
            <span className={`mictag${micOn ? " on" : ""}`}><i />{micOn ? "Mic on" : "Mic off"}</span>
          )}
          {transcript && st.phase === "running" ? (st.current ? `On it. ${st.current}.` : sub) : sub}
        </p>
        <Countdown active={phase === "hearing" && !toggle} seconds={windowS} />
      </div>
      <div className="voice__sep" />
      <Steps st={st} flow={flow} micOn={micOn} W={W} />
      {say.length > 0 && !["running", "muted", "offline", "starting"].includes(phase) && !transcript && (
        <div className="say" aria-label="Try saying">
          <div className="eyebrow">Try saying</div>
          <div className="say__chips">
            {say.map((s) => <span key={s} className="say__chip"><Mic size={14} />“{s}”</span>)}
          </div>
        </div>
      )}
      {phase === "running" && (
        <div className="voice__stop">
          <Stop size={16} />
          <span>Say <strong>“stop”</strong> at any time and both arms freeze.</span>
        </div>
      )}
    </section>
  );
}
