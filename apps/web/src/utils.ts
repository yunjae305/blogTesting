/** Ported from apps/web/public/app.js. */

import { MAX_REFERENCE_MATERIALS } from "./constants";
import type { FinalPost, GeneratedPostImage, ReferenceMaterial, ReferenceMaterialType } from "./api/types";

// 서버의 MAX_FILE_BYTES·MAX_TOTAL_FILE_BYTES와 **같은 값이어야 한다**
// (blog_task/validation.py). 어긋나면 화면은 통과시키고 서버가 거부한다.
export const MAX_REFERENCE_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_REFERENCE_FILES_BYTES = 10 * 1024 * 1024;
const MAX_REFERENCE_TEXT_CHARS = 16_000;

export function formatDate(iso?: string): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
}

/* ------------------------------------------------------------ reference files */

export function referenceTypeForFile(file: File): ReferenceMaterialType | null {
  const name = file.name.toLowerCase();
  if (file.type === "text/plain" || name.endsWith(".txt")) return "TEXT";
  if (file.type === "application/pdf" || name.endsWith(".pdf")) return "PDF";
  // 이미지는 형식을 가리지 않고 모두 받는다. 브라우저가 아는 image/* 이거나 흔한 이미지
  // 확장자면 IMAGE로 본다 — Anthropic이 못 받는 형식은 서버가 전송 직전 PNG로 변환한다.
  if (
    file.type.startsWith("image/") ||
    /\.(png|jpe?g|gif|webp|bmp|tiff?|avif|heic|heif|ico|svg)$/.test(name)
  ) {
    return "IMAGE";
  }
  return null;
}

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("파일을 읽을 수 없습니다."));
    reader.readAsDataURL(file);
  });
}

async function referenceFromFile(file: File): Promise<ReferenceMaterial | null> {
  const type = referenceTypeForFile(file);
  if (!type) return null;
  if (file.size > MAX_REFERENCE_FILE_BYTES) {
    throw new Error(`파일 하나는 최대 ${MAX_REFERENCE_FILE_BYTES / 1024 / 1024}MB까지 올릴 수 있습니다.`);
  }

  if (type === "TEXT") {
    const content = await file.text();
    return { type, name: file.name, value: content.slice(0, MAX_REFERENCE_TEXT_CHARS) };
  }
  return { type, name: file.name, value: await fileToDataUrl(file) };
}

/**
 * 참고 URL이 쓸 수 있는 주소인가. 쓸 수 있으면 null, 아니면 **왜 안 되는지** 한 줄.
 *
 * 예전에는 제출할 때 한 번만 보고 "http 또는 https로 시작해야 합니다"라고 알렸다.
 * 다 적고 다음을 누른 뒤에야 알게 되고, 어느 칸이 문제인지도 말해 주지 않았다
 * (2026-08-07 사용자 요청: 입력할 때 확인하고, 유효하지 않으면 다음으로 넘어가지 않게).
 *
 * **닿는 주소인지는 여기서 알 수 없다.** 브라우저가 남의 사이트를 직접 불러 볼 수
 * 없기 때문이다(CORS). 여기서 보는 것은 '주소로 성립하는가'다.
 */
export function referenceUrlProblem(value: string): string | null {
  const url = value.trim();
  if (!url) return null; // 빈 칸은 아직 안 적은 것이지 잘못 적은 것이 아니다.

  if (!/^https?:\/\//i.test(url)) {
    return "http:// 또는 https:// 로 시작해야 합니다.";
  }
  // 공백을 먼저 본다. new URL이 먼저 터지면 '형식이 올바르지 않습니다'만 나와,
  // 붙여넣다 공백이 섞인 흔한 경우에 무엇을 고쳐야 할지 알 수 없다.
  if (/\s/.test(url)) {
    return "주소에 공백이 들어 있습니다.";
  }
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "주소 형식이 올바르지 않습니다.";
  }
  if (parsed.username || parsed.password) {
    return "아이디나 비밀번호가 포함된 주소는 사용할 수 없습니다.";
  }
  if (url.includes("\\")) {
    return "주소 형식이 올바르지 않습니다.";
  }
  // 점이 없는 호스트(localhost, 사내 주소)는 글의 근거로 쓸 수 없다 — 읽는 사람이
  // 열어 볼 수 없는 주소다.
  const hostname = parsed.hostname.toLowerCase();
  const isIpLiteral = /^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostname) || hostname.includes(":");
  if (
    !hostname.includes(".") ||
    hostname.endsWith(".") ||
    hostname.endsWith(".localhost") ||
    hostname.endsWith(".local") ||
    hostname.endsWith(".internal") ||
    isIpLiteral
  ) {
    return "누구나 열 수 있는 주소가 아닙니다(예: https://example.com/page).";
  }
  const sensitiveKeys = new Set([
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "code",
    "credential",
    "id_token",
    "key",
    "passwd",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
  ]);
  for (const parameters of [parsed.searchParams, new URLSearchParams(parsed.hash.slice(1))]) {
    for (const key of parameters.keys()) {
      const normalizedKey = key.trim().toLowerCase().replaceAll("-", "_");
      if (
        sensitiveKeys.has(normalizedKey) ||
        ["_token", "_secret", "_signature", "_key"].some((suffix) =>
          normalizedKey.endsWith(suffix),
        )
      ) {
        return "로그인 토큰이나 서명이 포함된 주소는 사용할 수 없습니다. PDF 또는 텍스트로 첨부해 주세요.";
      }
    }
  }
  return null;
}

