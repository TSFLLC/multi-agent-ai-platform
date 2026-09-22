// Bounded, race-safe polling for the Control Room (MA7.6B) -- pure, with
// injectable timers so it is unit-tested without a clock
// (frontend/tests/runPolling.test.mjs).
//
// Why polling: the browser EventSource cannot send the Authorization header the
// API requires, and MA7.6B deliberately adds no WebSocket or auth redesign. A
// sequential poll of one snapshot endpoint is the smallest safe design:
//
//  * never two requests in flight: the next one is scheduled only AFTER the
//    previous settles (a slow server cannot pile up requests);
//  * a generation counter: after stop() (navigating away) or a newer start, a late
//    response is ignored, so a stale page never paints;
//  * it stops by itself when the data says the run is finished, and is bounded in
//    total time and by consecutive failures;
//  * it backs off on errors, and slows down while the run only waits for a person.
//
// State: "idle" | "live" | "finished" | "paused" | "stopped"
//   finished = the run is terminal (no more polling needed)
//   paused   = bound reached or too many failures (the user can Refresh)

export const DEFAULT_MAX_DURATION_MS = 2 * 60 * 60 * 1000; // an approval may wait a long time; two hours of live updates, then Refresh
export const DEFAULT_MAX_FAILURES = 6;
const MAX_BACKOFF_MS = 30000;

export function createPoller({
  fetchOnce,
  onData,
  onError = () => {},
  onState = () => {},
  isTerminal,
  intervalFor = () => 2000,
  maxDurationMs = DEFAULT_MAX_DURATION_MS,
  maxFailures = DEFAULT_MAX_FAILURES,
  timers = { setTimeout: (fn, ms) => setTimeout(fn, ms), clearTimeout: (h) => clearTimeout(h), now: () => Date.now() },
  isHidden = () => false,
}) {
  let generation = 0;
  let handle = null;
  let state = "idle";
  let startedAt = 0;
  let failures = 0;
  let activeGen = null; // the generation whose request is in flight
  let last = null;

  function setState(next) {
    if (state === next) return;
    state = next;
    onState(next);
  }

  function clearTimer() {
    if (handle !== null) {
      timers.clearTimeout(handle);
      handle = null;
    }
  }

  function delayAfter(data) {
    if (failures > 0) return Math.min(MAX_BACKOFF_MS, 1000 * 2 ** (failures - 1));
    return Math.max(250, Number(intervalFor(data, { hidden: isHidden() })) || 2000);
  }

  async function tick(gen) {
    if (gen !== generation) return;
    activeGen = gen;
    let result;
    let error = null;
    try {
      result = await fetchOnce();
    } catch (err) {
      error = err;
    }
    if (activeGen === gen) activeGen = null;
    if (gen !== generation) return; // stopped or restarted while the request was in flight: ignore it
    if (error) {
      failures += 1;
      onError(error, failures);
      if (failures >= maxFailures) {
        setState("paused");
        return;
      }
    } else {
      failures = 0;
      last = result;
      onData(result);
      if (isTerminal(result)) {
        setState("finished");
        return;
      }
    }
    if (timers.now() - startedAt >= maxDurationMs) {
      setState("paused");
      return;
    }
    clearTimer();
    handle = timers.setTimeout(() => {
      handle = null;
      tick(gen);
    }, delayAfter(last));
  }

  function begin() {
    generation += 1;
    clearTimer();
    failures = 0;
    startedAt = timers.now();
    setState("live");
    return tick(generation);
  }

  return {
    // Start (or restart) live updates. Resolves after the first poll settles.
    start: () => begin(),
    // One immediate fetch that also re-arms live updates (used by Refresh and after a mutation).
    refresh: () => begin(),
    // Stop for good (navigation away): pending timers cleared, late responses ignored.
    stop() {
      generation += 1;
      clearTimer();
      activeGen = null;
      setState("stopped");
    },
    get state() {
      return state;
    },
    get failures() {
      return failures;
    },
    get inFlight() {
      return activeGen !== null && activeGen === generation;
    },
  };
}
