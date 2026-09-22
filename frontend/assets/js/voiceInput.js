// Voice input for the assignment text (MA7.6B) -- push to talk, never automatic.
// Pure and unit-tested with a fake recognition object
// (frontend/tests/voiceInput.test.mjs): no DOM, no hardware.
//
// The V1 behaviour, and what this module guarantees:
//   * the user explicitly presses Start, then Stop -- it never listens on its own
//     (a hard time limit and abort() also end it);
//   * it only ever produces TEXT for the caller to place in an editable field. It
//     never submits anything, starts a workflow, or decides an approval;
//   * an unsupported browser or a denied permission is a clear state, and typing
//     keeps working exactly as before.
//
// It uses the browser's own speech recognition (window.SpeechRecognition or the
// prefixed webkitSpeechRecognition). That is a browser feature: on Chrome and Edge
// the audio is processed by the browser vendor's speech service, and it is not
// available in every browser (notably Firefox). No provider or backend integration
// is involved, and none is added by MA7.6B.

export const VOICE_STATE = {
  UNSUPPORTED: "unsupported",
  IDLE: "idle",
  STARTING: "starting", // asked for the microphone; waiting for permission / the engine
  LISTENING: "listening",
  STOPPING: "stopping",
  DENIED: "denied",
  ERROR: "error",
};

export const DEFAULT_MAX_LISTEN_MS = 2 * 60 * 1000;

export function getRecognitionConstructor(win) {
  if (!win) return null;
  return win.SpeechRecognition || win.webkitSpeechRecognition || null;
}

export function isVoiceSupported(win) {
  return typeof getRecognitionConstructor(win) === "function";
}

// Appends spoken text to what is already typed, with one separating space.
export function mergeTranscript(existing, spoken) {
  const addition = String(spoken == null ? "" : spoken).replace(/\s+/g, " ").trim();
  const current = String(existing == null ? "" : existing);
  if (!addition) return current;
  if (!current) return addition;
  return /\s$/.test(current) ? `${current}${addition}` : `${current} ${addition}`;
}

export function describeVoiceError(code) {
  switch (code) {
    case "unsupported":
      return "Voice input is not supported in this browser. You can still type your assignment.";
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access was blocked. Allow the microphone for this site in your browser settings, or type instead.";
    case "no-speech":
      return "No speech was detected. Press Start and try again, or type instead.";
    case "audio-capture":
      return "No microphone was found. Check that one is connected, or type instead.";
    case "network":
      return "The browser's speech service could not be reached. Try again, or type instead.";
    case "language-not-supported":
      return "This language is not supported for voice input. Type instead.";
    case "start-failed":
      return "Voice input could not start. Try again, or type instead.";
    default:
      return "Voice input stopped because of an error. You can try again, or type instead.";
  }
}

// createVoiceController({ Recognition, ... }) -> { state, supported, start, stop, abort }
//   onStateChange(state)   every state transition
//   onInterim(text)        the still-changing phrase (for a preview only; "" clears it)
//   onFinal(text)          a finished phrase -- append it to the field
//   onError({ code, message })
export function createVoiceController({
  Recognition,
  lang,
  maxListenMs = DEFAULT_MAX_LISTEN_MS,
  onStateChange = () => {},
  onInterim = () => {},
  onFinal = () => {},
  onError = () => {},
  setTimeoutFn = (fn, ms) => setTimeout(fn, ms),
  clearTimeoutFn = (handle) => clearTimeout(handle),
} = {}) {
  const supported = typeof Recognition === "function";
  let state = supported ? VOICE_STATE.IDLE : VOICE_STATE.UNSUPPORTED;
  let recognition = null;
  let generation = 0;
  let timer = null;

  function setState(next) {
    if (state === next) return;
    state = next;
    onStateChange(next);
  }

  function clearTimer() {
    if (timer !== null) {
      clearTimeoutFn(timer);
      timer = null;
    }
  }

  function fail(code) {
    setState(code === "not-allowed" || code === "service-not-allowed" ? VOICE_STATE.DENIED : VOICE_STATE.ERROR);
    onError({ code, message: describeVoiceError(code) });
  }

  function start() {
    if (!supported) {
      onError({ code: "unsupported", message: describeVoiceError("unsupported") });
      return false;
    }
    if (state === VOICE_STATE.STARTING || state === VOICE_STATE.LISTENING || state === VOICE_STATE.STOPPING) return false;
    const gen = (generation += 1);
    let instance;
    try {
      instance = new Recognition();
      instance.continuous = true;
      instance.interimResults = true;
      instance.maxAlternatives = 1;
      if (lang) instance.lang = lang;
      instance.onstart = () => {
        if (gen === generation && state === VOICE_STATE.STARTING) setState(VOICE_STATE.LISTENING);
      };
      instance.onresult = (event) => {
        if (gen !== generation) return;
        let interim = "";
        for (let i = event.resultIndex || 0; i < event.results.length; i += 1) {
          const result = event.results[i];
          const text = result && result[0] ? result[0].transcript : "";
          if (result.isFinal) {
            const finished = String(text).trim();
            if (finished) onFinal(finished);
          } else {
            interim += text;
          }
        }
        onInterim(interim.trim());
      };
      instance.onerror = (event) => {
        if (gen !== generation) return;
        const code = event && event.error;
        if (code === "aborted") return; // we asked for it
        fail(code || "unknown");
      };
      instance.onend = () => {
        if (gen !== generation) return;
        clearTimer();
        onInterim("");
        recognition = null;
        if (state !== VOICE_STATE.DENIED && state !== VOICE_STATE.ERROR) setState(VOICE_STATE.IDLE);
      };
      recognition = instance;
      setState(VOICE_STATE.STARTING);
      instance.start();
    } catch {
      recognition = null;
      generation += 1;
      fail("start-failed");
      return false;
    }
    clearTimer();
    timer = setTimeoutFn(() => stop(), maxListenMs); // it never listens indefinitely
    return true;
  }

  // Ask the engine to finish: any phrase still being processed is still delivered.
  function stop() {
    if (!recognition) return;
    if (state === VOICE_STATE.STARTING || state === VOICE_STATE.LISTENING) setState(VOICE_STATE.STOPPING);
    try {
      recognition.stop();
    } catch {
      // already stopped
    }
  }

  // Cancel immediately and drop anything in flight (used when the page or dialog goes away).
  function abort() {
    generation += 1;
    clearTimer();
    const current = recognition;
    recognition = null;
    if (current) {
      try {
        current.abort();
      } catch {
        // already stopped
      }
    }
    onInterim("");
    if (state !== VOICE_STATE.UNSUPPORTED && state !== VOICE_STATE.IDLE) setState(VOICE_STATE.IDLE);
  }

  return {
    start,
    stop,
    abort,
    get state() {
      return state;
    },
    supported,
  };
}
