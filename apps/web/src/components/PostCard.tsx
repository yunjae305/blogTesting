import { useEffect, useRef, useState } from "react";
import { request } from "../api/client";
import type { BlogTask, BlogTaskListItem } from "../api/types";
import { STATUS_LABELS, visiblePurposes } from "../constants";
import { resumeActionLabel } from "../resume";
import { useStore } from "../store";
import {
  articleHtmlForClipboard,
  articleMarkdownForClipboard,
  copyRichHtmlLazy,
  formatDate,
} from "../utils";

export function taskTitle(task: BlogTask | BlogTaskListItem): string {
  if ("title" in task) return task.title;
  return task.finalPost?.title || task.trendSelection?.finalTopic || task.input.topic;
}

function StatusBadge({ status }: { status: string }) {
  const label = STATUS_LABELS[status] ?? { text: status, tone: "" };
  return (
    <span className={`badge post-status ${label.tone}`} aria-label={`상태: ${label.text}`}>
      {label.text}
    </span>
  );
}

/**
 * 이 글이 어디서 왔는지. 예약 포스팅으로 만든 글에만 붙인다.
 *
 * 표식 없이 두면 같은 목록에 있는 카드인데 버튼이 어떤 것은 새 글 작성으로, 어떤
 * 것은 예약의 작업 큐로 간다 — 왜 다른지 화면에 설명이 없다(2026-08-06).
 */
function ScheduleBadge() {
  return (
    <span className="badge post-origin" title="예약 포스팅으로 만든 글입니다">
      예약 포스팅
    </span>
  );
}

type PostCardLayout = "list" | "previous-grid";

/** 원고 복사. 리스트에서는 글자 대신 이 아이콘만 쓴다 — 한 줄에 버튼 셋이 들어가야 한다. */
function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect
        x="9"
        y="9"
        width="11"
        height="11"
        rx="2.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <path
        d="M15 5.5A2.5 2.5 0 0 0 12.5 3h-6A3.5 3.5 0 0 0 3 6.5v6A2.5 2.5 0 0 0 5.5 15"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function EmptyPosts({ layout = "list" }: { layout?: PostCardLayout }) {
  if (layout === "previous-grid") {
    return (
      <div className="empty posts-card-empty">
        <p>아직 쓴 글이 없습니다.</p>
        <a className="button primary small" href="#/write">
          첫 글 쓰기
        </a>
      </div>
    );
  }

  return (
    <div className="empty posts-empty">
      <div className="posts-empty-copy">
        <strong>아직 쓴 글이 없습니다.</strong>
        <p className="subtle">아이디어를 메모하면 첫 원고를 바로 시작할 수 있어요.</p>
      </div>
      <a className="button primary small" href="#/write">
        첫 글 쓰기
      </a>
    </div>
  );
}

