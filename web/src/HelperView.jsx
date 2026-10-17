import { useEffect, useState } from "react";
import { post, fmtDist, fmtCm, shortSize, longSize, cap } from "./api.js";
import { useFrame, useLocal, useReach } from "./hooks.js";
import { Check } from "./icons.jsx";
import { roleFor, sayPrefix } from "./roles.js";
import Feed, { LiveBadge, waitText } from "./Feed.jsx";

const STAGES = [
  { id: "look", label: "Find it" },
  { id: "reach", label: "Reach" },
  { id: "grip", label: "Grip" },
  { id: "lift", label: "Lift" },
  { id: "deliver", label: "Hand it to you" },
];

// The Helper tab: arm 17's own camera and the overhead desk camera side by side, marking the same
// things; then the list of what's within reach, split into Verified and Unidentified.
export default function HelperView({ st, cams, busy, bump, onNotice }) {
  const { helper: wristCam, desk: deskCam } = cams;
  const arms = st.arms || [];
  const [deskKind, setDeskKind] = useLocal("bb:deskKind", "color");
  const kind = deskCam && deskCam.has_color === false ? "depth" : deskKind;

  const wristSrc = useFrame(wristCam ? wristCam.name : null, "color", !!wristCam, 150, bump, true);
  const deskSrc = useFrame(deskCam ? deskCam.name : null, kind, !!deskCam, 150, bump, true);

  // ---- things within reach, as one list ----
  const items = useReach(wristCam, deskCam);
  const [selKey, setSelKey] = useState(null);
  const [hovKey, setHovKey] = useState(null);
  useEffect(() => { if (selKey !== null && !items.some((t) => t.key === selKey)) setSelKey(null); }, [items, selKey]);
  useEffect(() => {
    const h = (ev) => { if (ev.key === "Escape") setSelKey(null); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, []);
  const pick = (k) => setSelKey((s) => (s === k ? null : k));

  // the thing the arm is going for right now: its name appears in the router's "current" line
  const cur = (st.phase === "running" && st.current ? st.current : "").toLowerCase();
  const isTarget = (t) => !!cur && t.named && cur.includes(t.label.toLowerCase());

  // boxes for one feed: each item where that camera sees it
  const boxesFor = (cam, side) => {
    if (!cam) return [];
    const [W, H] = cam.size || [640, 480];
    return items.filter((t) => t[side] && t[side].seen).map((t) => {
      const [x0, y0, x1, y1] = t[side].obj.bbox, pad = 4;
      const l = Math.max(0, x0 - pad), tp = Math.max(0, y0 - pad);
      return {
        key: t.key, label: t.label, sel: t.key === selKey, hov: t.key === hovKey, target: isTarget(t), low: y0 < H * 0.12,
        dist: fmtDist(t[side].obj.dist_mm),
        l: (l / W) * 100, t: (tp / H) * 100, w: ((Math.min(W, x1 + pad) - l) / W) * 100, h: ((Math.min(H, y1 + pad) - tp) / H) * 100,
      };
    });
  };

  // ---- click a thing, then "Grab with Helper" ----
  const sel = selKey === null ? null : items.find((t) => t.key === selKey) || null;
  const [grabBusy, setGrabBusy] = useState(false);
  const [grabMsg, setGrabMsg] = useState(null);
  const prefix = sayPrefix(st);
  const armRole = (armName) => { const a = arms.find((x) => x.name === armName); return a ? roleFor(a, arms).role : cap(armName); };
  async function grab() {
    if (!sel || !sel.grab || grabBusy) return;
    setGrabBusy(true); setGrabMsg({ cls: "", text: "Sending the arm…" });
    try {
      const j = await post({ action: "grab_object", id: sel.grab.obj.id, cam: sel.grab.cam });
      setGrabMsg({ cls: "ok", text: `${j.result.started}. Say “${prefix}stop” to halt it.` });
    } catch (e) {
      setGrabMsg({ cls: "no", text: "Couldn’t grab it: " + e.message });
    } finally { setGrabBusy(false); setTimeout(() => setGrabMsg(null), 6000); }
  }
  let grabWhy = "";
  if (sel) {
    grabWhy = !sel.seen ? "Not in view right now" : busy ? "Wait — an arm is busy"
      : sel.grab ? (sel.grab.info.via === "wrist" ? `Seen by ${armRole(sel.grab.info.arm)}’s own camera` : sel.grab.info.via === "link" ? "Seen by the desk camera" : `${armRole(sel.grab.info.arm)} is closest`)
      : sel.why ? `Can’t grab: ${sel.why}` : "Grabbing isn’t set up for this camera";
  }

  // where a grab is, as the router reports it ("look" → "reach" → "grip" → "lift" → "deliver" → "done")
  const stageAt = STAGES.findIndex((sg) => sg.id === st.stage);
  const grabbing = (st.phase === "running" && stageAt >= 0) || (st.phase === "done" && st.stage === "done");

  const named = items.filter((t) => t.named), unnamed = items.filter((t) => !t.named);
  const seenN = items.filter((t) => t.seen).length, bothN = items.filter((t) => t.wrist && t.desk).length;
  const anyCam = wristCam || deskCam;
  const nm = deskCam && deskCam.naming;
  const nmTxt = !nm || nm.state === "ready" ? "" : nm.state === "error" ? "Naming unavailable" : "Setting up names…";
  const empty = !anyCam ? "No camera is set up for the Helper."
    : [wristCam, deskCam].every((c) => !c || c.error) ? "The cameras aren’t connected."
    : [wristCam, deskCam].every((c) => !c || !c.ok) ? "Waiting for the cameras…"
    : !items.length ? "Nothing within reach right now." : "";
  const count = [seenN ? `${seenN} ${seenN === 1 ? "item" : "items"}` : "", bothN ? (bothN === seenN ? "seen by both cameras" : `${bothN} seen by both cameras`) : "", nmTxt].filter(Boolean).join(" · ");

  // a plain function, not a component: tiles keep their identity (and keyboard focus) across polls
  const tile = (t, generic) => {
    const o = t.obj;
    const phrase = t.grab && t.named ? `grab the ${t.label.toLowerCase()}` : "";
    const tip = `${t.label}. ${longSize(o)}, ${fmtCm(o.dist_mm)} from the camera.` + (phrase ? ` Say “${prefix}${phrase}”.` : "") + (t.seen ? "" : " (Not seen for a moment.)");
    const target = isTarget(t);
    return (
      <button key={t.key} type="button" className={`tile${generic ? " tile--generic" : ""}${t.seen ? "" : " gone"}${target ? " target" : ""}${t.estAt ? "" : " tile--settling"}`}
        aria-pressed={t.key === selKey} title={tip} aria-label={tip}
        onClick={() => pick(t.key)} onPointerEnter={() => setHovKey(t.key)} onPointerLeave={() => setHovKey(null)}>
        <span className="tile__top"><span className="tile__sw" style={{ background: t.swatch || "var(--fill2)" }} />{t.base}{t.n && <span className="tile__n">#{t.n}</span>}</span>
        <span className="tile__st">{target ? "Grabbing now" : [shortSize(o), fmtDist(o.dist_mm)].filter(Boolean).join(" · ")}</span>
      </button>
    );
  };

  const feeds = [wristCam, deskCam].filter(Boolean).length;
  return (
    <div className="panel panel--helper" role="tabpanel" id="panel-helper" aria-labelledby="tab-helper">
      <div className={`feeds${feeds < 2 ? " feeds--one" : ""}`}>
        {wristCam && (
          <Feed cam={wristCam} src={wristSrc} caption={wristCam.label || `${cap(wristCam.arm)} camera`} boxes={boxesFor(wristCam, "wrist")} onPick={pick} waitTxt={waitText(wristCam)}>
            <LiveBadge cam={wristCam} />
          </Feed>
        )}
        {deskCam && (
          <Feed cam={deskCam} src={deskSrc} kind={kind} caption={deskCam.label || "Desk camera"} boxes={boxesFor(deskCam, "desk")} onPick={pick} probe waitTxt={waitText(deskCam)}>
            {deskCam.has_color !== false ? (
              <div className="seg seg--xs" role="group" aria-label="Picture">
                {["color", "depth"].map((v) => <button key={v} type="button" aria-pressed={kind === v} onClick={() => setDeskKind(v)}>{cap(v)}</button>)}
              </div>
            ) : <LiveBadge cam={deskCam} />}
          </Feed>
        )}
        {!feeds && <div className="feed feed--none"><div className="feed__wait">No camera is set up for the Helper.</div></div>}
      </div>

      {grabbing && (
        <ol className="stages" aria-label="Grab progress">
          {STAGES.map((sg, i) => {
            const state = stageAt > i || st.stage === "done" ? "done" : stageAt === i ? "now" : "";
            return (
              <li key={sg.id} className={`stage${state ? " stage--" + state : ""}`} aria-current={state === "now" ? "step" : undefined}>
                <span className="stage__dot">{state === "done" ? <Check size={12} /> : i + 1}</span>{sg.label}
              </li>
            );
          })}
        </ol>
      )}

      <div className="reachhd">
        <div className="sh__inline">
          <h3>Within reach</h3>
          <span className="sh__sub">{cur ? "Going for the highlighted one" : items.some((t) => t.n) ? "Several of a kind are numbered left to right — say “grab the white cup 2”" : "What the Helper can pick up right now"}</span>
        </div>
        <span className="aux" title={nm && nm.detail ? nm.detail : ""}>{count}</span>
      </div>

      {(sel || grabMsg) && (
        <div className={`grabbar${sel && !sel.grab ? " nogo" : ""}`} role="region" aria-label="Selected object">
          {sel && <span className="tile__sw" style={{ background: sel.swatch || "var(--fill2)" }} />}
          <span className="grabbar__txt">
            {sel ? <><b>{sel.label}</b><span>{grabMsg ? grabMsg.text : grabWhy}</span></> : <span className={`msg ${grabMsg.cls}`}>{grabMsg.text}</span>}
          </span>
          {sel && sel.grab && <button type="button" className="btn btn--primary" disabled={grabBusy || busy || !sel.seen} onClick={grab}>Grab with {armRole(sel.grab.info.arm)}</button>}
        </div>
      )}

      <div className="objs">
        {empty ? (
          <div className="objs__empty">{empty}</div>
        ) : (
          <>
            {named.length > 0 && (
              <>
                <div className="group"><i className="group__dot group__dot--ok" /><span className="group__name">Verified</span><span className="group__sub">{named.length} · Buddy knows what these are</span></div>
                <div className="objgrid" role="list">{named.map((t) => tile(t, false))}</div>
              </>
            )}
            {unnamed.length > 0 && (
              <>
                <div className="group"><i className="group__dot group__dot--warn" /><span className="group__name">Unidentified</span><span className="group__sub">{unnamed.length} · Still reachable — say “grab the {unnamed[0].label.toLowerCase()}” or tap one, then Grab</span></div>
                <div className="objgrid" role="list">{unnamed.map((t) => tile(t, true))}</div>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
