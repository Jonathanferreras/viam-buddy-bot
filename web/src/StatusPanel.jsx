import { useState } from "react";
import { post } from "./api.js";
import { Reset, Release, Stop, Logo, Camera } from "./icons.jsx";
import { roleFor, armKey, isGameCommand, camLabel } from "./roles.js";

// One tile per arm: its job, its state, one line of what it's doing, and the controls that apply
// right now — Stop while it moves, Reset / Release while it's still.
function ArmTile({ arm, arms, st, busy, onNotice }) {
  const { role, blurb } = roleFor(arm, arms);
  const [btnBusy, setBtnBusy] = useState(false);
  const [err, setErr] = useState("");

  let kind = "idle", pill = "Idle", line = blurb, quiet = true;
  if (!arm.ok) { kind = "off"; pill = "Offline"; line = arm.error || "No reading"; }
  else if (arm.action) { kind = isGameCommand(arm.action, st) ? "game" : "busy"; pill = kind === "game" ? "Playing" : arm.moving ? "Moving" : "Working"; line = arm.action; quiet = false; }
  else if (arm.moving) { kind = "ext"; pill = "Moving"; line = "Moving — not from a voice command"; quiet = false; }
  else if (arm.holding) { kind = "hold"; pill = "Holding"; line = `Holding the ${arm.holding}`; quiet = false; }
  const active = kind === "busy" || kind === "game" || kind === "ext";
  const dim = busy && !active && kind !== "off"; // another arm has the floor

  async function act(action, verb, body = {}) {
    setBtnBusy(true); setErr("");
    try { await post({ action, ...body }); }
    catch (e) { setErr(`Couldn’t ${verb}: ${e.message}`); onNotice && onNotice(`Couldn’t ${verb}: ${e.message}`); }
    finally { setBtnBusy(false); }
  }
  const disabled = busy || btnBusy || !arm.ok;

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
        {active ? (
          <button type="button" className="btn btn--stop" disabled={btnBusy} title={`Halt ${arm.name} where it is`} onClick={() => act("stop", "stop")}>
            <Stop size={11} />Stop
          </button>
        ) : (
          <>
            <button type="button" className="btn" disabled={disabled || !arm.can_reset}
              title={!arm.can_reset ? `No starting position saved for ${arm.name} yet` : `Send ${arm.name} back to its starting position`}
              onClick={() => act("reset", "reset", { arm: armKey(arm) })}><Reset size={11} />Reset</button>
            {arm.has_gripper && (
              <button type="button" className={`btn${arm.holding ? " btn--on" : ""}`} disabled={disabled}
                title={arm.holding ? `Open ${arm.name}’s gripper and let go of the ${arm.holding}` : `Open ${arm.name}’s gripper`}
                onClick={() => act("release", "let go", { arm: armKey(arm) })}><Release size={11} />Release</button>
            )}
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
