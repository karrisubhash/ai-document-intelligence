import { useState, type FormEvent } from "react";

import { ApiError, api } from "../../api/client";
import { useAction, useResource } from "../../api/hooks";
import type { AdminDepartment } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { Icon } from "../../components/icons";
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

function DepartmentDialog({
  department,
  onClose,
  onSaved,
}: {
  department: AdminDepartment | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(department?.name ?? "");
  const [code, setCode] = useState(department?.code ?? "");
  const [description, setDescription] = useState(department?.description ?? "");
  const [active, setActive] = useState(department?.is_active ?? true);
  const save = useAction(() => {
    const body = { name: name.trim(), code: code.trim(), description, is_active: active };
    return department
      ? api.adminUpdateDepartment(department.id, body)
      : api.adminCreateDepartment(body);
  });
  const errors = save.error instanceof ApiError ? save.error.fieldErrors() : {};
  const renaming = Boolean(department && name.trim() && name.trim() !== department.name);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const result = await save.run();
    if (result.ok) onSaved();
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={department ? `Edit ${department.name}` : "New department"}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="department-form" loading={save.pending}>
            Save
          </Button>
        </>
      }
    >
      <form id="department-form" className="stack" onSubmit={submit} noValidate>
        <Field label="Name" error={errors.name} required>
          {(props) => (
            <input
              {...props}
              className="input"
              value={name}
              maxLength={255}
              onChange={(event) => setName(event.target.value)}
            />
          )}
        </Field>
        {renaming ? (
          <Alert tone="info">
            Renaming moves every member and document of “{department?.name}” along with it,
            in one step. Nobody loses access.
          </Alert>
        ) : null}
        <Field label="Short code" error={errors.code} hint="Optional, e.g. CSE.">
          {(props) => (
            <input
              {...props}
              className="input"
              value={code}
              maxLength={20}
              onChange={(event) => setCode(event.target.value)}
            />
          )}
        </Field>
        <Field label="Description" error={errors.description}>
          {(props) => (
            <textarea
              {...props}
              className="input"
              rows={3}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          )}
        </Field>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={active}
            onChange={(event) => setActive(event.target.checked)}
          />
          Active — accepts new members and documents
        </label>
        {save.error && !Object.keys(errors).length ? (
          <Alert tone="danger">{errorMessage(save.error)}</Alert>
        ) : null}
      </form>
    </Dialog>
  );
}

export default function DepartmentsPage() {
  useDocumentTitle("Departments");
  const toast = useToast();
  const { can } = useAuth();
  const editable = can("administer_departments");
  // Paged like every other admin list; only the first 50 were ever loaded.
  const [page, setPage] = useState(1);
  const departments = useResource(() => api.adminDepartments(page), [page]);
  const [editing, setEditing] = useState<AdminDepartment | "new" | null>(null);

  return (
    <div className="page">
      <PageHeader
        title="Departments"
        description="Departments are the boundary for document access: members read their department's documents and nobody else's."
        actions={
          editable ? (
            <Button variant="primary" icon="plus" onClick={() => setEditing("new")}>
              New department
            </Button>
          ) : null
        }
      />
      {departments.error ? (
        <ErrorState error={departments.error} onRetry={departments.reload} />
      ) : departments.loading && !departments.data ? (
        <Skeleton lines={5} />
      ) : !departments.data?.results.length ? (
        <div className="card">
          <EmptyState title="No departments yet" icon="building">
            Create departments before assigning people to them.
          </EmptyState>
        </div>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Department</th>
                <th scope="col" className="num">
                  Members
                </th>
                <th scope="col" className="num">
                  Documents
                </th>
                <th scope="col">Status</th>
                {editable ? (
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                ) : null}
              </tr>
            </thead>
            <tbody>
              {departments.data.results.map((department) => (
                <tr key={department.id}>
                  <td>
                    <div className="cell-title">
                      <span className="icon-tile" aria-hidden="true">
                        <Icon name="building" />
                      </span>
                      <div className="cell-title-text">
                        <span className="table-title">
                          {department.name}{" "}
                          {department.code ? <span className="code-chip">{department.code}</span> : null}
                        </span>
                        {department.description ? (
                          <span className="table-sub">{department.description}</span>
                        ) : null}
                      </div>
                    </div>
                  </td>
                  <td className="num">{department.member_count ?? "—"}</td>
                  <td className="num">{department.document_count ?? "—"}</td>
                  <td>
                    <Badge tone={department.is_active ? "success" : "muted"} dot>
                      {department.is_active ? "Active" : "Inactive"}
                    </Badge>
                  </td>
                  {editable ? (
                    <td className="cell-actions">
                      <Button size="sm" icon="pencil" onClick={() => setEditing(department)}>
                        Edit
                      </Button>
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {departments.data ? (
        <Pagination page={page} count={departments.data.count} onPage={setPage} />
      ) : null}
      {editing ? (
        <DepartmentDialog
          department={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            departments.reload();
            toast("Department saved.");
          }}
        />
      ) : null}
    </div>
  );
}
