import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import type { Collection } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  PageHeader,
  Skeleton,
  errorMessage,
  useDocumentTitle,
  useToast,
} from "../components/ui";

/**
 * Collections (spec 14): named sets of documents that Search and Ask can be
 * narrowed to. A collection never widens access - the server applies each
 * reader's document permissions first and the collection after.
 */
function CollectionCard({ collection, onChanged }: { collection: Collection; onChanged: () => void }) {
  const toast = useToast();
  // Candidates for "Add": the reader's documents in this collection's
  // department that are not members yet. The first page is enough for a
  // pilot-sized department; the server re-checks every id anyway.
  const candidates = useResource(
    () => api.documents({ department: collection.department }),
    [collection.id, collection.department],
    { enabled: collection.can_manage },
  );
  const [pick, setPick] = useState("");
  const change = useAction((ids: number[], add: boolean) =>
    api.setCollectionDocuments(collection.id, ids, add),
  );
  const remove = useAction(() => api.deleteCollection(collection.id));
  const members = new Set(collection.documents.map((doc) => doc.id));
  // The department filter is checked here as well: an empty department is
  // dropped from the query string, which then lists every department's
  // documents - each of which the server would refuse.
  const addable = (candidates.data?.results ?? []).filter(
    (doc) => doc.department === collection.department && !members.has(doc.id),
  );

  const update = async (ids: number[], add: boolean) => {
    const result = await change.run(ids, add);
    if (result.ok) {
      setPick("");
      onChanged();
    }
  };

  return (
    <Card
      title={
        <>
          {collection.name} <Badge>{collection.document_count} documents</Badge>
        </>
      }
      actions={
        collection.can_manage ? (
          <Button
            variant="ghost"
            loading={remove.pending}
            onClick={async () => {
              if (!window.confirm(`Delete “${collection.name}”? Its documents are not deleted.`)) return;
              const result = await remove.run();
              if (result.ok) {
                toast(`Deleted “${collection.name}”`);
                onChanged();
              }
            }}
          >
            Delete
          </Button>
        ) : null
      }
    >
      {collection.description ? <p className="muted">{collection.description}</p> : null}
      {collection.documents.length ? (
        <ul className="row-list">
          {collection.documents.map((doc) => (
            <li key={doc.id} className="row-item">
              <Link to={`/documents/${doc.id}`}>{doc.title}</Link>
              {collection.can_manage ? (
                <Button variant="ghost" size="sm" onClick={() => void update([doc.id], false)}>
                  Remove
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No documents yet.</p>
      )}
      {collection.can_manage ? (
        <div className="collection-add">
          <select
            className="input"
            aria-label={`Add a document to ${collection.name}`}
            value={pick}
            onChange={(event) => setPick(event.target.value)}
          >
            <option value="">Add a document…</option>
            {addable.map((doc) => (
              <option key={doc.id} value={doc.id}>
                {doc.title}
              </option>
            ))}
          </select>
          <Button disabled={!pick} loading={change.pending} onClick={() => void update([Number(pick)], true)}>
            Add
          </Button>
        </div>
      ) : null}
      {change.error || remove.error ? (
        <Alert tone="danger">{errorMessage(change.error ?? remove.error)}</Alert>
      ) : null}
    </Card>
  );
}

export default function CollectionsPage() {
  useDocumentTitle("Collections");
  const { can, me } = useAuth();
  // A system administrator says which department a collection is for, as
  // with an upload; everyone else's is their own.
  const anyDepartment = can("upload_to_any_department");
  const departments = useResource(() => api.departments(), [], { enabled: anyDepartment });
  const collections = useResource(() => api.collections(), []);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [department, setDepartment] = useState("");
  const create = useAction(() =>
    api.createCollection({
      name: name.trim(),
      description,
      ...(anyDepartment && department ? { department } : {}),
    }),
  );
  const errors = create.error instanceof ApiError ? create.error.fieldErrors() : {};

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const result = await create.run();
    if (result.ok) {
      setName("");
      setDescription("");
      setDepartment("");
      collections.reload();
    }
  };

  const list = collections.data?.results ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Collections"
        description="Named sets of documents. Choose one in Search or Ask to work within it."
      />

      {can("create_collections") ? (
        <Card title="New collection">
          <form className="stack" onSubmit={submit} noValidate>
            <Field label="Name" error={errors.name} required>
              {(props) => (
                <input
                  {...props}
                  className="input"
                  value={name}
                  maxLength={120}
                  onChange={(event) => setName(event.target.value)}
                />
              )}
            </Field>
            <Field label="Description" error={errors.description}>
              {(props) => (
                <input
                  {...props}
                  className="input"
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              )}
            </Field>
            {anyDepartment ? (
              <Field
                label="Department"
                error={errors.department}
                hint="Who can see it. It holds only that department's documents."
              >
                {(props) => (
                  <select
                    {...props}
                    className="input"
                    value={department}
                    onChange={(event) => setDepartment(event.target.value)}
                  >
                    <option value="">
                      {me?.department ? `Your own (${me.department})` : "Choose a department…"}
                    </option>
                    {(departments.data ?? []).map((item) => (
                      <option key={item.id} value={item.name}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                )}
              </Field>
            ) : null}
            {create.error && !Object.keys(errors).length ? (
              <Alert tone="danger">{errorMessage(create.error)}</Alert>
            ) : null}
            <div>
              <Button type="submit" variant="primary" loading={create.pending} disabled={!name.trim()}>
                Create
              </Button>
            </div>
          </form>
        </Card>
      ) : null}

      {collections.error ? <ErrorState error={collections.error} title="Could not load collections" /> : null}
      {collections.loading && !collections.data ? <Skeleton lines={4} /> : null}
      {collections.data && !list.length ? (
        <EmptyState title="No collections yet">
          {can("create_collections")
            ? "Create one above, then add documents to it."
            : "A faculty member or department administrator can create one."}
        </EmptyState>
      ) : null}
      <div className="stack">
        {list.map((collection) => (
          <CollectionCard key={collection.id} collection={collection} onChanged={collections.reload} />
        ))}
      </div>
    </div>
  );
}
