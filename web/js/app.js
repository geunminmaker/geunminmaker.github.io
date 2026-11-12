// 조각조각 - the app itself: one state object, eight screens and the actions that move between them.
//
//   home -> (prep) -> pick -> confirm -> split -> result -> (work) -> result
//                  \-> error
//
// document.body.dataset.screen always names the visible screen; the engine (Python in a worker)
// is only ever talked to through engine.js.
//
// Related modules: screens.js (markup of each screen), ui.js (sheet, toast, small DOM helpers),
// ui-sheets.js (sheet contents), ui-grid.js (grid drawings), copy.js (words), platform.js
// (device facts, install, service worker), save.js (handing files over).

import { Engine } from "./engine.js";
import * as platform from "./platform.js";
import * as save from "./save.js";
import {
  $, $$, announce, closeSheet, esc, focusHeadline, hideToast, ic, openSheet, setBusy, setHTML, sheetName, syncRadios, toast,
} from "./ui.js";
import { gridCells, gridLines } from "./ui-grid.js";
import * as screens from "./screens.js";
import { cancelSheet, deliverSheet, inAppSheet, installSheetAndroid, installSheetIOS, pickerSheet } from "./ui-sheets.js";
import { errorView, exportProblem, FALLBACK_COUNTS, FALLBACK_FORMATS, formatCopy } from "./copy.js";

const engine = new Engine();

const app = $("#app");
const view = $("#view");
const dock = $("#dock");
const inputPhotos = $("#input-photos");
const inputFiles = $("#input-files");
const backButton = $('[data-action="back"]');
const installButton = $('[data-action="install"]');
const restartButton = $('[data-action="restart"]');
const wideLayout = window.matchMedia("(min-width:900px)");

// The home screen is static markup in index.html (it paints before any script runs); keep it for later visits.
const HOME_HTML = view.innerHTML.replace(" screen--still", "");
const HOME_DOCK = dock.innerHTML;

/**
 * Android's document picker.  Chrome adds a camera entry (and asks for the camera permission) whenever
 * image types are acceptable, and an empty accept counts as acceptable - so the types are spelled out.
 */
const ANDROID_FILE_TYPES = ".pdf,.docx,.pptx,.hwp,.hwpx,.doc,.ppt,.zip,.jpg,.jpeg,.png,.webp,.gif,.bmp,.tif,.tiff,.heic";

/** A screen that appears for less than this is only a flicker, so it is not shown at all. */
const PREP_DELAY_MS = 300;
const MIN_VISIBLE_MS = 450;
/** Checking a password usually takes a blink; past this it gets the waiting screen instead of a button spinner. */
const UNLOCK_INLINE_MS = 150;

const S = {
  screen: "home",
  session: 0,          // changes whenever the picked files are dropped; late engine answers compare against it
  loadToken: 0,        // changes whenever a load starts or is cancelled
  inflight: null,      // the engine.openFile() that is running right now
  engineDirty: false,  // the engine holds files of this session (they are released when the session is dropped)
  stalled: false,      // the engine start was declared stuck by the watchdog; the next start replaces it
  files: [],           // what the user picked (kept for "retry" and the password screen)
  opened: [],          // [{ file, password }] the engine accepted, in order
  sources: [],         // every picture found in those files
  failures: [],        // [{ name, code }] files that could not be opened
  truncated: false,
  pickId: null,        // highlighted thumbnail on the pick screen
  source: null,        // the picture being split
  preview: null,       // { url, width, height }
  layouts: null,       // { "2": [layout...], ... } for the source
  layoutsJob: null,
  probeStem: "",       // the engine's name for the source, used to re-check ids after a restart
  count: null,
  layout: null,
  plan: null,          // engine plan for the chosen layout (out_w/out_h after any capping)
  stem: "",            // base name of the output files
  format: "pdf",
  paper: "original",
  zip: false,          // iOS: one ZIP instead of sharing every picture
  built: null,         // { key, files, zip, kind } - the files of the last export
  job: null,           // the export that is running
  problem: null,       // { text, retry } shown on the result screen after a failed export
  prep: { index: 0, total: 0, pkg: null, files: [], shownAt: 0, firstRun: false },
  error: null,         // { code, file }
  inApp: null,
  inAppAsked: false,   // the in-app notice was answered ("continue anyway")
  updateReady: false,
  thumbs: null,        // IntersectionObserver of the pick screen
};

/** Engine work that must finish before the next engine call (a restart after cancel, a reset). */
let gate = Promise.resolve();
const afterGate = (task) => {
  gate = gate.catch(() => {}).then(task).catch(() => {});
  return gate;
};

// ---------------------------------------------------------------------------
// Engine watchdog.  A start that loses a download half-way (a tunnel, an incomplete offline
// cache) can stay "loading" for ever without failing.  When neither progress nor a state change
// has been seen for a while, whoever is waiting is told, so the user gets a retry button
// instead of an endless bar.
// ---------------------------------------------------------------------------
const ENGINE_STALL_MS = 45_000;
let engineBeat = performance.now();
let stallTimer = 0;
const stallWaiters = new Set();

/** The promise, or a rejection with code OFFLINE as soon as the engine start is declared stuck. */
function untilStall(promise) {
  return new Promise((resolve, reject) => {
    const waiter = () => reject({ code: "OFFLINE", message: "" });
    if (S.stalled) {  // already stuck: whatever this waits for will not come
      waiter();
      return;
    }
    stallWaiters.add(waiter);
    Promise.resolve(promise).then(resolve, reject).finally(() => stallWaiters.delete(waiter));
  });
}

function watchEngine() {
  engineBeat = performance.now();
  if (engine.state !== "loading") {
    clearInterval(stallTimer);
    stallTimer = 0;
    return;
  }
  if (stallTimer) return;
  stallTimer = setInterval(() => {
    if (document.visibilityState !== "visible") {
      engineBeat = performance.now();  // a hidden page is throttled by the browser: that is not a stall
      return;
    }
    if (engine.state !== "loading" || performance.now() - engineBeat < ENGINE_STALL_MS) return;
    clearInterval(stallTimer);
    stallTimer = 0;
    S.stalled = true;
    for (const waiter of Array.from(stallWaiters)) waiter();
  }, 2000);
}

/** Start the engine, replacing a start that got stuck earlier. */
function startEngine() {
  if (S.stalled) {
    S.stalled = false;
    engine.restart().catch(() => {});
  } else {
    engine.start().catch(() => {});  // a failure is reported when a file is picked
  }
}

// ===========================================================================
// Shell: screens, the action area, the top bar
// ===========================================================================
function setExport(state) {
  document.body.dataset.export = state;
}

