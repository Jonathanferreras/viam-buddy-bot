import { useEffect, useRef, useState } from "react";
import { post } from "./api.js";

// Naming things on the Helper view, in place:
//   * hover a Verified thing (its box in a camera feed, or its tile) and press ×: it moves to Unidentified
//   * press "+ Add" on an Unidentified tile, type a name, Enter: it moves to Verified under that name
// The router remembers what the thing looks like (src/features/labels.py, src/taught.py), so the choice
// follows it around and survives restarts. Styles live here so the shared app.css stays untouched.
const CSS = `
.tilewrap { position: relative; display: flex; }
.tilewrap > .tile { flex: 1; min-width: 0; }
.tilewrap > .tile { padding-right: 30px; }
.tilewrap--add > .tile { padding-right: 62px; }
.tilewrap > .tile:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tile__x, .box__x { position: absolute; display: grid; place-items: center; width: 22px; height: 22px; padding: 0; border: 0;
  border-radius: 50%; background: rgba(28,28,30,.92); color: #fff; font-size: 14px; line-height: 1; cursor: pointer;
  opacity: 0; transition: opacity .12s, background .12s; z-index: 3; }
.tile__x { top: 6px; right: 6px; }
.tile__x:hover, .box__x:hover { background: var(--red); }
.tilewrap:hover .tile__x, .tile__x:focus-visible { opacity: 1; }
.box__x { transform: translate(-60%, -40%); }
.box:hover + .box__x, .box.hov + .box__x, .box__x:hover, .box__x:focus-visible { opacity: 1; }
.tile__add { position: absolute; top: 7px; right: 7px; height: 22px; padding: 0 9px; border: 0; border-radius: 999px;
  background: var(--fill2); color: var(--ink); font-size: 11px; font-weight: 700; cursor: pointer; z-index: 3; }
.tile__add:hover { background: var(--green); color: #000; }
.tileform { display: flex; flex-direction: column; gap: 6px; width: 100%; padding: 8px; border-radius: var(--r-tile);
  background: var(--surface2); box-shadow: 0 0 0 2px var(--green) inset; }
.tileform__in { width: 100%; height: 30px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--fill2);
  background: var(--fill4); color: var(--ink); font: inherit; font-size: 13px; }
.tileform__in:focus { outline: none; border-color: var(--green); }
.tileform__row { display: flex; gap: 6px; align-items: center; }
.tileform__row .btn { height: 26px; padding: 0 10px; font-size: 12px; }
.tileform__hint { flex: 1; font-size: 11px; color: var(--ink2); }
.tileform__err { font-size: 11px; color: var(--orange); }
`;
let styled = false;
export function useLabelStyles() {
  useEffect(() => {
    if (styled) return;
    styled = true;
    const el = document.createElement("style");
    el.textContent = CSS;
    document.head.appendChild(el);
  }, []);
}

// every camera's id for this thing (the wrist and desk cameras each track it separately)
function targetsOf(item, cams) {
  const out = [];
  if (item.wrist && cams.helper) out.push({ cam: cams.helper.name, id: item.wrist.obj.id });
  if (item.desk && cams.desk) out.push({ cam: cams.desk.name, id: item.desk.obj.id });
  return out;
}

export async function teach(item, cams, label, verified) {
  const targets = targetsOf(item, cams);
  if (!targets.length) throw new Error("it isn't in view right now");
  return (await post({ action: "label_object", targets, label, verified })).result;
}

// What you just did shows at once, before the cameras' next few readings catch up.
const HOLD_MS = 4000;
export function useOptimistic() {
  const ref = useRef(new Map());
  const [, force] = useState(0);
  const set = (key, patch) => { ref.current.set(key, { ...patch, until: Date.now() + HOLD_MS }); force((n) => n + 1); };
  const clear = (key) => { ref.current.delete(key); force((n) => n + 1); };
  const apply = (items) => {
    const now = Date.now();
    return items.map((t) => {
      const p = ref.current.get(t.key);
      if (!p) return t;
      if (now > p.until) { ref.current.delete(t.key); return t; }
      const label = p.label || t.label;
      const m = label.match(/^(.*) (\d+)$/);
      return { ...t, ...p, label, base: p.label ? (m ? m[1] : label) : t.base, n: p.label ? (m ? +m[2] : null) : t.n };
    });
  };
  return { set, clear, apply };
}

// The name box that takes an Unidentified tile's place while you name it.
export function AddForm({ item, cams, onDone, onCancel, optimistic }) {
  const guess = (item.obj && item.obj.kind_top && item.obj.kind_top[0] && item.obj.kind_top[0][0] !== "background")
    ? item.obj.kind_top[0][0] : "";
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const input = useRef(null);
  useEffect(() => { if (input.current) input.current.focus(); }, []);
  async function save(ev) {
    ev.preventDefault();
    const v = name.trim();
    if (!v || busy) return;
    setBusy(true); setErr("");
    try {
      const r = await teach(item, cams, v, true);
      optimistic.set(item.key, { label: r.label, named: true });
      onDone(r);
    } catch (e) { setErr(e.message); setBusy(false); }
  }
  return (
    <form className="tileform" onSubmit={save} onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onCancel(); } }}>
      <input ref={input} className="tileform__in" value={name} maxLength={40} onChange={(e) => setName(e.target.value)}
        placeholder={guess ? `Name it… (maybe “${guess}”?)` : "Name it… e.g. Stapler"} aria-label={`Name for the ${item.label}`} />
      <div className="tileform__row">
        <span className="tileform__hint">Enter to add · Esc to cancel</span>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="submit" className="btn btn--primary" disabled={!name.trim() || busy}>{busy ? "Adding…" : "Add"}</button>
      </div>
      {err && <span className="tileform__err">Couldn’t add it: {err}</span>}
    </form>
  );
}
