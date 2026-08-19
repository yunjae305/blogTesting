import { describe, expect, it } from "vitest";

import { countPublishJobs } from "./scheduleSummary";

/**
 * 일정 요약이 **거짓말하지 않는지** 본다.
 *
 * 여기서 나온 숫자가 일정 카드의 요약 줄에 그대로 실린다. '매일 오후 2시'처럼
 * 사용자가 그대로 믿을 문장이라, 사실이 아닐 때는 말하지 않아야 한다.
 */
describe("발행 작업 수", () => {
  it("소재마다 고른 플랫폼 수를 모두 더한다", () => {
    const counts = countPublishJobs(
      [["naver", "threads"], ["naver"], ["naver", "threads"], ["threads"]],
      4,
    );
    expect(counts.total).toBe(6);
    expect(counts.naver).toBe(3);
    expect(counts.threads).toBe(3);
  });

  it("글 수를 넘는 줄은 세지 않는다", () => {
    // 소재를 줄이면 platformsList가 잠깐 길게 남아 있을 수 있다.
    const counts = countPublishJobs([["naver"], ["naver", "threads"]], 1);
    expect(counts.total).toBe(1);
  });

  it("간격 방식도 같은 셈을 쓴다 — 줄마다 고른 플랫폼", () => {
    // 2026-08-06: 예전에는 간격 방식만 배치 하나의 값으로 따로 셌다(countIntervalJobs).
    // 그래서 소재 줄에 '쓰레드'라고 적어 두고도 요약은 '네이버 2건'이라고 말했다.
    const counts = countPublishJobs([["threads"], ["threads"]], 2);
    expect(counts).toEqual({ naver: 0, threads: 2, total: 2 });
  });
});
