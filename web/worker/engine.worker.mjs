// Web Worker that hosts the Python engine (Pyodide).
//
// page -> worker : { id, method, params }
// worker -> page : { id, ok: true, result } | { id, ok: false, error: { code, message, detail } }
//                  { id, event: "progress", stage, loaded, total }   download progress
//                  { id, event: "tile", done, total }                export progress
//                  { id, event: "file", name, mime, bytes }          one finished output file
//
// A module worker is required: current Pyodide ships as ES modules only.

const ROOT = new URL("../", import.meta.url);
const PYODIDE_URL = new URL("vendor/pyodide/", ROOT).href;
const APP_DIR = "/home/pyodide/app";

let pyodide = null;
let bridge = null;
const logLines = [];

const post = (message, transfer = []) => self.postMessage(message, transfer);
const log = (line) => { logLines.push(String(line)); if (logLines.length > 200) logLines.shift(); };

// ---------------------------------------------------------------------------
// Download progress: every fetch made while a scope is active is counted as
// its body streams in.  Nothing is fetched twice and no cache is assumed.
// ---------------------------------------------------------------------------
let scope = null;
const nativeFetch = self.fetch.bind(self);

// A download that fails while the engine starts must fail the start.  Pyodide leaves some of
// these as unhandled promise rejections and the page would otherwise wait for ever.
let failBoot = null;
function downloadFailed(reason) {
  if (!failBoot) return;
  const text = String((reason && reason.message) || reason).slice(0, 200);
  failBoot(engineError("OFFLINE", "", `download failed: ${text}`));
}
self.addEventListener("unhandledrejection", (event) => {
  if (!failBoot) return;
  event.preventDefault();
  downloadFailed(event.reason);
});