export async function collectReferenceMaterials(input: {
  /** 메모 입력. 탭 UI는 여러 메모를 각각의 참고자료로 쌓지만, 예전 단일 문자열 호출도
      그대로 받는다. */
  text: string | string[];
  /** 참고 URL. 여러 개를 받는다 — 공식 사이트·요금제·기능 페이지를 함께 근거로 삼는 것이
      실제 사용 방식이고, 백엔드는 처음부터 URL 자료를 여러 개 저장할 수 있었다.
      문자열 하나를 넘기던 옛 호출도 그대로 받는다. */
  url: string | string[];
  files: File[];
  /** 이미 저장돼 있어 이 호출이 만드는 목록에 안 잡히는 자료 수(수정 화면의 keptFiles).
      이걸 빼고 새로 고른 것만 10개 이하인지 봤더니, 이미 8개 저장된 글에 5개를 더
      올려도 통과해서 총합이 10개를 넘겼다 — 총합 기준으로 봐야 한다. */
  existingCount?: number;
}): Promise<ReferenceMaterial[]> {
  const materials: ReferenceMaterial[] = [];

  const totalFileBytes = input.files.reduce((total, file) => total + file.size, 0);
  if (totalFileBytes > MAX_REFERENCE_FILES_BYTES) {
    throw new Error(`첨부 파일 합계는 최대 ${MAX_REFERENCE_FILES_BYTES / 1024 / 1024}MB입니다.`);
  }

  const texts = (Array.isArray(input.text) ? input.text : [input.text])
    .map((value) => value.trim())
    .filter(Boolean);
  for (const text of texts) materials.push({ type: "TEXT", value: text });

  const urls = (Array.isArray(input.url) ? input.url : [input.url])
    .map((value) => value.trim())
    .filter(Boolean);
  const seenUrls = new Set<string>();
  for (const url of urls) {
    // 화면과 같은 판정을 쓴다. 두 곳이 어긋나면 화면은 통과인데 저장이 막힌다.
    const problem = referenceUrlProblem(url);
    if (problem) {
      throw new Error(`참고 URL이 올바르지 않습니다 — ${problem}`);
    }
    // 같은 주소를 두 번 저장하면 자료 개수만 차지하고 근거는 늘지 않는다.
    if (seenUrls.has(url)) continue;
    seenUrls.add(url);
    materials.push({ type: "URL", value: url });
  }

  for (const file of input.files) {
    const material = await referenceFromFile(file);
    if (!material) throw new Error("txt, 이미지, pdf 파일만 올릴 수 있습니다.");
    materials.push(material);
  }

  if ((input.existingCount ?? 0) + materials.length > MAX_REFERENCE_MATERIALS) {
    throw new Error(`참고자료는 최대 ${MAX_REFERENCE_MATERIALS}개까지 저장할 수 있습니다.`);
  }
  return materials;
}

/* ------------------------------------------------------------------ clipboard */

function textFromHtml(html: string): string {
  const element = document.createElement("div");
  element.innerHTML = html;
  return element.textContent?.trim() ?? "";
}

/**
 * 네이버에 붙여넣을 글 한 벌: 이미지는 URL로, 해시태그는 본문 끝에.
 *
 * The images go across as URLs, not base64 — 네이버 refuses a pasted data-URL image
 * ("허용되지 않는 형식의 이미지가 있어 해당 이미지는 제외됩니다") because its editor only
 * takes an image it can fetch. The hashtags live in their own array, so copying
 * htmlContent alone left them behind: they showed on screen and then went missing from
 * what was pasted.
 *
 * Both copy buttons — 발행 화면과 글 목록 카드 — go through here, so they cannot drift
 * apart again.
 */
/** 스티커 자리 표식(네이버 발행 전용 내부 표식). 복사본·미리보기에 원문을 노출하지 않는다. */
const STICKER_MARKER = /^\s*\[\[\s*STICKER\s*[:：]\s*(.+?)\s*\]\]\s*$/i;
const STICKER_MARKER_PARAGRAPH = /<p>\s*\[\[\s*STICKER\s*[:：][^\]]*\]\]\s*<\/p>/gi;

