// Markup of the screens.  Every function here is pure: values in, an HTML string out.
// app.js decides when a screen is shown and what the values are.
//
// Two conventions the markup relies on (see ui.js): data-css="..." becomes an inline style after
// insertion (the CSP has no room for style attributes), and every control carries data-action.

import { esc, formatBytes, ic, px } from "./ui.js";
import { gridLines, miniLayout, mosaic, veil } from "./ui-grid.js";
import { failureReason, formatCopy, paperText } from "./copy.js";

/** Typographic file badge: the extension, at most four letters. */
export const fileBadge = (name) => {
  const ext = /\.([a-z0-9]{1,5})$/i.exec(name || "");
  return ext ? ext[1].toUpperCase().slice(0, 4) : "파일";
};

const fileLine = (badge, title, sub) => `<div class="fileline"><span class="fbadge">${esc(badge)}</span>`
  + `<div class="row__t"><b>${esc(title)}</b><small>${esc(sub)}</small></div></div>`;

const stageCss = (source) => `--ar:${(source.width / source.height).toFixed(4)}`;

// ---------------------------------------------------------------------------
// Home (the screen itself is static markup in index.html; this is what gets added to it)
// ---------------------------------------------------------------------------
export function homeNotices({ inApp, updateReady }) {
  let html = "";
  if (inApp) {
    const where = inApp.key === "webview" ? "앱" : inApp.name;
    html += `<button type="button" class="notice" data-action="inapp">${ic("info")}<span class="notice__t">`
      + `<b>${esc(where)} 안에서는 저장이 안 될 수 있어요</b><small>Safari나 Chrome으로 여는 방법 보기</small></span>${ic("chev-r", "ic--20")}</button>`;
  }
  if (updateReady) {
    html += '<div class="notice notice--action"><span class="notice__t"><b>새 버전이 있어요</b></span>'
      + '<button type="button" class="textbtn textbtn--brand" data-action="update">업데이트</button></div>';
  }
  return html;
}

// ---------------------------------------------------------------------------
// Notices shared by several screens
// ---------------------------------------------------------------------------
/** "N files could not be opened" (with names and reasons) and "only the first 300". */
export function notices(failures, truncated) {
  let html = "";
  const count = failures.length;
  if (count) {
    const shown = failures.slice(0, 4).map((item) => `<small>${esc(item.name)} · ${failureReason(item.code)}</small>`).join("");
    const more = count > 4 ? `<small>그 밖에 ${count - 4}개</small>` : "";
    html += `<div class="notice notice--plain">${ic("info")}<div class="notice__t"><b>${count}개 파일은 열 수 없었어요</b>${shown}${more}</div></div>`;
  }
  if (truncated) {
    html += `<div class="notice notice--plain">${ic("info")}<div class="notice__t"><b>처음 300장만 가져왔어요</b>`
      + "<small>한 번에 300장까지 보여 드려요. 찾는 장이 없으면 파일을 나눠서 다시 골라 주세요.</small></div></div>";
  }
  return html;
}

/** The same notices as one sentence for the live region. */
export function noticesSpoken(failures, truncated) {
  const parts = [];
  if (failures.length) {
    parts.push(`${failures.length}개 파일은 열 수 없었어요. `
      + failures.map((item) => `${item.name}: ${failureReason(item.code)}`).join(", "));
  }
  if (truncated) parts.push("처음 300장만 가져왔어요.");
  return parts.join(" ");
}

// ---------------------------------------------------------------------------
// 2 · Preparing (the texts and the bar are filled in by app.js while things load)
// ---------------------------------------------------------------------------
export function prepScreen(files) {
  const one = files.length === 1;
  const size = formatBytes(files.reduce((sum, file) => sum + file.size, 0));
  return '<section class="screen prep"><h1 class="h1" tabindex="-1" id="prep-title"></h1><p class="lead" id="prep-lead"></p>'
    + fileLine(one ? fileBadge(files[0].name) : `${files.length}개`, one ? files[0].name : `파일 ${files.length}개`, size)
    + '<div class="prog"><div class="bar" id="prep-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i></i></div>'
    + '<div class="prog__row"><span id="prep-status" aria-live="polite"></span><span id="prep-figure"></span></div></div>'
    + '<p class="cap prep__note" id="prep-note"></p><div data-dock-slot></div></section>';
}