self.fetch = async (input, init) => {
  let response;
  try {
    response = await nativeFetch(input, init);
  } catch (error) {
    downloadFailed(error);
    throw error;
  }
  if (!response.ok) downloadFailed(new Error(`HTTP ${response.status}`));
  const current = scope;
  if (!current || !response.ok || !response.body || typeof TransformStream === "undefined") return response;
  try {
    const counter = new TransformStream({
      transform(chunk, controller) {
        current.loaded += chunk.byteLength;
        current.report(false);
        controller.enqueue(chunk);
      },
    });
    return new Response(response.body.pipeThrough(counter), {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  } catch (error) {
    return response;
  }
};

function openScope(id, stage, total) {
  let last = 0;
  scope = {
    loaded: 0,
    report(force) {
      const now = performance.now();
      if (!force && now - last < 80) return;
      last = now;
      post({ id, event: "progress", stage, loaded: Math.min(this.loaded, total || this.loaded), total });
    },
  };
  scope.report(true);
  return () => { if (scope) scope.report(true); scope = null; };
}

// ---------------------------------------------------------------------------
// Conversions
// ---------------------------------------------------------------------------
function toJs(result) {
  if (result && typeof result.toJs === "function") {
    const value = result.toJs({ dict_converter: Object.fromEntries, create_pyproxies: false });
    result.destroy();
    return value;
  }
  return result;
}

function collectBuffers(value, out, seen) {
  if (!value || typeof value !== "object") return;
  if (ArrayBuffer.isView(value)) {
    if (!seen.has(value.buffer)) { seen.add(value.buffer); out.push(value.buffer); }
    return;
  }
  if (Array.isArray(value)) { for (const item of value) collectBuffers(item, out, seen); return; }
  for (const key of Object.keys(value)) collectBuffers(value[key], out, seen);
}

// WebAssembly memory only ever grows; the page restarts the worker after very large jobs.
function heapBytes() {
  try { return pyodide._module.HEAPU8.length; } catch (error) { return 0; }
}

function engineError(code, message, detail) {
  const error = new Error(message || code);
  error.engine = { code, message: message || "", detail: detail || "" };
  return error;
}

// ---------------------------------------------------------------------------
// Methods
// ---------------------------------------------------------------------------
const handlers = {
  async boot({ bootBytes = 0, appleTouch = false } = {}, id) {
    if (bridge) return { result: { already: true } };
    if (typeof WebAssembly !== "object") throw engineError("NO_WASM", "WebAssembly를 지원하지 않는 브라우저예요.");
    // iPhone / iPad: Python must use its JavaScript call trampoline (the wasm-gc one crashed on
    // iOS 18, WebKit bug 293113).  Pyodide decides this from the user agent, but inside a worker
    // on iPadOS the agent says "Macintosh", so the page tells us and we make the agent say so.
    if (appleTouch && !/iPad|iPhone|iPod/.test(navigator.userAgent)) {
      try {
        const agent = navigator.userAgent;
        Object.defineProperty(self.navigator, "userAgent", { get: () => `${agent} iPad`, configurable: true });
      } catch (error) { /* keep going with the default */ }
    }
    const started = performance.now();
    const close = openScope(id, "engine", bootBytes);
    const failed = new Promise((resolve, reject) => { failBoot = reject; });
    failed.catch(() => {});
    const load = async () => {
      const { loadPyodide, version } = await import(PYODIDE_URL + "pyodide.mjs");
      pyodide = await loadPyodide({
        indexURL: PYODIDE_URL,
        packages: ["pillow"],
        stdout: log,
        stderr: log,
      });
      const response = await fetch(new URL("py/splitcore.zip", ROOT));
      if (!response.ok) throw new Error(`splitcore.zip ${response.status}`);
      const archive = await response.arrayBuffer();
      pyodide.FS.mkdirTree(APP_DIR);
      pyodide.unpackArchive(archive, "zip", { extractDir: APP_DIR });
      pyodide.runPython(`import sys\nif ${JSON.stringify(APP_DIR)} not in sys.path:\n    sys.path.insert(0, ${JSON.stringify(APP_DIR)})`);
      bridge = pyodide.pyimport("splitcore.bridge");
      const info = toJs(bridge.dispatch("info", {}, undefined));
      return { result: { pyodide: version, ms: Math.round(performance.now() - started), ...info } };
    };
    try {
      return await Promise.race([load(), failed]);
    } finally {
      failBoot = null;
      close();
    }
  },

  // Optional packages (PyMuPDF for PDF, olefile for HWP / legacy Office) load on first use.
  async ensure({ packages = [], bytes = 0 }, id) {
    const wanted = packages.filter((name) => !(pyodide.loadedPackages && pyodide.loadedPackages[name]));
    if (!wanted.length) return { result: { loaded: [] } };
    const close = openScope(id, "package", bytes);
    // A download that stops half-way (a tunnel, a switch between Wi-Fi and mobile data) would wait
    // for ever: give up when not a single byte arrived between two checks.  The page uses a fresh
    // worker for the next try, because the abandoned download keeps this one's loader busy.
    let watch = 0;
    const stalled = new Promise((resolve, reject) => {
      let seen = -1;
      watch = setInterval(() => {
        const now = scope ? scope.loaded : seen;
        if (now === seen) reject(engineError("OFFLINE", "", "package download stalled"));
        seen = now;
      }, 45000);
    });
    stalled.catch(() => {});
    try {
      await Promise.race([pyodide.loadPackage(wanted, { messageCallback: log, errorCallback: log }), stalled]);
    } catch (error) {
      if (error && error.engine) throw error;
      throw engineError("OFFLINE", "", `package download failed: ${String((error && error.message) || error).slice(0, 200)}`);
    } finally {
      clearInterval(watch);
      close();
    }
    // loadPackage reports a failed download through errorCallback only: check what arrived
    const plain = (name) => String(name).toLowerCase().replace(/[-_.]+/g, "-");
    const loaded = new Set(Object.keys(pyodide.loadedPackages || {}).map(plain));
    const missing = wanted.filter((name) => !loaded.has(plain(name)));
    if (missing.length) throw engineError("OFFLINE", "", `package not loaded: ${missing.join(", ")}`);
    return { result: { loaded: wanted } };
  },

  async call({ method, args = {} }, id) {
    if (!bridge) throw engineError("INTERNAL", "", "engine not booted");
    const callbacks = {
      progress: (done, total) => post({ id, event: "tile", done, total }),
      emit: (name, mime, pyBytes) => {
        // one copy out of the WebAssembly heap, then a zero-copy transfer to the page
        const view = pyBytes.getBuffer("u8");
        let copy;
        try { copy = view.data.slice(); } finally { view.release(); }
        post({ id, event: "file", name, mime, bytes: copy }, [copy.buffer]);
      },
    };
    const raw = bridge.dispatch(method, args || {}, method === "export" ? callbacks : undefined);
    const value = toJs(raw) || {};
    if (value.ok === false) {
      const e = value.error || {};
      throw engineError(e.code || "INTERNAL", e.message, e.detail);
    }
    const transfer = [];
    collectBuffers(value, transfer, new Set());
    if (method === "export") value.heap = heapBytes();
    return { result: value, transfer };
  },

  logs() {
    return { result: { lines: logLines.slice(-80) } };
  },
};

self.onmessage = async (event) => {
  const { id, method, params } = event.data || {};
  try {
    const handler = handlers[method];
    if (!handler) throw engineError("INTERNAL", "", `unknown method ${method}`);
    const { result, transfer } = await handler(params || {}, id);
    post({ id, ok: true, result }, transfer || []);
  } catch (error) {
    let payload = error && error.engine;
    if (!payload) {
      const text = String((error && error.message) || error);
      const memory = /out of memory|memory access out of bounds|allocation failed|Cannot enlarge memory|MemoryError|RangeError/i.test(text);
      payload = { code: memory ? "OUT_OF_MEMORY" : "INTERNAL", message: "", detail: text.slice(-600) };
    }
    post({ id, ok: false, error: payload });
  }
};

post({ event: "alive" });