function withoutStickerMarkers(value: string): string {
  return value
    .replace(STICKER_MARKER_PARAGRAPH, "")
    .split("\n")
    .filter((line) => !STICKER_MARKER.test(line))
    .join("\n");
}

export function articleHtmlForClipboard(
  post: { htmlContent: string; hashtags?: string[]; images?: { sourceUrl?: string | null }[] },
  postId: string,
): string {
  const html = withoutStickerMarkers(
    stripEditorAttributes(withHostedImages(post.htmlContent, postId, post.images)),
  );
  const tags = (post.hashtags ?? []).filter(Boolean);
  if (!tags.length) return html;
  return `${html}
<p>${tags.map((tag) => `#${tag}`).join(" ")}</p>`;
}

/**
 * 편집기 전용 class·style·data/aria 속성을 걷어낸 발행용 HTML.
 *
 * TipTap/ProseMirror는 편집 중 `class`나 `data-*` 같은 편집 전용 속성을 남긴다. 이걸
 * 그대로 붙여넣으면 네이버·티스토리 에디터에서 불필요한 스타일이 함께 실려 서식이
 * 뒤엉킨다. 링크(href)·이미지(src·alt) 같은 의미 있는 속성만 남긴다.
 *
 * 제목도 여기서 손본다 — `liftTitleToTop` 참고.
 */
function stripEditorAttributes(html: string): string {
  const element = document.createElement("div");
  element.innerHTML = html;
  element.querySelectorAll("*").forEach((node) => {
    node.removeAttribute("class");
    node.removeAttribute("style");
    for (const attr of Array.from(node.attributes)) {
      if (attr.name.startsWith("data-") || attr.name.startsWith("aria-")) {
        node.removeAttribute(attr.name);
      }
    }
  });
  liftTitleToTop(element);
  return element.innerHTML;
}

/**
 * 제목을 글 맨 앞으로 옮기고 `<h2>`로 낮춘다.
 *
 * 두 가지를 한 번에 고친다(둘 다 2026-08-03 실사용에서 확인).
 *
 * 1. **사라짐** — 네이버 스마트에디터는 붙여넣기에서 `<h1>`을 버린다. 복사본에 제목이
 *    들어 있어도 붙여넣으면 제목 줄만 통째로 없어졌다. `<h2>`는 살아남는다(발행된 글
 *    실측에서 `se-fs-fs19` 굵게로 렌더된다). 자동 발행 골격도 같은 이유로 h1을 쓰지
 *    않는다(posting/naver/plan.py).
 * 2. **순서** — 저장된 원고는 표지 이미지(`<figure>`)가 `<article>` 밖 맨 앞에 있고
 *    제목은 `<article>` 안의 첫 요소다. 그대로 붙여넣으면 '표지 → 제목' 순이 되어 제목이
 *    이미지 아래로 밀린다. 제목을 맨 앞으로 끌어올려 '제목 → 표지 → 본문'으로 만든다.
 */
function liftTitleToTop(root: HTMLElement): void {
  const title = root.querySelector("h1");
  if (!title) return;
  const heading = document.createElement("h2");
  heading.innerHTML = title.innerHTML;
  title.remove();
  root.insertBefore(heading, root.firstChild);
}

/** 같은 글의 Markdown 판. 이미지 주소와 해시태그는 HTML 판과 똑같이 따라간다. */
export function articleMarkdownForClipboard(
  post: {
    htmlContent: string;
    hashtags?: string[];
    title: string;
    images?: { sourceUrl?: string | null }[];
  },
  postId: string,
): string {
  const markdown = withoutStickerMarkers(
    markdownFromHtml(withHostedImages(post.htmlContent, postId, post.images), post.title),
  );
  const tags = (post.hashtags ?? []).filter(Boolean);
  if (!tags.length) return markdown;
  return `${markdown}

${tags.map((tag) => `#${tag}`).join(" ")}`;
}

/**
 * Naver and Tistory's editors are WYSIWYG: pasting markup as plain text shows the
 * literal tags. A text/html clipboard entry makes them paste it as rendered HTML.
 *
 * plainText is the text/plain twin non-HTML targets receive. Markdown 복사 passes the
 * markdown here so a markdown editor still gets markdown, while 네이버 — which reads
 * text/html — renders the same article with its images in place. Without the html twin
 * 네이버 showed the markdown literally: "# 제목", "![](...)", and no picture at all.
 */
export async function copyRichHtml(html: string, plainText?: string): Promise<void> {
  if (typeof ClipboardItem !== "undefined" && navigator.clipboard.write) {
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }),
        "text/plain": new Blob([plainText ?? textFromHtml(html)], { type: "text/plain" }),
      }),
    ]);
    return;
  }
  await navigator.clipboard.writeText(plainText ?? html);
}

