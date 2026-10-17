import { Fragment, useEffect, useRef, useState } from "react";
import { depthAt, fmtMM } from "./api.js";

// Where the picture actually sits inside the figure. The frame is letterboxed (object-fit: contain)
// so the figure can be any shape; the overlays are placed over the picture itself, in its own
// fractions, so boxes and rings land on the things they mark whatever the window size.
function useFit(figRef, ratio) {
  const [fit, setFit] = useState(null);
  useEffect(() => {
    const el = figRef.current;
    if (!el) return;
    const measure = () => {
      const W = el.clientWidth, H = el.clientHeight;
      if (!W || !H) return;
      const s = Math.min(W / ratio, H), w = s * ratio, h = s;
      setFit({ l: (W - w) / 2, t: (H - h) / 2, w, h });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [figRef, ratio]);
  return fit;
}

// One live picture with whatever sits on top of it: the work area, detection boxes, cup rings,
// a caption, and (for depth cameras) the distance under the pointer.
export default function Feed({ cam, src, kind = "color", caption, boxes = [], onPick, cups, probe: wantProbe, children, waitTxt }) {
  const figRef = useRef(null);
  const [W, H] = (cam && cam.size) || [640, 480];
  const fit = useFit(figRef, W / H);
  const a = cam && cam.work_area; // [x0, y0, x1, y1] fractions of the picture, or null for the whole picture

  // ---- hover readout: the distance under the pointer ----
  const [probe, setProbe] = useState(null);
  const [probeMM, setProbeMM] = useState(null);
  const at = useRef(null), probeBusy = useRef(false);
  async function readProbe() {
    const p = at.current;
    if (!p || probeBusy.current || !cam) return;
    probeBusy.current = true;
    try { const mm = await depthAt(cam.name, p.x, p.y); if (at.current) setProbeMM(mm); }
    catch {} finally { probeBusy.current = false; }
  }
  useEffect(() => { if (!wantProbe) return; const id = setInterval(readProbe, 300); return () => clearInterval(id); });
  function onMove(ev) {
    if (!wantProbe || !fit || !(cam && cam.ok)) return;
    const r = figRef.current.getBoundingClientRect();
    const x = (ev.clientX - r.left - fit.l) / fit.w, y = (ev.clientY - r.top - fit.t) / fit.h;
    if (x < 0 || x > 1 || y < 0 || y > 1) { leave(); return; }
    at.current = { x, y };
    setProbe({ x, y });
    readProbe();
  }
  function leave() { at.current = null; setProbe(null); }

  const ovStyle = fit ? { left: fit.l, top: fit.t, width: fit.w, height: fit.h } : { inset: 0 };
  return (
    <figure ref={figRef} className={`feed feed--${kind}`} onPointerMove={onMove} onPointerLeave={leave}>
      {src ? <img src={src} alt={caption ? `Live view from the ${caption}` : "Live camera view"} /> : <div className="feed__blank" />}
      <div className="feed__ov" style={ovStyle}>
        {a && src && (
          <div className="area" aria-hidden="true" style={{ left: `${a[0] * 100}%`, top: `${a[1] * 100}%`, width: `${(a[2] - a[0]) * 100}%`, height: `${(a[3] - a[1]) * 100}%` }}>
            <span>Within reach</span>
          </div>
        )}
        {src && boxes.map((b) => (
          <Fragment key={b.key}>
            <button type="button" className={`box${b.sel ? " sel" : ""}${b.hov ? " hov" : ""}${b.target ? " target" : ""}${b.low ? " low" : ""}`}
              style={{ left: `${b.l}%`, top: `${b.t}%`, width: `${b.w}%`, height: `${b.h}%` }} onClick={() => onPick && onPick(b.key)}
              aria-label={b.label} aria-pressed={b.sel}>
              <b>{b.label}{b.target && b.dist ? ` · ${b.dist}` : ""}</b>
            </button>
            {b.x && (   /* optional: a view can offer an × on a box (the Helper: "not verified") */
              <button type="button" className="box__x" style={{ left: `${b.l + b.w}%`, top: `${b.t}%` }}
                onClick={(e) => { e.stopPropagation(); b.x(); }} aria-label={`${b.label} isn't verified: move it to Unidentified`}
                title="Not verified — move to Unidentified">×</button>
            )}
          </Fragment>
        ))}
        {cups && src && cups.map((c) => (
          <button key={c.slot} type="button" className={`cuptap${c.picked ? " cuptap--picked" : ""}${c.live ? " cuptap--live" : ""}`}
            style={{ left: `${c.u * 100}%`, top: `${c.v * 100}%`, width: `${c.r * 200}%` }} disabled={!c.live}
            onClick={() => c.live && c.onTap(c.slot)} aria-label={`Guess the ${c.slot} cup`}>
            <b>{c.label}</b>
          </button>
        ))}
        {probe && <span className={`probe${probe.x > 0.7 ? " flip" : ""}`} style={{ left: `${probe.x * 100}%`, top: `${probe.y * 100}%` }}><b>{fmtMM(probeMM)}</b></span>}
      </div>
      {caption && <figcaption className="feed__cap">{caption}</figcaption>}
      <div className="feed__tr">{children}</div>
      {waitTxt && <div className="feed__wait">{waitTxt}</div>}
    </figure>
  );
}

// The little Live / Starting / Not connected badge in a feed's corner.
export function LiveBadge({ cam }) {
  const streaming = !!(cam && cam.ok && !cam.paused && !cam.error);
  return (
    <span className={`pill pill--dark livebadge${!cam || cam.error ? " bad" : streaming ? " on" : ""}`}>
      <i />{!cam ? "No camera" : cam.error ? "Not connected" : !cam.ok ? "Starting" : cam.paused ? "Paused" : "Live"}
      {streaming && <span className="fps">{cam.fps} fps</span>}
    </span>
  );
}

export const waitText = (cam) => !cam ? "" : cam.error ? `Can’t get pictures from ${cam.label || cam.name}: ${cam.error}` : cam.ok ? "" : "Starting the camera…";
