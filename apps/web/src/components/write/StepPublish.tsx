import { useEffect, useState } from "react";

import { request } from "../../api/client";
import type { BlogTask, PostingChannel } from "../../api/types";
import { STATUS_LABELS } from "../../constants";
import { WRITE_STEP } from "../../resume";
import { useStore } from "../../store";
import type { NaverStatus } from "../NaverConnect";
// 브랜드 표식은 예약 화면과 같은 것을 쓴다 — 두 벌로 만들면 한쪽만 바뀌는 날이 온다.
import { NaverMark, ThreadsMark } from "../scheduled/icons";
import { LiveSessionsPanel } from "../LiveSessionsPanel";
import { VerificationCodeModal } from "./VerificationCodeModal";
import type { PendingVerification } from "./VerificationCodeModal";
import {
  articleHtmlForClipboard,
  articleMarkdownForClipboard,
  copyRichHtml,
  downloadPostImages,
} from "../../utils";

// 발행 버튼 하나가 요청 하나다: 네이버 임시저장 / 네이버 발행 / 스레드 발행.
type PublishAction = "draft" | "naver" | "threads";

// 인증 대기를 살피는 주기. 사람이 문자를 확인하는 시간에 비하면 2초는 충분히 촘촘하다.
const VERIFICATION_POLL_MS = 2000;

