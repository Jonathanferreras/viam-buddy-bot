// Thin wrappers over the voice router's local status server (src/status_page.py).

export async function getState() {
  const r = await fetch("/state", { cache: "no-store" });
  return r.json();
}

export async function post(body) {
  const r = await fetch("/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "request failed");
  return j;
}

export async function getLogs(after) {
  const r = await fetch(`/logs?after=${after}`, { cache: "no-store" });
  return r.json();
}

export async function depthAt(cam, x, y) {
  const r = await fetch(
    `/live/depth_at?cam=${encodeURIComponent(cam)}&x=${x.toFixed(4)}&y=${y.toFixed(4)}`,
    { cache: "no-store" }
  );
  const j = await r.json();
  return j.mm;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- formatting helpers shared by several components ----
export const fmtMM = (mm) =>
  mm == null ? "no reading" : mm < 1000 ? `${Math.round(mm / 10)} cm` : `${(mm / 1000).toFixed(2)} m`;
export const fmtDist = (mm) =>
  mm == null ? "" : mm < 1000 ? `${Math.round(mm / 10)} cm` : `${(mm / 1000).toFixed(1)} m`;
export const fmtCm = (mm) =>
  mm == null ? "?" : mm < 1000 ? `${Math.max(1, Math.round(mm / 10))} cm` : `${(mm / 1000).toFixed(1)} m`;
export function shortSize(o) {
  const [l, w, h] = (o.size_mm || [0, 0, 0]).map((x) => Math.max(1, Math.round(x / 10)));
  return o.tall ? `${w}×${h} cm` : `${l}×${w} cm`;
}
export function longSize(o) {
  const [l, w, h] = (o.size_mm || [0, 0, 0]).map((x) => Math.max(1, Math.round(x / 10)));
  return o.tall ? `About ${w} cm wide, ${h} cm tall` : `About ${l} × ${w} cm`;
}
export const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
export const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");
export const isGeneric = (label) => /\bobject( \d+)?$/i.test(label || "");
// The name to show and say for a detection: numbered when the camera sees several of a kind ("White cup 2").
export const tagOf = (o) => o.tag || o.label || "";
