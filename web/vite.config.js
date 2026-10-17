import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the UI on :5173 and forwards every router endpoint to the
// Python status server on :8765, so the page works against the live module.
// `npm run build` writes web/dist, which status_page.py serves at http://localhost:8765.
const ROUTER = "http://127.0.0.1:8765";
const proxy = Object.fromEntries(
  ["/state", "/action", "/live", "/levels", "/logs", "/recordings", "/snapshot.jpg"].map((p) => [p, { target: ROUTER, changeOrigin: true }])
);

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
  build: { outDir: "dist", emptyOutDir: true },
});