export const cancelDock = '<button type="button" class="btn btn--secondary btn--block" data-action="cancel">취소</button>';

// ---------------------------------------------------------------------------
// 3 · Pick one picture
// ---------------------------------------------------------------------------
/**
 * How the candidates are grouped on the thumbnail screen:
 * one file -> one grid; only loose pictures -> one grid of square crops;
 * a mix -> a section per document, and the loose pictures together.
 */
function thumbSections(sources) {
  const groups = [];
  for (const source of sources) {
    const last = groups[groups.length - 1];
    if (last && last.origin === source.origin) last.items.push(source);
    else groups.push({ origin: source.origin, items: [source] });
  }
  const loose = (group) => group.items.length === 1 && group.items[0].kind === "image";
  if (groups.length === 1) return [{ title: "", items: groups[0].items, cover: false }];
  if (groups.every(loose)) return [{ title: "", items: sources.slice(), cover: true }];
  const sections = [];
  let photos = null;
  for (const group of groups) {
    if (loose(group)) {
      if (!photos) {
        photos = { title: "사진", items: [], cover: true };
        sections.push(photos);
      }
      photos.items.push(group.items[0]);
    } else {
      sections.push({ title: group.origin, items: group.items, cover: false });
    }
  }
  return sections;
}

const unitText = (items) => {
  if (items.every((item) => item.kind === "page")) return `${items.length}쪽`;
  if (items.every((item) => item.kind === "image" && item.label === item.origin)) return `${items.length}장`;
  return `이미지 ${items.length}개`;
};

function pickLead(sections, total, fileCount) {
  if (sections.length === 1 && !sections[0].cover) {
    const items = sections[0].items;
    const name = `<b>${esc(items[0].origin)}</b>`;
    if (items.every((item) => item.kind === "page")) return `${name} · ${total}쪽 중에서 한 장만 골라 주세요.`;
    return `${name}에 들어 있는 이미지 ${total}개 중에서 하나만 골라 주세요.`;
  }
  if (sections.length === 1) return `불러온 사진 ${total}장 중에서 한 장만 골라 주세요.`;
  return `불러온 파일 ${fileCount}개에서 찾은 이미지 ${total}개 중에서 하나만 골라 주세요.`;
}

function thumbGrid(section, pickId) {
  const items = section.items;
  const first = items[0];
  const sameSize = items.every((item) => item.width === first.width && item.height === first.height);
  const aspects = items.map((item) => item.width / item.height);
  const sameShape = aspects.every((aspect) => Math.abs(aspect / aspects[0] - 1) < 0.02);
  // pages and embedded pictures are shown whole; one shared frame shape keeps the rows even
  const frame = section.cover || !sameShape ? "1/1" : String(Math.min(1.8, Math.max(0.6, aspects[0])).toFixed(4));
  const two = items.length === 2 || items.length === 4;
  const thumbPx = two ? 512 : 360;  // two columns are wide enough to need the larger thumbnail
  const long = (item) => Math.max(item.width / item.height, item.height / item.width) >= 2.2;
  const thumbs = items.map((item) => `<button type="button" class="thumb${section.cover && long(item) ? " thumb--whole" : ""}" role="radio"`
    + ` aria-checked="${item.id === pickId}" data-action="select" data-source="${esc(item.id)}" data-px="${thumbPx}" aria-label="${esc(item.label)}">`
    + `<span class="thumb__frame"><img alt="" decoding="async"><span class="thumb__check">${ic("check", "ic--16")}</span></span>`
    + `<span class="thumb__label"><b>${esc(item.label)}</b>${sameSize ? "" : `<small>${item.width} × ${item.height}</small>`}</span></button>`).join("");
  const head = section.title
    ? `<div class="thumbs__head"><b>${esc(section.title)}</b><span class="cap">${unitText(items)}</span></div>` : "";
  return `${head}<div class="thumbs${two ? " thumbs--2" : ""}${section.cover ? " thumbs--cover" : ""}" data-css="--far:${frame}">${thumbs}</div>`;
}

