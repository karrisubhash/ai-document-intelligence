import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { API_BASE_URL, api } from "./api/client";
import { useResource } from "./api/hooks";
import { useAuth } from "./auth/AuthContext";
import { Pill, Spinner } from "./components/ui";
import ChatPage from "./pages/ChatPage";
import DocumentDetailPage from "./pages/DocumentDetailPage";
import DocumentsPage from "./pages/DocumentsPage";
import LoginPage from "./pages/LoginPage";
import SearchPage from "./pages/SearchPage";

function HealthIndicator() {
  const health = useResource(() => api.health(), []);

  if (health.loading) return <Pill tone="muted">checking API…</Pill>;

  if (health.error || health.data?.status !== "healthy") {
    return (
      <Pill tone="bad" title={`No healthy response from ${API_BASE_URL}`}>
        API unreachable
      </Pill>
    );
  }

  return (
    <Pill tone="ok" title={API_BASE_URL}>
      API healthy
    </Pill>
  );
}

function Shell() {
  const { me, logout } = useAuth();

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <div className="brand-title">Document Intelligence Console</div>
            <div className="brand-sub">{API_BASE_URL}</div>
          </div>
        </div>

        <nav className="nav">
          <NavLink to="/documents" className="nav-link">
            Documents
          </NavLink>
          <NavLink to="/search" className="nav-link">
            Search
          </NavLink>
          <NavLink to="/chat" className="nav-link">
            Ask
          </NavLink>
        </nav>

        <div className="topbar-right">
          <HealthIndicator />
          {me ? (
            <span className="whoami" title={`role: ${me.role ?? "none"}`}>
              {me.username}
              <span className="whoami-meta">
                {me.role ?? "no role"} · {me.department || "no department"}
              </span>
            </span>
          ) : null}
          <button className="button button-quiet" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      <main className="main">
        <Routes>
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/documents/:id" element={<DocumentDetailPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="*" element={<Navigate to="/documents" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const { token, restoring } = useAuth();

  // Rendering the console before the stored token is verified would fire
  // every page's requests with credentials that may already be revoked.
  if (restoring) {
    return (
      <div className="centered">
        <Spinner label="Restoring session…" />
      </div>
    );
  }

  if (!token) return <LoginPage />;

  return <Shell />;
}
