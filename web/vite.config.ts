import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The dev server proxies /api to Flask so the browser sees one origin and CORS
// never enters the picture. `changeOrigin` plus a disabled proxy timeout keeps
// SSE connections alive instead of cutting them at 30s.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.API_ORIGIN ?? "http://127.0.0.1:8000",
        changeOrigin: true,
        timeout: 0,
        proxyTimeout: 0,
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