/**
 * copyRichHtml의 지연판 — 복사할 내용을 아직 **받아오는 중**일 때 쓴다.
 *
 * 클립보드 쓰기 권한은 클릭 직후 몇 초만 유효하다. 상세 문서(이미지 포함 수 MB)를
 * 기다린 **뒤에** 쓰면 브라우저가 "사용자 동작이 아니다"라며 거부한다 — 목록 카드의
 * 원고 복사가 '불러오는 중'만 길게 돌다 실패하던 원인이다(2026-08-10). ClipboardItem은
 * Blob 대신 Promise를 받을 수 있어, 쓰기를 클릭 안에서 먼저 걸고 내용은 도착하는 대로
 * 채운다. 내용 Promise가 거부되면 쓰기 전체가 거부된다(아무것도 복사되지 않는다).
 *
 * 이 경로가 없는 브라우저는 기다렸다 쓰는 예전 방식으로 물러난다 — 그 경우 아주 큰
 * 문서에서는 여전히 거부될 수 있다(브라우저 한계).
 */
export async function copyRichHtmlLazy(
  html: Promise<string>,
  plainText: Promise<string>,
): Promise<void> {
  // 한쪽이 먼저 실패해 다른 쪽 Promise가 버려져도 '처리되지 않은 거부'가 되지 않게
  // 관찰만 걸어 둔다 — 아래 흐름의 실패 처리(쓰기 거부)는 그대로다.
  void html.catch(() => undefined);
  void plainText.catch(() => undefined);
  if (typeof ClipboardItem !== "undefined" && navigator.clipboard.write) {
    await navigator.clipboard.write([
      new ClipboardItem({
        "text/html": html.then((value) => new Blob([value], { type: "text/html" })),
        "text/plain": plainText.then(
          (value) => new Blob([value], { type: "text/plain" }),
        ),
      }),
    ]);
    return;
  }
  await copyRichHtml(await html, await plainText);
}

/* -------------------------------------------------------------------- preview */

/**
 * The preview renders the article the way a blog would: images sitting between
 * the paragraphs they illustrate, not stacked in a gallery above the text.
 *
 * The server already places them — it substitutes each generated image for the
 * `[[IMAGE: ...]]` tag the writer model left behind, inside markdownContent and
 * htmlContent. `body`, however, has the tags *stripped* and no images put back,
 * so a preview built from `body` can only ever be text. That is why images used
 * to appear in a block at the top: they were coming from the `images` array,
 * which carries no position at all.
 */
type PreviewBlock =
  | { kind: "image"; src: string; alt: string }
  | { kind: "caption"; text: string }
  /** level은 마크다운 `##`/`###`의 깊이다. 없으면(옛 경로) 2로 본다. */
  | { kind: "heading"; text: string; level?: number }
  | { kind: "list"; items: string[] }
  | { kind: "table"; header: string[]; rows: string[][] }
  | { kind: "paragraph"; text: string };

/** 굵게 한 조각. 미리보기가 **굵게**를 지우지 않고 그대로 보여 주기 위한 최소 단위다. */
type InlineSegment = { text: string; bold: boolean };

const INLINE_BOLD = /(\*\*|__)(.+?)\1/g;

/**
 * 문단 한 줄을 굵게/보통 조각으로 나눈다.
 *
 * 미리보기는 오랫동안 plainText()로 `**`를 지운 뒤 텍스트만 넘겼다 — 그래서 굵게가
 * 화면에 도달할 방법 자체가 없었다(2026-08-03 실측). 저장 문자열은 그대로 두고
 * 렌더 직전에만 조각으로 나눈다.
 */
export function inlineSegments(text: string): InlineSegment[] {
  const segments: InlineSegment[] = [];
  let cursor = 0;
  for (const match of text.matchAll(INLINE_BOLD)) {
    const index = match.index ?? 0;
    if (index > cursor) segments.push({ text: text.slice(cursor, index), bold: false });
    segments.push({ text: match[2], bold: true });
    cursor = index + match[0].length;
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), bold: false });
  return segments.length ? segments : [{ text, bold: false }];
}

const MARKDOWN_IMAGE = /!\[([^\]]*)]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
const MARKDOWN_HEADING = /^(#{1,6})\s+(.*)$/;
const MARKDOWN_BULLET = /^\s*(?:[-*+]|\d+\.)\s+/;
const MARKDOWN_TABLE_SEPARATOR = /^\s*\|?\s*:?-{2,}[-\s:|]*$/;
/** `| 셀 | 셀 |` 한 줄. 앞머리 파이프까지 요구해 본문 문장을 표로 오인하지 않는다. */
const MARKDOWN_TABLE_ROW = /^\s*\|.*\|?\s*$/;

/**
 * plainText와 같지만 **굵게**만 남긴다 — 화면에서 굵게로 그릴 것이므로 지우면 안 된다.
 * 기울임·형광펜·코드·링크·인용부호는 그대로 걷어낸다(미리보기는 그 서식을 그리지 않는다).
 */
function richText(markdown: string): string {
  return markdown
    .replace(/\[([^\]]*)]\([^)]*\)/g, "$1")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/^\s*>\s?/gm, "")
    .replace(/==([^=\n]+)==/g, "$1")
    .replace(/(?<!\*)\*(?!\*)([^*\n]+)\*(?!\*)/g, "$1")
    .replace(/(?<!_)_(?!_)([^_\n]+)_(?!_)/g, "$1")
    .trim();
}

