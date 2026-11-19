import { useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { demoApi } from "../api/demo";
import { useResource } from "../api/hooks";
import { useAuth } from "../auth/AuthContext";
import { BrandMark, Icon, type IconName } from "../components/icons";
import { Alert, Button, errorMessage, useDocumentTitle } from "../components/ui";
import { setTheme, useResolvedTheme } from "../lib/theme";
import "../styles/front.css";
import { SignInForm, offersSignup } from "./LoginPage";

/*
 * The front door, for anyone who is not signed in at "/".
 *
 * The sign-in form stays on this page - in the hero, beside the pitch on a
 * wide screen and straight under it on a phone - because a returning
 * reader should not have to click through marketing to get to work (and
 * the browser suite signs in from "/"). It is the page's only "Sign in"
 * heading and its only "Sign in" button: everything else that leads to it
 * is a link.
 *
 * Everything shown is drawn here, with CSS: the preview is static data
 * animated with keyframes, the diagram is inline SVG. No images to fetch,
 * nothing that needs a Content-Security-Policy exception.
 */

const CAPABILITIES: [IconName, string, string][] = [
  [
    "quote",
    "Answers with citations",
    "Ask in plain words. Every claim links to the passage it came from, and when your documents do not cover something, the answer says so instead of guessing.",
  ],
  [
    "sparkles",
    "Every document briefed on arrival",
    "A summary, the key facts and the deadlines are pulled out as each document is processed - each one a quotation you can open in place.",
  ],
  [
    "search",
    "Search by wording and by meaning",
    "Keyword and meaning-based search run together and are re-ranked, so “fee for late payment” finds the surcharge clause however it is worded.",
  ],
  [
    "scan",
    "Reads scans, tables and forms",
    "Text layers are extracted, scanned pages are read by OCR, and tables and fields are recognised - so a figure in a scanned circular is as findable as a typed one.",
  ],
  [
    "shield",
    "Access control and an audit trail",
    "Departments are hard boundaries for search and answers alike, and every view, download, upload and sign-in is recorded.",
  ],
  [
    "gauge",
    "Analytics for administrators",
    "See what people ask, which questions go unanswered, and how processing is doing - without reading anyone's conversations.",
  ],
];

const STEPS: [IconName, string, string][] = [
  ["upload", "Upload", "A PDF, a Word file, plain text or a scan - one file, several, or a whole .zip."],
  ["scan", "Read", "Text layers are extracted; pages without one are read by OCR."],
  ["layers", "Understand", "Tables, fields and the document's type are recognised, and a brief is written."],
  ["database", "Index", "Passages are indexed by keyword and by meaning, inside your department."],
  ["search", "Ask & search", "Questions are matched to the best passages and re-ranked by relevance."],
  ["quote", "Cite", "The answer quotes and links its sources - or says it does not know."],
];

const TRUST: [IconName, string, string][] = [
  [
    "building",
    "Department boundaries",
    "People see their own department's documents. The rule is applied inside every search and answer, not just hidden in the interface.",
  ],
  [
    "scroll",
    "A complete audit trail",
    "Views, downloads, uploads, sign-ins and administrative changes are recorded. Questions themselves are never written to the log.",
  ],
  [
    "server",
    "Your documents stay on your server",
    "It runs on infrastructure you control. Files are served only through signed-in, audited downloads.",
  ],
  [
    "cpu",
    "Models that run on your hardware",
    "Reading, OCR, search and re-ranking run locally. A hosted writing model is optional - without it, answers quote your documents directly.",
  ],
  [
    "alert-circle",
    "It says when it does not know",
    "An answer that cannot be traced to a passage is withheld, and you are shown the closest passages instead of a confident guess.",
  ],
];

const FAQ: [string, ReactNode][] = [
  [
    "What can I do in the live demo?",
    "Read, search and ask questions of a library of sample institutional documents - regulations, policies, circulars, a handbook and a scanned notice. The demo is read-only, your questions are visible only to you, and the demo account is deleted after a day.",
  ],
  [
    "Where do the documents go?",
    "Onto the server your institution runs, and nowhere else. If a hosted model is switched on for writing answers, only the few passages an answer needs are sent to it; switched off, nothing leaves the server at all.",
  ],
  [
    "Which files can it read?",
    "PDF, Word (.docx, .doc), Excel (.xlsx, .xls), PowerPoint (.pptx, .ppt), OpenDocument (.odt, .ods, .odp), RTF, web pages (.html), CSV, JSON, XML, Markdown and plain text, and images (PNG, JPEG, TIFF, WebP or BMP), up to 50 MB each, with no limit on the number of pages. Tables are read into the text row by row, every value beside its column's name and each row numbered ('Table 2, row 4'), so an answer cites the exact row; charts in Word and PowerPoint are read as the numbers they were drawn from, scanned pages and pictures of text are read by OCR, and with a vision model switched on charts, flowcharts and diagrams are read too.",
  ],
  [
    "Can it make things up?",
    "Answers are checked against the passages they cite before you see them. One that cannot be grounded is withheld, and you are told the documents do not answer the question.",
  ],
  [
    "Who sees what?",
    "Everyone sees their own department's documents and their own conversations. Administrators manage people and documents in their department, and every action is in the audit log.",
  ],
  [
    "How do I get an account?",
    "Request one from this page: an administrator reviews every request and decides the department and role it gets. Or ask your department administrator directly.",
  ],
];

const QUESTION = "What attendance do I need to sit the end-semester exam?";

function ThemeToggle() {
  const resolved = useResolvedTheme();
  const label = resolved === "dark" ? "Switch to the light theme" : "Switch to the dark theme";
  return (
    <button
      type="button"
      className="icon-button"
      aria-label={label}
      title={label}
      onClick={() => setTheme(resolved === "dark" ? "light" : "dark")}
    >
      <Icon name={resolved === "dark" ? "sun" : "moon"} size={18} />
    </button>
  );
}

/**
 * A question, its answer streaming in with citations, and the cited
 * passage lighting up on its page: what using the product looks like,
 * drawn from static data. A pause control, because it loops (WCAG 2.2.2);
 * with reduced motion it simply shows the finished answer.
 */
function ProductPreview() {
  const [paused, setPaused] = useState(false);
  return (
    <figure className={`front-preview${paused ? " is-paused" : ""}`}>
      <figcaption className="visually-hidden">
        An example: asked what attendance is needed for the end-semester examination,
        the answer says at least 75 percent in each course, with condonation possible
        between 65 and 75 percent on medical grounds, and cites clauses 4.2 and 4.3 of the
        Academic Regulations, which open with the quoted passage highlighted.
      </figcaption>
      <div className="front-window" aria-hidden="true">
        <div className="front-window-bar">
          <span className="front-window-dots">
            <span />
            <span />
            <span />
          </span>
          <span className="front-window-title">
            <Icon name="sparkles" size={13} /> Ask · Document Intelligence
          </span>
        </div>
        <div className="front-window-body">
          <div className="front-chat">
            <div className="front-bubble front-bubble-question">{QUESTION}</div>
            <div className="front-typing">
              <span />
              <span />
              <span />
            </div>
            <div className="front-bubble front-bubble-answer">
              <span className="front-answer-badge">
                <Icon name="check-circle" size={13} /> Grounded in 2 passages
              </span>
              <span className="front-stream front-stream-1">
                You need at least <strong>75 percent attendance in each course</strong>{" "}
                <span className="front-cite">1</span>
              </span>
              <span className="front-stream front-stream-2">
                Between 65 and 75 percent you can apply for condonation on medical grounds,
                with a certificate <span className="front-cite">2</span>
              </span>
              <span className="front-sources">
                <span className="front-source-chip">
                  <Icon name="file-text" size={12} /> Academic Regulations R20 · p. 1
                </span>
              </span>
            </div>
          </div>
          <div className="front-page">
            <div className="front-page-head">
              <Icon name="file-text" size={13} />
              <span>Academic Regulations R20</span>
              <span className="front-page-number">page 1</span>
            </div>
            <p className="front-page-heading">Clause 4.2 Attendance</p>
            <p className="front-page-text">
              <mark className="front-hit">
                A student must maintain a minimum of 75 percent attendance in each course to
                be eligible for the end-semester examination.
              </mark>{" "}
              Attendance is computed up to the last instructional day of the semester.
            </p>
            <p className="front-page-heading">Clause 4.3 Condonation</p>
            <p className="front-page-text">
              <mark className="front-hit front-hit-2">
                A student with attendance between 65 and 75 percent may apply for condonation
                on medical grounds
              </mark>
              , supported by a certificate from a registered practitioner.
            </p>
            <span className="front-page-line" />
            <span className="front-page-line front-page-line-short" />
          </div>
        </div>
      </div>
      <button
        type="button"
        className="front-preview-toggle"
        aria-pressed={paused}
        onClick={() => setPaused((value) => !value)}
      >
        <Icon name={paused ? "arrow-right" : "clock"} size={14} />
        {paused ? "Play the example" : "Pause the example"}
      </button>
    </figure>
  );
}

/** The pipeline, upload to cited answer, as a diagram and as a list. */
function Pipeline() {
  const spacing = 176;
  const left = 72;
  return (
    <div className="front-pipeline">
      <svg
        className="front-pipeline-diagram"
        viewBox="0 0 1024 176"
        role="img"
        aria-labelledby="front-pipeline-title front-pipeline-desc"
      >
        <title id="front-pipeline-title">How a document becomes a cited answer</title>
        <desc id="front-pipeline-desc">
          Six stages in a row: upload, read, understand, index, ask and search, cite.
        </desc>
        <line
          className="front-pipeline-track"
          x1={left}
          y1={60}
          x2={left + spacing * (STEPS.length - 1)}
          y2={60}
        />
        <line
          className="front-pipeline-flow"
          x1={left}
          y1={60}
          x2={left + spacing * (STEPS.length - 1)}
          y2={60}
        />
        {STEPS.map(([icon, label], index) => {
          const x = left + spacing * index;
          return (
            <g key={label} className={`front-node front-node-${index + 1}`}>
              <circle className="front-node-halo" cx={x} cy={60} r={38} />
              <circle className="front-node-disc" cx={x} cy={60} r={30} />
              <g transform={`translate(${x - 12} 48)`}>
                <Icon name={icon} size={24} strokeWidth={1.7} />
              </g>
              <text className="front-node-step" x={x} y={124} textAnchor="middle">
                {String(index + 1).padStart(2, "0")}
              </text>
              <text className="front-node-label" x={x} y={148} textAnchor="middle">
                {label}
              </text>
            </g>
          );
        })}
      </svg>
      <ol className="front-steps">
        {STEPS.map(([icon, label, text]) => (
          <li key={label} className="front-step">
            <span className="front-step-icon" aria-hidden="true">
              <Icon name={icon} size={18} />
            </span>
            <span>
              <strong className="front-step-title">{label}</strong>
              <span className="front-step-text">{text}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function demoError(caught: unknown): string {
  if (caught instanceof ApiError) {
    const code = (caught.payload as { code?: unknown } | null)?.code;
    if (code === "demo_full") return caught.message;
    if (caught.status === 429) {
      return "Several demos have just been started from your network. Try again in a little while.";
    }
    if (caught.status === 404) return "The live demo is not available right now.";
    if (caught.status === 0) {
      return "The server could not be reached. Check your connection and try again.";
    }
  }
  return errorMessage(caught);
}

export default function LandingPage() {
  useDocumentTitle("Answers you can trace back to the page");
  const { startDemo } = useAuth();
  const options = useResource(() => api.signupOptions(), []);
  const demo = useResource(() => demoApi.status(), []);
  const [starting, setStarting] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const username = useRef<HTMLInputElement | null>(null);
  const offerSignup = offersSignup(options);
  const demoOn = Boolean(demo.data?.enabled);

  const tryDemo = async () => {
    setStarting(true);
    setProblem(null);
    try {
      // Signed in as a guest: the application takes over from here.
      await startDemo();
    } catch (caught) {
      setProblem(demoError(caught));
      if (caught instanceof ApiError && caught.status === 404) demo.reload();
      setStarting(false);
    }
  };

  const toSignIn = () => {
    // After the browser has scrolled to the form.
    window.setTimeout(() => username.current?.focus(), 0);
  };

  return (
    <div className="front">
      <a className="skip-link" href="#sign-in" onClick={toSignIn}>
        Skip to the sign-in form
      </a>
      <header className="front-nav">
        <div className="front-shell front-nav-inner">
          <Link to="/" className="brand front-brand">
            <BrandMark />
            <span className="brand-name">Document Intelligence</span>
          </Link>
          <nav className="front-nav-links" aria-label="On this page">
            <a href="#features">Features</a>
            <a href="#how">How it works</a>
            <a href="#trust">Security</a>
            <a href="#faq">FAQ</a>
          </nav>
          <div className="front-nav-actions">
            <ThemeToggle />
            <Link to="/login" className="btn btn-secondary front-nav-signin">
              Sign in
            </Link>
          </div>
        </div>
      </header>

      <main id="main" className="front-main" tabIndex={-1}>
        <section className="front-hero" aria-labelledby="front-headline">
          <div className="front-shell front-hero-grid">
            <div className="front-hero-copy">
              <p className="front-eyebrow">
                <Icon name="sparkles" size={14} /> Document AI for institutions
              </p>
              <h1 id="front-headline" className="front-headline">
                Answers you can trace back to <span className="front-accent">the page</span>.
              </h1>
              <p className="front-subline">
                Ask your regulations, policies, circulars and forms a question in plain
                words. Every answer quotes the passage it came from - and when your
                documents do not say, it tells you so.
              </p>
              <div className="front-ctas">
                {demoOn ? (
                  <Button
                    variant="primary"
                    size="lg"
                    icon="sparkles"
                    loading={starting}
                    onClick={() => void tryDemo()}
                  >
                    Try the live demo
                  </Button>
                ) : null}
                {offerSignup ? (
                  <Link
                    to="/signup"
                    className={`btn btn-lg ${demoOn ? "btn-secondary" : "btn-primary"}`}
                  >
                    Request access
                  </Link>
                ) : null}
                <a href="#sign-in" className="btn btn-ghost btn-lg front-cta-signin" onClick={toSignIn}>
                  Sign in
                </a>
              </div>
              {problem ? (
                <Alert tone="warning" role="alert">
                  {problem}
                </Alert>
              ) : null}
              {demoOn ? (
                <p className="front-cta-note">
                  <Icon name="lock" size={14} />
                  No account needed. The demo is read-only, private to you, and cleared after a
                  day.
                </p>
              ) : null}
              <ul className="front-proof" aria-label="At a glance">
                <li>
                  <Icon name="quote" size={15} /> Cited answers
                </li>
                <li>
                  <Icon name="scan" size={15} /> OCR for scans
                </li>
                <li>
                  <Icon name="shield" size={15} /> Department boundaries
                </li>
              </ul>
            </div>
            <div className="front-signin" id="sign-in">
              <SignInForm
                offerSignup={offerSignup}
                heading="h2"
                autoFocus={false}
                usernameRef={username}
              />
            </div>
          </div>
        </section>

        <section className="front-section front-showcase" aria-labelledby="front-showcase-title">
          <div className="front-shell">
            <div className="front-section-head">
              <p className="front-kicker">See it work</p>
              <h2 id="front-showcase-title">A question, an answer, and the page it came from</h2>
              <p>
                Answers arrive as they are written, every claim carries a numbered citation,
                and a citation opens the source with the quoted passage highlighted.
              </p>
            </div>
            <ProductPreview />
          </div>
        </section>

        <section id="features" className="front-section" aria-labelledby="front-features-title">
          <div className="front-shell">
            <div className="front-section-head">
              <p className="front-kicker">Capabilities</p>
              <h2 id="front-features-title">Everything your documents know, one question away</h2>
            </div>
            <ul className="front-cards">
              {CAPABILITIES.map(([icon, title, text]) => (
                <li key={title} className="front-card">
                  <span className="front-card-icon" aria-hidden="true">
                    <Icon name={icon} size={20} />
                  </span>
                  <h3>{title}</h3>
                  <p>{text}</p>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section id="how" className="front-section front-how" aria-labelledby="front-how-title">
          <div className="front-shell">
            <div className="front-section-head">
              <p className="front-kicker">How it works</p>
              <h2 id="front-how-title">From upload to cited answer</h2>
              <p>
                Every document goes through the same pipeline, in the background, the moment
                it is uploaded.
              </p>
            </div>
            <Pipeline />
          </div>
        </section>

        <section id="trust" className="front-section front-trust" aria-labelledby="front-trust-title">
          <div className="front-shell">
            <div className="front-section-head">
              <p className="front-kicker">Security and trust</p>
              <h2 id="front-trust-title">Built to be trusted with your documents</h2>
            </div>
            <ul className="front-trust-grid">
              {TRUST.map(([icon, title, text]) => (
                <li key={title} className="front-trust-item">
                  <span className="front-trust-icon" aria-hidden="true">
                    <Icon name={icon} size={20} />
                  </span>
                  <h3>{title}</h3>
                  <p>{text}</p>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section id="faq" className="front-section" aria-labelledby="front-faq-title">
          <div className="front-shell front-faq">
            <div className="front-section-head">
              <p className="front-kicker">FAQ</p>
              <h2 id="front-faq-title">Questions, answered</h2>
            </div>
            <div className="front-faq-list">
              {FAQ.map(([question, answer]) => (
                <details key={question} className="front-faq-item">
                  <summary>
                    {question}
                    <Icon name="plus" size={16} className="front-faq-icon" />
                  </summary>
                  <p>{answer}</p>
                </details>
              ))}
            </div>
          </div>
        </section>
      </main>

      <footer className="front-footer">
        <div className="front-shell front-footer-inner">
          <div className="front-footer-brand">
            <span className="brand">
              <BrandMark size="sm" />
              <span className="brand-name">Document Intelligence</span>
            </span>
            <p>Search, read and ask questions of your institution's documents.</p>
          </div>
          <nav className="front-footer-links" aria-label="Footer">
            <a href="#features">Features</a>
            <a href="#how">How it works</a>
            <a href="#trust">Security</a>
            <a href="#faq">FAQ</a>
            <Link to="/login">Sign in</Link>
            {offerSignup ? <Link to="/signup">Request access</Link> : null}
          </nav>
          <p className="front-footer-note">
            The demo's documents are synthetic samples. © {new Date().getFullYear()} Document
            Intelligence.
          </p>
        </div>
      </footer>
    </div>
  );
}
