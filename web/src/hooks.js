import { useEffect, useMemo, useRef, useState } from "react";
import { getState, sleep, isGeneric, tagOf } from "./api.js";

// ---- /state, polled four times a second ----
export function useRouterState() {
  const [state, setState] = useState(null);
  const [online, setOnline] = useState(false);
  const [bump, setBump] = useState(0); // goes up whenever the router comes back: frame loops restart
  useEffect(() => {
    let dead = false, down = false;
    const tick = async () => {
      try {
        const st = await getState();
        if (dead) return;
        setState(st);
        setOnline(true);
        if (down) { down = false; setBump((b) => b + 1); }
      } catch {
        if (dead) return;
        setOnline(false);
        down = true;
      }
    };
    tick();
    const id = setInterval(tick, 250);
    return () => { dead = true; clearInterval(id); };
  }, []);
  return { state, online, bump };
}

// ---- a value remembered per browser (never essential: the page works without it) ----
export function useLocal(key, initial) {
  const [v, setV] = useState(() => {
    try { const s = localStorage.getItem(key); return s == null ? initial : JSON.parse(s); } catch { return initial; }
  });
  const set = (nv) => {
    setV(nv);
    try { localStorage.setItem(key, JSON.stringify(nv)); } catch {}
  };
  return [v, set];
}

// ---- one camera picture, fetched a frame at a time (see status_page._live_frame) ----
// detect=true asks the router for this camera in full (depth + detections); without it a color-only
// preview is enough, which keeps the network free for the arms.
export function useFrame(cam, kind, active, intervalMs = 120, bump = 0, detect = false) {
  const [src, setSrc] = useState(null);
  useEffect(() => {
    if (!cam || !active) { setSrc(null); return; }
    let seq = -1, url = null, dead = false;
    const enc = encodeURIComponent(cam);
    (async () => {
      while (!dead) {
        if (document.hidden) { await sleep(500); continue; }
        const t0 = performance.now();
        try {
          const r = await fetch(`/live/frame?cam=${enc}&kind=${kind}&have=${seq}${detect ? "&detect=1" : ""}`, { cache: "no-store" });
          if (r.status === 200 && !dead) {
            seq = +(r.headers.get("X-Seq") || -1);
            const u = URL.createObjectURL(await r.blob());
            const prev = url;
            url = u;
            setSrc(u);
            if (prev) setTimeout(() => URL.revokeObjectURL(prev), 1000);
          }
        } catch {
          await sleep(1500);
        }
        await sleep(Math.max(40, intervalMs - (performance.now() - t0)));
      }
      if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
    })();
    return () => { dead = true; };
  }, [cam, kind, active, intervalMs, bump, detect]);
  return src;
}

// ---- Hey Viam -> Command -> Action tracker ----
// The router bumps counters when the wake word fires and when a command is understood; we light the
// steps on each bump and hold them so they're visible even if everything happens within a second.
const MIN_GAP = 700, RESET_AFTER = 7000;
export function useFlow(state) {
  const ref = useRef({ seen: null, wakeAt: 0, cmdAt: 0, cmd: null, lastEvent: 0, flashAt: 0 });
  const [, force] = useState(0);
  useEffect(() => { const id = setInterval(() => force((n) => n + 1), 300); return () => clearInterval(id); }, []);
  const f = ref.current, ev = (state && state.events) || {}, now = Date.now(), phase = state ? state.phase : "";
  if (!state) return { wakeOn: false, showCmd: false, act: false, outcome: null, cmd: null, typed: false, flash: false };
  if (f.seen === null) f.seen = { wake: ev.wake_seq, cmd: ev.cmd_seq }; // first real state: no replay of old events
  if (ev.wake_seq !== f.seen.wake) {
    f.seen.wake = ev.wake_seq; f.wakeAt = now; f.cmd = null; f.cmdAt = 0; f.lastEvent = now; f.flashAt = now;
  }
  if (ev.cmd_seq !== f.seen.cmd) {
    f.seen.cmd = ev.cmd_seq; f.lastEvent = now;
    f.cmd = { text: ev.cmd_text, matched: ev.cmd_matched, label: ev.cmd_label, typed: ev.cmd_typed };
    if (ev.cmd_typed) f.wakeAt = 0;
    f.cmdAt = Math.max(now, f.wakeAt ? f.wakeAt + MIN_GAP : now);
  }
  const busy = ["hearing", "processing", "running"].includes(phase);
  if (!busy && f.lastEvent && now - f.lastEvent > RESET_AFTER) { f.wakeAt = 0; f.cmd = null; f.cmdAt = 0; f.lastEvent = 0; }

  const wakeOn = !!f.wakeAt || phase === "hearing";
  const showCmd = !!(f.cmd && now >= f.cmdAt);
  const act = showCmd && f.cmd.matched;
  if (act && ["running", "error", "stopped", "done", "notfound"].includes(phase)) f.cmd.outcome = phase;
  const outcome = act ? f.cmd.outcome || "running" : null;
  return {
    wakeOn, showCmd, act, outcome,
    cmd: showCmd ? f.cmd : null,
    typed: !!(f.cmd && f.cmd.typed),
    flash: now - f.flashAt < 900,
  };
}

