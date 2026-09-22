import { test } from "node:test";
import assert from "node:assert/strict";
import {
  VOICE_STATE,
  createVoiceController,
  describeVoiceError,
  getRecognitionConstructor,
  isVoiceSupported,
  mergeTranscript,
} from "../assets/js/voiceInput.js";

// A fake browser SpeechRecognition.
function makeFake() {
  const instances = [];
  class FakeRecognition {
    constructor() {
      this.started = false;
      this.stopped = false;
      this.aborted = false;
      instances.push(this);
    }
    start() { this.started = true; }
    stop() { this.stopped = true; }
    abort() { this.aborted = true; }
    emitStart() { this.onstart && this.onstart(); }
    emitResults(results, resultIndex = 0) {
      this.onresult && this.onresult({ resultIndex, results: results.map(([text, isFinal]) => Object.assign([{ transcript: text }], { isFinal })) });
    }
    emitError(code) { this.onerror && this.onerror({ error: code }); }
    emitEnd() { this.onend && this.onend(); }
  }
  return { FakeRecognition, instances };
}

function setup(options = {}) {
  const { FakeRecognition, instances } = makeFake();
  const log = { states: [], interim: [], final: [], errors: [] };
  const timers = [];
  const controller = createVoiceController({
    Recognition: options.noRecognition ? undefined : FakeRecognition,
    lang: "en-US",
    onStateChange: (s) => log.states.push(s),
    onInterim: (t) => log.interim.push(t),
    onFinal: (t) => log.final.push(t),
    onError: (e) => log.errors.push(e),
    setTimeoutFn: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeoutFn: (h) => { if (timers[h - 1]) timers[h - 1].cleared = true; },
    ...(options.controller || {}),
  });
  return { controller, instances, log, timers };
}

// -- support detection ------------------------------------------------------------------------------------

test("browser support is detected from SpeechRecognition or its webkit prefix", () => {
  function Ctor() {}
  assert.equal(getRecognitionConstructor({ SpeechRecognition: Ctor }), Ctor);
  assert.equal(getRecognitionConstructor({ webkitSpeechRecognition: Ctor }), Ctor);
  assert.equal(isVoiceSupported({ webkitSpeechRecognition: Ctor }), true);
  assert.equal(isVoiceSupported({}), false);
  assert.equal(isVoiceSupported(null), false);
});

test("an unsupported browser is a clear state and start() does nothing", () => {
  const { controller, log } = setup({ noRecognition: true });
  assert.equal(controller.supported, false);
  assert.equal(controller.state, VOICE_STATE.UNSUPPORTED);
  assert.equal(controller.start(), false);
  assert.equal(log.errors[0].code, "unsupported");
  assert.match(log.errors[0].message, /still type/);
});

// -- push to talk -----------------------------------------------------------------------------------------

test("it never starts listening on its own: nothing happens until start() is called", () => {
  const { controller, instances, log } = setup();
  assert.equal(controller.state, VOICE_STATE.IDLE);
  assert.equal(instances.length, 0, "no recognition object is even created before Start");
  assert.deepEqual(log.states, []);
});

test("Start requests the microphone, becomes listening, and configures a bounded push-to-talk session", () => {
  const { controller, instances, log, timers } = setup();
  assert.equal(controller.start(), true);
  assert.equal(controller.state, VOICE_STATE.STARTING);
  const rec = instances[0];
  assert.equal(rec.started, true);
  assert.equal(rec.continuous, true); // keeps listening until the user presses Stop...
  assert.equal(rec.interimResults, true);
  assert.equal(rec.lang, "en-US");
  assert.equal(timers[0].ms, 120000); // ...but never indefinitely
  rec.emitStart();
  assert.equal(controller.state, VOICE_STATE.LISTENING);
  assert.deepEqual(log.states, [VOICE_STATE.STARTING, VOICE_STATE.LISTENING]);
});

test("a second Start while listening is ignored (no duplicate sessions)", () => {
  const { controller, instances } = setup();
  controller.start();
  instances[0].emitStart();
  assert.equal(controller.start(), false);
  assert.equal(instances.length, 1);
});

test("Stop asks the engine to finish and the phrase in flight is still delivered", () => {
  const { controller, instances, log } = setup();
  controller.start();
  instances[0].emitStart();
  controller.stop();
  assert.equal(controller.state, VOICE_STATE.STOPPING);
  assert.equal(instances[0].stopped, true);
  instances[0].emitResults([["please add pagination", true]]);
  instances[0].emitEnd();
  assert.deepEqual(log.final, ["please add pagination"]);
  assert.equal(controller.state, VOICE_STATE.IDLE);
});

test("the maximum listening time stops the session by itself", () => {
  const { controller, instances, timers } = setup();
  controller.start();
  instances[0].emitStart();
  timers[0].fn();
  assert.equal(instances[0].stopped, true);
  assert.equal(controller.state, VOICE_STATE.STOPPING);
});

