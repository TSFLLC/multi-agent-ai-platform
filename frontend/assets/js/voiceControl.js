// The microphone control shown beside an assignment field (MA7.6B).
//
// A small push-to-talk widget over voiceInput.js: press "Dictate" to start,
// "Stop" to finish; each finished phrase is APPENDED to the field's text, which
// stays editable. It never submits the form, starts a workflow or decides
// anything, never listens on its own, and stops if it leaves the page.
//
// All text is set through textContent.

import { el } from "./dom.js";
import { VOICE_STATE, createVoiceController, describeVoiceError, getRecognitionConstructor, mergeTranscript } from "./voiceInput.js";

const CHECK_CONNECTED_MS = 500;

// Only one field is dictated into at a time: starting one stops the other.
let activeController = null;

export const VOICE_PRIVACY_NOTE =
  "Uses your browser's built-in speech recognition. In some browsers (such as Chrome) the audio is processed by the browser vendor's service. Nothing is sent until you press Dictate, and nothing is submitted for you.";

// buildVoiceControl({ label, getValue, setValue, win })
//   getValue()      the field's current text
//   setValue(text)  put new text in the field (the caller keeps it editable)
export function buildVoiceControl({ label, getValue, setValue, win = typeof window !== "undefined" ? window : null }) {
  const Recognition = getRecognitionConstructor(win);
  const status = el("div", { class: "voice-status hint", role: "status", "aria-live": "polite" });
  const preview = el("div", { class: "voice-preview hint" });
  let watcher = null;
  let button;

  const controller = createVoiceController({
    Recognition,
    lang: win && win.navigator ? win.navigator.language : undefined,
    onStateChange: (state) => paint(state),
    onInterim: (text) => {
      preview.textContent = text ? `… ${text}` : "";
    },
    onFinal: (text) => setValue(mergeTranscript(getValue(), text)),
    onError: ({ message }) => {
      status.textContent = message;
    },
  });

  function stopWatching() {
    if (watcher !== null) {
      clearInterval(watcher);
      watcher = null;
    }
  }

  function paint(state) {
    const listening = state === VOICE_STATE.LISTENING || state === VOICE_STATE.STARTING || state === VOICE_STATE.STOPPING;
    button.textContent = listening ? "■ Stop" : "🎤 Dictate";
    button.disabled = state === VOICE_STATE.UNSUPPORTED || state === VOICE_STATE.STOPPING;
    button.setAttribute("aria-pressed", listening ? "true" : "false");
    button.classList.toggle("recording", state === VOICE_STATE.LISTENING);
    if (state === VOICE_STATE.STARTING) status.textContent = "Waiting for microphone permission…";
    else if (state === VOICE_STATE.LISTENING) status.textContent = "Listening. Speak, then press Stop. Your words are added to the text, which you can edit.";
    else if (state === VOICE_STATE.STOPPING) status.textContent = "Finishing…";
    else if (state === VOICE_STATE.UNSUPPORTED) status.textContent = describeVoiceError("unsupported");
    else if (state === VOICE_STATE.IDLE) status.textContent = "";
    if (listening) {
      if (watcher === null) {
        // If this control leaves the page (dialog closed, route changed) the microphone must stop.
        watcher = setInterval(() => {
          if (!button.isConnected) {
            controller.abort();
            stopWatching();
          }
        }, CHECK_CONNECTED_MS);
      }
    } else {
      stopWatching();
      if (activeController === controller) activeController = null;
      if (state !== VOICE_STATE.LISTENING) preview.textContent = "";
    }
  }

  button = el(
    "button",
    {
      type: "button",
      class: "voice-button small",
      "aria-label": `Dictate ${label}`,
      title: "Press to start speaking, press again to stop",
      onclick: () => {
        const state = controller.state;
        if (state === VOICE_STATE.LISTENING || state === VOICE_STATE.STARTING) {
          controller.stop();
          return;
        }
        if (activeController && activeController !== controller) activeController.stop();
        activeController = controller;
        controller.start();
      },
    },
    "🎤 Dictate"
  );

  const container = el("div", { class: "voice-control" }, [
    el("div", { class: "row voice-row" }, [button, status]),
    preview,
    el("div", { class: "hint voice-privacy" }, VOICE_PRIVACY_NOTE),
  ]);
  paint(controller.state);
  return { element: container, controller, stop: () => controller.stop(), abort: () => controller.abort() };
}
