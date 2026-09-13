import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api, readToken, writeToken } from "../api/client";
import type { Me } from "../api/types";

interface AuthState {
  token: string | null;
  me: Me | null;
  /** True until the stored token has been checked against the API. */
  restoring: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => readToken());
  const [me, setMe] = useState<Me | null>(null);
  const [restoring, setRestoring] = useState<boolean>(() => readToken() !== null);

  const logout = useCallback(() => {
    writeToken(null);
    setToken(null);
    setMe(null);
  }, []);

  // A stored token is a claim, not a session. It is verified against
  // /api/auth/me before the console shows anything, so a revoked or stale
  // token lands on the login screen instead of a wall of 401s.
  useEffect(() => {
    if (!token) {
      setMe(null);
      setRestoring(false);
      return;
    }

    let cancelled = false;
    setRestoring(true);

    api
      .me(token)
      .then((profile) => {
        if (!cancelled) setMe(profile);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 401) logout();
      })
      .finally(() => {
        if (!cancelled) setRestoring(false);
      });

    return () => {
      cancelled = true;
    };
  }, [token, logout]);

  const login = useCallback(async (username: string, password: string) => {
    const { token: issued } = await api.login(username, password);
    writeToken(issued);
    setToken(issued);
  }, []);

  const value = useMemo<AuthState>(
    () => ({ token, me, restoring, login, logout }),
    [token, me, restoring, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside an AuthProvider");
  return context;
}

/**
 * The token, for the pages that only run when signed in. Throwing rather
 * than returning null keeps every caller from re-checking something the
 * router already guaranteed.
 */
export function useToken(): string {
  const { token } = useAuth();
  if (!token) throw new Error("useToken used outside an authenticated route");
  return token;
}
