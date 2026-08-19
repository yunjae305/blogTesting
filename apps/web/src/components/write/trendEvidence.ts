/**
 * 트렌드 카드 3줄 지표의 문구·숫자 포맷.
 *
 * 여기 있는 모든 숫자는 백엔드가 실제 API 응답에서 계산해 내려준 값이다 — 이 파일은
 * 표기만 다듬고, 없는 값을 0이나 임의 수치로 채우지 않는다. 값이 없으면 그 줄은
 * 정직한 보조 문구(muted)로 남는다.
 */

import type { TrendMode, TrendSourceEvidence } from "../../api/types";

/** 카드 지표를 그리는 출처. Instagram·소재 확장은 근거를 만들지 않으므로 여기 없다. */
export const EVIDENCE_SOURCES = new Set(["GOOGLE_TRENDS", "NAVER_DATALAB", "YOUTUBE"]);

type EvidenceTone = "up" | "muted";

interface EvidenceSegment {
  text: string;
  tone?: EvidenceTone;
}

interface EvidenceRow {
  id: string;
  /** TrendEvidenceBlock의 인라인 SVG 아이콘 키. 첫 줄은 아이콘 대신 출처 로고를 쓴다. */
  icon: string;
  segments: EvidenceSegment[];
}

/** 카드 목록 아래 공통 안내 한 줄. 여러 출처 카드가 섞여 있으므로 특정 플랫폼 기준으로
    고정하지 않는다. */
export const EVIDENCE_FOOTNOTE =
  "표시 수치는 각 출처의 최근 수집 결과이며, 출처마다 측정 기준이 다릅니다.";

/** 안내의 정보 아이콘(hover/focus)에 붙는 상세 기준. */
export const EVIDENCE_FOOTNOTE_DETAIL =
  "Google: 급상승 검색량과 상승률 · YouTube: 영상 누적 조회수와 게시 후 시간당 평균 · " +
  "Naver: 이번 검색 API 수집 표본에서 확인된 콘텐츠 수";

/** 네이버 수치가 무엇을 잰 것인지의 접근성 설명. 전체 검색량으로 오인하지 않게 한다. */
export const NAVER_SCOPE_NOTE =
  "네이버 검색 API의 이번 수집 결과에서 키워드가 확인된 문서 수입니다. " +
  "네이버 전체 검색량이나 전체 게시물 수를 의미하지 않습니다.";

/** 근거가 없는 카드(옛 캐시·근거 없는 출처)의 중립 문구. 수치를 지어내지 않는다. */
export const EVIDENCE_FALLBACK = "상세 지표는 새 수집 후 표시됩니다";

const trimmedFixed = (value: number, digits: number): string => {
  const text = value.toFixed(digits);
  return text.endsWith(".0") ? text.slice(0, -2) : text;
};

/**
 * 큰 수의 한국어 축약: 1,380,000 → "138만", 18,300 → "1.8만", 5,000 → "5천",
 * 8,400 → "8,400". 카드 폭이 좁아 긴 수는 말줄임 대신 축약한다.
 */
export function formatCompactCount(value: number): string {
  const rounded = Math.round(value);
  if (!Number.isFinite(rounded) || rounded < 0) return String(value);
  if (rounded >= 100_000_000) return `${trimmedFixed(rounded / 100_000_000, 1)}억`;
  if (rounded >= 10_000) {
    const man = rounded / 10_000;
    return man >= 10 ? `${Math.round(man)}만` : `${trimmedFixed(man, 1)}만`;
  }
  // "5천+ 검색"처럼 딱 떨어지는 천 단위만 축약한다. 8,400은 그대로 둔다.
  if (rounded >= 1_000 && rounded % 1_000 === 0) return `${rounded / 1_000}천`;
  return rounded.toLocaleString("ko-KR");
}

/** 상승률: 1000 → "+1,000%". 부호는 값이 말한다 — 없는 상승률에 +0%를 만들지 않는다. */
export function formatSignedPercent(value: number): string {
  const rounded = Math.round(value);
  const sign = rounded > 0 ? "+" : "";
  return `${sign}${rounded.toLocaleString("ko-KR")}%`;
}

