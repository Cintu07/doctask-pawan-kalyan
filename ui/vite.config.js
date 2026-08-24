import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build lands in src/analyst/static so FastAPI can serve it directly, and
// the built output is committed. A stranger cloning this repo gets a working
// review interface without needing node installed at all.
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: {
    outDir: "../src/analyst/static",
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8000", rewrite: (p) => p.replace(/^\/api/, "") } },
  },
});
