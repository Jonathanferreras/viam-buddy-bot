// Buddy Bot gives each arm a job. The first arm in the router's config is the Helper (it reaches
// and grabs things for you); the second is the Player (it runs the games). Extra arms keep their
// own names. A camera mounted on an arm is listed under that arm's role in the camera picker.
const ROLES = [
  { role: "Helper", blurb: "Grabs things for you" },
  { role: "Player", blurb: "Plays games with you" },
];

export const armKey = (a) => a.resource || a.name;

export function roleFor(arm, arms) {
  const i = arms.findIndex((a) => armKey(a) === armKey(arm));
  return ROLES[i] || { role: arm.name, blurb: "" };
}

export function camLabel(cam, arms) {
  if (cam.arm) {
    const arm = arms.find((a) => a.name === cam.arm);
    if (arm) return roleFor(arm, arms).role;
  }
  const l = (cam.label || cam.name).replace(/\s+camera$/i, "");
  return /^table$/i.test(l) ? "Desk" : l;
}

// The wake word, as the router reports it ("hey buddy"), capitalised for display: "Hey buddy".
export function wakeWord(st) {
  const w = (st && st.mic && st.mic.wake_word) || "hey buddy";
  return w.charAt(0).toUpperCase() + w.slice(1);
}

// What to say before a command: nothing in tap-to-talk mode, the wake word otherwise.
export const sayPrefix = (st) => ((st.mic && st.mic.mode) === "toggle" ? "" : `${wakeWord(st)}, `);

// The game the Player runs. While a game step is running (the router's st.game.running), or the
// command's label names the game, the page switches to the game accent.
export const isGameCommand = (label, st) => !!(st && ((st.game && st.game.running) || (st.features && st.features.jenga && st.features.jenga.running)))
  || /shell|game|jenga/i.test(label || "");

// The cameras by job: the Helper's wrist camera, the Player's, and the overhead desk camera (no arm).
export function camsByRole(st) {
  const cams = (st && st.cams) || [], arms = (st && st.arms) || [];
  const byRole = (role) => cams.find((c) => { const a = c.arm && arms.find((x) => x.name === c.arm); return a && roleFor(a, arms).role === role; }) || null;
  return { helper: byRole("Helper"), player: byRole("Player"), desk: cams.find((c) => !c.arm) || null, cams, arms };
}
