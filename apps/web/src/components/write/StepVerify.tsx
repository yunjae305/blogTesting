import { useEffect, useState } from "react";

import { request } from "../../api/client";
import type { BlogTask, SearchSource } from "../../api/types";
import { INTENT_CANDIDATE_COUNT, visiblePurposes } from "../../constants";
import { WRITE_STEP } from "../../resume";
import { useStore } from "../../store";

// 이 미만이면 기본 선택에서 빼둔다(점수가 실제로 매겨졌을 때만 적용).
const LOW_RELEVANCE = 40;

// 자료 성격 배지에 쓰는 한글 라벨. 분류가 없으면 '기타'.
const SOURCE_TYPE_LABELS: Record<string, string> = {
  OFFICIAL: "공식자료",
  NEWS: "뉴스",
  BLOG: "블로그·후기",
  REPORT: "통계·보고서",
  CASE: "활용 사례",
};

function sourceTypeLabel(type: SearchSource["sourceType"]): string {
  return (type && SOURCE_TYPE_LABELS[type]) || "기타";
}

/**
 * 출처 도메인. url에서 뽑아 쓰는 표시용 변환이라 없는 값을 만들어내지 않는다 — 주소가
 * 없거나 해석되지 않으면 빈 문자열이고, 그때는 도메인 칸을 비워 둔다.
 */
function sourceDomain(url: string): string {
  if (!url) return "";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** 값이 없어서 채워 넣은 자리표시자인지. 없는 값을 경고처럼 보이지 않게 흐리게 쓴다. */
const PLACEHOLDER_VALUES = new Set([
  "-",
  "없음",
  "지정 안 함",
  "선택 없음",
  "원고 생성 시 자동 생성",
]);

function InputIcon({ label }: { label: string }) {
  const common = {
    viewBox: "0 0 24 24",
    "aria-hidden": true,
    focusable: false,
  } as const;

  if (label === "글 목적") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="8" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    );
  }
  if (label === "선택 제목") {
    return (
      <svg {...common}>
        <path d="m12 4 2.4 4.8 5.3.8-3.8 3.7.9 5.3-4.8-2.5-4.8 2.5.9-5.3L4.3 9.6l5.3-.8L12 4Z" />
      </svg>
    );
  }
  if (label === "선택 키워드") {
    return (
      <svg {...common}>
        <path d="M9 4 7 20M17 4l-2 16M4 9h16M3 15h16" />
      </svg>
    );
  }
  if (label === "참고 URL") {
    return (
      <svg {...common}>
        <path d="M9.5 14.5 14.5 9M7.2 16.8l-1 1a3.5 3.5 0 0 1-5-5l3.2-3.2a3.5 3.5 0 0 1 5 0M16.8 7.2l1-1a3.5 3.5 0 0 1 5 5l-3.2 3.2a3.5 3.5 0 0 1-5 0" />
      </svg>
    );
  }
  if (label === "참고 이미지") {
    return (
      <svg {...common}>
        <path d="M16.5 11.5 11 17a3.2 3.2 0 0 1-4.5-4.5l7-7a2.2 2.2 0 0 1 3 3l-7 7a1.2 1.2 0 0 1-1.7-1.7l6-6" />
      </svg>
    );
  }
  if (label === "추가 자료") {
    return (
      <svg {...common}>
        <path d="M4 7.5A1.5 1.5 0 0 1 5.5 6h3.2l1.8 2H18a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 18 18H5.5A1.5 1.5 0 0 1 4 16.5Z" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M7 4h7l4 4v12H7zM14 4v4h4" />
      <path d="M10 13h5M10 16h5" />
    </svg>
  );
}

/**
 * 작성 전 검증 — 사용자가 준 것과 검색이 찾아온 것을 함께 보여 주고, 무거운 원고
 * 생성을 시작하기 전에 글의 방향을 고르게 한다.
 *
 * **팝업이 아니라 한 단계다.** 제목을 고른 뒤, 원고를 만들기 전에 온다. 예전에는 제목
 * 단계 위에 뜨는 팝업이었는데, 하는 일은 한 단계인데 막대에 안 보여서 지금 어디쯤인지
 * 알 수 없고 되돌아올 수도 없었다(2026-08-06 사용자 요청).
 */
