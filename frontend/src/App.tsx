import { lazy, Suspense, useRef, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";

import type { Capabilities } from "./api/types";
import { useAuth } from "./auth/AuthContext";
import { BrandMark } from "./components/icons";
import Layout from "./components/Layout";
import { Button, EmptyState, Spinner } from "./components/ui";
import LoginPage from "./pages/LoginPage";

// Split by area: someone who only searches never downloads the admin
// screens, and the first paint after sign-in waits on one chunk, not six.
const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const DocumentsPage = lazy(() => import("./pages/DocumentsPage"));
const DocumentDetailPage = lazy(() => import("./pages/DocumentDetailPage"));
const SearchPage = lazy(() => import("./pages/SearchPage"));
const ChatPage = lazy(() => import("./pages/ChatPage"));
const CollectionsPage = lazy(() => import("./pages/CollectionsPage"));
const AccountPage = lazy(() => import("./pages/AccountPage"));
const UsersPage = lazy(() => import("./pages/admin/UsersPage"));
const DepartmentsPage = lazy(() => import("./pages/admin/DepartmentsPage"));
const AuditPage = lazy(() => import("./pages/admin/AuditPage"));
const ProcessingPage = lazy(() => import("./pages/admin/ProcessingPage"));
const SystemPage = lazy(() => import("./pages/admin/SystemPage"));

/**
 * Hides a screen from someone who cannot use it. A convenience only: every
 * endpoint behind these screens enforces the same rule on the server, and a
 * user who types the URL gets an explanation rather than a broken page.
 */
function Requires({
  capability,
  children,
}: {
  capability: (keyof Capabilities)[];
  children: ReactNode;
}) {
  const { can } = useAuth();
  if (!capability.some((item) => can(item))) {
    return (
      <EmptyState title="You do not have access to this page" icon="lock">
        It is available to administrators. If you need it, ask your department
        administrator.
      </EmptyState>
    );
  }
  return <>{children}</>;
}

function NotFound() {
  return (
    <EmptyState title="Page not found" icon="compass">
      The address may be mistyped, or the page may have moved.
    </EmptyState>
  );
}

/**
 * A page that quick find can hand work to - a question for Ask, a query for
 * Search, an upload for Documents - starts afresh when it does, even when
 * it is already the page on screen: these pages read a hand-over only when
 * they mount. The key changes only when a NEW hand-over arrives, so the
 * page clearing its history state afterwards (or Ask moving /ask to
 * /ask/12) does not remount it again.
 */
function useHandoffKey() {
  const location = useLocation();
  const handoff = (location.state as { handoff?: string } | null)?.handoff;
  const key = useRef("initial");
  if (handoff && handoff !== key.current) key.current = handoff;
  return key.current;
}

function ChatRoute() {
  return <ChatPage key={useHandoffKey()} />;
}

function SearchRoute() {
  return <SearchPage key={useHandoffKey()} />;
}

function DocumentsRoute() {
  return <DocumentsPage key={useHandoffKey()} />;
}

export default function App() {
  const { state, refresh } = useAuth();

  if (state === "checking") {
    return (
      <div className="centered">
        <div className="session-state">
          <BrandMark size="lg" />
          <Spinner label="Checking your session…" />
        </div>
      </div>
    );
  }

  if (state === "signed-out") return <LoginPage />;

  if (state === "unreachable") {
    return (
      <div className="centered">
        <div className="session-card">
          <EmptyState title="The server cannot be reached" icon="wifi-off">
            <p>
              Your session may still be valid - nothing has been signed out. Check your
              connection, then try again.
            </p>
            <Button
              variant="primary"
              icon="refresh"
              onClick={() => void refresh().catch(() => undefined)}
            >
              Try again
            </Button>
          </EmptyState>
        </div>
      </div>
    );
  }

  return (
    <Layout>
      <Suspense
        fallback={
          <div className="centered-block">
            <Spinner />
          </div>
        }
      >
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/documents" element={<DocumentsRoute />} />
          <Route path="/documents/:id" element={<DocumentDetailPage />} />
          <Route path="/search" element={<SearchRoute />} />
          <Route path="/ask" element={<ChatRoute />} />
          <Route path="/ask/:conversationId" element={<ChatRoute />} />
          <Route path="/collections" element={<CollectionsPage />} />
          <Route path="/account" element={<AccountPage />} />
          <Route
            path="/admin/users"
            element={
              <Requires capability={["administer_users"]}>
                <UsersPage />
              </Requires>
            }
          />
          <Route
            path="/admin/departments"
            element={
              <Requires capability={["administer_departments", "administer_users"]}>
                <DepartmentsPage />
              </Requires>
            }
          />
          <Route
            path="/admin/audit"
            element={
              <Requires capability={["view_audit_log"]}>
                <AuditPage />
              </Requires>
            }
          />
          <Route
            path="/admin/processing"
            element={
              <Requires capability={["view_operations"]}>
                <ProcessingPage />
              </Requires>
            }
          />
          <Route
            path="/admin/system"
            element={
              <Requires capability={["view_system_status"]}>
                <SystemPage />
              </Requires>
            }
          />
          <Route path="/login" element={<Navigate to="/" replace />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </Layout>
  );
}
