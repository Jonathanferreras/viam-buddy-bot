import { useEffect, useRef, useState } from "react";
import { post, depthAt, fmtMM, fmtDist, fmtCm, shortSize, longSize, cap } from "./api.js";
import { useFrame, useLocal, useTracks } from "./hooks.js";
import { Mic } from "./icons.jsx";
import { camLabel, roleFor } from "./roles.js";

// ---- one picture (color or depth) with its detection boxes and the hover readout ----
function View({ kind, src, cam, boxes, probe, probeMM, waitTxt, onPick }) {
  return (
    <figure className="view" data-kind={kind}>
      {src ? (
        <img src={src} alt={kind === "color" ? `Live color view from ${cam.label || cam.name}` : `Live depth view: warm colors are closer, cool colors are farther away`} />
      ) : (
        <div className="view__blank" />
      )}
      <div className="view__ov" aria-hidden="true">
        {boxes.map((b) => (
          <div key={b.key} className={`box${b.sel ? " sel" : ""}${b.hov ? " hov" : ""}${b.target ? " target" : ""}${b.low ? " low" : ""}`}
            style={{ left: `${b.l}%`, top: `${b.t}%`, width: `${b.w}%`, height: `${b.h}%` }} onClick={() => onPick(b.key)}>
            <b>{b.label}{b.target && b.dist ? ` · ${b.dist}` : ""}</b>
          </div>
        ))}
      </div>
      <figcaption className="pill pill--dark">{kind === "color" ? "Color" : "Depth"}</figcaption>
      {waitTxt && <div className="view__wait">{waitTxt}</div>}
      {probe && <span className={`probe${probe.x > 0.7 ? " flip" : ""}`} style={{ left: `${probe.px}%`, top: `${probe.py}%` }}><b>{fmtMM(probeMM)}</b></span>}
    </figure>
  );
}

