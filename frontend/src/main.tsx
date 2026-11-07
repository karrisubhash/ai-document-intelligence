import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { AppErrorBoundary } from "./components/ErrorBoundary";
import { ToastProvider } from "./components/ui";
// Loaded on every screen, the sign-in page included: it keeps <html>'s
// theme and the browser's theme colour in step with the system setting.
import "./lib/theme";
// Bundled, not linked from a font CDN: the Content-Security-Policy allows
// fonts from this origin only, and nothing about a reader's visit should
// reach a third party.
import "@fontsource-variable/inter";
import "./styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("#root is missing from index.html");

createRoot(container).render(
  <StrictMode>
    {/* Outside every provider: its fallback must not depend on any of them. */}
    <AppErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </BrowserRouter>
    </AppErrorBoundary>
  </StrictMode>,
);
