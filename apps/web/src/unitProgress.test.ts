import { describe, expect, it } from "vitest";

import { draftProgressPercent, unitFill } from "./draftProgress";

/**
 * 개수를 아는 단계는 짐작하지 않는다(2026-08-11).
 *
 * 이미지 생성은 만들 장수가 시작할 때 정해지고 한 장 끝날 때마다 사실을 안다. 그런데도
 * 시간 곡선으로 채우면, 빨리 끝나도 천천히 차고 늦어지면 92%에 붙어 버틴다.
 */
describe("사실 비율로 채우기", () => {
  it("개수를 알면 그 비율을 쓴다", () => {
    expect(unitFill(3, 5, () => 0.1)).toBeCloseTo(0.6);
    expect(unitFill(0, 5, () => 0.4)).toBe(0);
  });

  it("개수를 모르면 시간 추정으로 돌아간다", () => {
    // 옛 문서·이 값을 보고하지 않는 단계.
    expect(unitFill(undefined, undefined, () => 0.42)).toBe(0.42);
    expect(unitFill(3, 0, () => 0.42)).toBe(0.42);
    expect(unitFill(3, undefined, () => 0.42)).toBe(0.42);
  });

  it("다 끝나도 그 칸을 꽉 채우지는 않는다", () => {
    // 마지막 한 장이 저장·후처리를 남겨 두는 동안 100%가 보이면 거짓말이 된다.
    expect(unitFill(5, 5, () => 0)).toBeLessThan(1);
  });

  it("전체 진행률이 그 사실을 반영한다", () => {
    const base = {
      steps: ["구조 설계", "본문", "이미지", "다듬기"],
      stepIndex: 2,
      elapsedInStepMs: 1_000,
    };

    const guessed = draftProgressPercent(base);
    const known = draftProgressPercent({ ...base, unitsDone: 4, unitsTotal: 5 });

    // 1초밖에 안 지났지만 4/5장을 끝냈다 — 시간 추정보다 훨씬 앞서 있어야 한다.
    expect(known).toBeGreaterThan(guessed);
  });
});
