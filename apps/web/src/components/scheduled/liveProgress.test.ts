import { describe, expect, it } from "vitest";

import type { ScheduledJob, TaskProgress } from "../../api/types";
import { runningStageFraction } from "./topics";

/**
 * 예약 큐 진행률이 원고 생성 중에도 움직이는가(2026-08-11 사용자 요청:
 * "예약 큐에서 확인 가능한 진행율 바 이것도 실시간으로 퍼센트 나눠").
 *
 * 원고 생성은 5~8분짜리 한 칸이라, 단계 몫만 쓰면 그 시간 내내 같은 숫자에 멈춘다.
 */
function job(overrides: Partial<ScheduledJob> = {}): ScheduledJob {
  return {
    jobId: "job_1",
    batchId: "batch_1",
    userId: "user_1",
    sequence: 0,
    topic: "소재",
    status: "RUNNING",
    stage: "DRAFT_GENERATION",
    ...overrides,
  } as ScheduledJob;
}

function progress(minutesInStep: number, step = 1): TaskProgress {
  const startedAt = new Date(Date.now() - minutesInStep * 60_000).toISOString();
  return {
    phase: "DRAFT",
    step,
    totalSteps: 4,
    label: "본문 원고 작성",
    steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
    startedAt,
    updatedAt: startedAt,
  } as TaskProgress;
}

describe("예약 큐 진행률", () => {
  it("원고 생성 중에는 머문 시간만큼 더 차오른다", () => {
    const jobs = [job()];
    const early = runningStageFraction(jobs, Date.now(), () => progress(0.1));
    const later = runningStageFraction(jobs, Date.now(), () => progress(3));

    expect(later).toBeGreaterThan(early);
  });

  it("원고 생성 칸을 넘어서지 않는다", () => {
    // 다음 단계(발행)의 몫까지 먹으면 아직 안 한 일을 했다고 말하는 셈이다.
    const value = runningStageFraction([job()], Date.now(), () => progress(60, 4));

    expect(value).toBeLessThanOrEqual(0.9);
    expect(value).toBeGreaterThanOrEqual(0.6);
  });

  it("진행 정보가 없으면 예전처럼 단계 몫만 쓴다", () => {
    // 옛 작업·아직 안 온 폴링에서 숫자가 0으로 떨어지면 막대가 뒤로 간다.
    expect(runningStageFraction([job()], Date.now())).toBe(0.6);
  });

  it("원고 생성이 아닌 단계는 예전 그대로다", () => {
    const value = runningStageFraction([job({ stage: "NAVER_PUBLISH" })], Date.now(), () =>
      progress(3),
    );

    expect(value).toBe(0.9);
  });
});

/**
 * **올릴 곳을 고르지 않은 작업**의 막대(2026-08-13 사용자 지적).
 *
 *     "플랫폼 선택안했을때는 원고만 생성 다 하면 작업이 완료가 되는 거잖아.
 *      그러면 진행바도 100퍼가 되어야겠지."
 *
 * 그런 작업은 원고를 만들면 그 자리에서 끝난다(서버의 _finish_without_publishing).
 * 발행 몫(0.9→1)을 남겨 두면 다 한 일이 90%에서 멈춘 것처럼 보인다.
 */
describe("올릴 곳이 없는 작업의 진행률", () => {
  const noPlatform = { publishNaver: false, publishThreads: false };

  it("원고를 다 만들면 100% 가까이 찬다", () => {
    const value = runningStageFraction([job(noPlatform)], Date.now(), () =>
      progress(60, 4),
    );

    // 예전 천장(발행 몫을 남긴 0.9)을 넘는다. 딱 1이 되지는 않는다 — 안쪽 계산기가
    // '완료' 신호 전까지 마지막 몇 %를 남겨 둔다(draftProgressPercent).
    expect(value).toBeGreaterThan(0.95);
    expect(value).toBeLessThanOrEqual(1);
  });

  it("올릴 곳이 있는 작업은 예전처럼 발행 몫을 남긴다", () => {
    const value = runningStageFraction([job({ publishNaver: true })], Date.now(), () =>
      progress(60, 4),
    );

    expect(value).toBeLessThanOrEqual(0.9);
  });

  it("같은 시점이면 올릴 곳이 없는 쪽이 더 차 있다", () => {
    const at = Date.now();
    const without = runningStageFraction([job(noPlatform)], at, () => progress(2));
    const with_ = runningStageFraction([job()], at, () => progress(2));

    expect(without).toBeGreaterThan(with_);
  });
});
