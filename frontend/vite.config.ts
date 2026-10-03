import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../web/static/spa",
    emptyOutDir: true,
    sourcemap: false,
    minify: "esbuild",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8080", ws: true },
    },
  },
});
