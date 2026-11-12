// Platform facts and browser plumbing: which device this is, in-app browsers,
// installing, the service worker and the screen wake lock.  No UI in here.

const ua = navigator.userAgent || "";

/** iPadOS reports itself as a Mac; touch points tell them apart. */
export const isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
export const isAndroid = /Android/i.test(ua);
export const isDesktop = !isIOS && !isAndroid;

function detectStandalone() {
  let twa = false;
  try {
    // A Trusted Web Activity only shows its referrer on the first navigation, so remember it.
    twa = sessionStorage.getItem("jj.twa") === "1";
    if (!twa && document.referrer.startsWith("android-app://")) {
      twa = true;
      sessionStorage.setItem("jj.twa", "1");
    }
  } catch (error) { /* storage blocked: the other two signals still work */ }
  return twa || window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
}

/** Running as an installed app (home screen icon, WebAPK, TWA, desktop install). */
export const isStandalone = detectStandalone();

export const isIOSSafari = isIOS && /Safari\//.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|DuckDuckGo|GSA\//.test(ua);

/** Safari major version (0 when unknown).  Safari 26+ freezes the OS version in the UA, so only Version/ is reliable. */
export const safariVersion = (() => {
  const match = ua.match(/Version\/(\d+)/);
  return match ? Number(match[1]) : 0;
})();

export const saveData = Boolean(navigator.connection && navigator.connection.saveData === true);

// ---------------------------------------------------------------------------
// In-app browsers (KakaoTalk, Naver, ...): saving files and installing are blocked there
// ---------------------------------------------------------------------------
const IN_APP = [
  ["kakaotalk", /KAKAOTALK/i, "카카오톡"],
  ["naver", /NAVER\(inapp/i, "네이버 앱"],
  ["line", /\bLine\//i, "라인"],
  ["instagram", /\bInstagram/i, "인스타그램"],
  ["facebook", /\bFB[\w_]+\/|FBAN|FBAV|FB_IAB/i, "페이스북"],
  ["daum", /DaumApps/i, "다음 앱"],
  ["band", /\bBAND\//i, "밴드"],
];

/** @returns {{key: string, name: string} | null} */
export function inAppBrowser() {
  if (isStandalone) return null;  // an iOS home-screen app also has no "Safari/" in its user agent
  for (const [key, pattern, name] of IN_APP) {
    if (pattern.test(ua)) return { key, name };
  }
  if (/Android.*; wv\)/i.test(ua)) return { key: "webview", name: "다른 앱" };
  if (/(iPhone|iPod|iPad)(?!.*Safari\/)/i.test(ua)) return { key: "webview", name: "다른 앱" };
  return null;
}

/** True when this page was already opened through our own hand-off link (avoid a loop). */
export const cameFromHandOff = /[?&]ext=1\b/.test(location.search);

/**
 * A link that asks the host app to open this page in the real browser.
 * Only LINE documents its parameter; the others are conventions that work in practice,
 * which is why the notice always shows the manual steps as well.
 */
export function externalUrl(inApp) {
  const url = new URL(location.href);
  url.searchParams.set("ext", "1");
  const key = inApp ? inApp.key : "";
  if (key === "kakaotalk") return "kakaotalk://web/openExternal?url=" + encodeURIComponent(url.href);
  if (key === "line") {
    url.searchParams.set("openExternalBrowser", "1");
    return url.href;
  }
  if (isAndroid) {
    const scheme = url.protocol.replace(":", "");
    return `intent://${url.host}${url.pathname}${url.search}#Intent;scheme=${scheme};package=com.android.chrome;`
      + `S.browser_fallback_url=${encodeURIComponent(url.href)};end`;
  }
  if (isIOS) return "x-safari-" + url.href;
  return url.href;
}

/** The address to hand to someone (or another browser): no query noise. */
export function shareableUrl() {
  return location.origin + location.pathname;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    // Older in-app WebViews have no async clipboard: fall back to a selection copy.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "sr";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (inner) { ok = false; }
    area.remove();
    return ok;
  }
}

// ---------------------------------------------------------------------------
// Installing
// ---------------------------------------------------------------------------
export const APK_URL = "https://github.com/geunminmaker/geunminmaker.github.io/releases/latest/download/jogakjogak.apk";

let deferredPrompt = null;
const installListeners = new Set();
const notifyInstall = () => { for (const listener of installListeners) listener(); };

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();  // keep the browser's own mini-bar away; our button shows the prompt
  deferredPrompt = event;
  notifyInstall();
});
window.addEventListener("appinstalled", () => {
  deferredPrompt = null;
  notifyInstall();
});

export function onInstallChange(listener) {
  installListeners.add(listener);
  return () => installListeners.delete(listener);
}

/**
 * How installing works here:
 *   "none"    - already installed, or this browser cannot install
 *   "prompt"  - the browser gave us an install prompt (desktop Chromium)
 *   "android" - show the Android sheet (prompt when available, menu steps, APK link)
 *   "ios"     - show the Add to Home Screen steps
 */
export function installMode() {
  if (isStandalone || inAppBrowser()) return "none";
  if (isIOS) return "ios";
  if (isAndroid) return "android";
  return deferredPrompt ? "prompt" : "none";
}

export const canPromptInstall = () => Boolean(deferredPrompt);

/** Shows the browser's install prompt.  Resolves to "accepted", "dismissed" or "unavailable". */
export async function promptInstall() {
  const event = deferredPrompt;
  if (!event) return "unavailable";
  deferredPrompt = null;  // a prompt event can be used once
  try {
    event.prompt();
    const choice = await event.userChoice;
    return choice && choice.outcome === "accepted" ? "accepted" : "dismissed";
  } catch (error) {
    return "unavailable";
  } finally {
    notifyInstall();
  }
}

