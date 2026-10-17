import { useEffect, useState } from "react";
import { post } from "./api.js";
import { Mic, Check, Play } from "./icons.jsx";
import { sayPrefix } from "./roles.js";

// The Player arm's games. Idle: the games it can play. Playing: where the game is, as the router
// reports it (st.game.stage: ready → scan → lift → ball → cover → shuffle → guess → reveal → result).
const STEPS = [
  { id: "ready", label: "Get ready", stages: ["ready", "scan"] },
  { id: "lift", label: "Lift the cup", stages: ["lift"] },
  { id: "ball", label: "Place the ball", stages: ["ball"] },
  { id: "cover", label: "Cover it", stages: ["cover"] },
  { id: "shuffle", label: "Shuffle", stages: ["shuffle"] },
  { id: "guess", label: "Your guess", stages: ["guess"] },
  { id: "reveal", label: "Reveal", stages: ["reveal", "result"] },
];
const SLOTS = ["left", "middle", "right"];
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");

// Three cups on a table. up: which cup is lifted; ball: where the ball shows (only when known).
function Cups({ up, ball, spot, peek, pick, picked, shuffling, onPick }) {
  return (
    <div className={`cups${shuffling ? " cups--shuffle" : ""}`}>
      {SLOTS.map((s) => {
        const lifted = up === s;
        const body = (
          <>
            <svg className="cup__svg" viewBox="0 0 64 60" aria-hidden="true">
              <path className="cup__body" d="M15 4h34l7 50H8z" />
              <path className="cup__rim" d="M8 54h48" />
            </svg>
            {ball === s && <i className={`cup__ball${spot ? " cup__ball--spot" : ""}`} />}
            <span className="cup__name">{cap(s)}</span>
          </>
        );
        const cls = `cup${lifted ? " cup--up" : peek === s ? " cup--peek" : ""}${picked === s ? " cup--picked" : ""}`;
        return pick ? (
          <button key={s} type="button" className={`${cls} cup--tap`} onClick={() => onPick(s)} aria-label={`The ${s} cup`}>{body}</button>
        ) : (
          <div key={s} className={cls}>{body}</div>
        );
      })}
    </div>
  );
}

export function GameSteps({ stage }) {
  const at = STEPS.findIndex((s) => s.stages.includes(stage));
  const done = stage === "result";
  return (
    <ol className="stages stages--game" aria-label="Game progress">
      {STEPS.map((s, i) => {
        const state = done || at > i ? "done" : at === i ? "now" : "";
        return (
          <li key={s.id} className={`stage${state ? " stage--" + state : ""}`} aria-current={state === "now" ? "step" : undefined}>
            <span className="stage__dot">{state === "done" ? <Check size={12} /> : i + 1}</span>{s.label}
          </li>
        );
      })}
    </ol>
  );
}

// ---- Jenga: arm 18 builds the tower (st.features.jenga, from src/features/jenga.py) ----
const JSTEPS = [
  { id: "ready", label: "Get ready", stages: ["ready", "scan"] },
  { id: "build", label: "Build the tower", stages: ["aim", "pick", "place", "check", "wait"] },
  { id: "done", label: "Your move", stages: ["done"] },
];

function Tower({ placed, total }) {
  const layers = Math.max(1, Math.round(total / 3));
  const rows = [];
  for (let L = layers - 1; L >= 0; L--) {
    rows.push(
      <div key={L} className={`jt__row jt__row--${L % 2 ? "across" : "along"}`}>
        {[0, 1, 2].map((k) => {
          const i = L * 3 + k;
          return <i key={k} className={`jt__b${i < placed ? " jt__b--on" : ""}${i === placed ? " jt__b--next" : ""}`} />;
        })}
      </div>
    );
  }
  return <div className="jt" aria-label={`${placed} of ${total} blocks placed`}>{rows}</div>;
}

