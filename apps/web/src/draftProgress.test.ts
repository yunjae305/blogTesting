import { beforeEach, describe, expect, it } from "vitest";

import {
  DRAFT_STEP_SECONDS,
  draftProgressPercent,
  draftStepSeconds,
  elapsedSince,
  formatElapsed,
  loadObservedStepSeconds,
  recordObservedStepSeconds,
  stepPercent,
  stepWeights,
} from "./draftProgress";

const STEPS = ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·최종본"];

function at(stepIndex: number, elapsedSeconds: number, extra = {}) {
  return {
    steps: STEPS,
    stepIndex,
    elapsedInStepMs: elapsedSeconds * 1000,
    ...extra,
  };
}

beforeEach(() => {
  // 관측값은 브라우저에 남는다. 테스트끼리 새면 다른 테스트의 기대가 흔들린다.
  window.localStorage.clear();
});

describe("관측한 실제 소요로 스스로 보정한다", () => {
  it("첫 실행에는 추정치를 쓴다", () => {
    expect(loadObservedStepSeconds(4)).toBeNull();
    expect(draftStepSeconds(4)).toEqual(DRAFT_STEP_SECONDS);
  });

  it("한 단계가 실제로 오래 걸리면 다음 실행에서 그 몫이 커진다", () => {
    const before = stepWeights(4)[2];

    // 카드 이미지 생성이 3분 걸린 실행을 한 번 겪었다.
    recordObservedStepSeconds(2, 180, 4);

    const after = stepWeights(4)[2];
    expect(after).toBeGreaterThan(before);
    expect(stepWeights(4).reduce((sum, w) => sum + w, 0)).toBeCloseTo(1);
  });

  it("한 번의 이상치가 값을 통째로 덮어쓰지 않는다", () => {
    recordObservedStepSeconds(2, 180, 4);
    const observed = loadObservedStepSeconds(4)!;

    // 45초 추정치와 180초 관측 사이 어딘가여야 한다(지수이동평균).
    expect(observed[2]).toBeGreaterThan(DRAFT_STEP_SECONDS[2]);
    expect(observed[2]).toBeLessThan(180);
    // 겪지 않은 단계는 그대로다.
    expect(observed[0]).toBe(DRAFT_STEP_SECONDS[0]);
  });

  it("말이 안 되는 값은 버린다 — 탭이 멈춰 있던 시간을 단계 소요로 삼지 않는다", () => {
    recordObservedStepSeconds(1, 6 * 60 * 60, 4);
    recordObservedStepSeconds(1, 0, 4);
    expect(loadObservedStepSeconds(4)).toBeNull();
  });

  it("서버가 다른 단계 목록을 보내면 기록하지 않는다", () => {
    recordObservedStepSeconds(0, 30, 2);
    expect(loadObservedStepSeconds(4)).toBeNull();
    expect(draftStepSeconds(2)).toEqual([1, 1]);
  });
});

describe("stepWeights", () => {
  it("splits the bar by how long each step actually takes", () => {
    const weights = stepWeights(STEPS.length);

    expect(weights).toHaveLength(4);
    expect(weights.reduce((sum, w) => sum + w, 0)).toBeCloseTo(1);
    // 2026-08-11 실측으로 뒤집힌 자리다. 예전에는 "카드 이미지가 가장 오래 걸린다"고
    // 적어 두고 그렇게 못박았는데, 실제로는 **사실 검수·다듬기가 가장 길다**
    // (155초 > 구조 설계 115초 > 이미지 94초 > 본문 50초).
    expect(weights[3]).toBeGreaterThan(weights[2]);
    expect(weights[0]).toBeGreaterThan(weights[1]);
  });

  it("falls back to an even split when the server reports a different step list", () => {
    expect(stepWeights(2)).toEqual([0.5, 0.5]);
    expect(stepWeights(0)).toEqual([]);
  });
});

