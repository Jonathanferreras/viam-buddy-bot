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
// The last picture of each camera stays around after its view unmounts, so switching tabs shows
// it at once (the router only grabs frames while a page asks, so the first fresh one takes a moment).
const LAST_FRAME = new Map();   // "cam/kind" -> { url, seq }
export function useFrame(cam, kind, active, intervalMs = 120, bump = 0, detect = false) {
  const key = `${cam}/${kind}`;
  const [src, setSrc] = useState(() => (cam && active && LAST_FRAME.get(key)) ? LAST_FRAME.get(key).url : null);
  useEffect(() => {
    if (!cam || !active) { setSrc(null); return; }
    const last = LAST_FRAME.get(key);
    let seq = last ? last.seq : -1, dead = false, ctrl = null;
    setSrc(last ? last.url : null);
    const enc = encodeURIComponent(cam);
    (async () => {
      while (!dead) {
        if (document.hidden) { await sleep(500); continue; }
        const t0 = performance.now();
        try {
          ctrl = new AbortController();
          const r = await fetch(`/live/frame?cam=${enc}&kind=${kind}&have=${seq}${detect ? "&detect=1" : ""}`, { cache: "no-store", signal: ctrl.signal });
          if (r.status === 200 && !dead) {
            seq = +(r.headers.get("X-Seq") || -1);
            const u = URL.createObjectURL(await r.blob());
            const prev = LAST_FRAME.get(key);
            LAST_FRAME.set(key, { url: u, seq });
            setSrc(u);
            if (prev) setTimeout(() => URL.revokeObjectURL(prev.url), 1000);
          }
        } catch (e) {
          if (dead) break;
          await sleep(1500);
        }
        await sleep(Math.max(40, intervalMs - (performance.now() - t0)));
      }
    })();
    return () => { dead = true; if (ctrl) ctrl.abort(); };
  }, [cam, kind, active, intervalMs, bump, detect, key]);
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
const STORES = new Map();   // camera name -> its tracks, kept while the page is open
export function useTracks(objects, fps, store) {
  const own = useRef({ list: [], seq: 0 });
  let TR = own;
  if (store) {
    if (!STORES.has(store)) STORES.set(store, { current: { list: [], seq: 0 } });
    TR = STORES.get(store);
  }
  const iv = fps > 0 ? 1000 / fps : 0;
  const staleMs = Math.max(STALE_MS, 1.6 * iv), keepMs = Math.max(KEEP_MS, 3.5 * iv);
  const objs = objects || [];
  const [, force] = useState(0);
  useEffect(() => { const id = setInterval(() => force((n) => n + 1), 250); return () => clearInterval(id); }, []);
  // a kept store pauses while its view is away: the camera isn't watched then, so nothing was "unseen"
  useEffect(() => () => { TR.current.pausedAt = Date.now(); }, [TR]);
  const now = Date.now();
  // runs once per /state poll (the objects array is a fresh one each time); a repeat of the same
  // detections still counts as "seen", so a still scene never fades out
  useMemo(() => {
    const tr = TR.current;
    if (tr.pausedAt) {
      const shift = now - tr.pausedAt;
      for (const t of tr.list) { t.lastSeen += shift; t.firstSeen += shift; if (t.estAt) t.estAt += shift; }
      tr.pausedAt = null;
    }
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
               seen: now - t.lastSeen <= staleMs,
               // verified: what you set on the page (src/taught.py) wins; otherwise "has a real name"
               named: t.obj.verified != null ? !!t.obj.verified : !isGeneric(label),
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

// ---- within reach: one list of things, seen by the Helper's wrist camera and the desk camera ----
// Each camera has its own tracks; here they're paired by name ("White cup 2" in both views, or the
// only white cup each view sees) into one item per thing. Both feeds draw boxes for the same items,
// each where its own camera sees them, and anything only one camera sees is still listed. A grab
// goes through whichever view can reach it: the arm's own camera first, the desk camera's link second.
export function useReach(wristCam, deskCam) {
  const live = (c) => (c && c.ok && !c.error ? c.objects : []);
  const wrist = useTracks(live(wristCam), wristCam ? wristCam.fps : 0, wristCam && wristCam.name);
  const desk = useTracks(live(deskCam), deskCam ? deskCam.fps : 0, deskCam && deskCam.name);
  // Calm list: each thing keeps one identity (so its tile never remounts), its pairing between the two
  // cameras sticks, the order is the order things first showed up (nothing is ever re-sorted), a thing is
  // listed only once it's been there a moment, and stays (faded) a few seconds after it's gone.
  const R = useRef({ ids: new Map(), seq: 0, items: new Map(), pass: 0 });
  const mem = R.current, now = Date.now();
  const grabOf = (t, cam) => (t && cam && t.obj.grab && t.obj.grab.arm ? { cam: cam.name, obj: t.obj, info: t.obj.grab } : null);
  const merge = (key, w, d) => {
    // a view where you set the name / verified status on the page names it (even "not verified", which
    // must beat the other camera's own guess); otherwise the view that knows what it is names it
    const taught = [w, d].find((t) => t && t.obj.taught);
    const pick = taught || (w && w.named ? w : d && d.named ? d : w || d);
    const ests = [w, d].filter((t) => t && t.estAt).map((t) => t.estAt);
    return {
      key, label: pick.label, base: pick.base, n: pick.n, swatch: pick.swatch, named: pick.named,
      seen: !!((w && w.seen) || (d && d.seen)), estAt: ests.length ? Math.min(...ests) : 0, sure: Math.max(w ? w.sure : 0, d ? d.sure : 0),
      order: pick.order, wrist: w, desk: d, obj: pick.obj,
      grab: grabOf(w, wristCam) || grabOf(d, deskCam) || null,
      why: (w && w.obj.grab && w.obj.grab.why) || (d && d.obj.grab && d.obj.grab.why) || "",
    };
  };
  // pair the two views: pairs made before stay together; the rest pair by name as before
  const pairs = [], usedD = new Set(), usedW = new Set();
  for (const w of wrist) {
    const k = mem.ids.get(`w${w.key}`);
    const d = k && desk.find((t) => !usedD.has(t.key) && mem.ids.get(`d${t.key}`) === k);
    if (d) { pairs.push([w, d]); usedW.add(w.key); usedD.add(d.key); }
  }
  for (const w of wrist) {
    if (usedW.has(w.key)) continue;
    // a desk view not already paired this time; one with no wrist partner yet is preferred
    const free = desk.filter((t) => !usedD.has(t.key));
    const lone = (t) => ![...usedW].some((wk) => mem.ids.get(`w${wk}`) === mem.ids.get(`d${t.key}`));
    let d = free.find((t) => t.label === w.label && lone(t)) || free.find((t) => t.label === w.label);
    if (!d) d = free.find((t) => t.base === w.base && (!t.n || !w.n));
    if (d) usedD.add(d.key);
    pairs.push([w, d || null]);
  }
  for (const d of desk) if (!usedD.has(d.key)) pairs.push([null, d]);
  // one stable key per thing; a thing the camera lost for a moment and found again (a new track) takes back
  // its old tile when it's the same kind of thing in about the same spot
  const pass = ++mem.pass || (mem.pass = 1);
  const near = (a, b, r) => a && b && Math.hypot(a.obj.uv[0] - b.obj.uv[0], a.obj.uv[1] - b.obj.uv[1]) < r;
  const adopt = (w, d, used) => {
    const t = w || d;
    for (const [key, m] of mem.items) {
      if (used.has(key) || m.pass === pass || m.slot === null) continue;
      const it = m.item;
      if (it.base !== t.base && it.named === t.named) continue;
      if ((d && near(d, it.desk, 50)) || (w && near(w, it.wrist, 90))) return key;
    }
    return null;
  };
  const usedKeys = new Set();
  for (const [w, d] of pairs) {
    let k = (d && mem.ids.get(`d${d.key}`)) || (w && mem.ids.get(`w${w.key}`));
    if (!k || usedKeys.has(k)) k = adopt(w, d, usedKeys) || `i${++mem.seq}`;
    usedKeys.add(k);
    if (d) mem.ids.set(`d${d.key}`, k);
    if (w) mem.ids.set(`w${w.key}`, k);
    const prev = mem.items.get(k);
    mem.items.set(k, { item: merge(k, w, d), first: prev ? prev.first : now, slot: prev ? prev.slot : null, last: now, pass });
  }
  const SHOW_AFTER = 900, LINGER = 3500;
  const out = [];
  for (const [k, m] of mem.items) {
    if (now - m.last > LINGER) { mem.items.delete(k); continue; }
    if (m.slot === null) {
      if (now - m.first < SHOW_AFTER) continue;          // brand new: wait a moment before listing it
      m.slot = ++mem.seq;                                // its place in the list, for good
    }
    out.push(now - m.last > 600 ? { ...m.item, seen: false, slot: m.slot } : { ...m.item, slot: m.slot });
  }
  if (mem.ids.size > 400) for (const id of [...mem.ids.keys()].slice(0, 200)) mem.ids.delete(id);
  return out.sort((a, b) => a.slot - b.slot);
}
