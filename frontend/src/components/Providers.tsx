"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

const ACTOR_STORAGE_KEY = "poe.actor";
const DEFAULT_ACTOR = "system";

interface ActorContextValue {
  actor: string;
  setActor: (actor: string) => void;
}

const ActorContext = createContext<ActorContextValue>({
  actor: DEFAULT_ACTOR,
  setActor: () => undefined,
});

/**
 * Who is acting. There is no auth in this app by design — the recruiter
 * switcher in the header is the entire identity model, and its value is sent
 * as X-Actor on every mutating request so the audit log records a person
 * rather than "system".
 *
 * Read from localStorage after mount rather than during render: the server
 * has no localStorage, and seeding state from it directly would make the first
 * client render disagree with the server's HTML and trigger a hydration error.
 */
export function useActor(): ActorContextValue {
  return useContext(ActorContext);
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [actor, setActorState] = useState(DEFAULT_ACTOR);

  useEffect(() => {
    const stored = window.localStorage.getItem(ACTOR_STORAGE_KEY);
    if (stored) setActorState(stored);
  }, []);

  const setActor = useCallback((next: string) => {
    setActorState(next);
    window.localStorage.setItem(ACTOR_STORAGE_KEY, next);
  }, []);

  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // AI mutations change candidate risk, so a stale list is actively
            // misleading. Short and explicit beats the default 0 plus
            // aggressive refetching on every window focus.
            staleTime: 15_000,
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      }),
  );

  const actorValue = useMemo(() => ({ actor, setActor }), [actor, setActor]);

  return (
    <QueryClientProvider client={queryClient}>
      <ActorContext.Provider value={actorValue}>{children}</ActorContext.Provider>
    </QueryClientProvider>
  );
}
