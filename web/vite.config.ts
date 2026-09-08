import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The bundle is served by the API under /ui, so asset URLs must be prefixed to match.
// scripts/build_ui.py copies dist/ into the Python package before a release build.
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // In development the API runs separately; proxying keeps the frontend on a single
    // origin so it can use the same relative request paths as the bundled build.
    proxy: {
      "/v1": {
        target: process.env.BCE_API_URL ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