function placeDock() {
  // Wide screens show the actions inside the screen; phones keep them pinned to the bottom edge.
  const slot = wideLayout.matches ? $("[data-dock-slot]", view) : null;
  const parent = slot || app;
  if (dock.parentElement !== parent) parent.appendChild(dock);
}
wideLayout.addEventListener("change", () => {
  placeDock();
  fitStages();
});

function paintTopBar() {
  installButton.hidden = !(S.screen === "home" && platform.installMode() !== "none");
  restartButton.hidden = S.screen !== "result";
}

function setScreen(name, html, { step = 0, dockHTML = "", back = true, focus = true } = {}) {
  if (S.thumbs) {
    S.thumbs.disconnect();
    S.thumbs = null;
  }
  hideToast();  // a toast belongs to the screen it appeared on
  app.appendChild(dock);  // out of the markup that is about to be replaced
  S.screen = name;
  document.body.dataset.screen = name;
  app.dataset.step = String(step);
  setHTML(view, html);
  setHTML(dock, dockHTML);
  delete dock.dataset.html;
  placeDock();
  backButton.hidden = !back;
  paintTopBar();
  window.scrollTo(0, 0);
  fitStages();
  if (focus) focusHeadline();
}

/**
 * The preview sits letterboxed in a neutral stage.  When the bars at the sides would only be
 * slivers (under 24px each) the stage grows a little instead, so the picture meets both edges.
 */
function fitStages() {
  for (const stage of $$(".stage", view)) {
    stage.style.removeProperty("--maxh");
    const fit = $(".stage__fit", stage);
    const aspect = parseFloat(stage.style.getPropertyValue("--ar"));
    if (!fit || !(aspect > 0)) continue;
    const gap = stage.clientWidth - fit.offsetWidth;
    if (gap > 0 && gap < 48) stage.style.setProperty("--maxh", `${Math.ceil(stage.clientWidth / aspect + 1)}px`);
  }
}
window.addEventListener("resize", fitStages);

/** <img> for the chosen picture; shows a spinner until the engine has made the preview. */
function previewImage(alt) {
  if (S.preview) return `<img class="is-in" src="${S.preview.url}" alt="${alt}" data-preview>`;
  return `<span class="spin stage__spin"></span><img alt="${alt}" data-preview>`;
}

function paintPreview() {
  if (!S.preview) return;
  for (const img of $$("img[data-preview]", view)) {
    if (img.getAttribute("src") === S.preview.url) continue;
    img.onload = () => {
      img.classList.add("is-in");
      const spinner = $(".stage__spin", img.parentElement);
      if (spinner) spinner.remove();
    };
    img.src = S.preview.url;
  }
  const result = $(".result", view);
  if (result) result.style.setProperty("--img", `url("${S.preview.url}")`);
}

// ===========================================================================
// History: every step after home is one history entry, so the Android back
// button and the iOS edge swipe walk the flow backwards instead of leaving.
// ===========================================================================
const nav = { depth: 0, skip: 0 };

function depthOf(screen) {
  const several = S.sources.length > 1;
  if (screen === "home") return 0;
  if (screen === "prep" || screen === "error" || screen === "pick") return 1;
  if (screen === "confirm") return several ? 2 : 1;
  if (screen === "split") return several ? 3 : 2;
  return several ? 4 : 3;  // result, work
}

/** Bring the number of history entries above the first one in line with the visible screen. */
function syncHistory() {
  const target = depthOf(S.screen);
  while (nav.depth < target) {
    nav.depth += 1;
    history.pushState({ jj: 1, d: nav.depth }, "");
  }
  if (nav.depth > target) {
    nav.skip += 1;  // the popstate this causes is ours
    history.go(target - nav.depth);
    nav.depth = target;
  }
}

window.addEventListener("popstate", (event) => {
  if (nav.skip > 0) {
    nav.skip -= 1;
    return;
  }
  const depth = event.state && event.state.jj ? event.state.d : 0;
  if (depth > nav.depth) {  // "forward": screens cannot be rebuilt from history, so step back again
    nav.skip += 1;
    history.go(nav.depth - depth);
    return;
  }
  nav.depth = depth;
  goBack();
});

/** One step back in the flow (browser back, the top-bar arrow, or a cancel button). */
function goBack() {
  if (sheetName()) {
    closeSheet();
  } else if (S.screen === "work") {
    openSheet(cancelSheet());
  } else if (S.screen === "prep") {
    showHome();
  } else if (S.screen === "pick" || S.screen === "error") {
    showHome();
  } else if (S.screen === "confirm") {
    if (S.sources.length > 1) showPick();
    else showHome();
  } else if (S.screen === "split") {
    showConfirm();
  } else if (S.screen === "result") {
    showSplit();
  }
  syncHistory();
}

function requestBack() {
  if (nav.depth > 0) history.back();
  else goBack();
}

// ===========================================================================
// 1 · Home
// ===========================================================================
function dropSession() {
  S.loadToken += 1;
  S.session += 1;
  const hadFiles = S.engineDirty;
  S.engineDirty = false;
  S.files = [];
  S.opened = [];
  S.sources = [];
  S.failures = [];
  S.truncated = false;
  S.pickId = null;
  S.error = null;
  S.job = null;
  setSource(null);
  setExport("idle");
  if (hadFiles) {
    const inflight = S.inflight;
    // free the pictures in the engine, but only after the load in flight has settled
    // (or has been given up on: the gate must never wait for an engine start that is stuck)
    afterGate(() => untilStall(inflight).catch(() => {}).then(() => engine.reset()));
  }
}

function setSource(source) {
  S.source = source;
  S.preview = null;
  S.layouts = null;
  S.layoutsJob = null;
  S.probeStem = "";
  S.count = null;
  S.layout = null;
  S.plan = null;
  S.stem = "";
  S.built = null;
  S.problem = null;
}

function showHome({ focus = true } = {}) {
  dropSession();
  setScreen("home", HOME_HTML, { dockHTML: HOME_DOCK, back: false, focus });
  paintHome();
}

function paintHome() {
  const box = $("#home-notices");
  if (!box) return;
  setHTML(box, screens.homeNotices({ inApp: S.inApp, updateReady: S.updateReady }));
  const title = $("[data-drop-title]");
  if (title && !platform.isDesktop) title.textContent = "나눌 사진이나 문서를 골라 주세요";
}

function openPicker() {
  // Android has two system pickers (photos / documents), so it asks first; elsewhere one picker covers both.
  if (platform.isAndroid) openSheet(pickerSheet());
  else inputFiles.click();
}

function maybeStartEngine() {
  if (S.inApp && !S.inAppAsked) return;                  // nothing heavy before the in-app notice is answered
  if (platform.isIOS && !platform.isStandalone) return;  // a Safari tab does not share storage with the installed app
  startEngine();
}