export function StepVerify({
  onReanalyze,
  onBackToTitle,
  analyzing,
}: {
  /** Re-runs M3 and resolves to the refreshed task. */
  onReanalyze: () => Promise<BlogTask | null>;
  /** 돌고 있는 검증을 멈추고 제목 단계로 돌아간다. */
  onBackToTitle: () => void;
  analyzing: boolean;
}) {
  const {
    task,
    recommendation,
    setTask,
    setStep,
    setRoute,
    setDraftAutoStart,
    draftRounds,
    setDraftRounds,
    setSelectedTrendKeywordIds,
    setTopicCandidates,
    showToast,
    reportError,
  } = useStore();
  const [busy, setBusy] = useState(false);
  // 원고 작업 시각을 정해 둔 글(2026-08-11). 이 화면에서 **자료를 모으지 않는다** —
  // 수집·검증은 예약 시각에 원고를 만들면서 함께 돈다. 며칠 뒤에 쓸 글의 자료를 오늘
  // 모아 두면 그 사이에 나온 이슈가 빠지고, 그것이 이 예약의 존재 이유이기 때문이다.
  const scheduledRunAt = task?.input?.scheduledRunAt ?? "";
  const candidates = task?.intentValidationResult?.intentCandidates ?? [];
  const [selectedIntentId, setSelectedIntentId] = useState<string>("");
  /**
   * 만들 원고 수와, 지금 몇 편째를 고르는 중인가(2026-08-12).
   *
   * 한때 이 화면에서 **한 번에** 방향 N개를 고르게 했는데, 제목도 편마다 달라야 한다는
   * 것이 드러나 ②③을 편수만큼 도는 방식으로 바꿨다. 그래서 여기서 고르는 것은 늘 하나다.
   */
  const draftCount = Math.max(1, task?.input?.draftCount ?? 1);
  /**
   * **자료를 지금 모으지 않는 글**인가. 가르는 것은 **작업 시각을 정했는가** 하나다.
   *
   * 시각을 정한 글만 나중에 모은다 — 며칠 뒤에 쓸 글의 자료를 오늘 모으면 그 사이에
   * 나온 이슈가 빠지고, 그것이 그 예약의 존재 이유다(2026-08-11).
   *
   * 편수 조건은 걷어냈다(2026-08-13 사용자 지적: "수집한 자료가 보여져야지"). 2026-08-12
   * 에는 2편 이상이면 나중에 모았는데, 그때는 여러 편이 작업 큐에서 순서대로 돌아 원고
   * 시점이 한참 뒤였다. 지금은 시각을 정하지 않은 여러 편이 곧바로 함께 돌고, 검증에서
   * 모은 자료가 그대로 원고에 쓰인다.
   *
   * 서버의 `_collects_sources_now`와 **같은 규칙이어야 한다** — 어긋나면 화면이
   * "나중에 모읍니다"라고 적는 동안 서버가 모으고 있게 된다.
   */
  const sourcesLater = Boolean(scheduledRunAt);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());

  const rows = verifyInputRows(task, recommendation?.trendKeywords ?? []);
  const search = verifySearchSummary(task);
  // M2에서 고른 제목. 고르지 않고 넘어갔으면 빈 문자열 — 그때는 원고가 제목을 짓는다.
  const confirmedTitle = confirmedTitleOf(task);

  // 팝업일 때는 여기서 바디를 잠그고, 포커스를 안으로 가두고, Escape로 닫았다.
  // 한 단계가 된 뒤로는 셋 다 필요 없다 — 화면 전체가 이 단계다.

  const selectedIntent =
    candidates.find((candidate) => candidate.intentId === selectedIntentId) ?? candidates[0];
  const draftSources = selectedIntent?.sources ?? [];
  // 검색이 실제로 찾아 온 자료의 총 개수. 옛 검증 결과에는 없다(0).
  const collectedTotal = task?.intentValidationResult?.collectedSourceCount ?? 0;

  useEffect(() => {
    if (!candidates.some((candidate) => candidate.intentId === selectedIntentId)) {
      setSelectedIntentId(candidates[0]?.intentId ?? "");
    }
  }, [candidates, selectedIntentId]);

  // 관련도 높은 자료가 먼저 오도록 정렬해서 보여주고 기본 선택도 그 순서를 따른다.
  const sortedSources = [...draftSources].sort(
    (a, b) => (b.relevanceScore ?? 0) - (a.relevanceScore ?? 0),
  );

  // 새 검증이면 후보가 바뀌므로 옛 제외 목록을 버린다. 관련도 점수가 실제로 매겨진
  // 경우엔 관련도 낮은 자료(<40)를 기본 제외해 두어 사용자가 좋은 자료부터 보게 한다 —
  // 점수가 전부 0이면(옛 자료 등) 아무것도 제외하지 않는다.
  useEffect(() => {
    const scored = draftSources.filter((s) => (s.relevanceScore ?? 0) > 0);
    if (scored.length === 0) {
      setExcluded(new Set());
      return;
    }
    // 점수 0은 '무관'이 아니라 '채점되지 않음'이다(모델이 고르지 않아 수집 자료에서
    // 채운 항목 등). 실제로 채점된 것 중 낮은 것만 기본 제외한다 — 안 그러면 멀쩡한
    // 자료가 이유 없이 체크 해제된 채로 보인다.
    const low = draftSources
      .filter((s) => s.url && (s.relevanceScore ?? 0) > 0 && s.relevanceScore! < LOW_RELEVANCE)
      .map((s) => s.url);
    setExcluded(new Set(low));
    // draftSources는 선택한 의도가 바뀌면 함께 바뀌므로 intentId만 의존해도 최신 값을 본다.
  }, [selectedIntent?.intentId]);

  function toggleSource(url: string) {
    setExcluded((current) => {
      const next = new Set(current);
      if (next.has(url)) next.delete(url);
      else next.add(url);
      return next;
    });
  }

  /**
   * 지금 몇 편째를 고르는 중인가.
   *
   * **끝난 라운드 수로 센다** — 배열 길이가 아니다(2026-08-12 사용자 신고: "첫번째꺼
   * 글 쓰는 건데 2번째 글 쓰는 거로 기록되는 것 같아"). ②가 1편째 제목을 담으면 길이가
   * 1이 되는데, 그때 ③은 아직 1편째다. 한 라운드는 **방향까지 골라야** 끝난다.
   */
  const roundIndex = draftRounds.filter((round) => round.intentId).length;

  async function confirm() {
    if (!task || !selectedIntent) return;

    setBusy(true);
    try {
      // 화면에 떠 있는 방향(intent) 후보가 서버의 현재 후보와 어긋나 있을 수 있다. '다시
      // 검증'이 백그라운드에서 요약/검색에 실패하면 후보가 3개에서 1개짜리 폴백으로 줄어드는데,
      // 폴링이 먼저 끝나 화면에는 직전 성공본(3개)이 그대로 남곤 한다. 그 상태로 옛 방향을
      // 제출하면 서버가 "그 방향은 이제 없다"며 원고 생성을 통째로 거절한다(생성 실패). 무거운
      // 생성을 시작하기 직전에 최신 글을 한 번 더 확인해, 고른 방향이 아직 있으면 진행하고,
      // 사라졌으면 최신 후보로 화면을 갱신한 뒤 다시 고르게 한다 — 막다른 실패를 없앤다.
      const fresh = await request<BlogTask>(`/posts/${task.postId}`);
      const freshCandidates = fresh.intentValidationResult?.intentCandidates ?? [];
      if (!freshCandidates.some((candidate) => candidate.intentId === selectedIntent.intentId)) {
        setTask(fresh);
        showToast("검증 결과가 갱신되었어요. 글 방향을 다시 선택해 주세요.", true);
        return;
      }

      // 여러 편을 만들 때는 **서버에 저장하지 않고** 이 라운드의 방향만 기억한다
      // (2026-08-12). 글 하나에는 방향 자리가 하나뿐이라 다음 라운드가 덮어쓴다 —
      // 마지막 라운드에서 모아 둔 짝 전부를 한 번에 보낸다.
      if (draftCount > 1) {
        const next = [...draftRounds];
        next[roundIndex] = {
          ...next[roundIndex],
          keywords: next[roundIndex]?.keywords ?? [],
          intentId: selectedIntent.intentId,
          intentTitle: selectedIntent.title,
          // 고른 방향을 통째로 들고 간다 — 자리번호만으로는 서버가 이 편이 무엇을 골랐는지
          // 되찾을 수 없다(store.DraftRound.intent 주석).
          intent: selectedIntent,
        };
        setDraftRounds(next);

        // 끝난 라운드 수로 센다 — 방금 이 라운드가 방향까지 골라 끝났다.
        const done = next.filter((round) => round.intentId).length;
        if (done < draftCount) {
          // 아직 남았다 — 제목 단계로 돌아가 다음 편을 고른다.
          //
          // **앞 편의 선택을 지우고 보낸다**(2026-08-12 사용자 신고: 3편째 키워드 단계에서
          // '트렌드 없이 소재만으로'를 누르니 "선택한 트렌드 키워드 'AIONA 마블'가
          // 해제됩니다"라고 물었다 — 2편째에 고른 것이다). 글 하나에는 트렌드 선택 자리가
          // 하나뿐이라 서버의 trendSelection에는 앞 편의 키워드·제목이 그대로 남아 있고,
          // 화면 쪽 선택과 제목 후보까지 남으면 다음 편이 '이미 고른 화면'으로 열린다.
          // 편마다 처음부터 고르는 것이 이 흐름의 전제다.
          setSelectedTrendKeywordIds([], true);
          setTopicCandidates([], "");
          showToast(`${done + 1}편째 제목을 골라 주세요.`);
          setStep(WRITE_STEP.TITLE);
          return;
        }

        // 마지막이다. 첫 편은 원본 글에 적용하고 나머지는 복제한다(서버가 한다).
        await request(`/posts/${task.postId}/schedule`, {
          method: "POST",
          body: {
            primaryDraft: {
              intentId: next[0].intentId,
              title: next[0].title,
              intent: next[0].intent,
            },
            additionalDrafts: next.slice(1).map((round) => ({
              intentId: round.intentId,
              title: round.title,
              intent: round.intent,
            })),
          },
        });
        showToast(`${draftCount}편을 작업 큐에 걸었습니다.`);
        setDraftRounds([]);
        setRoute("scheduled");
        return;
      }

      const updated = await request<BlogTask>(`/posts/${task.postId}/intents/select`, {
        method: "POST",
        body: {
          intentId: selectedIntent.intentId,
          excludedSourceUrls: draftSources
            .map((source) => source.url)
            .filter((url) => url && excluded.has(url)),
        },
      });
      setTask(updated);

      // 작업 시각을 정해 둔 글은 여기서 예약으로 넘어간다(2026-08-11). 방향까지가 사람이
      // 정할 몫이고, 원고는 그 시각에 만든다 — 지금 만들면 그 사이에 나온 이슈가 빠진다.
      // 오늘 모은 자료는 그때 새로 모은 것으로 갈린다(service._refresh_sources).
      if (updated.input?.scheduledRunAt) {
        await request(`/posts/${task.postId}/schedule`, {
          method: "POST",
          // 한 편짜리 예약이다. 여러 편은 위의 라운드 흐름이 이미 보냈다.
          body: {},
        });
        showToast("예약했어요. 지정한 시각에 자료를 새로 모아 원고를 만듭니다.");
        setRoute("scheduled");
        return;
      }

      // Confirming is the go-ahead: the 원고 step starts generating on arrival.
      // **여기서 넘어갈 때만** 원고 생성이 저절로 시작된다(2026-08-12). 목록에서 연 글은
      // 사람이 '원고 생성 시작'을 누를 때까지 기다린다 — 옛 글의 생성이 새 글을 쓰는
      // 동안 함께 도는 일을 막는다.
      setDraftAutoStart(true);
      showToast("원고 생성을 시작합니다.");
      setStep(WRITE_STEP.DRAFT);
    } catch (error) {
      reportError(error);
    } finally {
      setBusy(false);
    }
  }

  // 검증이 끝났다 = 고를 방향이 화면에 있다. 이 하나로 팝업의 두 얼굴(검색 중 / 검증 완료)이
  // 갈린다 — 별도의 상태를 새로 만들지 않고 기존 데이터가 있는지로만 판단한다.
  //
  // **실패는 예외다**(2026-08-12 사용자 신고: "방향 4가지 보여주는거 어디갔어"). 자료 수집이
  // 실패하면 서버가 실패 사유를 담은 후보 **한 장**을 만들어 보낸다(_failed_validation_result).
  // 그것을 개수로만 세면 화면이 '검증 완료'라고 말하면서 실패 안내를 '방향 1'이라는 고를 수
  // 있는 카드로 그린다 — 기능이 망가진 것처럼 읽힌다. 실패는 실패라고 말해야 한다.
  const failed =
    candidates.length === 1 && candidates[0].intentId.endsWith("_intent_failed");
  const verified = candidates.length > 0 && !failed;
  const status = analyzing
    ? {
        tone: "busy",
        text: sourcesLater
          ? "Gemini가 글의 방향을 세분화하는 중이에요"
          : (task?.progress?.label ?? "자료 검색"),
      }
    : failed
      ? { tone: "idle", text: "검증 실패" }
      : verified
        ? { tone: "done", text: "검증 완료" }
        : { tone: "idle", text: "검증 필요" };

  const inputGrid = (
    <div className="prewriting-input-grid">
      {rows.map(([label, value]) => {
        const empty = PLACEHOLDER_VALUES.has(value);
        return (
          <div
            className={`prewriting-input-card${label === "선택 제목" ? " prewriting-input-card--title" : ""}`}
            key={label}
          >
            <span className="prewriting-input-label">
              <InputIcon label={label} />
              {label}
            </span>
            <strong className={`prewriting-input-value${empty ? " is-empty" : ""}`}>{value}</strong>
          </div>
        );
      })}
    </div>
  );

  return (
    <>
      <section
        className="panel write-verify-panel prewriting-modal"
        aria-labelledby="verifyTitle"
        aria-describedby="verifyIntro"
      >
        <div className="verify-dialog-header prewriting-modal__header">
          <div className="panel-heading-copy prewriting-modal__heading">
            <p className="verify-kicker">BEFORE WRITING</p>
            <h2 id="verifyTitle">작성 전 검증</h2>
            <p id="verifyIntro" className="panel-subtitle">
              찾은 자료와 글의 방향을 확인하면 원고 생성을 시작합니다.
            </p>
          </div>

          <div className="prewriting-status-group">
            {/* 상태 표시와 재시도는 하는 일이 다르므로 나눠 둔다 — 상태 칸이 '다시 검증'
                버튼처럼 보이면 완료 상태를 누르려 하게 된다. */}
            <p
              className={`prewriting-status prewriting-status--${status.tone}`}
              role="status"
              aria-live="polite"
            >
              {analyzing ? (
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <circle cx="11" cy="11" r="6.5" />
                  <path d="m16 16 4 4" />
                </svg>
              ) : verified ? (
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <path d="m5 12.5 4.5 4.5L19 7.5" />
                </svg>
              ) : (
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <circle cx="12" cy="12" r="8" />
                  <path d="M12 8v5M12 16h.01" />
                </svg>
              )}
              <span>{status.text}</span>
              {analyzing && <span className="prewriting-status__spinner" aria-hidden="true" />}
            </p>
            <button
              className="button small prewriting-retry"
              type="button"
              onClick={onReanalyze}
              disabled={analyzing || busy}
            >
              다시 검증
            </button>
          </div>


        </div>

        <div className="verify-dialog-body prewriting-modal__body">
          {/* 검증이 끝나기 전에는 무엇을 근거로 검색하는지가 이 팝업의 본문이다. 끝난 뒤에는
              방향·자료 고르기가 본문이 되므로, 입력정보는 접어 두되 없애지는 않는다 —
              무엇으로 쓰는 글인지 확인하고 싶은 사람이 열어 볼 수 있어야 한다. */}
          {verified ? (
            <details className="prewriting-input-fold">
              <summary>사용자 입력정보 보기</summary>
              {inputGrid}
            </details>
          ) : (
            <section className="prewriting-section">
              <h3 className="prewriting-section-title">사용자 입력정보</h3>
              {inputGrid}
            </section>
          )}

          {/* 실패는 방향 자리에 **실패라고** 적는다. 고를 수 있는 카드처럼 그리면 사용자는
              "방향 4개는 어디 갔냐"로 읽는다(2026-08-12 신고) — 후보가 줄어든 것이 아니라
              자료 수집이 실패해 후보를 만들지 못한 것이다. */}
          {failed && (
            <section className="prewriting-section">
              <div className="prewriting-failed" role="alert">
                <h3>글의 방향을 만들지 못했습니다</h3>
                <p className="prewriting-failed-reason">{candidates[0].rationale}</p>
                <p className="prewriting-failed-hint">
                  방향 후보 {INTENT_CANDIDATE_COUNT}개는 자료를 모은 뒤에 만들어집니다. 위
                  사유를 확인하고 <b>'다시 검증'</b>을 눌러 주세요. 그대로 진행하면 자료 없이
                  원고를 만듭니다.
                </p>
              </div>
            </section>
          )}

          {verified && (
            <section className="prewriting-section">
              <h3 className="prewriting-section-title">
                <span className="prewriting-section-num" aria-hidden="true">
                  1
                </span>
                방향 선택
              </h3>
              {/* 방향 후보를 자기가 고른 제목으로 착각하는 일이 있었다. 카드 위에 확정
                  제목을 먼저 못박아, 아래 목록이 제목이 아니라 '같은 제목을 푸는 각도'임을
                  읽기 전에 알 수 있게 한다. */}
              <div className="prewriting-anchor">
                <span className="prewriting-anchor-label">
                  {confirmedTitle ? "확정 제목" : "제목"}
                </span>
                <strong className={confirmedTitle ? "" : "prewriting-anchor-auto"}>
                  {confirmedTitle || "원고 생성 시 자동 생성"}
                </strong>
              </div>
              <p className="prewriting-section-hint">
                제목은 바뀌지 않습니다. 이 제목을 <b>어떤 각도로 풀지</b>만 고르세요.
                {draftCount > 1 && (
                  <>
                    {" "}
                    지금은 <b>{roundIndex + 1}번째 글</b>의 방향입니다. 고르면 다음 편의 제목을
                    고르러 갑니다({draftCount}편 중 {roundIndex + 1}편째).
                  </>
                )}
              </p>
              <div className="prewriting-direction-grid" role="radiogroup" aria-label="글 방향">
                {candidates.map((candidate, index) => {
                  const chosen = candidate.intentId === selectedIntent?.intentId;
                  return (
                    <label
                      className={`prewriting-direction-card${
                        chosen ? " prewriting-direction-card--selected" : ""
                      }`}
                      key={candidate.intentId}
                    >
                      <span className="prewriting-direction-head">
                        <input
                          type="radio"
                          name="intent"
                          value={candidate.intentId}
                          checked={chosen}
                          disabled={busy || analyzing}
                          onChange={() => setSelectedIntentId(candidate.intentId)}
                        />
                        {/* 제목 자리가 아니라는 것을 항목마다 먼저 말해 준다. 옛 검증 결과에는
                            제목처럼 긴 문장이 들어 있을 수 있어, 방향 배지로 구분한다. */}
                        <span className="prewriting-direction-badge">방향 {index + 1}</span>
                        {draftCount > 1 && (
                          <span className="prewriting-direction-order">
                            {roundIndex + 1}번째 글
                          </span>
                        )}
                      </span>
                      <span className="prewriting-direction-title">{candidate.title}</span>
                      <span className="prewriting-direction-reader">
                        <span className="prewriting-field-label">독자</span>
                        {candidate.targetReader}
                      </span>
                      <span className="prewriting-direction-rationale">{candidate.rationale}</span>
                    </label>
                  );
                })}
              </div>
            </section>
          )}

          <section className="prewriting-section">
            <h3 className="prewriting-section-title">
              {verified && (
                <span className="prewriting-section-num" aria-hidden="true">
                  2
                </span>
              )}
              참고 자료
            </h3>
            <div className="prewriting-source-panel">
              <div className="prewriting-source-head">
                {/* 예약 글에서는 이 단계가 자료를 다루지 않는다(2026-08-11 사용자 지적).
                    여기서 AI가 하는 일은 **방향 3가지를 세분화하는 것**뿐이고, 자료는
                    작업 시각에 모인다. 자료를 이미 고른 것처럼 읽히는 문구·개수는 뺀다. */}
                <span>
                  {sourcesLater
                    ? "고른 방향으로 원고를 생성합니다"
                    : "선택한 자료를 바탕으로 원고를 생성합니다"}
                </span>
                {!sourcesLater && draftSources.length > 0 && (
                  <span className="prewriting-source-count">
                    {draftSources.length - excluded.size}/{draftSources.length}개 사용
                  </span>
                )}
              </div>
              <strong className="prewriting-source-title">{search.title}</strong>
              {/* 예약 글에서는 근거 설명을 싣지 않는다(2026-08-11 사용자 지적). 이 문장은
                  자료를 읽고 쓴 것처럼 구체적인 사실(모델 수·기능)을 말하는데, 이 단계에서
                  사용자에게 보여 주기로 한 일은 방향을 나누는 것뿐이다. 자료는 작업
                  시각에 모이므로, 그 전에 사실을 아는 것처럼 말해서는 안 된다. */}
              {!sourcesLater && (
                <p className="prewriting-source-basis">
                  {selectedIntent?.rationale || search.description}
                </p>
              )}
              {!sourcesLater && draftSources.length > 0 && (
                <p className="prewriting-source-hint">
                  관련도 높은 자료가 기본 선택됩니다. 무관한 자료는 체크를 해제하세요 — 해제한
                  자료는 최종 원고에 사용되지 않습니다.
                </p>
              )}

              {sourcesLater ? (
                // 지금 자료를 모으지 않는 글 — **시각을 정해 둔 글뿐이다**(2026-08-13).
                // 며칠 뒤에 쓸 글의 자료를 오늘 모아 봐야 그 사이에 나온 이슈가 빠진다
                // (2026-08-11). 편수는 더 이상 보지 않는다.
                <p className="prewriting-source-empty" role="status">
                  작업 예정 시각에 자료가 함께 수집됩니다.
                </p>
              ) : analyzing && !sortedSources.length ? (
                // 검색 진행 안내. 예상 시간은 기존 문구를 그대로 옮긴 것이며, 진행률을
                // 흉내 내는 막대나 새로 계산한 남은 시간은 두지 않는다.
                <p className="prewriting-search-progress" role="status" aria-live="polite">
                  <span className="prewriting-search-progress__spinner" aria-hidden="true" />
                  <span>
                    관련 자료를 검색하고 있습니다. <b>1~2분</b> 정도 소요될 수 있어요.
                  </span>
                </p>
              ) : sortedSources.length ? (
                <ul className="prewriting-source-list">
                  {sortedSources.map((source, index) => {
                    const included = !(source.url && excluded.has(source.url));
                    const domain = sourceDomain(source.url);
                    const inputId = `prewriting-source-${index}`;
                    return (
                      <li
                        key={source.url || source.title}
                        className={`prewriting-source-item${included ? "" : " is-excluded"}`}
                      >
                        <input
                          id={inputId}
                          type="checkbox"
                          checked={included}
                          disabled={!source.url || busy}
                          onChange={() => source.url && toggleSource(source.url)}
                        />
                        {/* 링크는 label 밖에 둔다 — label 안에 있으면 주소를 누를 때마다
                            체크가 함께 뒤집혔다. */}
                        <label className="prewriting-source-body" htmlFor={inputId}>
                          <span className="prewriting-source-badge">
                            {sourceTypeLabel(source.sourceType)}
                          </span>
                          <span className="prewriting-source-domain">{domain || "출처 미상"}</span>
                          <span className="prewriting-source-desc">
                            {source.title}
                            {source.snippet && (
                              <span className="prewriting-source-snippet">{source.snippet}</span>
                            )}
                          </span>
                        </label>
                        {source.url && (
                          <a
                            className="prewriting-source-link"
                            href={source.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            aria-label={`${source.title} 새 탭에서 열기`}
                          >
                            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                              <path d="M14 5h5v5M19 5l-7.5 7.5" />
                              <path d="M18 14v4.5A1.5 1.5 0 0 1 16.5 20h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" />
                            </svg>
                          </a>
                        )}
                      </li>
                    );
                  })}
                  {/* 화면에 보이는 것은 이 방향에 붙은 자료뿐이다. 검색은 그보다 많이
                      찾아 오는데, 그 사실을 안 적으면 사용자는 "자료를 이만큼밖에 못
                      찾았나"로 읽는다(2026-08-07 신고). 옛 검증 결과에는 총 개수가
                      없으므로(0) 그때는 이 줄을 그리지 않는다 — 지어내지 않는다. */}
                  {collectedTotal > sortedSources.length && (
                    <li className="prewriting-source-more">
                      검색은 자료를 <b>총 {collectedTotal}개</b> 찾았습니다. 그중 이 방향에
                      관련도가 높은 {sortedSources.length}개입니다.
                    </li>
                  )}
                </ul>
              ) : (
                <p className="prewriting-source-empty">
                  {analyzing
                    ? "자료를 검색하고 있습니다. 1~2분 걸릴 수 있습니다."
                    : selectedIntent
                      ? // 검증이 끝났는데 자료가 없다 — 위 설명(rationale)에 실패 사유나
                        // 빈 검색 사유가 적혀 있으므로 그걸 보라고 안내하고, 막다른 길이
                        // 아님을 알린다.
                        "참고 자료를 찾지 못했습니다. 위 설명을 확인한 뒤 '다시 검증'을 누르거나, 자료 없이 계속 진행할 수 있습니다."
                      : "아직 검증 결과가 없습니다. '다시 검증'을 눌러 시도해 주세요."}
                </p>
              )}
            </div>
          </section>
        </div>

        <div className="verify-dialog-actions prewriting-modal__footer">
          {/* Otherwise the disabled button just looks broken. */}
          {!selectedIntent && !analyzing && (
            <span className="hint">검증이 끝나야 다음으로 넘어갈 수 있습니다.</span>
          )}
          {/* 팝업일 때는 '닫기'였다. 단계가 된 뒤로는 **되돌아갈 곳이 분명하다** —
              제목 단계다. 닫아 봐야 갈 데가 없다.

              돌아가면서 **돌고 있는 검증을 멈춘다**(2026-08-07 사용자 지적). 그 검증의
              결과는 어느 쪽이든 쓰이지 않는다 — 제목을 바꾸면 근거가 달라져 다시 돌려야
              하고, 같은 제목으로 돌아와도 사용자가 다시 시작한다. 그런데 그대로 두면
              잡은 계속 돌면서 검색과 LLM을 끝까지 쓴다. */}
          <button className="button" type="button" onClick={onBackToTitle} disabled={busy}>
            제목 다시 고르기
          </button>
          <button
            className="button primary prewriting-confirm"
            type="button"
            onClick={confirm}
            disabled={!selectedIntent || busy || analyzing}
          >
            {busy ? (
              <>
                <span className="spinner" aria-hidden="true" /> 처리 중
              </>
            ) : (
              <>
                {/* 검증이 실패했는데 '확인하고 계속'이라고 적으면 무엇을 확인했다는 뜻인지
                    알 수 없다. 그대로 가면 자료 없이 쓴다는 것을 버튼이 말한다. */}
                {failed ? "자료 없이 계속" : scheduledRunAt ? "예약 등록" : "확인하고 계속"}
                <span aria-hidden="true">→</span>
              </>
            )}
          </button>
        </div>
      </section>
    </>
  );
}

