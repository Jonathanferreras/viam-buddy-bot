import { camsByRole } from "./roles.js";
import HelperView from "./HelperView.jsx";
import PlayerView from "./PlayerView.jsx";

// The main view: Helper (arm 17 + desk camera, things to grab) or Player (arm 18 + the game).
export default function MainView({ st, tab, setTab, busy, bump, onNotice }) {
  const cams = camsByRole(st);
  const game = st.game || {};
  const gameOn = !!game.stage, grabOn = st.phase === "running" && !!st.stage;
  const sub = tab === "player"
    ? (gameOn ? `${game.detail || "Playing"}` : `${cams.player ? "Arm 18’s own camera · " : ""}plays games with you`)
    : (grabOn && st.current ? `Locked on — ${st.current}` : "Arm 17’s camera and the desk camera see the same things — say “grab the …” and name one");
  const T = ({ id, label, live }) => (
    <button type="button" role="tab" id={`tab-${id}`} aria-selected={tab === id} aria-controls={`panel-${id}`} className={`tab tab--${id}`}
      title={`${label} (${id[0].toUpperCase()})`} onClick={() => setTab(id)}>
      <i className={live ? "live" : ""} />{label}
    </button>
  );
  return (
    <>
      <div className="tabs">
        <div className="tablist" role="tablist" aria-label="View">
          <T id="helper" label="Helper" live={grabOn} />
          <T id="player" label="Player" live={gameOn} />
        </div>
        <div className="tabs__sub">{sub}</div>
      </div>
      {tab === "player"
        ? <PlayerView st={st} cams={cams} busy={busy} bump={bump} onNotice={onNotice} />
        : <HelperView st={st} cams={cams} busy={busy} bump={bump} onNotice={onNotice} />}
    </>
  );
}
