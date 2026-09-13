import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Pinned because the API's CORS_ALLOWED_ORIGINS development default
    // names this exact origin. Vite silently picks the next free port when
    // this one is taken, which would produce a browser-side CORS failure
    // that looks nothing like its cause.
    port: 5173,
    strictPort: true,
  },
});