/** @param {{sources: object[], pickId: string | null, fileCount: number, notices: string}} view */
export function pickScreen({ sources, pickId, fileCount, notices: noticeHtml }) {
  const sections = thumbSections(sources);
  return '<section class="screen pick"><h1 class="h1" tabindex="-1">이 중에 어떤 걸\n나눌까요?</h1>'
    + `<p class="lead">${pickLead(sections, sources.length, fileCount)}</p>${noticeHtml}`
    + `<div role="radiogroup" aria-label="이미지 선택">${sections.map((section) => thumbGrid(section, pickId)).join("")}</div>`
    + "<div data-dock-slot></div></section>";
}

export const pickDock = (pickId) => `<button type="button" class="btn btn--primary btn--block" data-action="next"${pickId ? "" : " disabled"}>다음</button>`;

// ---------------------------------------------------------------------------
// 4 · Is this the picture?
// ---------------------------------------------------------------------------
/**
 * @param {{source: object, alone: boolean, fileSize: number | null, image: string, notices: string}} view
 *        alone: the only candidate (then the file name is the title); image: the preview <img> markup
 */
export function confirmScreen({ source, alone, fileSize, image, notices: noticeHtml }) {
  const title = alone || source.label === source.origin ? source.origin : `${source.origin} · ${source.label}`;
  const facts = [px(source.width, source.height)];
  if (source.kind === "page") {
    const paper = paperText(source.physical_mm);
    if (paper) facts.push(paper);
  }
  if (fileSize != null) facts.push(formatBytes(fileSize));
  return '<section class="screen confirm"><h1 class="h1" tabindex="-1">이 이미지가 맞나요?</h1>'
    + `<figure class="stage" data-css="${stageCss(source)}"><div class="stage__fit">${image}</div></figure>`
    + `<div class="meta"><b>${esc(title)}</b><span>${facts.join(" · ")}</span></div>${noticeHtml}`
    + "<div data-dock-slot></div></section>";
}

export const confirmDock = '<div class="btnrow"><button type="button" class="btn btn--secondary" data-action="confirm-no">아니요</button>'
  + '<button type="button" class="btn btn--primary" data-action="confirm-yes">네, 맞아요</button></div>';

// ---------------------------------------------------------------------------
// 5 · How many pieces, which shape
// ---------------------------------------------------------------------------
/** @param {{source: object, counts: {count: number, usable: boolean}[], image: string}} view */
export function splitScreen({ source, counts, image }) {
  const buttons = counts.map(({ count, usable }) => '<button type="button" class="count" role="radio" aria-checked="false" data-action="count"'
    + ` data-count="${count}" aria-label="${count}조각"${usable ? "" : " disabled"}>${count}</button>`).join("");
  return '<section class="screen split"><h1 class="h1" tabindex="-1">몇 조각으로 나눌까요?</h1>'
    + `<div class="split__stage"><figure class="stage" data-css="${stageCss(source)}"><div class="stage__fit">${image}`
    + '<div id="split-overlay"></div></div></figure><p class="cap stage__cap" id="split-cap" aria-live="polite"></p></div>'
    + `<div class="split__panel"><div class="counts" role="radiogroup" aria-label="조각 수">${buttons}</div>`
    + '<div class="layouts-wrap" id="layouts-wrap" hidden><div class="lw__head"><span class="label">나누는 모양</span><span class="cap">가로 × 세로</span></div>'
    + '<div class="layouts" role="radiogroup" aria-label="나누는 모양" id="layouts"></div></div>'
    + "<div data-dock-slot></div></div></section>";
}

