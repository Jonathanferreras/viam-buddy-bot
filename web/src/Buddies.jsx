import { useState } from "react";
import { post } from "./api.js";
import { useFrame } from "./hooks.js";
import { Reset, Release, Stop } from "./icons.jsx";
import { roleFor, armKey, isGameCommand } from "./roles.js";

// One card per arm: its wrist camera, its job, what it's doing, Reset / Release, and joint angles.
function ArmCard({ arm, arms, cam, busy, bump }) {
  const { role, blurb } = roleFor(arm, arms);
  const camName = cam && !cam.error ? cam.name : null;
  const src = useFrame(camName, "color", true, 450, bump);
  const [btnBusy, setBtnBusy] = useState(false);
  const [err, setErr] = useState("");

  let kind = "idle", pill = "Idle", line = blurb, quiet = true;
  if (!arm.ok) { kind = "off"; pill = "Offline"; line = arm.error || "No reading"; }
  else if (arm.action) { kind = isGameCommand(arm.action) ? "game" : "busy"; pill = arm.moving ? "Moving" : "Working"; line = arm.action; quiet = false; }
  else if (arm.moving) { kind = "ext"; pill = "Moving"; line = "Moving — not from a voice command"; quiet = false; }
  else if (arm.holding) { kind = "hold"; pill = "Holding"; line = `Holding the ${arm.holding}`; quiet = false; }

  const nocam = !cam ? "No camera" : cam.error ? "Camera not connected" : "Starting the camera…";
  const disabled = busy || btnBusy || !arm.ok;

  async function act(action, verb) {
    setBtnBusy(true); setErr("");
    try { await post({ action, arm: armKey(arm) }); }
    catch (e) { setErr(`Couldn’t ${verb}: ${e.message}`); }
    finally { setBtnBusy(false); }
  }

  return (
    <article className={`buddy buddy--${kind}`} aria-label={`${role} (${arm.name})`}>
      <div className="buddy__cam">
        {src ? <img src={src} alt={`Live view from ${arm.name}’s camera`} /> : <span className="buddy__nocam">{nocam}</span>}
        <span className={`pill pill--${kind}`}><i />{pill}</span>
      </div>
      <div className="buddy__row">
        <div className="buddy__who">
          <div className="buddy__name">{role} <span>{arm.name}</span></div>
          <div className={`buddy__line${quiet ? " quiet" : ""}`} title={err || line}>{err || line}</div>
        </div>
        <div className="buddy__btns">
          <button type="button" className="btn" disabled={disabled || !arm.can_reset}
            title={!arm.can_reset ? `No starting position saved for ${arm.name} yet` : `Send ${arm.name} back to its starting position`}
            onClick={() => act("reset", "reset")}><Reset size={14} />Reset</button>
          {arm.has_gripper && (
            <button type="button" className={`btn${arm.holding ? " btn--on" : ""}`} disabled={disabled}
              title={arm.holding ? `Open ${arm.name}’s gripper and let go of the ${arm.holding}` : `Open ${arm.name}’s gripper`}
              onClick={() => act("release", "let go")}><Release size={14} />Release</button>
          )}
        </div>
      </div>
      {arm.ok && arm.joints && (
        <div className="buddy__joints" aria-label="Joint angles">
          {arm.joints.map((v, i) => <span key={i}>J{i + 1} <b>{Math.round(v)}°</b></span>)}
        </div>
      )}
    </article>
  );
}

export default function Buddies({ st, busy, bump, onNotice }) {
  const arms = st.arms || [], cams = st.cams || [];
  const [sent, setSent] = useState(false);
  if (!arms.length) return null;
  async function stopAll() {
    setSent(true);
    try { await post({ action: "stop" }); }
    catch (e) { onNotice("Couldn’t stop: " + e.message); }
    finally { setTimeout(() => setSent(false), 300); }
  }
  return (
    <section className="buddies" aria-label="Arms">
      <div className="sh">
        <h2>Your buddies</h2>
        <button type="button" className={`btn btn--danger${sent ? " sent" : ""}`} title="Halt every arm and gripper right now (Esc)" onClick={stopAll}>
          <Stop size={14} />Stop all
        </button>
      </div>
      <div className="buddies__list">
        {arms.map((a) => (
          <ArmCard key={armKey(a)} arm={a} arms={arms} cam={cams.find((c) => c.arm && c.arm === a.name)} busy={busy} bump={bump} />
        ))}
      </div>
    </section>
  );
}
