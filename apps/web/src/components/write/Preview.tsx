import { Fragment, lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";

import { ApiError, requestWithSessionToken } from "../../api/client";
import type { BlogTask, FinalReview } from "../../api/types";
import { useStore } from "../../store";
import { inlineSegments, previewBlocks } from "../../utils";
import { ImageSourceNote } from "./ImageSourceNote";

/**
 * 편집기는 **'편집'을 눌렀을 때** 받아 온다.
 *
 * 이 에디터(Tiptap)와 그 확장들이 작성 화면 조각의 대부분이었다 — 조각 하나가 516kB였고
 * 그중 400kB 남짓이 여기서 왔다. 그런데 원고를 손으로 고치는 사람은 일부이고, 소재 입력·
 * 제목 고르기·원고 생성·발행은 편집기 없이 끝난다. '새 글 작성'을 누른 모든 사람이 쓰지도
 * 않을 편집기를 먼저 받느라 기다리고 있었다(2026-08-06).
 *
 * 나눠도 동작은 같다. 편집을 누르면 그때 받아 오고, 받는 동안 그 자리에 안내가 뜬다.
 */
const DraftEditor = lazy(() =>
  import("./DraftEditor").then((module) => ({ default: module.DraftEditor })),
);

/** Long enough that a sentence is not saved a word at a time; short enough that
    nobody loses work by closing the tab. */
const AUTOSAVE_DELAY_MS = 3000;

/**
 * 최종 검수 결과를 사람이 읽을 한 줄씩으로.
 *
 * 사용자에게 내부 LLM 프롬프트나 원시 JSON을 보여주지 않는다는 것이 이 함수의 존재 이유다.
 * 항목별 판정·인용문·교체문은 전부 저장돼 있지만, 화면에는 "무엇이 끝났고 무엇을 확인해야
 * 하는가"만 남긴다.
 */
function reviewSummary(review: FinalReview | undefined): string[] {
  if (!review) return [];
  // 검수 실패 안내("품질 검수를 마치지 못했습니다")는 뺐다(2026-08-07 사용자 결정).
  // 사용자가 할 수 있는 행동이 없는 표시인데 체크 아이콘 때문에 오히려 '검수가 안 된
  // 최종본인가' 혼란을 줬다. 실패 사유는 finalReview.error와 서버 로그("최종 검수
  // 실패")에 그대로 남는다 — 화면에서만 뺀 것이지 사실을 지운 것이 아니다.
  if (review.error) return [];

  // '품질 검수 완료'는 빼 뒀다(2026-08-06 사용자 요청). 아무 행동도 요구하지 않는
  // 표시라 화면만 차지한다 — **문제가 있을 때만 말한다.** 아래 항목이 하나도 없으면
  // 이 칸은 통째로 그려지지 않는다.
  const notes: string[] = [];
  if (review.applied > 0) notes.push(`일부 표현 자동 수정 ${review.applied}건`);
  if (review.removedImages > 0) notes.push(`관련 없는 이미지 ${review.removedImages}장 제외`);
  // 남은 지적 = 자동으로 고치지 못해 사람이 봐야 하는 것.
  if (review.issues.length > 0) {
    notes.push(`확인이 필요한 표현 ${review.issues.length}건`);
  }
  return notes;
}

/** 굵게를 화면에 그린다. 텍스트 조각만 다루므로 HTML을 주입하지 않는다. */
function inline(text: string) {
  return inlineSegments(text).map((segment, index) =>
    segment.bold ? <strong key={index}>{segment.text}</strong> : <span key={index}>{segment.text}</span>,
  );
}

export function Preview() {
  const { session, task, setTask, showToast, reportError } = useStore();
  const post = task?.finalPost;
  const draftToken = session?.accessToken;

  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [html, setHtml] = useState("");
  // 저장 상태를 하나의 값으로 관리해 저장 중·저장 완료·저장 실패를 모두 사용자에게 보여준다.
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [savedAt, setSavedAt] = useState<string | null>(null);

  const saving = saveState === "saving";

  const timer = useRef<number>(0);
  const dirty = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const blocks = post ? previewBlocks(post) : [];

  // 이미지 종류(사진·도표·참고자료·화면 캡처·표지)는 서버가 판정해 images에 실어 준다.
  // 미리보기는 markdownContent에서 블록을 만드는데 마크다운에는 class를 담을 수 없으므로,
  // src로 맞춰 종류를 되찾는다. 종류를 모르면 사진으로 둔다(가장 흔한 경우).
  const mediaKinds = new Map((post?.images ?? []).map((image) => [image.dataUrl, image.mediaKind]));
  const mediaClass = (src: string) => {
    const kind = mediaKinds.get(src) ?? "photo";
    return `preview-image blog-media blog-media--${kind}`;
  };

  // 이미지 모델이 그린 사진에만 'AI이미지'를 붙인다(2026-08-05 사용자 요청).
  // 코드로 그린 도표(rendered)·웹 사진(web)·사용자 업로드(reference)는 AI 생성물이 아니다.
  //
  // **이 표시는 미리보기에만 있다.** 발행·복사는 서버가 만든 htmlContent를 그대로 쓰는데
  // 여기 배지는 React가 이 화면에서만 그리는 것이라 그 HTML에 들어가지 않는다 — 발행된
  // 글에는 나타나지 않는다.
  const aiGenerated = new Set(
    (post?.images ?? []).filter((image) => image.source === "generated").map((image) => image.dataUrl),
  );
  const aiBadge = (src: string) =>
    aiGenerated.has(src) ? <figcaption className="preview-ai-badge">AI이미지</figcaption> : null;

  // 구조화된 출처(2026-08-11). 미리보기는 markdownContent로 블록을 만드는데 마크다운은
  // 출처를 캡션 문자열 한 줄로만 실어 나른다 — 그 줄로는 원문 페이지로 갈 수 없다.
  // mediaKinds와 같은 방식으로 src를 열쇠 삼아 되찾는다. 없는 이미지(옛 문서·도표·
  // 사용자 업로드)는 undefined이고, 그때는 예전처럼 캡션만 보인다.
  const imageSources = new Map(
    (post?.images ?? []).map((image) => [image.dataUrl, image.imageSource]),
  );
  const sourceNote = (src: string) => <ImageSourceNote source={imageSources.get(src)} />;

  // 최종 검수 요약. **결과만** 보여준다 — 내부 프롬프트나 원시 판정 JSON은 화면에 내지 않는다.
  const reviewNotes = reviewSummary(task?.draftGenerationResult?.finalReview);

  // Only reachable for a draft whose body carries no image markup at all — an
  // older post, or one written before the writer model placed any tag. Without
  // this the images would simply not be shown.
  const orphanImages =
    post && !blocks.some((block) => block.kind === "image") ? (post.images ?? []) : [];

  // Editable only while it is still a draft. Once it is published, what is on 네이버
  // has left, and changing the copy here would only make the two disagree.
  const canEdit = Boolean(post) && task?.status === "READY_TO_PUBLISH";

  const save = useCallback(
    async (nextTitle: string, nextHtml: string, silent = false) => {
      if (!task || !draftToken || !nextTitle.trim() || !nextHtml.trim()) return;

      setSaveState("saving");
      try {
        // Pin the token that owns this draft. When account B is activated, the
        // cleanup from account A may still flush the last three seconds of edits.
        // Using the global token there would either lose the edit or send A's post
        // id under B's authorization.
        const updated = await requestWithSessionToken<BlogTask>(
          `/posts/${task.postId}/draft`,
          draftToken,
          {
            method: "PUT",
            body: { title: nextTitle.trim(), html: nextHtml },
          },
        );
        if (!mountedRef.current) return;

        setTask(updated);
        dirty.current = false;
        setSavedAt(new Date().toLocaleTimeString("ko-KR"));
        setSaveState("saved");
        if (!silent) showToast("원고를 저장했습니다.");
      } catch (error) {
        // A current-session 401 is already translated into the single global
        // "login expired" flow. A response owned by an unmounted account must
        // not leak a save error into the account that replaced it.
        if (
          !mountedRef.current ||
          (error instanceof ApiError && error.status === 401)
        ) {
          return;
        }

        // dirty를 그대로 두어(초기화하지 않음) 다음 편집이나 '편집 마치기'에서 다시 저장된다.
        setSaveState("error");
        reportError(error);
      }
    },
    [draftToken, task, setTask, showToast, reportError],
  );

  // Autosave. The edit is only in the browser until this runs, so it also fires on
  // the way out of the editor — closing it must not be how you lose a paragraph.
  useEffect(() => {
    if (!editing || !dirty.current) return;

    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void save(title, html, true), AUTOSAVE_DELAY_MS);
    return () => window.clearTimeout(timer.current);
  }, [editing, title, html, save]);

  // 미리보기는 1·2단계에서 화면에 없다(WriteView). 그래서 편집 도중 단계를 되돌아가면 이
  // 컴포넌트가 통째로 사라지는데, 위 타이머는 cleanup에서 취소만 되고 저장되지 않는다 —
  // 3초 안에 단계를 옮기면 방금 쓴 문단이 그대로 없어진다. 사라지기 전에 한 번 흘려보낸다.
  //
  // ref에 담아 두는 이유: 언마운트에서만 돌게 하려면 effect의 의존성이 비어야 하는데,
  // 그러면 클로저가 첫 렌더의 낡은 값을 잡는다. 매 렌더마다 최신 값을 ref에 갈아 끼우고
  // cleanup은 그 ref를 부른다.
  const flush = useRef<() => void>(() => {});
  flush.current = () => {
    window.clearTimeout(timer.current);
    if (editing && dirty.current) void save(title, html, true);
  };
  useEffect(() => () => flush.current(), []);

  function startEditing() {
    if (!post) return;
    setTitle(post.title);
    setHtml(post.htmlContent);
    setSavedAt(null);
    setSaveState("idle");
    dirty.current = false;
    setEditing(true);
  }

  async function done() {
    window.clearTimeout(timer.current);
    if (dirty.current) await save(title, html);
    // 저장이 실패해 아직 저장되지 않은 변경이 남았다면 편집기를 닫지 않는다 —
    // 닫으면 저장 안 된 원고를 잃는다. 상태 표시가 '저장 실패'로 남아 재시도를 안내한다.
    if (dirty.current) return;
    setEditing(false);
  }

  return (
    <section
      className={`panel preview-panel ${editing ? "is-editing" : "is-reading"}${
        !editing && !post ? " is-awaiting" : ""
      }`}
    >
      <div className="panel-header">
        <div className="panel-heading-copy">
          <p className="panel-kicker">{editing ? "EDITING DESK" : "ARTICLE PREVIEW"}</p>
          <div className="preview-title-row">
            <h2 className="panel-title">{editing ? "원고 수정 및 편집" : "미리보기"}</h2>
            {/* 원고가 아직 없다면 이 자리는 '앞으로 여기에 나온다'는 약속이다. */}
            {!editing && !post && <span className="preview-coming-badge">COMING SOON</span>}
          </div>
          <p className="panel-subtitle">
            {editing
              ? "내용을 다듬으면 3초 뒤 자동으로 저장됩니다."
              : post
                ? "실제 블로그에 발행될 문서의 흐름을 확인해 보세요."
                : "모든 단계를 완료하면 실제 원고를 미리 볼 수 있어요."}
          </p>
        </div>

        {editing ? (
          <div className="actions">
            <span
              className={`hint ${saveState === "error" ? "hint-error" : ""}`}
              aria-live="polite"
            >
              {saveState === "saving"
                ? "저장 중..."
                : saveState === "error"
                  ? "저장 실패 · 편집을 마치면 다시 시도합니다"
                  : savedAt
                    ? `${savedAt} 자동 저장됨`
                    : "3초마다 자동 저장"}
            </span>
            <button
              className="button primary small"
              type="button"
              id="finishEditing"
              onClick={done}
              disabled={saving}
            >
              편집 마치기
            </button>
          </div>
        ) : (
          <div className="keyword-list">
            {post?.hashtags?.map((tag) => (
              <span className="chip" key={tag}>
                #{tag}
              </span>
            ))}
            {canEdit && (
              <button className="button small" type="button" id="editDraft" onClick={startEditing}>
                원고 수정
              </button>
            )}
          </div>
        )}
      </div>

      <div className="panel-body">
        {editing ? (
          <div className="form-grid editor-workspace">
            <div className="field full">
              <label htmlFor="draftTitle">제목</label>
              <input
                id="draftTitle"
                value={title}
                onChange={(event) => {
                  dirty.current = true;
                  setTitle(event.target.value);
                }}
              />
            </div>
            <div className="field full">
              <div className="editor-field-heading">
                <label>본문</label>
                <span>서식과 이미지 배치를 직접 조정할 수 있어요.</span>
              </div>
              <Suspense
                fallback={<div className="loading-state">편집기를 준비하는 중입니다.</div>}
              >
                <DraftEditor
                  html={html}
                  onChange={(next) => {
                    dirty.current = true;
                    setHtml(next);
                  }}
                />
              </Suspense>
              <p className="hint">
                이미지는 끌어서 옮기거나 지울 수 있습니다. 소제목·목록·인용·링크·표를 쓸 수
                있습니다.
              </p>
            </div>
          </div>
        ) : (
          <div className="preview-document-wrap">

            <article className="preview preview-document">
              {post ? (
                <>
                  <header className="preview-article-header">
                    <p className="preview-article-kicker">BLOG-IT ARTICLE</p>
                    <h2>{post.title}</h2>
                    <div className="preview-article-rule" aria-hidden="true" />
                  </header>

                  {/* 검수 요약. 결과만 한 줄씩 보여주고 판정 근거·인용문은 내지 않는다. */}
                  {reviewNotes.length > 0 && (
                    <p className="preview-review-summary">
                      {reviewNotes.map((note, index) => (
                        <span className="preview-review-note" key={index}>
                          {note}
                        </span>
                      ))}
                    </p>
                  )}

                  {orphanImages.length > 0 && (
                    <div className="preview-images">
                      {orphanImages.map((image, index) => (
                        <div key={`${image.dataUrl.slice(0, 32)}-${index}`}>
                          <figure className={mediaClass(image.dataUrl)}>
                            <img src={image.dataUrl} alt={image.altText} />
                            {aiBadge(image.dataUrl)}
                          </figure>
                          {/* 본문에 자리가 없는 이미지라도 출처는 함께 보여야 한다. */}
                          {image.caption && <p className="visual-caption">{image.caption}</p>}
                          {sourceNote(image.dataUrl)}
                        </div>
                      ))}
                    </div>
                  )}

                  {blocks.map((block, index) => {
                    switch (block.kind) {
                      case "image": {
                        // 캡션 블록이 뒤따르면 출처 줄은 그 아래에 붙인다 — 캡션과 출처가
                        // 그림 사이에 끼어 순서가 뒤집히면 어느 그림의 출처인지 흐려진다.
                        const captioned = blocks[index + 1]?.kind === "caption";
                        return (
                          // 감싸는 요소를 두지 않는다(Fragment) — 지금 지면 CSS는 figure와
                          // 캡션이 .preview의 바로 아래 자식이라는 전제로 여백을 잡는다.
                          <Fragment key={index}>
                            <figure className={mediaClass(block.src)}>
                              <img src={block.src} alt={block.alt} loading="lazy" />
                              {aiBadge(block.src)}
                            </figure>
                            {!captioned && sourceNote(block.src)}
                          </Fragment>
                        );
                      }
                      // 사진 출처·기준시점. 바로 위 그림에 붙어 한 묶음으로 읽혀야 한다.
                      case "caption": {
                        const above = blocks[index - 1];
                        return (
                          <Fragment key={index}>
                            <p className="visual-caption">{block.text}</p>
                            {above?.kind === "image" && sourceNote(above.src)}
                          </Fragment>
                        );
                      }
                      case "heading":
                        // 마크다운의 `##`은 본문 소제목이다. 레벨을 버리고 전부 h3로
                        // 그리면 편집기가 보여 주는 위계가 미리보기에서 사라진다.
                        return block.level && block.level >= 3 ? (
                          <h4 key={index}>{inline(block.text)}</h4>
                        ) : (
                          <h3 key={index}>{inline(block.text)}</h3>
                        );
                      case "list":
                        return (
                          <ul key={index}>
                            {block.items.map((item, itemIndex) => (
                              <li key={itemIndex}>{inline(item)}</li>
                            ))}
                          </ul>
                        );
                      case "table":
                        return (
                          <table className="preview-table" key={index}>
                            <thead>
                              <tr>
                                {block.header.map((cell, cellIndex) => (
                                  <th key={cellIndex}>{cell}</th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {block.rows.map((row, rowIndex) => (
                                <tr key={rowIndex}>
                                  {row.map((cell, cellIndex) => (
                                    <td key={cellIndex}>{cell}</td>
                                  ))}
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        );
                      default:
                        return <p key={index}>{inline(block.text)}</p>;
                    }
                  })}
                </>
              ) : (
                <div className="preview-empty">
                  <svg viewBox="0 0 48 48" aria-hidden="true" focusable="false">
                    <path d="M14 9h13l7 7v23H14z" />
                    <path d="M27 9v7h7" />
                    <path d="M19 24h11M19 30h11" />
                  </svg>
                  <span>
                    모든 단계가 완료되면
                    <strong>여기에 원고가 표시됩니다.</strong>
                  </span>
                </div>
              )}
            </article>
          </div>
        )}
      </div>
    </section>
  );
}