/** One card per layout: a pictogram in the picture's proportions, "3 × 2" and the same in words. */
export function layoutCards(layouts, selected, aspect) {
  return layouts.map((layout) => `<button type="button" class="layout${layout.rare ? " layout--rare" : ""}" role="radio"`
    + ` aria-checked="${layout === selected}" data-action="layout" data-layout="${layout.key}"`
    + ` aria-label="가로 ${layout.cols}칸, 세로 ${layout.rows}줄${layout.recommended ? ", 추천" : ""}"${layout.usable ? "" : " disabled"}>`
    + `${layout.recommended ? '<span class="layout__tag">추천</span>' : ""}${miniLayout(layout.cols, layout.rows, aspect)}`
    + `<span class="layout__label">${layout.cols} × ${layout.rows}</span>`
    + `<span class="layout__sub">가로 ${layout.cols}칸 · 세로 ${layout.rows}줄</span></button>`).join("");
}

export function splitDock(layout) {
  if (!layout) return '<button type="button" class="btn btn--primary btn--block" disabled>조각 수를 골라 주세요</button>';
  return `<div class="ask"><b>이대로 분할할까요?</b><span>${layout.cols} × ${layout.rows} · ${layout.count}조각</span></div>`
    + '<div class="btnrow"><button type="button" class="btn btn--secondary" data-action="split-no">아니요</button>'
    + '<button type="button" class="btn btn--primary" data-action="split-yes">네, 분할할게요</button></div>';
}

/** The line under the preview.  `plan` is the engine's plan for this layout once it has answered. */
export function splitCaption(layout, plan) {
  if (!layout) return "조각 수를 고르면 나누는 선이 보여요";
  const size = plan ? px(plan.out_w, plan.out_h) : px(layout.out_w, layout.out_h);
  return `조각마다 ${size}로 키워 드려요${plan && plan.capped ? " · 원본이 아주 커서 조각 크기를 줄였어요" : ""}`;
}

// ---------------------------------------------------------------------------
// 6 · Making the files
// ---------------------------------------------------------------------------
/** @param {{source: object, layout: object, format: string, image: string}} view */
export function workScreen({ source, layout, format, image }) {
  const count = layout.count;
  return `<section class="screen work"><h1 class="h1" tabindex="-1">${formatCopy(format).making(count)}</h1>`
    + '<p class="lead">화면을 켜 둔 채로 잠시만 기다려 주세요.</p>'
    + `<div class="work__stage"><figure class="stage" data-css="${stageCss(source)}"><div class="stage__fit">${image}`
    + `${veil(layout.cols, layout.rows)}${gridLines(layout.cols, layout.rows)}</div></figure></div>`
    + `<div class="work__panel"><div class="prog"><div class="bar" id="work-bar" role="progressbar" aria-valuemin="0" aria-valuemax="${count}" aria-valuenow="0"><i></i></div>`
    + '<div class="prog__row" aria-live="polite" aria-atomic="true"><span id="work-status">조각 만드는 중</span>'
    + `<span id="work-figure">0 / ${count}</span></div></div><div data-dock-slot></div></div></section>`;
}

// ---------------------------------------------------------------------------
// 7 · Result
// ---------------------------------------------------------------------------
/**
 * @param {{source: object, layout: object, plan: object, formats: object[]}} view
 *        plan: out_w / out_h of the pieces; formats: the engine's list ({ key, label, kind })
 */
export function resultScreen({ source, layout, plan, formats }) {
  const count = layout.count;
  const tileAspect = (source.width / layout.cols) / (source.height / layout.rows);
  const cards = formats.map((format) => `<button type="button" class="fmt" role="radio" aria-checked="false" data-action="format" data-format="${format.key}">`
    + `<span class="fmt__t"><b>${esc(format.label)}</b><small>${formatCopy(format.key).desc(count)}</small></span>`
    + `<span class="fmt__radio">${ic("check", "ic--16")}</span></button>`).join("");
  return `<section class="screen result" data-css="${stageCss(source)}">`
    + `<h1 class="h1" tabindex="-1">${count}조각으로 나눴어요</h1>`
    + `<p class="lead num">가로 ${layout.cols} × 세로 ${layout.rows} · 각 ${px(plan.out_w, plan.out_h)}</p>`
    + `<div class="result__stage">${mosaic(layout.cols, layout.rows, tileAspect, `나눈 조각 ${count}개, 가로 ${layout.cols}칸 세로 ${layout.rows}줄`)}</div>`
    + '<div class="result__panel"><h2 class="h2">어떤 파일로 저장할까요?</h2>'
    + `<div class="fmts" role="radiogroup" aria-label="파일 형식">${cards}</div>`
    + '<p class="cap help" id="format-help"></p>'
    + '<div class="opt" id="paper-option"><span class="label" id="paper-label">용지 크기</span><div class="seg" role="radiogroup" aria-labelledby="paper-label">'
    + '<button type="button" role="radio" data-action="paper" data-paper="original">원본 비율</button>'
    + '<button type="button" role="radio" data-action="paper" data-paper="a4">A4</button></div></div>'
    + '<button type="button" class="check" id="zip-option" role="checkbox" aria-checked="false" data-action="zip-toggle" hidden>'
    + `<span class="check__box">${ic("check", "ic--16")}</span><span class="check__t"><b>ZIP 파일 하나로 받기</b></span></button>`
    + '<div id="result-problem"></div>'
    + '<button type="button" class="textbtn result__more" data-action="resplit">다른 조각 수로 다시</button>'
    + "<div data-dock-slot></div></div></section>";
}