export default function DeskView({ st, busy, bump, onNotice }) {
  const cams = st.cams || (st.live ? [{ ...st.live, name: st.live.camera }] : []);
  const arms = st.arms || [];
  const [camSel, setCamSel] = useLocal("bb:cam", null);
  const cam = cams.find((c) => c.name === camSel) || cams[0] || null;
  const name = cam ? cam.name : null;
  const [views, setViews] = useLocal("bb:view", {});
  let view = (name && views[name]) || "both";
  if (cam && cam.has_color === false) view = "depth";
  const setView = (v) => setViews({ ...views, [name]: v });

  const streaming = !!(cam && cam.ok && !cam.paused && !cam.error);
  const colorSrc = useFrame(name, "color", !!cam && (view === "both" || view === "color"), 120, bump);
  const depthSrc = useFrame(name, "depth", !!cam && (view === "both" || view === "depth"), 120, bump);

  // ---- things on the desk ----
  const tracks = useTracks(cam && cam.ok && !cam.error ? cam.objects : [], cam ? cam.fps : 0);
  const [selKey, setSelKey] = useState(null);
  const [hovKey, setHovKey] = useState(null);
  useEffect(() => { if (selKey !== null && !tracks.some((t) => t.key === selKey)) setSelKey(null); }, [tracks, selKey]);
  useEffect(() => {
    const h = (ev) => { if (ev.key === "Escape") setSelKey(null); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, []);
  const pick = (k) => setSelKey((s) => (s === k ? null : k));

  // the thing the arm is going for right now: its name appears in the router's "current" line
  const cur = (st.phase === "running" && st.current ? st.current : "").toLowerCase();
  const isTarget = (t) => !!cur && t.named && cur.includes(t.label.toLowerCase());

  const [W, H] = (cam && cam.size) || [640, 480];
  const boxes = tracks.filter((t) => t.seen).map((t) => {
    const [x0, y0, x1, y1] = t.obj.bbox, pad = 4;
    const l = Math.max(0, x0 - pad), tp = Math.max(0, y0 - pad);
    return {
      key: t.key, label: t.label, sel: t.key === selKey, hov: t.key === hovKey, target: isTarget(t), low: y0 < H * 0.12,
      dist: fmtDist(t.obj.dist_mm),
      l: (l / W) * 100, t: (tp / H) * 100, w: ((Math.min(W, x1 + pad) - l) / W) * 100, h: ((Math.min(H, y1 + pad) - tp) / H) * 100,
    };
  });

  // ---- hover readout: the distance under the pointer, same spot on both pictures ----
  const [probe, setProbe] = useState(null);
  const [probeMM, setProbeMM] = useState(null);
  const probeRef = useRef(null), probeBusy = useRef(false);
  async function readProbe() {
    const at = probeRef.current;
    if (!at || probeBusy.current || !name) return;
    probeBusy.current = true;
    try { const mm = await depthAt(name, at.x, at.y); if (probeRef.current) setProbeMM(mm); }
    catch {} finally { probeBusy.current = false; }
  }
  useEffect(() => { const id = setInterval(readProbe, 300); return () => clearInterval(id); });
  function onMove(ev) {
    const fig = ev.target.closest(".view");
    if (!fig || !(cam && cam.ok)) return;
    const img = fig.querySelector("img");
    if (!img) return;
    const r = img.getBoundingClientRect(), nw = img.naturalWidth || 4, nh = img.naturalHeight || 3;
    const s = Math.min(r.width / nw, r.height / nh), w = nw * s, h = nh * s;
    const x = (ev.clientX - r.left - (r.width - w) / 2) / w, y = (ev.clientY - r.top - (r.height - h) / 2) / h;
    if (x < 0 || x > 1 || y < 0 || y > 1) { leave(); return; }
    probeRef.current = { x, y };
    setProbe({ x, y, px: (((r.width - w) / 2 + x * w) / r.width) * 100, py: (((r.height - h) / 2 + y * h) / r.height) * 100 });
    readProbe();
  }
  function leave() { probeRef.current = null; setProbe(null); }

  // ---- click a thing, then "Grab with Helper" ----
  const sel = selKey === null ? null : tracks.find((t) => t.key === selKey) || null;
  const [grabBusy, setGrabBusy] = useState(false);
  const [grabMsg, setGrabMsg] = useState(null);
  const armRole = (armName) => { const a = arms.find((x) => x.name === armName); return a ? roleFor(a, arms).role : cap(armName); };
  async function grab() {
    if (!sel || grabBusy) return;
    setGrabBusy(true); setGrabMsg({ cls: "", text: "Sending the arm…" });
    try {
      const j = await post({ action: "grab_object", id: sel.obj.id, cam: name });
      setGrabMsg({ cls: "ok", text: `${j.result.started}. Say “Hey Viam, stop” to halt it.` });
    } catch (e) {
      setGrabMsg({ cls: "no", text: "Couldn’t grab it: " + e.message });
    } finally { setGrabBusy(false); }
  }
  let grabWhy = "", canGrab = false;
  if (sel) {
    const g = sel.obj.grab;
    canGrab = !!(g && g.arm);
    grabWhy = !g ? "Grabbing isn’t set up for this camera" : g.why ? `Can’t grab: ${g.why}` : !sel.seen ? "Not in view right now"
            : busy ? "Wait — an arm is busy" : g.via === "wrist" ? `Seen by ${armRole(g.arm)}’s own camera` : `${armRole(g.arm)} is closest`;
  }

  if (!cam) return null;

  // ---- what to say: a few things the arm can grab right now, and the game ----
  const grabbable = tracks.filter((t) => t.seen && t.named && t.obj.grab && t.obj.grab.arm).map((t) => t.label.toLowerCase());
  const say = [...new Set(grabbable)].slice(0, 2).map((l, i) => (i ? `hand me the ${l}` : `grab the ${l}`));
  const game = (st.command_list || []).find((c) => /shell|game/i.test(c.label || "") || (c.phrases || []).some((p) => /shell game/i.test(p)));
  if (game) say.push("let’s play shell game");
  const prefix = (st.mic && st.mic.mode) === "toggle" ? "" : "Hey Viam, ";

  const named = tracks.filter((t) => t.named), unnamed = tracks.filter((t) => !t.named);
  const seenN = tracks.filter((t) => t.seen).length;
  const nm = cam.naming;
  const nmTxt = !nm || nm.state === "ready" ? "" : nm.state === "error" ? "Naming unavailable" : "Setting up names…";
  const empty = cam.error ? "The camera isn’t connected."
    : cam.detect_error ? `Can’t check the desk: ${cam.detect_error}`
    : !cam.ok || cam.table == null ? "Waiting for the camera…"
    : cam.table === false ? "Can’t find the desk surface. Point the camera at the desk."
    : !tracks.length ? "Nothing on the desk right now." : "";
  const waitTxt = cam.error ? `Can’t get pictures from ${cam.label || cam.name}: ${cam.error}` : cam.ok ? "" : "Starting the camera…";
  const ar = cam.size ? (cam.size[0] / cam.size[1]).toFixed(4) : "1.3333";
  const subline = cur ? `Locked on — ${st.current}` : "Depth camera, overhead · sees everything within reach";

  // a plain function, not a component: tiles keep their identity (and keyboard focus) across polls
  const tile = (t, generic) => {
    const o = t.obj;
    const phrase = o.grab && o.grab.arm && t.named ? `grab the ${t.label.toLowerCase()}` : "";
    const tip = `${t.label}. ${longSize(o)}, ${fmtCm(o.dist_mm)} from the camera.` + (phrase ? ` Say “${prefix}${phrase}”.` : "") + (t.seen ? "" : " (Not seen for a moment.)");
    const target = isTarget(t);
    return (
      <button key={t.key} type="button" className={`tile${generic ? " tile--generic" : ""}${t.seen ? "" : " gone"}${target ? " target" : ""}`}
        aria-pressed={t.key === selKey} title={tip} aria-label={tip}
        onClick={() => pick(t.key)} onPointerEnter={() => setHovKey(t.key)} onPointerLeave={() => setHovKey(null)}>
        <span className="tile__top"><span className="tile__sw" style={{ background: t.swatch || "var(--fill2)" }} />{t.label}</span>
        <span className="tile__st">{target ? "Grabbing now" : [shortSize(o), fmtDist(o.dist_mm)].filter(Boolean).join(" · ")}</span>
      </button>
    );
  };

  return (
    <section className="desk" aria-label="Your desk">
      <div className="sh">
        <div>
          <h2>Your desk</h2>
          <div className="sh__sub">{subline}</div>
        </div>
        {cams.length > 1 && (
          <div className="seg" role="group" aria-label="Choose a camera">
            {cams.map((c) => (
              <button key={c.name} type="button" aria-pressed={c.name === cam.name} onClick={() => setCamSel(c.name)}>{camLabel(c, arms)}</button>
            ))}
          </div>
        )}
      </div>

      <div className="media">
        <div className={`views views--${view}`} style={{ "--ar": ar }} onPointerMove={onMove} onPointerLeave={leave}>
          {(view === "both" || view === "color") && <View kind="color" src={colorSrc} cam={cam} boxes={boxes} probe={probe} probeMM={probeMM} waitTxt={waitTxt} onPick={pick} />}
          {(view === "both" || view === "depth") && <View kind="depth" src={depthSrc} cam={cam} boxes={boxes} probe={probe} probeMM={probeMM} waitTxt={waitTxt} onPick={pick} />}
        </div>
        <div className={`pill pill--dark livebadge${cam.error ? " bad" : streaming ? " on" : ""}`}>
          <i />{cam.error ? "Not connected" : !cam.ok ? "Starting" : cam.paused ? "Paused" : "Live"}
          {streaming && <span className="fps">{cam.fps} fps</span>}
        </div>
        {sel && (
          <div className={`grabbar${canGrab ? "" : " nogo"}`} role="region" aria-label="Selected object">
            <span className="tile__sw" style={{ background: sel.swatch || "var(--fill2)" }} />
            <span className="grabbar__txt"><b>{sel.label}</b><span>{grabWhy}</span></span>
            {canGrab && <button type="button" className="btn btn--primary" disabled={grabBusy || busy || !sel.seen} onClick={grab}>Grab with {armRole(sel.obj.grab.arm)}</button>}
          </div>
        )}
      </div>

      <div className="camrow">
        <div className="legend" aria-label="Depth color scale">
          <span>{cam.range_mm ? fmtMM(cam.range_mm[0]) : "near"}</span><i className="ramp" /><span>{cam.range_mm ? fmtMM(cam.range_mm[1]) : "far"}</span>
        </div>
        {grabMsg && <div className={`msg ${grabMsg.cls}`} role="status">{grabMsg.text}</div>}
        {cam.has_color !== false && (
          <div className="seg seg--sm" role="group" aria-label="Picture">
            {["both", "color", "depth"].map((v) => <button key={v} type="button" aria-pressed={view === v} onClick={() => setView(v)}>{cap(v)}</button>)}
          </div>
        )}
      </div>

      {say.length > 0 && (
        <div className="say">
          <div className="eyebrow">Try saying</div>
          <div className="say__chips">
            {say.map((s) => <span key={s} className="say__chip"><Mic size={16} />“{prefix}{s}”</span>)}
          </div>
        </div>
      )}

      <div className="objs">
        <div className="sh sh--tight">
          <div className="sh__inline">
            <h3>Within reach</h3>
            <span className="sh__sub">{cur ? "Going for the highlighted one" : "Just say “grab the …” and name one"}</span>
          </div>
          <span className="aux" title={nm && nm.detail ? nm.detail : ""}>{[seenN ? `${seenN} ${seenN === 1 ? "item" : "items"}` : "", nmTxt].filter(Boolean).join(" · ")}</span>
        </div>
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
                <div className="group"><i className="group__dot group__dot--warn" /><span className="group__name">Unidentified</span><span className="group__sub">{unnamed.length} · Still reachable — tap one, then Grab</span></div>
                <div className="objgrid" role="list">{unnamed.map((t) => tile(t, true))}</div>
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}