// ===========================================================================
// 2 · Preparing: engine download, looking through the files, PDF tools
// ===========================================================================
const engineSeen = () => {
  try { return localStorage.getItem("jj.engine") === "1"; } catch (error) { return false; }
};

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Once the waiting screen is visible it stays long enough to be read. */
async function holdPrep() {
  const left = MIN_VISIBLE_MS - (performance.now() - S.prep.shownAt);
  if (left > 0) await pause(left);
}

function showPrep(files) {
  S.prep = { index: 0, total: files.length, pkg: null, files, shownAt: performance.now(), firstRun: !engineSeen() };
  setScreen("prep", screens.prepScreen(files), { step: 1, dockHTML: screens.cancelDock });
  paintPrep();
}

const megabytes = (bytes) => (bytes / (1024 * 1024)).toFixed(1);

function paintPrep() {
  if (S.screen !== "prep") return;
  const prep = S.prep;
  const bar = $("#prep-bar");
  const set = (id, text, html = false) => {
    const node = $(id);
    if (html) { if (node.innerHTML !== text) node.innerHTML = text; } else if (node.textContent !== text) node.textContent = text;
  };
  let fraction = null;  // null = no figure to show, the bar just moves
  if (engine.state !== "ready") {
    const { loaded, total } = engine.progress;
    const downloading = total > 0 && loaded < total;
    fraction = total > 0 ? Math.min(1, loaded / total) : 0;
    if (prep.firstRun) {
      set("#prep-title", "처음 한 번만\n준비할게요");
      set("#prep-lead", "이미지를 나누는 도구를 내려받고 있어요. 다음부터는 기다리지 않아도 돼요.");
      set("#prep-status", downloading || !total ? "분할 도구 내려받는 중" : "거의 다 됐어요");
      set("#prep-figure", total > 0 ? `${megabytes(loaded)} / ${megabytes(total)}MB` : "");
      set("#prep-note", "처음 한 번만 받아요. Wi-Fi가 아니면 데이터가 쓰일 수 있어요.");
    } else {
      set("#prep-title", "잠시만\n기다려 주세요");
      set("#prep-lead", "이미지를 나누는 도구를 준비하고 있어요.");
      set("#prep-status", "준비하는 중");
      set("#prep-figure", "");
      set("#prep-note", "");
    }
  } else {
    const file = prep.files[prep.index] || prep.files[0];
    const several = prep.total > 1;
    set("#prep-title", "파일을 살펴보고\n있어요");
    set("#prep-lead", several
      ? `파일 ${prep.total}개에서 이미지를 찾는 중이에요.`
      : `<b>${esc(file ? file.name : "")}</b>에서 이미지를 찾는 중이에요.`, true);
    if (prep.pkg && prep.pkg.total > 0 && prep.pkg.loaded < prep.pkg.total) {
      const pdf = (prep.pkg.packages || []).includes("pymupdf");
      fraction = Math.min(1, prep.pkg.loaded / prep.pkg.total);
      set("#prep-status", pdf ? "PDF 도구 내려받는 중" : "필요한 도구 내려받는 중");
      set("#prep-figure", `${megabytes(prep.pkg.loaded)} / ${megabytes(prep.pkg.total)}MB`);
      set("#prep-note", pdf ? "PDF를 처음 열 때 한 번만 받아요." : "처음 한 번만 받아요.");
    } else {
      set("#prep-status", "이미지 찾는 중");
      set("#prep-figure", several ? `${Math.min(prep.index + 1, prep.total)} / ${prep.total}` : "");
      set("#prep-note", "");
    }
  }
  bar.classList.toggle("is-indet", fraction === null);
  bar.classList.toggle("is-busy", fraction !== null);
  if (fraction === null) {
    bar.removeAttribute("aria-valuenow");
    bar.firstElementChild.style.width = "";
  } else {
    bar.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
    bar.firstElementChild.style.width = `${(fraction * 100).toFixed(1)}%`;
  }
}

/** Errors that are about the engine itself, not about one file. */
const ENGINE_TROUBLE = new Set(["NO_WASM", "ENGINE_CRASHED"]);

function normalizeError(error) {
  if (error && error.code) return error;
  const name = error && error.name;
  // File.arrayBuffer() fails like this for folders, cloud files that are gone, revoked permissions
  const code = name === "NotFoundError" || name === "NotReadableError" || name === "SecurityError" ? "UNREADABLE" : "INTERNAL";
  return { code, message: "", detail: String(error) };
}

/** Open what the user picked, in order, and move on to the right screen. */
async function openFiles(list) {
  const files = Array.from(list || []).filter((file) => file && typeof file.size === "number");
  if (!files.length || S.job) return;
  closeSheet();
  hideToast();
  dropSession();
  const token = S.loadToken;
  const session = S.session;
  S.files = files;
  S.inAppAsked = true;  // picking a file is the clearest "continue anyway"
  const progress = { index: 0, pkg: null };

  let prepShown = false;
  const show = () => {
    if (token !== S.loadToken || prepShown) return;
    prepShown = true;
    showPrep(files);
    Object.assign(S.prep, progress);
    paintPrep();
    syncHistory();
  };
  // From the home screen there is nothing to show when the engine is up and the file opens at once.
  // From any other screen (an error, an earlier picture) the old content must go right away.
  const quiet = engine.state === "ready" && S.screen === "home";
  const timer = quiet ? setTimeout(show, PREP_DELAY_MS) : (show(), 0);
  const report = (patch) => {
    Object.assign(progress, patch);
    if (!prepShown || token !== S.loadToken) return;
    Object.assign(S.prep, patch);
    paintPrep();
  };
  startEngine();

  const sources = [];
  const opened = [];
  const failures = [];
  let truncated = false;
  let trouble = null;
  await gate.catch(() => {});
  for (let index = 0; index < files.length; index += 1) {
    if (token !== S.loadToken) return;
    const file = files[index];
    report({ index, pkg: null });
    try {
      S.engineDirty = true;
      S.inflight = engine.openFile(file, { onProgress: (download) => report({ pkg: download }) });
      const result = await untilStall(S.inflight);
      sources.push(...result.sources);
      opened.push({ file, password: null });
      truncated = truncated || Boolean(result.truncated);
    } catch (raw) {
      const error = normalizeError(raw);
      if (error.code === "CANCELLED") return;
      failures.push({ file, name: file.name, code: error.code, error });
      if (ENGINE_TROUBLE.has(error.code) || error.code === "OFFLINE" || engine.state === "failed") {
        trouble = error;
        break;
      }
    } finally {
      S.inflight = null;
    }
  }
  clearTimeout(timer);
  if (token !== S.loadToken) return;
  if (prepShown) {
    await holdPrep();
    if (token !== S.loadToken) return;
  }

  if (trouble) {
    const booting = engine.state === "failed" && !ENGINE_TROUBLE.has(trouble.code);
    showError(booting ? { code: "OFFLINE", message: "" } : trouble, { file: files[0] });
  } else if (!sources.length) {
    const first = failures[0];
    S.failures = failures.slice(1).map(({ name, code }) => ({ name, code }));
    showError(first.error, { file: first.file });
  } else {
    acceptSources(session, sources, opened, failures.map(({ name, code }) => ({ name, code })), truncated);
  }
  syncHistory();
}

