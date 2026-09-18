// MA6.4B: Evaluation polling lifecycle — manages independent bounded
// polling loop for EvaluationRuns. Separated from rendering
// (evaluationConfig.js) to keep timer/network code localized.
//
// Usage:
//   const pollState = createEvaluationPollState();
//   startEvaluationPolling(pollState, comparisonId, onUpdate);
//   // ... later ...
//   stopEvaluationPolling(pollState);

import { api } from "./api.js";

const POLL_INTERVAL_MS = 1500; // Consistent with candidate polling
const POLL_BOUND_MS = 20 * 60 * 1000; // Stop after 20 minutes

export function createEvaluationPollState() {
  return {
    isPolling: false,
    pollHandle: null,
    startedAt: null,
    lastError: null,
  };
}

// Start polling GET /comparisons/{comparisonId}/evaluations.
// onUpdate(data) called with each successful response.
// Stops automatically when all runs reach terminal status or time bound exceeded.
export function startEvaluationPolling(pollState, comparisonId, onUpdate) {
  if (pollState.isPolling) return;

  pollState.isPolling = true;
  pollState.startedAt = Date.now();
  pollState.lastError = null;

  async function poll() {
    try {
      const data = await api.get(`/comparisons/${comparisonId}/evaluations`);
      pollState.lastError = null;

      // Check if all runs are terminal
      const allTerminal = shouldStopPolling(data);
      const withinBound = Date.now() - pollState.startedAt < POLL_BOUND_MS;

      onUpdate(data);

      if (allTerminal || !withinBound) {
        stopEvaluationPolling(pollState);
      }
    } catch (err) {
      pollState.lastError = err;
      // Tolerate transient errors, continue polling
    }
  }

  pollState.pollHandle = setInterval(poll, POLL_INTERVAL_MS);
  // Run once immediately
  poll();
}

// Stop polling immediately
export function stopEvaluationPolling(pollState) {
  if (pollState.pollHandle !== null) {
    clearInterval(pollState.pollHandle);
    pollState.pollHandle = null;
  }
  pollState.isPolling = false;
}

// Determine if all EvaluationRuns in the response are terminal
function shouldStopPolling(evaluationsData) {
  if (!evaluationsData || !evaluationsData.candidates) {
    return false;
  }

  for (const candidate of evaluationsData.candidates) {
    for (const run of candidate.evaluation_runs || []) {
      if (run.status !== "completed" && run.status !== "failed" && run.status !== "cancelled") {
        return false; // Found a non-terminal run
      }
    }
  }

  return true; // All terminal
}
