import { useCallback, useEffect, useRef, useState } from "react";

import { request } from "../../api/client";
import type {
  BlogTask,
  TopicCandidate,
  TrendKeyword,
  TrendMode,
  TrendRecommendation,
  TrendTopicResult,
} from "../../api/types";
import { WRITE_STEP } from "../../resume";
import { canSelectTrendTopic, useStore } from "../../store";
import { TrendEvidenceBlock } from "./TrendEvidenceBlock";
import { TrendSourceBadge } from "./TrendSourceBadge";
import {
  EVIDENCE_FOOTNOTE,
  EVIDENCE_FOOTNOTE_DETAIL,
  EVIDENCE_SOURCES,
  evidenceForCard,
  formatRelativeTime,
  latestObservedAt,
} from "./trendEvidence";
import {
  INITIAL_VISIBLE_KEYWORDS,
  TREND_KEYWORD_COUNT,
  TREND_MODE_BUTTONS,
  defaultTrendKeywordIds,
  recommendedTrendKeywordId,
  selectedTrendKeywordIdSet,
  topicCandidatesForDisplay,
  trendKeywordsForDisplay,
  trendSourcesForRow,
} from "./trends";
import { formatDate } from "../../utils";

/** 탭 아이콘. 최신순은 상승 추세, 소재 관련순은 링크 — 두 탭의 성격을 글자 없이도 구분한다. */
function ModeIcon({ mode }: { mode: TrendMode }) {
  const common = {
    viewBox: "0 0 24 24",
    "aria-hidden": true,
    focusable: false,
  } as const;

  if (mode === "MATERIAL_RELATED") {
    return (
      <svg {...common}>
        <path d="M9.5 14.5 14.5 9M7.2 16.8l-1 1a3.5 3.5 0 0 1-5-5l3.2-3.2a3.5 3.5 0 0 1 5 0M16.8 7.2l1-1a3.5 3.5 0 0 1 5 5l-3.2 3.2a3.5 3.5 0 0 1-5 0" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M4 17.5 10 11l3.5 3L20 7" />
      <path d="M15.5 7H20v4.5" />
    </svg>
  );
}

/**
 * Keyword ids repeat across refreshes ("trend_google_trends_1" every time), so
 * identity has to include the word itself — otherwise a refresh that lands a new
 * keyword in the same slot would look like the same selection and the titles
 * would never be rewritten.
 */
function keywordKey(keyword: TrendKeyword): string {
  return `${keyword.trendKeywordId}|${keyword.keyword}`;
}

export function StepTrends({
  onChosen,
  onReopenVerify,
}: {
  onChosen: () => Promise<void>;
  onReopenVerify: () => void;
}) {
  const {
    task,
    recommendation,
    setRecommendation,
    trendMode,
    setTrendMode,
    setTopicCandidates,
    selectedTrendKeywordIds,
    trendKeywordSelectionTouched,
    selectTrendKeyword,
    setSelectedTrendKeywordIds,
    setTask,
    draftRounds,
    setDraftRounds,
    showToast,
    reportError,
  } = useStore();
  /** 몇 편을 만들 것인가, 지금 몇 편째를 고르는 중인가(2026-08-12). */
  const draftCount = Math.max(1, task?.input?.draftCount ?? 1);
  /**
   * 지금 몇 편째를 고르는 중인가.
   *
   * **끝난 라운드 수로 센다** — 배열 길이가 아니다(2026-08-12 사용자 신고: "첫번째꺼
   * 글 쓰는 건데 2번째 글 쓰는 거로 기록되는 것 같아"). ②가 1편째 제목을 담으면 길이가
   * 1이 되는데, 그때 ③은 아직 1편째다. 한 라운드는 **방향까지 골라야** 끝난다.
   */
  const roundIndex = draftRounds.filter((round) => round.intentId).length;
  /**
   * **이 편의** 제목을 이미 골랐는가.
   *
   * `task.trendSelection`으로 판단하면 안 된다 — 글 하나에는 트렌드 선택 자리가 하나뿐이라
   * 거기에는 **앞 편에서 고른 것**이 남아 있다. 그것을 이 편의 선택으로 읽는 바람에, 아직
   * 아무것도 고르지 않은 3편째 화면이 2편째 제목을 '선택한 제목'으로 보여 주고 다음 단계
   * 버튼까지 열어 줬다(2026-08-12 사용자 신고). 이 편의 기록이 배열에 생겼는지로만 본다.
   */
  const roundChosen = draftCount > 1 ? Boolean(draftRounds[roundIndex]) : true;

  const [busy, setBusy] = useState<string | null>(null);
  // 두 보기 방식(최신순=TRENDING, 소재 관련순=MATERIAL_RELATED)은 서로 다른 후보 풀을 쓴다.
  // 버튼을 누르면 그 모드의 결과를 불러오고, 한 번 불러온 모드는 recByMode에 담아 다시
  // 전환할 때 재수집 없이 즉시 보여준다.
  //
  // 지금 어느 보기인지는 **store가 들고 있다**(이 화면의 지역 상태가 아니다). 목록
  // (recommendation)도 store에 있어서, 여기에만 두면 소재 단계에 다녀오는 것만으로 탭이
  // '최신순'으로 되돌아가고 카드는 소재 관련순 그대로 남았다(2026-08-11 사용자 신고).
  /**
   * 브랜드를 얹은 글은 **소재 관련순만** 쓴다(2026-08-20 사용자 지시).
   *
   * 최신순은 소재와 무관한 실시간 인기 검색어다. 그것을 브랜드 글에 붙이면 소재도
   * 브랜드도 아닌 제3의 키워드가 제목의 중심이 되어, 글이 어디로도 닿지 않는다 —
   * 이 글의 목적은 **소재를 검색한 사람**을 데려오는 것이다.
   *
   * 탭 자체를 감춘다. 눌러 봐야 쓸 수 없는 탭을 남겨 두면 무엇을 고르라는 것인지
   * 알 수 없다.
   */
  const brandPicked = Boolean(task?.input?.brandId);
  const mode: TrendMode = brandPicked ? "MATERIAL_RELATED" : trendMode;

  // store의 보기 방식도 맞춰 둔다. 화면만 바꾸면 소재 단계에 다녀오거나 브랜드를 뺐을 때
  // store에 남아 있던 '최신순'이 되살아나, 방금 본 목록과 탭이 어긋난다.
  useEffect(() => {
    if (brandPicked && trendMode !== "MATERIAL_RELATED") {
      setTrendMode("MATERIAL_RELATED");
    }
  }, [brandPicked, setTrendMode, trendMode]);
  const recByMode = useRef<Partial<Record<TrendMode, TrendRecommendation>>>({});
  // 수집 진행 상태. busy와 분리한 이유: 수집은 탭 전환이나 다른 UI를 막지 않는 백그라운드
  // 작업이라서다 — 로딩 중에도 최신순으로 돌아갔다가 다시 오면 끝난 결과를 즉시 본다.
  // inFlight(ref)는 같은 입력의 중복 요청을 막는 동기 가드, loadingModes(state)는 화면 표시용.
  const [loadingModes, setLoadingModes] = useState<Partial<Record<TrendMode, boolean>>>({});
  const inFlight = useRef<Partial<Record<TrendMode, boolean>>>({});
  // 백그라운드 수집이 끝난 시점에 '지금 보고 있는 모드'인지 판별하기 위한 최신 mode 값.
  // 렌더마다 맞춰 둔다 — 다시 그려질 때(소재 단계에 다녀오는 등) store에 남아 있던 보기
  // 방식을 그대로 따라가야, 늦게 도착한 수집 결과가 엉뚱한 탭에 그려지지 않는다.
  const modeRef = useRef<TrendMode>(mode);
  modeRef.current = mode;
  // 펼침 여부. 개수가 아니라 상태로 들고 있어야 '다른 키워드 보기'로 목록이 통째로 바뀌어도
  // 사용자가 고른 보기 방식(펼침/접힘)이 그대로 유지된다 — 펼쳐 놓고 다른 후보를 보면
  // 다시 펼친 채로, 접어 놓고 보면 접힌 채로 새 키워드가 나온다.
  const [expanded, setExpanded] = useState(false);

  // 소재 관련순 '다른 키워드 보기'의 위치. 서버가 만든 불투명 값이라 여기서 해석하지 않고
  // 받은 그대로 되돌려보낸다. exclude 누적과 달리 후보가 말라붙지 않는다 — 끝에 닿으면
  // 서버가 순환시키기 때문이다.
  const cursorByMode = useRef<Partial<Record<TrendMode, string>>>({});

  // '지금 보고 있는 모드'가 수집 중일 때만 로딩 화면을 보여준다 — 다른 모드가 백그라운드에서
  // 수집 중이어도 현재 모드의 카드는 그대로 쓸 수 있다.
  const collecting = !!loadingModes[mode];
  const writingTitles = busy === "topics";
  const selectionAllowed = canSelectTrendTopic(task);

  // 전체 후보(백엔드가 모드별 점수순·중복제거로 내려준 것) → 접혀 있으면 앞에서부터 8개만.
  // 정렬은 모드가 결정한다: 최신순은 트렌드 강도순, 소재 관련순은 관련도순으로 백엔드가 내려주므로
  // 클라이언트에서 다시 정렬하지 않는다.
  const allKeywords = trendKeywordsForDisplay(recommendation);
  const visibleKeywords = expanded
    ? allKeywords
    : allKeywords.slice(0, INITIAL_VISIBLE_KEYWORDS);
  const hasMore = !expanded && allKeywords.length > INITIAL_VISIBLE_KEYWORDS;

  // 하단 안내의 '가장 최근 수집'은 출처 데이터를 실제 관측한 시각(observedAt) 기준이다.
  // 응답을 만든 시각(generatedAt)으로 대신하지 않는다 — 저장된 풀을 그대로 보여준 응답도
  // generatedAt은 늘 '방금'이라, 며칠 전 수집분을 두고 "가장 최근 수집 방금 전"이라고
  // 말하게 된다. 관측 시각을 모르면 그 문장을 아예 쓰지 않는다.
  const latestCollectedText = formatRelativeTime(latestObservedAt(allKeywords));

  const candidates = topicCandidatesForDisplay(recommendation);
  const selectedTopicId = task?.trendSelection?.topicCandidateId;
  const subject = task?.input?.topic?.trim() ?? "";
  // 용어는 화면 전체에서 '트렌드 키워드'로 통일한다(트렌드/키워드/트렌드 키워드 혼용 제거).
  const collectionStatus = collecting
    ? mode === "MATERIAL_RELATED"
      ? // 소재 관련순은 한 요청 안에서 두 단계가 일어난다: 저장된 후보(DB) 확인 → 관련
        // 키워드가 하나도 없을 때만 새로 수집. 화면은 단계를 나눠 볼 수 없으므로 둘을
        // 함께 알린다.
        "저장된 후보에서 관련 키워드 확인 중 (없으면 새로 수집)"
      : allKeywords.length
        ? "다른 트렌드 키워드 불러오는 중"
        : "트렌드 키워드 불러오는 중"
    : recommendation?.cacheStatus === "unavailable"
      ? "트렌드 수집 소스가 설정되지 않았습니다"
      : recommendation?.refreshing
      ? "저장된 트렌드 키워드 표시 중"
      : recommendation
        ? recommendation.source === "external_api"
          ? "새로 수집한 트렌드 키워드 표시 중"
          : "최신 트렌드 키워드 표시 중"
        : "불러오기 대기";
  const statusHint =
    recommendation?.cacheStatus === "unavailable"
      ? "트렌드 수집 API 키가 없어 이 단계는 사용할 수 없습니다. 아래 버튼으로 소재만 사용해 계속할 수 있습니다."
      : recommendation?.source === "external_api"
      ? "저장된 후보에 소재와 관련된 키워드가 없어, 방금 새로 수집해 같은 검증을 거친 결과입니다. 수집분은 저장돼 다음부터 재사용됩니다."
      : recommendation?.cacheStatus === "stale" && recommendation.refreshing
        ? "저장된 트렌드 키워드를 먼저 표시했습니다. 최신 데이터를 백그라운드에서 갱신하고 있습니다."
        : "보기 방식을 바꾸거나 '더 보기'로 더 많이 비교하고, '다른 키워드 보기'로 저장된 후보를 다시 섞어 볼 수 있습니다.";

  // 추천 배지는 화면에 보이는 몇 개가 아니라 전체 후보를 기준으로 하나 고른다.
  const selectedKeywordIds = selectedTrendKeywordIdSet({
    topicCandidates: candidates,
    selectedTopicId,
    recommendation,
    task,
    selectedTrendKeywordIds,
    // 아직 이 편을 고르지 않았으면 **앞 편의 선택으로 되돌아가지 않는다**. touched=false일 때
    // task.trendSelection을 기본값으로 쓰는 규칙이 있는데, 여러 편 흐름에서는 그 값이 앞 편의
    // 것이라 3편째 카드에 2편째 키워드가 '선택됨'으로 찍혔다(2026-08-12 신고).
    touched: trendKeywordSelectionTouched || !roundChosen,
  });
  const recommendedId = recommendedTrendKeywordId(allKeywords, mode);
  const selectedKeyword = allKeywords.find((item) => selectedKeywordIds.has(item.trendKeywordId));

  const titleChosen = roundChosen && Boolean(task?.trendSelection && !task.trendSelection.skipped);
  const selectedTitle = titleChosen ? task?.trendSelection?.finalTopic ?? "" : "";

  // Which keyword the titles on screen were written for. Set on every path that
  // produces titles, so the effect below never rewrites what is already correct.
  const titlesFor = useRef<string | null>(null);
  /** 키워드마다 '제목 추천'을 몇 번 눌렀는지. 서버가 이 회차로 재생성 방향을 고른다.
      화면을 벗어나면 초기화돼도 무해하다 — 같은 회차면 같은 방향이라는 성질만 필요하다. */
  const regenerations = useRef<Record<string, number>>({});

  /** Rewrites the title list for one keyword.
   *
   *  화면에 있던 후보를 그대로 되돌려 보낸다 — 제목 문자열만이 아니라 그 후보가 쓴 후킹 유형·
   *  기본 유형까지. 제목만 보내면 모델은 같은 후킹·같은 유형으로 표현만 바꿔 오고, 그건
   *  재생성이 아니라 문장 교체다. 몇 번째 재생성인지도 함께 보내면 서버가 그 회차의 관점 축을
   *  결정적으로 골라 준다(같은 회차 = 같은 축, 난수 아님).
   */
  const writeTitles = useCallback(
    async (keyword: TrendKeyword, previous: TopicCandidate[]) => {
      if (!task) return;
      const key = keywordKey(keyword);
      titlesFor.current = key;
      setBusy("topics");
      const attempt = (regenerations.current[key] ?? 0) + (previous.length ? 1 : 0);
      regenerations.current[key] = attempt;
      try {
        const result = await request<TrendTopicResult>(`/posts/${task.postId}/trends/topics`, {
          method: "POST",
          body: {
            trendKeywordId: keyword.trendKeywordId,
            keyword: keyword.keyword,
            source: keyword.source,
            excludeTitles: previous.map((item) => item.title),
            excludeAngles: previous.map((item) => ({
              title: item.title,
              hookType: item.hookType ?? undefined,
              titleType: item.description ?? undefined,
            })),
            regenerationCount: attempt,
          },
        });
        // 탭 전환이 더 이상 잠기지 않으므로, 쓰는 도중 모드를 바꿨다면(titlesFor가 비워짐)
        // 이전 키워드의 제목으로 화면을 덮어쓰지 않는다.
        if (titlesFor.current === key) {
          setTopicCandidates(result.topicCandidates, result.generatedAt);
        }
      } catch (error) {
        reportError(error);
      } finally {
        setBusy(null);
      }
    },
    [task, setTopicCandidates, reportError],
  );

  /** Collects one mode's ranked candidate set. Titles cost a model call and are written
      on demand, when 제목 추천 is pressed.

      refresh(다른 키워드 보기)는 모드마다 다르게 동작한다:
      - 최신순: shuffle — 서버가 노출 이력을 무시하고 저장된 풀 전체에서 무작위 16개를
        뽑는다(중복 노출 허용). 이력 제외 방식은 풀을 한 바퀴 돌면 후보가 말라붙어
        버튼이 죽는 문제가 있었다.
      - 소재 관련순: 그 탭에서 본 키워드를 제외로 보내 아직 안 본 적격 후보를 받는다. */
  const collect = useCallback(
    async (targetMode: TrendMode, refresh = false) => {
      if (!task) return;
      // 같은 모드(=같은 소재·목적·페르소나 입력)의 요청이 이미 진행 중이면 다시 보내지
      // 않는다 — 로딩 중 탭을 오갔다 와도 요청은 한 번이다.
      if (inFlight.current[targetMode]) return;
      inFlight.current[targetMode] = true;
      setLoadingModes((prev) => ({ ...prev, [targetMode]: true }));
      try {
        const result = await request<TrendRecommendation>(
          `/posts/${task.postId}/trends/recommend`,
          {
            method: "POST",
            body: {
              mode: targetMode,
              maxKeywords: TREND_KEYWORD_COUNT,
              excludeKeywords: [],
              shuffle: refresh && targetMode === "TRENDING",
              // 소재 관련순의 '다른 키워드 보기'는 커서로 다음 묶음을 받는다. 처음 여는
              // 경우(refresh=false)에는 커서를 보내지 않아 관련도 상위부터 시작한다.
              cursor:
                refresh && targetMode === "MATERIAL_RELATED"
                  ? cursorByMode.current[targetMode]
                  : undefined,            },
          },
        );
        // '이미 보여준 키워드'는 이제 서버가 기록한다(TrendExposureStore) — 클라이언트가
        // 누적해 exclude로 보내던 방식은 풀을 한 바퀴 돌면 후보가 말라붙어 폐기했다.
        // 다음 묶음의 위치를 기억한다. 소재 관련순에서만 서버가 채워 준다.
        cursorByMode.current[targetMode] = result.nextCursor;        // 모드별로 마지막 결과를 담아 둔다 — 다른 보기로 갔다 돌아올 때 재수집 없이 즉시 보여준다.
        recByMode.current[targetMode] = result;
        // 로딩 중 사용자가 다른 탭으로 갔을 수 있다. 결과는 위에서 저장했으니, 화면 상태는
        // 지금 보고 있는 모드일 때만 바꾼다 — 다른 탭을 보고 있는데 선택·제목이 지워지거나
        // 목록이 갈리면 안 된다. 돌아오면 switchMode가 recByMode 캐시로 즉시 보여준다.
        if (modeRef.current === targetMode) {
          // 후보를 한 바퀴 다 본 것은 오류가 아니라 정상 순환이다 — 그 사실을 알려 주지
          // 않으면 사용자는 '다른 키워드 보기'가 같은 것을 다시 내줬다고 여긴다.
          //
          // 저장된 후보가 한 화면보다 적으면 순환해도 같은 것이 나올 수밖에 없다. 그때
          // "새로운 순서로 보여드립니다"는 사실이 아니다 — 목록이 눈에 보이게 그대로이므로
          // 버튼이 고장 난 것처럼 읽힌다. 후보가 몇 개뿐인지와 다음에 무엇을 누르면 되는지를
          // 대신 알린다(무관한 키워드로 자리를 채우지 않는다는 원칙은 그대로다).
          if (result.cycled) {
            const pool = result.poolSize ?? result.trendKeywords.length;
            showToast(
              pool <= result.trendKeywords.length
                ? `소재와 관련 있다고 확인된 후보가 ${pool}개뿐입니다. '새 키워드 찾기'으로 더 모아 보세요.`
                : "저장된 관련 후보를 모두 확인하여 새로운 순서로 다시 보여드립니다.",
            );
          }
          setRecommendation(result);
          // '다른 키워드 보기'(refresh)는 같은 목록을 다시 뽑아 보여주는 것이므로 사용자가
          // 고른 펼침/접힘을 건드리지 않는다 — 펼쳐 놓고 눌렀는데 8개로 접혀 버리면 매번
          // 다시 펼쳐야 한다. 처음 불러오는 경우에만 접힌 상태에서 시작한다.
          if (!refresh) setExpanded(false);
          setSelectedTrendKeywordIds(defaultTrendKeywordIds(), false);
          titlesFor.current = null;
        }
      } catch (error) {
        reportError(error);
      } finally {
        inFlight.current[targetMode] = false;
        setLoadingModes((prev) => ({ ...prev, [targetMode]: false }));
      }
    },
    [task, setRecommendation, setSelectedTrendKeywordIds, showToast, reportError],
  );

  /** '새 키워드 찾기': forceCollect로 캐시를 우회해 소스에서 새 트렌드 키워드를 수집하고 기존
      풀에 합친 뒤, **이어서 다음 묶음을 받아 화면에 반영한다**. 풀은 모드와 무관하므로 현재
      모드로 요청한다.

      예전에는 수집만 하고 화면을 그대로 두었다("'다른 키워드 보기'로 확인하세요" 토스트). 눌러도
      목록이 그대로였으므로 버튼이 아무 일도 안 한 것처럼 보였고, 실제로 후보가 늘어난 경우와
      늘지 않은 경우를 사용자가 구분할 방법이 없었다. 수집 결과 자체는 화면에 그리지 않는 것이
      맞다(응답이 노출 이력에 잡히지 않도록 서버가 force_collect 응답을 이력에서 뺀다) — 그래서
      수집 뒤에 일반 '다른 키워드 보기'와 똑같은 요청을 한 번 더 보낸다. */
  const collectToRedis = useCallback(async () => {
    if (!task) return;
    setBusy("collect");
    try {
      await request<TrendRecommendation>(`/posts/${task.postId}/trends/recommend`, {
        method: "POST",
        body: {
          mode,
          maxKeywords: TREND_KEYWORD_COUNT,
          forceCollect: true,        },
      });
      showToast("트렌드 키워드를 새로 수집했습니다. 새 후보를 불러옵니다.");
    } catch (error) {
      reportError(error);
      setBusy(null);
      return;
    }
    setBusy(null);
    // 늘어난 풀에서 다음 묶음을 받아 화면을 갱신한다. 실패해도 수집 자체는 이미 끝났으므로
    // collect가 자기 오류를 알린다.
    await collect(mode, true);
  }, [task, mode, collect, showToast, reportError]);

  // Opening the tab collects the initial mode (추천어) straight away, so the panel is
  // never empty. Guarded per post: a failed collection surfaces the error rather than
  // retrying in a loop, and 트렌드 불러오기 is the way back from it.
  //
  // 기본값(최신순)이 아니라 **지금 고른 보기 방식**으로 모은다. 소재를 고치고 돌아오면
  // 옛 목록은 버려지는데, 그때 보기 방식까지 최신순으로 되돌리면 사용자는 방금 고른
  // 소재 관련순을 다시 눌러야 한다.
  const collectedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!task || !selectionAllowed || recommendation) return;
    if (collectedFor.current === task.postId) return;

    collectedFor.current = task.postId;
    void collect(mode);
  }, [task, selectionAllowed, recommendation, collect, mode]);

  // Picking a keyword no longer writes titles. It used to, which meant a title
  // model call for every keyword the user clicked through. 제목 추천 is the ask now.
  //
  // What the click still has to do is drop the titles on screen: they were written
  // for the previous keyword, and leaving them there is how the list ends up naming
  // a keyword the user is no longer on.
  useEffect(() => {
    if (!selectedKeyword) return;
    const key = keywordKey(selectedKeyword);
    if (titlesFor.current === null || titlesFor.current === key) return;

    titlesFor.current = null;
    setTopicCandidates([], "");
  }, [selectedKeyword, setTopicCandidates]);

  function switchMode(next: TrendMode) {
    // 탭은 클릭 즉시 활성 상태로 바뀐다 — 데이터 처리가 끝날 때까지 기다리지 않고, 로딩
    // 중에도 다른 탭으로 오갈 수 있다(수집은 백그라운드에서 계속된다). 서로 다른 후보
    // 풀이므로 이전 키워드 선택·제목은 비운다. 이미 불러온 모드면 재수집 없이
    // 캐시(recByMode)를 즉시 보여주고, 처음 여는 모드면 그 모드를 수집한다.
    if (next === mode) return;
    setTrendMode(next);
    modeRef.current = next;
    setExpanded(false);
    // 보기 방식을 바꾸면 다른 후보 풀이므로 위치도 처음으로 되돌린다.
    cursorByMode.current[next] = undefined;
    setSelectedTrendKeywordIds(defaultTrendKeywordIds(), false);
    titlesFor.current = null;
    setTopicCandidates([], "");
    const cached = recByMode.current[next];
    if (cached) {
      setRecommendation(cached);
    } else {
      // 아직 결과가 없는 모드: 이전 모드의 카드가 남아 보이지 않게 비우고 로딩 패널을
      // 보여준다. 이미 수집이 진행 중이면 collect의 inFlight 가드가 중복 요청을 걸러낸다.
      setRecommendation(null);
      void collect(next);
    }
  }

  /** 선택 칩의 ×. 새 해제 로직을 만들지 않고 모드 전환·재수집이 쓰는 것과 같은 경로를
      쓴다(touched=true라 추천 후보로 되돌아가지 않는다). 화면에 남은 제목은 해제한
      키워드로 쓴 것이므로 함께 비운다 — 고르지도 않은 키워드의 제목이 남으면 안 된다. */
  function clearKeywordSelection() {
    setSelectedTrendKeywordIds(defaultTrendKeywordIds(), true);
    titlesFor.current = null;
    setTopicCandidates([], "");
  }

  /** Selecting a topic (or skipping) advances the task, then kicks off M3.

      ``openVerify``: 선택 직후 검증 화면으로 넘어간다. 제목을 고른 경우에는 쓰지
      않는다 — 이 화면에 남아 고른 제목을 확인하고 다른 후보를 다시 볼 수 있어야
      한다(2026-08-07 결정). 건너뛰기에는 그 이유가 없다. */
  async function chooseTopic(
    body: Record<string, unknown>,
    successMessage: string,
    key: string,
    { openVerify = false } = {},
  ) {
    if (!task) return;
    setBusy(key);
    try {
      const updated = await request<BlogTask>(`/posts/${task.postId}/trends/select`, {
        method: "POST",
        body,
      });
      setTask(updated);
      // 여러 편을 만들 때는 **이 라운드의 제목·키워드를 화면이 따로 기억한다**
      // (2026-08-12). 글 하나에는 트렌드 선택 자리가 하나뿐이라, 다음 라운드가 이 값을
      // 덮어쓴다 — 요약 패널과 마지막 전송이 읽을 곳은 이 배열이다.
      if (draftCount > 1) {
        const round = {
          title: (body.finalTopic as string) || undefined,
          keywords: (body.selectedKeywords as string[]) ?? [],
        };
        const next = [...draftRounds];
        next[roundIndex] = { ...next[roundIndex], ...round };
        setDraftRounds(next);
      }
      showToast(successMessage);
    } catch (error) {
      reportError(error);
      setBusy(null);
      return;
    }
    setBusy(null);
    // 검증(onChosen)은 1분쯤 걸린다 — 화면 이동을 그 앞에 둔다. 검증 화면이 진행
    // 상태를 스스로 보여준다.
    if (openVerify) onReopenVerify();
    await onChosen();
  }

  /** 트렌드 없이 소재만으로 진행. 이미 고른 트렌드 키워드가 있으면 그 선택이 버려지므로,
      먼저 해제 여부를 확인한다.

      선택이 저장되면 **곧장 검증 화면으로 넘어간다**(2026-08-07 사용자 요청). 여기
      남으면 키워드·제목 추천 UI가 그대로 보이는데, 방금 그것 없이 가겠다고 누른
      사람에게 보여 줄 것이 아니다. */
  function skipTrend() {
    if (
      selectedKeyword &&
      !window.confirm(
        `선택한 트렌드 키워드 '${selectedKeyword.keyword}'가 해제됩니다. 트렌드 없이 소재만으로 진행할까요?`,
      )
    ) {
      return;
    }
    return chooseTopic(
      { skipped: true, selectedTrendKeywordIds: [] },
      "소재만으로 진행합니다.",
      "skip",
      { openVerify: true },
    );
  }

  function selectTopic(topicCandidateId: string) {
    const candidate = recommendation?.topicCandidates.find(
      (item) => item.topicCandidateId === topicCandidateId,
    );
    if (!candidate) return;

    const ids =
      trendKeywordSelectionTouched || selectedTrendKeywordIds.length
        ? selectedTrendKeywordIds.slice(0, 1)
        : candidate.trendKeywordIds.slice(0, 1);
    // 고른 키워드의 **문자열**도 함께 보낸다. 키워드 목록은 서버에 저장되지 않으므로
    // 여기서 보내지 않으면 "사용자가 무엇으로 검색했는지"가 원고 단계까지 가지 못한다
    // (hookType과 같은 이유). 원고는 이 원본 검색어를 문장에 그대로 복사하지 않고
    // 자연스러운 표현으로 바꿔 쓰는 기준으로 사용한다.
    const keywords = ids
      .map((id) => allKeywords.find((item) => item.trendKeywordId === id)?.keyword)
      .filter((keyword): keyword is string => Boolean(keyword));

    return chooseTopic(
      {
        topicCandidateId: candidate.topicCandidateId,
        finalTopic: candidate.title,
        selectedTrendKeywordIds: ids,
        selectedKeywords: keywords,
        skipped: false,
        // 고른 제목의 후킹 유형을 그대로 되돌려 보낸다. 후보 목록은 서버에 저장되지 않아
        // 여기서 보내지 않으면 "이 제목이 무엇을 약속했는지"가 원고 단계까지 가지 못한다.
        // 예전 후보에는 없을 수 있고, 없으면 서버가 그냥 비워 둔다.
        hookType: candidate.hookType,
      },
      "제목을 정했습니다.",
      topicCandidateId,
    );
  }

  const keywordHeadline = collecting
    ? "트렌드 키워드 불러오는 중"
    : allKeywords.length
      ? `트렌드 키워드 ${allKeywords.length}개 후보`
      : recommendation?.cacheStatus === "unavailable"
        ? "트렌드 키워드 수집을 사용할 수 없습니다"
        : mode === "MATERIAL_RELATED"
          ? // 백엔드가 부족분을 자동 보충하고 후보를 순환시키므로, 여기까지 오는 것은 소재
            // 관련 검색어를 한 개도 찾지 못한 드문 경우뿐이다.
            "관련 검색어를 찾지 못했습니다"
          : "불러오기 대기";

  return (
    <section className="panel write-paper-card title-step" aria-label="제목 단계">
      <header className="panel-header title-step-head">
        <div className="panel-heading-copy">
          {/* 브랜드를 얹은 글은 실시간 트렌드를 쓰지 않는다 — 그 글에서 이 글자는 사실이 아니다. */}
          <p className="panel-kicker">
            STEP {String(WRITE_STEP.TITLE + 1).padStart(2, "0")} ·{" "}
            {brandPicked ? "TOPIC KEYWORD" : "REALTIME TREND"}
          </p>
          <h2 className="panel-title">키워드 선택</h2>
          {/* 수집 상태·시각 한 줄. '실제 갱신 여부'(불러오는 중 / 최신 표시 / 저장분 표시)와
              마지막으로 데이터를 가져온 시각을 함께 보여준다. */}
          <p className="title-step-meta">
            {collectionStatus}
            {recommendation ? ` · ${formatDate(recommendation.generatedAt)} 기준` : ""}
          </p>
        </div>
        <svg className="title-step-doodle" viewBox="0 0 64 64" aria-hidden="true">
          <path d="M32 5v12M32 47v12M5 32h12M47 32h12M13 13l8.5 8.5M42.5 42.5 51 51M51 13l-8.5 8.5" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="2.5" />
          <circle cx="32" cy="32" r="8" fill="#ffe46a" stroke="currentColor" strokeWidth="2" />
        </svg>
      </header>

      <div className="panel-body title-step-body">
        {/* 이 단계는 건너뛸 수 있음을 화면에서 바로 알린다 — 경고가 아니라 선택 기능 설명. */}
        <p className="title-optional-note">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M9 18h6M10 21h4M8.5 14.5a6 6 0 1 1 7 0c-.8.7-1.2 1.3-1.4 2h-4.2c-.2-.7-.6-1.3-1.4-2Z" />
          </svg>
          <span>선택 사항 · 최신 이슈와 입력한 소재를 결합하면 더 다양한 글을 만들 수 있어요.</span>
        </p>

        {/* 여러 편을 만들 때는 이 화면을 편수만큼 다시 밟는다. 지금 몇 편째인지, 앞 편에서
            무엇을 골랐는지를 함께 보여 준다 — 화면이 앞 편의 선택을 지운 것처럼 보이면
            사용자는 기록이 날아갔다고 읽는다(2026-08-12 사용자 지적). 선택은 지워지지 않고
            편마다 따로 쌓인다. */}
        {draftCount > 1 && (
          <div className="title-round-note" role="status">
            <p className="title-round-line">
              <span className="title-round-badge">{roundIndex + 1}편째</span>
              <span>
                <b>
                  {draftCount}편 중 {roundIndex + 1}편째
                </b>{" "}
                제목을 고르는 중입니다. 편마다 키워드와 제목을 따로 고르며, 앞 편에서 고른 것은
                그대로 남습니다.
              </span>
            </p>
            {roundIndex > 0 && (
              <ul className="title-round-done">
                {draftRounds.slice(0, roundIndex).map((round, index) => (
                  <li key={`round-${index}`}>
                    <b>원고{index + 1}</b>
                    <span>
                      {(round?.keywords ?? []).filter(Boolean).join(", ") || "트렌드 없이 소재만"}
                    </span>
                    <span className="title-round-done-title">
                      {round?.title || "제목 자동 생성"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* 트렌드를 쓰지 않겠다는 선택은 이 단계를 시작하기 전에 이미 정해져 있을 수 있다.
            화면 맨 아래에 있으면 키워드를 다 훑고 나서야 발견하게 되므로, '선택 사항'
            안내 바로 아래에 두어 처음부터 한 번에 고를 수 있게 한다. */}
        <div className="title-skip-row">
          <button
            className="button title-skip-button"
            type="button"
            id="skipTrend"
            disabled={!selectionAllowed || !!busy}
            onClick={skipTrend}
          >
            소재만으로 작성
          </button>
          <p className="title-action-note">
            키워드 없이 입력한 소재만으로 제목을 만들 수도 있습니다.
          </p>
        </div>

        {/* 두 보기 방식(최신순/소재 관련순)은 서로 다른 후보 풀을 불러온다 — 정렬 토글이
            아니라 모드 전환이다. 카드를 고른 뒤에도 감추지 않고 언제든 전환할 수 있게 둔다.

            브랜드를 얹은 글에서는 고를 것이 없다: 최신순은 소재와 무관한 실시간 인기
            검색어라 그 글에 쓸 수 없다(2026-08-20). 탭 대신 무엇을 보여 주고 있는지
            한 줄로 말한다. */}
        {brandPicked ? (
          <p className="title-trend-fixed-note">
            <strong>소재 관련순</strong>으로만 보여 드립니다. 브랜드를 함께 쓰는 글은
            소재를 검색한 사람이 들어와야 하므로, 소재와 무관한 실시간 인기 검색어는
            제외합니다.
          </p>
        ) : (
        <div className="title-trend-tabs" role="tablist" aria-label="트렌드 보기 방식">
          {/* 탭은 어떤 상황에도 disabled 하지 않는다 — 로딩 중에도 자유롭게 오갈 수 있고,
              수집은 백그라운드에서 계속된다. */}
          {TREND_MODE_BUTTONS.map((option) => (
            <button
              key={option.id}
              type="button"
              id={`title-trend-tab-${option.id}`}
              role="tab"
              aria-selected={mode === option.id}
              aria-controls="title-trend-tabpanel"
              className={`title-trend-tab ${mode === option.id ? "selected" : ""}`}
              // 동작까지 설명하는 긴 문장은 화면을 채우지 않고 tooltip으로만 붙인다.
              title={option.hint}
              onClick={() => switchMode(option.id)}
            >
              <ModeIcon mode={option.id} />
              <span>{option.label}</span>
            </button>
          ))}
        </div>
        )}

        {/* 두 탭의 설명은 활성 여부와 상관없이 늘 함께 보여준다 — 무엇이 다른 기능인지
            눌러보지 않고도 비교할 수 있어야 한다. 고를 것이 없는 브랜드 글에서는 비교할
            것도 없으므로 함께 감춘다. */}
        {!brandPicked && (
        <div className="title-trend-tab-notes" aria-hidden="true">
          {TREND_MODE_BUTTONS.map((option) => (
            <p key={option.id} className={mode === option.id ? "current" : ""}>
              {option.note}
            </p>
          ))}
        </div>
        )}

        <div
          className="title-trend-panel"
          id="title-trend-tabpanel"
          role="tabpanel"
          aria-labelledby={`title-trend-tab-${mode}`}
        >
          <p className="title-keyword-headline">{keywordHeadline}</p>

          {/* 로딩 중에는 카드 목록 자리를 통째로 로딩 패널로 바꾼다 — 버튼을 비활성화해
              '불러오는 중...'으로 바꾸거나 이전 모드의 카드를 흐리게 남기지 않는다.
              skeleton은 결과가 들어올 자리를 미리 잡아 화면이 위아래로 튀지 않게 한다. */}
          {collecting ? (
            <div className="title-loading-state" role="status" aria-live="polite">
              <span className="title-loading-spinner" aria-hidden="true" />
              <strong className="title-loading-title">
                {mode === "MATERIAL_RELATED"
                  ? "키워드 확인 및 수집 중"
                  : "트렌드 키워드 불러오는 중"}
              </strong>
              <p className="title-loading-hint">
                {mode === "MATERIAL_RELATED"
                  ? "입력하신 소재와 연관된 키워드 후보를 확인하고, 검색량 및 연관성 점수를 분석하고 있어요."
                  : "모아둔 트렌드 키워드를 인기순으로 불러오고 있어요."}
              </p>
              <div className="title-loading-skeleton" aria-hidden="true">
                {[0, 1, 2].map((row) => (
                  <span className="title-skeleton-card" key={row}>
                    <span className="title-skeleton-line wide" />
                    <span className="title-skeleton-line" />
                  </span>
                ))}
              </div>
            </div>
          ) : (
            /* 작은 카드 그리드. 전체 후보 중 상위 8개를 먼저 보이고 '더 보기'로 나머지를
               누적 노출한다. */
            <div className="title-keyword-grid" role="group" aria-label="트렌드 키워드 후보">
              {visibleKeywords.map((item) => {
                const selected = selectedKeywordIds.has(item.trendKeywordId);
                const isRecommended = item.trendKeywordId === recommendedId;
                // 카드의 3줄 지표는 한 출처의 근거만 그린다 — 여러 출처의 숫자를 합치지
                // 않는다. 대표 출처에 근거가 없으면(구글 자동완성처럼 수치를 주지 않는
                // 출처) 그 수치를 실제로 잰 출처로 넘어가고, 머리줄에 두 로고를 함께
                // 보여 제안한 곳과 잰 곳을 구분한다.
                const shown = evidenceForCard(item.source, item.evidenceBySource);
                const evidenceSource = shown?.source ?? item.source;
                // 지표 머리줄에 이미 나온 로고는 아래 보조 줄에서 뺀다.
                const headSources = new Set(
                  EVIDENCE_SOURCES.has(evidenceSource)
                    ? [evidenceSource, item.source]
                    : [],
                );
                const secondarySources = trendSourcesForRow(item).filter(
                  (source) => !headSources.has(source),
                );
                return (
                  <button
                    key={item.trendKeywordId}
                    type="button"
                    className={`title-keyword-card ${selected ? "title-keyword-card--selected" : ""} ${
                      isRecommended ? "title-keyword-card--recommended" : ""
                    }`}
                    aria-pressed={selected}
                    // 카드에 hover 툴팁을 두지 않는다. '트렌드 점수 56 · 단일 출처'처럼
                    // 내부 점수를 띄우면, 그 숫자가 무엇을 뜻하는지도 카드끼리 비교하라는
                    // 뜻인지도 알 수 없어 오히려 헷갈린다(2026-08-07 사용자 결정). 카드가
                    // 스스로를 설명하는 자리는 아래 3줄 지표다.
                    // Locked while titles are being written: switching keywords
                    // mid-flight would race two title lists into the same slot.
                    disabled={!!busy}
                    onClick={() => selectTrendKeyword(item.trendKeywordId)}
                  >
                    <span className="title-keyword-word">{item.keyword}</span>
                    {/* 추천(=전체 후보 중 대표)과 선택(=내가 고른 것)은 서로 다른 표시로
                        구분한다. 색만으로 나누지 않도록 둘 다 글자를 함께 둔다. */}
                    {isRecommended && <span className="title-keyword-reco">추천</span>}
                    {selected && (
                      <span className="title-keyword-check">
                        <span aria-hidden="true">✓</span> 선택됨
                      </span>
                    )}
                    <TrendEvidenceBlock
                      mode={mode}
                      source={evidenceSource}
                      evidence={shown?.evidence}
                      suggestedBy={shown?.measuredElsewhere ? item.source : undefined}
                    />
                    {secondarySources.length > 0 && (
                      <span className="title-keyword-signals">
                        {secondarySources.map((source) => (
                          <TrendSourceBadge key={source} source={source} />
                        ))}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}

          {/* 카드가 로딩 패널로 바뀐 동안에는 목록에 딸린 버튼도 함께 감춘다 — 비활성화된
              버튼을 로딩 표시로 쓰지 않는다. */}
          {!collecting && allKeywords.length > 0 && (
            <div className="title-keyword-actions">
              {hasMore && (
                <button
                  className="button small title-keyword-action"
                  type="button"
                  disabled={!!busy}
                  // 펼치면 한 번에 전부(요청 수까지) 보여준다 — 단계적으로 더 누르게 하지 않는다.
                  onClick={() => setExpanded(true)}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <path d="m6 9 6 6 6-6" />
                  </svg>
                  더 보기 ({allKeywords.length - INITIAL_VISIBLE_KEYWORDS}개)
                </button>
              )}
              {expanded && allKeywords.length > INITIAL_VISIBLE_KEYWORDS && (
                <button
                  className="button small title-keyword-action"
                  type="button"
                  disabled={!!busy}
                  onClick={() => setExpanded(false)}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <path d="m6 15 6-6 6 6" />
                  </svg>
                  접기
                </button>
              )}
              {/* '다른 키워드 보기'(보기 동작): 새로 수집하지 않고 이미 모아둔 풀에서 다시 가져온다.
                  최신순은 풀 전체에서 무작위 16개(중복 노출 허용 — 이력 제외는 풀을 다 돌면 버튼이
                  죽는다), 소재 관련순은 아직 안 본 적격 후보를 가져온다. 옆의 '새 키워드 찾기'
                  (수집 동작)와 라벨의 동사(보기 vs 찾기)로 역할을 구분한다. */}
              <button
                className="button small title-keyword-action"
                type="button"
                disabled={!!busy}
                title={
                  mode === "TRENDING"
                    ? "저장된 후보 전체에서 무작위로 다시 뽑아 보여줍니다. (새로 수집하지 않음)"
                    : "이미 모아둔 후보 중 아직 안 본 트렌드 키워드를 화면에 보여줍니다. (새로 수집하지 않음)"
                }
                onClick={() => collect(mode, true)}
              >
                {/* 마주 보는 두 화살표(⇄) — 있는 것 안에서 자리를 바꾼다는 뜻. */}
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <path d="M4 9h15m-3.5-3.5L19 9l-3.5 3.5" />
                  <path d="M20 15H5m3.5-3.5L5 15l3.5 3.5" />
                </svg>
                다른 키워드 보기
              </button>
              {/* '새 키워드 찾기'(수집 동작): 캐시를 우회해 소스에서 새 키워드를 끌어와 풀을 키우고
                  (force_collect), 이어서 늘어난 풀의 다음 묶음을 받아 화면을 갱신한다. */}
              <button
                className="button small title-keyword-action title-keyword-action--collect"
                type="button"
                disabled={!!busy}
                title="웹에서 트렌드 키워드를 새로 모아 후보 목록을 늘리고, 늘어난 후보를 바로 보여줍니다."
                onClick={collectToRedis}
              >
                {/* 열린 원형 화살표(↻) — 밖에서 새로 가져온다는 뜻. */}
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <path d="M20 12a8 8 0 1 1-2.4-5.7" />
                  <path d="M20 4.5V11h-6.5" />
                </svg>
                {busy === "collect" ? "찾는 중..." : "새 키워드 찾기"}
              </button>
            </div>
          )}

          {/* 카드 목록 공통 안내. 여러 출처 카드가 섞여 있으므로 특정 플랫폼 하나를 기준으로
              한 문구를 고정하지 않고, 출처별 측정 기준은 정보 아이콘의 툴팁으로 안내한다. */}
          {!collecting && allKeywords.length > 0 && (
            <p className="title-evidence-footnote">
              <span>
                {EVIDENCE_FOOTNOTE}
                {latestCollectedText ? ` 가장 최근 수집 ${latestCollectedText}` : ""}
              </span>
              <span
                className="title-evidence-info"
                tabIndex={0}
                role="note"
                aria-label={`수치 기준 안내: ${EVIDENCE_FOOTNOTE_DETAIL}`}
                title={EVIDENCE_FOOTNOTE_DETAIL}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <circle cx="12" cy="12" r="8.5" />
                  <path d="M12 11v5M12 7.6v.4" />
                </svg>
              </span>
            </p>
          )}

          {!collecting && !allKeywords.length ? (
            mode === "MATERIAL_RELATED" ? (
              // 소재 관련어가 없는 것은 오류가 아니다 — 소재와 직접 관련된 검색 키워드만 선별하다
              // 보니 적거나 없을 수 있다. 무관 키워드로 채우지 않으므로, 최신순으로 넘어갈 길을 준다.
              <div className="title-empty-state">
                <p>
                  저장된 후보와 새로 수집한 키워드 모두에서 소재와 관련된 키워드를 찾지 못했습니다.
                  {brandPicked
                    ? " 소재만으로도 제목을 만들 수 있습니다."
                    : " '최신순'에서 지금 뜨는 키워드를 골라 소재와 연결하거나, 키워드 없이 소재만으로 작성할 수 있습니다."}
                </p>
                <div className="title-empty-actions">
                  {/* 수집 실패·일시 오류일 수 있으므로 재시도 길을 함께 둔다 — 로딩이 무한히
                      이어지는 대신, 끝난 상태에서 다시 시도할 수 있다. */}
                  <button
                    className="button small title-keyword-action"
                    type="button"
                    onClick={() => collect(mode)}
                    disabled={!!busy}
                  >
                    다시 확인
                  </button>
                  {/* 브랜드를 얹은 글에는 최신순으로 넘어갈 길을 주지 않는다(2026-08-20).
                      그 목록은 소재와 무관한 실시간 인기 검색어라, 여기서 열어 주면 위에서
                      탭을 감춘 것이 무의미해진다 — 나가는 문이 하나 더 있는 셈이다. */}
                  {!brandPicked && (
                    <button
                      className="button small title-keyword-action"
                      type="button"
                      onClick={() => switchMode("TRENDING")}
                      disabled={!!busy}
                    >
                      최신순으로 보기
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <div className="title-empty-state">
                <p>불러온 트렌드 키워드가 없습니다.</p>
                <div className="title-empty-actions">
                  <button
                    className="button small title-keyword-action"
                    type="button"
                    onClick={() => collect(mode)}
                    disabled={!!busy}
                  >
                    트렌드 불러오기
                  </button>
                </div>
              </div>
            )
          ) : (
            // 로딩 패널이 떠 있는 동안에는 상태 힌트도 감춘다 — 옛 목록에 대한 설명이라서다.
            !collecting && allKeywords.length > 0 && <p className="title-status-hint">{statusHint}</p>
          )}
        </div>

        {/* 고른 키워드는 제목 후보 바로 위에 칩으로 모아 보여준다. 고른 것이 없으면 이 줄
            자체를 감춘다 — 빈 자리는 설명이 되지 못한다. */}
        {selectedKeyword && (
          <div className="title-selected-keywords">
            <span className="title-selected-keywords-label">선택한 키워드</span>
            <span className="title-keyword-pill">
              {selectedKeyword.keyword}
              <button
                type="button"
                className="title-keyword-pill-remove"
                aria-label={`선택한 키워드 '${selectedKeyword.keyword}' 해제`}
                disabled={!!busy}
                onClick={clearKeywordSelection}
              >
                ×
              </button>
            </span>
          </div>
        )}

        <div className="title-candidate-head">
          <div className="title-candidate-heading">
            <p className="title-candidate-kicker">소재·트렌드 결합</p>
            <h3>{subject ? `'${subject}' 제목 후보` : "제목 후보"}</h3>
            {selectedKeyword && (
              <p className="title-candidate-subline">
                선택한 트렌드 키워드 · {selectedKeyword.keyword}
              </p>
            )}
          </div>
          <button
            className="button small title-candidate-generate"
            type="button"
            disabled={!selectedKeyword || !!busy}
            // 화면에 있는 후보를 그대로 되돌려 보낸다(제목 + 후킹 유형 + 기본 유형).
            onClick={() => selectedKeyword && writeTitles(selectedKeyword, candidates)}
          >
            {writingTitles ? "제목 쓰는 중..." : "제목 추천"}
          </button>
        </div>

        <div className="title-candidate-list">
          {candidates.length && !writingTitles ? (
            candidates.map((item) => {
              const isSelected = item.topicCandidateId === selectedTopicId;
              return (
                <button
                  key={item.topicCandidateId}
                  type="button"
                  className={`title-candidate-item ${isSelected ? "title-candidate-item--selected" : ""} ${
                    item.recommended ? "title-candidate-item--recommended" : ""
                  }`}
                  disabled={!selectionAllowed || !!busy}
                  onClick={() => selectTopic(item.topicCandidateId)}
                >
                  <span className="title-candidate-copy">
                    <strong>{item.title}</strong>
                    {/* 추천/점수 근거 한 줄. 상세 점수는 감추고 근거만 보여준다. */}
                    {item.reason && <span className="title-candidate-reason">{item.reason}</span>}
                  </span>
                  {item.recommended && <span className="title-candidate-badge">추천</span>}
                  <span className="title-candidate-use">
                    {isSelected ? (
                      <>
                        <span aria-hidden="true">✓</span> 선택됨
                      </>
                    ) : (
                      "사용하기"
                    )}
                  </span>
                </button>
              );
            })
          ) : (
            <p className="title-candidate-empty">
              {collecting
                ? "트렌드 키워드를 불러오고 있습니다."
                : writingTitles
                  ? "선택한 트렌드 키워드로 제목을 쓰는 중입니다."
                  : selectedKeyword
                    ? `'${selectedKeyword.keyword}' 트렌드 키워드로 제목을 만들려면 제목 추천을 눌러주세요.`
                    : "키워드를 하나 고른 뒤 제목 추천을 눌러주세요. 키워드 없이 소재만으로 작성할 수도 있습니다."}
            </p>
          )}
        </div>

        <div className={`title-selected-result ${titleChosen ? "chosen" : ""}`}>
          <span className="title-selected-result-label">선택한 제목</span>
          <strong className="title-selected-result-value">
            {titleChosen ? selectedTitle : "선택 없음"}
          </strong>
        </div>

        <div className="title-action-bar">
          {/* 제목은 후보의 '사용하기'를 누르는 순간 저장되고, **단계는 여기 그대로 머문다**
              (2026-08-07 사용자 요청). 다음으로 가는 것은 이 버튼이다 — 저장과 동시에
              자료 검증이 뒤에서 돌기 시작하므로, 고른 제목을 확인하거나 다른 후보를 다시
              보는 동안 기다림이 늘지 않는다. 아직 고르지 않았을 때는 무엇을 해야 하는지
              비활성 상태로 알려 준다. */}
          {/* 여러 편을 만들 때는 **이 편을 골랐을 때만** 열린다. 상태(SEARCH_ANALYZING)는
              앞 편에서 이미 올라가 있어서, 그것만 보면 아무것도 고르지 않은 편에서도 다음으로
              넘어갈 수 있었다(2026-08-12 신고). */}
          {task?.status === "SEARCH_ANALYZING" && roundChosen ? (
            <button className="button primary title-primary-cta" type="button" onClick={onReopenVerify}>
              작성 전 검증으로
              <span aria-hidden="true">→</span>
            </button>
          ) : (
            <button
              className="button primary title-primary-cta"
              type="button"
              disabled
              title="제목 후보에서 '사용하기'를 누르면 제목이 저장되고, 이 버튼으로 다음 단계에 갑니다."
            >
              제목을 먼저 고르세요
              <span aria-hidden="true">→</span>
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