export function PostCard({
  task,
  layout = "list",
  order,
  selectionMode = false,
  selected = false,
  onToggleSelect,
}: {
  task: BlogTaskListItem;
  layout?: PostCardLayout;
  /**
   * 리스트에서 이 줄이 **몇 번째로 보이는가**(1부터). 카드형에서는 쓰지 않는다.
   *
   * 글에 붙은 번호가 아니라 **지금 화면의 순서**다 — 정렬이나 필터를 바꾸면 같은 글의
   * 번호가 달라진다. 그것이 이 칸의 뜻이다: "위에서 몇 번째 줄인가".
   */
  order?: number;
  selectionMode?: boolean;
  selected?: boolean;
  onToggleSelect?: (postId: string) => void;
}) {
  const { session, openPost, scheduledPostIds, showToast, reportError } = useStore();
  const [copying, setCopying] = useState(false);
  const activeOwner = session?.user.userId ?? task.userId;
  const activeOwnerRef = useRef<string | null>(activeOwner);

  useEffect(() => {
    activeOwnerRef.current = activeOwner;
    return () => {
      activeOwnerRef.current = null;
    };
  }, [activeOwner]);

  // 예약으로 만든 글의 내부 기본 목적은 싣지 않는다 — 사용자가 고른 값이 아니다.
  const purpose = visiblePurposes(task.purposes);
  const candidate = task.postUrl;
  const url = candidate && /^https?:\/\//i.test(candidate) ? candidate : null;

  /** 발행 화면의 HTML 복사와 같은 것을 준다. 여기만 마크다운이던 것은 실수였다. */
  async function copyPost() {
    if (!task.hasFinalPost || copying) return;
    const ownerAtStart = activeOwner;
    setCopying(true);
    // 클립보드 쓰기는 **클릭 안에서** 시작해야 한다. 상세 문서(이미지 포함 수 MB)를
    // 기다린 뒤에 쓰면 클릭 권한이 만료돼 거부된다 — '불러오는 중'만 길게 돌다
    // 복사가 안 되던 원인(2026-08-10). 그래서 문서 요청을 Promise로 걸어 두고
    // 지연 쓰기(copyRichHtmlLazy)에 그대로 넘긴다.
    const finalPost = request<BlogTask>(
      `/posts/${encodeURIComponent(task.postId)}`,
    ).then((latest) => {
      if (activeOwnerRef.current !== ownerAtStart) {
        throw new Error("copy-aborted");
      }
      if (!latest.finalPost) {
        throw new Error("copy-no-draft");
      }
      return latest.finalPost;
    });
    try {
      await copyRichHtmlLazy(
        finalPost.then((post) => articleHtmlForClipboard(post, task.postId)),
        finalPost.then((post) => articleMarkdownForClipboard(post, task.postId)),
      );
      if (activeOwnerRef.current !== ownerAtStart) return;
      showToast("글을 복사했습니다. 에디터에 그대로 붙여넣으세요.");
    } catch (error) {
      if (activeOwnerRef.current !== ownerAtStart) return;
      if (error instanceof Error && error.message === "copy-aborted") return;
      if (error instanceof Error && error.message === "copy-no-draft") {
        showToast("저장된 원고를 찾지 못했습니다.", true);
        return;
      }
      reportError(error);
    } finally {
      if (activeOwnerRef.current === ownerAtStart) setCopying(false);
    }
  }

  const toggle = () => onToggleSelect?.(task.postId);

  /**
   * 이 글은 **예약 포스팅이 만든 것인가.** 그렇다면 아직 만들고 있는 동안의 진행은
   * 예약의 작업 큐에서 봐야 한다.
   *
   * 두 기능은 하는 일이 다르다(2026-08-06 사용자 지적). 새 글 작성은 글 한 편을 손으로
   * 끌고 가는 자리라 3단계에 '다시 생성하기'가 있고 사용자가 단계를 오간다. 예약
   * 포스팅은 소재 여러 개를 걸어 두면 서버가 순서대로 만들어 올리는 자리이고, 지금
   * 몇 번째 글이 어디까지 갔는지는 작업 큐가 보여 준다. 예약 글을 새 글 작성으로
   * 열면 그 글만 뚝 떼어 놓은 화면이 나와, 나머지 예약이 어떻게 되고 있는지 알 수 없다.
   *
   * **원고가 나온 뒤에는 그대로 둔다.** 그때는 '발행하기'·'원고 보기'가 원고를 확인하고
   * 손으로 올리는 자리이고, 그건 예약 글에도 쓸모가 있다.
   */
  // 목록보다 늦게 도착하거나(예약 조회 실패) 아예 없을 수 있다 — 그때는 예전 동작
  // 그대로다. 곁들이 정보 하나 때문에 카드가 통째로 깨지면 안 된다.
  const fromSchedule = scheduledPostIds?.has(task.postId) ?? false;
  const stillMaking = fromSchedule && !task.hasFinalPost;

  const openQueue = () => {
    location.hash = "#/scheduled/queue";
  };

  const actionLabel = stillMaking ? "예약 작업 보기" : resumeActionLabel(task);
  const openAction = stillMaking ? openQueue : () => void openPost(task.postId);

  if (layout === "previous-grid") {
    return (
      <article
        className={`post-card post-card--previous${selectionMode ? " selectable" : ""}${selected ? " selected" : ""}`}
        onClick={selectionMode ? toggle : undefined}
        role="listitem"
      >
        <div className="post-card-top">
          <div className="post-card-heading">
            {selectionMode && (
              <input
                type="checkbox"
                className="post-select"
                checked={selected}
                onChange={toggle}
                onClick={(event) => event.stopPropagation()}
                aria-label={`${taskTitle(task)} 선택`}
              />
            )}
            <h3>{taskTitle(task)}</h3>
          </div>
          <span className="post-card-badges">
            {fromSchedule && <ScheduleBadge />}
            <StatusBadge status={task.status} />
          </span>
        </div>
        {/* 목적 개수는 뺐다(2026-08-06 사용자 요청). 목적은 원래 하나만 고를 수 있어
            '1개'라는 숫자가 아무것도 알려 주지 않는다 — 바로 아래에 그 목적이 칩으로
            적혀 있다. */}
        <p className="post-meta">{formatDate(task.createdAt)}</p>
        <div className="keyword-list">
          {purpose.map((keyword) => (
            <span className="chip plain" key={keyword}>
              {keyword}
            </span>
          ))}
        </div>
        {!selectionMode && (
          <div className="actions post-card-previous-actions">
            {/* 이 카드의 postId만 넘긴다. 여는 단계는 그 글의 서버 상태가 정하고,
                버튼 문구도 같은 규칙(resume.ts)에서 나온다. 예약이 아직 만들고 있는
                글만 예외로 작업 큐를 연다(위 stillMaking 참고). */}
            <button className="button small" type="button" onClick={openAction}>
              {actionLabel}
            </button>
            {task.hasFinalPost && (
              <button className="button small" type="button" onClick={copyPost} disabled={copying}>
                {copying ? "불러오는 중" : "원고 복사"}
              </button>
            )}
            {url && (
              <a className="button small" href={url} target="_blank" rel="noopener noreferrer">
                발행글 열기
              </a>
            )}
          </div>
        )}
      </article>
    );
  }

  return (
    <article
      className={`post-card${selectionMode ? " selectable" : ""}${selected ? " selected" : ""}`}
      onClick={selectionMode ? toggle : undefined}
      role="listitem"
    >
      {/* 몇 번째 줄인가(2026-08-07 사용자 요청). 읽어 주는 쪽에는 감춘다 — 줄마다
          "1 AIONA 원고 준비 중"처럼 번호가 먼저 읽히면 방해가 되고, 순서는 목록이
          이미 순서대로 놓여 있다는 사실로 전해진다. */}
      <div className="post-card-cell post-card-index-cell" aria-hidden="true">
        {order}
      </div>

      {/* 소재는 제목 아래가 아니라 **제 칸**에 둔다(2026-08-06 사용자 요청). 제목 밑에
          붙여 두면 두 줄짜리 덩어리가 되어 줄 높이가 들쭉날쭉해지고, 소재끼리
          비교해 보기도 어렵다. */}
      <div className="post-card-cell post-card-subject-cell">
        {selectionMode && (
          <input
            type="checkbox"
            className="post-select"
            checked={selected}
            onChange={toggle}
            // The article's onClick also toggles; stop the click here so a tick
            // on the box does not fire both and cancel itself out.
            onClick={(event) => event.stopPropagation()}
            aria-label={`${taskTitle(task)} 선택`}
          />
        )}
        <span className="post-card-mobile-label">소재</span>
        <span className="post-card-subject">{task.subject || task.topic}</span>
      </div>

      <div className="post-card-cell post-card-title-cell">
        <span className="post-card-mobile-label">제목</span>
        <h3>{taskTitle(task)}</h3>
      </div>

      <div className="post-card-cell post-card-status-cell">
        <span className="post-card-mobile-label">상태</span>
        <span className="post-card-badges">
          {fromSchedule && <ScheduleBadge />}
          <StatusBadge status={task.status} />
        </span>
      </div>

      <div className="post-card-cell post-card-purpose-cell">
        <span className="post-card-mobile-label">글 목적</span>
        <div className="keyword-list">
          {purpose.map((keyword) => (
            <span className="chip plain" key={keyword}>
              {keyword}
            </span>
          ))}
        </div>
      </div>

      <div className="post-card-cell post-card-date-cell">
        {/* 목록의 기본 정렬이 '만든 순'이라 만든 시각을 보여 준다. 수정일을 보여 주면
            늘어선 순서와 적힌 날짜가 어긋나 보인다(2026-08-06 사용자 요청). */}
        <span className="post-card-mobile-label">생성 시각</span>
        <time className="post-meta" dateTime={task.createdAt}>
          {formatDate(task.createdAt)}
        </time>
      </div>

      {/* In selection mode the whole card is a checkbox, so the per-post actions
          would just fight the tick. They come back the moment 삭제 is cancelled. */}
      {!selectionMode && (
        <div className="actions post-card-actions">
          <button className="button small" type="button" onClick={openAction}>
            {actionLabel}
          </button>
          {url && (
            <a className="button small" href={url} target="_blank" rel="noopener noreferrer">
              발행글 열기
            </a>
          )}
          {/* 복사는 맨 오른쪽이다(2026-08-06 사용자 요청). 글자 버튼끼리 붙어 있어야
              읽기 좋고, 아이콘 하나가 사이에 끼면 줄이 끊겨 보인다. */}
          {task.hasFinalPost && (
            <button
              className="button small icon-button post-card-copy"
              type="button"
              onClick={copyPost}
              disabled={copying}
              aria-label={copying ? "원고를 불러오는 중" : "원고 복사"}
              title="원고 복사"
            >
              {copying ? <span className="spinner" aria-hidden="true" /> : <CopyIcon />}
            </button>
          )}
        </div>
      )}
    </article>
  );
}
