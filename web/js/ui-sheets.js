// Contents of the bottom sheets.  Each function returns { name, title, body, foot }
// for openSheet(); nothing here touches state.

import { esc, formatBytes, ic } from "./ui.js";
import { APK_URL, isAndroid, isIOS } from "./platform.js";
import { totalBytes } from "./save.js";

const step = (n, title, sub = "") => `<li><span class="howto__n">${n}</span><div class="howto__t"><b>${title}</b>${sub ? `<small>${sub}</small>` : ""}</div></li>`;
const key = (name) => `<span class="key">${ic(name, "ic--16")}</span>`;
const steps = (items) => `<ol class="howto">${items.map(([title, sub], index) => step(index + 1, title, sub)).join("")}</ol>`;

/** Android only: the photo picker and the document picker are two different system screens. */
export function pickerSheet() {
  const row = (action, icon, title, sub) => `<button type="button" class="row" data-action="${action}">`
    + `<span class="fbadge">${ic(icon, "ic--20")}</span><span class="row__t"><b>${title}</b><small>${sub}</small></span>${ic("chev-r", "ic--20")}</button>`;
  return {
    name: "picker",
    title: "어디에서\n가져올까요?",
    body: row("pick-photos", "image", "사진 보관함", "갤러리에 있는 사진과 화면 캡처")
      + row("pick-files", "folder", "파일", "PDF, 한글, Word, PowerPoint 문서"),
  };
}

/**
 * Add to Home Screen steps for iPhone and iPad.
 * @param {{inSafari: boolean, modern: boolean, ipad: boolean}} options  modern = Safari 26 or later (Share sits
 *        behind "..."); ipad = the toolbar with Share is at the top
 */
export function installSheetIOS({ inSafari, modern, ipad = false }) {
  const items = [];
  if (!inSafari) items.push(["Safari에서 이 페이지를 열어요", "다른 브라우저나 앱 안에서는 추가하지 못할 수 있어요."]);
  if (ipad) {
    items.push([`위쪽 ${key("share")} 공유 버튼을 눌러요`, `버튼이 안 보이면 ${key("more")}를 먼저 눌러 보세요.`]);
  } else if (modern) {
    items.push([`아래쪽 ${key("more")} 다음에 ${key("share")} 공유를 눌러요`, "공유 버튼이 바로 보이면 그것만 눌러도 돼요."]);
  } else {
    items.push([`아래쪽 ${key("share")} 공유 버튼을 눌러요`, "버튼이 안 보이면 화면을 살짝 내려 보세요."]);
  }
  items.push(["‘홈 화면에 추가’를 골라요", `${key("plus-box")} 표시가 있는 줄이에요. 목록을 조금 내리면 보여요.`]);
  items.push(["오른쪽 위 ‘추가’를 누르면 끝이에요", modern ? "‘웹 앱으로 열기’는 켜 둔 채로 두세요." : "홈 화면에 조각조각 아이콘이 생겨요."]);
  return {
    name: "install",
    title: "홈 화면에 추가하면\n앱처럼 바로 열려요",
    body: steps(items),
    foot: '<button type="button" class="btn btn--secondary btn--block" data-action="close-sheet">확인했어요</button>',
  };
}

/** @param {{canPrompt: boolean}} options  canPrompt = the browser handed us its install prompt */
export function installSheetAndroid({ canPrompt }) {
  const items = canPrompt
    ? [
      ["아래 ‘앱 설치하기’를 눌러요", "누르면 설치 창이 떠요."],
      ["설치 창에서 ‘설치’를 눌러요", "홈 화면과 앱 서랍에 아이콘이 생겨요."],
      [`창이 안 뜨면 ${key("more-v")} 메뉴를 열어요`, "‘홈 화면에 추가’나 ‘앱 설치’를 고르면 돼요."],
    ]
    : [
      [`브라우저의 ${key("more-v")} 메뉴를 열어요`, "Chrome은 오른쪽 위, 삼성 인터넷은 오른쪽 아래에 있어요."],
      ["‘홈 화면에 추가’나 ‘앱 설치’를 골라요", "브라우저마다 이름이 조금 달라요."],
      ["‘설치’를 누르면 끝이에요", "홈 화면과 앱 서랍에 아이콘이 생겨요."],
    ];
  const primary = canPrompt
    ? '<button type="button" class="btn btn--primary btn--block" data-action="install-now">앱 설치하기</button>'
    : '<button type="button" class="btn btn--secondary btn--block" data-action="close-sheet">확인했어요</button>';
  return {
    name: "install",
    title: "홈 화면에 추가하면\n앱처럼 바로 열려요",
    body: steps(items),
    foot: primary + `<a class="btn btn--ghost" href="${APK_URL}" target="_blank" rel="noopener" data-action="apk">설치 파일(APK)로 받기</a>`,
  };
}

