// Words: output formats, error screens, paper names.
// Rule kept throughout: a file name is always followed by "파일" (or 에/에서), so no particle
// ever has to agree with the last letter of an extension.

import { esc } from "./ui.js";

// ---------------------------------------------------------------------------
// Output formats (the list and order come from the engine; this adds the words)
// ---------------------------------------------------------------------------
export const FORMAT_COPY = {
  pdf: {
    desc: (n) => `한 파일에 ${n}쪽`,
    help: () => "인쇄할 때 가장 편해요. 한 쪽에 조각이 하나씩 들어가요.",
    making: () => "PDF 파일을\n만들고 있어요",
  },
  jpg: {
    desc: (n) => `이미지 ${n}장`,
    help: ({ zipped }) => (zipped
      ? "용량이 작아 주고받기 좋아요. 여러 장은 ZIP 파일 하나로 묶어 드려요."
      : "용량이 작아 주고받기 좋아요. 조각마다 이미지 한 장이에요."),
    making: (n) => `JPG 이미지 ${n}장을\n만들고 있어요`,
  },
  png: {
    desc: (n) => `이미지 ${n}장`,
    help: ({ zipped }) => (zipped
      ? "화질이 그대로예요. 파일이 크고 시간이 더 걸려요. ZIP 파일 하나로 묶어 드려요."
      : "화질이 그대로예요. 대신 파일이 크고 만드는 데 시간이 더 걸려요."),
    making: (n) => `PNG 이미지 ${n}장을\n만들고 있어요`,
  },
  docx: {
    desc: (n) => `문서 ${n}쪽`,
    help: () => "쪽마다 조각이 하나씩 들어간 Word 문서(.docx)예요.",
    making: () => "Word 문서를\n만들고 있어요",
  },
  pptx: {
    desc: (n) => `슬라이드 ${n}장`,
    help: () => "슬라이드마다 조각이 하나씩 들어간 발표 자료(.pptx)예요.",
    making: () => "PowerPoint 파일을\n만들고 있어요",
  },
  hwpx: {
    desc: (n) => `문서 ${n}쪽`,
    help: () => "쪽마다 조각이 하나씩 들어간 한글 문서(.hwpx)예요.",
    making: () => "한글 문서를\n만들고 있어요",
  },
};

const GENERIC_FORMAT = {
  desc: (n) => `${n}조각`,
  help: () => "",
  making: () => "파일을\n만들고 있어요",
};

export const formatCopy = (key) => FORMAT_COPY[key] || GENERIC_FORMAT;

/** Used until the engine has reported its own list (it always has by the time a format is shown). */
export const FALLBACK_FORMATS = [
  { key: "pdf", label: "PDF", ext: "pdf", kind: "document" },
  { key: "jpg", label: "JPG", ext: "jpg", kind: "images" },
  { key: "png", label: "PNG", ext: "png", kind: "images" },
  { key: "docx", label: "Word", ext: "docx", kind: "document" },
  { key: "pptx", label: "PowerPoint", ext: "pptx", kind: "document" },
  { key: "hwpx", label: "한글", ext: "hwpx", kind: "document" },
];

export const FALLBACK_COUNTS = [2, 4, 6, 8, 9, 12, 16, 18];

// ---------------------------------------------------------------------------
// Paper sizes (PDF pages carry a physical size)
// ---------------------------------------------------------------------------
const PAPERS = [
  ["A3", 297, 420], ["A4", 210, 297], ["A5", 148, 210],
  ["B4", 257, 364], ["B5", 182, 257],
  ["Letter", 216, 279], ["Legal", 216, 356],
];

