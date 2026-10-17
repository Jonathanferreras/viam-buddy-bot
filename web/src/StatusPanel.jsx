import { useState } from "react";
import { post } from "./api.js";
import { Reset, Release, Stop, Logo, Camera, Check } from "./icons.jsx";
import { roleFor, armKey, isGameCommand, camLabel } from "./roles.js";

// Small stroke icons for the arm tile's buttons (same 24-unit grid as icons.jsx).
const Ico = ({ size = 13, children }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
);
// Four-way arrows: "move it by hand" (manual mode).
const Move = (p) => (
  <Ico {...p}><path d="M12 3v18M3 12h18" /><path d="M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3" /></Ico>
);
// A house: "this is where it starts".
const House = (p) => <Ico {...p}><path d="M4 11l8-7 8 7" /><path d="M6 10v10h12V10" /><path d="M10 20v-5h4v5" /></Ico>;
// A map pin: "put things here" (the clean-up spot).
const Pin = (p) => <Ico {...p}><path d="M12 21s-6-5.6-6-11a6 6 0 0 1 12 0c0 5.4-6 11-6 11z" /><circle cx="12" cy="10" r="2.2" /></Ico>;

// An icon-only button: the name lives in its tooltip and its accessible label.
function IconBtn({ label, title, className = "", ...rest }) {
  return <button type="button" className={`btn btn--icon ${className}`} aria-label={label} title={title || label} {...rest} />;
}

// One tile per arm: its job, its state, one line of what it's doing, and the controls that apply
// right now, as one row of icon buttons — Stop while it moves, Reset / Release / Manual while it's still,
// and in manual mode (moved by hand) Done plus buttons to save where it is.
function ArmTile({ arm, arms, st, busy, onNotice }) {
  const { role, blurb } = roleFor(arm, arms);
  const [btnBusy, setBtnBusy] = useState(false);
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState("");

  const manualOn = !!(st.features && st.features.manual && st.features.manual.arms || {})[armKey(arm)];
  const cleanupArm = st.features && st.features.cleanup && st.features.cleanup.arm;

  let kind = "idle", pill = "Idle", line = blurb, quiet = true;
  if (!arm.ok) { kind = "off"; pill = "Offline"; line = arm.error || "No reading"; }
  else if (manualOn) { kind = "manual"; pill = "Manual"; line = saved || "Move it by hand"; quiet = false; }
  else if (arm.action) { kind = isGameCommand(arm.action, st) ? "game" : "busy"; pill = kind === "game" ? "Playing" : arm.moving ? "Moving" : "Working"; line = arm.action; quiet = false; }
  else if (arm.moving) { kind = "ext"; pill = "Moving"; line = "Moving — not from a voice command"; quiet = false; }
  else if (arm.holding) { kind = "hold"; pill = "Holding"; line = `Holding the ${arm.holding}`; quiet = false; }
  const active = kind === "busy" || kind === "game" || kind === "ext";
  const dim = busy && !active && kind !== "off"; // another arm has the floor

  async function act(action, verb, body = {}, done = "") {
    setBtnBusy(true); setErr("");
    try { await post({ action, ...body }); setSaved(done); }
    catch (e) { setErr(`Couldn’t ${verb}: ${e.message}`); onNotice && onNotice(`Couldn’t ${verb}: ${e.message}`); }
    finally { setBtnBusy(false); }
  }
  const disabled = busy || btnBusy || !arm.ok;
  const key = armKey(arm);

  return (
    <article className={`stile stile--${kind}${dim ? " stile--dim" : ""}`} aria-label={`${role} (${arm.name})`}>
      <div className="stile__top">
        <span className={`stile__ic stile__ic--${role.toLowerCase()}`}><Logo size={18} tone="currentColor" /></span>
        <span className={`pill pill--${kind}`}><i />{pill}</span>
      </div>
      <div className="stile__txt">
        <div className="stile__name">{role} <span>{arm.name}</span></div>
        <div className={`stile__line${quiet ? " quiet" : ""}`} title={err || line}>{err || line}</div>
      </div>
      <div className="stile__btns">
        {kind === "manual" ? (
          <>
            <IconBtn className="btn--primary" disabled={btnBusy} label="Done"
              title={`Done: lock ${arm.name} where it is (it holds still; nothing moves)`}
              onClick={() => act("manual_mode", "leave manual mode", { arm: key, on: false })}><Check size={13} /></IconBtn>
            <IconBtn disabled={btnBusy} label="Save start"
              title={`Save start: make where ${arm.name} is now its starting position (what Reset goes back to)`}
              onClick={() => act("manual_save_home", "save the start", { arm: key }, "Saved as its starting position")}><House /></IconBtn>
            {cleanupArm === arm.name && (
              <IconBtn disabled={btnBusy} label="Clean-up spot"
                title={`Clean-up spot: things go under ${arm.name}’s gripper here when you say “clean up”`}
                onClick={() => act("cleanup_save_spot", "save the clean-up spot", {}, "Saved as the clean-up spot")}><Pin /></IconBtn>
            )}
          </>
        ) : active ? (
          <IconBtn className="btn--stop" disabled={btnBusy} label="Stop" title={`Stop: halt ${arm.name} where it is`}
            onClick={() => act("stop", "stop")}><Stop size={12} /></IconBtn>
        ) : (
          <>
            <IconBtn disabled={disabled || !arm.can_reset} label="Reset"
              title={!arm.can_reset ? `Reset: no starting position saved for ${arm.name} yet` : `Reset: send ${arm.name} back to its starting position`}
              onClick={() => act("reset", "reset", { arm: armKey(arm) })}><Reset size={13} /></IconBtn>
            {arm.has_gripper && (
              <IconBtn className={arm.holding ? "btn--on" : ""} disabled={disabled} label="Release"
                title={arm.holding ? `Release: open ${arm.name}’s gripper and let go of the ${arm.holding}` : `Release: open ${arm.name}’s gripper`}
                onClick={() => act("release", "let go", { arm: armKey(arm) })}><Release size={13} /></IconBtn>
            )}
            <IconBtn disabled={disabled} label="Manual mode"
              title={`Manual mode: move ${arm.name} by hand. Hold it — it floats rather than locks.`}
              onClick={() => act("manual_mode", "enter manual mode", { arm: key, on: true })}><Move /></IconBtn>
          </>
        )}
      </div>
    </article>
  );
}