/**
 * 상대 시각: 50분 미만 "약 N분 전", 그 위는 "약 N시간 전", 하루가 넘으면 "N일 전",
 * 지나치게 오래되면 날짜. plain이면 "약 "을 뗀다("업로드 8시간 전" 표기용).
 */
export function formatRelativeTime(
  iso: string | null | undefined,
  nowMs: number = Date.now(),
  plain = false,
): string | null {
  if (!iso) return null;
  const time = Date.parse(iso);
  if (Number.isNaN(time)) return null;
  const diffMinutes = Math.max(0, Math.round((nowMs - time) / 60_000));

  let text: string;
  if (diffMinutes < 1) return "방금 전";
  else if (diffMinutes < 50) text = `약 ${diffMinutes}분 전`;
  else if (diffMinutes < 24 * 60) text = `약 ${Math.max(1, Math.round(diffMinutes / 60))}시간 전`;
  else if (diffMinutes < 30 * 24 * 60) return `${Math.round(diffMinutes / (24 * 60))}일 전`;
  else {
    const date = new Date(time);
    return `${date.getFullYear()}.${String(date.getMonth() + 1).padStart(2, "0")}.${String(
      date.getDate(),
    ).padStart(2, "0")}`;
  }
  return plain ? text.replace(/^약 /, "") : text;
}

const row = (id: string, icon: string, segments: EvidenceSegment[]): EvidenceRow => ({
  id,
  icon,
  segments,
});

const mutedRow = (id: string, icon: string, text: string): EvidenceRow =>
  row(id, icon, [{ text, tone: "muted" }]);

function googleRows(
  mode: TrendMode,
  evidence: TrendSourceEvidence,
  nowMs: number,
): EvidenceRow[] {
  const google = evidence.google ?? {};
  const isRss = evidence.dataOrigin === "GOOGLE_RSS" || google.feedType === "GOOGLE_RSS";

  if (isRss) {
    // RSS 폴백에는 상승률·시작 시각이 없다 — 있는 값(근사 검색량·수집 시각)만 말한다.
    const collected = formatRelativeTime(evidence.observedAt, nowMs);
    return [
      row("status", "logo", [{ text: "Google 공식 급상승 피드 확인" }]),
      google.approximateTraffic != null
        ? row("volume", "chart", [
            { text: `약 ${formatCompactCount(google.approximateTraffic)}+ 검색` },
          ])
        : mutedRow("volume", "chart", EVIDENCE_FALLBACK),
      collected
        ? row("observed", "clock", [{ text: `${collected} 수집` }])
        : mutedRow("observed", "clock", "수집 시각 미제공"),
    ];
  }

  const statusText =
    google.active === true
      ? mode === "MATERIAL_RELATED"
        ? "현재 급상승 목록에서 확인"
        : "현재 급상승 중"
      : google.active === false
        ? "최근 급상승 목록에서 확인"
        : "급상승 목록에서 확인";

  const metrics: EvidenceSegment[] = [];
  if (google.searchVolume != null) {
    metrics.push({ text: `${formatCompactCount(google.searchVolume)}+ 검색` });
  }
  if (google.increasePercentage != null) {
    if (metrics.length) metrics.push({ text: " · " });
    metrics.push({ text: formatSignedPercent(google.increasePercentage), tone: "up" });
  }

  const started = formatRelativeTime(google.startedAt, nowMs);
  return [
    row("status", "logo", [{ text: statusText }]),
    metrics.length ? row("volume", "chart", metrics) : mutedRow("volume", "chart", "검색량·상승률 미제공"),
    started
      ? row("started", "clock", [{ text: `${started} 상승 시작` }])
      : mutedRow("started", "clock", "상승 시작 시각 미제공"),
  ];
}

