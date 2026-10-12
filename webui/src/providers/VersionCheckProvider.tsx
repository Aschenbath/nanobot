import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";

import { checkVersion } from "@/lib/api";

type VersionResult =
  | { type: "up-to-date" }
  | { type: "update"; latestVersion: string; pypiUrl?: string }
  | { type: "error"; message: string }
  | null;

interface VersionCheckState {
  result: VersionResult;
  checking: boolean;
  dismissedVersion: string | null;
  check: () => Promise<void>;
  dismiss: () => void;
}

const VersionCheckContext = createContext<VersionCheckState | null>(null);

// Per-connection store: old responses can only update their original store.
// Changing credentials must not remount the application or discard chat drafts.
function createVersionCheck(token: string) {
  let snapshot: Pick<VersionCheckState, "result" | "checking" | "dismissedVersion"> = {
    result: null, checking: false, dismissedVersion: null,
  };
  let pending: Promise<void> | null = null;
  const listeners = new Set<() => void>();
  const update = (patch: Partial<typeof snapshot>) => {
    snapshot = { ...snapshot, ...patch };
    listeners.forEach((listener) => listener());
  };
  const check = () => {
    if (pending) return pending;
    update({ checking: true });
    pending = (async () => {
      try {
        const response = await checkVersion(token);
        update({ result: response.updateAvailable
          ? { type: "update", ...response.updateAvailable }
          : { type: "up-to-date" } });
      } catch (error) {
        update({ result: { type: "error", message: (error as Error).message } });
      } finally {
        pending = null;
        update({ checking: false });
      }
    })();
    return pending;
  };
  return {
    check,
    getSnapshot: () => snapshot,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    dismiss: () => {
      if (snapshot.result?.type === "update") update({ dismissedVersion: snapshot.result.latestVersion });
    },
  };
}

export function VersionCheckProvider({ token, autoCheck = false, children }: {
  token: string;
  autoCheck?: boolean;
  children: ReactNode;
}) {
  const store = useMemo(() => createVersionCheck(token), [token]);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot);

  useEffect(() => {
    // One quiet check on connection, not a timer or an installation request.
    if (autoCheck && token) void store.check();
  }, [autoCheck, store, token]);

  return <VersionCheckContext.Provider value={{ ...snapshot, check: store.check, dismiss: store.dismiss }}>
    {children}
  </VersionCheckContext.Provider>;
}

export function useVersionCheck() {
  const value = useContext(VersionCheckContext);
  if (!value) throw new Error("useVersionCheck must be used within a VersionCheckProvider");
  return value;
}