/** The engine found pictures: go to the thumbnails, or straight to the confirmation when there is one. */
function acceptSources(session, sources, opened, failures, truncated) {
  if (session !== S.session) return;
  S.sources = sources;
  S.opened = opened;
  S.failures = failures;
  S.truncated = truncated;
  S.error = null;
  if (sources.length === 1) {
    setSource(sources[0]);
    showConfirm();
  } else {
    showPick();
  }
  announceNotices();
}

const noticesNow = () => screens.notices(S.failures, S.truncated);

function announceNotices() {
  const spoken = screens.noticesSpoken(S.failures, S.truncated);
  if (spoken) announce(spoken);
}

// ===========================================================================
// 3 · Pick one picture
// ===========================================================================
function showPick() {
  if (S.source) S.pickId = S.source.id;
  setScreen("pick",
    screens.pickScreen({ sources: S.sources, pickId: S.pickId, fileCount: S.opened.length, notices: noticesNow() }),
    { step: 1, dockHTML: screens.pickDock(S.pickId) });
  watchThumbs();
  const chosen = S.pickId && $(`.thumb[data-source="${S.pickId}"]`, view);
  if (chosen) chosen.scrollIntoView({ block: "center" });
}

/**
 * Thumbnails are made by the engine one at a time, so only those near the viewport are asked for
 * (a 300-page PDF must not render 300 pages up front), newest request first.
 */
function watchThumbs() {
  const wanted = new Map();  // source id -> thumbnail button, in the order they came into view
  let active = 0;
  const session = S.session;
  let observer = null;
  const pump = () => {
    while (active < 2 && wanted.size) {
      const [id, thumb] = Array.from(wanted).pop();
      wanted.delete(id);
      observer.unobserve(thumb);
      const img = $("img", thumb);
      active += 1;
      engine.thumbnail(id, Number(thumb.dataset.px) || 360).then(({ url }) => {
        if (session !== S.session || !img.isConnected) return;
        img.onload = () => img.classList.add("is-in");
        img.src = url;
      }, () => { /* the frame stays neutral; the label still identifies the picture */ }).finally(() => {
        active -= 1;
        pump();
      });
    }
  };
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const thumb = entry.target;
      if (entry.isIntersecting) wanted.set(thumb.dataset.source, thumb);
      else wanted.delete(thumb.dataset.source);  // scrolled past before its turn came
    }
    pump();
  }, { rootMargin: "240px 0px" });
  for (const thumb of $$(".thumb", view)) observer.observe(thumb);
  S.thumbs = observer;
}

// ===========================================================================
// 4 · Is this the picture?
// ===========================================================================
function showConfirm() {
  const alone = S.sources.length === 1;
  setScreen("confirm", screens.confirmScreen({
    source: S.source,
    alone,
    fileSize: alone && S.opened.length === 1 ? S.opened[0].file.size : null,
    image: previewImage("고른 이미지 미리보기"),
    notices: noticesNow(),
  }), { step: 1, dockHTML: screens.confirmDock });
  loadSource();
}

/** Ask the engine for the preview and, right behind it, the layouts of the chosen picture. */
function loadSource() {
  const source = S.source;
  const session = S.session;
  const current = () => session === S.session && S.source === source;
  if (!S.preview) {
    gate.catch(() => {}).then(() => engine.preview(source.id, 1600)).then((preview) => {
      if (!current()) return;
      S.preview = preview;
      paintPreview();
    }, (error) => {
      if (current()) sourceFailed(error);
    });
  }
  ensureLayouts().catch(() => {});
}

function ensureLayouts() {
  const source = S.source;
  const session = S.session;
  if (S.layouts) return Promise.resolve(S.layouts);
  if (!S.layoutsJob) {
    S.layoutsJob = gate.catch(() => {})
      .then(() => Promise.all([engine.layouts(source.id), engine.describe(source.id, 1, 1)]))
      .then(([layouts, probe]) => {
        if (session === S.session && S.source === source) {
          S.layouts = layouts;
          S.probeStem = probe.stem;
        }
        return layouts;
      });
    S.layoutsJob.catch(() => { if (S.source === source) S.layoutsJob = null; });
  }
  return S.layoutsJob;
}

/** The chosen picture cannot be used after all (a page that will not render, memory, a crashed engine). */
function sourceFailed(raw) {
  const error = normalizeError(raw);
  if (error.code === "CANCELLED") return;
  const entry = S.opened.find((item) => item.file.name === (S.source && S.source.origin)) || S.opened[0];
  showError(error, { file: entry ? entry.file : null });
  syncHistory();
}

// ===========================================================================
// 5 · How many pieces, which shape
// ===========================================================================
const counts = () => (engine.info && engine.info.counts) || FALLBACK_COUNTS;
const layoutsOf = (count) => (S.layouts && S.layouts[String(count)]) || [];

function showSplit() {
  setScreen("split", screens.splitScreen({
    source: S.source,
    counts: counts().map((count) => ({ count, usable: layoutsOf(count).some((layout) => layout.usable) })),
    image: previewImage("나눌 이미지"),
  }), { step: 2 });
  paintSplit(true);
  paintPreview();
}

/** The size line under the preview; the engine's plan replaces the estimate once it has answered. */
const splitCaption = () => screens.splitCaption(S.layout, S.layout && S.plan && S.plan.key === S.layout.key ? S.plan : null);

function paintSplit(relist) {
  if (S.screen !== "split") return;
  const layout = S.layout;
  for (const button of $$(".count", view)) button.setAttribute("aria-checked", String(Number(button.dataset.count) === S.count));
  setHTML($("#split-overlay"), layout ? gridLines(layout.cols, layout.rows) + gridCells(layout.cols, layout.rows) : "");
  $("#split-cap").textContent = splitCaption();
  const wrap = $("#layouts-wrap");
  if (!S.count) {
    wrap.hidden = true;
  } else if (relist) {
    wrap.hidden = false;
    setHTML($("#layouts"), screens.layoutCards(layoutsOf(S.count), layout, S.source.width / S.source.height));
    $("#layouts").scrollLeft = 0;
  } else {
    for (const card of $$(".layout", view)) card.setAttribute("aria-checked", String(Boolean(layout) && card.dataset.layout === layout.key));
  }
  setHTML(dock, screens.splitDock(layout));
  syncRadios(view);
}

