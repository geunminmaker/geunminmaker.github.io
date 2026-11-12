// Client side of the Python engine: starts the worker, speaks its protocol,
// and offers the handful of operations the screens need.

const WORKER_URL = new URL("../worker/engine.worker.mjs", import.meta.url);
const ASSETS_URL = new URL("../assets.json", import.meta.url);

export const MAX_FILE_BYTES = 300 * 1024 * 1024;
/** Largest bitmap the browser fallback decoder produces (iOS canvas limit is 16.7 MP). */
const MAX_CANVAS_PIXELS = 16_000_000;
const APPLE_TOUCH = typeof navigator !== "undefined" && (/iPad|iPhone|iPod/.test(navigator.userAgent || "")
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1));

/**
 * WebAssembly memory never shrinks: above this the engine is restarted once it is idle.
 * iPhone and iPad do not report low memory, they close the page, so they get the tighter limits.
 */
const RECYCLE_HEAP_BYTES = (APPLE_TOUCH ? 250 : 400) * 1024 * 1024;

/** Tile size limit for devices with little memory (0 = the engine's own 25 MP). */
function defaultMaxPixels() {
  if (APPLE_TOUCH) return 16_000_000;  // Safari has no deviceMemory to ask
  const memory = typeof navigator !== "undefined" ? navigator.deviceMemory : undefined;  // GB, Chromium only
  if (typeof memory === "number" && memory <= 2) return 12_500_000;
  return 0;
}

function isHeic(head) {
  if (head.length < 12) return false;
  const tag = String.fromCharCode(head[4], head[5], head[6], head[7]);
  const brand = String.fromCharCode(head[8], head[9], head[10], head[11]);
  return tag === "ftyp" && ["heic", "heix", "hevc", "hevx", "heim", "heis", "mif1", "msf1"].includes(brand);
}

