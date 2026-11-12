// Service worker: keeps the app usable offline.
//
//  * App shell (HTML, CSS, JS, icons) is cached when this worker installs and
//    is always served from that cache, so every screen of one build matches.
//  * Engine files (Pyodide, Python wheels - about 31 MB) are cached the first
//    time they are fetched and on request from the page ("precache-engine").
//    They live in their own cache so an app update does not download them again.
//    Each file is stored under its content hash: a file that changed in a new
//    build is fetched again, the others are reused, and a response that is not
//    exactly the file of this build is never stored.
//  * A new build fetches the changed engine files this device already keeps
//    while it installs, so the first start after an update also works offline.
//
// The block below is rewritten by tools/build_web.py.
/* BUILD:START */
const BUILD = "eb7b84b444cb";
const SHELL = ["assets.json", "css/base.css", "css/components.css", "css/demo.svg", "css/screens.css", "css/tokens.css", "icons/apple-touch-icon.png", "icons/favicon-32.png", "icons/favicon.svg", "icons/icon-1024.png", "icons/icon-192.png", "icons/icon-512.png", "icons/icon-maskable-192.png", "icons/icon-maskable-512.png", "index.html", "js/app.js", "js/copy.js", "js/engine.js", "js/platform.js", "js/save.js", "js/screens.js", "js/ui-grid.js", "js/ui-sheets.js", "js/ui.js", "js/zip.js", "licenses.html", "manifest.webmanifest", "worker/engine.worker.mjs"];
const ENGINE = {"py/splitcore.zip": "3307536bac1942c2f77b3c32970ab409b9c6c774c0a12d4f17ab6440c3ae9eda", "vendor/pyodide/olefile-0.47-py2.py3-none-any.whl": "a7ae597c79e7d4e9bd9a8bf365e8356647b88d7916f76c2f6fcf0c735421a1f4", "vendor/pyodide/pillow-12.2.0-cp314-cp314-pyemscripten_2026_0_wasm32.whl": "e60a2a2322a058db0070720fa1da8e2e76efb86c150bcebb352a62fdc7a237be", "vendor/pyodide/pillow_heif-1.3.0-cp314-cp314-pyemscripten_2026_0_wasm32.whl": "8698acc6d9ee3a4cb487d4ad766db6c9e80c27192f7bc8eb565ce8a40a434090", "vendor/pyodide/pymupdf-1.27.2.2-cp314-abi3-pyemscripten_2026_0_wasm32.whl": "1d46849a43b48c59358c3f5cf2f237195c4b20e91228a4a4860746abbc7dff2c", "vendor/pyodide/pyodide-lock.json": "b50276737c6fd82c89d69ab8dc37496aa9d9071f1e098d81ba86b3af8356841f", "vendor/pyodide/pyodide.asm.mjs": "f7cdc8ece80678ceb712f8e65ebe6d3a83203a180c399865f49612a051693635", "vendor/pyodide/pyodide.asm.wasm": "cc36e3cab04fdfc9a63ff13eb52eae2b911bf46c025cc7b281f394bd3de1d5e6", "vendor/pyodide/pyodide.mjs": "6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109", "vendor/pyodide/python_stdlib.zip": "ea6bca51ea8b6690cff30ead78a6486096c8b769027b936d0e965a10360fadba"};
/* BUILD:END */

const SHELL_CACHE = `shell-${BUILD}`;
const ENGINE_CACHE = "engine-v1";
const SCOPE = new URL("./", self.location.href);
const absolute = (path) => new URL(path, SCOPE).href;
const ENGINE_PATHS = new Map(Object.keys(ENGINE).map((path) => [absolute(path), path]));  // url -> path
const engineKey = (path) => `${absolute(path)}?h=${ENGINE[path].slice(0, 16)}`;
const ENGINE_KEYS = new Set(Object.keys(ENGINE).map(engineKey));
const SHELL_URLS = new Set(SHELL.map(absolute));
const INDEX_URL = absolute("index.html");

/** ".../pillow-12.2.0-cp314-...whl" -> ".../pillow": the same package in another version. */
function family(url) {
  const clean = url.split("?")[0];
  const cut = clean.lastIndexOf("/") + 1;
  return clean.slice(0, cut) + clean.slice(cut).split("-")[0];
}

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    // "reload" bypasses the HTTP cache so a new build never mixes with stale files
    await cache.addAll(SHELL.map((path) => new Request(absolute(path), { cache: "reload" })));
    // Engine files this device already keeps and that changed in this build are fetched now, so
    // the first start under this build also works offline.  If one cannot be had the install
    // fails and the previous build stays in charge; the browser tries again at the next visit.
    const engine = await caches.open(ENGINE_CACHE);
    const kept = new Set((await engine.keys()).map((request) => family(request.url)));
    for (const path of Object.keys(ENGINE)) {
      if (!kept.has(family(absolute(path))) || await engine.match(engineKey(path))) continue;
      const fresh = await fetch(new Request(absolute(path), { cache: "no-cache" }));
      if (fresh.status !== 200 || !(await storeEngine(path, fresh))) throw new Error(`engine file not ready: ${path}`);
    }
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => (name.startsWith("shell-") && name !== SHELL_CACHE) || (name.startsWith("engine-") && name !== ENGINE_CACHE))
      .map((name) => caches.delete(name)));
    // drop engine files that changed or are no longer part of the build
    const engine = await caches.open(ENGINE_CACHE);
    for (const request of await engine.keys()) {
      if (!ENGINE_KEYS.has(request.url)) await engine.delete(request);
    }
    await self.clients.claim();
  })());
});

