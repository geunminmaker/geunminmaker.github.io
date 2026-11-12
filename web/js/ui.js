// Small DOM helpers shared by the screens: templating, the bottom sheet,
// the toast, the live region and keyboard support for radio groups.

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

/** Inline icon from the sprite in index.html. */
export const ic = (name, cls = "") => `<svg class="ic ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

export const px = (width, height) => `${width} × ${height}px`;

export function formatBytes(bytes) {
  if (!(bytes > 0)) return "0KB";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))}KB`;
  const mb = bytes / (1024 * 1024);
  return `${mb >= 100 ? Math.round(mb) : mb.toFixed(1)}MB`;
}

/**
 * innerHTML plus the two things markup cannot do here:
 *  - the page's CSP forbids style="" attributes, so templates write data-css="..." and it is applied through the CSSOM;
 *  - radio groups get their roving tabindex.
 */
export function setHTML(element, html) {
  element.innerHTML = html;
  for (const node of $$("[data-css]", element)) {
    node.style.cssText = node.dataset.css;
    node.removeAttribute("data-css");
  }
  syncRadios(element);
}

// ---------------------------------------------------------------------------
// Radio groups: one tab stop per group, arrow keys move and select
// ---------------------------------------------------------------------------
export function syncRadios(root = document) {
  for (const group of $$('[role="radiogroup"]', root)) {
    const radios = $$('[role="radio"]', group);
    const enabled = radios.filter((radio) => !radio.disabled);
    const current = enabled.find((radio) => radio.getAttribute("aria-checked") === "true") || enabled[0];
    for (const radio of radios) radio.tabIndex = radio === current ? 0 : -1;
  }
}

document.addEventListener("keydown", (event) => {
  const radio = event.target instanceof Element ? event.target.closest('[role="radio"]') : null;
  if (!radio) return;
  const group = radio.closest('[role="radiogroup"]');
  if (!group) return;
  const radios = $$('[role="radio"]', group).filter((item) => !item.disabled);
  const index = radios.indexOf(radio);
  let next = -1;
  if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % radios.length;
  else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + radios.length) % radios.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = radios.length - 1;
  if (next < 0 || !radios[next]) return;
  event.preventDefault();
  radios[next].focus();
  radios[next].click();
});

// ---------------------------------------------------------------------------
// Live region + toast
// ---------------------------------------------------------------------------
const liveEl = $("#live");
const toastEl = $("#toast");
let toastTimer = 0;

/** Text for screen readers (and for anything that looks at role="status"). */
export function announce(text) {
  // identical text twice in a row would not be read again: a zero-width space makes it new
  liveEl.textContent = liveEl.textContent === text ? text + "​" : text;
}

export function toast(message, { icon = "check", long = false, wrap = false } = {}) {
  toastEl.innerHTML = ic(icon, "ic--20") + `<span>${esc(message)}</span>`;
  toastEl.classList.toggle("toast--wrap", wrap);
  toastEl.classList.add("is-open");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("is-open"), long ? 5200 : 2600);
  announce(message);
}

export function hideToast() {
  clearTimeout(toastTimer);
  toastEl.classList.remove("is-open");
}

// ---------------------------------------------------------------------------
// Bottom sheet (a centred dialog on wide screens)
// ---------------------------------------------------------------------------
const appEl = $("#app");
const sheetEl = $("#sheet");
const scrimEl = $("#scrim");
let sheet = null;  // { name, onClose, returnFocus, watcher }

export const sheetName = () => (sheet ? sheet.name : null);

/**
 * @param {{name: string, title: string, body: string, foot?: string, onClose?: (reason: string) => void}} spec
 *        title/body/foot are trusted markup built by the caller (user text must be escaped there)
 */
export function openSheet({ name, title, body, foot = "", onClose = null }) {
  const returnFocus = sheet ? sheet.returnFocus : document.activeElement;
  if (sheet) releaseSheet("replace");
  setHTML(sheetEl,
    '<div class="sheet__grab"></div>'
    + `<div class="sheet__head"><h2 class="sheet__title" id="sheet-title">${title}</h2>`
    + `<button type="button" class="iconbtn" data-action="close-sheet" aria-label="닫기">${ic("close")}</button></div>`
    + `<div class="sheet__body">${body}</div>`
    + (foot ? `<div class="sheet__foot">${foot}</div>` : ""));
  sheetEl.dataset.name = name;
  sheetEl.getBoundingClientRect();  // commit the closed position so the slide-in animates
  scrimEl.classList.add("is-open");
  sheetEl.classList.add("is-open");
  document.documentElement.style.overflow = "hidden";
  appEl.inert = true;  // focus and screen readers stay inside the sheet
  sheet = { name, onClose, returnFocus, watcher: null };
  // Android's back button (and Esc) close the sheet instead of leaving the screen, where CloseWatcher exists.
  if (typeof CloseWatcher === "function") {
    try {
      const watcher = new CloseWatcher();
      watcher.onclose = () => { if (sheet && sheet.watcher === watcher) closeSheet("dismiss"); };
      sheet.watcher = watcher;
    } catch (error) { /* not allowed right now: Esc and the scrim still work */ }
  }
  sheetEl.focus({ preventScroll: true });
}

function releaseSheet(reason) {
  const closing = sheet;
  sheet = null;
  if (!closing) return null;
  if (closing.watcher) {
    closing.watcher.onclose = null;
    try { closing.watcher.destroy(); } catch (error) { /* already gone */ }
  }
  if (closing.onClose) closing.onClose(reason);
  return closing;
}

/** @param {string} reason "dismiss" (user closed it) or "done" (an action in the sheet finished) */
export function closeSheet(reason = "dismiss") {
  if (!sheet) return;
  const closing = releaseSheet(reason);
  scrimEl.classList.remove("is-open");
  sheetEl.classList.remove("is-open");
  document.documentElement.style.overflow = "";
  appEl.inert = false;
  const target = closing.returnFocus;
  if (target && target.isConnected && typeof target.focus === "function") target.focus({ preventScroll: true });
}

document.addEventListener("keydown", (event) => {
  if (!sheet) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeSheet("dismiss");
    return;
  }
  if (event.key !== "Tab") return;
  // keep Tab inside the sheet
  const focusable = $$('button:not(:disabled), a[href], input:not(:disabled), [tabindex="0"]', sheetEl)
    .filter((node) => node.tabIndex >= 0 && node.offsetParent !== null);
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || active === sheetEl)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
});

/** Move focus to the screen's headline after a screen change. */
export function focusHeadline() {
  const headline = $("#view .h1");
  if (headline) headline.focus({ preventScroll: true });
}

/** Button with a spinner while something runs (keeps its width and its data-action). */
export function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    if (button.dataset.idle == null) button.dataset.idle = button.innerHTML;
    button.setAttribute("aria-busy", "true");
    button.innerHTML = `<span class="spin"></span>${esc(label || "")}`;
  } else if (button.dataset.idle != null) {
    button.innerHTML = button.dataset.idle;
    delete button.dataset.idle;
    button.removeAttribute("aria-busy");
  }
}
