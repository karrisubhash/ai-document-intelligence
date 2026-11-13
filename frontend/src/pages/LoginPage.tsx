import { useState, type FormEvent, type RefObject } from "react";
import { Link, useLocation } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { useResource, type Resource } from "../api/hooks";
import type { SignupOptions } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { BrandMark, Icon, type IconName } from "../components/icons";
import {
  Alert,
  Button,
  ErrorState,
  Field,
  Spinner,
  errorMessage,
  useDocumentTitle,
} from "../components/ui";
import "../styles/front.css";
import SignupForm from "./SignupForm";

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

/**
 * A sign-in refused because the account does not exist YET: the applicant's
 * own request, proved with its password (accounts.api.views), is waiting or
 * was declined. Anything else is an ordinary failure.
 */
function signupRefusal(caught: unknown) {
  if (!(caught instanceof ApiError) || caught.status !== 403) return null;
  const code = (caught.payload as { code?: unknown } | null)?.code;
  if (code === "signup_pending" || code === "signup_rejected") {
    return { code, message: caught.message };
  }
  return null;
}

/** Whether the server offers sign-up: null until it has answered. */
export function offersSignup(options: Resource<SignupOptions>): boolean | null {
  if (options.data) return options.data.enabled;
  return options.loading ? null : false;
}

/**
 * The password box with a show/hide switch. The switch is named by its own
 * (visually hidden) words, never by aria-label: a label containing
 * "password" would make it a second match for every "Password" label
 * lookup - the browser suite's getByLabel included - and its state is
 * carried by aria-pressed, as for any toggle.
 */
function PasswordField({
  value,
  onChange,
  shown,
  onToggle,
}: {
  value: string;
  onChange: (value: string) => void;
  shown: boolean;
  onToggle: () => void;
}) {
  return (
    <Field label="Password" required>
      {(props) => (
        <div className="password-input">
          <input
            {...props}
            className="input input-lg"
            name="password"
            type={shown ? "text" : "password"}
            autoComplete="current-password"
            // Spelled out while shown, the password must not be corrected
            // or capitalised.
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            required
          />
          <button
            type="button"
            className="password-toggle"
            aria-pressed={shown}
            aria-controls={props.id}
            onClick={onToggle}
          >
            <Icon name={shown ? "eye-off" : "eye"} size={18} />
            <span className="visually-hidden">Show password</span>
          </button>
        </div>
      )}
    </Field>
  );
}

/**
 * The sign-in form, shared by the focused sign-in page and the landing
 * page's hero. `offerSignup` is null until the server has answered.
 */
export function SignInForm({
  offerSignup,
  heading = "h1",
  autoFocus = true,
  usernameRef,
}: {
  offerSignup: boolean | null;
  heading?: "h1" | "h2";
  autoFocus?: boolean;
  usernameRef?: RefObject<HTMLInputElement | null>;
}) {
  const { login, expired } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [shown, setShown] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<ReturnType<typeof signupRefusal>>(null);
  const Heading = heading;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    // Hidden again before it is sent: a browser offering to remember what
    // was typed should see a password field, not a text box.
    setShown(false);
    setPending(true);
    setError(null);
    setRefusal(null);
    try {
      await login(username.trim(), password);
    } catch (caught) {
      const signup = signupRefusal(caught);
      if (signup) {
        setRefusal(signup);
      } else if (caught instanceof ApiError && caught.status === 429) {
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
    <>
      <Heading className="login-title">Sign in</Heading>
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
              ref={usernameRef}
              className="input input-lg"
              name="username"
              autoComplete="username"
              // Usernames are matched exactly: a phone keyboard that turns
              // "nia" into "Nia" signs nobody in.
              autoCapitalize="none"
              spellCheck={false}
              autoFocus={autoFocus}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          )}
        </Field>
        <PasswordField
          value={password}
          onChange={setPassword}
          shown={shown}
          onToggle={() => setShown((value) => !value)}
        />
        {refusal ? (
          // An alert, like every other sign-in failure: it is the answer to
          // pressing Sign in, and the only one a waiting applicant gets.
          <Alert
            role="alert"
            tone={refusal.code === "signup_pending" ? "info" : "warning"}
            title={
              refusal.code === "signup_pending" ? "Waiting for approval" : "Request declined"
            }
          >
            {refusal.message}
          </Alert>
        ) : null}
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
      {offerSignup ? (
        <p className="login-switch">
          New here?{" "}
          <Link to="/signup" className="link">
            Request an account
          </Link>
        </p>
      ) : null}
      {/* Nothing until the server has said whether sign-up is offered,
          rather than one sentence that turns into another. */}
      {offerSignup !== null ? (
        <p className="login-footnote">
          <Icon name="lock" size={14} />
          {offerSignup
            ? "Every account is approved by an administrator."
            : "Accounts are issued by your department administrator."}
        </p>
      ) : null}
    </>
  );
}

function SignupPanel({ options }: { options: Resource<SignupOptions> }) {
  if (options.data?.enabled) return <SignupForm options={options.data} />;
  if (options.data || options.status === 404) {
    // Turned off here, or a server that predates sign-up.
    return (
      <>
        <h1 className="login-title">Request an account</h1>
        <Alert tone="info" title="Sign-up is not available here">
          Accounts are issued by your department administrator. Ask them for one.
        </Alert>
        <p className="login-switch">
          <Link to="/login" className="link">
            Back to sign in
          </Link>
        </p>
      </>
    );
  }
  if (options.error) return <ErrorState error={options.error} onRetry={options.reload} />;
  return (
    <div className="centered-block">
      <Spinner />
    </div>
  );
}

function SignInTitle() {
  useDocumentTitle("Sign in");
  return null;
}

/**
 * The focused sign-in view: /login, /signup, and any address visited while
 * signed out other than the landing page - so a bookmarked document opens
 * once its reader has signed in.
 */
export default function LoginPage() {
  const { pathname } = useLocation();
  // "/signup/" too: the edge serves the app for it, and the signed-in
  // routes treat the two alike.
  const signingUp = pathname.replace(/\/+$/, "") === "/signup";
  // Whether to offer sign-up at all. Nothing is offered until the server
  // says so: one that has it turned off, predates it or cannot be reached
  // shows the sign-in form exactly as before.
  const options = useResource(() => api.signupOptions(), []);

  return (
    <div className="login">
      <div className="login-aside">
        <Link to="/" className="brand front-home-link">
          <BrandMark />
          <span className="brand-name">Document Intelligence</span>
        </Link>
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
        <div className={`login-panel${signingUp ? " login-panel-wide" : ""}`}>
          <Link to="/" className="brand login-mobile-brand front-home-link">
            <BrandMark />
            <span className="brand-name">Document Intelligence</span>
          </Link>
          {signingUp ? (
            <SignupPanel options={options} />
          ) : (
            <>
              <SignInTitle />
              <SignInForm offerSignup={offersSignup(options)} />
            </>
          )}
          <p className="front-back">
            <Link to="/" className="link">
              What is Document Intelligence?
            </Link>
          </p>
        </div>
      </main>
    </div>
  );
}
