import { describe, expect, it } from "vitest";

import type { TrendSourceEvidence } from "../../api/types";
import {
  evidenceForCard,
  evidenceRows,
  formatCompactCount,
  formatRelativeTime,
  formatSignedPercent,
  latestObservedAt,
} from "./trendEvidence";

/** 모든 상대 시각 계산의 기준. 테스트가 실제 시계에 좌우되지 않게 고정한다. */
const NOW = Date.parse("2026-08-07T12:00:00.000Z");

const hoursAgo = (hours: number) => new Date(NOW - hours * 3600_000).toISOString();

const text = (segments: { text: string }[]) => segments.map((s) => s.text).join("");

describe("formatCompactCount", () => {
  it("만·천 단위로 축약하고 작은 수는 그대로 둔다", () => {
    expect(formatCompactCount(1_380_000)).toBe("138만");
    expect(formatCompactCount(200_000)).toBe("20만");
    expect(formatCompactCount(18_300)).toBe("1.8만");
    expect(formatCompactCount(80_000)).toBe("8만");
    expect(formatCompactCount(5_000)).toBe("5천");
    expect(formatCompactCount(8_400)).toBe("8,400");
    expect(formatCompactCount(215_0000)).toBe("215만");
  });
});

describe("formatSignedPercent", () => {
  it("양수에 +와 천 단위 구분을 붙인다", () => {
    expect(formatSignedPercent(1000)).toBe("+1,000%");
    expect(formatSignedPercent(320)).toBe("+320%");
  });
});

describe("formatRelativeTime", () => {
  it("분·시간·일 단위로 말하고 없는 값은 null이다", () => {
    expect(formatRelativeTime(hoursAgo(0.05), NOW)).toBe("약 3분 전");
    expect(formatRelativeTime(hoursAgo(2), NOW)).toBe("약 2시간 전");
    expect(formatRelativeTime(hoursAgo(3 * 24), NOW)).toBe("3일 전");
    expect(formatRelativeTime(undefined, NOW)).toBeNull();
    expect(formatRelativeTime("not-a-date", NOW)).toBeNull();
  });

  it("plain이면 '약 '을 뗀다 — '업로드 8시간 전' 표기용", () => {
    expect(formatRelativeTime(hoursAgo(8), NOW, true)).toBe("8시간 전");
  });
});

function googleEvidence(overrides: Record<string, unknown> = {}): TrendSourceEvidence {
  return {
    source: "GOOGLE_TRENDS",
    observedAt: hoursAgo(0.05),
    dataOrigin: "SERPAPI",
    google: {
      active: true,
      searchVolume: 200_000,
      increasePercentage: 1000,
      startedAt: hoursAgo(2),
      ...overrides,
    },
  };
}

