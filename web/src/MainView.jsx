import { camsByRole } from "./roles.js";
import HelperView from "./HelperView.jsx";
import PlayerView from "./PlayerView.jsx";

// One tab. Defined at module level: a component created inside render is a new type each time the
// state polls, which remounts the button four times a second and swallows clicks.
function Tab({ id, label, live, tab, setTab }) {
  return (
    <button type="button" role="tab" id={`tab-${id}`} aria-selected={tab === id} aria-controls={`panel-${id}`} className={`tab tab--${id}`}
      title={`${label} (${id[0].toUpperCase()})`} onClick={() => setTab(id)}>
      <i className={live ? "live" : ""} />{label}
    </button>
  );
}

// The main view: Helper (arm 17 + desk camera, things to grab) or Player (arm 18 + the game).
export default function MainView({ st, tab, setTab, busy, bump, onNotice }) {
  const cams = camsByRole(st);
  const game = st.game || {};
  const gameOn = !!game.stage, grabOn = st.phase === "running" && !!st.stage;
  const sub = tab === "player"
    ? (gameOn ? `${game.detail || "Playing"}` : `${cams.player ? "Arm 18’s own camera · " : ""}plays games with you`)
    : (grabOn && st.current ? `Locked on — ${st.current}` : "Arm 17’s camera and the desk camera see the same things — say “grab the …” and name one");
  return (
    <>
      <div className="tabs">
        <div className="tablist" role="tablist" aria-label="View">
          <Tab id="helper" label="Helper" live={grabOn} tab={tab} setTab={setTab} />
          <Tab id="player" label="Player" live={gameOn} tab={tab} setTab={setTab} />
        </div>
        <div className="tabs__sub">{sub}</div>
      </div>
      {tab === "player"
        ? <PlayerView st={st} cams={cams} busy={busy} bump={bump} onNotice={onNotice} />
        : <HelperView st={st} cams={cams} busy={busy} bump={bump} onNotice={onNotice} />}
    </>
  );
}