function naverRows(evidence: TrendSourceEvidence): EvidenceRow[] {
  const naver = evidence.naver ?? {};
  const count = (value: number) => value.toLocaleString("ko-KR");

  // 보강 경로: 키워드 자체를 검색해 네이버가 세어 준 총수다. 표본 수치와 같은 문장을 쓰면
  // 어느 쪽도 사실이 아니게 되므로 문구를 달리한다.
  if (naver.basis === "SEARCH_API_TOTAL") {
    return [
      naver.totalNewsCount != null
        ? row("news", "news", [{ text: `네이버 뉴스 검색 결과 ${count(naver.totalNewsCount)}건` }])
        : mutedRow("news", "news", EVIDENCE_FALLBACK),
      naver.totalBlogCount != null
        ? row("blog", "speech", [
            { text: `네이버 블로그 검색 결과 ${count(naver.totalBlogCount)}건` },
          ])
        : mutedRow("blog", "speech", "블로그 검색 결과 미제공"),
      naver.recentDocumentCount != null
        ? row("related", "docs", [
            {
              // 날짜순 응답을 세므로 상한에 걸릴 수 있다. 그때 정확히 N건이라고 하면 거짓이다.
              text: `최근 24시간 새 뉴스 ${count(naver.recentDocumentCount)}건${
                naver.recentHitCap ? "+" : ""
              }`,
            },
          ])
        : mutedRow("related", "docs", "최근 24시간 집계 미제공"),
    ];
  }

  // 발굴 경로: '이번 API 수집 표본에서 실제 확인한 고유 문서 수'다. 0도 실측이므로
  // 숨기지 않고 그대로 적는다 — 없는 값(null)만 보조 문구로 대신한다.
  return [
    naver.recentNewsCount != null
      ? row("news", "news", [
          { text: `최근 24시간 확인 뉴스 ${naver.recentNewsCount.toLocaleString("ko-KR")}건` },
        ])
      : mutedRow("news", "news", EVIDENCE_FALLBACK),
    naver.collectedBlogCount != null
      ? row("blog", "speech", [
          { text: `이번 수집 확인 블로그 ${naver.collectedBlogCount.toLocaleString("ko-KR")}건` },
        ])
      : mutedRow("blog", "speech", "블로그 확인 수 미제공"),
    naver.collectedRelatedContentCount != null
      ? row("related", "docs", [
          {
            text: `이번 수집 관련 콘텐츠 ${naver.collectedRelatedContentCount.toLocaleString(
              "ko-KR",
            )}건`,
          },
        ])
      : mutedRow("related", "docs", "관련 콘텐츠 수 미제공"),
  ];
}

function youtubeRows(
  mode: TrendMode,
  evidence: TrendSourceEvidence,
  nowMs: number,
): EvidenceRow[] {
  const youtube = evidence.youtube ?? {};
  const viewsLabel = mode === "MATERIAL_RELATED" ? "관련 상위 영상" : "인기 영상";
  const windowDays = youtube.recentWindowDays ?? 7;

  const viewsRow =
    youtube.topViewCount != null
      ? row("views", "logo", [
          { text: `${viewsLabel} ${formatCompactCount(youtube.topViewCount)} 조회` },
        ])
      : mutedRow("views", "logo", "조회수 통계 미제공");

  let middle: EvidenceRow;
  if (mode === "MATERIAL_RELATED") {
    middle =
      youtube.recentVideoCount != null
        ? row("recent", "video", [
            {
              text:
                youtube.recentVideoCount > 0
                  ? `최근 ${windowDays}일 관련 영상 ${youtube.recentVideoCount}개`
                  : `최근 ${windowDays}일 관련 영상 없음`,
            },
          ])
        : mutedRow("recent", "video", "최근 영상 수 미제공");
  } else {
    const uploaded = formatRelativeTime(youtube.topVideoPublishedAt, nowMs, true);
    middle = uploaded
      ? row("uploaded", "video", [{ text: `업로드 ${uploaded}` }])
      : mutedRow("uploaded", "video", "게시 시각 미제공");
  }

  // 누적 조회수 ÷ 게시 후 경과시간이다. '현재 시간당 N 조회'라고 쓰지 않는다 —
  // 실시간 조회 속도가 아니기 때문이다.
  //
  // 1 미만은 '0 조회'로 반올림하지 않는다. 오래된 영상(몇 년 전 CF 등)은 누적 조회수가
  // 수백만이어도 시간당으로 나누면 0.05 같은 값이 되는데, 화면에 '시간당 평균 0 조회'라고
  // 적으면 아무도 안 본 영상처럼 읽힌다 — 실제로 화면에 그렇게 떴다.
  const rateRow =
    youtube.averageViewsPerHour != null
      ? row("rate", "rate", [
          {
            text:
              youtube.averageViewsPerHour > 0 && youtube.averageViewsPerHour < 1
                ? "업로드 후 시간당 평균 1회 미만 조회"
                : `업로드 후 시간당 평균 ${formatCompactCount(
                    youtube.averageViewsPerHour,
                  )} 조회`,
          },
        ])
      : mutedRow("rate", "rate", "시간당 평균 미제공");

  return [viewsRow, middle, rateRow];
}

