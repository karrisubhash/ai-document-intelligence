import { useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import { useAction, useDebouncedValue, useResource } from "../../api/hooks";
import type { AdminUser, Role, SignupRequest, SignupStatus } from "../../api/types";
import { useAuth, useMe } from "../../auth/AuthContext";
import { Icon } from "../../components/icons";
import {
  Alert,
  Avatar,
  Badge,
  Button,
  ConfirmDialog,
  DescriptionList,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  PageHeader,
  Pagination,
  Skeleton,
  TabPanel,
  Tabs,
  errorMessage,
  useDocumentTitle,
  useToast,
} from "../../components/ui";
import { displayName, formatRelative } from "../../lib/format";
import { ROLE_LABEL } from "../../lib/labels";

type Draft = {
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  role: Role;
  department: string;
  password: string;
  is_active: boolean;
};

function UserDialog({
  user,
  open,
  onClose,
  onSaved,
}: {
  user: AdminUser | null;
  open: boolean;
  onClose: () => void;
  onSaved: (user: AdminUser) => void;
}) {
  const me = useMe();
  const { can } = useAuth();
  const system = can("administer_departments");
  const roles = useResource(() => api.adminRoles(), [], { enabled: open });
  const departments = useResource(() => api.departments(), [], { enabled: open && system });

  const [draft, setDraft] = useState<Draft>(() => ({
    username: user?.username ?? "",
    email: user?.email ?? "",
    first_name: user?.first_name ?? "",
    last_name: user?.last_name ?? "",
    role: user?.role ?? "STUDENT",
    department: user?.department ?? (system ? "" : me.department),
    password: "",
    is_active: user?.is_active ?? true,
  }));
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const save = useAction(async () => {
    const body: Partial<AdminUser> & { password?: string } = {
      email: draft.email.trim(),
      first_name: draft.first_name.trim(),
      last_name: draft.last_name.trim(),
      role: draft.role,
      is_active: draft.is_active,
      // Only when it CHANGED: the picker lists active departments, so for a
      // member of an inactive one an unchanged value was sent back and
      // refused - no edit to that person could be saved at all.
      ...(system && draft.department !== (user?.department ?? "")
        ? { department: draft.department }
        : {}),
    };
    if (user) return api.adminUpdateUser(user.id, body);
    return api.adminCreateUser({ ...body, username: draft.username.trim(), password: draft.password });
  });
  const errors = save.error instanceof ApiError ? save.error.fieldErrors() : {};
  const self = user?.id === me.id;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const result = await save.run();
    if (result.ok) onSaved(result.value);
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={user ? `Edit ${displayName(user)}` : "New account"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="user-form" loading={save.pending}>
            {user ? "Save changes" : "Create account"}
          </Button>
        </>
      }
    >
      <form id="user-form" className="stack" onSubmit={submit} noValidate>
        <div className="form-row">
          <Field label="Username" error={errors.username} required>
            {(props) => (
              <input
                {...props}
                className="input"
                value={draft.username}
                disabled={Boolean(user)}
                autoComplete="off"
                onChange={(event) => set("username", event.target.value)}
              />
            )}
          </Field>
          <Field label="Email" error={errors.email}>
            {(props) => (
              <input
                {...props}
                type="email"
                className="input"
                value={draft.email}
                onChange={(event) => set("email", event.target.value)}
              />
            )}
          </Field>
        </div>
        <div className="form-row">
          <Field label="First name" error={errors.first_name}>
            {(props) => (
              <input
                {...props}
                className="input"
                value={draft.first_name}
                onChange={(event) => set("first_name", event.target.value)}
              />
            )}
          </Field>
          <Field label="Last name" error={errors.last_name}>
            {(props) => (
              <input
                {...props}
                className="input"
                value={draft.last_name}
                onChange={(event) => set("last_name", event.target.value)}
              />
            )}
          </Field>
        </div>
        <div className="form-row">
          <Field
            label="Role"
            error={errors.role}
            hint={self ? "You cannot change your own role." : undefined}
          >
            {(props) => (
              <select
                {...props}
                className="input"
                value={draft.role}
                disabled={self}
                onChange={(event) => set("role", event.target.value as Role)}
              >
                {(roles.data ?? []).map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
                {!roles.data?.some((option) => option.value === draft.role) ? (
                  <option value={draft.role}>{ROLE_LABEL[draft.role]}</option>
                ) : null}
              </select>
            )}
          </Field>
          <Field
            label="Department"
            error={errors.department}
            hint={system ? "Decides which documents they can read." : "Accounts you manage stay in your department."}
          >
            {(props) =>
              system ? (
                <select
                  {...props}
                  className="input"
                  value={draft.department}
                  onChange={(event) => set("department", event.target.value)}
                >
                  <option value="">No department</option>
                  {(departments.data ?? []).map((department) => (
                    <option key={department.id} value={department.name}>
                      {department.name}
                    </option>
                  ))}
                  {/* The person's current department, when it is no longer
                      active: shown as what it is rather than silently
                      displayed as "No department". */}
                  {user?.department &&
                  departments.data &&
                  !departments.data.some((department) => department.name === user.department) ? (
                    <option value={user.department}>{user.department} (inactive)</option>
                  ) : null}
                </select>
              ) : (
                <input {...props} className="input" value={draft.department} disabled />
              )
            }
          </Field>
        </div>
        {!user ? (
          <Field
            label="Initial password"
            error={errors.password}
            hint="Share it securely; ask them to change it after signing in."
            required
          >
            {(props) => (
              <input
                {...props}
                type="password"
                className="input"
                autoComplete="new-password"
                value={draft.password}
                onChange={(event) => set("password", event.target.value)}
              />
            )}
          </Field>
        ) : null}
        <label className="checkbox">
          <input
            type="checkbox"
            checked={draft.is_active}
            disabled={self}
            onChange={(event) => set("is_active", event.target.checked)}
          />
          Active — can sign in
        </label>
        {user && !draft.is_active && user.is_active ? (
          <Alert tone="warning">
            Deactivating ends their sessions and revokes their API tokens. Their documents and
            audit history are kept.
          </Alert>
        ) : null}
        {errors.non_field_errors || (save.error && !Object.keys(errors).length) ? (
          <Alert tone="danger">{errors.non_field_errors || errorMessage(save.error)}</Alert>
        ) : null}
      </form>
    </Dialog>
  );
}

function PasswordDialog({
  user,
  onClose,
}: {
  user: AdminUser | null;
  onClose: () => void;
}) {
  const toast = useToast();
  const [password, setPassword] = useState("");
  const reset = useAction((value: string) => api.adminSetPassword(user!.id, value));
  const errors = reset.error instanceof ApiError ? reset.error.fieldErrors() : {};

  return (
    <Dialog
      open={Boolean(user)}
      onClose={onClose}
      title={user ? `Set a new password for ${displayName(user)}` : ""}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={reset.pending}
            disabled={!password}
            onClick={async () => {
              const result = await reset.run(password);
              if (result.ok) {
                setPassword("");
                onClose();
                toast("Password set. Their existing sessions and tokens have ended.");
              }
            }}
          >
            Set password
          </Button>
        </>
      }
    >
      <div className="stack">
        <Field label="New password" error={errors.password}>
          {(props) => (
            <input
              {...props}
              type="password"
              className="input"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          )}
        </Field>
        {/* Anything that is not about the password field - a 403, a throttle,
            an unreachable server - used to show nothing at all. */}
        {reset.error && !errors.password ? (
          <Alert tone="danger">{errorMessage(reset.error)}</Alert>
        ) : null}
      </div>
    </Dialog>
  );
}

