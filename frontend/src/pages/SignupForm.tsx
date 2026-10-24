import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { useAction } from "../api/hooks";
import type { Role, SignupOptions } from "../api/types";
import { Icon } from "../components/icons";
import { Alert, Button, Field, errorMessage, useDocumentTitle } from "../components/ui";

/**
 * "My department is not listed" is a real answer - the request goes to a
 * system administrator, who can place it anywhere - and has to be told
 * apart from not having chosen yet, which the empty value means.
 */
const NOT_LISTED = "__not_listed__";

type Draft = {
  first_name: string;
  last_name: string;
  email: string;
  username: string;
  department: string;
  role: Role;
  password: string;
  confirm: string;
  note: string;
};

function sendError(error: unknown): string {
  if (error instanceof ApiError && error.status === 0) {
    return "The server could not be reached. Check your connection and try again.";
  }
  // The per-address throttle, as opposed to the full-queue 429, which
  // explains itself.
  if (error instanceof ApiError && error.status === 429 && /throttled/i.test(error.message)) {
    return "Too many requests have come from your network. Try again later.";
  }
  return errorMessage(error);
}

function Sent({ username }: { username: string }) {
  const heading = useRef<HTMLHeadingElement>(null);
  // The form the focus was in has gone; the outcome is what to read next.
  useEffect(() => heading.current?.focus(), []);
  return (
    <div className="signup-sent" role="status">
      <span className="signup-sent-icon" aria-hidden="true">
        <Icon name="check-circle" size={24} />
      </span>
      <h1 className="login-title" ref={heading} tabIndex={-1}>
        Request sent
      </h1>
      <p className="login-subtitle">
        An administrator will review it. No email will come: to see whether it has been
        approved, sign in as <strong>{username}</strong> with the password you chose. If it
        is still waiting after a few days, contact your department office.
      </p>
      {/* Replacing this page, so Back does not return to a filled-in form
          that would only be refused: the name is already taken - by this
          request. */}
      <Link to="/" replace className="btn btn-primary btn-lg btn-block">
        Back to sign in
      </Link>
    </div>
  );
}

