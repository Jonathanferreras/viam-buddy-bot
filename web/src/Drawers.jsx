import { useEffect, useRef, useState } from "react";
import { getLogs, cap, isGeneric } from "./api.js";
import { useLocal } from "./hooks.js";
import { Close } from "./icons.jsx";
import { roleFor, wakeWord } from "./roles.js";

const EXAMPLES = ["pick up the white cup", "get the red ball", "grab the yellow one", "hand me the cup", "drop it", "put it down",
                  "go back to the start", "reset arm 17", "wave arm 18", "let’s play shell game", "stop"];

// ---- what you can say ----
export function CommandsDrawer({ st, onClose }) {
  const arms = st.arms || [];
  const toggle = (st.mic && st.mic.mode) === "toggle";
  const cam = (st.cams || []).find((c) => c.arm && (c.objects || []).some((o) => o.grab && o.grab.arm));
  const seen = cam ? [...new Set(cam.objects.filter((o) => o.grab && o.grab.arm && !isGeneric(o.label)).map((o) => o.label.toLowerCase()))].slice(0, 10) : [];
  const who = cam ? (arms.find((a) => a.name === cam.arm) ? roleFor(arms.find((a) => a.name === cam.arm), arms).role : cap(cam.arm)) : "";
  return (
    <aside className="drawer" aria-label="What you can say">
      <div className="drawer__hd">
        <h2>What you can say</h2>
        <button type="button" className="drawer__x" aria-label="Close the commands" onClick={onClose}><Close size={18} /></button>
      </div>
      <div className="drawer__body">
        <p className="lead">{toggle ? "Tap the mic, then say it." : `Say “${wakeWord(st)}”, wait for the chime, then the command (you have ${(st.mic && st.mic.window_s) || 5} seconds).`} Exact words aren’t needed: say what you want, like the examples below.</p>
        {seen.length > 0 && (
          <>
            <h3>{who} can see</h3>
            <div className="chips">{seen.map((l) => <span key={l} className="chip chip--seen">grab the {l}</span>)}</div>
          </>
        )}
        <h3>Also understood</h3>
        <div className="chips">{EXAMPLES.map((e) => <span key={e} className="chip">{e}</span>)}</div>
        {(st.command_list || []).length > 0 && (
          <>
            <h3>Set phrases</h3>
            <ul className="cmdlist">
              {st.command_list.map((c) => (
                <li key={c.label}><b>{c.label}</b><div className="chips">{(c.phrases || []).map((p) => <span key={p} className="chip">{p}</span>)}</div></li>
              ))}
            </ul>
          </>
        )}
      </div>
    </aside>
  );
}

// ---- the router's recent log lines ----
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
const isProblem = (r) => r.level === "warning" || r.level === "error" || r.level === "critical";

export function useLogs(open) {
  const L = useRef({ seq: 0, rows: [] });
  const [rows, setRows] = useState([]);
  const [unseen, setUnseen] = useState(0);
  useEffect(() => {
    let dead = false;
    const poll = async () => {
      try {
        const j = await getLogs(L.current.seq);
        if (dead) return;
        if (j.seq < L.current.seq) { L.current = { seq: 0, rows: [] }; setRows([]); return; }
        if (j.logs.length) {
          L.current.rows = [...L.current.rows, ...j.logs].slice(-500);
          setRows(L.current.rows);
          if (!open && L.current.seq) setUnseen((n) => n + j.logs.filter((x) => x.level === "error" || x.level === "critical").length);
        }
        L.current.seq = j.seq;
      } catch {}
    };
    poll();
    const id = setInterval(poll, 1000);
    return () => { dead = true; clearInterval(id); };
  }, [open]);
  useEffect(() => { if (open) setUnseen(0); }, [open]);
  return { rows, unseen };
}

export function LogsDrawer({ rows, onClose }) {
  const [problems, setProblems] = useLocal("bb:logsProblems", false);
  const list = useRef(null);
  const shown = rows.filter((r) => !problems || isProblem(r));
  useEffect(() => {
    const el = list.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 80) el.scrollTop = el.scrollHeight;
  }, [shown.length]);
  return (
    <aside className="drawer" aria-label="Logs">
      <div className="drawer__hd">
        <h2>Logs</h2>
        <label className="drawer__f"><input type="checkbox" checked={problems} onChange={(e) => setProblems(e.target.checked)} /> Problems only</label>
        <button type="button" className="drawer__x" aria-label="Close the logs" onClick={onClose}><Close size={18} /></button>
      </div>
      <ol ref={list} className="loglist" aria-live="off">
        {shown.length ? shown.map((r, i) => (
          <li key={i} className={r.level}><span className="lt">{hhmm(r.t)}</span><span className="lv">{r.level === "warning" ? "warn" : r.level}</span><span className="lm">{r.msg}</span></li>
        )) : <li className="empty">{problems ? "No warnings or errors." : "Nothing logged yet."}</li>}
      </ol>
    </aside>
  );
}