/** Strips the inline syntax that would otherwise show up as literal characters. */
function plainText(markdown: string): string {
  return markdown
    .replace(/\[([^\]]*)]\([^)]*\)/g, "$1")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/(\*|_)(.*?)\1/g, "$2")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/^\s*>\s?/gm, "")
    .trim();
}

/**
 * 사진 바로 아래 한 줄 이탤릭은 그 사진의 설명이다 — 서버의 image_markdown이 캡션(출처·
 * 기준시점)을 그렇게 쓴다. 일반 문단으로 읽으면 출처가 본문에 섞여 사진과 떨어져 보인다.
 */
const MARKDOWN_CAPTION = /^\*([^*\n]+)\*$/;

/** Splits one markdown block into text runs and the images embedded in it. */
function splitOutImages(block: string): PreviewBlock[] {
  const blocks: PreviewBlock[] = [];
  let cursor = 0;

  for (const match of block.matchAll(MARKDOWN_IMAGE)) {
    const before = block.slice(cursor, match.index).trim();
    if (before) blocks.push(...textBlocks(before));

    blocks.push({ kind: "image", src: match[2], alt: match[1] || "본문 이미지" });
    cursor = match.index + match[0].length;

    // 이미지 뒤에 곧바로 붙은 이탤릭 한 줄만 캡션으로 떼어 낸다. 나머지는 평소대로 본문이다.
    const rest = block.slice(cursor);
    const indent = rest.length - rest.trimStart().length;
    const body = rest.slice(indent);
    const newline = body.indexOf("\n");
    const line = (newline < 0 ? body : body.slice(0, newline)).trim();
    const caption = MARKDOWN_CAPTION.exec(line);
    if (caption) {
      blocks.push({ kind: "caption", text: caption[1].trim() });
      cursor += indent + (newline < 0 ? body.length : newline + 1);
    }
  }

  const rest = block.slice(cursor).trim();
  if (rest) blocks.push(...textBlocks(rest));
  return blocks;
}

/**
 * 마크다운 표를 표로 읽는다. 표 지원이 없던 동안 `| 구분 | 스탠딩석 |`이 글자 그대로
 * 문단에 찍혔다 — 서버는 표를 HTML `<table>`로 바꿔 두는데, 미리보기는 markdownContent를
 * 먼저 읽기 때문에 그 변환을 보지 못했다.
 */
function tableBlock(lines: string[]): PreviewBlock | null {
  if (lines.length < 2) return null;
  if (!lines.every((line) => line.includes("|"))) return null;
  if (!lines.slice(0, 3).some((line) => MARKDOWN_TABLE_SEPARATOR.test(line))) return null;

  const rows = lines
    .filter((line) => !MARKDOWN_TABLE_SEPARATOR.test(line))
    .map((line) =>
      line
        .trim()
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((cell) => plainText(cell.trim())),
    );
  if (rows.length < 2) return null;
  return { kind: "table", header: rows[0], rows: rows.slice(1) };
}

/**
 * 스티커 자리 표식(2026-08-10): 화면에는 이름 칩만 보여 준다 — 실제 스티커는 네이버
 * 발행 때 이 자리에 붙는다. 마커 원문([[STICKER: …]])을 그대로 노출하지 않는다.
 */
function stickerChip(line: string): PreviewBlock | null {
  const sticker = STICKER_MARKER.exec(line);
  if (!sticker) return null;
  return {
    kind: "caption",
    text: `🩵 스티커: ${sticker[1].trim()} (네이버 발행 시 이 자리에 붙어요)`,
  };
}

function textBlocks(block: string): PreviewBlock[] {
  const lines = block.split("\n").filter((line) => line.trim());
  if (!lines.length) return [];

  const table = tableBlock(lines);
  if (table) return [table];

  if (lines.every((line) => MARKDOWN_BULLET.test(line))) {
    return [
      {
        kind: "list",
        items: lines.map((line) => richText(line.replace(MARKDOWN_BULLET, ""))),
      },
    ];
  }

  return lines.flatMap((line): PreviewBlock[] => {
    const heading = MARKDOWN_HEADING.exec(line);
    if (heading) {
      const text = richText(heading[2]);
      // `##`이면 2, `###`이면 3. 레벨을 버리면 미리보기의 소제목 위계가 사라진다.
      return text ? [{ kind: "heading", text, level: heading[1].length }] : [];
    }
    const chip = stickerChip(line);
    if (chip) return [chip];
    const text = richText(line);
    return text ? [{ kind: "paragraph", text }] : [];
  });
}

