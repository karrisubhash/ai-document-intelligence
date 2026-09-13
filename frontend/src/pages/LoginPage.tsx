import { useState, type FormEvent } from "react";

import { API_BASE_URL } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ErrorBox } from "../components/ui";

export default function LoginPage() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    try {
      await login(username, password);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="centered">
      <form className="card login" onSubmit={submit}>
        <h1>Document Intelligence Console</h1>
        <p className="muted">
          Signs in against <code>{API_BASE_URL}</code> and holds the DRF token for
          every call this console makes.
        </p>

        <label>
          <span>Username</span>
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>

        <label>
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        <ErrorBox error={error} />

        <button className="button" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="muted small">
          No account yet?{" "}
          <code>docker compose exec backend python manage.py createsuperuser</code>
        </p>
      </form>
    </div>
  );
}
