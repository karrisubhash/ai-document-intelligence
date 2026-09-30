/// <reference types="vitest/config" />
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The browser talks to ONE origin, in development exactly as in production.
// Vite proxies the API paths to Django here; nginx does the same in the
// production image. That is what lets the session cookie stay httpOnly and
// SameSite=Lax with CORS credentials switched off: there is no second origin
// for the browser to attach it to.
//
// changeOrigin stays OFF on purpose. Django's CSRF check compares the
// request's Origin header with its Host header; rewriting Host to the
// target would make every unsafe request from the dev server look
// cross-origin and fail with "Origin checking failed".
const PROXIED = ["/api", "/health", "/ready", "/admin", "/static"];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const target = env.VITE_PROXY_TARGET || "http://localhost:8000";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: Object.fromEntries(
        PROXIED.map((path) => [path, { target, changeOrigin: false }]),
      ),
    },
    preview: {
      port: 4173,
      proxy: Object.fromEntries(
        PROXIED.map((path) => [path, { target, changeOrigin: false }]),
      ),
    },
    build: {
      sourcemap: false,
      // One vendor chunk: the app is small, and a stable vendor hash means
      // a deploy that changes only application code re-downloads only it.
      rollupOptions: {
        output: {
          // By path, not by package name: the object form only matches a
          // package's entry module, so react-dom/client landed in the app
          // chunk and every deploy re-downloaded React.
          manualChunks(id) {
            return id.includes("node_modules") ? "vendor" : undefined;
          },
        },
      },
    },
    test: {
      // Unit tests only. e2e/ holds Playwright specs that need a live stack.
      include: ["src/**/*.test.{ts,tsx}"],
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: false,
      restoreMocks: true,
    },
  };
});