/** @param {{ios: boolean, share: boolean}} options  share: Android's extra share button */
export function resultDock({ ios, share }) {
  const hint = ios ? '<p class="cap dock__hint">공유 창에서 ‘파일에 저장’이나 ‘이미지 저장’을 고르면 돼요</p>' : "";
  if (share) {
    return `${hint}<div class="btnrow"><button type="button" class="btn btn--secondary" data-action="share">${ic("share", "ic--20")}공유</button>`
      + '<button type="button" class="btn btn--primary" data-action="save">저장하기</button></div>';
  }
  return `${hint}<button type="button" class="btn btn--primary btn--block" data-action="save">저장하기</button>`;
}

/** A failed export, shown under the options with a way to try again. */
export const problemNotice = (text) => `<div class="notice notice--action">${ic("alert")}<span class="notice__t"><b>${esc(text)}</b></span>`
  + '<button type="button" class="textbtn textbtn--brand" data-action="retry">다시 시도</button></div>';

// ---------------------------------------------------------------------------
// 8 · Errors
// ---------------------------------------------------------------------------
export const WRONG_PASSWORD_TEXT = "암호가 맞지 않아요. 대소문자를 확인해 주세요.";

/**
 * @param {{code: string, spec: object, file: File | null, typed: string, notices: string}} view
 *        spec: from copy.js errorView(); typed: a password that was just refused (stays in the field)
 */
export function errorScreen({ code, spec, file, typed, notices: noticeHtml }) {
  const wrong = code === "WRONG_PASSWORD";
  const field = spec.field
    ? `<label class="field${wrong ? " is-error" : ""}" id="password-field"><span class="field__label">문서 암호</span><span class="field__box">`
      + `<input id="password" type="password" value="${esc(typed)}" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="암호 입력" aria-describedby="password-message">`
      + `<button type="button" class="iconbtn" data-action="password-eye" aria-label="암호 보기">${ic("eye")}</button></span>`
      + `<span class="field__msg" id="password-message" role="alert">${wrong ? WRONG_PASSWORD_TEXT : ""}</span></label>`
    : "";
  const named = spec.showFile && file ? fileLine(fileBadge(file.name), file.name, formatBytes(file.size)) : "";
  return `<section class="screen error" data-error-code="${esc(code)}"><div class="error__icon${spec.danger ? " error__icon--danger" : ""}">${ic(spec.icon)}</div>`
    + `<h1 class="h1" tabindex="-1">${spec.title}</h1><p class="lead">${spec.lead}</p>${named}${field}${noticeHtml}`
    + "<div data-dock-slot></div></section>";
}

export function errorDock(spec, typed = "") {
  const blocked = spec.field && !typed;  // "open the document" needs a password first
  return `<button type="button" class="btn btn--primary btn--block" data-action="${spec.primary.action}"${blocked ? " disabled" : ""}>${spec.primary.label}</button>`
    + (spec.ghost ? `<button type="button" class="btn btn--ghost" data-action="${spec.ghost.action}">${spec.ghost.label}</button>` : "");
}
