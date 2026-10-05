import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api, setUnauthorizedHandler } from "../api/client";
import type { Capabilities, Me } from "../api/types";

/**
 * "unreachable": the session check failed for a reason that says nothing
 * about the session (network down, a 5xx). Not a sign-out - asking for a
 * password the server cannot check would be wrong, and the session may be
 * perfectly valid.
 */
type SessionState = "checking" | "signed-in" | "signed-out" | "unreachable";

interface AuthState {
  me: Me | null;
  state: SessionState;
  /** Set when the session ended underneath the user rather than by sign-out. */
  expired: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  can: (capability: keyof Capabilities) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<SessionState>("checking");
  const [expired, setExpired] = useState(false);
  const meRef = useRef<Me | null>(null);
  meRef.current = me;

  const refresh = useCallback(async () => {
    try {
      setMe(await api.me());
      setState("signed-in");
    } catch (error) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        setMe(null);
        setState("signed-out");
        return;
      }
      // Network failure or a 5xx: the session may be perfectly valid, so
      // this is not a sign-out. It used to be one - the comment above said
      // otherwise while the code showed the login form.
      setMe(null);
      setState("unreachable");
      throw error;
    }
  }, []);

  // The session is a cookie the page cannot read, so the only way to know
  // whether one exists is to ask the server.
  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);

  // Any API call that comes back 401 means the session is gone - it hit
  // its eight-hour lifetime, or the password was changed elsewhere. The
  // user lands on the login screen with an explanation instead of a page
  // full of errors.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (meRef.current) setExpired(true);
      setMe(null);
      setState("signed-out");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const profile = await api.login(username, password);
    setExpired(false);
    setMe(profile);
    setState("signed-in");
  }, []);

  // Signed out only once the server says so. The session cookie is
  // httpOnly - the page cannot remove it - so a sign-out request that
  // failed (a network drop, a 5xx) used to LOOK signed out on a shared
  // machine while the session stayed valid for hours. A 401 means it was
  // already gone, which is the outcome asked for.
  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) throw error;
    }
    setMe(null);
    setExpired(false);
    setState("signed-out");
  }, []);

  const can = useCallback(
    (capability: keyof Capabilities) => Boolean(me?.capabilities?.[capability]),
    [me],
  );

  const value = useMemo<AuthState>(
    () => ({ me, state, expired, login, logout, refresh, can }),
    [me, state, expired, login, logout, refresh, can],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside an AuthProvider");
  return context;
}

/** The signed-in user, for screens that only render when there is one. */
export function useMe(): Me {
  const { me } = useAuth();
  if (!me) throw new Error("useMe used outside an authenticated route");
  return me;
}