/**
 * 대표 출처의 3줄 지표. 근거가 아예 없거나(옛 데이터) 그 출처의 세부 근거가 없으면
 * null — 호출한 쪽이 중립 문구로 대신한다. 없는 수치를 만들어 채우지 않는다.
 */
export function evidenceRows(
  mode: TrendMode,
  source: string,
  evidence: TrendSourceEvidence | null | undefined,
  nowMs: number = Date.now(),
): EvidenceRow[] | null {
  if (!evidence) return null;
  if (source === "GOOGLE_TRENDS" && evidence.google) return googleRows(mode, evidence, nowMs);
  if (source === "NAVER_DATALAB" && evidence.naver) return naverRows(evidence);
  if (source === "YOUTUBE" && evidence.youtube) return youtubeRows(mode, evidence, nowMs);
  return null;
}

/**
 * 카드가 실제로 그릴 근거를 고른다.
 *
 * 대표 출처(item.source)의 근거가 있으면 그것이다. 없을 때는 **근거가 있는 다른 출처**로
 * 넘어간다 — 구글 자동완성이 소재 연관 키워드를 내놓고 네이버가 그 수치를 재는 경우가
 * 그렇다(구글은 소재별 검색량을 주지 않는다). 이때 잰 곳은 네이버이므로 화면은 두 출처를
 * 함께 표시한다: 제안한 곳과 잰 곳을 바꿔치기하지 않는다.
 */
export function evidenceForCard(
  source: string,
  evidenceBySource: Record<string, TrendSourceEvidence> | undefined,
): { source: string; evidence: TrendSourceEvidence; measuredElsewhere: boolean } | null {
  if (!evidenceBySource) return null;
  const own = evidenceBySource[source];
  if (own) return { source, evidence: own, measuredElsewhere: false };
  // 지표를 그릴 수 있는 출처만 후보다(EVIDENCE_SOURCES). 순서는 결정적이어야 한다.
  for (const candidate of EVIDENCE_SOURCES) {
    const evidence = evidenceBySource[candidate];
    if (evidence) return { source: candidate, evidence, measuredElsewhere: true };
  }
  return null;
}

/**
 * 화면에 보이는 키워드들의 근거 중 가장 최근 관측 시각. 하단 안내의
 * "가장 최근 수집 N분 전"이 이 값을 쓴다(없으면 null — 응답 생성 시각으로 폴백).
 */
export function latestObservedAt(
  keywords: { evidenceBySource?: Record<string, TrendSourceEvidence> }[],
): string | null {
  let latest: string | null = null;
  for (const keyword of keywords) {
    for (const evidence of Object.values(keyword.evidenceBySource ?? {})) {
      const observed = evidence.observedAt;
      if (observed && (!latest || observed > latest)) latest = observed;
    }
  }
  return latest;
}