describe("evidenceRows — Google", () => {
  it("최신순 SerpApi 카드: 급상승 상태·검색량/상승률·시작 시각 3줄", () => {
    const rows = evidenceRows("TRENDING", "GOOGLE_TRENDS", googleEvidence(), NOW)!;

    expect(rows).toHaveLength(3);
    expect(text(rows[0].segments)).toBe("현재 급상승 중");
    expect(text(rows[1].segments)).toBe("20만+ 검색 · +1,000%");
    expect(text(rows[2].segments)).toBe("약 2시간 전 상승 시작");
    // 상승률은 강조 색을 갖는다(색만이 아니라 텍스트도 함께 있다).
    expect(rows[1].segments.at(-1)).toMatchObject({ text: "+1,000%", tone: "up" });
  });

  it("소재 관련순 SerpApi 카드: active=true의 1줄 문구가 다르다", () => {
    const rows = evidenceRows("MATERIAL_RELATED", "GOOGLE_TRENDS", googleEvidence(), NOW)!;
    expect(text(rows[0].segments)).toBe("현재 급상승 목록에서 확인");
  });

  it("active=false는 두 모드 모두 '최근 급상승 목록에서 확인'", () => {
    const rows = evidenceRows(
      "TRENDING",
      "GOOGLE_TRENDS",
      googleEvidence({ active: false }),
      NOW,
    )!;
    expect(text(rows[0].segments)).toBe("최근 급상승 목록에서 확인");
  });

  it("검색량과 상승률 중 있는 값만 표시한다 — 없는 값에 +0%를 만들지 않는다", () => {
    const rows = evidenceRows(
      "TRENDING",
      "GOOGLE_TRENDS",
      googleEvidence({ increasePercentage: null }),
      NOW,
    )!;
    expect(text(rows[1].segments)).toBe("20만+ 검색");
    expect(text(rows[1].segments)).not.toContain("%");
  });

  it("RSS 폴백: 근사 검색량과 수집 시각만 말하고, 상승률·시작 시각을 지어내지 않는다", () => {
    const rows = evidenceRows(
      "TRENDING",
      "GOOGLE_TRENDS",
      {
        source: "GOOGLE_TRENDS",
        observedAt: hoursAgo(0.05),
        dataOrigin: "GOOGLE_RSS",
        google: { approximateTraffic: 5000, feedType: "GOOGLE_RSS" },
      },
      NOW,
    )!;

    expect(text(rows[0].segments)).toBe("Google 공식 급상승 피드 확인");
    expect(text(rows[1].segments)).toBe("약 5천+ 검색");
    expect(text(rows[2].segments)).toBe("약 3분 전 수집");
    const all = rows.map((row) => text(row.segments)).join(" ");
    expect(all).not.toContain("%");
    expect(all).not.toContain("상승 시작");
  });

  it("시작 시각이 없으면 정직한 보조 문구를 쓴다", () => {
    const rows = evidenceRows(
      "TRENDING",
      "GOOGLE_TRENDS",
      googleEvidence({ startedAt: null }),
      NOW,
    )!;
    expect(rows[2].segments).toEqual([{ text: "상승 시작 시각 미제공", tone: "muted" }]);
  });
});

describe("evidenceRows — Naver", () => {
  const naverEvidence: TrendSourceEvidence = {
    source: "NAVER_DATALAB",
    observedAt: hoursAgo(0.05),
    dataOrigin: "NAVER_SEARCH_API",
    naver: {
      recentNewsCount: 184,
      collectedBlogCount: 63,
      collectedRelatedContentCount: 247,
      sampledDocumentCount: 500,
      basis: "SEARCH_API_SAMPLE",
    },
  };

  it.each(["TRENDING", "MATERIAL_RELATED"] as const)(
    "%s 카드: 이번 수집 표본에서 확인한 문서 수 3줄",
    (mode) => {
      const rows = evidenceRows(mode, "NAVER_DATALAB", naverEvidence, NOW)!;

      expect(rows).toHaveLength(3);
      expect(text(rows[0].segments)).toBe("최근 24시간 확인 뉴스 184건");
      expect(text(rows[1].segments)).toBe("이번 수집 확인 블로그 63건");
      expect(text(rows[2].segments)).toBe("이번 수집 관련 콘텐츠 247건");
      // '네이버 전체'로 오인할 문구를 쓰지 않는다.
      const all = rows.map((row) => text(row.segments)).join(" ");
      expect(all).not.toContain("전체");
    },
  );

  it("0건은 실측이므로 그대로 적고, 없는 값(null)만 보조 문구로 대신한다", () => {
    const rows = evidenceRows(
      "TRENDING",
      "NAVER_DATALAB",
      {
        ...naverEvidence,
        naver: { recentNewsCount: 0, collectedBlogCount: null, collectedRelatedContentCount: 5 },
      },
      NOW,
    )!;
    expect(text(rows[0].segments)).toBe("최근 24시간 확인 뉴스 0건");
    expect(rows[1].segments[0].tone).toBe("muted");
    expect(text(rows[2].segments)).toBe("이번 수집 관련 콘텐츠 5건");
  });
});