export function StepPublish() {
  const { task, setTask, setRoute, showToast, reportError } = useStore();
  // 어떤 버튼이 도는 중인지 구분한다 — 임시저장과 발행이 같은 busy를 쓰면 둘 다 스피너가 돈다.
  const [busyAction, setBusyAction] = useState<PublishAction | null>(null);
  const busy = busyAction !== null;
  const [naver, setNaver] = useState<NaverStatus | null>(null);
  // 발행 자동화가 2단계 인증에서 멈추면 이 값이 채워지고 코드 입력창이 뜬다.
  const [verification, setVerification] = useState<PendingVerification | null>(null);

  const hasDraft = Boolean(task?.finalPost);
  // 중복 발행 가드는 채널별이다 — 스레드에 올린 글이라도 네이버에는 아직 발행할 수 있다.
  // 채널 필드가 없던 옛 로그는 전부 네이버 발행이었다.
  const hasChannelAutoSuccess = (channel: PostingChannel) =>
    Boolean(
      task?.postingLogs.some(
        (log) =>
          log.method === "auto" &&
          log.result === "success" &&
          (log.channel ?? "naver") === channel,
      ),
    );
  const publishableTo = (channel: PostingChannel) =>
    Boolean(
      task &&
        (["READY_TO_PUBLISH", "POSTING_NEEDS_HUMAN", "FAILED"].includes(task.status) ||
          (task.status === "POSTED" && !hasChannelAutoSuccess(channel))),
    );
  const legacyCopyCompleted =
    task?.status === "POSTED" && !hasChannelAutoSuccess("naver") && !hasChannelAutoSuccess("threads");
  const label = legacyCopyCompleted
    ? { text: "원고 생성 완료", tone: "" }
    : (STATUS_LABELS[task?.status ?? "INPUT"] ?? { text: "대기", tone: "" });

  useEffect(() => {
    void (async () => {
      try {
        setNaver(await request<NaverStatus>("/naver/status"));
      } catch {
        // The publish screen still works without it: 복사·스레드 발행 needs no 네이버 session.
      }
    })();
  }, []);

  // 발행이 도는 동안만 인증 대기를 살핀다. 발행 요청은 끝날 때까지 응답을 주지 않으므로
  // (자동화가 코드를 기다리며 멈춰 있다) 상태는 이 폴링으로만 알 수 있다.
  useEffect(() => {
    if (!busy) {
      setVerification(null);
      return;
    }
    let alive = true;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const next = await request<{ pending: PendingVerification | null }>(
            "/posting/verification",
          );
          if (alive) setVerification(next.pending);
        } catch {
          // 폴링 실패는 조용히 넘긴다 — 발행 자체는 그대로 진행 중이다.
        }
      })();
    }, VERIFICATION_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [busy]);

  async function publish(action: PublishAction) {
    if (!task) return;
    // 네이버로 가는 두 동작만 네이버 저장 정보가 필요하다. 스레드는 사용자별 브라우저
    // 세션으로 가므로(네이버와 같은 구조) 여기서 막지 않는다.
    if (action !== "threads" && !naver?.saved) {
      showToast("설정에서 Naver 로그인 정보를 먼저 저장해 주세요.", true);
      return;
    }
    setBusyAction(action);
    try {
      const updated = await request<BlogTask>(`/posts/${task.postId}/publish`, {
        method: "POST",
        body:
          action === "draft"
            ? { method: "draft", channel: "naver" }
            : { method: "auto", channel: action },
      });
      setTask(updated);
      if (action === "draft") {
        // 임시저장은 발행이 아니라서 상태가 READY_TO_PUBLISH에 그대로 머문다. 창을 닫지
        // 않고 두므로, 열린 네이버 창에서 이어 확인하라고 알린다.
        showToast("Naver에 임시저장했습니다. 열린 Naver 창에서 확인해 주세요.");
      } else if (updated.status === "POSTED") {
        showToast(
          action === "threads"
            ? "Threads에 발행했습니다. 발행된 글을 브라우저로 엽니다."
            : "Naver에 발행했습니다.",
        );
      } else {
        // 실패·추가 인증 필요 사유는 방금 붙은 발행 로그가 안다 — 두루뭉술한 안내 대신 그걸 보여준다.
        const lastLog = updated.postingLogs[updated.postingLogs.length - 1];
        showToast(lastLog?.errorMessage ?? "발행을 시도했습니다. 상태를 확인해 주세요.", true);
      }
    } catch (error) {
      reportError(error);
    } finally {
      setBusyAction(null);
    }
  }

  const imageCount = task?.finalPost?.images?.length ?? 0;

  /**
   * The images go across as URLs, not as base64. 네이버 refuses a pasted data-URL
   * image — "허용되지 않는 형식의 이미지가 있어 해당 이미지는 제외됩니다" — because its
   * editor only takes an image it can fetch. The copy therefore points at
   * GET /posts/{id}/images/{n}, which serves the same bytes over HTTP, and the editor
   * pulls them in like any other image on the web.
   */
  async function copy(kind: "markdown" | "html") {
    const post = task?.finalPost;
    if (!task || !post) return;
    try {
      const html = articleHtmlForClipboard(post, task.postId);
      const markdown = articleMarkdownForClipboard(post, task.postId);
      // 한 클립보드 항목에 HTML과 Markdown 평문을 함께 싣는다. 네이버·티스토리는
      // text/html을, 벨로그 같은 Markdown 편집기는 text/plain을 골라 붙여넣는다.
      await copyRichHtml(html, markdown);
      if (kind === "markdown") {
        showToast(
          "Markdown 형식으로 복사했습니다. 벨로그·깃허브·노션 등 Markdown 편집기에 붙여넣으세요.",
        );
      } else {
        showToast(
          "HTML·Markdown 호환 형식으로 복사했습니다. Naver·티스토리·벨로그에 붙여넣으세요.",
        );
      }
    } catch {
      showToast("복사에 실패했습니다. 미리보기에서 직접 선택해 주세요.", true);
    }
  }

  function saveImages() {
    const post = task?.finalPost;
    if (!post?.images?.length) return;

    const count = downloadPostImages(post.images, post.title);
    showToast(`이미지 ${count}장을 저장했습니다. 에디터의 사진 버튼으로 올려주세요.`);
  }

  const connected = Boolean(naver?.saved);

  return (
    <>
      {verification && (
        <VerificationCodeModal
          pending={verification}
          // 코드를 넘기면 창을 닫는다. 코드가 틀렸으면 자동화가 다시 물어보고,
          // 폴링이 그 새 요청을 잡아 창을 다시 띄운다.
          onDone={() => setVerification(null)}
        />
      )}
    <section className="panel step-panel write-paper-card publish-panel">
      <div className="panel-header publish-step-header">
        <div className="panel-heading-copy">
          <p className="panel-kicker">STEP {String(WRITE_STEP.PUBLISH + 1).padStart(2, "0")} · PUBLISH</p>
          <div className="publish-title-row">
            <h2 className="panel-title">발행 준비</h2>
            <span className={`badge publish-status-badge ${label.tone}`}>{label.text}</span>
          </div>
          <p className="panel-subtitle">완성한 글을 복사하거나 Naver 블로그에 바로 발행해 보세요.</p>
        </div>
        {/* 계정 세부정보를 이 화면에 늘어놓는 대신, 관리하는 자리로 가는 길만 둔다. */}
        <button
          className="button small publish-settings-link"
          type="button"
          onClick={() => setRoute("settings")}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0V21a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15H2.9a2 2 0 1 1 0-4H3a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4V2.9a2 2 0 1 1 4 0V3a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 10h.1a2 2 0 1 1 0 4H21a1.7 1.7 0 0 0-1.6 1Z" />
          </svg>
          연결 설정
        </button>
      </div>

      <div className="panel-body publish-panel-body">
        {/* 발행이 도는 동안 서버에서 열린 Chrome 화면을 그대로 중계한다 — 로그인·
            2단계 인증·캡차가 뜨면 사용자가 이 화면에서 직접 처리할 수 있다. 중계할
            화면이 없으면 아무것도 그려지지 않는다. */}
        {busy && <LiveSessionsPanel kinds={["publish"]} />}
        {/* 연결에 문제가 있을 때만 말한다(2026-08-07 사용자 결정: '연결 완료' 배너는
            아무 행동도 요구하지 않으면서 화면 한 줄을 차지했다). 정상 연결이면 이 칸은
            통째로 그려지지 않고 아래 발행 버튼들이 그만큼 올라온다. 아이디·비밀번호는
            어느 쪽이든 이 화면에 두지 않는다. */}
        {!connected && (
          <div className="publish-connection">
            <span className="publish-connection-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path
                  d="M7 3.5h10A3.5 3.5 0 0 1 20.5 7v10a3.5 3.5 0 0 1-3.5 3.5H7A3.5 3.5 0 0 1 3.5 17V7A3.5 3.5 0 0 1 7 3.5Z"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.7"
                />
                <path d="M8 16V8l8 8V8" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
              </svg>
            </span>
            <span className="publish-connection-copy">
              <strong>Naver 계정 연결 필요</strong>
              <span className="publish-connection-detail">
                <span>설정에서 Naver 계정을 저장하면 자동 발행을 쓸 수 있어요.</span>
              </span>
            </span>
            {/* 상태를 색만으로 알리지 않도록 글자를 함께 둔다. */}
            <span className="publish-connection-state">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <circle cx="12" cy="12" r="8" />
                <path d="M12 8v5M12 16h.01" />
              </svg>
              미연결
            </span>
          </div>
        )}

        <div className="publish-actions">
          {/* 왼쪽은 붙여넣기용 보조 액션, 오른쪽은 자동 발행 하나. 하는 일이 다르므로
              같은 줄에 같은 무게로 늘어놓지 않는다. */}
          <div className="publish-actions__secondary">
            <button
              className="button publish-action"
              type="button"
              onClick={() => copy("html")}
              disabled={!hasDraft}
              title="HTML 편집기에는 서식으로, 벨로그 같은 Markdown 편집기에는 이미지 포함 Markdown으로 붙여넣습니다."
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <rect x="9" y="9" width="11" height="11" rx="2" />
                <path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3" />
              </svg>
              HTML(서식) 복사
            </button>
            <button
              className="button publish-action"
              type="button"
              onClick={() => copy("markdown")}
              disabled={!hasDraft}
              title="마크다운 문법(#, **굵게**, - 목록, ![]())으로 복사합니다. 마크다운 편집기용입니다."
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <rect x="3" y="6" width="18" height="12" rx="2" />
                <path d="M6.5 15V9l2.5 3 2.5-3v6M16 9v4.5M14 12.5 16 15l2-2.5" />
              </svg>
              Markdown 복사
            </button>
            <button
              className="button publish-action"
              type="button"
              id="saveImages"
              onClick={saveImages}
              disabled={!imageCount}
              title="에디터가 이미지를 가져오지 못할 때 내려받아 직접 올릴 수 있습니다."
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M12 4v10m0 0-3.5-3.5M12 14l3.5-3.5" />
                <path d="M5 16v2.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V16" />
              </svg>
              이미지 다운로드 ({imageCount}장)
            </button>
          </div>

          <div className="publish-actions__primary">
            <div className="publish-actions__primary-row">
            <button
              className="button publish-action"
              type="button"
              onClick={() => publish("draft")}
              disabled={!publishableTo("naver") || !naver?.saved || busy}
              title="Naver 에디터의 저장 버튼을 눌러 임시저장합니다. 발행되지는 않습니다."
            >
              {busyAction === "draft" ? (
                <>
                  <span className="spinner" aria-hidden="true" /> 임시저장 중
                </>
              ) : (
                <>
                  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <path d="M5 5h10l4 4v10H5z" />
                    <path d="M8 5v5h7M8 19v-5h8v5" />
                  </svg>
                  Naver에 임시저장
                </>
              )}
            </button>
            {/* 세 버튼은 같은 줄의 같은 종류다 — 임시저장·Naver 발행·Threads 발행.
                예전에는 Naver 발행만 노란 primary에 더 큰 크기였는데, 한 줄에서 혼자
                크고 노랗다고 그 동작이 더 옳은 것은 아니다(2026-08-05 사용자 요청). */}
            <button
              className="button publish-action"
              type="button"
              onClick={() => publish("naver")}
              disabled={!publishableTo("naver") || !naver?.saved || busy}
            >
              {busyAction === "naver" ? (
                <>
                  <span className="spinner" aria-hidden="true" /> 처리 중
                </>
              ) : (
                <>
                  {/* 종이비행기 대신 네이버 브랜드 마크 — 예약 화면의 표식과 같은 것을 쓴다. */}
                  <NaverMark className="publish-brand-mark" />
                  Naver 발행
                </>
              )}
            </button>
            <button
              className="button publish-action publish-threads"
              type="button"
              onClick={() => publish("threads")}
              disabled={!publishableTo("threads") || busy}
              title="같은 소재로 쓰레드용 글을 새로 써서, 브라우저로 threads.net에 로그인해 연속 게시합니다(짧게 2~3개, 중간 3~5개 — 글 길이 설정이 정합니다). 처음 한 번은 열린 창에서 Threads(인스타그램) 로그인이 필요합니다."
            >
              {busyAction === "threads" ? (
                <>
                  <span className="spinner" aria-hidden="true" /> 처리 중
                </>
              ) : (
                <>
                  {/* 두 발행 버튼이 같은 규칙을 따른다 — 각 플랫폼의 브랜드 마크. */}
                  <ThreadsMark className="publish-brand-mark" />
                  Threads 발행
                </>
              )}
            </button>
            </div>

          </div>
        </div>


      </div>
    </section>
    </>
  );
}
