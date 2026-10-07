import { useEffect, useRef } from "react";
import { Runs, type RunStreamMessage } from "../api/client";

/**
 * 0.1.2 #10 — subscribe to the live-trace WebSocket for a run.
 *
 * The hook opens ``Runs.stream(agentId, runId)`` when ``runId``
 * becomes truthy and tears it down on unmount or when ``runId``
 * changes.  ``onEvent`` is invoked once per server frame; the hook
 * also exposes a small ``state`` (see return value) so the caller
 * can show a "live" indicator and detect server-initiated
 * disconnects.
 *
 * Reconnect / fallback: the hook does NOT auto-reconnect.  When
 * the socket closes before ``run.complete`` was observed the
 * caller is expected to fall back to polling.  This keeps the
 * hook simple and matches the "WS is best-effort" contract
 * documented in :mod:`app.api.ws`.
 */
export type RunStreamState = "idle" | "connecting" | "open" | "closed" | "error";

export interface UseRunStreamResult {
  /** Current connection state, useful for status indicators. */
  state: RunStreamState;
  /** Manually close the socket (e.g. when the user navigates away). */
  close: () => void;
}

export function useRunStream(
  agentId: string | null,
  runId: string | null,
  onEvent: (msg: RunStreamMessage) => void,
  onClose?: (event: { code: number; reason: string; hadComplete: boolean }) => void,
): UseRunStreamResult {
  // We mirror the active socket in a ref so the cleanup function
  // can close the *latest* one even if the effect re-ran in the
  // meantime.  Storing the WebSocket in state would cause an extra
  // render on every open which we don't need.
  const wsRef = useRef<WebSocket | null>(null);
  const stateRef = useRef<RunStreamState>("idle");
  const sawCompleteRef = useRef<boolean>(false);

  // Latest callback refs avoid re-running the effect every time the
  // parent re-renders (callers in this codebase pass an inline
  // arrow which would otherwise tear the socket down on every
  // render).
  const onEventRef = useRef(onEvent);
  const onCloseRef = useRef(onClose);
  onEventRef.current = onEvent;
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!agentId || !runId) {
      stateRef.current = "idle";
      return;
    }
    sawCompleteRef.current = false;
    stateRef.current = "connecting";
    const ws = Runs.stream(agentId, runId);
    wsRef.current = ws;

    ws.onopen = () => {
      stateRef.current = "open";
    };
    ws.onmessage = (ev: MessageEvent<string>) => {
      let parsed: RunStreamMessage;
      try {
        parsed = JSON.parse(ev.data) as RunStreamMessage;
      } catch {
        return; // ignore malformed frames
      }
      if (parsed.event === "run.complete") {
        sawCompleteRef.current = true;
      }
      try {
        onEventRef.current(parsed);
      } catch {
        // The caller's handler must not break the socket loop.
      }
    };
    ws.onerror = () => {
      stateRef.current = "error";
    };
    ws.onclose = (ev: CloseEvent) => {
      stateRef.current = "closed";
      const cb = onCloseRef.current;
      if (cb) {
        try {
          cb({ code: ev.code, reason: ev.reason, hadComplete: sawCompleteRef.current });
        } catch {
          /* ignore */
        }
      }
    };

    return () => {
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      if (wsRef.current === ws) {
        wsRef.current = null;
      }
      stateRef.current = "idle";
    };
  }, [agentId, runId]);

  return {
    get state(): RunStreamState {
      return stateRef.current;
    },
    close: () => {
      const ws = wsRef.current;
      if (ws && ws.readyState <= WebSocket.OPEN) {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      }
    },
  };
}
