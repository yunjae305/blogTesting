import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { TrendMode, TrendSourceEvidence } from "../../api/types";
import { TrendEvidenceBlock } from "./TrendEvidenceBlock";

/**
 * 카드 안 3줄 지표 블록. 여기 나오는 숫자는 전부 백엔드가 실제 수집에서 계산한 값이고,
 * 이 컴포넌트는 표기만 한다 — 값이 없으면 지어내지 않고 중립 문구로 남는지를 본다.
 */
const NOW = Date.parse("2026-08-07T12:00:00.000Z");
const hoursAgo = (hours: number) => new Date(NOW - hours * 3600_000).toISOString();

describe("TrendEvidenceBlock", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function render(mode: TrendMode, source: string, evidence?: TrendSourceEvidence | null) {
    act(() =>
      root.render(
        <TrendEvidenceBlock mode={mode} source={source} evidence={evidence} now={NOW} />,
      ),
    );
    return container.querySelector<HTMLElement>(".title-evidence");
  }

  it("Google 카드: 로고 머리줄 + 3줄 지표를 그린다", () => {
    const block = render("TRENDING", "GOOGLE_TRENDS", {
      source: "GOOGLE_TRENDS",
      observedAt: hoursAgo(0.05),
      dataOrigin: "SERPAPI",
      google: {
        active: true,
        searchVolume: 80_000,
        increasePercentage: 320,
        startedAt: hoursAgo(3),
      },
    });

    expect(block).not.toBeNull();
    const rows = block!.querySelectorAll(".title-evidence-row");
    expect(rows).toHaveLength(3);
    // 첫 줄은 출처 로고(sr-only 이름 포함)와 근거 제목이다.
    expect(rows[0].querySelector(".sr-only")?.textContent).toBe("Google");
    expect(rows[0].textContent).toContain("현재 급상승 중");
    expect(rows[1].textContent).toBe("8만+ 검색 · +320%");
    expect(rows[2].textContent).toBe("약 3시간 전 상승 시작");
    // 상승률 강조는 색과 함께 클래스로 남는다.
    expect(block!.querySelector(".title-evidence-up")?.textContent).toBe("+320%");
  });

  it("Naver 카드: 수치 3줄과 측정 범위 접근성 설명을 함께 둔다", () => {
    const block = render("TRENDING", "NAVER_DATALAB", {
      source: "NAVER_DATALAB",
      observedAt: hoursAgo(0.1),
      dataOrigin: "NAVER_SEARCH_API",
      naver: {
        recentNewsCount: 184,
        collectedBlogCount: 63,
        collectedRelatedContentCount: 247,
      },
    });

    const rows = block!.querySelectorAll(".title-evidence-row");
    expect(rows[0].textContent).toContain("최근 24시간 확인 뉴스 184건");
    expect(rows[1].textContent).toBe("이번 수집 확인 블로그 63건");
    expect(rows[2].textContent).toBe("이번 수집 관련 콘텐츠 247건");
    // 전체 검색량으로 오인하지 않도록 하는 안내는 읽어 주는 쪽에 있다.
    expect(block!.textContent).toContain("네이버 전체 검색량이나 전체 게시물 수를 의미하지 않습니다");
  });

  it("YouTube 소재 관련순 카드: 관련 영상 지표 3줄", () => {
    const block = render("MATERIAL_RELATED", "YOUTUBE", {
      source: "YOUTUBE",
      observedAt: hoursAgo(0.05),
      dataOrigin: "YOUTUBE_API",
      youtube: {
        topViewCount: 630_000,
        averageViewsPerHour: 13_000,
        recentVideoCount: 12,
        recentWindowDays: 7,
      },
    });

    const rows = block!.querySelectorAll(".title-evidence-row");
    expect(rows[0].querySelector(".sr-only")?.textContent).toBe("YouTube");
    expect(rows[0].textContent).toContain("관련 상위 영상 63만 조회");
    expect(rows[1].textContent).toBe("최근 7일 관련 영상 12개");
    expect(rows[2].textContent).toBe("업로드 후 시간당 평균 1.3만 조회");
  });

  it("근거가 없는 옛 데이터: 수치를 지어내지 않고 로고 + 중립 문구만 남긴다", () => {
    const block = render("TRENDING", "GOOGLE_TRENDS", undefined);

    expect(block).not.toBeNull();
    expect(block!.textContent).toContain("상세 지표는 새 수집 후 표시됩니다");
    // 가짜 0이 없다.
    expect(block!.textContent).not.toMatch(/0[%건개]/);
    // 로고는 그대로 남는다.
    expect(block!.querySelector(".title-keyword-source--google")).not.toBeNull();
  });

  it("Instagram·소재 확장은 지표 대상이 아니다 — 블록을 그리지 않는다", () => {
    expect(render("TRENDING", "INSTAGRAM", undefined)).toBeNull();
    expect(render("TRENDING", "RELATED_EXPANSION", undefined)).toBeNull();
  });
});
