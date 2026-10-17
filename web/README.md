# Buddy Bot — status page

The browser UI for the voice router, built with Vite + React. The Python module
(`src/status_page.py`) serves the built files at http://localhost:8765 and provides
the JSON/stream endpoints the page talks to (`/state`, `/action`, `/live/frame`,
`/live/depth_at`, `/levels`, `/logs`, `/recordings/…`).

```bash
cd web
npm install
npm run build     # -> web/dist, served by the module (restart viam-server to pick it up the first time)
npm run dev       # hot-reloading dev server on :5173, proxied to the module on :8765
```

If `web/dist` is missing, the module falls back to the original single-file page.

Layout: `src/App.jsx` (polling, shortcuts) → `TopBar`, `VoicePanel` (orb + Hey Viam → Command →
Action), `Buddies` (one card per arm: Helper, Player), `DeskView` (cameras, detections, hover depth,
grab bar, “Within reach”), `Drawers` (what you can say, logs). Shared hooks in `src/hooks.js`.

Keys: **M** mic on/off (toggle mode) · **C** commands · **L** logs · **Esc** stop all arms.