describe("evidenceRows — Naver 보강 경로(총수 기준)", () => {
  const measured: TrendSourceEvidence = {
    source: "NAVER_DATALAB",
    observedAt: hoursAgo(0.05),
    dataOrigin: "NAVER_SEARCH_API",
    naver: {
      totalNewsCount: 12_340,
      totalBlogCount: 45_600,
      recentDocumentCount: 8,
      recentHitCap: false,
      basis: "SEARCH_API_TOTAL",
    },
  };

  it("표본이 아니라 검색 결과 총수라고 말한다", () => {
    const rows = evidenceRows("MATERIAL_RELATED", "NAVER_DATALAB", measured, NOW)!;

    expect(text(rows[0].segments)).toBe("네이버 뉴스 검색 결과 12,340건");
    expect(text(rows[1].segments)).toBe("네이버 블로그 검색 결과 45,600건");
    expect(text(rows[2].segments)).toBe("최근 24시간 새 뉴스 8건");
    // 표본 경로의 문구를 쓰면 어느 쪽도 사실이 아니게 된다.
    const all = rows.map((row) => text(row.segments)).join(" ");
    expect(all).not.toContain("이번 수집");
  });

  it("표본 상한에 걸린 최근 집계는 '+'로 적는다", () => {
    const rows = evidenceRows(
      "MATERIAL_RELATED",
      "NAVER_DATALAB",
      { ...measured, naver: { ...measured.naver, recentDocumentCount: 50, recentHitCap: true } },
      NOW,
    )!;
    expect(text(rows[2].segments)).toBe("최근 24시간 새 뉴스 50건+");
  });
});

describe("evidenceForCard", () => {
  const naver: TrendSourceEvidence = {
    source: "NAVER_DATALAB",
    naver: { totalNewsCount: 10, basis: "SEARCH_API_TOTAL" },
  };

  it("대표 출처에 근거가 있으면 그것을 쓴다", () => {
    const google: TrendSourceEvidence = { source: "GOOGLE_TRENDS", google: { searchVolume: 100 } };
    const shown = evidenceForCard("GOOGLE_TRENDS", { GOOGLE_TRENDS: google, NAVER_DATALAB: naver });
    expect(shown).toMatchObject({ source: "GOOGLE_TRENDS", measuredElsewhere: false });
  });

  it("대표 출처에 수치가 없으면 실제로 잰 출처로 넘어간다", () => {
    // 구글 자동완성은 소재 연관 키워드를 주지만 검색량을 주지 않는다 — 네이버가 잰다.
    const shown = evidenceForCard("GOOGLE_TRENDS", { NAVER_DATALAB: naver });
    expect(shown).toMatchObject({ source: "NAVER_DATALAB", measuredElsewhere: true });
  });

  it("아무 근거도 없으면 null이다 — 다른 출처 수치를 끌어다 붙이지 않는다", () => {
    expect(evidenceForCard("GOOGLE_TRENDS", undefined)).toBeNull();
    expect(evidenceForCard("GOOGLE_TRENDS", {})).toBeNull();
  });
});