async function fromShell(request) {
  const cache = await caches.open(SHELL_CACHE);
  const hit = await cache.match(request, { ignoreSearch: true });
  if (hit) return hit;
  const response = await fetch(request);
  if (response.ok) cache.put(request, response.clone()).catch(() => {});
  return response;
}

const hex = (buffer) => Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, "0")).join("");

/** Keep one engine file, but only if it is exactly the file this build was made with. */
async function storeEngine(path, response) {
  try {
    const body = await response.arrayBuffer();
    if (hex(await crypto.subtle.digest("SHA-256", body)) !== ENGINE[path]) {
      // The site has moved on to another build: look for the new worker now instead of at the next visit.
      self.registration.update().catch(() => {});
      return false;
    }
    const headers = {
      "Content-Type": response.headers.get("Content-Type") || "application/octet-stream",
      "Content-Length": String(body.byteLength),
    };
    const cache = await caches.open(ENGINE_CACHE);
    await cache.put(engineKey(path), new Response(body, { status: 200, headers }));
    return true;
  } catch (error) {
    return false;  // out of space or interrupted: fetched again next time
  }
}

// Engine files on their way right now (path -> promise of "stored"): the precache and the page
// share one download instead of fetching the same 18 MB twice.
const loading = new Map();

/** Register a download.  Call with no await between the loading.has() check and this call. */
function announce(path) {
  let done;
  const job = new Promise((resolve) => { done = resolve; });
  loading.set(path, job);
  job.then(() => { if (loading.get(path) === job) loading.delete(path); });
  return done;
}

async function fromEngine(event, path) {
  const cache = await caches.open(ENGINE_CACHE);
  let hit = await cache.match(engineKey(path));
  if (hit) return hit;
  if (loading.has(path)) {  // already being downloaded: wait for that one
    await loading.get(path);
    hit = await cache.match(engineKey(path));
    if (hit) return hit;
  }
  const done = announce(path);
  let response;
  try {
    // "no-cache" = ask the server whether the browser's own copy is still current (a short
    // exchange when it is).  Without it a copy from the previous build could be used for a while.
    response = await fetch(new Request(event.request, { cache: "no-cache" }));
  } catch (error) {
    done(false);
    throw error;
  }
  if (response.status === 200) {
    event.waitUntil(storeEngine(path, response.clone()).then(done));
  } else {
    done(false);
    // a file this build expects is gone: the site has moved on to another build
    if (response.status === 404) self.registration.update().catch(() => {});
  }
  return response;
}

/**
 * Fill the engine cache.
 *   files    only these paths (what the page has just downloaded itself); default: all of them
 *   network  false = take them from the browser's HTTP cache only, never spend data
 */
async function precacheEngine({ files, network = true }, client) {
  const cache = await caches.open(ENGINE_CACHE);
  const known = (path) => Object.prototype.hasOwnProperty.call(ENGINE, path);
  const wanted = (Array.isArray(files) ? files : Object.keys(ENGINE)).filter(known);
  let done = 0;
  for (const path of wanted) {
    if (loading.has(path)) await loading.get(path);
    if (await cache.match(engineKey(path))) {
      done += 1;
      continue;
    }
    const url = absolute(path);
    let kept = false;
    try {
      const local = await fetch(new Request(url, { cache: "only-if-cached", mode: "same-origin" }));
      if (local.status === 200) kept = await storeEngine(path, local);
    } catch (error) { /* not in the HTTP cache */ }
    if (!kept && network) {
      if (loading.has(path)) {
        kept = await loading.get(path);  // the page started it meanwhile
      } else {
        const stored = announce(path);
        try {
          const fresh = await fetch(new Request(url, { cache: "no-cache" }));
          if (fresh.status === 200) kept = await storeEngine(path, fresh);
        } catch (error) {
          stored(false);
          break;  // offline: try again next time
        }
        stored(kept);
      }
    }
    if (kept) done += 1;
  }
  if (client) client.postMessage({ type: "engine-cached", done, total: wanted.length });
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.href.startsWith(SCOPE.href)) return;
  const clean = url.origin + url.pathname;

  if (request.mode === "navigate") {
    const startPage = clean === SCOPE.href || clean === INDEX_URL;
    if (!startPage && !SHELL_URLS.has(clean)) return;  // not ours: let the network answer
    event.respondWith((async () => {
      const cache = await caches.open(SHELL_CACHE);
      const hit = await cache.match(startPage ? INDEX_URL : clean);
      if (hit) return hit;
      try {
        return await fetch(request);
      } catch (error) {
        return new Response("오프라인이에요. 인터넷에 연결한 뒤 다시 열어 주세요.", {
          status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      }
    })());
    return;
  }
  const enginePath = ENGINE_PATHS.get(clean);
  if (enginePath) {
    event.respondWith(fromEngine(event, enginePath));
    return;
  }
  if (SHELL_URLS.has(clean)) {
    event.respondWith(fromShell(request));
  }
});

self.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "skip-waiting") {
    self.skipWaiting();
    return;
  }
  if (data.type === "precache-engine") {
    event.waitUntil(precacheEngine(data, event.source));
  }
});