// ---------------------------------------------------------------------------
// Service worker: offline shell, quiet updates, engine files for offline use
// ---------------------------------------------------------------------------
let registration = null;
let reloadOnTakeover = false;

// The service worker keeps what the engine downloads only while it controls the page.  On a
// first visit it has to install first, so the engine start waits for it (see whenControlled).
let noWorker = false;           // there will be no service worker for this page
const controlWaiters = new Set();
function giveUpOnWorker() {
  noWorker = true;
  for (const waiter of Array.from(controlWaiters)) waiter(false);
}

/** Resolves true once the service worker controls this page; false when there is none or after `limit` ms. */
export function whenControlled(limit = 4000) {
  if (!("serviceWorker" in navigator) || noWorker) return Promise.resolve(false);
  if (navigator.serviceWorker.controller) return Promise.resolve(true);
  return new Promise((resolve) => {
    let timer = 0;
    const done = (value) => {
      clearTimeout(timer);
      controlWaiters.delete(done);
      navigator.serviceWorker.removeEventListener("controllerchange", taken);
      resolve(value);
    };
    const taken = () => done(true);
    timer = setTimeout(() => done(false), limit);
    controlWaiters.add(done);
    navigator.serviceWorker.addEventListener("controllerchange", taken);
  });
}

/**
 * @param {{onUpdate: () => void}} hooks  onUpdate fires when a new version is waiting
 */
export function registerServiceWorker({ onUpdate }) {
  if (!("serviceWorker" in navigator)) return;
  const local = location.hostname === "localhost" || location.hostname === "127.0.0.1";
  if (local && /[?&]nosw\b/.test(location.search)) {
    giveUpOnWorker();
    return;
  }

  let reloaded = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    // The first install also changes the controller (clients.claim); only reload for an update the user asked for.
    if (!reloadOnTakeover || reloaded) return;
    reloaded = true;
    location.reload();
  });

  const watch = (worker) => {
    if (!worker) return;
    worker.addEventListener("statechange", () => {
      if (worker.state === "installed" && navigator.serviceWorker.controller) onUpdate();
    });
  };

  const register = async () => {
    try {
      registration = await navigator.serviceWorker.register("./sw.js", { scope: "./", updateViaCache: "none" });
      // an active worker that does not control the page: this was a forced reload, it never will
      if (registration.active && !navigator.serviceWorker.controller) giveUpOnWorker();
      if (registration.waiting && navigator.serviceWorker.controller) onUpdate();
      watch(registration.installing);
      registration.addEventListener("updatefound", () => watch(registration.installing));
    } catch (error) {
      /* private browsing or a blocked worker: the app still works online */
      giveUpOnWorker();
    }
  };
  if (document.readyState === "complete") register();
  else window.addEventListener("load", register, { once: true });
}

export const updateWaiting = () => Boolean(registration && registration.waiting && navigator.serviceWorker.controller);

/** Switch to the waiting version; the page reloads once it has taken over. */
export function applyUpdate() {
  if (!registration || !registration.waiting) return false;
  reloadOnTakeover = true;
  registration.waiting.postMessage({ type: "skip-waiting" });
  return true;
}

/**
 * Keep the engine for offline use.
 *  - installed app or desktop: all of it (PDF tools included), downloading what is missing
 *  - phone browser tab: only the files this page has just downloaded (`used`), taken from the
 *    browser's own cache, so no mobile data is spent unasked
 */
export function precacheEngine(used = []) {
  if (!("serviceWorker" in navigator)) return;
  const everything = (isStandalone || isDesktop) && !saveData;
  if (!everything && !used.length) return;
  const message = everything
    ? { type: "precache-engine" }
    : { type: "precache-engine", files: used, network: false };
  navigator.serviceWorker.ready.then((ready) => {
    const worker = navigator.serviceWorker.controller || ready.active;
    if (worker) worker.postMessage(message);
  }).catch(() => {});
}

/** Ask the browser not to evict our caches.  Asked once, the answer does not matter. */
export function persistStorage() {
  try {
    if (localStorage.getItem("jj.persist") === "1") return;
    localStorage.setItem("jj.persist", "1");
  } catch (error) { /* ask anyway */ }
  try {
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
  } catch (error) { /* not supported */ }
}

// ---------------------------------------------------------------------------
// Screen wake lock (the phone must not sleep in the middle of an export)
// ---------------------------------------------------------------------------
let wakeLock = null;
let wakeWanted = false;

export async function keepAwake(on) {
  wakeWanted = on;
  if (!("wakeLock" in navigator)) return;
  try {
    if (on && !wakeLock && document.visibilityState === "visible") {
      const lock = await navigator.wakeLock.request("screen");
      lock.addEventListener("release", () => { if (wakeLock === lock) wakeLock = null; });
      wakeLock = lock;
      if (!wakeWanted) keepAwake(false);  // the job ended while the request was pending
    } else if (!on && wakeLock) {
      const lock = wakeLock;
      wakeLock = null;
      await lock.release();
    }
  } catch (error) {
    /* refused (battery saver, not allowed in this context): nothing to fall back to */
  }
}

document.addEventListener("visibilitychange", () => {
  // The lock is dropped whenever the page is hidden; take it again when the user comes back mid-job.
  if (wakeWanted && document.visibilityState === "visible") keepAwake(true);
});