/** Shown inside KakaoTalk, Naver and other in-app browsers, before anything heavy starts. */
export function inAppSheet(inApp) {
  const where = inApp.key === "webview" ? "다른 앱" : inApp.name;
  const items = isIOS
    ? [
      [`화면 아래쪽 ${key("share")} 또는 ${key("more")} 버튼을 눌러요`, "앱마다 자리가 조금 달라요."],
      ["‘Safari로 열기’를 골라요", "‘다른 브라우저로 열기’라고 나오기도 해요."],
    ]
    : [
      [`화면 위나 아래의 ${key("more-v")} 메뉴를 눌러요`, `${key("share")} 모양일 때도 있어요.`],
      ["‘다른 브라우저로 열기’를 골라요", "Chrome이나 삼성 인터넷을 고르면 돼요."],
    ];
  const target = isIOS ? "Safari로 열기" : isAndroid ? "Chrome으로 열기" : "다른 브라우저로 열기";
  return {
    name: "inapp",
    title: "Safari나 Chrome으로\n열어 주세요",
    body: `<p class="lead">지금은 ${esc(where)} 안에서 열려 있어요. 여기서는 파일 저장과 앱 설치가 막혀 있어요.</p>`
      + '<p class="cap">아래 버튼이 안 되면 이렇게 해 보세요.</p>' + steps(items),
    foot: `<button type="button" class="btn btn--primary btn--block" data-action="open-external">${ic("external", "ic--20")}${target}</button>`
      + '<div class="btnrow"><button type="button" class="btn btn--ghost" data-action="copy-url">주소 복사하기</button>'
      + '<button type="button" class="btn btn--ghost" data-action="close-sheet">이대로 계속할게요</button></div>',
  };
}

/**
 * "The files are ready" - the one tap that actually hands them over.
 * (Sharing needs a fresh tap: the one that started the build is too old by the time it finishes.)
 * @param {{files: File[], mode: "share" | "download", intent: "save" | "share", again?: boolean}} options
 */
export function deliverSheet({ files, mode, intent, again = false }) {
  const one = files.length === 1;
  const first = files[0];
  const ext = (first.name.split(".").pop() || "").toUpperCase().slice(0, 4);
  const label = one ? first.name : `이미지 ${files.length}장`;
  const badge = one ? ext : `${files.length}장`;
  let hint = "";
  if (again) hint = "방금은 창을 열지 못했어요. 한 번만 더 눌러 주세요.";
  else if (mode === "share" && isIOS) hint = "공유 창에서 ‘파일에 저장’이나 ‘이미지 저장’을 고르면 돼요.";
  else if (mode === "share") hint = "공유할 앱을 고르는 창이 열려요.";
  const action = intent === "share" ? `${ic("share", "ic--20")}공유하기` : "저장하기";
  return {
    name: "deliver",
    title: "파일이 준비됐어요",
    body: `<div class="fileline"><span class="fbadge">${esc(badge)}</span><div class="row__t"><b>${esc(label)}</b>`
      + `<small>${formatBytes(totalBytes(files))}</small></div></div>${hint ? `<p class="cap">${hint}</p>` : ""}`,
    foot: `<button type="button" class="btn btn--primary btn--block" data-action="deliver" data-intent="${intent}">${action}</button>`,
  };
}

/** Back was pressed while the files are being made. */
export function cancelSheet() {
  return {
    name: "cancel",
    title: "만들기를\n그만할까요?",
    body: '<p class="lead">지금 그만두면 만들던 조각은 사라져요.</p>',
    foot: '<div class="btnrow"><button type="button" class="btn btn--secondary" data-action="close-sheet">아니요</button>'
      + '<button type="button" class="btn btn--primary" data-action="cancel-confirm">네, 그만할게요</button></div>',
  };
}