export default function SignupForm({ options }: { options: SignupOptions }) {
  useDocumentTitle("Request an account");
  const [draft, setDraft] = useState<Draft>({
    first_name: "",
    last_name: "",
    email: "",
    username: "",
    department: "",
    role: options.roles.some((role) => role.value === "STUDENT")
      ? "STUDENT"
      : (options.roles[0]?.value ?? "STUDENT"),
    password: "",
    confirm: "",
    note: "",
  });
  const [problems, setProblems] = useState<Record<string, string>>({});
  const [sentAs, setSentAs] = useState<string | null>(null);
  // Bumped by each attempt that fails, so the effect below runs once the
  // errors it caused have rendered.
  const [failures, setFailures] = useState(0);
  const form = useRef<HTMLFormElement>(null);
  const submitButton = useRef<HTMLButtonElement>(null);
  const domains = options.email_domains ?? [];
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const send = useAction(() =>
    api.signup({
      first_name: draft.first_name.trim(),
      last_name: draft.last_name.trim(),
      email: draft.email.trim(),
      username: draft.username.trim(),
      department: draft.department === NOT_LISTED ? "" : draft.department,
      role: draft.role,
      password: draft.password,
      note: draft.note.trim(),
    }),
  );
  const server = send.error instanceof ApiError ? send.error.fieldErrors() : {};
  const errors: Record<string, string | undefined> = { ...server, ...problems };
  const fieldless =
    send.error && !Object.keys(server).some((key) => key !== "non_field_errors");

  // After a failed attempt, to the first field it was about: on a phone
  // that field may be scrolled out of sight, and a screen reader reads its
  // label and error from there. Otherwise back to the button - it disabled
  // itself while sending, which dropped focus to the page.
  useEffect(() => {
    if (!failures) return;
    const invalid = form.current?.querySelector<HTMLElement>('[aria-invalid="true"]');
    (invalid ?? submitButton.current)?.focus();
  }, [failures]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const found: Record<string, string> = {};
    if (!draft.first_name.trim()) found.first_name = "Enter your first name.";
    if (!draft.email.trim()) found.email = "Enter your email address.";
    if (!draft.username.trim()) found.username = "Choose a username.";
    if (options.departments.length && !draft.department) {
      found.department = "Choose your department, or “My department is not listed”.";
    }
    if (!draft.password) found.password = "Choose a password.";
    else if (draft.password !== draft.confirm) found.confirm = "The passwords do not match.";
    setProblems(found);
    if (Object.keys(found).length) {
      setFailures((count) => count + 1);
      return;
    }
    const result = await send.run();
    if (result.ok) {
      setSentAs(draft.username.trim());
      return;
    }
    // Typed again only when the password itself was the problem - not for
    // a taken username or a dropped connection.
    if (result.error instanceof ApiError && result.error.fieldErrors().password) {
      setDraft((current) => ({ ...current, password: "", confirm: "" }));
    }
    setFailures((count) => count + 1);
  };

  if (sentAs) return <Sent username={sentAs} />;

  return (
    <>
      <h1 className="login-title">Request an account</h1>
      <p className="login-subtitle">
        An administrator reviews every request. You can sign in once yours is approved.
      </p>

      <form ref={form} className="stack" onSubmit={submit} noValidate>
        <div className="form-row">
          <Field label="First name" error={errors.first_name} required>
            {(props) => (
              <input
                {...props}
                className="input"
                autoComplete="given-name"
                autoFocus
                maxLength={150}
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
                autoComplete="family-name"
                maxLength={150}
                value={draft.last_name}
                onChange={(event) => set("last_name", event.target.value)}
              />
            )}
          </Field>
        </div>
        <Field
          label="Email"
          error={errors.email}
          hint={
            domains.length
              ? `An address at ${domains.join(" or ")}.`
              : "Administrators see it when they review your request."
          }
          required
        >
          {(props) => (
            <input
              {...props}
              type="email"
              className="input"
              autoComplete="email"
              maxLength={254}
              value={draft.email}
              onChange={(event) => set("email", event.target.value)}
            />
          )}
        </Field>
        <Field
          label="Username"
          error={errors.username}
          hint="You will sign in with this, exactly as typed. English letters, digits and @ . + - _ only."
          required
        >
          {(props) => (
            <input
              {...props}
              className="input"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={150}
              value={draft.username}
              onChange={(event) => set("username", event.target.value)}
            />
          )}
        </Field>
        <div className="form-row">
          {options.departments.length ? (
            <Field label="Department" error={errors.department} required>
              {(props) => (
                <select
                  {...props}
                  className="input"
                  value={draft.department}
                  onChange={(event) => set("department", event.target.value)}
                >
                  <option value="" disabled>
                    Choose…
                  </option>
                  {options.departments.map((department) => (
                    <option key={department.id} value={department.name}>
                      {department.name}
                    </option>
                  ))}
                  <option value={NOT_LISTED}>My department is not listed</option>
                </select>
              )}
            </Field>
          ) : null}
          <Field label="I am" error={errors.role}>
            {(props) => (
              <select
                {...props}
                className="input"
                value={draft.role}
                onChange={(event) => set("role", event.target.value as Role)}
              >
                {options.roles.map((role) => (
                  <option key={role.value} value={role.value}>
                    {role.label}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>
        <div className="form-row">
          <Field
            label="Password"
            error={errors.password}
            hint="At least 8 characters, not only digits, and unlike your name."
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
          <Field label="Confirm password" error={errors.confirm} required>
            {(props) => (
              <input
                {...props}
                type="password"
                className="input"
                autoComplete="new-password"
                value={draft.confirm}
                onChange={(event) => set("confirm", event.target.value)}
              />
            )}
          </Field>
        </div>
        <Field
          label="Note for the administrator"
          error={errors.note}
          hint={
            draft.department === NOT_LISTED
              ? "Say which department you belong to, so an administrator can place you."
              : "Optional. Anything that helps them recognise you, such as a student or staff number."
          }
        >
          {(props) => (
            <textarea
              {...props}
              className="input"
              rows={2}
              maxLength={500}
              value={draft.note}
              onChange={(event) => set("note", event.target.value)}
            />
          )}
        </Field>
        {errors.non_field_errors || fieldless ? (
          <Alert tone="danger">{errors.non_field_errors || sendError(send.error)}</Alert>
        ) : null}
        <Button
          ref={submitButton}
          type="submit"
          variant="primary"
          size="lg"
          loading={send.pending}
          className="btn-block"
        >
          Send request
        </Button>
      </form>
      <p className="login-switch">
        Already have an account?{" "}
        <Link to="/" className="link">
          Sign in
        </Link>
      </p>
    </>
  );
}