function selectedKeywordText(
  task: BlogTask | null,
  pool: { trendKeywordId: string; keyword: string }[],
): string {
  const selection = task?.trendSelection;
  if (!selection || selection.skipped || !selection.selectedTrendKeywordIds.length) {
    return "선택 없음";
  }
  const matched = selection.selectedTrendKeywordIds
    .map((id) => pool.find((item) => item.trendKeywordId === id)?.keyword)
    .filter(Boolean);
  return matched.length
    ? matched.join(", ")
    : `${selection.selectedTrendKeywordIds.length}개 선택됨`;
}

/**
 * M2에서 확정된 제목. 고르지 않고 넘어갔으면(skipped) 빈 문자열 — 그때는 원고 생성이
 * 제목까지 짓는다. 입력 요약과 '글 방향 선택'이 같은 규칙을 봐야 두 곳이 어긋나지 않는다.
 */
function confirmedTitleOf(task: BlogTask | null): string {
  const selection = task?.trendSelection;
  if (!selection || selection.skipped) return "";
  return selection.finalTopic || "";
}

function verifyInputRows(
  task: BlogTask | null,
  pool: { trendKeywordId: string; keyword: string }[],
): [string, string][] {
  const input = task?.input;
  const materials = input?.referenceMaterials ?? [];
  const urls = materials.filter((m) => m.type === "URL").map((m) => m.value);
  // 이미지와 문서를 갈라 센다. 예전에는 둘을 '참고 파일'로 묶어 개수만 보여 줬는데,
  // 이미지 9장과 PDF 2개가 "11개"로 뭉쳐 무엇이 실렸는지 알 수 없었다.
  const images = materials.filter((m) => m.type === "IMAGE");
  // 올린 문서(TEXT·PDF)는 **이름**으로 보여 준다. 값을 그대로 이으면 브랜드 자료 본문이
  // 통째로 카드에 쏟아진다(2026-08-06 사용자 지적).
  const documents = materials
    .filter((m) => m.type === "TEXT" || m.type === "PDF")
    .map((m) => m.name?.trim() || (m.type === "PDF" ? "이름 없는 PDF" : shortText(m.value)));
  // 제목을 안 고르고 넘어가면(skipped) 원고 생성 때 제목까지 자동 생성된다.
  // "선택 없음"은 빠뜨린 것처럼 보이므로, 자동 생성임을 명시한다.
  const confirmedTitle = confirmedTitleOf(task);

  return [
    ["소재", input?.topic || "-"],
    // 예약의 내부 기본 목적은 뺀다 — 사용자가 고른 값이 아니므로 '지정 안 함'이 맞다.
    ["글 목적", visiblePurposes(input?.purpose ?? input?.keywords).join(", ") || "지정 안 함"],
    ["선택 제목", confirmedTitle || "원고 생성 시 자동 생성"],
    ["선택 키워드", selectedKeywordText(task, pool)],
    ["참고 URL", urls.length ? urls.join(", ") : "없음"],
    ["참고 이미지", images.length ? `${images.length}장` : "없음"],
    ["추가 자료", documents.length ? documents.join(" / ") : "없음"],
    // 원고 작업 시각(2026-08-11). 비워 두면 이 화면에서 곧바로 원고를 만든다는 뜻이라
    // '즉시 생성'으로 적는다 — '없음'이라고 적으면 빠뜨린 칸처럼 보인다.
    ["작업 예정 시각", formatWorkStartLabel(input?.scheduledRunAt)],
  ];
}

