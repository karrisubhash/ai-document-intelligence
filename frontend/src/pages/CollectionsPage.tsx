import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { useAction, useResource } from "../api/hooks";
import { collectionsApi } from "../api/collections";
import type { Collection } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Icon } from "../components/icons";
import {
  Alert,
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  IconButton,
  PageHeader,
  SelectBox,
  Skeleton,
  errorMessage,
  plural,
  useDocumentTitle,
  useToast,
} from "../components/ui";
import { pluralize } from "../lib/format";

/**
 * Collections (spec 14): named sets of documents that Search and Ask can be
 * narrowed to. A collection never widens access - the server applies each
 * reader's document permissions first and the collection after.
 */
function NewCollectionDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (collection: Collection) => void;
}) {
  const { can, me } = useAuth();
  // A system administrator says which department a collection is for, as
  // with an upload; everyone else's is their own.
  const anyDepartment = can("upload_to_any_department");
  const departments = useResource(() => api.departments(), [], { enabled: anyDepartment });
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

  // Checked on submit, as every other dialog's form is: the button is
  // always there to press, and pressing it says what is missing.
  const [missingName, setMissingName] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) {
      setMissingName(true);
      return;
    }
    const result = await create.run();
    if (result.ok) onCreated(result.value);
  };
  // Not closable while Create runs: closing would look like cancelling, and
  // the collection would be created regardless (as ConfirmDialog does).
  const close = () => {
    if (!create.pending) onClose();
  };

  return (
    <Dialog
      open
      onClose={close}
      title="New collection"
      description="A collection only ever narrows what Search and Ask look at. It never shows anyone a document they cannot already read."
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={create.pending}>
            Cancel
          </Button>
          <Button
            variant="primary"
            type="submit"
            form="collection-form"
            icon="plus"
            loading={create.pending}
          >
            Create
          </Button>
        </>
      }
    >
      <form id="collection-form" className="stack" onSubmit={submit} noValidate>
        <Field
          label="Name"
          error={
            errors.name ?? (missingName && !name.trim() ? "Give the collection a name." : null)
          }
          required
        >
          {(props) => (
            <input
              {...props}
              className="input"
              value={name}
              maxLength={120}
              placeholder="e.g. Exam rules"
              onChange={(event) => setName(event.target.value)}
            />
          )}
        </Field>
        <Field
          label="Description"
          error={errors.description}
          hint="Optional. Shown on the collection."
        >
          {(props) => (
            <textarea
              {...props}
              className="input"
              rows={2}
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
      </form>
    </Dialog>
  );
}

/** Rename a collection or change its description (the server audits it). */
function EditCollectionDialog({
  collection,
  onClose,
  onSaved,
}: {
  collection: Collection;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(collection.name);
  const [description, setDescription] = useState(collection.description);
  const save = useAction(() =>
    collectionsApi.update(collection.id, { name: name.trim(), description }),
  );
  const errors = save.error instanceof ApiError ? save.error.fieldErrors() : {};
  const [missingName, setMissingName] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) {
      setMissingName(true);
      return;
    }
    const result = await save.run();
    if (result.ok) {
      toast(`Collection “${result.value.name}” saved.`);
      onSaved();
    }
  };
  const close = () => {
    if (!save.pending) onClose();
  };

  return (
    <Dialog
      open
      onClose={close}
      title={`Edit ${collection.name}`}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={save.pending}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form="collection-edit-form" loading={save.pending}>
            Save
          </Button>
        </>
      }
    >
      <form id="collection-edit-form" className="stack" onSubmit={submit} noValidate>
        <Field
          label="Name"
          error={errors.name ?? (missingName && !name.trim() ? "Give the collection a name." : null)}
          required
        >
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
        <Field label="Description" error={errors.description} hint="Optional. Shown on the collection.">
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
        {save.error && !Object.keys(errors).length ? (
          <Alert tone="danger">{errorMessage(save.error)}</Alert>
        ) : null}
      </form>
    </Dialog>
  );
}