/** "A4 · 210 × 297 mm" for a known sheet, "150 × 100 mm" otherwise. */
export function paperText(physicalMm) {
  if (!physicalMm || physicalMm.length !== 2) return "";
  const width = Math.round(physicalMm[0]);
  const height = Math.round(physicalMm[1]);
  if (!(width > 0 && height > 0)) return "";
  const short = Math.min(width, height);
  const long = Math.max(width, height);
  const hit = PAPERS.find(([, s, l]) => Math.abs(s - short) <= 2 && Math.abs(l - long) <= 2);
  return `${hit ? `${hit[0]} · ` : ""}${width} × ${height} mm`;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------
/** The engine's stock sentence per code.  Anything else it sends is more specific and is shown as it is. */
const STOCK = {
  UNSUPPORTED: "지원하지 않는 파일 형식이에요.",
  CORRUPT: "파일이 손상되었거나 읽을 수 없어요.",
  EMPTY_FILE: "빈 파일이에요.",
  ENCRYPTED: "암호가 걸린 문서예요.",
  WRONG_PASSWORD: "암호가 맞지 않아요.",
  PROTECTED: "보안(배포용/DRM) 문서라서 이미지를 꺼낼 수 없어요.",
  NO_IMAGE: "문서에서 이미지를 찾지 못했어요.",
  TOO_LARGE: "파일이 너무 커서 이 기기에서 처리할 수 없어요.",
  IMAGE_TOO_LARGE: "이미지 해상도가 너무 커서 처리할 수 없어요.",
  IMAGE_TOO_SMALL: "이미지가 너무 작아서 이만큼 나눌 수 없어요.",
  BAD_LAYOUT: "이 분할 방식은 사용할 수 없어요.",
  BAD_FORMAT: "지원하지 않는 저장 형식이에요.",
  NOT_FOUND: "선택한 이미지를 찾을 수 없어요. 파일을 다시 선택해 주세요.",
  OUT_OF_MEMORY: "메모리가 부족해요. 더 작은 이미지나 적은 분할 수로 시도해 주세요.",
  INTERNAL: "처리 중 문제가 생겼어요.",
};

/** A sentence from the engine that says more than its stock line for that code, or "". */
export function specificMessage(code, message) {
  const text = String(message || "").trim();
  if (!text || text === code || text === STOCK[code]) return "";
  return text;
}

const PICK = { label: "다른 파일 고르기", action: "pick" };
const RETRY_SAME = { label: "같은 파일로 다시 시도", action: "retry" };
const RETRY = { label: "다시 시도", action: "retry" };

const file = (name) => (name ? `<b>${esc(name)}</b> 파일` : "이 파일");

const ERRORS = {
  UNSUPPORTED: {
    icon: "file-x",
    title: "이 파일은\n열 수 없어요",
    body: (name) => `${file(name)}은 지원하지 않는 형식이에요. 사진(JPG, PNG)이나 PDF, 한글, Word, PowerPoint 파일을 골라 주세요.`,
    primary: PICK,
  },
  CORRUPT: {
    icon: "alert",
    danger: true,
    title: "파일이 손상된 것\n같아요",
    body: (name) => `${file(name)}을 끝까지 읽지 못했어요. 파일을 다시 내려받거나, 원래 프로그램에서 새로 저장한 뒤 시도해 주세요.`,
    primary: PICK,
    ghost: RETRY_SAME,
  },
  EMPTY_FILE: {
    icon: "file-x",
    title: "내용이 없는\n파일이에요",
    body: (name) => `${file(name)}에는 아무것도 들어 있지 않아요. 파일이 제대로 저장됐는지 확인한 뒤 다시 골라 주세요.`,
    primary: PICK,
  },
  ENCRYPTED: {
    icon: "lock",
    title: "암호가 걸린\n문서예요",
    body: (name) => `${file(name)}을 열려면 암호가 필요해요. 입력한 암호는 이 기기 밖으로 나가지 않아요.`,
    field: true,
    primary: { label: "문서 열기", action: "unlock" },
    ghost: PICK,
  },
  PROTECTED: {
    icon: "lock",
    title: "보안이 걸린\n문서예요",
    body: (name) => `${file(name)}은 보안이 걸려 있어서 이미지를 꺼낼 수 없어요. 원래 프로그램에서 보안을 풀고 다시 저장한 뒤 골라 주세요.`,
    primary: PICK,
  },
  NO_IMAGE: {
    icon: "image-off",
    title: "이미지를\n찾지 못했어요",
    body: (name) => `${file(name)}에는 글만 들어 있어요. 이미지가 들어 있는 문서나 사진을 골라 주세요.`,
    primary: PICK,
  },
  TOO_LARGE: {
    icon: "file-x",
    title: "파일이\n너무 커요",
    body: (name) => `${file(name)}은 300MB를 넘어서 이 기기에서 열 수 없어요. 더 작은 파일을 골라 주세요.`,
    primary: PICK,
  },
  IMAGE_TOO_LARGE: {
    icon: "file-x",
    title: "이미지가\n너무 커요",
    body: (name) => `${file(name)}은 해상도가 너무 높아서 이 기기에서 열 수 없어요. 크기를 줄여서 다시 골라 주세요.`,
    primary: PICK,
  },
  OUT_OF_MEMORY: {
    icon: "alert",
    title: "메모리가\n부족해요",
    body: (name) => `${file(name)}을 여는 데 필요한 메모리가 모자라요. 다른 앱이나 탭을 닫고 다시 시도해 주세요.`,
    primary: RETRY,
    ghost: PICK,
  },
  UNREADABLE: {
    icon: "file-x",
    title: "파일을\n읽지 못했어요",
    body: (name) => `${file(name)}을 가져오지 못했어요. 파일이 옮겨졌거나 지워졌을 수 있어요. 다시 골라 주세요.`,
    primary: PICK,
  },
  NO_WASM: {
    icon: "alert",
    title: "이 브라우저에서는\n쓸 수 없어요",
    body: () => "이미지를 나누는 도구가 이 브라우저에서는 동작하지 않아요. 최신 Chrome, Safari, 삼성 인터넷으로 열어 주세요.",
    primary: { label: "주소 복사하기", action: "copy-url" },
  },
  ENGINE_CRASHED: {
    icon: "alert",
    title: "잠시 문제가\n생겼어요",
    body: () => "이미지를 나누는 도구가 멈췄어요. 다시 시도하면 대부분 해결돼요.",
    primary: RETRY,
    ghost: PICK,
  },
  OFFLINE: {
    icon: "wifi-off",
    title: "분할 도구를\n내려받지 못했어요",
    body: () => "인터넷 연결을 확인한 뒤 다시 시도해 주세요. 한 번 받아 두면 다음부터는 연결이 없어도 돼요.",
    primary: RETRY,
  },
  INTERNAL: {
    icon: "alert",
    title: "문제가\n생겼어요",
    body: (name) => `${file(name)}을 처리하다가 문제가 생겼어요. 다시 시도해도 같으면 다른 파일로 해 보세요.`,
    primary: RETRY,
    ghost: PICK,
  },
};
ERRORS.WRONG_PASSWORD = ERRORS.ENCRYPTED;
ERRORS.NOT_FOUND = ERRORS.INTERNAL;

/**
 * Everything the error screen shows for a code.
 * @param {string} code      engine code (or UNREADABLE / OFFLINE from the app itself)
 * @param {{name?: string, message?: string}} detail
 */
export function errorView(code, { name = "", message = "" } = {}) {
  const spec = ERRORS[code] || ERRORS.INTERNAL;
  const specific = spec.field ? "" : specificMessage(code, message);
  return {
    icon: spec.icon,
    danger: Boolean(spec.danger),
    title: spec.title,
    // a specific sentence from the engine replaces the generic body; the file is then named on its own line
    lead: specific ? esc(specific) : spec.body(name),
    showFile: Boolean(specific && name),
    field: Boolean(spec.field),
    primary: spec.primary,
    ghost: spec.ghost || null,
  };
}

/** Short reason used in the "some files could not be opened" notice. */
const REASONS = {
  UNSUPPORTED: "지원하지 않는 형식",
  CORRUPT: "손상된 파일",
  EMPTY_FILE: "내용이 없는 파일",
  ENCRYPTED: "암호가 걸린 문서",
  WRONG_PASSWORD: "암호가 걸린 문서",
  PROTECTED: "보안이 걸린 문서",
  NO_IMAGE: "이미지가 없는 문서",
  TOO_LARGE: "너무 큰 파일",
  IMAGE_TOO_LARGE: "해상도가 너무 높은 이미지",
  OUT_OF_MEMORY: "메모리 부족",
  UNREADABLE: "읽을 수 없는 파일",
};
export const failureReason = (code) => REASONS[code] || "열 수 없는 파일";

/** One line for a problem while making the output files (shown on the result screen). */
export function exportProblem(code, message, formatLabel) {
  if (code === "OUT_OF_MEMORY") return "메모리가 부족해서 만들지 못했어요. 조각 수를 줄이거나 JPG로 저장해 보세요.";
  if (code === "ENGINE_CRASHED") return "분할 도구가 멈춰서 만들지 못했어요. 다시 시도해 주세요.";
  if (code === "IMAGE_TOO_SMALL") return "이미지가 너무 작아서 이만큼 나눌 수 없어요. 조각 수를 줄여 주세요.";
  const specific = specificMessage(code, message);
  if (specific) return specific;
  return `${formatLabel} 파일을 만들지 못했어요. 다시 시도하거나 다른 형식을 골라 주세요.`;
}
