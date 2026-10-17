import { mmss } from "./api.js";
import { Logo, Chat, Lines, Play } from "./icons.jsx";

// Recording pill: every camera being filmed right now, or the last clip saved.
function RecPill({ recs }) {
  const active = recs.filter((r) => r.recording || r.state === "saving");
  if (active.length) {
    const secs = Math.max(...active.map((r) => r.secs || 0));
    const saving = active.every((r) => r.state === "saving");
    const still = active.every((r) => r.still_for != null && r.still_for >= 1);
    const left = Math.max(...active.map((r) => (r.still_for != null ? Math.max(0, Math.ceil(r.idle_s - r.still_for)) : 0)));
    const who = active.length === 1 ? active[0].label || active[0].camera : `${active.length} cameras`;
    const sub = [who, saving ? "saving" : still ? `stops in ${left}s` : ""].filter(Boolean).join(" · ");
    return (
      <span className="pill pill--rec" title={`Recording ${active.map((r) => r.label || r.camera).join(", ")} — each ends ${active[0].idle_s}s after the arms stop moving`}>
        <i />REC {mmss(secs)} <span className="rs">· {sub}</span>
      </span>
    );
  }
  const last = recs.map((r) => ({ ...r.last, cam: r.label || r.camera })).filter((l) => l.url && /\.mp4$/.test(l.url))
    .sort((a, b) => String(b.started || "").localeCompare(String(a.started || "")))[0];
  if (!last) return null;
  const href = "/recordings/" + last.url.split("/").map(encodeURIComponent).join("/");
  return (
    <a className="pill pill--link" href={href} target="_blank" rel="noreferrer" title={`Play ${last.url} (in the project's recordings folder)`}>
      <Play size={14} />Last clip <span className="rs">{mmss(last.seconds || 0)} · {last.cam}</span>
    </a>
  );
}

export default function TopBar({ st, online, drawer, setDrawer, logUnseen }) {
  const recs = st ? st.recs || (st.rec ? [st.rec] : []) : [];
  return (
    <header className="top">
      <div className="top__brand">
        <span className="top__mark"><Logo /></span>
        <span className="top__name">Buddy Bot</span>
        <span className="top__div" />
        <span className="top__by">built on <span className="wordmark" role="img" aria-label="Viam" /></span>
      </div>
      <div className="top__right">
        <RecPill recs={recs} />
        <button type="button" className={`btn${drawer === "commands" ? " btn--on" : ""}`} aria-expanded={drawer === "commands"} title="What you can say (C)"
          onClick={() => setDrawer(drawer === "commands" ? null : "commands")}><Chat size={16} />Commands</button>
        <button type="button" className={`btn${drawer === "logs" ? " btn--on" : ""}`} aria-expanded={drawer === "logs"} title="Router logs (L)"
          onClick={() => setDrawer(drawer === "logs" ? null : "logs")}>
          <Lines size={16} />Logs{logUnseen > 0 && <span className="badge">{logUnseen > 99 ? "99+" : logUnseen}</span>}
        </button>
        <span className={`pill live${online ? " on" : " down"}`}><i />{online ? "Live" : "Offline"}</span>
      </div>
    </header>
  );
}
