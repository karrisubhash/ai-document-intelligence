import { useState, type FormEvent } from "react";

import { ApiError, api } from "../../api/client";
import { useAction, useDebouncedValue, useResource } from "../../api/hooks";
import type { AdminUser, Role } from "../../api/types";
import { useAuth, useMe } from "../../auth/AuthContext";
import {
  Alert,
  Badge,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  PageHeader,
  Pagination,
  Skeleton,
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

export default function UsersPage() {
  useDocumentTitle("Users");
  const toast = useToast();
  const { can } = useAuth();
  const me = useMe();
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [role, setRole] = useState("");
  const [active, setActive] = useState("");
  const search = useDebouncedValue(searchInput.trim(), 300);
  const [editing, setEditing] = useState<AdminUser | "new" | null>(null);
  const [resetting, setResetting] = useState<AdminUser | null>(null);

  const users = useResource(
    () => api.adminUsers({ page, search, role, is_active: active }),
    [page, search, role, active],
  );

  return (
    <div className="page">
      <PageHeader
        title="Users"
        description={
          can("administer_departments")
            ? "Every account on the platform."
            : "Accounts in your department. You can manage faculty, staff and students."
        }
        actions={
          <Button variant="primary" onClick={() => setEditing("new")}>
            New account
          </Button>
        }
      />
      <div className="toolbar">
        <input
          type="search"
          className="input toolbar-search"
          placeholder="Search by name, username or email…"
          aria-label="Search users"
          value={searchInput}
          onChange={(event) => {
            setSearchInput(event.target.value);
            setPage(1);
          }}
        />
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
          <option value="">Active and inactive</option>
          <option value="true">Active</option>
          <option value="false">Inactive</option>
        </select>
      </div>

      {users.error && !users.data ? (
        <ErrorState error={users.error} onRetry={users.reload} />
      ) : users.loading && !users.data ? (
        <Skeleton lines={6} />
      ) : !users.data?.results.length ? (
        <EmptyState title="No accounts match" icon="◉" />
      ) : (
        <div className="table-wrap">
          <table className="table">
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
                    <span className="table-title">{displayName(user)}</span>
                    <span className="table-sub">
                      {user.username}
                      {user.email ? ` · ${user.email}` : ""}
                    </span>
                  </td>
                  <td>{ROLE_LABEL[user.role] ?? user.role}</td>
                  <td>{user.department || <span className="muted">—</span>}</td>
                  <td>
                    <Badge tone={user.is_active ? "success" : "muted"}>
                      {user.is_active ? "Active" : "Inactive"}
                    </Badge>
                  </td>
                  <td>{user.last_login ? formatRelative(user.last_login) : "Never"}</td>
                  <td>
                    {user.can_manage ? (
                      <div className="button-row">
                        <Button size="sm" onClick={() => setEditing(user)}>
                          Edit
                        </Button>
                        {/* Your own password is changed under Account, with
                            the current password - never here. */}
                        {user.id !== me.id ? (
                          <Button size="sm" variant="ghost" onClick={() => setResetting(user)}>
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
    </div>
  );
}
