import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_MAX_FAILURES, createPoller } from "../assets/js/runPolling.js";

// A manual clock: timers only fire when the test says so.
function fakeTimers() {
  let now = 1_000_000;
  let nextId = 1;
  const pending = new Map();
  return {
    timers: {
      setTimeout: (fn, ms) => {
        const id = nextId++;
        pending.set(id, { fn, at: now + ms, ms });
        return id;
      },
      clearTimeout: (id) => pending.delete(id),
      now: () => now,
    },
    pendingCount: () => pending.size,
    nextDelay: () => [...pending.values()][0]?.ms,
    advance(ms) {
      now += ms;
    },
    async fireNext() {
      const [id, entry] = [...pending.entries()][0];
      pending.delete(id);
      entry.fn();
      await new Promise((r) => setImmediate(r));
    },
  };
}

const flush = () => new Promise((r) => setImmediate(r));

function harness(overrides = {}) {
  const clock = fakeTimers();
  const events = { data: [], errors: [], states: [] };
  let responses = overrides.responses || [{ status: "running" }];
  let calls = 0;
  const resolvers = [];
  const poller = createPoller({
    fetchOnce: overrides.fetchOnce || (() => {
      calls += 1;
      const value = responses[Math.min(calls - 1, responses.length - 1)];
      if (value instanceof Error) return Promise.reject(value);
      return Promise.resolve(value);
    }),
    onData: (d) => events.data.push(d),
    onError: (e, n) => events.errors.push([e.message, n]),
    onState: (s) => events.states.push(s),
    isTerminal: (d) => ["completed", "failed", "cancelled"].includes(d.status),
    intervalFor: overrides.intervalFor || ((d) => (d.status === "node_waiting_for_approval" ? 5000 : 2000)),
    timers: clock.timers,
    ...(overrides.options || {}),
  });
  return { poller, clock, events, calls: () => calls, resolvers, setResponses: (r) => { responses = r; calls = 0; } };
}

test("it polls, schedules the next poll after the response, and reports live", async () => {
  const h = harness();
  await h.poller.start();
  assert.equal(h.calls(), 1);
  assert.deepEqual(h.events.data, [{ status: "running" }]);
  assert.equal(h.poller.state, "live");
  assert.equal(h.clock.pendingCount(), 1);
  assert.equal(h.clock.nextDelay(), 2000);
  await h.clock.fireNext();
  assert.equal(h.calls(), 2);
});

test("a terminal run is delivered once and polling stops by itself", async () => {
  const h = harness({ responses: [{ status: "running" }, { status: "completed" }] });
  await h.poller.start();
  await h.clock.fireNext();
  assert.equal(h.poller.state, "finished");
  assert.equal(h.clock.pendingCount(), 0, "no further poll is scheduled");
  assert.equal(h.calls(), 2);
  assert.deepEqual(h.events.data.map((d) => d.status), ["running", "completed"]);
});

for (const status of ["completed", "failed", "cancelled"]) {
  test(`a run that is already ${status} is fetched once and never polled again`, async () => {
    const h = harness({ responses: [{ status }] });
    await h.poller.start();
    assert.equal(h.poller.state, "finished");
    assert.equal(h.clock.pendingCount(), 0);
    assert.equal(h.calls(), 1);
  });
}

test("a non-terminal run keeps polling, and slows down while it only waits for a person", async () => {
  const h = harness({ responses: [{ status: "running" }, { status: "node_waiting_for_approval" }, { status: "node_waiting_for_approval" }] });
  await h.poller.start();
  assert.equal(h.clock.nextDelay(), 2000);
  await h.clock.fireNext();
  assert.equal(h.clock.nextDelay(), 5000); // waiting for approval: slower cadence, but STILL polling (never gives up on a pending decision)
  await h.clock.fireNext();
  assert.equal(h.poller.state, "live");
  assert.equal(h.clock.pendingCount(), 1);
});

test("requests never overlap: the next poll is only scheduled after the previous settles", async () => {
  const clock = fakeTimers();
  let release;
  let inFlight = 0;
  let maxInFlight = 0;
  const poller = createPoller({
    fetchOnce: () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      return new Promise((resolve) => {
        release = () => { inFlight -= 1; resolve({ status: "running" }); };
      });
    },
    onData: () => {},
    isTerminal: () => false,
    timers: clock.timers,
  });
  const started = poller.start();
  assert.equal(poller.inFlight, true);
  assert.equal(clock.pendingCount(), 0, "nothing is scheduled while a request is in flight");
  release();
  await started;
  assert.equal(poller.inFlight, false);
  assert.equal(clock.pendingCount(), 1);
  await clock.fireNext();
  release();
  await flush();
  assert.equal(maxInFlight, 1);
});