/** The layout cards appear below the tapped control: lift them above the pinned action area. */
function revealLayouts() {
  const wrap = $("#layouts-wrap");
  if (!wrap || wrap.hidden || getComputedStyle(dock).position !== "sticky") return;
  const limit = window.innerHeight - dock.offsetHeight - 12;
  const bottom = wrap.getBoundingClientRect().bottom;
  if (bottom > limit) window.scrollBy({ top: bottom - limit, behavior: "smooth" });
}

function chooseLayout(layout) {
  S.layout = layout;
  S.plan = null;
  S.built = null;
  S.problem = null;
  if (!layout) return;
  const source = S.source;
  // the layout list is computed without the size cap; the plan has the final numbers
  gate.catch(() => {}).then(() => engine.describe(source.id, layout.rows, layout.cols)).then((info) => {
    if (S.source !== source || S.layout !== layout) return;
    S.plan = info.plan;
    S.stem = info.stem;
    if (S.screen === "split") $("#split-cap").textContent = splitCaption();
  }, () => { /* asked again when the user confirms */ });
}

// ===========================================================================
// 7 · Result: the pieces, the format, saving
// ===========================================================================
const formats = () => (engine.info && engine.info.formats && engine.info.formats.length ? engine.info.formats : FALLBACK_FORMATS);
const currentFormat = () => formats().find((format) => format.key === S.format) || formats()[0];
const isSet = () => currentFormat().kind === "images" && S.layout.count > 1;

function exportKey() {
  const format = currentFormat();
  return [S.source.id, S.layout.key, format.key, format.kind === "document" ? S.paper : "-"].join("|");
}
const builtNow = () => (S.built && S.built.key === exportKey() ? S.built : null);

/** Android's extra share button: only for types and sizes its share sheet takes. */
function shareOffered() {
  if (!platform.isAndroid || S.inApp) return false;
  const built = builtNow();
  if (built) return save.androidShareable(built.files);
  return save.androidShareLikely(currentFormat().key, isSet() ? S.layout.count : 1);
}

const resultDock = () => screens.resultDock({ ios: platform.isIOS, share: shareOffered() });

function showResult({ focus = true } = {}) {
  setScreen("result",
    screens.resultScreen({ source: S.source, layout: S.layout, plan: S.plan || S.layout, formats: formats() }),
    { step: 3, dockHTML: resultDock(), focus });
  paintPreview();
  paintResult();
}

function paintResult() {
  if (S.screen !== "result") return;
  const format = currentFormat();
  const images = format.kind === "images";
  for (const card of $$(".fmt", view)) card.setAttribute("aria-checked", String(card.dataset.format === format.key));
  // iOS shares every picture unless the ZIP is asked for; elsewhere picture sets always travel as one ZIP
  const zipped = images && (platform.isIOS ? S.zip : true);
  $("#format-help").textContent = formatCopy(format.key).help({ zipped });
  const paper = $("#paper-option");
  const zip = $("#zip-option");
  const zipChoice = images && platform.isIOS;
  paper.hidden = zipChoice;
  zip.hidden = !zipChoice;
  zip.setAttribute("aria-checked", String(S.zip));
  paper.classList.toggle("is-off", images);
  for (const button of $$("button", paper)) {
    button.setAttribute("aria-checked", String(button.dataset.paper === S.paper));
    button.disabled = images;
  }
  const problem = S.problem;
  setHTML($("#result-problem"), problem ? screens.problemNotice(problem.text) : "");
  if (!S.job) setExport(builtNow() ? "ready" : problem ? "failed" : "idle");
  const wanted = resultDock();
  if (dock.dataset.html !== wanted) {
    setHTML(dock, wanted);
    dock.dataset.html = wanted;
  }
  syncRadios(view);
}

// ===========================================================================
// 6 · Making the files (shown between two looks at the result screen)
// ===========================================================================
function showWork() {
  setScreen("work",
    screens.workScreen({ source: S.source, layout: S.layout, format: currentFormat().key, image: previewImage("") }),
    { step: 3, back: false, dockHTML: screens.cancelDock });
  paintPreview();
}

function paintWork(done, total, status) {
  if (S.screen !== "work") return;
  const cells = $$(".veil i", view);
  cells.forEach((cell, index) => { cell.className = index < done ? "is-done" : index === done ? "is-now" : ""; });
  const bar = $("#work-bar");
  bar.setAttribute("aria-valuenow", String(done));
  bar.firstElementChild.style.width = `${(100 * done) / total}%`;
  $("#work-figure").textContent = `${done} / ${total}`;
  if (status) $("#work-status").textContent = status;
}

/** Build the files for the current choice, then hand them over (or ask for one more tap). */
async function startExport(intent) {
  if (S.job) return;
  const source = S.source;
  const layout = S.layout;
  const format = currentFormat();
  const job = { key: exportKey(), intent, cancelled: false };
  const alive = () => S.job === job && !job.cancelled;
  S.job = job;
  S.problem = null;
  S.built = null;  // one finished set at a time: picture sets can be large
  setExport("working");
  showWork();
  platform.keepAwake(true);
  const started = performance.now();
  let built = null;
  let failure = null;
  try {
    await gate.catch(() => {});
    if (!alive()) return;
    job.key = exportKey();  // after the gate: an engine restart may have given the picture a new id
    const count = layout.count;
    paintWork(0, count);
    const { files } = await engine.export(source.id, layout.rows, layout.cols, format.key, { paper: S.paper }, {
      onTile: (done, total) => { if (alive()) paintWork(done, total, done >= total ? "파일로 묶는 중" : ""); },
    });
    if (!alive()) return;
    if (!files.length) throw new Error("the export produced no file");
    const outFiles = save.toFiles(files);
    built = { key: job.key, files: outFiles, zip: null, kind: format.kind };
    if (save.needsZip({ kind: format.kind, count: outFiles.length, zipPreferred: S.zip, intent })) {
      paintWork(count, count, "ZIP 파일로 묶는 중");
      built.zip = await save.zipFiles(outFiles, `${zipStem(outFiles)}.zip`);
    }
    const left = MIN_VISIBLE_MS - (performance.now() - started);
    if (left > 0) await new Promise((resolve) => setTimeout(resolve, left));
  } catch (raw) {
    failure = normalizeError(raw);
  }
  if (!alive()) return;  // cancelled meanwhile: cancelExport() has already put the result screen back
  S.job = null;
  platform.keepAwake(false);
  if (sheetName() === "cancel") closeSheet();  // "stop making them?" no longer applies
  if (failure) {
    if (failure.code !== "CANCELLED") {
      S.problem = { text: exportProblem(failure.code, failure.message, format.label) };
      // after a crash or an out-of-memory stop the worker is not trustworthy: start a fresh one behind the scenes
      if (failure.code === "ENGINE_CRASHED" || failure.code === "OUT_OF_MEMORY" || engine.state === "failed") recoverEngine();
    }
    showResult();
    if (S.problem) {
      // the notice sits below the format cards: bring it into view and tell screen readers
      const notice = $("#result-problem");
      if (notice) notice.scrollIntoView({ block: "center" });
      announce(S.problem.text);
    }
    return;
  }
  S.built = built;
  platform.persistStorage();
  showResult();
  handOver(intent, { afterBuild: true });
}

