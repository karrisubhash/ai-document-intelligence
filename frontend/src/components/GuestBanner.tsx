import { useState } from "react";

import { api } from "../api/client";
import { useResource } from "../api/hooks";
import { useAuth } from "../auth/AuthContext";
import "../styles/guest.css";
import { Icon } from "./icons";

/**
 * The strip a one-click demo visitor sees above every page: whose
 * workspace this is, that nothing here can be changed, and the way to an
 * account of their own. Renders nothing for anyone else.
 *
 * Leaving signs the guest out and then LOADS the next page rather than
 * routing to it: the guest's session is over, and a fresh page also drops
 * whatever the application held in memory for it - on a shared computer the
 * next person starts clean.
 */
export default function GuestBanner() {
  const { can, logout } = useAuth();
  const guest = can("is_guest");
  const options = useResource(() => api.signupOptions(), [], { enabled: guest });
  const [leaving, setLeaving] = useState(false);
  if (!guest) return null;

  const offerSignup = Boolean(options.data?.enabled);
  const destination = offerSignup ? "/signup" : "/login";

  const leave = async () => {
    setLeaving(true);
    try {
      await logout();
    } catch {
      // Signed out or not, the next page asks the server again.
    }
    window.location.assign(destination);
  };

  return (
    <div className="guest-banner" role="region" aria-label="Demo workspace">
      <span className="guest-banner-icon" aria-hidden="true">
        <Icon name="sparkles" size={15} />
      </span>
      <p className="guest-banner-text">
        <strong>You're exploring a demo workspace.</strong>{" "}
        <span className="guest-banner-detail">
          Read, search and ask anything - changes are switched off, and the demo is cleared
          after a day.
        </span>
      </p>
      <button
        type="button"
        className="guest-banner-action"
        onClick={() => void leave()}
        disabled={leaving}
      >
        {offerSignup ? "Request an account" : "Sign in with your account"}
        <Icon name="arrow-right" size={14} />
      </button>
    </div>
  );
}