// -- transcripts ------------------------------------------------------------------------------------------

test("interim text is a preview only; only FINAL phrases are delivered as text", () => {
  const { controller, instances, log } = setup();
  controller.start();
  instances[0].emitStart();
  instances[0].emitResults([["add pag", false]]);
  assert.deepEqual(log.final, []);
  assert.equal(log.interim.at(-1), "add pag");
  instances[0].emitResults([["add pagination", true]]);
  assert.deepEqual(log.final, ["add pagination"]);
  assert.equal(log.interim.at(-1), "");
});

test("multiple phrases arrive in order and empty ones are dropped", () => {
  const { controller, instances, log } = setup();
  controller.start();
  instances[0].emitStart();
  instances[0].emitResults([["first phrase", true], ["  ", true], ["second phrase", true]]);
  assert.deepEqual(log.final, ["first phrase", "second phrase"]);
});

test("results already delivered are not repeated (resultIndex)", () => {
  const { controller, instances, log } = setup();
  controller.start();
  instances[0].emitStart();
  instances[0].emitResults([["one", true], ["two", true]], 1);
  assert.deepEqual(log.final, ["two"]);
});

test("mergeTranscript appends to the typed text with one space, and never loses either side", () => {
  assert.equal(mergeTranscript("", "hello world"), "hello world");
  assert.equal(mergeTranscript("Fix the bug", "in the login page"), "Fix the bug in the login page");
  assert.equal(mergeTranscript("Fix the bug ", "now"), "Fix the bug now");
  assert.equal(mergeTranscript("Line one\n", "line two"), "Line one\nline two");
  assert.equal(mergeTranscript("keep", "   "), "keep");
  assert.equal(mergeTranscript(null, undefined), "");
  assert.equal(mergeTranscript("a", "  spaced   out  "), "a spaced out");
});

// -- errors -----------------------------------------------------------------------------------------------

test("a denied microphone is a distinct state with guidance", () => {
  const { controller, instances, log } = setup();
  controller.start();
  instances[0].emitError("not-allowed");
  assert.equal(controller.state, VOICE_STATE.DENIED);
  assert.equal(log.errors[0].code, "not-allowed");
  assert.match(log.errors[0].message, /blocked/);
  instances[0].emitEnd();
  assert.equal(controller.state, VOICE_STATE.DENIED, "the end event does not hide the denial");
  assert.equal(controller.start(), true, "the user can try again after changing the setting");
});

test("other errors are reported clearly and typing is never affected", () => {
  for (const [code, fragment] of [["no-speech", /No speech/], ["audio-capture", /No microphone/], ["network", /speech service/], ["weird", /error/]]) {
    const { controller, instances, log } = setup();
    controller.start();
    instances[0].emitError(code);
    assert.equal(controller.state, VOICE_STATE.ERROR, code);
    assert.match(log.errors[0].message, fragment);
    assert.deepEqual(log.final, []);
  }
  assert.match(describeVoiceError("service-not-allowed"), /blocked/);
});

test("'aborted' is our own doing and is not an error", () => {
  const { controller, instances, log } = setup();
  controller.start();
  instances[0].emitStart();
  instances[0].emitError("aborted");
  assert.deepEqual(log.errors, []);
  assert.equal(controller.state, VOICE_STATE.LISTENING);
});

test("if the engine throws on start the failure is reported and the controller stays usable", () => {
  class Broken { start() { throw new Error("boom"); } }
  const log = { errors: [], states: [] };
  const controller = createVoiceController({ Recognition: Broken, onError: (e) => log.errors.push(e), onStateChange: (s) => log.states.push(s) });
  assert.equal(controller.start(), false);
  assert.equal(log.errors[0].code, "start-failed");
  assert.equal(controller.state, VOICE_STATE.ERROR);
});

// -- abort / staleness ---------------------------------------------------------------------------------------

test("abort() stops immediately and a late result from that session is ignored", () => {
  const { controller, instances, log } = setup();
  controller.start();
  instances[0].emitStart();
  controller.abort();
  assert.equal(instances[0].aborted, true);
  assert.equal(controller.state, VOICE_STATE.IDLE);
  instances[0].emitResults([["too late", true]]);
  instances[0].emitEnd();
  assert.deepEqual(log.final, [], "nothing from an aborted session reaches the field");
});

test("a new session after abort is independent of the old one", () => {
  const { controller, instances, log } = setup();
  controller.start();
  controller.abort();
  controller.start();
  assert.equal(instances.length, 2);
  instances[1].emitStart();
  instances[0].emitResults([["ghost", true]]);
  instances[1].emitResults([["real", true]]);
  assert.deepEqual(log.final, ["real"]);
});

test("the controller only ever produces text: it exposes no way to submit, start a workflow or decide", () => {
  const { controller } = setup();
  assert.deepEqual(Object.keys(controller).sort(), ["abort", "start", "state", "stop", "supported"]);
});