export class EngineError extends Error {
  constructor(code, message = "", detail = "") {
    super(message || code);
    this.name = "EngineError";
    this.code = code;
    this.detail = detail;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class Engine {
  constructor() {
    this.state = "idle";            // idle | loading | ready | failed
    this.progress = { loaded: 0, total: 0 };
    this.info = null;
    this.assets = null;
    this.error = null;
    this._worker = null;
    this._pending = new Map();
    this._seq = 0;
    this._ready = null;
    this._listeners = new Set();
    this.gate = null;               // optional async function awaited before the engine is downloaded
    this._opened = [];              // [{ name, blob, password }] for replay after a restart
    this._packages = new Set();     // optional packages downloaded so far
    this._urls = new Map();         // cache key -> { url, width, height }
    this._generation = 0;           // number of the current start(); an older start must not touch anything
    this._epoch = 0;                // bumped by reset(): answers that belong to an earlier session are dropped
    this._recycle = false;          // restart before the next call (memory hygiene)
  }

  // ------------------------------------------------------------ state
  subscribe(listener) {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  _emit() {
    for (const listener of this._listeners) {
      try { listener(this); } catch (error) { console.error(error); }
    }
  }

  _set(state, error = null) {
    this.state = state;
    this.error = error;
    this._emit();
  }

  /** Start (or keep) loading the engine.  Safe to call many times. */
  start() {
    if (this._ready) return this._ready;
    this._set("loading");
    // restart() moves on to the next number.  Whatever an abandoned start still does - finish
    // or fail - must not dispose of the worker that replaced it or change the state.
    const generation = ++this._generation;
    const current = () => generation === this._generation;
    const ready = this._boot(generation).then(() => this._replay(generation)).then(
      () => {
        if (!current()) throw new EngineError("CANCELLED");
        this._set("ready");
      },
      (error) => {
        if (current()) {
          this._ready = null;
          this._dispose();
          this._set("failed", error);
        }
        throw error;
      },
    );
    ready.catch(() => {});
    this._ready = ready;
    return ready;
  }

  async _boot(generation) {
    if (typeof Worker === "undefined" || typeof WebAssembly !== "object") {
      throw new EngineError("NO_WASM", "이 브라우저에서는 실행할 수 없어요.");
    }
    if (!this.assets) {
      try {
        const response = await fetch(ASSETS_URL, { cache: "no-cache" });
        this.assets = response.ok ? await response.json() : {};
      } catch (error) {
        this.assets = {};
      }
    }
    if (this.gate) {
      try { await this.gate(); } catch (error) { /* start anyway */ }
    }
    if (generation !== this._generation) throw new EngineError("CANCELLED");  // replaced while waiting
    let worker;
    try {
      worker = new Worker(WORKER_URL, { type: "module", name: "split-engine" });
    } catch (error) {
      throw new EngineError("NO_WASM", "이 브라우저에서는 실행할 수 없어요.", String(error));
    }
    this._worker = worker;
    worker.onmessage = (event) => this._onMessage(event.data);
    const crash = (event) => {
      if (generation !== this._generation) return;
      const detail = (event && (event.message || event.type)) || "worker error";
      this._failAll(new EngineError("ENGINE_CRASHED", "", String(detail)));
      this._ready = null;
      this._dispose();
      this._set("failed", new EngineError("ENGINE_CRASHED", "", String(detail)));
    };
    worker.onerror = crash;
    worker.onmessageerror = crash;
    this.progress = { loaded: 0, total: this.assets.boot_bytes || 0 };
    this.info = await this._send("boot", { bootBytes: this.assets.boot_bytes || 0, appleTouch: APPLE_TOUCH }, [], {
      progress: (event) => {
        this.progress = { loaded: event.loaded, total: event.total };
        this._emit();
      },
    });
  }

  _dispose() {
    if (this._worker) {
      this._worker.onmessage = null;
      this._worker.onerror = null;
      this._worker.terminate();
      this._worker = null;
    }
  }

  _failAll(error) {
    for (const entry of this._pending.values()) entry.reject(error);
    this._pending.clear();
  }

  /** Reopen the files of this session in a fresh engine (same order, so ids stay the same). */
  async _replay(generation) {
    const list = this._opened;
    for (const item of list) {
      if (this._opened !== list) break;  // reset() ran meanwhile: these files are no longer wanted
      try {
        await this._addBlob(item.name, item.blob, item.password, null, true);
      } catch (error) {
        /* only files that opened before are replayed; a failure here leaves that file out */
      }
      if (generation !== this._generation) throw new EngineError("CANCELLED");
    }
    if (this._opened !== list) {
      // The session was reset while its files were being reopened: drop them from the worker, then
      // reopen what belongs to the new session, so its ids start at the beginning again.
      await this._raw("clear");
      if (generation !== this._generation) throw new EngineError("CANCELLED");
      await this._replay(generation);
    }
  }

  /** Stop whatever the engine is doing, start a fresh one and reopen the same files. */
  async restart() {
    this._generation += 1;
    this._failAll(new EngineError("CANCELLED"));
    this._dispose();
    this._ready = null;
    this._recycle = false;
    await this.start();
  }

  /** Alias used by the screens: cancel the running job. */
  cancel() { return this.restart(); }

  // ------------------------------------------------------------ protocol
  _send(method, params, transfer = [], handlers = {}) {
    if (!this._worker) return Promise.reject(new EngineError("ENGINE_CRASHED"));
    const id = ++this._seq;
    return new Promise((resolve, reject) => {
      this._pending.set(id, { resolve, reject, handlers });
      try {
        this._worker.postMessage({ id, method, params }, transfer);
      } catch (error) {
        this._pending.delete(id);
        reject(new EngineError("INTERNAL", "", String(error)));
      }
    });
  }

  _onMessage(message) {
    if (!message || message.id == null) return;
    const entry = this._pending.get(message.id);
    if (!entry) return;
    if (message.event) {
      const handler = entry.handlers[message.event];
      if (handler) {
        try { handler(message); } catch (error) { console.error(error); }
      }
      return;
    }
    this._pending.delete(message.id);
    if (message.ok) entry.resolve(message.result);
    else {
      const e = message.error || {};
      entry.reject(new EngineError(e.code || "INTERNAL", e.message, e.detail));
    }
  }

  async call(method, args = {}, transfer = [], handlers = {}) {
    if (this._recycle && this._pending.size === 0) await this.restart();
    await this.start();
    return this._send("call", { method, args }, transfer, handlers);
  }

  /** Internal call used while the engine is still booting (file replay). */
  _raw(method, args = {}, transfer = [], handlers = {}) {
    return this._send("call", { method, args }, transfer, handlers);
  }

  async _ensure(packages, onProgress) {
    if (!packages || !packages.length) return;
    const sizes = (this.assets && this.assets.packages) || {};
    const bytes = packages.reduce((sum, name) => sum + ((sizes[name] && sizes[name].size) || 0), 0);
    try {
      await this._send("ensure", { packages, bytes }, [], {
        progress: (event) => onProgress && onProgress({ stage: "package", loaded: event.loaded, total: event.total, packages }),
      });
    } catch (error) {
      // a download that failed or stalled half-way keeps the worker's package loader busy: next time use a fresh worker
      if (error instanceof EngineError && error.code === "OFFLINE") this._recycle = true;
      throw error;
    }
    const before = this._packages.size;
    for (const name of packages) this._packages.add(name);
    if (this._packages.size !== before && this.state === "ready") this._emit();  // more files to keep for offline use
  }

  /** Engine files this page has downloaded so far (the service worker keeps them for offline use). */
  usedFiles() {
    const assets = this.assets || {};
    const files = ["vendor/pyodide/pyodide.mjs", "vendor/pyodide/pyodide.asm.mjs"];
    for (const item of assets.boot || []) files.push(item.file);
    for (const name of this._packages) {
      const entry = (assets.packages || {})[name];
      if (entry) files.push(entry.file);
    }
    return Array.from(new Set(files));
  }

  // ------------------------------------------------------------ files
  /** Forget every opened file (new session). */
  async reset() {
    this._epoch += 1;
    this._opened = [];
    for (const entry of this._urls.values()) URL.revokeObjectURL(entry.url);
    this._urls.clear();
    if (this.state === "ready") {
      try { await this.call("clear"); } catch (error) { /* a broken engine is restarted on next use */ }
    }
  }

  async _addBlob(name, blob, password, onProgress, booting = false) {
    const call = booting ? this._raw.bind(this) : this.call.bind(this);
    const head = new Uint8Array(await blob.slice(0, 2048).arrayBuffer());
    const probe = await call("probe", { name, head });
    await this._ensure(probe.packages, onProgress);
    for (let attempt = 0; ; attempt += 1) {
      const data = new Uint8Array(await blob.arrayBuffer());
      try {
        return await call("add_file", { name, data, password: password || null }, [data.buffer]);
      } catch (error) {
        // The engine found out late that it needs an optional package (e.g. a TIFF scan that
        // only MuPDF can decode): load it and try once more.
        if (attempt === 0 && error instanceof EngineError && error.code === "NEED_PACKAGE" && error.detail) {
          await this._ensure([error.detail], onProgress);
          continue;
        }
        throw error;
      }
    }
  }

  /**
   * Open one picked file.  Resolves to { file, sources:[...], truncated, total }.
   * Rejects with EngineError (code ENCRYPTED asks for a password, etc).
   */
  async openFile(file, { password = null, onProgress = null } = {}) {
    const name = file.name || "파일";
    if (file.size === 0) throw new EngineError("EMPTY_FILE");
    if (file.size > MAX_FILE_BYTES) throw new EngineError("TOO_LARGE");
    await this.start();
    const epoch = this._epoch;
    let blob = file;
    let result;
    // HEIC: Safari decodes it natively and fast; the engine's own decoder takes many seconds.
    const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
    if (isHeic(head)) {
      // Above the canvas limit the browser hands back a smaller picture.  On a computer the engine's
      // own decoder (slower, every pixel kept) takes those; on iPhone and iPad memory is the tighter
      // limit, so the smaller picture is used and the confirm screen says so.
      const png = await decodeWithBrowser(file, { refuseLarge: !APPLE_TOUCH }).catch(() => null);
      if (png) {
        result = await this._addBlob(name, png, null, onProgress);
        result.converted = true;
        if (png.reducedFrom) {
          for (const source of result.sources) source.reduced_from = png.reducedFrom;
        }
        if (epoch !== this._epoch) throw new EngineError("CANCELLED");  // the session was reset meanwhile
        this._opened.push({ name, blob: png, password: null });
        return result;
      }
    }
    try {
      result = await this._addBlob(name, blob, password, onProgress);
    } catch (error) {
      const imageLike = /^image\//.test(file.type || "") || /^image\//.test(error.detail || "");
      if (!(error instanceof EngineError) || error.code !== "UNSUPPORTED" || !imageLike) throw error;
      // The engine cannot read this picture type (HEIC, AVIF, SVG ...): let the browser decode it.
      const png = await decodeWithBrowser(file).catch(() => null);
      if (!png) throw error;
      blob = png;
      result = await this._addBlob(name, blob, null, onProgress);
      result.converted = true;
      if (png.reducedFrom) {  // the browser could only draw it smaller: the confirm screen says so
        for (const source of result.sources) source.reduced_from = png.reducedFrom;
      }
    }
    if (epoch !== this._epoch) throw new EngineError("CANCELLED");  // the session was reset meanwhile
    this._opened.push({ name, blob, password });
    return result;
  }

  async _image(method, id, maxPx) {
    const key = `${method}:${id}:${maxPx}`;
    if (this._urls.has(key)) return this._urls.get(key);
    const epoch = this._epoch;
    const result = await this.call(method, { id, max_px: maxPx });
    // ids start again at s1 in every session: an answer that was asked for before reset() is a
    // picture of the previous file and must never be kept under this id
    if (epoch !== this._epoch) throw new EngineError("CANCELLED");
    const entry = {
      url: URL.createObjectURL(new Blob([result.bytes], { type: result.mime })),
      width: result.width,
      height: result.height,
    };
    this._urls.set(key, entry);
    return entry;
  }

  thumbnail(id, maxPx = 360) { return this._image("thumbnail", id, maxPx); }

  preview(id, maxPx = 1600) { return this._image("preview", id, maxPx); }

  async layouts(id) { return (await this.call("layouts", { id })).layouts; }

  _options(options) {
    const merged = { ...(options || {}) };
    const limit = defaultMaxPixels();
    if (limit && !merged.max_pixels) merged.max_pixels = limit;
    return merged;
  }

  describe(id, rows, cols, format = null, options = {}) {
    return this.call("describe", { id, rows, cols, format, options: this._options(options) });
  }

  /**
   * Produce the output files.  Resolves to { files: [{ name, mime, blob }], plan }.
   * onTile(done, total) reports progress.
   */
  async export(id, rows, cols, format, options = {}, { onTile = null } = {}) {
    const files = [];
    const result = await this.call("export", { id, rows, cols, format, options: this._options(options) }, [], {
      tile: (event) => onTile && onTile(event.done, event.total),
      file: (event) => files.push({ name: event.name, mime: event.mime, blob: new Blob([event.bytes], { type: event.mime }) }),
    });
    if (result.heap > RECYCLE_HEAP_BYTES) this._recycle = true;  // restart before the next call
    return { files, plan: result.plan };
  }
}

// ---------------------------------------------------------------------------
// Browser fallback decoder
// ---------------------------------------------------------------------------
/**
 * Decode a picture the engine cannot read and hand it back as PNG.
 * refuseLarge: fail instead of shrinking when the picture is above the canvas limit.
 * A blob that had to be shrunk carries `reducedFrom` = the original { width, height }.
 */
async function decodeWithBrowser(file, { refuseLarge = false } = {}) {
  let source = null;
  let width = 0;
  let height = 0;
  if (typeof createImageBitmap === "function") {
    try {
      source = await createImageBitmap(file, { imageOrientation: "from-image" });
      width = source.width;
      height = source.height;
    } catch (error) {
      source = null;
    }
  }
  let objectUrl = null;
  if (!source) {
    objectUrl = URL.createObjectURL(file);
    const image = new Image();
    image.decoding = "async";
    image.src = objectUrl;
    try {
      await image.decode();
    } catch (error) {
      URL.revokeObjectURL(objectUrl);
      throw error;
    }
    source = image;
    width = image.naturalWidth || 0;
    height = image.naturalHeight || 0;
    if (!width || !height) {  // SVG without intrinsic size
      width = 2048;
      height = 2048;
    }
    if (/svg/i.test(file.type || file.name || "") && Math.max(width, height) < 3000) {
      const grow = 3000 / Math.max(width, height);  // vector art: rasterise generously
      width = Math.round(width * grow);
      height = Math.round(height * grow);
    }
  }
  try {
    if (!width || !height) throw new Error("empty image");
    const pixels = width * height;
    let reducedFrom = null;
    if (pixels > MAX_CANVAS_PIXELS) {
      if (refuseLarge) throw new Error("too large for a canvas");
      reducedFrom = { width, height };
      const shrink = Math.sqrt(MAX_CANVAS_PIXELS / pixels);
      width = Math.max(1, Math.floor(width * shrink));
      height = Math.max(1, Math.floor(height * shrink));
    }
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    context.drawImage(source, 0, 0, width, height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    canvas.width = 0;
    canvas.height = 0;
    if (!blob) throw new Error("toBlob failed");
    if (reducedFrom) blob.reducedFrom = reducedFrom;
    return blob;
  } finally {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    if (source && typeof source.close === "function") source.close();
  }
}

export { sleep };