/**
 * 행 사이에 빈 줄을 넣어 온 표를 한 덩어리로 되돌린다. 문단 분리가 빈 줄이라 그대로 두면
 * 행 하나하나가 별개 문단이 된다. 서버도 같은 규칙으로 합치지만, 이미 저장된 원고의
 * markdownContent에는 빈 줄이 그대로 남아 있다.
 */
function mergeTableBlocks(blocks: string[]): string[] {
  const merged: string[] = [];
  let run: string[] = [];

  const flush = () => {
    if (!run.length) return;
    const lines = run.flatMap((block) => block.split("\n").filter((line) => line.trim()));
    if (run.length > 1 && tableBlock(lines)) merged.push(lines.join("\n"));
    else merged.push(...run);
    run = [];
  };

  for (const block of blocks) {
    const lines = block.split("\n").filter((line) => line.trim());
    if (lines.length && lines.every((line) => MARKDOWN_TABLE_ROW.test(line))) {
      run.push(block);
      continue;
    }
    flush();
    merged.push(block);
  }
  flush();
  return merged;
}

function blocksFromMarkdown(markdown: string, title: string): PreviewBlock[] {
  const blocks = mergeTableBlocks(markdown.split(/\n{2,}/)).flatMap((block) =>
    splitOutImages(block.trim()),
  );

  return withoutTitleHeading(blocks, title);
}

/**
 * 제목과 같은 소제목 하나를 본문에서 뺀다.
 *
 * 미리보기는 제목을 헤더로 따로 그리므로, 본문에 같은 줄이 남아 있으면 제목이 두 번 나온다.
 *
 * 예전에는 **첫 블록만** 봤다. 표지 이미지가 제목보다 앞에 오는 글에서는 제목이 두 번째
 * 블록이라 검사에 걸리지 않았고, 그래서 표지 사진 아래에 제목이 한 번 더 찍혔다
 * (2026-08-03 실사례: 'MBC 놀면 뭐하니…'). 이제 위치와 관계없이 처음 만나는 하나를 뺀다.
 */
function withoutTitleHeading(blocks: PreviewBlock[], title: string): PreviewBlock[] {
  const wanted = title.trim();
  if (!wanted) return blocks;
  const index = blocks.findIndex(
    (block) => block.kind === "heading" && block.text.trim() === wanted,
  );
  return index < 0 ? blocks : [...blocks.slice(0, index), ...blocks.slice(index + 1)];
}

/**
 * 요소의 글자를 읽되 굵게(<strong>/<b>)는 `**`로 표시해 남긴다.
 *
 * textContent로 눌러 담으면 굵게가 평문이 된다 — 마크다운 경로와 HTML 경로가 같은
 * 글을 다르게 보여 주던 원인이다(2026-08-03). 태그를 화면에 넘기지 않고 표식만 남기므로
 * HTML 주입 위험은 없다.
 */
function markedText(node: Element): string {
  const parts: string[] = [];
  for (const child of node.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) {
      parts.push(child.textContent ?? "");
      continue;
    }
    if (!(child instanceof Element)) continue;
    const inner = markedText(child);
    if (!inner) continue;
    parts.push(/^(STRONG|B)$/.test(child.tagName) ? `**${inner}**` : inner);
  }
  return parts.join("").trim();
}

function blocksFromHtml(html: string, title: string): PreviewBlock[] {
  const blocks: PreviewBlock[] = [];
  for (const node of contentBlocks(html)) {
    if (node.tagName === "SCRIPT" || node.tagName === "STYLE") continue;
    const image = node.matches("img") ? node : node.querySelector("img");
    if (image instanceof HTMLImageElement) {
      blocks.push({
        kind: "image",
        src: image.getAttribute("src") ?? "",
        alt: image.getAttribute("alt") || "본문 이미지",
      });
      continue;
    }

    // 서버가 캡션에 붙여 둔 표식. 마크다운에는 class를 담을 수 없어 이쪽에만 있다.
    if (node.classList?.contains("visual-caption")) {
      const text = node.textContent?.trim();
      if (text) blocks.push({ kind: "caption", text });
      continue;
    }

    if (/^H[1-6]$/.test(node.tagName)) {
      const text = markedText(node);
      if (text) {
        blocks.push({ kind: "heading", text, level: Number(node.tagName[1]) });
      }
      continue;
    }

    if (node.tagName === "UL" || node.tagName === "OL") {
      const items = [...node.querySelectorAll("li")].map(markedText).filter(Boolean);
      if (items.length) blocks.push({ kind: "list", items });
      continue;
    }

    // 표를 textContent로 눌러 담으면 셀이 한 줄로 붙어 비교 자체가 사라진다.
    if (node.tagName === "TABLE") {
      const rows = [...node.querySelectorAll("tr")].map((row) =>
        [...row.querySelectorAll("th, td")].map((cell) => cell.textContent?.trim() ?? ""),
      );
      if (rows.length >= 2) {
        blocks.push({ kind: "table", header: rows[0], rows: rows.slice(1) });
        continue;
      }
    }

    const text = markedText(node);
    if (text) blocks.push(stickerChip(text) ?? { kind: "paragraph", text });
  }

  return withoutTitleHeading(
    blocks.filter((block) => block.kind !== "image" || block.src),
    title,
  );
}

