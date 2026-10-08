import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    // Dev: proxy /substrate/* AND /finance/* to a locally-running mcp-hub so the
    // browser stays same-origin and we don't need CORS during development. Both
    // prefixes are served by mcp-hub in prod (nginx routes them there), so the
    // dev proxy mirrors that — without /finance the Today/Budget/Bills aggregate
    // cards 404 locally. Point VITE_DEV_MCP_HUB at wherever mcp-hub is running
    // (`kubectl -n platform-mcp port-forward svc/mcp-hub 8000:8000`).
    proxy: {
      "/substrate": {
        target: process.env.VITE_DEV_MCP_HUB ?? "http://localhost:8000",
        changeOrigin: true,
      },
      "/api": {
        target: process.env.VITE_DEV_MCP_HUB ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
