import { useEffect, useRef, useState } from "react";
import { post, isGeneric, tagOf } from "./api.js";
import { useRouterState, useFlow, useKey, useLocal } from "./hooks.js";
import { camsByRole, sayPrefix } from "./roles.js";
import TopBar from "./TopBar.jsx";
import VoicePanel from "./VoicePanel.jsx";
import StatusPanel from "./StatusPanel.jsx";
import MainView from "./MainView.jsx";
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

// A few things to try saying right now: what the Helper can grab, and the game.
function suggestions(st) {
  const { helper, desk } = camsByRole(st);
  const seen = [];
  for (const c of [helper, desk]) for (const o of (c && c.objects) || []) {
    const l = tagOf(o).toLowerCase();
    if (o.grab && o.grab.arm && !isGeneric(l) && !seen.includes(l)) seen.push(l);
  }
  const say = seen.slice(0, 1).map((l) => `grab the ${l}`);
  const game = st.game && st.game.games && st.game.games[0];
  if (game) say.push(game.say.replace(/'/g, "’"));
  else if (seen[1]) say.push(`hand me the ${seen[1]}`);
  return say.map((s) => `${sayPrefix(st)}${s}`);
}

export default function App() {
  const { state, online, bump } = useRouterState();
  const levels = useLevels();
  const [drawer, setDrawer] = useState(null); // "commands" | "logs" | null
  const [notice, setNotice] = useState("");
  const { rows: logRows, unseen: logUnseen } = useLogs(drawer === "logs");

  // ---- mute switch: tap the orb or press M (wake mode mutes the wake word; toggle mode is tap-to-talk) ----
  const [micPending, setMicPending] = useState(null); // optimistic value while the click is on its way
  const raw = state || { phase: "starting" };
  const st = micPending !== null && ["waiting", "muted", "hearing"].includes(raw.phase) ? { ...raw, phase: micPending ? "waiting" : "muted" } : raw;
  const micOn = micPending !== null ? micPending : !!(st.mic && st.mic.on);
  const toggleMode = ["toggle", "wake"].includes(st.mic && st.mic.mode);
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

  // ---- Helper / Player tab: remembered, and it follows the action unless you just picked one ----
  const [tabSaved, setTabSaved] = useLocal("bb:tab", "helper");
  const [tab, setTabState] = useState(tabSaved);
  const pickedAt = useRef(0);
  const setTab = (t) => { pickedAt.current = Date.now(); setTabState(t); setTabSaved(t); };
  const gameOn = !!(st.game && st.game.stage), grabOn = st.phase === "running" && !!st.stage;
  useEffect(() => {
    if (Date.now() - pickedAt.current < 20000) return;
    if (gameOn) setTabState("player"); else if (grabOn) setTabState("helper");
  }, [gameOn, grabOn]);

  useKey("m", toggleMic, [toggleMode, micOn]);
  useKey("c", () => setDrawer((d) => (d === "commands" ? null : "commands")));
  useKey("l", () => setDrawer((d) => (d === "logs" ? null : "logs")));
  useKey("h", () => setTab("helper"));
  useKey("p", () => setTab("player"));
  useKey("Escape", (ev) => {
    if (ev.defaultPrevented) return;
    if (drawer) { setDrawer(null); return; }
    post({ action: "stop" }).catch((e) => flash("Couldn’t stop: " + e.message)); // Esc halts the arms
  }, [drawer]);

  const conn = !online ? "Can’t reach the voice router — is viam-server running?" : notice;
  const say = state && !busy && ["waiting", "hearing"].includes(st.phase) ? suggestions(st) : [];

  return (
    <div className="app">
      <TopBar st={state} online={online} drawer={drawer} setDrawer={setDrawer} logUnseen={logUnseen} />
      {conn && <div className="conn" role="status">{conn}</div>}
      <main className="main">
        <aside className="col col--left">
          <VoicePanel st={st} flow={flow} levels={levels} micOn={micOn} onToggleMic={toggleMic} say={say} />
          <StatusPanel st={st} busy={busy} onNotice={flash} />
        </aside>
        <div className="col col--right">
          <MainView st={st} tab={tab} setTab={setTab} busy={busy} bump={bump} onNotice={flash} />
        </div>
      </main>
      {drawer === "commands" && <CommandsDrawer st={st} onClose={() => setDrawer(null)} />}
      {drawer === "logs" && <LogsDrawer rows={logRows} onClose={() => setDrawer(null)} />}
    </div>
  );
}
