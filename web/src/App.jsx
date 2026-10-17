import { useEffect, useRef, useState } from "react";
import { post } from "./api.js";
import { useRouterState, useFlow, useKey } from "./hooks.js";
import TopBar from "./TopBar.jsx";
import VoicePanel from "./VoicePanel.jsx";
import Buddies from "./Buddies.jsx";
import DeskView from "./DeskView.jsx";
import { CommandsDrawer, LogsDrawer, useLogs } from "./Drawers.jsx";

// Live mic loudness from the router (server-sent events), read by the orb's animation loop.
function useLevels() {
  const ref = useRef({ target: 0, live: false });
  useEffect(() => {
    const es = new EventSource("/levels"); // reconnects on its own if the router restarts
    es.onmessage = (e) => {
      if (e.data === "off") { ref.current.live = false; ref.current.target = 0; }
      else { ref.current.live = true; ref.current.target = Math.max(0, Math.min(1, parseFloat(e.data) || 0)); }
    };
    es.onerror = () => { ref.current.live = false; ref.current.target = 0; };
    return () => es.close();
  }, []);
  return ref;
}

export default function App() {
  const { state, online, bump } = useRouterState();
  const levels = useLevels();
  const [drawer, setDrawer] = useState(null); // "commands" | "logs" | null
  const [notice, setNotice] = useState("");
  const { rows: logRows, unseen: logUnseen } = useLogs(drawer === "logs");

  // ---- mic switch (toggle mode): tap the orb or press M ----
  const [micPending, setMicPending] = useState(null); // optimistic value while the click is on its way
  const raw = state || { phase: "starting" };
  const st = micPending !== null && ["waiting", "muted"].includes(raw.phase) ? { ...raw, phase: micPending ? "waiting" : "muted" } : raw;
  const micOn = micPending !== null ? micPending : !!(st.mic && st.mic.on);
  const toggleMode = (st.mic && st.mic.mode) === "toggle";
  async function toggleMic() {
    if (!toggleMode) return;
    const want = !micOn;
    setMicPending(want);
    try { await post({ action: "mic", on: want }); }
    catch (e) { flash("Couldn’t switch the microphone: " + e.message); }
    finally { setTimeout(() => setMicPending(null), 400); }
  }
  function flash(msg) { setNotice(msg); setTimeout(() => setNotice((n) => (n === msg ? "" : n)), 5000); }

  const flow = useFlow(state);
  const busy = st.phase === "running";

  useKey("m", toggleMic, [toggleMode, micOn]);
  useKey("c", () => setDrawer((d) => (d === "commands" ? null : "commands")));
  useKey("l", () => setDrawer((d) => (d === "logs" ? null : "logs")));
  useKey("Escape", (ev) => {
    if (ev.defaultPrevented) return;
    if (drawer) { setDrawer(null); return; }
    post({ action: "stop" }).catch((e) => flash("Couldn’t stop: " + e.message)); // Esc halts the arms
  }, [drawer]);

  const conn = !online ? "Can’t reach the voice router — is viam-server running?" : notice;

  return (
    <div className="app">
      <TopBar st={state} online={online} drawer={drawer} setDrawer={setDrawer} logUnseen={logUnseen} />
      {conn && <div className="conn" role="status">{conn}</div>}
      <main className="main">
        <aside className="col col--left">
          <VoicePanel st={st} flow={flow} levels={levels} micOn={micOn} onToggleMic={toggleMic} />
          <Buddies st={st} busy={busy} bump={bump} onNotice={flash} />
        </aside>
        <div className="col col--right">
          <DeskView st={st} busy={busy} bump={bump} onNotice={flash} />
        </div>
      </main>
      {drawer === "commands" && <CommandsDrawer st={st} onClose={() => setDrawer(null)} />}
      {drawer === "logs" && <LogsDrawer rows={logRows} onClose={() => setDrawer(null)} />}
    </div>
  );
}
