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
//
// Matching is by prefix. "/admin" is NOT here: it is the app's own
// administration screens, and proxying it once sent every reload of
// /admin/users to Django's admin. Django's admin is at /django-admin/.
const PROXIED = ["/api", "/health", "/ready", "/django-admin", "/static"];

// React, its DOM renderer, the router and React's scheduler (either path
// separator: builds run on Windows too).
const VENDOR = /[\\/]node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/;

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const target = env.VITE_PROXY_TARGET || "http://localhost:8000";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      // Playwright writes its traces into e2e/.results while the browser
      // suite runs against this server, and a trace's .html resources are
      // pages to Vite's watcher: each one made it reload the browser under
      // the test, closing whatever dialog was open.
      watch: { ignored: ["**/e2e/.results/**"] },
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
      rollupOptions: {
        output: {
          // The framework in one "vendor" chunk: every screen needs it, and a
          // stable hash means a deploy that changes only application code
          // re-downloads only that. Nothing else is forced in: a library
          // imported only by a lazy screen (a PDF renderer, a chart kit)
          // stays in that screen's chunk instead of loading with every page,
          // which "everything under node_modules" did.
          //
          // By path, not by package name: the object form only matches a
          // package's entry module, so react-dom/client landed in the app
          // chunk and every deploy re-downloaded React.
          manualChunks(id) {
            return VENDOR.test(id) ? "vendor" : undefined;
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
      // Well above the 5 s that each findBy*/waitFor may wait (src/test/
      // setup.ts). At vitest's default, also 5 s, a test whose waits were
      // each within their limit timed out as a whole on a loaded machine -
      // seven tests at 5.2-6.6 s, all passing alone - and a query whose
      // element never appeared reported "Test timed out" instead of the
      // query's own error.
      testTimeout: 20_000,
    },
  };
});
