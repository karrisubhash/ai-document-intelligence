import { useState, type FormEvent } from "react";

import { ApiError, api } from "../api/client";
import { useAction } from "../api/hooks";
import { useMe } from "../auth/AuthContext";
import { Icon } from "../components/icons";
import {
  Alert,
  Avatar,
  Button,
  Card,
  DescriptionList,
  Field,
  PageHeader,
  errorMessage,
  useDocumentTitle,
  useToast,
} from "../components/ui";
import { displayName } from "../lib/format";
import { ROLE_LABEL } from "../lib/labels";

export default function AccountPage() {
  useDocumentTitle("Account");
  const me = useMe();
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [mismatch, setMismatch] = useState(false);
  const change = useAction(api.changePassword);
  const fieldErrors = change.error instanceof ApiError ? change.error.fieldErrors() : {};

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (next !== confirm) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    const result = await change.run(current, next);
    if (result.ok) {
      setCurrent("");
      setNext("");
      setConfirm("");
      toast("Password changed. Other signed-in sessions have been ended.");
    }
  };

  return (
    <div className="page page-narrow">
      <PageHeader title="Account" description="Your profile and sign-in details." />
      <Card title="Profile" icon="user">
        <div className="profile">
          <Avatar name={displayName(me)} size="lg" />
          <div>
            <p className="profile-name">{displayName(me)}</p>
            <p className="profile-meta">
              {me.role ? ROLE_LABEL[me.role] : "No role"}
              {me.department ? ` · ${me.department}` : ""}
            </p>
          </div>
        </div>
        <DescriptionList
          items={[
            ["Name", displayName(me)],
            ["Username", me.username],
            ["Email", me.email || "—"],
            ["Role", me.role ? ROLE_LABEL[me.role] : "—"],
            ["Department", me.department || "None assigned"],
          ]}
        />
        <p className="panel-note">
          <Icon name="shield" size={14} />
          Your role and department decide which documents you can see. Only an administrator
          can change them.
        </p>
      </Card>

      {me.capabilities?.is_guest ? (
        // A demo account has no password to change: it is reached only
        // through the session the demo started, and it is cleared after a
        // day.
        <Card title="Demo account" icon="sparkles">
          <p className="guest-note">
            <Icon name="lock" size={16} />
            This account was made for your visit to the demo. It has no password, can read but
            not change anything, and is deleted with its conversations after a day.
          </p>
        </Card>
      ) : (
      <Card
        title="Change password"
        icon="key"
        description="Changing it signs you out everywhere else."
      >
        <form className="stack" onSubmit={submit} noValidate>
          <Field label="Current password" error={fieldErrors.current_password} required>
            {(props) => (
              <input
                {...props}
                className="input"
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(event) => setCurrent(event.target.value)}
              />
            )}
          </Field>
          <Field
            label="New password"
            error={fieldErrors.new_password}
            hint="At least 8 characters, not entirely numbers, and not similar to your username."
            required
          >
            {(props) => (
              <input
                {...props}
                className="input"
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(event) => setNext(event.target.value)}
              />
            )}
          </Field>
          <Field
            label="Confirm new password"
            error={mismatch ? "The two new passwords do not match." : null}
            required
          >
            {(props) => (
              <input
                {...props}
                className="input"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
              />
            )}
          </Field>
          {change.error && !Object.keys(fieldErrors).length ? (
            <Alert tone="danger">{errorMessage(change.error)}</Alert>
          ) : null}
          <div className="form-actions">
            <Button
              type="submit"
              variant="primary"
              loading={change.pending}
              disabled={!current || !next || !confirm}
            >
              Change password
            </Button>
          </div>
        </form>
      </Card>
      )}
    </div>
  );
}
