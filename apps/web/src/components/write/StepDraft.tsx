import { useEffect, useRef, useState } from "react";

import { ApiError, request } from "../../api/client";
import type { BlogTask } from "../../api/types";
import { DRAFT_FLOW_STEPS } from "../../constants";
import {
  draftProgressPercent,
  elapsedSince,
  formatElapsed,
  recordObservedStepSeconds,
} from "../../draftProgress";
import { WRITE_STEP } from "../../resume";
import { useStore } from "../../store";

/** 진행 막대를 다시 그리는 주기. 폴링(1.2초)과 무관하게 막대가 계속 움직여야 한다. */
const TICK_MS = 1000;

/** 작업 현황 줄이 한 줄씩 나타나는 간격. 같은 초에 일어난 이벤트 여러 개가 한 폴링에
    함께 실려 와도(2026-08-10 사용자 지적: 3줄이 동시에 뜸) 화면에는 차례로 나타난다 —
    표시 지연일 뿐, 줄 내용과 시각은 서버 사실 그대로다. */
const ACTIVITY_REVEAL_MS = 350;

/**
 * 한 단계가 이만큼 넘게 그대로면 "예상보다 오래 걸린다"고 말한다.
 *
 * 실측에서 가장 긴 단계(카드 이미지)가 약 85초다. 3분은 그 두 배를 넘는 시간이라,
 * 정상적인 지연을 멈춤이라고 부르지 않으면서도 사용자가 하염없이 기다리지는 않게 한다.
 * 진행을 막지는 않는다 — 계속 기다릴지 다시 시작할지는 사용자가 정한다.
 */
const SLOW_STEP_MS = 3 * 60 * 1000;

/** 작업 현황 로그 줄의 시각 표기(HH:MM:SS). 값이 이상하면 빈 문자열 — 줄은 그대로 보인다. */
function clockLabel(iso: string): string {
  const time = new Date(iso);
  return Number.isNaN(time.getTime()) ? "" : time.toTimeString().slice(0, 8);
}

/**
 * Generation is not something the user asks for here — confirming the verify
 * popup is the ask. This step exists to show the run happening and then the
 * result, so it starts the draft itself on arrival.
 *
 * The steps shown are the ones the server reports it is on. They used to advance
 * on a 1.8s timer that had nothing to do with the work, so a draft that took a
 * minute showed a finished-looking list for most of it.
 */