type Editing = AdminUser | "new" | null;

function Accounts({
  editing,
  setEditing,
}: {
  editing: Editing;
  setEditing: (value: Editing) => void;
}) {
  const toast = useToast();
  const me = useMe();
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [role, setRole] = useState("");
  const [active, setActive] = useState("");
  const search = useDebouncedValue(searchInput.trim(), 300);
  const [resetting, setResetting] = useState<AdminUser | null>(null);

  const users = useResource(
    () => api.adminUsers({ page, search, role, is_active: active }),
    [page, search, role, active],
  );

  return (
    <>
      <div className="toolbar">
        <div className="input-icon toolbar-search">
          <Icon name="search" />
          <input
            type="search"
            className="input"
            placeholder="Search by name, username or email…"
            aria-label="Search users"
            value={searchInput}
            onChange={(event) => {
              setSearchInput(event.target.value);
              setPage(1);
            }}
          />
        </div>
        <select
          className="input"
          aria-label="Role"
          value={role}
          onChange={(event) => {
            setRole(event.target.value);
            setPage(1);
          }}
        >
          <option value="">Any role</option>
          {(Object.keys(ROLE_LABEL) as Role[]).map((value) => (
            <option key={value} value={value}>
              {ROLE_LABEL[value]}
            </option>
          ))}
        </select>
        <select
          className="input"
          aria-label="Status"
          value={active}
          onChange={(event) => {
            setActive(event.target.value);
            setPage(1);
          }}
        >
          <option value="">Any status</option>
          <option value="true">Active</option>
          <option value="false">Inactive</option>
        </select>
      </div>

      {users.error && !users.data ? (
        <ErrorState error={users.error} onRetry={users.reload} />
      ) : users.loading && !users.data ? (
        <div className="card card-body">
          <Skeleton lines={6} />
        </div>
      ) : !users.data?.results.length ? (
        <div className="card">
          <EmptyState title="No accounts match" icon="users" />
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table table-stack">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Role</th>
                <th scope="col">Department</th>
                <th scope="col">Status</th>
                <th scope="col">Last sign-in</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {users.data.results.map((user) => (
                <tr key={user.id}>
                  <td>
                    <div className="cell-title">
                      <Avatar name={displayName(user)} />
                      <div className="cell-title-text">
                        <span className="table-title">{displayName(user)}</span>
                        <span
                          className="table-sub table-sub-truncate"
                          title={user.email ? `${user.username} · ${user.email}` : undefined}
                        >
                          {user.username}
                          {user.email ? ` · ${user.email}` : ""}
                        </span>
                      </div>
                    </div>
                  </td>
                  <td data-label="Role">{ROLE_LABEL[user.role] ?? user.role}</td>
                  <td data-label="Department">{user.department || <span className="muted">—</span>}</td>
                  <td data-label="Status">
                    <Badge tone={user.is_active ? "success" : "muted"} dot>
                      {user.is_active ? "Active" : "Inactive"}
                    </Badge>
                  </td>
                  <td className="nowrap muted" data-label="Last sign-in">
                    {user.last_login ? formatRelative(user.last_login) : "Never"}
                  </td>
                  <td className="cell-actions">
                    {user.can_manage ? (
                      <div className="button-row">
                        <Button size="sm" icon="pencil" onClick={() => setEditing(user)}>
                          Edit
                        </Button>
                        {/* Your own password is changed under Account, with
                            the current password - never here. */}
                        {user.id !== me.id ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            icon="key"
                            onClick={() => setResetting(user)}
                          >
                            Set password
                          </Button>
                        ) : null}
                      </div>
                    ) : (
                      <span className="muted small">Managed by a system administrator</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {users.data ? (
        <Pagination page={page} count={users.data.count} onPage={setPage} />
      ) : null}

      {editing ? (
        <UserDialog
          key={editing === "new" ? "new" : editing.id}
          user={editing === "new" ? null : editing}
          open
          onClose={() => setEditing(null)}
          onSaved={(saved) => {
            setEditing(null);
            users.reload();
            toast(editing === "new" ? `Account ${saved.username} created.` : "Changes saved.");
          }}
        />
      ) : null}
      {/* Keyed by account: what was typed (and any error) for one person
          must not carry over to the next. */}
      <PasswordDialog
        key={resetting?.id ?? "closed"}
        user={resetting}
        onClose={() => setResetting(null)}
      />
    </>
  );
}

const SIGNUP_STATUS_LABEL: Record<SignupStatus, string> = {
  PENDING: "Waiting",
  APPROVED: "Approved",
  REJECTED: "Declined",
};

function ApproveDialog({
  signup,
  onClose,
  onApproved,
}: {
  signup: SignupRequest;
  onClose: () => void;
  onApproved: (signup: SignupRequest) => void;
}) {
  const me = useMe();
  const { can } = useAuth();
  const system = can("administer_departments");
  const roles = useResource(() => api.adminRoles(), []);
  const departments = useResource(() => api.departments(), [], { enabled: system });
  // What they asked for, until the reviewer says otherwise.
  const [role, setRole] = useState<Role>(signup.role);
  const [department, setDepartment] = useState(signup.department);

  const approve = useAction(() =>
    api.adminApproveSignup(signup.id, {
      role,
      // A department admin's approvals land in their own department, which
      // the server fills in.
      ...(system ? { department } : {}),
    }),
  );
  const errors = approve.error instanceof ApiError ? approve.error.fieldErrors() : {};
  const close = () => {
    if (!approve.pending) onClose();
  };

  return (
    <Dialog
      open
      onClose={close}
      title={`Approve ${displayName(signup)}`}
      description="Creates their account. They sign in with the password they chose."
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={approve.pending}>
            Cancel
          </Button>
          <Button
            variant="primary"
            icon="check"
            loading={approve.pending}
            onClick={async () => {
              const result = await approve.run();
              if (result.ok) onApproved(result.value);
            }}
          >
            Approve and create account
          </Button>
        </>
      }
    >
      <div className="stack">
        <DescriptionList
          items={[
            ["Username", signup.username],
            ["Email", signup.email],
            ...(signup.note ? ([["Their note", signup.note]] as [string, string][]) : []),
          ]}
        />
        <div className="form-row">
          <Field label="Role" error={errors.role} hint={`They asked to be: ${ROLE_LABEL[signup.role]}.`}>
            {(props) => (
              <select
                {...props}
                className="input"
                value={role}
                onChange={(event) => setRole(event.target.value as Role)}
              >
                {(roles.data ?? []).map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
                {!roles.data?.some((option) => option.value === role) ? (
                  <option value={role}>{ROLE_LABEL[role]}</option>
                ) : null}
              </select>
            )}
          </Field>
          <Field
            label="Department"
            error={errors.department}
            hint={system ? "Decides which documents they can read." : undefined}
          >
            {(props) =>
              system ? (
                <select
                  {...props}
                  className="input"
                  value={department}
                  onChange={(event) => setDepartment(event.target.value)}
                >
                  <option value="">No department</option>
                  {(departments.data ?? []).map((option) => (
                    <option key={option.id} value={option.name}>
                      {option.name}
                    </option>
                  ))}
                  {/* Asked for before it was retired: shown as what it is,
                      and refused by the server if kept. */}
                  {signup.department &&
                  departments.data &&
                  !departments.data.some((option) => option.name === signup.department) ? (
                    <option value={signup.department}>{signup.department} (inactive)</option>
                  ) : null}
                </select>
              ) : (
                <input {...props} className="input" value={me.department} disabled />
              )
            }
          </Field>
        </div>
        {approve.error && !errors.role && !errors.department ? (
          <Alert tone="danger">{errorMessage(approve.error)}</Alert>
        ) : null}
      </div>
    </Dialog>
  );
}

function DeclineDialog({
  signup,
  onClose,
  onDeclined,
}: {
  signup: SignupRequest;
  onClose: () => void;
  onDeclined: () => void;
}) {
  const [note, setNote] = useState("");
  const decline = useAction(() => api.adminRejectSignup(signup.id, note.trim()));
  return (
    <ConfirmDialog
      open
      title={`Decline ${displayName(signup)}?`}
      confirmLabel="Decline request"
      pending={decline.pending}
      error={decline.error}
      onClose={onClose}
      onConfirm={async () => {
        const result = await decline.run();
        if (result.ok) onDeclined();
      }}
    >
      <p>No account is created. They can send a new request later.</p>
      <Field label="Reason" hint="Optional. Shown to them when they next try to sign in.">
        {(props) => (
          <textarea
            {...props}
            className="input"
            rows={2}
            maxLength={500}
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
        )}
      </Field>
    </ConfirmDialog>
  );
}

function SignupRequests({ onDecided }: { onDecided: () => void }) {
  const toast = useToast();
  const [status, setStatus] = useState<SignupStatus>("PENDING");
  const [page, setPage] = useState(1);
  const [approving, setApproving] = useState<SignupRequest | null>(null);
  const [declining, setDeclining] = useState<SignupRequest | null>(null);

  const requests = useResource(
    () => api.adminSignupRequests({ status, page }),
    [status, page],
  );
  const decided = () => {
    requests.reload();
    onDecided();
  };

  return (
    <>
      <div className="toolbar">
        <select
          className="input"
          aria-label="Status"
          value={status}
          onChange={(event) => {
            setStatus(event.target.value as SignupStatus);
            setPage(1);
          }}
        >
          {(Object.keys(SIGNUP_STATUS_LABEL) as SignupStatus[]).map((value) => (
            <option key={value} value={value}>
              {SIGNUP_STATUS_LABEL[value]}
            </option>
          ))}
        </select>
      </div>

      {requests.error && !requests.data ? (
        <ErrorState error={requests.error} onRetry={requests.reload} />
      ) : requests.loading && !requests.data ? (
        <div className="card card-body">
          <Skeleton lines={4} />
        </div>
      ) : !requests.data?.results.length ? (
        <div className="card">
          <EmptyState
            title={status === "PENDING" ? "Nobody is waiting" : "No requests here"}
            icon="inbox"
          >
            {status === "PENDING"
              ? "New sign-up requests for you to review will appear here."
              : null}
          </EmptyState>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table table-stack">
            <thead>
              <tr>
                <th scope="col">Applicant</th>
                <th scope="col">Asked for</th>
                <th scope="col">Note</th>
                <th scope="col">{status === "PENDING" ? "Sent" : "Decided"}</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {requests.data.results.map((signup) => (
                <tr key={signup.id}>
                  <td>
                    <div className="cell-title">
                      <Avatar name={displayName(signup)} />
                      <div className="cell-title-text">
                        <span className="table-title">{displayName(signup)}</span>
                        <span
                          className="table-sub table-sub-truncate"
                          title={`${signup.username} · ${signup.email}`}
                        >
                          {signup.username} · {signup.email}
                        </span>
                      </div>
                    </div>
                  </td>
                  <td data-label="Asked for">
                    {ROLE_LABEL[signup.role] ?? signup.role}
                    <span className="table-sub">
                      {signup.department || "Department not listed"}
                    </span>
                  </td>
                  <td data-label="Note" className="signup-note">
                    {signup.status === "REJECTED" && signup.decision_note ? (
                      <>
                        <span className="muted small">Reason given: </span>
                        {signup.decision_note}
                      </>
                    ) : (
                      signup.note || <span className="muted">—</span>
                    )}
                  </td>
                  <td className="nowrap muted" data-label={status === "PENDING" ? "Sent" : "Decided"}>
                    {formatRelative(signup.status === "PENDING" ? signup.created_at : signup.reviewed_at)}
                    {signup.reviewed_by ? (
                      <span className="table-sub">by {signup.reviewed_by}</span>
                    ) : null}
                  </td>
                  <td className="cell-actions">
                    {signup.status === "PENDING" ? (
                      <div className="button-row">
                        <Button size="sm" variant="primary" icon="check" onClick={() => setApproving(signup)}>
                          Approve
                        </Button>
                        <Button size="sm" variant="ghost" icon="x" onClick={() => setDeclining(signup)}>
                          Decline
                        </Button>
                      </div>
                    ) : (
                      <Badge tone={signup.status === "APPROVED" ? "success" : "muted"} dot>
                        {SIGNUP_STATUS_LABEL[signup.status]}
                      </Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {requests.data ? (
        <Pagination page={page} count={requests.data.count} onPage={setPage} />
      ) : null}

      {approving ? (
        <ApproveDialog
          key={approving.id}
          signup={approving}
          onClose={() => setApproving(null)}
          onApproved={(signup) => {
            setApproving(null);
            decided();
            toast(`Approved. ${signup.username} can sign in now.`);
          }}
        />
      ) : null}
      {declining ? (
        <DeclineDialog
          key={declining.id}
          signup={declining}
          onClose={() => setDeclining(null)}
          onDeclined={() => {
            setDeclining(null);
            decided();
            toast(`Request from ${declining.username} declined.`);
          }}
        />
      ) : null}
    </>
  );
}

export default function UsersPage() {
  useDocumentTitle("Users");
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "requests" ? "requests" : "accounts";
  const [editing, setEditing] = useState<Editing>(null);
  // For the tab's count. Its own call, so the number is right whichever
  // tab is open and whatever the requests tab is filtered to.
  const waiting = useResource(() => api.adminSignupRequests({ status: "PENDING" }), []);

  return (
    <div className="page">
      <PageHeader
        title="Users"
        description={
          can("administer_departments")
            ? "Every account on the platform, and requests for new ones."
            : "Accounts in your department. You can manage faculty, staff and students, and approve their sign-up requests."
        }
        actions={
          tab === "accounts" ? (
            <Button variant="primary" icon="plus" onClick={() => setEditing("new")}>
              New account
            </Button>
          ) : undefined
        }
      />
      {/* One block, as on a document's page: the panel's own padding
          separates it from the tabs, not the page's gap as well. Inside it,
          the toolbar, list and pagination need a gap of their own - the
          page's applies only to its direct children. */}
      <div>
        <Tabs
          label="Users"
          selected={tab}
          onSelect={(id) =>
            setParams(id === "requests" ? { tab: "requests" } : {}, { replace: true })
          }
          tabs={[
            { id: "accounts", label: "Accounts", icon: "users" },
            {
              id: "requests",
              label: "Sign-up requests",
              icon: "inbox",
              count: waiting.data?.count ? waiting.data.count : null,
            },
          ]}
        />
        <TabPanel id={tab}>
          <div className="stack-lg">
            {tab === "accounts" ? (
              <Accounts editing={editing} setEditing={setEditing} />
            ) : (
              <SignupRequests onDecided={waiting.reload} />
            )}
          </div>
        </TabPanel>
      </div>
    </div>
  );
}