/** The article as ordered blocks, with images where the writer put them. */
export function previewBlocks(post: FinalPost): PreviewBlock[] {
  // markdownContent first: it is the field the server substitutes images into,
  // and parsing it needs no HTML injection.
  if (post.markdownContent?.trim()) {
    const blocks = blocksFromMarkdown(post.markdownContent, post.title);
    if (blocks.length) return blocks;
  }

  if (post.htmlContent?.trim()) {
    const blocks = blocksFromHtml(post.htmlContent, post.title);
    if (blocks.length) return blocks;
  }

  // Last resort — a draft with neither. Text only; there is nowhere to place an
  // image from, so the caller falls back to showing the images array.
  return (post.body ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((text) => ({ kind: "paragraph" as const, text }));
}

/* ----------------------------------------------------------------- publishing */

/**
 * Where the pasted images are fetched from. It has to be absolute and it has to be
 * the API — the editor fetching them is on 네이버's page, not ours, so a relative
 * path would resolve against blog.naver.com.
 *
 * VITE_API_BASE is set in development, where the API is on another port. In
 * production the API serves the frontend too, so the page's own origin is it.
 */
const IMAGE_ORIGIN = (import.meta.env.VITE_API_BASE as string) || window.location.origin;

/**
 * The article with its images pointing at real URLs, for pasting.
 *
 * 네이버 refuses a base64 image outright — "허용되지 않는 형식의 이미지가 있어 해당
 * 이미지는 제외됩니다" — because its editor only takes an image it can fetch. Ours are
 * data URLs, since the image model returns bytes rather than a link. So the copy
 * points at GET /posts/{id}/images/{n}, which serves the same bytes over HTTP, and
 * the editor pulls them in like any other image on the web.
 *
 * It also takes the clipboard from 134KB to about 3KB. That mattered: an editor
 * handed three base64 photos worth of markup was dropping the formatting with them.
 */
function withHostedImages(
  html: string,
  postId: string,
  imageMeta?: { sourceUrl?: string | null }[],
): string {
  const element = document.createElement("div");
  element.innerHTML = html;

  const images = element.querySelectorAll("img");
  images.forEach((image, index) => {
    const src = image.getAttribute("src") ?? "";
    if (!src.startsWith("data:")) return;
    // 웹에서 가져온 사진은 **원본 이미지 주소**가 먼저다(2026-08-10). 로컬 서버 주소는
    // 이 PC에서만 열린다 — 네이버가 서버에서 이미지를 끌어갈 때도, 벨로그·티스토리에
    // 붙여넣을 때도 원본 주소라야 그림이 보인다. 원본이 없는 이미지(생성·업로드)는
    // 예전처럼 로컬 엔드포인트다.
    const original = imageMeta?.[index]?.sourceUrl ?? "";
    image.setAttribute(
      "src",
      /^https?:\/\//i.test(original)
        ? original
        : `${IMAGE_ORIGIN}/posts/${postId}/images/${index}`,
    );
  });

  return element.innerHTML;
}

/** Saves the post's images as files, so they can go through 네이버's own uploader. */
export function downloadPostImages(images: GeneratedPostImage[], title: string): number {
  // Characters Windows will not put in a filename.
  const safeTitle = title.replace(/[\\/:*?"<>|]/g, "").slice(0, 40) || "blog-it";

  images.forEach((image, index) => {
    // 확장자는 **실제 값인 dataUrl에서** 읽는다. mimeType은 원고에 적힌 메타데이터라
    // 저장된 바이트와 어긋날 수 있다 — 서버가 이미지를 900px JPEG으로 다시 구우면서
    // 옛 글의 mimeType은 image/png인 채로 남았다(2026-08-06). 그대로 두면 JPEG 파일이
    // .png라는 이름으로 내려가 네이버 업로더에 그 이름 그대로 올라간다.
    const fromDataUrl = /^data:image\/([a-z0-9.+-]+)/i.exec(image.dataUrl ?? "")?.[1];
    const extension = (fromDataUrl ?? image.mimeType?.split("/")[1] ?? "jpg").replace(
      "jpeg",
      "jpg",
    );
    const link = document.createElement("a");
    link.href = image.dataUrl;
    link.download = `${safeTitle}-${index + 1}.${extension}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
  });

  return images.length;
}

/**
 * 본문 블록들을 순회 대상으로 돌려준다.
 *
 * 대표 이미지가 있으면 저장 HTML은 `대표 이미지 + 출처 + <article>본문</article>` 구조다.
 * 최상위 자식이 하나일 때만 article을 벗기면 이 경우 article 전체가 한 블록으로 남고,
 * markdownFromHtml이 그 안의 첫 이미지만 본 뒤 본문을 통째로 건너뛴다. 구조적 래퍼는
 * 최상위 개수와 관계없이 재귀적으로 벗겨 실제 블록을 문서 순서대로 돌려준다.
 */
function contentBlocks(html: string): Element[] {
  const element = document.createElement("div");
  element.innerHTML = html;

  const blocks: Element[] = [];
  const append = (node: Element) => {
    if (/^(ARTICLE|BODY|MAIN)$/.test(node.tagName)) {
      Array.from(node.children).forEach(append);
      return;
    }
    blocks.push(node);
  };
  Array.from(element.children).forEach(append);
  return blocks;
}

/** 인라인 서식(굵게·기울임·링크·코드·줄바꿈)을 마크다운 기호로 옮긴다. */
function inlineMarkdown(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (!(node instanceof HTMLElement)) return node.textContent ?? "";
  const inner = Array.from(node.childNodes).map(inlineMarkdown).join("");
  switch (node.tagName) {
    case "STRONG":
    case "B":
      return inner.trim() ? `**${inner}**` : inner;
    // 형광펜(<mark>)은 마크다운 표준에 없다. 강조라는 의미가 사라지는 것보다 굵게로
    // 남는 것이 낫다 — 네이버 붙여넣기는 HTML 경로라 형광펜이 그대로 유지된다.
    case "MARK":
      return inner.trim() ? `**${inner}**` : inner;
    case "EM":
    case "I":
      return inner.trim() ? `*${inner}*` : inner;
    case "CODE":
      return inner.trim() ? `\`${inner}\`` : inner;
    case "A": {
      const href = node.getAttribute("href");
      return href ? `[${inner}](${href})` : inner;
    }
    case "BR":
      return "\n";
    default:
      return inner;
  }
}

/** Markdown for the same article, carrying whatever image URLs the HTML now has. */
export function markdownFromHtml(html: string, title: string): string {
  const lines: string[] = [`# ${title}`];
  for (const node of contentBlocks(html)) {
    const image = node.matches("img") ? node : node.querySelector("img");
    if (image instanceof HTMLImageElement) {
      lines.push(`![${image.getAttribute("alt") ?? ""}](${image.getAttribute("src") ?? ""})`);
      continue;
    }
    const heading = /^H([1-6])$/.exec(node.tagName);
    if (heading) {
      // 제목(h1)은 위에서 이미 넣었으므로 본문 소제목만 남긴다.
      const level = Number(heading[1]);
      if (level === 1) continue;
      lines.push(`${"#".repeat(level)} ${inlineMarkdown(node).trim()}`);
      continue;
    }
    if (node.tagName === "UL" || node.tagName === "OL") {
      const ordered = node.tagName === "OL";
      let index = 1;
      node.querySelectorAll(":scope > li").forEach((item) => {
        const marker = ordered ? `${index++}.` : "-";
        lines.push(`${marker} ${inlineMarkdown(item).trim()}`);
      });
      continue;
    }
    if (node.tagName === "BLOCKQUOTE") {
      const quote = inlineMarkdown(node).trim();
      if (quote)
        lines.push(
          quote
            .split("\n")
            .map((line) => `> ${line}`)
            .join("\n"),
        );
      continue;
    }
    if (node.tagName === "TABLE") {
      // 표를 셀 텍스트 덩어리로 붙이지 않고 마크다운 표 문법으로 옮긴다 — Markdown 복사에서도
      // 비교 구조가 살아남아야 한다(HTML 복사·네이버 발행은 <table> 그대로 간다).
      const table = markdownTable(node);
      if (table) lines.push(table);
      continue;
    }
    const text = inlineMarkdown(node).trim();
    if (text) lines.push(text);
  }

  return lines.join("\n\n");
}

/** <table>을 마크다운 표로. 셀 안의 파이프 문자는 표 구조를 깨뜨리므로 이스케이프한다. */
function markdownTable(table: Element): string {
  const cellText = (cell: Element) =>
    inlineMarkdown(cell).trim().replace(/\n+/g, " ").replace(/\|/g, "\\|");
  const rows = Array.from(table.querySelectorAll("tr")).map((row) =>
    Array.from(row.querySelectorAll("th, td")).map(cellText),
  );
  if (!rows.length || !rows[0].length) return "";

  const width = Math.max(...rows.map((row) => row.length));
  const pad = (row: string[]) => [...row, ...Array(width - row.length).fill("")];
  const line = (row: string[]) => `| ${pad(row).join(" | ")} |`;

  return [line(rows[0]), `| ${Array(width).fill("---").join(" | ")} |`, ...rows.slice(1).map(line)].join(
    "\n",
  );
}
