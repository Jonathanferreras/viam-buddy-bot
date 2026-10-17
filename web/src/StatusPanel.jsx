import { useState } from "react";
import { post } from "./api.js";
import { Reset, Release, Stop, Logo, Camera } from "./icons.jsx";
import { roleFor, armKey, isGameCommand, camLabel } from "./roles.js";

// One row per arm: its job, what it's doing, joint angles, and the controls that apply right now —
// Stop while it moves, Reset / Release while it's still.
function ArmRow({ arm, arms, st, busy, onNotice }) {
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
    <article className={`srow srow--${kind}${dim ? " srow--dim" : ""}`} aria-label={`${role} (${arm.name})`}>
      <div className="srow__top">
        <div className="srow__who">
          <span className={`srow__ic srow__ic--${role.toLowerCase()}`}><Logo size={18} tone="currentColor" /></span>
          <div className="srow__txt">
            <div className="srow__name">{role} <span>{arm.name}</span></div>
            <div className={`srow__line${quiet ? " quiet" : ""}`} title={err || line}>{err || line}</div>
          </div>
        </div>
        <span className={`pill pill--${kind}`}><i />{pill}</span>
      </div>
      {arm.ok && arm.joints && (
        <div className="srow__joints" aria-label="Joint angles">
          {arm.joints.map((v, i) => <span key={i}>J{i + 1} <b>{Math.round(v)}°</b></span>)}
        </div>
      )}
      <div className="srow__btns">
        {active ? (
          <button type="button" className="btn btn--stop" disabled={btnBusy} title={`Halt ${arm.name} where it is`} onClick={() => act("stop", "stop")}>
            <Stop size={11} />Stop
          </button>
        ) : (
          <>
            <button type="button" className="btn" disabled={disabled || !arm.can_reset}
              title={!arm.can_reset ? `No starting position saved for ${arm.name} yet` : `Send ${arm.name} back to its starting position`}
              onClick={() => act("reset", "reset", { arm: armKey(arm) })}><Reset size={12} />Reset</button>
            {arm.has_gripper && (
              <button type="button" className={`btn${arm.holding ? " btn--on" : ""}`} disabled={disabled}
                title={arm.holding ? `Open ${arm.name}’s gripper and let go of the ${arm.holding}` : `Open ${arm.name}’s gripper`}
                onClick={() => act("release", "let go", { arm: armKey(arm) })}><Release size={12} />Release</button>
            )}
          </>
        )}
      </div>
    </article>
  );
}

// A camera that isn't on an arm (the overhead desk camera): is it live, and how fast.
function CamRow({ cam, arms, helping }) {
  const streaming = !!(cam.ok && !cam.paused && !cam.error);
  const kind = cam.error ? "off" : "idle";
  const label = camLabel(cam, arms);
  const sub = cam.error ? cam.error : !cam.ok ? "Starting the camera…" : cam.table === false ? "Can’t find the desk surface" : helping || "Overhead · helps the Helper see the desk";
  return (
    <article className={`srow srow--${kind} srow--cam`} aria-label={cam.label || cam.name}>
      <div className="srow__top">
        <div className="srow__who">
          <span className="srow__ic srow__ic--cam"><Camera size={18} /></span>
          <div className="srow__txt">
            <div className="srow__name">{label} camera {cam.has_color === false || cam.range_mm ? <span>depth</span> : null}</div>
            <div className="srow__line quiet" title={sub}>{sub}</div>
          </div>
        </div>
        <span className={`pill pill--live${streaming ? " on" : ""}${cam.error ? " bad" : ""}`}>
          <i />{cam.error ? "Not connected" : !cam.ok ? "Starting" : cam.paused ? "Paused" : "Live"}
          {streaming && <span className="rs">{cam.fps} fps</span>}
        </span>
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
  const helping = game.stage ? "Overhead · checks the cups after each shuffle" : busy && st.stage ? "Overhead · recording this grab" : "";
  return (
    <section className="status" aria-label="Component status">
      <div className="status__hd">
        <h2>Status</h2>
        <button type="button" className={`btn btn--danger${sent ? " sent" : ""}`} title="Halt every arm and gripper right now (Esc)" onClick={stopAll}>
          <Stop size={12} />Stop all
        </button>
      </div>
      <div className="status__list">
        {arms.map((a) => <ArmRow key={armKey(a)} arm={a} arms={arms} st={st} busy={busy} onNotice={onNotice} />)}
        {deskCams.map((c) => <CamRow key={c.name} cam={c} arms={arms} helping={helping} />)}
      </div>
    </section>
  );
}
