import { useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { BrandMark, Icon, type IconName } from "../components/icons";
import { Alert, Button, Field, errorMessage } from "../components/ui";

const FEATURES: [IconName, string, string][] = [
  [
    "quote",
    "Answers with their sources",
    "Every answer links to the passage it came from - or says plainly that your documents do not cover it.",
  ],
  [
    "search",
    "Search by wording and by meaning",
    "Find a rule, a figure or a reference number across every document you are allowed to read.",
  ],
  [
    "shield",
    "Access stays where it belongs",
    "Your department decides what you can see, and every view and change is recorded in the audit log.",
  ],
];

/**
 * A cited answer and the page it came from, drawn without words: what the
 * product does, not a claim about any policy. The passage the answer cites
 * is highlighted on the page behind it.
 */
function Illustration() {
  return (
    <div className="login-illustration" aria-hidden="true">
      <div className="login-sheet">
        <div className="login-sheet-head">
          <Icon name="file-text" size={13} />
          <span className="login-sheet-title" />
          <span className="login-sheet-page">p. 12</span>
        </div>
        <span className="login-sheet-line" />
        <span className="login-sheet-line" />
        <span className="login-sheet-line login-sheet-hit" />
        <span className="login-sheet-line login-sheet-hit" />
        <span className="login-sheet-line" />
        <span className="login-sheet-line" />
        <span className="login-sheet-line" />
      </div>
      <div className="login-answer">
        <span className="login-illustration-question" />
        <div className="login-illustration-answer">
          <BrandMark size="sm" />
          <div className="login-illustration-lines">
            <span className="login-illustration-bar" />
            <span className="login-illustration-row">
              <span className="login-illustration-bar" />
              <span className="login-illustration-cite">1</span>
            </span>
            <span className="login-illustration-bar login-illustration-bar-short" />
          </div>
        </div>
        <div className="login-illustration-source">
          <Icon name="file-text" size={14} />
          <span>Source 1 · page 12</span>
          <Icon name="arrow-up-right" size={13} className="login-illustration-open" />
        </div>
      </div>
    </div>
  );
}

export default function LoginPage() {
  const { login, expired } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await login(username.trim(), password);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 429) {
        setError("Too many sign-in attempts. Wait a minute before trying again.");
      } else if (caught instanceof ApiError && caught.status === 400) {
        setError("That username and password do not match an active account.");
      } else if (caught instanceof ApiError && caught.status === 0) {
        setError("The server could not be reached. Check your connection and try again.");
      } else {
        setError(errorMessage(caught));
      }
      setPassword("");
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="login">
      <div className="login-aside">
        <div className="brand">
          <BrandMark />
          <span className="brand-name">Document Intelligence</span>
        </div>
        <div className="login-pitch">
          <p className="login-headline">Answers you can trace back to the page.</p>
          <p className="login-lead">
            Search, read and ask questions of your institution's regulations, policies,
            circulars and forms.
          </p>
          <ul className="login-features">
            {FEATURES.map(([icon, title, text]) => (
              <li key={title} className="login-feature">
                <span className="login-feature-icon" aria-hidden="true">
                  <Icon name={icon} size={18} />
                </span>
                <span>
                  <span className="login-feature-title">{title}</span>
                  <br />
                  <span className="login-feature-text">{text}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
        <Illustration />
      </div>

      <main className="login-main">
        <div className="login-panel">
          <div className="brand login-mobile-brand">
            <BrandMark />
            <span className="brand-name">Document Intelligence</span>
          </div>
          <h1 className="login-title">Sign in</h1>
          <p className="login-subtitle">
            Search, read and ask questions of your institution's documents.
          </p>

          {expired ? (
            <Alert tone="info" title="Your session ended">
              You were signed out after a period of time or a password change. Sign in to
              continue.
            </Alert>
          ) : null}

          <form className="stack" onSubmit={submit} noValidate>
            <Field label="Username" required>
              {(props) => (
                <input
                  {...props}
                  className="input input-lg"
                  name="username"
                  autoComplete="username"
                  autoFocus
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  required
                />
              )}
            </Field>
            <Field label="Password" required>
              {(props) => (
                <input
                  {...props}
                  className="input input-lg"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
              )}
            </Field>
            {error ? <Alert tone="danger">{error}</Alert> : null}
            <Button
              type="submit"
              variant="primary"
              size="lg"
              loading={pending}
              disabled={!username.trim() || !password}
              className="btn-block"
            >
              Sign in
            </Button>
          </form>
          <p className="login-footnote">
            <Icon name="lock" size={14} />
            Accounts are issued by your department administrator.
          </p>
        </div>
      </main>
    </div>
  );
}
