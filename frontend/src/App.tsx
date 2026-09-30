import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import type { Capabilities } from "./api/types";
import { useAuth } from "./auth/AuthContext";
import Layout from "./components/Layout";
import { EmptyState, Spinner } from "./components/ui";
import LoginPage from "./pages/LoginPage";

// Split by area: someone who only searches never downloads the admin
// screens, and the first paint after sign-in waits on one chunk, not six.
const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const DocumentsPage = lazy(() => import("./pages/DocumentsPage"));
const DocumentDetailPage = lazy(() => import("./pages/DocumentDetailPage"));
const SearchPage = lazy(() => import("./pages/SearchPage"));
const ChatPage = lazy(() => import("./pages/ChatPage"));
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
      <EmptyState title="You do not have access to this page" icon="🔒">
        It is available to administrators. If you need it, ask your department
        administrator.
      </EmptyState>
    );
  }
  return <>{children}</>;
}

function NotFound() {
  return (
    <EmptyState title="Page not found" icon="🧭">
      The address may be mistyped, or the page may have moved.
    </EmptyState>
  );
}

export default function App() {
  const { state } = useAuth();

  if (state === "checking") {
    return (
      <div className="centered">
        <Spinner label="Checking your session…" />
      </div>
    );
  }

  if (state === "signed-out") return <LoginPage />;

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
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/documents/:id" element={<DocumentDetailPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/ask" element={<ChatPage />} />
          <Route path="/ask/:conversationId" element={<ChatPage />} />
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