// ---- things on the table: detections -> stable tracks ----
// The detector runs ~5x a second and is noisy: the same cup can come back with a new id, flip between
// "White object" and "Gray object", or drop out for a frame. Each detection is matched to the nearest
// existing track; a track only appears once it's been seen twice, shows its most common recent label,
// and fades for a moment before it's removed.
//
// Ordering is meant to be calm. A track that has been seen steadily under one name for a few seconds
// becomes ESTABLISHED: from then on its label only changes if the detector is unanimous about a new one,
// and its place in the grid is fixed by the moment it was established, so the things Buddy is surest
// about sit top-left in a stable order. Tracks still settling follow, sorted by how sure we are, and
// those are the only tiles that move around.
const SHOW_AFTER_HITS = 2, STALE_MS = 700, KEEP_MS = 1800, MATCH_PX = 40, LABEL_WINDOW = 8;
const ESTABLISH_HITS = 16, ESTABLISH_AGREE = 0.75;   // ~4 s of polls, ≥ 3/4 of recent names agreeing
export function mode(arr) {
  const n = new Map(); let best = arr[arr.length - 1], bn = 0;
  for (const v of arr) { const c = (n.get(v) || 0) + 1; n.set(v, c); if (c >= bn) { bn = c; best = v; } }
  return best;
}
export function useTracks(objects, fps) {
  const TR = useRef({ list: [], seq: 0 });
  const iv = fps > 0 ? 1000 / fps : 0;
  const staleMs = Math.max(STALE_MS, 1.6 * iv), keepMs = Math.max(KEEP_MS, 3.5 * iv);
  const objs = objects || [];
  const [, force] = useState(0);
  useEffect(() => { const id = setInterval(() => force((n) => n + 1), 250); return () => clearInterval(id); }, []);
  const now = Date.now();
  // runs once per /state poll (the objects array is a fresh one each time); a repeat of the same
  // detections still counts as "seen", so a still scene never fades out
  useMemo(() => {
    const tr = TR.current;
    const free = new Set(tr.list);
    const pairs = [];
    for (const o of objs) for (const t of free) {
      const d = Math.hypot(t.obj.uv[0] - o.uv[0], t.obj.uv[1] - o.uv[1]) + (t.obj.color === o.color ? 0 : 10);
      if (d <= MATCH_PX) pairs.push([d, o, t]);
    }
    pairs.sort((a, b) => a[0] - b[0]);
    const used = new Set();
    for (const [, o, t] of pairs) {
      if (used.has(o) || !free.has(t)) continue;
      used.add(o); free.delete(t);
      t.obj = o; t.lastSeen = now; t.hits++;
      t.labels.push([tagOf(o), o.swatch || ""]); if (t.labels.length > LABEL_WINDOW) t.labels.shift();
    }
    for (const o of objs) if (!used.has(o))
      tr.list.push({ key: ++tr.seq, obj: o, firstSeen: now, lastSeen: now, hits: 1, labels: [[tagOf(o), o.swatch || ""]], order: o.uv[0] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [objects]);
  const tr = TR.current;
  tr.list = tr.list.filter((t) => now - t.lastSeen <= keepMs);
  const shown = tr.list
    .filter((t) => t.hits >= SHOW_AFTER_HITS)
    .map((t) => {
      const joined = t.labels.map((x) => x.join("\u0000"));
      const best = mode(joined);
      const agree = joined.filter((x) => x === best).length / joined.length;
      if (!t.estAt && t.hits >= ESTABLISH_HITS && agree >= ESTABLISH_AGREE && t.labels.length >= LABEL_WINDOW) {
        t.estAt = now; t.shown = best;             // settled: this name and this slot stick
      }
      if (t.estAt) {
        if (agree === 1 && best !== t.shown) t.shown = best;   // only a unanimous new name changes it
      } else {
        t.shown = best;
      }
      const [label, swatch] = t.shown.split("\u0000");
      const m = label.match(/^(.*) (\d+)$/); // "White cup 2" -> base "White cup", n 2
      return { key: t.key, obj: t.obj, label, base: m ? m[1] : label, n: m ? +m[2] : null, swatch, order: t.order,
               seen: now - t.lastSeen <= staleMs, named: !isGeneric(label),
               estAt: t.estAt || 0,
               // confidence in tenths, so two unsettled tiles don't trade places over a rounding difference
               sure: Math.round((Math.min(t.hits, ESTABLISH_HITS) / ESTABLISH_HITS * 0.6 + agree * 0.4) * 10) };
    })
    .sort((a, b) => {
      if (!!a.estAt !== !!b.estAt) return a.estAt ? -1 : 1;                 // established things first…
      if (a.estAt) return a.estAt - b.estAt || a.order - b.order || a.key - b.key; // …in the order they settled (ties: left to right), never reshuffled
      return b.sure - a.sure || a.order - b.order || a.key - b.key;          // the rest: surest first, then left to right
    });
  return shown;
}

// ---- keyboard shortcut that ignores typing in fields and modifier combos ----
export function useKey(key, fn, deps = []) {
  useEffect(() => {
    const h = (ev) => {
      if (ev.metaKey || ev.ctrlKey || ev.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
      if (ev.key.toLowerCase() === key.toLowerCase()) fn(ev);
    };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
