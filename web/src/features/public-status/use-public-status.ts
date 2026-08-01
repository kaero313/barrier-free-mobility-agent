"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { PublicServiceStatus } from "@/contracts/public-status";

import { getPublicStatus } from "./api";

let initialRequest: ReturnType<typeof getPublicStatus> | null = null;

function getInitialPublicStatus() {
  initialRequest ??= getPublicStatus().finally(() => {
    initialRequest = null;
  });
  return initialRequest;
}

export type PublicStatusLoadState =
  | Readonly<{ kind: "checking" }>
  | Readonly<{ data: PublicServiceStatus; kind: "loaded" }>
  | Readonly<{ kind: "unavailable" }>;

export function usePublicStatus() {
  const [requestVersion, setRequestVersion] = useState(0);
  const [state, setState] = useState<PublicStatusLoadState>({ kind: "checking" });
  const refreshPending = useRef(false);

  useEffect(() => {
    const controller = requestVersion === 0 ? null : new AbortController();
    let active = true;
    const request =
      requestVersion === 0
        ? getInitialPublicStatus()
        : getPublicStatus(controller?.signal);

    void request.then(
      (data) => {
        if (active) {
          refreshPending.current = false;
          setState({ data, kind: "loaded" });
        }
      },
      (error: unknown) => {
        if (active && !(error instanceof DOMException && error.name === "AbortError")) {
          refreshPending.current = false;
          setState({ kind: "unavailable" });
        }
      },
    );

    return () => {
      active = false;
      controller?.abort();
    };
  }, [requestVersion]);

  const refresh = useCallback(() => {
    if (refreshPending.current) {
      return;
    }
    refreshPending.current = true;
    setState({ kind: "checking" });
    setRequestVersion((version) => version + 1);
  }, []);

  return { refresh, state } as const;
}