/** Base name for the ZIP: the engine's stem, or the common part of the tile names. */
function zipStem(files) {
  if (S.stem) return S.stem;
  return files[0].name.replace(/_\d+\.[a-z0-9]+$/i, "") || "조각조각";
}

function cancelExport() {
  const job = S.job;
  if (!job) return;
  job.cancelled = true;
  S.job = null;
  platform.keepAwake(false);
  recoverEngine();  // the only way to stop Python mid-export is to replace the worker
  closeSheet();
  showResult();
  syncHistory();
  toast("만들기를 그만뒀어요", { icon: "info" });
}

/**
 * Replace the worker and make sure the pictures are still reachable under the ids the screens hold.
 * engine.restart() reopens the files itself; if the ids moved anyway, the files are opened again
 * from here and the ids are matched up by position.
 */
function recoverEngine() {
  const session = S.session;
  let outcome = "ok";  // "ok" | "lost" (the pictures could not be matched up again) | "down" (no working engine)
  return afterGate(async () => {
    try {
      await untilStall(engine.restart());
    } catch (error) {
      outcome = "down";
      return;
    }
    if (session !== S.session || !S.source) return;
    let intact = false;
    try {
      const probe = await engine.describe(S.source.id, 1, 1);
      intact = probe.plan.src_w === S.source.width && probe.plan.src_h === S.source.height
        && (!S.probeStem || probe.stem === S.probeStem);
    } catch (error) {
      intact = false;
    }
    if (intact) return;
    try {
      await engine.reset();
      const fresh = [];
      for (const item of S.opened) {
        const result = await engine.openFile(item.file, { password: item.password });
        fresh.push(...result.sources);
      }
      if (session !== S.session) return;
      if (fresh.length !== S.sources.length) throw new Error("the files no longer give the same pictures");
      S.sources.forEach((source, index) => { source.id = fresh[index].id; });
      S.built = null;  // its key carries the old id
      const preview = await engine.preview(S.source.id, 1600);  // reset() revoked the old preview URL
      if (session !== S.session) return;
      S.preview = preview;
      paintPreview();
      if (S.screen === "result") paintResult();
    } catch (error) {
      outcome = engine.state === "ready" ? "lost" : "down";
    }
  }).then(() => {
    if (outcome === "ok" || session !== S.session || S.screen === "home") return;
    if (S.job) {  // an export that was waiting for this engine cannot go on
      S.job.cancelled = true;
      S.job = null;
      platform.keepAwake(false);
    }
    closeSheet();
    if (outcome === "down") {
      showError({ code: S.stalled ? "OFFLINE" : "ENGINE_CRASHED", message: "" }, { file: S.files[0] || null });
    } else {
      showHome();
      toast("문제가 생겨서 처음으로 돌아왔어요", { icon: "alert", long: true });
    }
    syncHistory();
  });
}

// ---------------------------------------------------------------------------
// Handing the files over
// ---------------------------------------------------------------------------
function delivery(intent) {
  const built = builtNow();
  return built ? save.deliveryFor({ intent, files: built.files, zip: built.zip, kind: built.kind, zipPreferred: S.zip }) : null;
}

/** Save / share was tapped. */
function requestDelivery(intent) {
  if (S.job) return;
  const built = builtNow();
  if (!built) {
    startExport(intent);
    return;
  }
  if (save.needsZip({ kind: built.kind, count: built.files.length, zipPreferred: S.zip, intent }) && !built.zip) {
    const button = $('[data-action="save"]', dock);
    setBusy(button, true, "묶는 중");
    save.zipFiles(built.files, `${zipStem(built.files)}.zip`).then((zip) => {
      built.zip = zip;
      setBusy(button, false);
      if (builtNow() === built && S.screen === "result") handOver(intent, { afterBuild: true });
    }, () => {
      setBusy(button, false);
      toast("ZIP 파일로 묶지 못했어요", { icon: "alert" });
    });
    return;
  }
  handOver(intent, { afterBuild: false });
}

/**
 * Deliver now when that is allowed, otherwise ask for one more tap.
 * A download may start as long as the tap that began the build is still "active" (5 seconds);
 * the share sheet always gets a tap of its own after a build.
 */
function handOver(intent, { afterBuild }) {
  const plan = delivery(intent);
  if (!plan) return;
  if (intent === "share" && !save.androidShareable(plan.files)) {
    toast("파일이 커서 공유할 수 없어요. 저장하기를 눌러 주세요", { icon: "alert", long: true, wrap: true });
    paintResult();
    return;
  }
  if (plan.mode === "none") {
    toast("이 화면에서는 저장할 수 없어요. Safari에서 열어 주세요", { icon: "alert", long: true, wrap: true });
    return;
  }
  const active = Boolean(navigator.userActivation && navigator.userActivation.isActive);
  if (!afterBuild || (plan.mode === "download" && active)) {
    deliverNow(intent);
    return;
  }
  openSheet(deliverSheet({ files: plan.files, mode: plan.mode, intent }));
}

/** Runs synchronously inside a tap, with files that already exist. */
function deliverNow(intent) {
  const plan = delivery(intent);
  if (!plan || plan.mode === "none") return;
  if (plan.mode === "download") {
    const file = plan.files[0];
    save.downloadFile(file);
    if (sheetName() === "deliver") closeSheet("done");
    flashSaved();
    toast(`저장했어요 · ${file.name}`);
    return;
  }
  const outcome = save.shareFiles(plan.files);  // first thing in the tap: share() spends the tap's activation
  if (sheetName() === "deliver") closeSheet("done");
  outcome.then((result) => {
    if (result === "shared") {
      toast("공유 창을 열었어요", { icon: "share" });
    } else if (result === "blocked") {
      // the browser no longer counted the tap: one more tap on a fresh sheet
      if (builtNow() && S.screen === "result") openSheet(deliverSheet({ files: plan.files, mode: plan.mode, intent, again: true }));
    } else if (result === "failed") {
      toast("공유 창을 열지 못했어요. 다시 눌러 주세요", { icon: "alert", long: true, wrap: true });
    }
  });
}

