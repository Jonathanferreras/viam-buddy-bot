import { useEffect, useState } from "react";
import { post, cap } from "./api.js";
import { useFrame } from "./hooks.js";
import Feed, { LiveBadge, waitText } from "./Feed.jsx";
import GamesPanel, { GameSteps } from "./GamesPanel.jsx";

// The Player tab: arm 18's own camera (tap a cup on it while you guess) beside the game, and the
// game's steps underneath while one is on.
export default function PlayerView({ st, cams, busy, bump, onNotice }) {
  const cam = cams.player;
  const src = useFrame(cam ? cam.name : null, "color", !!cam, 150, bump);
  const game = st.game || {};

  const [cupPick, setCupPick] = useState(null);
  useEffect(() => { if (game.stage !== "guess" && game.stage !== "reveal") setCupPick(null); }, [game.stage]);
  async function tapCup(slot) {
    if (game.stage !== "guess" || cupPick) return;
    setCupPick(slot);
    try { await post({ action: "shell_guess", slot }); }
    catch (e) { setCupPick(null); onNotice && onNotice("Couldn’t send your guess: " + e.message); }
  }
  const cups = cam && game.cups_view ? Object.entries(game.cups_view)
    .filter(([, c]) => c.u > -0.05 && c.u < 1.05 && c.v > -0.05 && c.v < 1.05)
    .map(([slot, c]) => ({ slot, ...c, label: cap(slot), onTap: tapCup,
      live: game.stage === "guess" && !cupPick, picked: slot === (cupPick || game.guess) })) : null;

  const active = !!game.stage, failed = game.stage === "error" || game.stage === "stopped";
  return (
    <div className="panel panel--player" role="tabpanel" id="panel-player" aria-labelledby="tab-player">
      <div className="player">
        {cam ? (
          <Feed cam={cam} src={src} caption={cam.label || `${cap(cam.arm)} camera`} cups={cups} waitTxt={waitText(cam)}>
            <LiveBadge cam={cam} />
          </Feed>
        ) : (
          <div className="feed feed--none"><div className="feed__wait">No camera is set up for the Player.</div></div>
        )}
        <div className="player__side">
          <GamesPanel st={st} busy={busy} steps={false} />
        </div>
      </div>
      {active && !failed && game.stage !== "putdown" && <GameSteps stage={game.stage} />}
    </div>
  );
}
