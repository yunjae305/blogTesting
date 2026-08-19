/**
 * Trend display helpers.
 *
 * Nothing here fabricates keywords or titles. The panel used to pad itself with
 * the user's own inputs plus two hardcoded strings, all labelled "GOOGLE
 * TRENDS" — indistinguishable from collected data, and wrong. Step 2 now shows
 * what the sources actually returned, and a loading state until they do.
 */

import type {
  BlogTask,
  TopicCandidate,
  TrendKeyword,
  TrendMode,
  TrendRecommendation,
} from "../../api/types";
import { TREND_SOURCE_LABELS } from "../../constants";

/**
 * Keywords fetched at once, ranked. The backend returns this many across all live
 * sources by composite score (hotness·cross-source), deduped. The panel
 * shows INITIAL_VISIBLE_KEYWORDS first and reveals the rest with 더 보기 — it never
 * hides the current candidates to swap in a different set. Capped at the API's
 * MAX_TREND_KEYWORDS (20).
 */
export const TREND_KEYWORD_COUNT = 16;

/** How many keywords show before 더 보기 reveals the rest (accumulate, never replace). */
export const INITIAL_VISIBLE_KEYWORDS = 8;

/**
 * 트렌드 영역 상단의 두 보기 버튼. 같은 소재라도 목적이 다른 두 종류의 키워드를 완전히
 * 분리해서 준다 — 각 버튼이 서로 다른 후보 풀(수집·검증 결과)을 불러온다.
 * - 최신순(TRENDING): DB에 저장된 공용 풀을 소재별 LLM 채점 없이 인기순으로 보여준다.
 *   같은 시점이면 소재가 무엇이든 결과가 같다. 소스 API는 저장분이 아예 없을 때와
 *   '새 키워드 찾기'을 눌렀을 때만 부른다.
 * - 소재 관련순(MATERIAL_RELATED): 저장된 풀 중 소재와 관련이 확인된 키워드 전부, 관련도
 *   높은 순. 관련 후보가 하나도 없을 때만 새로 수집해 저장한다. '아무 상관 없음' 판정만
 *   빠지며, 관련 후보가 적으면 그만큼만 보여준다.
 * 초기 선택은 최신순.
 */
export const TREND_MODE_BUTTONS: {
  id: TrendMode;
  label: string;
  /** 탭 아래에 늘 보이는 한 줄. 두 탭이 무엇으로 다른지 화면에서 바로 읽히게 한다. */
  note: string;
  /** 동작까지 설명하는 긴 문장. 화면을 채우지 않고 탭의 tooltip으로만 붙는다. */
  hint: string;
}[] = [
  {
    id: "TRENDING",
    label: "최신순",
    note: "현재 가장 많이 검색된 트렌드 키워드예요.",
    hint: "모아둔 트렌드 키워드를 소재와 무관하게 인기순으로 보여줍니다. '새 키워드 찾기'으로 최신 키워드를 더할 수 있습니다.",
  },
  {
    id: "MATERIAL_RELATED",
    label: "소재 관련순",
    note: "입력한 소재와 관련 있으며 검색량이 높은 키워드예요.",
    hint: "소재와 직접 관련된 키워드를 우선 보여주며, 후보가 부족하면 관련 검색어를 추가로 수집합니다. '다른 키워드 보기'를 누르면 아직 보지 않은 후보를 이어서 보여주고, 모두 확인하면 새로운 순서로 다시 순환합니다.",
  },
];

export const DEFAULT_TREND_MODE: TrendMode = "TRENDING";

/** Titles offered per keyword. Matches TOPIC_CANDIDATE_COUNT in the API. */
const TOPIC_CANDIDATE_COUNT = 5;

export function trendSourceLabel(source: string): string {
  return TREND_SOURCE_LABELS[source] ?? String(source ?? "API").replace(/_/g, " ");
}

export function trendKeywordsForDisplay(recommendation: TrendRecommendation | null): TrendKeyword[] {
  return recommendation?.trendKeywords?.slice(0, TREND_KEYWORD_COUNT) ?? [];
}

