// Getting finished files out of the browser.
//
// The rules (they differ per platform and are easy to get wrong):
//  * iOS: navigator.share({ files }) only.  In a home-screen app a download link opens a dead-end
//    preview (WebKit bug 290847), so it is never used there; a Safari tab may fall back to it.
//  * Android / desktop: <a download> with a blob URL, one file per tap; picture sets travel as one ZIP.
//  * Android sharing is an extra, and only for types Chromium's share sheet accepts.
//  * share() needs the tap's user activation and uses it up, so everything here is synchronous:
//    the File objects must exist before the tap.

import { makeZip } from "./zip.js";
import { isIOS, isStandalone } from "./platform.js";

/** Chromium's canShare() answers true for anything; share() then rejects what is not on its allow-list. */
const ANDROID_SHARE_TYPES = /\.(pdf|jpe?g|png)$/i;
const ANDROID_SHARE_MAX_FILES = 10;
const ANDROID_SHARE_MAX_BYTES = 50 * 1024 * 1024;

/** Download URLs must outlive the click: the browser reads the blob after the handler returns. */
const REVOKE_AFTER_MS = 90_000;

export const totalBytes = (files) => files.reduce((sum, file) => sum + file.size, 0);

/** Engine output ({ name, mime, blob }) -> File objects with the engine's own MIME types. */
export function toFiles(outputs) {
  return outputs.map((output) => new File([output.blob], output.name, { type: output.mime }));
}

/** One ZIP holding the given files (stored, not compressed: pictures are compressed already). */
export async function zipFiles(files, name) {
  const blob = await makeZip(files.map((file) => ({ name: file.name, blob: file })));
  return new File([blob], name, { type: "application/zip" });
}

export const hasShare = () => typeof navigator.share === "function";

function canShare(files) {
  if (!hasShare()) return false;
  if (typeof navigator.canShare !== "function") return true;
  try {
    return navigator.canShare({ files });
  } catch (error) {
    return false;
  }
}

/** May these files go through Android's share sheet?  (type allow-list, 10 files, 50 MB) */
export function androidShareable(files) {
  return files.length > 0
    && files.length <= ANDROID_SHARE_MAX_FILES
    && files.every((file) => ANDROID_SHARE_TYPES.test(file.name))
    && totalBytes(files) <= ANDROID_SHARE_MAX_BYTES
    && canShare(files);
}

/** The same question before anything is built: only the format and the number of files are known. */
export function androidShareLikely(format, fileCount) {
  if (!hasShare() || !ANDROID_SHARE_TYPES.test("x." + format) || fileCount > ANDROID_SHARE_MAX_FILES) return false;
  // some browsers have share() but cannot share files at all (Firefox for Android)
  try {
    return canShare([new File([""], "x.pdf", { type: "application/pdf" })]);
  } catch (error) {
    return false;
  }
}

/** Does this delivery need the ZIP (so it has to be built before the tap that delivers)? */
export function needsZip({ kind, count, zipPreferred, intent }) {
  if (kind !== "images" || count < 2) return false;
  if (isIOS) return zipPreferred || !hasShare();
  return intent !== "share";
}

/**
 * What a tap on save / share will do with the built files.
 * @returns {{mode: "share" | "download" | "none", files: File[]}}
 *   "none": nothing safe is available (an iOS home-screen app without share)
 */
export function deliveryFor({ intent, files, zip, kind, zipPreferred }) {
  const set = kind === "images" && files.length > 1;
  if (isIOS) {
    const payload = set && zipPreferred && zip ? [zip] : files;
    if (canShare(payload)) return { mode: "share", files: payload };
    const single = set ? zip : files[0];
    if (isStandalone || !single) return { mode: "none", files: payload };
    return { mode: "download", files: [single] };
  }
  if (intent === "share") return { mode: "share", files };
  return { mode: "download", files: [set ? zip : files[0]] };
}

/** Save one file through a download link.  Call inside the user's tap. */
export function downloadFile(file) {
  const url = URL.createObjectURL(file);
  const link = document.createElement("a");
  link.href = url;
  link.download = file.name;
  link.rel = "noopener";
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS);
}

/**
 * Open the system share sheet with files only (title/text next to files breaks sharing in WebKit).
 * Must be called synchronously inside the user's tap - no await before it.
 * Resolves to "shared", "cancelled" (the user closed the sheet), "blocked" (the tap's activation
 * was gone: ask for another tap) or "failed".
 */
export function shareFiles(files) {
  const classify = (error) => {
    const name = error && error.name;
    if (name === "AbortError") return "cancelled";
    if (name === "NotAllowedError") return "blocked";
    return "failed";
  };
  let pending;
  try {
    pending = navigator.share({ files });
  } catch (error) {
    return Promise.resolve(classify(error));
  }
  return Promise.resolve(pending).then(() => "shared", classify);
}