function JengaGame({ j, busy, act, sending, prefix }) {
  const failed = j.stage === "error" || j.stage === "stopped";
  const [placed, total] = j.progress || [0, (j.layers || 6) * 3];
  const needBlocks = failed && /need \d+ more block/i.test(j.detail || "");     // ran out of blocks: the player's turn, not a fault
  const tone = j.stage === "done" ? "win" : needBlocks ? "you" : failed ? "bad" : j.stage === "wait" || j.stage === "aim" ? "you" : "";
  const who = j.stage === "done" ? "Your move!" : needBlocks ? "Your turn" : failed ? (j.stage === "stopped" ? "Stopped" : "Something went wrong")
    : j.stage === "wait" ? "Your turn" : j.stage === "aim" ? "Check the aim" : "Arm is building";
  const at = JSTEPS.findIndex((s) => s.stages.includes(j.stage));
  return (
    <section className="games" aria-label="Jenga in progress">
      <div className="sh sh--tight">
        <div className="sh__inline"><h3>Jenga</h3><span className="sh__sub">{cap(j.arm || "arm 18")} is setting up the tower</span></div>
      </div>
      <div className={`game__status game__status--${tone || "arm"}`} role="status" aria-live="polite">
        <span className="game__who">{who}</span>
        <span className="game__detail">{j.detail}</span>
      </div>
      {!failed && j.stage !== "putdown" && (
        <ol className="stages stages--game" aria-label="Jenga progress">
          {JSTEPS.map((s, i) => {
            const state = j.stage === "done" || at > i ? "done" : at === i ? "now" : "";
            return (
              <li key={s.id} className={`stage${state ? " stage--" + state : ""}`} aria-current={state === "now" ? "step" : undefined}>
                <span className="stage__dot">{state === "done" ? <Check size={12} /> : i + 1}</span>{s.label}
              </li>
            );
          })}
        </ol>
      )}
      <div className="jt__wrap">
        <Tower placed={placed} total={total} />
        <div className="jt__count"><b>{placed}</b><span>of {total} blocks</span></div>
      </div>
      {j.stage === "aim" && <div className="game__hint">The arm is pausing a few centimetres above the block. Its fingers should line up just past both ends. If they don’t, say “{prefix}stop”.</div>}
      {(j.stage === "done" || failed) && (
        <div className="game__actions">
          {failed && !needBlocks && <button type="button" className="btn" disabled={busy || !!sending} onClick={() => act("jenga_putdown")}>Put it down</button>}
          <button type="button" className="btn btn--game" disabled={busy || !!sending} onClick={() => act("jenga_start")}><Play size={14} />{failed ? "Keep building" : "Build another"}</button>
          <button type="button" className="btn" disabled={!!sending} onClick={() => act("jenga_clear")}>Done</button>
        </div>
      )}
    </section>
  );
}

