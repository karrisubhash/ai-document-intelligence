/**
 * The first-run checklist on the home screen: the few things that make the
 * product useful, ticked off from what has already happened (no separate
 * progress is stored). Dismissed per person, in this browser.
 */
import { useState } from "react";
import { Link } from "react-router-dom";

import { homeApi } from "../../api/home";
import { useResource } from "../../api/hooks";
import { Icon, type IconName } from "../icons";
import { Button, IconButton, errorMessage, useToast } from "../ui";

export interface OnboardingStep {
  key: string;
  title: string;
  detail: string;
  done: boolean;
  icon: IconName;
  /** A link, or a button's action. */
  to?: string;
  action?: { label: string; run: () => void; pending?: boolean };
}

const storageKey = (userId: number) => `adi-onboarding-dismissed:${userId}`;

/** Whether this person closed the checklist. localStorage can throw (private
 *  modes, blocked storage); then it simply shows again next time. */
export function wasDismissed(userId: number): boolean {
  try {
    return window.localStorage.getItem(storageKey(userId)) === "1";
  } catch {
    return false;
  }
}

function remember(userId: number) {
  try {
    window.localStorage.setItem(storageKey(userId), "1");
  } catch {
    // Not remembered; it shows again next visit, which is harmless.
  }
}

export function Onboarding({ userId, steps, onDismiss }: { userId: number; steps: OnboardingStep[]; onDismiss: () => void }) {
  const done = steps.filter((step) => step.done).length;
  return (
    <section className="onboarding card" aria-labelledby="onboarding-title">
      <div className="onboarding-head">
        <div>
          <h2 id="onboarding-title" className="card-title">
            Get started
          </h2>
          <p className="card-description">
            {done} of {steps.length} done - each step makes the assistant more useful.
          </p>
        </div>
        <IconButton
          icon="x"
          label="Dismiss the getting-started list"
          size="sm"
          onClick={() => {
            remember(userId);
            onDismiss();
          }}
        />
      </div>
      <div
        className="onboarding-progress"
        role="progressbar"
        aria-label="Getting started"
        aria-valuemin={0}
        aria-valuemax={steps.length}
        aria-valuenow={done}
      >
        <span style={{ width: `${(done / Math.max(1, steps.length)) * 100}%` }} />
      </div>
      <ol className="onboarding-steps">
        {steps.map((step) => (
          <li key={step.key} className={`onboarding-step${step.done ? " is-done" : ""}`}>
            <span className="onboarding-check" aria-hidden="true">
              <Icon name={step.done ? "check" : step.icon} size={16} />
            </span>
            <span className="onboarding-text">
              <span className="onboarding-title">
                {step.title}
                {step.done ? <span className="visually-hidden"> (done)</span> : null}
              </span>
              <span className="onboarding-detail">{step.detail}</span>
            </span>
            {!step.done && step.to ? (
              <Link to={step.to} className="btn btn-secondary btn-sm">
                Start
              </Link>
            ) : null}
            {!step.done && step.action ? (
              <Button size="sm" onClick={step.action.run} loading={step.action.pending}>
                {step.action.label}
              </Button>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * The "Load sample documents" step for a system administrator with an empty
 * library - offered only where the server can do it (POST /admin/samples/).
 */
export function useSampleStep(enabled: boolean, department: string, onLoaded: () => void): OnboardingStep | null {
  const toast = useToast();
  const [pending, setPending] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const available = useResource(() => homeApi.samplesAvailable(), [enabled], { enabled });
  if (!enabled || !available.data) return null;
  return {
    key: "samples",
    title: "Load sample documents",
    detail: department
      ? `A small sample library for ${department}, processed like any upload, to try search and questions on.`
      : "A small sample library, processed like any upload, to try search and questions on.",
    icon: "inbox",
    done: loaded,
    action: {
      label: "Load samples",
      pending,
      run: async () => {
        setPending(true);
        try {
          const result = await homeApi.loadSamples(department);
          setLoaded(true);
          toast(
            result.queued
              ? `${result.queued} sample ${result.queued === 1 ? "document is" : "documents are"} being processed.`
              : "The samples are already in the library.",
          );
          onLoaded();
        } catch (caught) {
          toast(errorMessage(caught), "danger");
        } finally {
          setPending(false);
        }
      },
    },
  };
}
