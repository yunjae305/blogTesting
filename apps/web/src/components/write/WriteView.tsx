import { useRef, useState } from "react";

import { request } from "../../api/client";
import type { BlogTask } from "../../api/types";
import { STEPS } from "../../constants";
import { WRITE_STEP } from "../../resume";
import { maxStep, useStore } from "../../store";
import { Preview } from "./Preview";
import { StepDraft } from "./StepDraft";
import { StepPublish } from "./StepPublish";
import { StepTopic, type BriefPreview } from "./StepTopic";
import { StepTrends } from "./StepTrends";
import { Summary } from "./Summary";
import { StepVerify } from "./StepVerify";
// 처음 여는 사람이 옆에 두고 읽는 안내. 자동 포스팅과 같은 카드를 쓴다(2026-08-12).
import { WriteHelperCard } from "./WriteHelperCard";

export function WriteView() {
  const {
    task,
    postLoading,
    postLoadError,
    activePostId,
    openPost,
    setRoute,
    step,
    setStep,
    setTask,
    followTask,
    reportError,
  } = useStore();
  const [analyzing, setAnalyzing] = useState(false);
  const [briefPreview, setBriefPreview] = useState<BriefPreview | null>(null);
  // 진행 중인 검증의 회차. 제목을 다시 고르면 새 회차가 시작되고, 옛 회차는 결과 보고를
  // 포기한다 — 불리언 가드였을 때는 옛 검증이 도는 동안 제목을 다시 골라도 새 검증
  // 요청이 조용히 삼켜져, 팝업이 옛 제목의 검증을 하염없이 기다렸다.
  const analyzeRunRef = useRef(0);

  // **상태를 보고 단계를 옮기지 않는다.** 예전에는 SEARCH_ANALYZING이 되면(=제목을 고르면)
  // 무조건 검증 단계로 세우는 효과가 여기 있었다. 그것이 두 가지를 망가뜨렸다:
  //
  // 1. 제목 후보의 '사용하기'를 누른 순간 화면이 통째로 넘어갔다. 저장이 곧 이동이라
  //    고른 제목을 확인할 틈이 없었다.
  // 2. 검증에서 '제목 다시 고르기'로 돌아와도, 상태는 여전히 SEARCH_ANALYZING이라
  //    같은 효과가 다시 사용자를 앞으로 끌고 갈 수 있었다.
  //
  // 들어올 때(새로고침·이어서 쓰기) 어느 단계에 서는가는 이 효과가 아니라 resumeStep이
  // 정한다 — 그쪽이 저장된 결과를 순서대로 짚어 가장 멀리 간 지점을 찾는다. 세션 안에서의
  // 이동은 사용자가 누른 버튼이 정한다(2026-08-07).

  /** Kicks off M3 and follows it to the end, returning the refreshed task.
   *
   * M3 answers 202 — the search and the summary take about a minute between them
   * and run in the background — so the result comes from polling, not from the
   * response to this request.
   */
  async function analyzeIntent(): Promise<BlogTask | null> {
    if (!task) return null;

    // 새 회차가 시작되면 이전 회차는 무엇을 받아도 화면에 반영하지 않는다. 서버 쪽은
    // 같은 근거의 중복 요청을 알아서 무시하고(진행 중이면 no-op), 근거가 바뀐 요청은
    // 새 검증으로 받는다 — 중복 방지를 여기서 불리언으로 다시 할 필요가 없다.
    const run = ++analyzeRunRef.current;
    const superseded = () => run !== analyzeRunRef.current;
    setAnalyzing(true);
    try {
      const started = await request<BlogTask>(`/posts/${task.postId}/search/analyze`, {
        method: "POST",
      });
      if (superseded()) return null;
      setTask(started);

      const settled = await followTask(started.postId);
      // 제목을 다시 골라 새 검증이 시작됐다 — 결과 보고는 그 회차의 몫이다.
      if (superseded()) return null;
      if (!settled) {
        reportError(new Error("검증이 예상보다 오래 걸립니다. 다시 검증을 눌러주세요."));
        return null;
      }

      // The job can finish having failed. Saying nothing left the popup showing
      // "아직 검색 결과가 없습니다", which reads as an empty search rather than a
      // search that did not happen.
      if (!settled.intentValidationResult?.intentCandidates?.length) {
        reportError(new Error("자료 검색에 실패했습니다. '다시 검증'을 눌러 다시 시도해 주세요."));
        return null;
      }
      return settled;
    } catch (error) {
      if (!superseded()) reportError(error);
      return null;
    } finally {
      if (!superseded()) setAnalyzing(false);
    }
  }

  /** 제목을 골랐다: **단계는 그대로 두고** 뒤에서 분석만 돌린다.
   *
   * 예전에는 여기서 곧바로 검증 단계로 넘어갔다. 제목 후보의 '사용하기'를 누른 것뿐인데
   * 화면이 통째로 바뀌어, 고른 제목이 맞는지 확인할 틈도 다른 후보를 다시 볼 기회도
   * 없었다(2026-08-07 사용자 요청). 검증으로 넘어가는 것은 제목 단계의 '작성 전 검증
   * 열기'가 맡는다 — 분석은 그 사이에 이미 돌고 있으므로 기다림이 늘지 않는다.
   */
  async function afterTopicChosen() {
    await analyzeIntent();
  }

  /** 검증 단계의 '제목 다시 고르기': **돌고 있는 검증을 멈추고** 제목 단계로 돌아간다.
   *
   * 회차 번호를 올리면 진행 중이던 검증은 무엇을 받아도 화면에 반영하지 않는다
   * (analyzeIntent의 superseded). 그대로 두면 제목을 다시 고르는 동안 옛 제목의 검증
   * 결과가 뒤늦게 도착해 화면을 덮고, 스피너도 계속 돈다(2026-08-07 사용자 지적).
   *
   * 서버의 검증 잡을 중단시키지는 않는다 — 멈출 방법이 없고, 멈출 이유도 약하다.
   * 같은 제목으로 다시 들어오면 그 잡의 결과를 그대로 쓰고(진행 중이면 기다린다),
   * 다른 제목을 고르면 근거가 바뀌어 그 잡은 결과를 저장하지 않고 끝난다.
   */
  async function backToTitle() {
    // 화면 쪽 회차를 먼저 올린다 — 아래 요청을 기다리는 동안 옛 검증 결과가 도착해도
    // 반영되지 않게.
    analyzeRunRef.current += 1;
    setAnalyzing(false);
    setStep(WRITE_STEP.TITLE);
    if (!task) return;
    try {
      // **서버에서도 실제로 멈춘다.** 화면에서만 무시하면 잡은 계속 돌면서 Google
      // 검색과 LLM을 끝까지 쓴다(2026-08-07 사용자 지적).
      await request(`/posts/${task.postId}/search/analyze/cancel`, { method: "POST" });
    } catch {
      // 멈추지 못해도 사용자를 붙잡지 않는다. 제목을 다시 고르면 근거가 달라져 그 잡은
      // 결과를 저장하지 않고 끝난다 — 낭비일 뿐 틀린 글이 나오지는 않는다.
    }
  }

  // 다른 글을 여는 중이다. 서버가 그 글의 진행 상태를 돌려줄 때까지는 아무 단계도
  // 그리지 않는다 — 직전 글의 소재·제목·원고가 한 프레임이라도 비치면 안 된다.
  if (postLoading && !task) {
    return (
      <section className="write-page" aria-label="새 글 작성 워크스페이스" aria-busy="true">
        <div className="panel">
          <div className="panel-body loading-state" role="status">
            <span className="spinner" aria-hidden="true" /> 저장된 진행 상태를 불러오는 중입니다.
          </div>
        </div>
      </section>
    );
  }

  /**
   * 그 글을 **열지 못했다.** 빈 '새 글 작성'을 그리지 않는다.
   *
   * 예전에는 조회가 실패하면 task가 null인 채로 남아 소재 입력 폼이 그려졌다.
   * 사용자는 목록에서 '발행하기'를 눌렀을 뿐인데 아무것도 없는 새 글 앞에 서게 되고,
   * 그 글이 사라진 것처럼 보인다(2026-08-06 신고 — Mongo 조회가 20초 만에 시간 초과해
   * 500이 났다). 글은 멀쩡히 있고 한 번의 조회가 실패했을 뿐이라, 그 사실을 말하고
   * 다시 시도할 길을 준다.
   */
  if (!task && activePostId && postLoadError) {
    return (
      <section className="write-page" aria-label="새 글 작성 워크스페이스">
        <div className="panel">
          <div className="panel-body post-load-error" role="alert">
            <h2>글을 열지 못했습니다</h2>
            <p className="subtle">{postLoadError}</p>
            <p className="subtle">
              글은 그대로 있습니다. 잠시 뒤 다시 시도하거나 목록에서 다시 열어 주세요.
            </p>
            <div className="actions">
              <button
                className="button primary"
                type="button"
                onClick={() => void openPost(activePostId)}
              >
                다시 시도
              </button>
              <button className="button" type="button" onClick={() => setRoute("posts")}>
                내 글 목록으로
              </button>
            </div>
          </div>
        </div>
      </section>
    );
  }

  const limit = maxStep(task);
  // 원고 작업 시각을 정해 둔 글은 이 화면에서 **검증까지만** 간다(2026-08-11 사용자 지시).
  // 원고·발행은 예약 시각에 서버가 하므로, 남은 칸을 보여 주면 이 화면에서 이어서 할 일이
  // 있는 것처럼 읽힌다. 시각을 비운 글은 예전 그대로 다섯 칸이다.
  const visibleSteps = task?.input?.scheduledRunAt
    ? STEPS.slice(0, WRITE_STEP.VERIFY + 1)
    : STEPS;
  // 소재·제목·원고 세 단계는 같은 작업실(Writing Brief / Trend Title Workshop /
  // Live Writing Progress Board) 디자인을 쓴다 — 단계 표시줄과 오른쪽 요약 메모지의
  // 종이 질감을 그대로 이어받게 하는 표식.
  const workshop = step <= WRITE_STEP.TITLE;

  return (
    <section
      className={`write-page${workshop ? " write-page--brief" : ""}`}
      aria-label="새 글 작성 워크스페이스"
    >
      <nav className="panel write-stepper-shell" aria-label="글 작성 단계">
        <div className="stepper">
          {visibleSteps.map((entry, index) => {
            // 단계마다 지금 어디쯤인지 한 단어로 알려 준다. 무엇을 하는 단계인지(hint)는
            // 자리를 두 줄로 늘리지 않도록 tooltip으로 남긴다.
            const state = index === step ? "현재 단계" : index < limit ? "완료" : "대기";
            return (
              <button
                key={entry.title}
                type="button"
                className={`step ${index < limit ? "done" : ""} ${index === step ? "current" : ""}`}
                aria-current={index === step ? "step" : undefined}
                data-step={index + 1}
                title={entry.hint}
                // Step 1 used to close once the task existed, because re-entering it
                // would have created a second post. It now edits the post in place, so
                // the user can go back and fix the input until the draft stage begins.
                //
                // 원고 단계까지 갔다는 것은 방향(selectedIntent)이 확정됐다는 뜻이라, 소재·제목·
                // 검증으로는 되돌아가지 않는다 — 원고가 그 방향으로 쓰이고 있다.
                disabled={index > limit || (limit >= WRITE_STEP.DRAFT && index < WRITE_STEP.DRAFT)}
                onClick={() => setStep(index)}
              >
                <span className="step-number" aria-hidden="true">
                  {index < limit && index !== step ? "✓" : index + 1}
                </span>
                <span className="step-copy">
                  <strong>{entry.title}</strong>
                  <span className="step-state">{state}</span>
                </span>
              </button>
            );
          })}
        </div>
      </nav>

      <div className={`split write-workspace write-workspace--step-${step + 1}`}>
        <main className="stack write-primary-column">
          {step === WRITE_STEP.TOPIC && <StepTopic onPreviewChange={setBriefPreview} />}
          {step === WRITE_STEP.TITLE && (
            <StepTrends
              onChosen={afterTopicChosen}
              onReopenVerify={() => setStep(WRITE_STEP.VERIFY)}
            />
          )}
          {step === WRITE_STEP.VERIFY && (
            <StepVerify
              onReanalyze={analyzeIntent}
              onBackToTitle={backToTitle}
              analyzing={analyzing}
            />
          )}
          {step === WRITE_STEP.DRAFT && <StepDraft />}
          {step === WRITE_STEP.PUBLISH && <StepPublish />}

          {/* 미리보기는 원고가 생긴 뒤부터 보여준다. 1·2단계(소재·제목)는 원고에 쓸 자료를
              받아 모으는 자리라 보여 줄 원고가 아직 없고, "원고를 만들면 여기에서 미리 볼 수
              있습니다"라는 빈 칸이 화면 절반을 차지하며 입력을 아래로 밀어냈다.
              4단계(발행)에도 남긴다 — 복사 실패 안내가 "미리보기에서 직접 선택해 주세요"로
              이 패널을 가리키고, 올리기 전 마지막으로 확인하는 자리이기도 하다. */}
          {step >= WRITE_STEP.DRAFT && <Preview />}
        </main>

        {/* 발행 단계에서는 요약 메모를 접는다. 여기서 할 일은 '복사할지 발행할지 고르고
            완성된 글을 마지막으로 확인하는 것'이고, 그 정보는 이미 아래 미리보기에 다 있다 —
            옆에 요약이 남아 있으면 마지막 화면이 필요 이상으로 복잡해진다. */}
        {/* 검증 단계도 뺀다. 그 화면은 입력정보를 자기 안에 접어 들고 있어 요약이
            겹치고, 팝업이던 시절에도 요약은 가려져 안 보였다. */}
        {step < WRITE_STEP.PUBLISH && step !== WRITE_STEP.VERIFY && (
          <aside
            className={`stack write-summary-column${workshop ? " brief-summary-column" : ""}`}
            aria-label="작성 중인 글 요약"
          >
            <Summary brief={workshop} draft={step === 0 ? briefPreview : null} />
            {/* 요약 **아래**다(2026-08-12 사용자 요청). 위가 '무엇을 정했는지', 아래가
                '무엇을 정해야 하는지'다 — 처음 여는 사람은 아래를 읽고 위를 채운다.
                요약이 접히는 검증·발행 단계에서는 이 칸 자체가 없어 함께 사라진다. */}
            <WriteHelperCard />
          </aside>
        )}
      </div>

    </section>
  );
}
