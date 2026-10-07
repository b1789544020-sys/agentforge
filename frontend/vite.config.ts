import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        // 0.1.2 #10 — the live-trace WebSocket lives under
        // ``/api/agents/.../runs/.../ws``; without ``ws: true``
        // the dev server would refuse the upgrade and the front
        // end would fall back to polling.  Default is true in
        // Vite >= 5 but we set it explicitly so future maintainers
        // don't have to wonder.
        ws: true,
      },
    },
  },
});