function flashSaved() {
  const button = $('[data-action="save"]', dock);
  if (!button || button.dataset.idle != null) return;
  const idle = button.innerHTML;
  button.dataset.idle = idle;
  button.setAttribute("aria-busy", "true");
  button.innerHTML = `${ic("check", "ic--20")}저장했어요`;
  setTimeout(() => {
    if (!button.isConnected) return;
    button.innerHTML = idle;
    delete button.dataset.idle;
    button.removeAttribute("aria-busy");
  }, 1800);
}

// ===========================================================================
// 8 · Errors
// ===========================================================================
/**
 * @param {{code: string, message?: string}} error
 * @param {{file?: File | null, typed?: string}} options  typed: the password that was just refused (kept in the field)
 */
function showError(error, { file = null, typed = "" } = {}) {
  const code = error.code || "INTERNAL";
  const spec = errorView(code, { name: file ? file.name : "", message: error.message });
  S.error = { code, file, spec };
  setExport("idle");
  setScreen("error", screens.errorScreen({ code, spec, file, typed, notices: noticesNow() }),
    { step: 0, dockHTML: screens.errorDock(spec, typed) });
  if (code === "WRONG_PASSWORD") {
    const input = $("#password");
    input.focus({ preventScroll: true });
    input.select();
  }
}

async function unlock() {
  const input = $("#password");
  const entry = S.error;
  if (!input || !input.value || !entry || !entry.file) return;
  const password = input.value;
  const session = S.session;
  const button = $('[data-action="unlock"]', dock);
  const here = () => session === S.session && (S.screen === "error" || S.screen === "prep");
  setBusy(button, true, "여는 중");
  // A quick answer stays on this screen (spinner in the button); a slow one gets the waiting screen.
  let waiting = false;
  const timer = setTimeout(() => {
    if (session !== S.session || S.screen !== "error") return;
    waiting = true;
    showPrep([entry.file]);
    paintPrep();
  }, UNLOCK_INLINE_MS);
  let result = null;
  let failure = null;
  try {
    S.inflight = engine.openFile(entry.file, { password });
    result = await S.inflight;
  } catch (raw) {
    failure = normalizeError(raw);
  } finally {
    S.inflight = null;
    clearTimeout(timer);
  }
  if (waiting && here()) await holdPrep();
  if (!here()) return;
  if (result) {
    acceptSources(session, result.sources, [{ file: entry.file, password }], S.failures, Boolean(result.truncated));
  } else if (failure.code === "WRONG_PASSWORD" || failure.code === "ENCRYPTED") {
    if (waiting) {
      showError({ code: "WRONG_PASSWORD" }, { file: entry.file, typed: password });
    } else {
      setBusy(button, false);
      S.error.code = "WRONG_PASSWORD";
      $(".error", view).dataset.errorCode = "WRONG_PASSWORD";
      $("#password-field").classList.add("is-error");
      $("#password-message").textContent = screens.WRONG_PASSWORD_TEXT;
      input.focus();
      input.select();
    }
  } else if (failure.code !== "CANCELLED") {
    showError(failure, { file: entry.file });
  }
  syncHistory();
}

function retry() {
  if (S.screen === "result") {  // a failed export
    startExport("save");
    return;
  }
  const files = S.files.slice();
  if (!files.length) {
    showHome();
    syncHistory();
    return;
  }
  if (engine.state !== "ready") {
    // a crashed or stuck engine: forget what it held and start a fresh worker (not gated - a start may hang)
    S.stalled = false;
    engine.reset().catch(() => {});
    engine.restart().catch(() => {});
  }
  openFiles(files);
}

// ===========================================================================
// Sheets that need state
// ===========================================================================
function openInstall() {
  const mode = platform.installMode();
  if (mode === "prompt") {
    platform.promptInstall().then((outcome) => { if (outcome === "accepted") toast("앱을 설치했어요"); });
  } else if (mode === "android") {
    openSheet(installSheetAndroid({ canPrompt: platform.canPromptInstall() }));
  } else if (mode === "ios") {
    // Safari 26 moved Share behind "...".  Other iOS browsers do not say which Safari is installed: use the newer wording.
    const modern = !platform.isIOSSafari || platform.safariVersion === 0 || platform.safariVersion >= 26;
    openSheet(installSheetIOS({ inSafari: platform.isIOSSafari, modern }));
  }
}

function openInApp() {
  openSheet({
    ...inAppSheet(S.inApp),
    onClose: () => {
      if (S.inAppAsked) return;
      S.inAppAsked = true;  // "continue anyway": from here on this is a normal browser to us
      maybeStartEngine();
    },
  });
}