function CollectionCard({
  collection,
  selected,
  onSelect,
  onChanged,
  onDeleted,
}: {
  collection: Collection;
  selected: boolean;
  onSelect: (on: boolean) => void;
  onChanged: () => void;
  /** After this card's own delete: the page reloads and puts focus somewhere that stays. */
  onDeleted: () => void;
}) {
  const toast = useToast();
  // Members ticked for "Remove selected" - removal only, never deletion.
  // Their boxes show only in select mode: at rest, each row's own Remove is
  // enough, and every card's rows line up whatever its size.
  const [selectingMembers, setSelectingMembers] = useState(false);
  const [picked, setPicked] = useState<Set<number>>(() => new Set());
  // Candidates for "Add": the reader's documents in this collection's
  // department that are not members yet. The first page is enough for a
  // pilot-sized department; the server re-checks every id anyway.
  const candidates = useResource(
    () => api.documents({ department: collection.department }),
    [collection.id, collection.department],
    { enabled: collection.can_manage },
  );
  const [pick, setPick] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [editing, setEditing] = useState(false);
  const navigate = useNavigate();
  const addSelect = useRef<HTMLSelectElement>(null);
  const modeButton = useRef<HTMLButtonElement>(null);
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
    let done = 0;
    // The membership endpoint takes at most 500 ids a request.
    for (let start = 0; start < ids.length; start += 500) {
      const result = await change.run(ids.slice(start, start + 500), add);
      if (!result.ok) break;
      done = start + 500;
    }
    if (done > 0) {
      setPick("");
      if (!add) {
        const gone = new Set(ids.slice(0, done));
        setPicked((current) => new Set([...current].filter((id) => !gone.has(id))));
      }
      onChanged();
      // The control that was used goes away (a removed row's Remove, an
      // emptied "Remove selected", the Add button disabled again), and focus
      // with it. It moves to what stays: select mode's button while two or
      // more documents remain to select from, otherwise the Add picker.
      const remaining = collection.documents.length + (add ? 1 : -Math.min(done, ids.length));
      window.setTimeout(() => {
        const keepSelecting = !add && selectingMembers && remaining > 1;
        (keepSelecting ? modeButton.current : addSelect.current)?.focus();
      }, 0);
    }
  };
  // Only picks still in the collection count: membership can change
  // underneath (another curator, a deleted document).
  const pickedNow = collection.documents.filter((doc) => picked.has(doc.id)).map((doc) => doc.id);
  const allPicked =
    collection.documents.length > 0 && pickedNow.length === collection.documents.length;
  const selectable = collection.can_manage && collection.documents.length > 1;
  // Select mode ends when there is no longer anything to select from: it
  // must not come back, unasked, when documents are added again later.
  useEffect(() => {
    if (!selectable) {
      setSelectingMembers(false);
      setPicked(new Set());
    }
  }, [selectable]);
  const pickMember = (id: number, on: boolean) =>
    setPicked((current) => {
      const next = new Set(current);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  return (
    <Card
      leading={
        collection.can_manage ? (
          <SelectBox label={`Select ${collection.name}`} checked={selected} onChange={onSelect} />
        ) : null
      }
      icon="folder"
      className={`collection-card${selected ? " collection-card-selected" : ""}`}
      title={
        <>
          {collection.name}
          <Badge>{pluralize(collection.document_count, "document")}</Badge>
        </>
      }
      description={collection.department || undefined}
      actions={
        <>
          {collection.document_count ? (
            <>
              {/* The scope travels in history state, as quick find's does. */}
              <IconButton
                icon="search"
                size="sm"
                label={`Search in ${collection.name}`}
                onClick={() => navigate("/search", { state: { collectionId: collection.id } })}
              />
              <IconButton
                icon="sparkles"
                size="sm"
                label={`Ask about ${collection.name}`}
                onClick={() => navigate("/ask", { state: { collectionId: collection.id } })}
              />
            </>
          ) : null}
          {collection.can_manage ? (
            <>
              <IconButton
                icon="pencil"
                size="sm"
                label={`Edit ${collection.name}`}
                onClick={() => setEditing(true)}
              />
              <IconButton
                icon="trash"
                size="sm"
                label={`Delete ${collection.name}`}
                onClick={() => {
                  remove.clearError();
                  setConfirmingDelete(true);
                }}
              />
            </>
          ) : null}
        </>
      }
    >
      {editing ? (
        <EditCollectionDialog
          collection={collection}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
        />
      ) : null}
      {collection.description ? (
        <p className="collection-description">{collection.description}</p>
      ) : null}
      {selectable ? (
        <div className={`member-select-bar${selectingMembers ? " member-select-bar-on" : ""}`}>
          {selectingMembers ? (
            <>
              <SelectBox
                label={`Select all documents in ${collection.name}`}
                checked={allPicked}
                indeterminate={pickedNow.length > 0 && !allPicked}
                onChange={(on) =>
                  setPicked(on ? new Set(collection.documents.map((doc) => doc.id)) : new Set())
                }
              />
              <span className="small muted" aria-live="polite">
                {pickedNow.length ? `${pickedNow.length} selected` : "Select documents to remove"}
              </span>
              {pickedNow.length ? (
                <Button
                  variant="ghost"
                  size="sm"
                  icon="x"
                  loading={change.pending}
                  onClick={() => void update(pickedNow, false)}
                >
                  Remove selected
                </Button>
              ) : null}
            </>
          ) : (
            <span className="member-list-label">Documents</span>
          )}
          {/* One button for both, so focus stays on it as the mode flips. */}
          <Button
            ref={modeButton}
            variant="ghost"
            size="sm"
            icon={selectingMembers ? "check" : "list-checks"}
            className="member-select-mode"
            aria-label={
              selectingMembers
                ? `Done selecting documents in ${collection.name}`
                : `Select documents in ${collection.name}`
            }
            onClick={() => {
              setSelectingMembers((on) => !on);
              setPicked(new Set());
            }}
          >
            {selectingMembers ? "Done" : "Select"}
          </Button>
        </div>
      ) : null}
      {collection.documents.length ? (
        <ul className="row-list collection-docs">
          {collection.documents.map((doc) => (
            <li key={doc.id} className="row-item">
              {selectable && selectingMembers ? (
                <SelectBox
                  label={`Select ${doc.title} in ${collection.name}`}
                  checked={picked.has(doc.id)}
                  onChange={(on) => pickMember(doc.id, on)}
                />
              ) : null}
              <Link to={`/documents/${doc.id}`} className="row-link">
                <span className="icon-tile" aria-hidden="true">
                  <Icon name="file-text" />
                </span>
                <span className="row-text">
                  <span className="row-title">{doc.title}</span>
                </span>
              </Link>
              {collection.can_manage ? (
                <Button variant="ghost" size="sm" onClick={() => void update([doc.id], false)}>
                  Remove
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="collection-empty">
          <Icon name="inbox" size={16} />
          {collection.can_manage
            ? "No documents yet. Add the first one below."
            : "No documents yet."}
        </p>
      )}
      {change.error ? <Alert tone="danger">{errorMessage(change.error)}</Alert> : null}
      {collection.can_manage ? (
        <div className="collection-add">
          <select
            ref={addSelect}
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
          <Button
            icon="plus"
            disabled={!pick}
            loading={change.pending}
            onClick={() => void update([Number(pick)], true)}
          >
            Add
          </Button>
        </div>
      ) : null}

      <ConfirmDialog
        open={confirmingDelete}
        title={`Delete “${collection.name}”?`}
        confirmLabel="Delete collection"
        pending={remove.pending}
        error={remove.error}
        onClose={() => setConfirmingDelete(false)}
        onConfirm={async () => {
          const result = await remove.run();
          if (result.ok) {
            setConfirmingDelete(false);
            toast(`Deleted “${collection.name}”.`, "success");
            onDeleted();
          }
        }}
      >
        <p>
          The collection is removed.{" "}
          {collection.document_count === 0
            ? "It holds no documents."
            : collection.document_count === 1
              ? "Its document is not deleted and stays in Documents, Search and Ask."
              : `Its ${collection.document_count.toLocaleString()} documents are not deleted and stay in Documents, Search and Ask.`}
        </p>
      </ConfirmDialog>
    </Card>
  );
}

export default function CollectionsPage() {
  useDocumentTitle("Collections");
  const { can } = useAuth();
  const collections = useResource(() => api.collections(), []);
  const [creating, setCreating] = useState(false);
  const newButton = useRef<HTMLButtonElement>(null);
  // Where focus goes back to when the dialog closes: the button that opened
  // it (the header's, or the empty state's).
  const opener = useRef<HTMLElement | null>(null);
  const openCreate = () => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setCreating(true);
  };
  // Deferred: until the dialog has gone, the page behind the modal is inert
  // and a focus() call there is ignored.
  const focusLater = (target: () => HTMLElement | null | undefined) =>
    window.setTimeout(() => (target() ?? document.getElementById("main"))?.focus(), 0);

  const list = collections.data?.results ?? [];

  // Collections ticked for deletion. Every collection is loaded at once
  // (api.collections pages through them), so "all" is simply every one
  // shown that the user may delete.
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const toast = useToast();
  const [deleteError, setDeleteError] = useState<unknown>(null);
  const selectAllBox = useRef<HTMLInputElement>(null);
  const deletable = list.filter((collection) => collection.can_manage);
  const chosen = deletable.filter((collection) => selected.has(collection.id));
  const everyOne = chosen.length > 1 && chosen.length === list.length;
  const select = (id: number, on: boolean) =>
    setSelected((current) => {
      const next = new Set(current);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  const deleteChosen = async () => {
    setDeleting(true);
    setDeleteError(null);
    const ids = chosen.map((collection) => collection.id);
    const answered: number[] = [];
    let deleted = 0;
    let refused = 0;
    try {
      for (let start = 0; start < ids.length; start += 100) {
        const result = await api.bulkDeleteCollections({ ids: ids.slice(start, start + 100) });
        const forbidden = result.forbidden ?? [];
        answered.push(...result.deleted, ...result.not_found, ...forbidden);
        deleted += result.deleted.length;
        refused += forbidden.length;
      }
      const left = refused
        ? ` ${refused} you may not delete ${refused === 1 ? "was" : "were"} left in place.`
        : "";
      toast(
        `Deleted ${deleted} ${plural(deleted, "collection")}.${left}`,
        refused ? "warning" : "success",
      );
      setConfirmingDelete(false);
      window.setTimeout(() => selectAllBox.current?.focus(), 0);
    } catch (caught) {
      // Shown in the dialog: a toast would sit under its backdrop.
      setDeleteError(caught);
    } finally {
      setDeleting(false);
      setSelected((current) => new Set([...current].filter((id) => !answered.includes(id))));
      if (answered.length) collections.reload();
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Collections"
        description="Named sets of documents. Choose one in Search or Ask to work within it."
        actions={
          can("create_collections") ? (
            <Button ref={newButton} variant="primary" icon="folder-plus" onClick={openCreate}>
              New collection
            </Button>
          ) : null
        }
      />

      {collections.error ? <ErrorState error={collections.error} title="Could not load collections" /> : null}
      {collections.loading && !collections.data ? (
        <div className="collections-grid">
          {[0, 1].map((index) => (
            <div key={index} className="card card-body">
              <Skeleton lines={4} />
            </div>
          ))}
        </div>
      ) : null}
      {collections.data && !list.length ? (
        <div className="card">
          <EmptyState
            title="No collections yet"
            icon="folder"
            action={
              can("create_collections") ? (
                <Button variant="primary" icon="folder-plus" onClick={openCreate}>
                  New collection
                </Button>
              ) : null
            }
          >
            {can("create_collections")
              ? "Group the documents that belong together, such as every exam rule, so Search and Ask can work within them."
              : "A faculty member or department administrator can create one."}
          </EmptyState>
        </div>
      ) : null}
      {deletable.length > 1 || chosen.length > 0 ? (
        <div
          className={chosen.length ? "bulk-bar" : "bulk-bar bulk-bar-idle"}
          role="region"
          aria-label="Selected collections"
        >
          <SelectBox
            inputRef={selectAllBox}
            label={
              deletable.length < list.length
                ? "Select all collections you can delete"
                : "Select all collections"
            }
            checked={chosen.length > 0 && chosen.length === deletable.length}
            indeterminate={chosen.length > 0 && chosen.length < deletable.length}
            onChange={(on) =>
              setSelected(on ? new Set(deletable.map((collection) => collection.id)) : new Set())
            }
          />
          <span className="bulk-bar-count" aria-live="polite">
            {chosen.length ? `${chosen.length} selected` : "Select collections to delete"}
          </span>
          {chosen.length ? (
            <>
              <Button
                variant="ghost"
                size="sm"
                icon="x"
                onClick={() => {
                  setSelected(new Set());
                  selectAllBox.current?.focus();
                }}
              >
                Clear selection
              </Button>
              <Button
                variant="danger"
                size="sm"
                icon="trash"
                className="bulk-bar-action"
                onClick={() => {
                  setDeleteError(null);
                  setConfirmingDelete(true);
                }}
              >
                {/* "All" only when it is all: not when some are someone else's. */}
                {everyOne ? "Delete all" : "Delete selected"}
              </Button>
            </>
          ) : null}
        </div>
      ) : null}
      {list.length ? (
        <div className="collections-grid">
          {list.map((collection) => (
            <CollectionCard
              key={collection.id}
              collection={collection}
              selected={selected.has(collection.id)}
              onSelect={(on) => select(collection.id, on)}
              onChanged={collections.reload}
              onDeleted={() => {
                collections.reload();
                // Its card - and the trash button the dialog would return
                // focus to - is about to go.
                focusLater(() => newButton.current);
              }}
            />
          ))}
        </div>
      ) : null}

      {creating ? (
        <NewCollectionDialog
          onClose={() => {
            setCreating(false);
            focusLater(() => (opener.current?.isConnected ? opener.current : newButton.current));
          }}
          onCreated={(collection) => {
            setCreating(false);
            // Not the empty state's button: the new collection replaces it.
            focusLater(() => newButton.current);
            toast(`Created “${collection.name}”. Add documents to it below.`, "success");
            collections.reload();
          }}
        />
      ) : null}

      <ConfirmDialog
        open={confirmingDelete}
        title={`Delete ${chosen.length} ${plural(chosen.length, "collection")}?`}
        confirmLabel="Delete permanently"
        pending={deleting}
        error={deleteError}
        onClose={() => setConfirmingDelete(false)}
        onConfirm={() => void deleteChosen()}
      >
        <p>
          {chosen.length === 1 ? "The collection is" : "The collections are"} removed. Their
          documents are not deleted and stay in Documents, Search and Ask.
        </p>
      </ConfirmDialog>
    </div>
  );
}