export function topicCandidatesForDisplay(
  recommendation: TrendRecommendation | null,
): TopicCandidate[] {
  return recommendation?.topicCandidates?.slice(0, TOPIC_CANDIDATE_COUNT) ?? [];
}

/** Every source a keyword was confirmed in, for the per-row 출처 칩. Falls back
    to the single source when the backend did not attach the cross-source list.
    한 줄로 이어 붙이지 않고 배열로 준다 — 카드에서는 출처마다 칩 한 개다.
    라벨이 아니라 원본 키(GOOGLE_TRENDS 등)를 그대로 준다 — 칩이 서비스별 로고·브랜드 색을
    골라야 하므로, 표시 문구로 바꾸는 일은 칩(TrendSourceBadge)에서 한다. */
export function trendSourcesForRow(keyword: TrendKeyword): string[] {
  const sources = keyword.sources?.length ? keyword.sources : [keyword.source];
  return [...new Set(sources)];
}

export function selectedTrendKeywordIdSet(input: {
  topicCandidates: TopicCandidate[];
  selectedTopicId?: string;
  recommendation: TrendRecommendation | null;
  task: BlogTask | null;
  selectedTrendKeywordIds: string[];
  touched: boolean;
}): Set<string> {
  const { topicCandidates, selectedTopicId, recommendation, task } = input;

  if (input.touched || input.selectedTrendKeywordIds.length > 0) {
    return new Set(input.selectedTrendKeywordIds.slice(0, 1));
  }
  if (task?.trendSelection?.selectedTrendKeywordIds?.length) {
    return new Set(task.trendSelection.selectedTrendKeywordIds.slice(0, 1));
  }
  if (!recommendation) return new Set();

  const fromTopic =
    topicCandidates.find((item) => item.topicCandidateId === selectedTopicId)?.trendKeywordIds ??
    topicCandidates.find((item) => item.recommended)?.trendKeywordIds ??
    [];
  return new Set(fromTopic.slice(0, 1));
}

/**
 * Nothing. Collecting keywords used to select the first card for the user, which
 * is a choice being made on their behalf — and the one card it happened to land on
 * carried no more meaning than the three beside it.
 */
export function defaultTrendKeywordIds(): string[] {
  return [];
}

// 카드 hover 툴팁(trendScoreTooltip)은 없앴다. '트렌드 점수 56 · 단일 출처'처럼 내부
// 점수를 띄우면 그 숫자가 무엇을 뜻하는지도, 카드끼리 비교하라는 뜻인지도 알 수 없어
// 오히려 헷갈렸다(2026-08-07 사용자 결정). 카드가 스스로를 설명하는 자리는 출처별 3줄
// 지표(TrendEvidenceBlock)이고, 그쪽은 실제 수집 수치라 뜻이 분명하다.

/**
 * '추천' 배지를 붙일 카드 하나(리스트 순서와는 별개다).
 * - 최신순(TRENDING): 소재별 채점을 하지 않으므로 트렌드 강도가 가장 높은 키워드.
 * - 소재 관련순(MATERIAL_RELATED): 리스트 1위. 백엔드가 소재 관련도 게이트를 통과한 후보를
 *   관련도 내림차순으로 내려주므로, 1위가 곧 "관련된 것 중 관련도 최고"다.
 */
export function recommendedTrendKeywordId(
  keywords: TrendKeyword[],
  mode: TrendMode = DEFAULT_TREND_MODE,
): string | undefined {
  if (!keywords.length) return undefined;
  const byRank = (a: TrendKeyword, b: TrendKeyword) =>
    Number(a.rank ?? Number.MAX_SAFE_INTEGER) - Number(b.rank ?? Number.MAX_SAFE_INTEGER);
  if (mode === "MATERIAL_RELATED") {
    return [...keywords].sort(byRank)[0]?.trendKeywordId;
  }
  const trendScore = (item: TrendKeyword) => Number(item.trendScore ?? item.score ?? 0);
  return [...keywords].sort((a, b) => trendScore(b) - trendScore(a) || byRank(a, b))[0]
    ?.trendKeywordId;
}