// ===========================================================================
// Actions (every control carries data-action)
// ===========================================================================
const ACTIONS = {
  back: () => requestBack(),
  "close-sheet": () => closeSheet(),
  pick: () => openPicker(),
  "pick-photos": () => { closeSheet(); inputPhotos.click(); },
  "pick-files": () => { closeSheet(); inputFiles.click(); },
  cancel: () => {
    if (S.screen === "work") cancelExport();
    else requestBack();
  },
  "cancel-confirm": () => {
    closeSheet();
    cancelExport();
  },
  select: (element) => {
    S.pickId = element.dataset.source;
    for (const thumb of $$(".thumb", view)) thumb.setAttribute("aria-checked", String(thumb === element));
    setHTML(dock, screens.pickDock(S.pickId));
  },
  next: () => {
    const source = S.sources.find((item) => item.id === S.pickId);
    if (!source) return;
    if (S.source !== source) setSource(source);
    showConfirm();
    syncHistory();
  },
  "confirm-no": () => {
    if (S.sources.length > 1) {
      requestBack();
      return;
    }
    // a single picture: back to the start, with the picker open again
    showHome();
    syncHistory();
    openPicker();
  },
  "confirm-yes": (element) => {
    const source = S.source;
    setBusy(element, !S.layouts, "네, 맞아요");
    ensureLayouts().then(() => {
      if (S.source !== source || S.screen !== "confirm") return;
      showSplit();
      syncHistory();
    }, (error) => { if (S.source === source && S.screen === "confirm") sourceFailed(error); });
  },
  count: (element) => {
    const count = Number(element.dataset.count);
    const list = layoutsOf(count);
    S.count = count;
    chooseLayout(list.find((layout) => layout.recommended && layout.usable) || list.find((layout) => layout.usable) || null);
    paintSplit(true);
    revealLayouts();
  },
  layout: (element) => {
    const layout = layoutsOf(S.count).find((item) => item.key === element.dataset.layout);
    if (!layout || !layout.usable) return;
    chooseLayout(layout);
    paintSplit(false);
  },
  "split-no": () => {
    S.count = null;
    chooseLayout(null);
    paintSplit(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  },
  "split-yes": (element) => {
    const source = S.source;
    const layout = S.layout;
    if (!layout) return;
    const go = () => {
      if (S.source !== source || S.layout !== layout || S.screen !== "split") return;
      showResult();
      syncHistory();
    };
    if (S.plan && S.plan.key === layout.key) {
      go();
      return;
    }
    setBusy(element, true, "네, 분할할게요");
    gate.catch(() => {}).then(() => engine.describe(source.id, layout.rows, layout.cols)).then((info) => {
      if (S.layout === layout) {
        S.plan = info.plan;
        S.stem = info.stem;
      }
      go();
    }, (raw) => {
      setBusy(element, false);
      const error = normalizeError(raw);
      if (error.code === "ENGINE_CRASHED" || engine.state === "failed") sourceFailed(error);
      else if (error.code !== "CANCELLED") toast(exportProblem(error.code, error.message, ""), { icon: "alert", long: true, wrap: true });
    });
  },
  format: (element) => {
    S.format = element.dataset.format;
    S.problem = null;
    paintResult();
  },
  paper: (element) => {
    S.paper = element.dataset.paper;
    S.problem = null;
    paintResult();
  },
  "zip-toggle": () => {
    S.zip = !S.zip;
    paintResult();
  },
  save: () => requestDelivery("save"),
  share: () => requestDelivery("share"),
  deliver: (element) => deliverNow(element.dataset.intent === "share" ? "share" : "save"),
  resplit: () => requestBack(),
  restart: () => {
    showHome();
    syncHistory();
  },
  retry: () => retry(),
  unlock: () => unlock(),
  "password-eye": (element) => {
    const input = $("#password");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    element.innerHTML = ic(show ? "eye-off" : "eye");
    element.setAttribute("aria-label", show ? "암호 숨기기" : "암호 보기");
  },
  install: () => openInstall(),
  "install-now": () => {
    closeSheet();
    platform.promptInstall().then((outcome) => {
      if (outcome === "accepted") toast("앱을 설치했어요");
      else if (outcome === "unavailable") toast("브라우저 메뉴에서 ‘앱 설치’를 골라 주세요", { icon: "info", long: true, wrap: true });
    });
  },
  inapp: () => openInApp(),
  "open-external": () => { window.location.href = platform.externalUrl(S.inApp); },
  "copy-url": () => {
    platform.copyText(platform.shareableUrl()).then((ok) => {
      if (ok) toast("주소를 복사했어요", { icon: "copy" });
      else toast("주소를 복사하지 못했어요. 주소창에서 직접 복사해 주세요", { icon: "alert", long: true, wrap: true });
    });
  },
  update: (element) => {
    setBusy(element, true, "");
    if (!platform.applyUpdate()) window.location.reload();
  },
};

document.addEventListener("click", (event) => {
  const element = event.target instanceof Element ? event.target.closest("[data-action]") : null;
  if (!element || element.disabled || element.getAttribute("aria-busy") === "true") return;
  const action = ACTIONS[element.dataset.action];
  if (!action) return;
  action(element, event);
  syncRadios();
});

document.addEventListener("input", (event) => {
  if (event.target.id !== "password" || !S.error) return;
  $("#password-field").classList.remove("is-error");
  $("#password-message").textContent = "";
  const button = $('[data-action="unlock"]', dock);
  if (button) button.disabled = !event.target.value;
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.id === "password" && event.target.value) unlock();
});

// ---------------------------------------------------------------------------
// Files arriving: the two inputs, drag and drop, paste
// ---------------------------------------------------------------------------
for (const input of [inputPhotos, inputFiles]) {
  input.addEventListener("change", () => {
    const files = Array.from(input.files || []);
    input.value = "";  // picking the same file again must fire "change" again
    if (files.length) openFiles(files);
  });
}

const carriesFiles = (event) => Boolean(event.dataTransfer) && Array.from(event.dataTransfer.types || []).includes("Files");
const takesFiles = () => !S.job && S.screen !== "work";
let dragDepth = 0;

function setDragging(on) {
  document.body.classList.toggle("is-drag", on);
  const zone = $("#drop");
  if (!zone) return;
  zone.classList.toggle("is-over", on);
  const title = $("[data-drop-title]", zone);
  if (title && platform.isDesktop) title.textContent = on ? "여기에 놓으면 바로 시작해요" : "파일을 여기에 끌어다 놓으세요";
}

window.addEventListener("dragenter", (event) => {
  if (!carriesFiles(event)) return;
  event.preventDefault();
  dragDepth += 1;
  if (takesFiles()) setDragging(true);
});
window.addEventListener("dragover", (event) => {
  if (!carriesFiles(event)) return;
  event.preventDefault();  // without this the browser would open the dropped file instead
  event.dataTransfer.dropEffect = takesFiles() ? "copy" : "none";
});
window.addEventListener("dragleave", (event) => {
  if (!carriesFiles(event)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) setDragging(false);
});
window.addEventListener("drop", (event) => {
  if (!carriesFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  setDragging(false);
  if (takesFiles()) openFiles(event.dataTransfer.files);
});
window.addEventListener("paste", (event) => {
  const files = Array.from((event.clipboardData && event.clipboardData.files) || []);
  if (!files.length || !takesFiles()) return;
  event.preventDefault();
  openFiles(files);
});

// ===========================================================================
// Start
// ===========================================================================
// Download the engine through the service worker, so that it is kept for offline use.
engine.gate = () => platform.whenControlled();
engine.subscribe((current) => {
  document.body.dataset.engine = current.state;
  watchEngine();
  if (current.state === "ready") {
    try { localStorage.setItem("jj.engine", "1"); } catch (error) { /* only changes the wording of the waiting screen */ }
    platform.precacheEngine(engine.usedFiles());
  }
  paintPrep();
});

function start() {
  // After a reload the old entries of this page are still in the history: walk back to the first one.
  if (history.state && history.state.jj && history.state.d > 0) {
    nav.skip += 1;
    history.go(-history.state.d);
  } else {
    history.replaceState({ jj: 1, d: 0 }, "");
  }
  S.inApp = platform.inAppBrowser();
  if (platform.isAndroid) inputFiles.accept = ANDROID_FILE_TYPES;
  document.body.dataset.ready = "1";
  placeDock();
  paintHome();
  paintTopBar();
  syncRadios();
  platform.onInstallChange(paintTopBar);
  platform.registerServiceWorker({
    onUpdate: () => {
      S.updateReady = true;
      if (S.screen === "home") paintHome();
    },
  });
  if (S.inApp && !platform.cameFromHandOff) openInApp();
  maybeStartEngine();
}

start();