// steps={false} leaves the step strip to the caller (the Player tab draws it under the camera).
export default function GamesPanel({ st, busy, steps = true }) {
  const g = st.game || { games: [] };
  const prefix = sayPrefix(st);
  const [sending, setSending] = useState(null);
  const [err, setErr] = useState("");
  const [picked, setPicked] = useState(null);
  useEffect(() => { if (g.stage !== "guess" && g.stage !== "reveal") setPicked(null); }, [g.stage]);

  async function act(action, extra = {}, key = action) {
    setSending(key); setErr("");
    try { await post({ action, ...extra }); }
    catch (e) { setErr(e.message); if (action === "shell_guess") setPicked(null); }
    finally { setSending(null); }
  }
  const guess = (slot) => { if (g.stage !== "guess" || picked) return; setPicked(slot); act("shell_guess", { slot }, "guess"); };

  const game = g.games && g.games[0];
  const j = st.features && st.features.jenga;
  if (j && j.stage) {
    return <>{<JengaGame j={j} busy={busy} act={act} sending={sending} prefix={prefix} />}{err && <div className="msg no" role="status">{err}</div>}</>;
  }
  if (!game && !j) {
    return <section className="games"><div className="objs__empty">No games are set up for this arm.</div></section>;
  }

  const active = !!g.stage && !!game;
  const failed = g.stage === "error" || g.stage === "stopped";
  const head = (
    <div className="sh sh--tight">
      <div className="sh__inline">
        <h3>{active ? game.name : "Games"}</h3>
        <span className="sh__sub">{active ? `${cap(game.arm)} is playing` : "Pick one, or just say it"}</span>
      </div>
    </div>
  );

  if (!active) {
    return (
      <section className="games" aria-label="Games">
        {head}
        {(g.notice || (j && j.notice)) && <div className="game__notice" role="status">{g.notice || j.notice}</div>}
        {game && (
          <article className="game">
            <div className="game__art" aria-hidden="true"><Cups up={null} ball={null} /></div>
            <div className="game__info">
              <div className="game__title">{game.name}</div>
              <p className="game__blurb">{game.blurb}</p>
              <div className="game__say"><Mic size={16} />“{prefix}{game.say}”</div>
            </div>
            <button type="button" className="btn btn--game" disabled={busy || !!sending} onClick={() => act("shell_start")}>
              <Play size={14} />Play
            </button>
          </article>
        )}
        {j && (
          <article className="game">
            <div className="game__art" aria-hidden="true"><Tower placed={6} total={9} /></div>
            <div className="game__info">
              <div className="game__title">{j.name}</div>
              <p className="game__blurb">{j.blurb}</p>
              <div className="game__say"><Mic size={16} />“{prefix}{j.say}”</div>
            </div>
            <button type="button" className="btn btn--game" disabled={busy || !!sending} onClick={() => act("jenga_start")}>
              <Play size={14} />Build
            </button>
          </article>
        )}
        {err && <div className="msg no" role="status">{err}</div>}
      </section>
    );
  }

  // what the little table shows at each stage
  const up = g.stage === "lift" || g.stage === "ball" ? "middle"
    : (g.stage === "reveal" || g.stage === "result") ? g.guess : null;
  const ball = g.stage === "ball" || g.stage === "cover" ? "middle" : g.stage === "result" ? g.answer : null;
  const tone = g.stage === "result" ? (g.result === "win" ? "win" : g.result === "lose" ? "lose" : "")
    : failed ? "bad" : g.stage === "ball" || g.stage === "guess" ? "you" : "";

  return (
    <section className="games" aria-label="Game in progress">
      {head}
      <div className={`game__status game__status--${tone || "arm"}`} role="status" aria-live="polite">
        <span className="game__who">{tone === "you" ? "Your turn" : g.stage === "result" ? (g.result === "win" ? "Winner!" : g.result === "lose" ? "So close" : "The answer") : failed ? (g.stage === "stopped" ? "Stopped" : "Something went wrong") : "Arm is working"}</span>
        <span className="game__detail">{g.detail}</span>
      </div>
      {steps && !failed && g.stage !== "putdown" && <GameSteps stage={g.stage} />}
      <Cups up={up} ball={ball} spot={g.stage === "ball"} peek={g.stage === "result" && g.result === "lose" ? g.answer : null} pick={g.stage === "guess"} picked={picked || (g.stage === "reveal" ? g.guess : null)}
        shuffling={g.stage === "shuffle"} onPick={guess} />
      {g.stage === "guess" && <div className="game__hint">Tap a cup here or in the camera view — or say “{prefix}left cup”, “middle cup” or “right cup”.</div>}
      {g.stage === "ball" && <div className="game__hint">Set the red ball down where the middle cup stood and take your hand away. The arm covers it once the ball sits still.</div>}
      {(g.stage === "result" || failed) && (
        <div className="game__actions">
          {failed && <button type="button" className="btn" disabled={busy || !!sending} onClick={() => act("shell_putdown")}>Put the cup down</button>}
          <button type="button" className="btn btn--game" disabled={busy || !!sending} onClick={() => act("shell_start")}><Play size={14} />{failed ? "Try again" : "Play again"}</button>
          <button type="button" className="btn" disabled={!!sending} onClick={() => act("shell_clear")}>Done</button>
        </div>
      )}
      {err && <div className="msg no" role="status">{err}</div>}
    </section>
  );
}