describe("evidenceRows — YouTube", () => {
  it("최신순 카드: 대표 조회수·업로드 시점·업로드 후 시간당 평균", () => {
    const rows = evidenceRows(
      "TRENDING",
      "YOUTUBE",
      {
        source: "YOUTUBE",
        observedAt: hoursAgo(0.05),
        dataOrigin: "YOUTUBE_API",
        youtube: {
          topViewCount: 1_380_000,
          topVideoPublishedAt: hoursAgo(8),
          averageViewsPerHour: 160_000,
        },
      },
      NOW,
    )!;

    expect(text(rows[0].segments)).toBe("인기 영상 138만 조회");
    expect(text(rows[1].segments)).toBe("업로드 8시간 전");
    // '현재 시간당'이 아니라 '업로드 후 시간당 평균' — 실시간 조회 속도가 아니다.
    expect(text(rows[2].segments)).toBe("업로드 후 시간당 평균 16만 조회");
  });

  it("소재 관련순 카드: 관련 상위 조회수·최근 7일 영상 수·시간당 평균", () => {
    const rows = evidenceRows(
      "MATERIAL_RELATED",
      "YOUTUBE",
      {
        source: "YOUTUBE",
        observedAt: hoursAgo(0.05),
        dataOrigin: "YOUTUBE_API",
        youtube: {
          topViewCount: 840_000,
          averageViewsPerHour: 18_000,
          recentVideoCount: 18,
          recentWindowDays: 7,
        },
      },
      NOW,
    )!;

    expect(text(rows[0].segments)).toBe("관련 상위 영상 84만 조회");
    expect(text(rows[1].segments)).toBe("최근 7일 관련 영상 18개");
    expect(text(rows[2].segments)).toBe("업로드 후 시간당 평균 1.8만 조회");
  });

  it("최근 7일 영상 0개는 숨기지 않고 '없음'으로 말한다", () => {
    const rows = evidenceRows(
      "MATERIAL_RELATED",
      "YOUTUBE",
      {
        source: "YOUTUBE",
        dataOrigin: "YOUTUBE_API",
        youtube: { topViewCount: 1000, recentVideoCount: 0, averageViewsPerHour: 10 },
      },
      NOW,
    )!;
    expect(text(rows[1].segments)).toBe("최근 7일 관련 영상 없음");
  });

  it("시간당 평균이 1회 미만이면 '0 조회'로 반올림하지 않는다", () => {
    // 몇 년 전 CF는 누적 797만 조회여도 시간당으로 나누면 0.05가 된다. '평균 0 조회'라고
    // 적으면 아무도 안 본 영상처럼 읽힌다 — 실제로 화면에 그렇게 떴다.
    const rows = evidenceRows(
      "MATERIAL_RELATED",
      "YOUTUBE",
      {
        source: "YOUTUBE",
        dataOrigin: "YOUTUBE_API",
        youtube: { topViewCount: 4025, averageViewsPerHour: 0.05, recentVideoCount: 0 },
      },
      NOW,
    )!;
    expect(text(rows[2].segments)).toBe("업로드 후 시간당 평균 1회 미만 조회");
    expect(text(rows[2].segments)).not.toContain("평균 0 조회");
  });

  it("조회수 통계가 없으면 0회를 지어내지 않고 미제공이라고 말한다", () => {
    const rows = evidenceRows(
      "TRENDING",
      "YOUTUBE",
      { source: "YOUTUBE", dataOrigin: "YOUTUBE_API", youtube: { topVideoPublishedAt: hoursAgo(8) } },
      NOW,
    )!;
    expect(rows[0].segments).toEqual([{ text: "조회수 통계 미제공", tone: "muted" }]);
    const all = rows.map((row) => text(row.segments)).join(" ");
    expect(all).not.toContain("0 조회");
  });
});

describe("evidenceRows — 근거가 없을 때", () => {
  it("evidence 자체가 없으면 null — 호출한 쪽이 중립 문구를 그린다", () => {
    expect(evidenceRows("TRENDING", "GOOGLE_TRENDS", undefined, NOW)).toBeNull();
  });

  it("출처와 세부 근거가 어긋나면 지표를 만들지 않는다", () => {
    expect(
      evidenceRows(
        "TRENDING",
        "GOOGLE_TRENDS",
        { source: "GOOGLE_TRENDS", naver: { recentNewsCount: 5 } },
        NOW,
      ),
    ).toBeNull();
  });
});

describe("latestObservedAt", () => {
  it("보이는 키워드 근거 중 가장 최신 관측 시각을 고른다", () => {
    const keywords: { evidenceBySource?: Record<string, TrendSourceEvidence> }[] = [
      { evidenceBySource: { GOOGLE_TRENDS: { source: "GOOGLE_TRENDS", observedAt: "2026-08-07T10:00:00.000Z" } } },
      { evidenceBySource: { NAVER_DATALAB: { source: "NAVER_DATALAB", observedAt: "2026-08-07T11:30:00.000Z" } } },
      {},
    ];
    expect(latestObservedAt(keywords)).toBe("2026-08-07T11:30:00.000Z");
  });

  it("근거가 하나도 없으면 null — 없는 시각을 지어내지 않는다", () => {
    expect(latestObservedAt([{}, {}])).toBeNull();
  });
});
