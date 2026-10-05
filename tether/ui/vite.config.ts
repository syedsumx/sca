import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, /api is proxied to the FastAPI server. In Docker, nginx does the same.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.TETHER_API_URL ?? "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
