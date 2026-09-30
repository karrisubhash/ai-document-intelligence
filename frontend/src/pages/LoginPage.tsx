import { useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { Alert, Button, Field, errorMessage } from "../components/ui";

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
      <div className="login-panel">
        <div className="brand brand-large">
          <span className="brand-mark" aria-hidden="true" />
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
                className="input"
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
                className="input"
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
            loading={pending}
            disabled={!username.trim() || !password}
            className="btn-block"
          >
            Sign in
          </Button>
        </form>
        <p className="login-footnote">
          Accounts are issued by your department administrator.
        </p>
      </div>
    </div>
  );
}