// A camera that isn't on an arm (the overhead desk camera): is it live, and how fast.
function CamTile({ cam, arms, helping }) {
  const streaming = !!(cam.ok && !cam.paused && !cam.error);
  const sub = cam.error ? cam.error : !cam.ok ? "Starting the camera…" : cam.table === false ? "Can’t find the desk" : helping || "Overhead view";
  return (
    <article className={`stile${cam.error ? " stile--off" : ""}`} aria-label={cam.label || cam.name}>
      <div className="stile__top">
        <span className="stile__ic stile__ic--cam"><Camera size={18} /></span>
        <span className={`pill pill--live${streaming ? " on" : ""}${cam.error ? " bad" : ""}`}>
          <i />{cam.error ? "Offline" : !cam.ok ? "Starting" : cam.paused ? "Paused" : "Live"}
          {streaming && <span className="rs">{cam.fps} fps</span>}
        </span>
      </div>
      <div className="stile__txt">
        <div className="stile__name">{camLabel(cam, arms)} camera {cam.has_color === false || cam.range_mm ? <span>depth</span> : null}</div>
        <div className="stile__line quiet" title={sub}>{sub}</div>
      </div>
    </article>
  );
}

export default function StatusPanel({ st, busy, onNotice }) {
  const arms = st.arms || [], cams = st.cams || [];
  const [sent, setSent] = useState(false);
  const deskCams = cams.filter((c) => !c.arm);
  if (!arms.length && !deskCams.length) return null;
  async function stopAll() {
    setSent(true);
    try { await post({ action: "stop" }); }
    catch (e) { onNotice("Couldn’t stop: " + e.message); }
    finally { setTimeout(() => setSent(false), 300); }
  }
  const game = st.game || {};
  const helping = game.stage ? "Overhead · checks cups" : busy && st.stage ? "Overhead · recording" : "";
  const n = (k, one, many) => (k ? `${k} ${k === 1 ? one : many}` : "");
  return (
    <section className="status" aria-label="Component status">
      <div className="status__hd">
        <h2>Status</h2>
        <span className="aux">{[n(arms.length, "arm", "arms"), n(deskCams.length, "camera", "cameras")].filter(Boolean).join(" · ")}</span>
      </div>
      <div className="status__grid">
        {arms.map((a) => <ArmTile key={armKey(a)} arm={a} arms={arms} st={st} busy={busy} onNotice={onNotice} />)}
        {deskCams.map((c) => <CamTile key={c.name} cam={c} arms={arms} helping={helping} />)}
        <button type="button" className={`stile stile--stop${sent ? " sent" : ""}`} title="Halt every arm and gripper right now (Esc)" onClick={stopAll}>
          <span className="stile__ic stile__ic--stop"><Stop size={16} /></span>
          <span className="stile__txt"><span className="stile__name">Stop all</span><span className="stile__line quiet">Both arms freeze · Esc</span></span>
        </button>
      </div>
    </section>
  );
}