/** 원고 작업 시각을 사람이 읽는 문구로. 비어 있으면 '즉시 생성'이다. */
function formatWorkStartLabel(iso: string | null | undefined): string {
  if (!iso) return "즉시 생성";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "즉시 생성";
  const hours = at.getHours();
  const meridiem = hours < 12 ? "오전" : "오후";
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  const minutes = String(at.getMinutes()).padStart(2, "0");
  return `${at.getMonth() + 1}월 ${at.getDate()}일 ${meridiem} ${hour12}:${minutes}`;
}

/** 이름 없는 메모는 앞부분만 보여 준다 — 카드에 본문을 통째로 쏟지 않는다. */
function shortText(value: string): string {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > 40 ? `${text.slice(0, 40)}…` : text || "메모";
}

function verifySearchSummary(task: BlogTask | null): {
  title: string;
  description: string;
  sources: SearchSource[];
} {
  const candidates = task?.intentValidationResult?.intentCandidates ?? [];
  const top = candidates[0];
  const trendPicked = Boolean(task?.trendSelection && !task.trendSelection.skipped);

  const basis = [
    task?.input.topic && "소재",
    visiblePurposes(task?.input.purpose).length && "글 목적",
    task?.input.referenceMaterials.length && "참고 자료",
  ].filter(Boolean) as string[];

  const seen = new Set<string>();
  const sources = candidates
    .flatMap((candidate) => candidate.sources ?? [])
    .filter((source) => {
      const key = source.url || source.title;
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });

  return {
    title: task?.trendSelection?.finalTopic || task?.input.topic || "-",
    description:
      top?.rationale ||
      (trendPicked
        ? "선택한 트렌드 키워드를 중심으로 검증합니다."
        : `선택된 트렌드가 없어 ${basis.join(", ") || "입력 정보"} 중심으로 검증합니다.`),
    sources,
  };
}