test("stop() ignores a response that arrives late (navigating away can never repaint)", async () => {
  const clock = fakeTimers();
  let resolveFetch;
  const data = [];
  const poller = createPoller({
    fetchOnce: () => new Promise((resolve) => { resolveFetch = resolve; }),
    onData: (d) => data.push(d),
    isTerminal: () => false,
    timers: clock.timers,
  });
  poller.start();
  poller.stop();
  resolveFetch({ status: "running" });
  await flush();
  assert.deepEqual(data, [], "the stale response was dropped");
  assert.equal(clock.pendingCount(), 0, "and it did not re-arm the timer");
  assert.equal(poller.state, "stopped");
});

test("stop() clears a pending timer", async () => {
  const h = harness();
  await h.poller.start();
  assert.equal(h.clock.pendingCount(), 1);
  h.poller.stop();
  assert.equal(h.clock.pendingCount(), 0);
  assert.equal(h.poller.state, "stopped");
});

test("refresh() while a request is in flight fetches again and only the newest response counts", async () => {
  const clock = fakeTimers();
  const resolvers = [];
  const data = [];
  const poller = createPoller({
    fetchOnce: () => new Promise((resolve) => resolvers.push(resolve)),
    onData: (d) => data.push(d),
    isTerminal: () => false,
    timers: clock.timers,
  });
  poller.start();
  poller.refresh(); // a second request begins although the first is still pending
  assert.equal(resolvers.length, 2);
  resolvers[0]({ status: "running", tag: "old" });
  resolvers[1]({ status: "running", tag: "new" });
  await flush();
  assert.deepEqual(data.map((d) => d.tag), ["new"]);
  assert.equal(clock.pendingCount(), 1, "exactly one follow-up poll is scheduled");
});

test("errors back off exponentially, then pause after too many failures (and Refresh recovers)", async () => {
  const boom = new Error("network down");
  const h = harness({ responses: [boom] });
  await h.poller.start();
  assert.deepEqual(h.events.errors[0], ["network down", 1]);
  assert.equal(h.clock.nextDelay(), 1000);
  await h.clock.fireNext();
  assert.equal(h.clock.nextDelay(), 2000);
  await h.clock.fireNext();
  assert.equal(h.clock.nextDelay(), 4000);
  for (let i = 3; i < DEFAULT_MAX_FAILURES; i += 1) await h.clock.fireNext();
  assert.equal(h.poller.state, "paused");
  assert.equal(h.clock.pendingCount(), 0);
  h.setResponses([{ status: "running" }]);
  await h.poller.refresh();
  assert.equal(h.poller.state, "live");
  assert.equal(h.poller.failures, 0);
});

test("a success resets the failure count", async () => {
  const h = harness({ responses: [new Error("blip"), { status: "running" }, { status: "running" }] });
  await h.poller.start();
  assert.equal(h.poller.failures, 1);
  await h.clock.fireNext();
  assert.equal(h.poller.failures, 0);
  assert.equal(h.clock.nextDelay(), 2000);
});

test("live updates are bounded in time and then pause instead of polling forever", async () => {
  const h = harness({ options: { maxDurationMs: 10_000 } });
  await h.poller.start();
  h.clock.advance(11_000);
  await h.clock.fireNext();
  assert.equal(h.poller.state, "paused");
  assert.equal(h.clock.pendingCount(), 0);
});

test("a hidden tab polls less often", async () => {
  let hidden = false;
  const h = harness({ intervalFor: (d, { hidden: isHidden }) => (isHidden ? 15000 : 2000), options: { isHidden: () => hidden } });
  await h.poller.start();
  assert.equal(h.clock.nextDelay(), 2000);
  hidden = true;
  await h.clock.fireNext();
  assert.equal(h.clock.nextDelay(), 15000);
});

test("the interval never goes below a sane floor even if misconfigured", async () => {
  const h = harness({ intervalFor: () => 0 });
  await h.poller.start();
  assert.ok(h.clock.nextDelay() >= 250);
});

test("state transitions are reported in order", async () => {
  const h = harness({ responses: [{ status: "running" }, { status: "completed" }] });
  await h.poller.start();
  await h.clock.fireNext();
  h.poller.stop();
  assert.deepEqual(h.events.states, ["live", "finished", "stopped"]);
});