describe("draftProgressPercent", () => {
  it("moves while a single step stays put", () => {
    // 서버가 알려 주는 것은 '2단계 진행 중'뿐이다. 그 안에서도 막대가 움직여야 한다.
    const early = draftProgressPercent(at(1, 5));
    const later = draftProgressPercent(at(1, 40));

    expect(later).toBeGreaterThan(early);
  });

  it("never reaches 100 before the draft exists", () => {
    // 한 단계에 한 시간이 걸려도 '다 됐다'고 말하지 않는다.
    expect(draftProgressPercent(at(3, 3600))).toBeLessThan(100);
    expect(draftProgressPercent(at(0, 100000))).toBeLessThan(100);
  });

  it("counts finished steps in full and only estimates the running one", () => {
    const weights = stepWeights(STEPS.length);
    const settled = (weights[0] + weights[1]) * 100;

    // 3단계에 막 들어선 시점 = 앞의 두 단계 몫은 확정, 세 번째는 이제 시작.
    expect(draftProgressPercent(at(2, 0))).toBe(Math.floor(settled));
    expect(draftProgressPercent(at(2, 20))).toBeGreaterThan(settled);
  });

  it("only shows 100 when the draft is actually there", () => {
    expect(draftProgressPercent(at(3, 10, { done: true }))).toBe(100);
  });

  it("stops where it stopped when generation failed", () => {
    const weights = stepWeights(STEPS.length);
    const settled = Math.floor((weights[0] + weights[1]) * 100);
    const failed = draftProgressPercent(at(2, 300, { failed: true }));

    // 멈춘 막대가 계속 차오르면 아직 도는 것처럼 보인다.
    expect(failed).toBe(settled);
    expect(failed).toBeLessThan(draftProgressPercent(at(2, 300)));
  });

  it("is zero before the first step is reported", () => {
    expect(draftProgressPercent(at(-1, 0))).toBe(0);
    expect(draftProgressPercent({ steps: [], stepIndex: -1, elapsedInStepMs: 0 })).toBe(0);
  });

  it("never goes backwards as time passes inside a step", () => {
    let previous = -1;
    for (let seconds = 0; seconds <= 600; seconds += 5) {
      const percent = draftProgressPercent(at(1, seconds));
      expect(percent).toBeGreaterThanOrEqual(previous);
      previous = percent;
    }
  });

  it("keeps rising across a step boundary", () => {
    // 예상보다 빨리 끝난 단계에서 다음 단계로 넘어가도 막대가 뒤로 가면 안 된다.
    const endOfStep = draftProgressPercent(at(1, DRAFT_STEP_SECONDS[1]));
    const startOfNext = draftProgressPercent(at(2, 0));

    expect(startOfNext).toBeGreaterThanOrEqual(endOfStep);
  });
});

describe("stepPercent", () => {
  it("fills finished steps, empties future ones, and grows the current one", () => {
    const input = at(1, 20);

    expect(stepPercent(0, input)).toBe(100);
    expect(stepPercent(1, input)).toBeGreaterThan(0);
    expect(stepPercent(1, input)).toBeLessThan(100);
    expect(stepPercent(2, input)).toBe(0);
  });

  it("fills every step once the draft is done", () => {
    const input = at(3, 1, { done: true });

    expect(STEPS.map((_, index) => stepPercent(index, input))).toEqual([100, 100, 100, 100]);
  });
});

describe("elapsedSince", () => {
  const now = Date.parse("2026-07-28T00:05:00Z");

  it("measures forward from the reported start", () => {
    expect(elapsedSince("2026-07-28T00:04:00Z", now)).toBe(60_000);
  });

  it("refuses values a skewed clock would produce", () => {
    // 서버가 앞서 있으면 음수가, 크게 어긋나면 몇 시간이 나온다 — 둘 다 보여줄 수 없다.
    expect(elapsedSince("2026-07-28T00:06:00Z", now)).toBe(0);
    expect(elapsedSince("2020-01-01T00:00:00Z", now)).toBe(0);
    expect(elapsedSince("not a date", now)).toBe(0);
    expect(elapsedSince(undefined, now)).toBe(0);
  });
});

describe("formatElapsed", () => {
  it("reads as 분·초", () => {
    expect(formatElapsed(0)).toBe("0초");
    expect(formatElapsed(45_000)).toBe("45초");
    expect(formatElapsed(80_000)).toBe("1분 20초");
    expect(formatElapsed(600_000)).toBe("10분 0초");
  });
});