export function StepDraft() {
  const { task, followTask, setTask, setStep, draftAutoStart, showToast, reportError } =
    useStore();

  const [busy, setBusy] = useState<string | null>(null);
  const startedRef = useRef(false);
  // 이 화면에서 생성을 시작했거나 진행 중인 작업을 따라가다가, 원고 없이 끝났다. 서버가
  // FAILED로 표시하지 못한 채 멈추는 경우가 실제로 있어서(아래 stalled 참고) 화면이 스스로
  // 판단해야 한다. 사유는 사용자에게 그대로 보여 준다.
  const [stalledReason, setStalledReason] = useState<string | null>(null);

  const hasDraft = Boolean(task?.finalPost);
  const running = task?.status === "GENERATING";
  const failed = task?.status === "FAILED" && !hasDraft;

  /**
   * 돌고 있지도, 실패도, 완성도 아니다 — 화면만 '생성 준비 중'에 남아 있는 상태.
   *
   * 서버가 재시작하면 복구 스위퍼가 GENERATING인 글을 INTENT_SELECTED로 되돌리고 진행
   * 상황을 지운다(modules/blog_task/recovery.py). 그러면 이 화면은 아무 일도 일어나지
   * 않는 0%에 영원히 머물렀다 — 이미 시작한 뒤라 자동 재시작도 하지 않고, 다시 시도
   * 버튼은 FAILED에만 있었기 때문이다. 그 구멍을 여기서 메운다.
   *
   * **판정 근거는 stalledReason 하나뿐이다.** 예전에는 `startedRef.current ||`가 함께
   * 걸려 있었는데, 그 ref는 '중복 생성을 막는 표시'이지 '멈췄다는 증거'가 아니다.
   * generate()가 POST를 보내기 **전에** ref를 세우므로(중복 방지가 그래야 한다), 202가
   * 돌아오기까지의 정상 구간 내내 이 식이 참이 됐다 — 그 사이 화면은 "원고 생성 멈춤 /
   * 확인 필요 / 0% / 다시 시도해 주세요"를 보여 주고, 정작 그 버튼은 busy라 눌리지도
   * 않았다. 사용자가 본 '계속 멈춤'이 이것이다(2026-08-03).
   *
   * 멈춤은 관찰된 사실일 때만 참이어야 한다: 폴링이 원고 없이 끝났거나(waitForResult),
   * 시작 자체가 실패했거나(generate의 catch). 둘 다 stalledReason을 채운다.
   */
  const stalled = !hasDraft && !running && !failed && stalledReason !== null;

  // A draft that failed is retryable: the API un-fails the post and runs M4 again.
  const canGenerate = task?.status === "INTENT_SELECTED" || failed;

  const progress = task?.progress;
  const steps = progress?.steps ?? DRAFT_FLOW_STEPS;
  // step is 1-based on the wire; -1 means nothing is running.
  const currentIndex = progress ? progress.step - 1 : -1;

  // 서버는 단계가 바뀔 때만 알려 주고 한 단계가 1분 넘게 걸린다. 폴링만으로는 그 사이에
  // 화면이 한 픽셀도 움직이지 않아 멈춘 것처럼 보이므로, 초마다 다시 그려 경과 시간과
  // 막대가 실제로 흐르게 한다. 돌고 있을 때만 켠다.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, [running]);

  // 단계가 넘어갈 때 방금 끝난 단계가 **실제로** 얼마나 걸렸는지 기록한다(2026-08-11).
  // 막대의 몫은 원래 하드코딩된 추측이었는데, 회선·모델·이미지 장수에 따라 사람마다
  // 달라서 하나의 상수로는 맞출 수 없다. 재는 값은 서버가 준 두 단계 전환 시각의 차이라
  // 클라이언트 시계·탭 정지와 무관하다.
  const lastStepRef = useRef<{ index: number; startedAt: string } | null>(null);
  useEffect(() => {
    const startedAt = progress?.updatedAt;
    if (currentIndex < 0 || !startedAt) {
      lastStepRef.current = null;
      return;
    }
    const previous = lastStepRef.current;
    lastStepRef.current = { index: currentIndex, startedAt };
    if (!previous || previous.index === currentIndex) return;
    // 되돌아간 경우(재시작)는 재지 않는다 — 그 구간은 한 단계의 소요가 아니다.
    if (currentIndex < previous.index) return;
    const seconds = (Date.parse(startedAt) - Date.parse(previous.startedAt)) / 1000;
    recordObservedStepSeconds(previous.index, seconds, steps.length);
  }, [currentIndex, progress?.updatedAt, steps.length]);

  // updatedAt은 지금 단계가 시작된 시각, startedAt은 원고 생성 전체가 시작된 시각이다.
  const elapsedInStepMs = elapsedSince(progress?.updatedAt, now);

  // '작업 현황' 로그(2026-08-10 사용자 요청). 폴링 status가 실어 온 사용자 문구 줄들을
  // 그대로 보여 주고, 새 줄이 오면 목록 끝으로 따라 내려간다 — 기다리는 동안 지금 무슨
  // 일이 도는지 터미널 로그처럼 읽힌다.
  const activity = task?.activityLog ?? [];
  // 한 폴링에 여러 줄이 와도 한 줄씩 드러낸다. 새 실행이 시작돼 로그가 줄면 따라 줄인다.
  const [revealedCount, setRevealedCount] = useState(0);
  useEffect(() => {
    if (activity.length < revealedCount) {
      setRevealedCount(activity.length);
      return;
    }
    if (revealedCount >= activity.length) return;
    const timer = window.setTimeout(
      () => setRevealedCount((count) => Math.min(count + 1, activity.length)),
      ACTIVITY_REVEAL_MS,
    );
    return () => window.clearTimeout(timer);
  }, [activity.length, revealedCount]);
  const visibleActivity = activity.slice(0, revealedCount);

  const activityListRef = useRef<HTMLOListElement | null>(null);
  useEffect(() => {
    const list = activityListRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [revealedCount]);
  const progressInput = {
    steps,
    stepIndex: currentIndex,
    elapsedInStepMs,
    // 개수를 아는 단계(이미지 생성)는 짐작하지 않는다 — 서버가 센 값을 그대로 쓴다.
    unitsDone: progress?.unitsDone,
    unitsTotal: progress?.unitsTotal,
    // 서버가 이 환경에서 실제로 잰 단계 소요. 없으면 계산기가 기본 상수를 쓴다.
    stepSeconds: progress?.stepSeconds,
    done: hasDraft,
    failed,
  };
  const percent = draftProgressPercent(progressInput);
  const elapsedTotalMs = elapsedSince(progress?.startedAt, now);

  async function waitForResult(postId: string) {
    const settled = await followTask(postId);
    if (!settled) {
      setStalledReason("서버 상태를 오래 확인하지 못했습니다. 연결이 끊겼을 수 있어요.");
      showToast("원고 생성 상태를 오래 확인하지 못했습니다. 잠시 후 다시 열어 주세요.", true);
      return;
    }
    if (settled.status === "FAILED" || !settled.finalPost) {
      // 서버가 FAILED로 표시했든(실패) 표시하지 못했든(재시작 복구) 사용자에게는 같은
      // 사실이다: 원고가 나오지 않았고, 지금은 아무것도 돌고 있지 않다.
      setStalledReason(
        settled.status === "FAILED"
          ? "생성이 중간에 실패했습니다."
          : "서버에서 생성이 중단되었습니다. 서버가 다시 시작되면 진행 중이던 작업이 멈춥니다.",
      );
      showToast("원고 생성에 실패했습니다. 다시 시도해 주세요.", true);
      return;
    }
    setStalledReason(null);
    showToast("원고를 만들었습니다.");
  }

  async function generate() {
    if (!task || startedRef.current) return;

    startedRef.current = true;
    setBusy("generate");
    try {
      // 202: the draft is now being written behind this response.
      const started = await request<BlogTask>(`/posts/${task.postId}/draft/generate`, {
        method: "POST",
        body: { format: "html" },
      });
      setTask(started);

      await waitForResult(started.postId);
    } catch (error) {
      // 409는 오류가 아니라 **진행 중**이라는 서버의 답이다("이 글은 이미 원고를
      // 생성하고 있습니다"). 그런데 지금까지는 다른 실패와 똑같이 토스트만 띄우고
      // 끝냈다 — 폴링을 다시 붙이지 않으므로 화면은 계속 '멈춤'에 남고, 사용자는
      // 다시 누르고 또 409를 받는다(실사례: 409 세 번 연속, 그동안 서버는 3/4단계를
      // 정상 진행 중이었다).
      //
      // 여기서 할 일은 알리는 것이 아니라 **따라붙는 것**이다.
      if (error instanceof ApiError && error.status === 409) {
        setStalledReason(null);
        try {
          // 폴링은 첫 조회까지 한 박자 쉬므로, 지금 상태를 한 번 읽어 화면을 곧바로
          // '진행 중'으로 되돌린다. 실패해도 곧 폴링이 다시 읽는다.
          setTask(await request<BlogTask>(`/posts/${task.postId}`));
        } catch {
          /* 폴링이 이어서 읽는다 */
        }
        await waitForResult(task.postId);
        return;
      }
      reportError(error);
      // 시작 자체가 실패했다. 예전에는 stalled 식의 `startedRef.current ||`가 이 구멍을
      // 덮고 있었는데(그래서 정상 구간까지 멈춤으로 보였다), 이제는 사유를 분명히 적는다.
      // 이것이 없으면 화면이 0%인 채로 다시 시도 버튼도 없이 남는다.
      setStalledReason("원고 생성을 시작하지 못했습니다. 다시 시도해 주세요.");
    } finally {
      setBusy(null);
    }
  }

  async function resume(postId: string) {
    if (startedRef.current) return;

    startedRef.current = true;
    setBusy("generate");
    try {
      // 새로고침하거나 글 목록에서 다시 들어온 경우다. 서버 작업은 이미 돌고 있으므로
      // 생성 POST를 반복하지 않고 기존 post만 따라간다.
      await waitForResult(postId);
    } catch (error) {
      reportError(error);
    } finally {
      setBusy(null);
    }
  }

  function retry() {
    startedRef.current = false;
    setStalledReason(null);
    void generate();
  }

  /**
   * 저절로 시작하는 것은 **검증에서 방금 넘어왔을 때뿐이다**(2026-08-12 사용자 신고).
   *
   * 예전에는 이 화면에 오기만 하면 시작했다. 그래서 서버가 꺼졌다 켜진 뒤 목록에서 반쯤
   * 만든 글을 열면 중단된 자리에서 저절로 이어 돌았고, 사용자가 새 글을 쓰는 동안 옛 글의
   * 원고 생성이 함께 도는 일이 생겼다(실제로 그랬다 — 로그에 다른 글의 생성이 찍혔다).
   *
   * ``running``(서버가 지금 만들고 있다)일 때는 자동 시작 여부와 무관하게 **따라붙는다.**
   * 이미 도는 일을 화면이 모른 척하면 진행 상황을 볼 수 없고, 따라붙는 것 자체는 새 일을
   * 시작하지 않는다.
   */
  useEffect(() => {
    if (!task || hasDraft || failed || startedRef.current) return;
    if (running) {
      void resume(task.postId);
    } else if (canGenerate && draftAutoStart) {
      void generate();
    }
    // generate()/resume() are guarded by startedRef, so they are safe to leave out
    // of the deps. A failed run is excluded so it does not retry itself forever.
  }, [canGenerate, draftAutoStart, failed, hasDraft, running, task?.postId]);

  // 한 단계에 오래 머물러 있다. 멈춘 것은 아니지만, 아무 말도 하지 않으면 멈춘 것과
  // 구별되지 않는다. 진행은 그대로 두고 사실만 알린다.
  const slow = running && elapsedInStepMs >= SLOW_STEP_MS;

  function flowItemState(index: number): string {
    if (hasDraft) return "done";
    if (failed || stalled) return index === Math.max(currentIndex, 0) ? "error" : "pending";
    if (currentIndex < 0) return "pending";
    if (index < currentIndex) return "done";
    return index === currentIndex ? "current" : "pending";
  }

  // 화면 위쪽 세 줄(제목·배지·설명)은 같은 상태를 세 가지 층위로 말한다.
  const stopped = failed || stalled;
  /**
   * 사람이 눌러 주기를 기다리는 상태(2026-08-12). 목록에서 반쯤 만든 글을 열면 여기다.
   *
   * 만들 수는 있는데(``canGenerate``) 아직 아무도 시작하지 않았고, 저절로 시작해도 된다는
   * 신호(``draftAutoStart``)가 없다. 이때 버튼이 없으면 0%짜리 화면 앞에서 할 수 있는
   * 일이 없어진다.
   */
  const waitingForUser =
    canGenerate && !draftAutoStart && !running && !hasDraft && !stopped && !busy;
  // 실패와 멈춤은 다르다. 실패는 서버가 그렇게 표시한 것이고, 멈춤은 서버가 아무 말도
  // 남기지 못한 채 끝난 것이다 — 사용자가 할 일은 같지만 무슨 일이 있었는지는 다르다.
  const headline = failed
    ? "원고 생성 실패"
    : stalled
      ? "원고 생성 멈춤"
      : hasDraft
        ? "원고 생성 완료"
        : "원고 생성 중";
  const badge = stopped
    ? { tone: "error", text: "확인 필요" }
    : hasDraft
      ? { tone: "done", text: "완료" }
      : running
        ? { tone: "busy", text: slow ? "예상보다 오래 걸리는 중" : "AI가 초안을 만들고 있어요" }
        : { tone: "busy", text: "생성 준비 중" };
  // 진행 중 기본 안내("조금만 기다려주세요…")는 두지 않는다(2026-08-07 사용자 결정).
  // 바로 아래 진행률 막대·단계 이름·상태 배지가 이미 같은 말을 하고 있고, 상태가 바뀌지
  // 않는 동안 계속 남아 자리만 차지했다. 말할 것이 실제로 있는 상태(멈춤·완료·오래 걸림)
  // 에서만 한 줄을 낸다 — 없으면 문단 자체를 그리지 않는다(빈 줄로 자리를 남기지 않는다).
  const subtitle = stopped
    ? "지금은 아무 작업도 진행되고 있지 않습니다. 아래에서 다시 시도해 주세요."
    : hasDraft
      ? "초안이 준비되었습니다. 아래 미리보기에서 내용을 확인해 보세요."
      : slow
        ? "아직 돌고 있어요. 오래 걸릴 때가 있습니다."
        : null;

  const doneCount = hasDraft ? steps.length : Math.max(0, Math.min(currentIndex, steps.length));

  return (
    <section className="panel step-panel write-paper-card generate-panel">
      {/* No step counter here: the flow list below already shows which step is
          running, and saying it twice in two different ways only invites them to
          disagree. */}
      <div className="panel-header generate-step-header">
        <div className="panel-heading-copy">
          <p className="panel-kicker">STEP {String(WRITE_STEP.DRAFT + 1).padStart(2, "0")} · GENERATE</p>
          <div className="generate-title-row">
            <h2 className="panel-title">{headline}</h2>
            <span
              className={`generate-status-badge generate-status-badge--${badge.tone}`}
              role="status"
            >
              {badge.text}
            </span>
          </div>
          {subtitle && <p className="panel-subtitle">{subtitle}</p>}
        </div>
      </div>

      <div className="panel-body generate-panel-body">
        <section className="generate-progress-board" aria-live="polite">
          {/* 전체 진행 막대. 완료된 단계는 확정분이고, 진행 중인 단계는 머문 시간만큼
              천천히 차오른다(draftProgress). 100%는 원고가 실제로 나왔을 때만 나온다. */}
          <div
            className={`generate-progress-summary${stopped ? " failed" : ""}${
              hasDraft ? " done" : ""
            }`}
          >
            <div className="generate-progress-head">
              <span className="generate-progress-caption">전체 진행률</span>
              <strong className="generate-progress-percent">{percent}%</strong>
            </div>
            <div
              className="generate-progress-track"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              aria-label="원고 생성 진행률"
            >
              <span className="generate-progress-fill" style={{ width: `${percent}%` }} />
            </div>
            <div className="generate-progress-meta">
              {/* "1/4 · 지금 하는 일" 한 줄은 두지 않는다(2026-08-10 사용자 요청) —
                  같은 말이 바로 아래 '작업 현황' 로그와 단계 표시줄에 이미 있다. */}
              <span className="generate-progress-counts">
                {hasDraft ? (
                  // 완료되면 도중의 단계 수 대신 실제로 걸린 총 시간을 보여준다 — 진행 중에는
                  // 경과 시간을 보여주지 않다가(남은 시간을 모르는데 흐르는 숫자만 있으면
                  // 오히려 불안하다), 끝난 뒤에는 사실로 확정된 총 소요 시간만 보여준다.
                  elapsedTotalMs > 0 && (
                    <span className="generate-progress-elapsed">
                      총 소요 시간 {formatElapsed(elapsedTotalMs)}
                    </span>
                  )
                ) : (
                  <span className="generate-progress-stagecount">
                    {doneCount}/{steps.length} 단계 완료
                  </span>
                )}
              </span>
            </div>
          </div>

          {/* 단계 표시줄 — 상단 위저드(.stepper)와 같은 문법이다(2026-08-10 사용자 요청).
              몇 단계 중 어디인지는 여기서 읽고, 그 단계 안에서 지금 무슨 일이 도는지는
              아래 '작업 현황' 로그가 말한다. 카드형 목록과 단계별 막대는 이 둘로 대체했다. */}
          <ol className="stepper generate-stepper">
            {steps.map((step, index) => {
              const state = flowItemState(index);
              return (
                <li
                  className={`step generate-stage-item generate-stage-item--${state}${
                    state === "done" ? " done" : ""
                  }`}
                  key={step}
                  aria-current={state === "current" ? "step" : undefined}
                >
                  <span className="step-number" aria-hidden="true">
                    {state === "done" ? "✓" : index + 1}
                  </span>
                  <span className="step-copy">
                    <strong>{step}</strong>
                    <span className="step-state">
                      {state === "done"
                        ? "완료"
                        : state === "current"
                          ? // 이 칸에 머문 시간은 서버가 준 단계 시작 시각에서 나온 사실이다.
                            elapsedInStepMs > 0
                            ? `진행 중 · ${formatElapsed(elapsedInStepMs)}`
                            : "진행 중"
                          : state === "error"
                            ? "다시 시도해 주세요"
                            : "대기 중"}
                    </span>
                  </span>
                </li>
              );
            })}
          </ol>

          {activity.length > 0 && (
            <section className="generate-activity" aria-label="작업 현황">
              <div className="generate-activity-head">
                작업 현황
                {running && (
                  <span className="generate-activity-live" aria-hidden="true" />
                )}
              </div>
              <ol className="generate-activity-list" ref={activityListRef}>
                {visibleActivity.map((entry, index) => (
                  <li
                    key={`${entry.at}-${index}`}
                    className={index === visibleActivity.length - 1 ? "latest" : undefined}
                  >
                    <span className="generate-activity-time">{clockLabel(entry.at)}</span>
                    <span className="generate-activity-text">{entry.message}</span>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {!hasDraft && !stopped && !slow && (
            <p className="generate-tip">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="m12 4 1.6 3.6 3.9.4-2.9 2.6.8 3.8-3.4-1.9-3.4 1.9.8-3.8L6.5 8l3.9-.4L12 4Z" />
              </svg>
              AI가 더 좋은 글을 만들기 위해 여러 단계를 거치고 있어요. 잠시만 기다려 주세요!
            </p>
          )}

          {/* 오래 걸리는 중. 멈춤이 아니라는 것과, 그래도 다시 시작할 수 있다는 것을
              함께 말한다 — 둘 중 하나만 말하면 사용자는 계속 기다릴지 판단할 수 없다. */}
          {slow && !hasDraft && (
            <p className="generate-tip generate-tip--slow" role="status">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z" />
              </svg>
              {/* **'아직 작업 중'이라고 단정하지 않는다.** 서버가 재시작하면 잡은 죽는데
                  글의 상태는 '생성 중'에 그대로 남는다 — 그때도 이 화면은 시간만 계속
                  세면서 돌고 있다고 말했다(2026-08-06 신고: 진행 기록은 13분째 멈춰
                  있었다). 이 화면은 그 둘을 구분할 수 없으므로, 아는 사실(얼마나
                  지났는가)만 말하고 판단은 사용자에게 맡긴다. */}
              {/* '이미지 생성이'라고 못 박지 않는다 — 이 안내는 어느 단계에서든 뜬다
                  (2026-08-07 사용자 수정 요청). */}
              {`이 단계가 ${formatElapsed(elapsedInStepMs)}째 그대로예요. 오래 걸릴 때가 있지만,`}
              {" 서버가 다시 시작돼 멈춘 것일 수도 있어요. 새로고침(F5)을 해주세요."}
            </p>
          )}

          {/* 멈췄다는 사실과 그 이유를 화면 안에 남긴다. 토스트는 사라지므로, 자리를
              비웠다가 돌아온 사용자에게는 아무 설명도 남지 않는다. */}
          {stopped && (
            <p className="generate-tip generate-tip--stopped" role="alert">
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M12 7.5v5.5M12 16.5h.01M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z" />
              </svg>
              {stalledReason ??
                "생성이 중간에 멈췄습니다. 지금은 아무 작업도 진행되고 있지 않습니다."}
            </p>
          )}
        </section>

        {/* 멈췄을 때, 그리고 **오래 그대로일 때**도 버튼을 둔다.
            2026-07-31에는 오래 걸리는 중에 버튼을 두지 않기로 했다 — 그때는 서버가
            'GENERATING이라 시작할 수 없다'고 거절해서, 누를 수 있는데 실패하는 버튼이
            됐기 때문이다. 그 거절이 2026-08-06에 없어졌다: 아무도 돌리고 있지 않은
            GENERATING은 명시적인 요청에 되살아난다(draft/service._require_intent_selected_task).
            그래서 버튼을 막을 이유가 사라졌고, 막아 두면 죽은 잡 앞에서 사용자가 할 수
            있는 일이 없다 — 실제로 그렇게 갇혔다. */}
        {waitingForUser && (
          <div className="draft-waiting">
            <p className="draft-waiting-lead">
              이 글은 아직 원고를 만들지 않았습니다. 시작하면 5~8분쯤 걸려요.
            </p>
            <div className="actions generate-actions">
              <button
                className="button primary"
                type="button"
                id="startDraft"
                onClick={retry}
              >
                원고 생성 시작
              </button>
            </div>
          </div>
        )}

        {(stopped || (slow && !hasDraft)) && (
          <div className="actions generate-actions">
            <button
              className="button primary"
              type="button"
              id="retryDraft"
              onClick={retry}
              disabled={!!busy}
            >
              다시 생성하기
            </button>
          </div>
        )}

        {/* 문체 다듬기 lived here. Editing the draft by hand in the preview says
            exactly what you want, where asking a model to "다듬어" says roughly. */}
        {hasDraft && (
          <div className="actions generate-actions">
            <button
              className="button primary"
              type="button"
              id="goPublish"
              onClick={() => setStep(WRITE_STEP.PUBLISH)}
            >
              발행하러 가기
              <span aria-hidden="true">→</span>
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
