import { describe, expect, it } from "vitest";

import type { ScheduledJob } from "../../api/types";
import { draftLabels, withDraftLabel } from "./draftLabels";

/**
 * 2026-08-12 사용자 요청 — "이렇게 소재가 같은 경우에는 첫번째 원고인지 두번째 원고인지
 * 사용자가 파악할 수 있게도 해야지."
 *
 * 소재 하나로 여러 편을 걸면 작업 큐에 같은 이름의 줄이 나란히 서고, 작업 현황에는 글자
 * 하나 다르지 않은 줄이 두 번씩 찍힌다.
 */
function job(overrides: Partial<ScheduledJob>): ScheduledJob {
  return {
    jobId: "job_1",
    batchId: "batch_1",
    userId: "user_1",
    sequence: 0,
    topic: "롯데리아",
    status: "WAITING",
    stage: "CREATE_POST",
    createdAt: "2026-08-12T04:00:00.000Z",
    updatedAt: "2026-08-12T04:00:00.000Z",
    ...overrides,
  } as ScheduledJob;
}

describe("같은 소재의 편 번호", () => {
  it("한 편뿐인 소재에는 붙이지 않는다", () => {
    // 모든 줄에 '1편째'가 붙으면 구분이 되지 않는다.
    expect(draftLabels([job({ jobId: "a" }), job({ jobId: "b", topic: "버거킹" })])).toEqual({});
  });

  it("같은 소재가 둘 이상이면 큐 순서대로 번호를 매긴다", () => {
    const labels = draftLabels([
      job({ jobId: "second", sequence: 1 }),
      job({ jobId: "first", sequence: 0 }),
    ]);

    expect(labels).toEqual({ first: "1편째", second: "2편째" });
  });

  it("소재가 섞여 있어도 소재별로 센다", () => {
    const labels = draftLabels([
      job({ jobId: "a", sequence: 0 }),
      job({ jobId: "b", sequence: 1, topic: "버거킹" }),
      job({ jobId: "c", sequence: 2 }),
    ]);

    expect(labels).toEqual({ a: "1편째", c: "2편째" });
  });

  it("붙일 것이 없으면 문장을 그대로 둔다", () => {
    expect(withDraftLabel("원고를 만듭니다.", undefined)).toBe("원고를 만듭니다.");
    expect(withDraftLabel("원고를 만듭니다.", "2편째")).toBe("2편째 · 원고를 만듭니다.");
  });
});

/**
 * **한 번에 건 묶음 안에서 센다**(2026-08-13 사용자 지적).
 *
 *     "지금 한번에 예약한 편끼리 1편2편3편으로 편수가 늘어나는게 아니라 지금까지 했던
 *      작업들 전부 합쳐셔 편수로 해뒀네. 한번에 예약할때 잡히는 것들을 한덩어리로 보고
 *      거기서 1편2편3편으로 표기해야지"
 *
 * 새 글 작성에서 건 예약은 돌고 있는 배치에 계속 붙기 때문에, 소재로도 배치로도 묶이지
 * 않는다. 서버가 등록마다 발급하는 seriesId가 그 묶음이다.
 */
describe("묶음 단위 편 번호", () => {
  it("같은 소재라도 다른 묶음이면 따로 센다", () => {
    const labels = draftLabels([
      job({ jobId: "a1", sequence: 0, seriesId: "s1" }),
      job({ jobId: "a2", sequence: 1, seriesId: "s1" }),
      job({ jobId: "b1", sequence: 2, seriesId: "s2" }),
      job({ jobId: "b2", sequence: 3, seriesId: "s2" }),
    ]);

    expect(labels).toEqual({
      a1: "1편째",
      a2: "2편째",
      b1: "1편째",
      b2: "2편째",
    });
  });

  it("묶음에 한 편뿐이면 붙이지 않는다", () => {
    // 같은 소재가 목록에 셋이지만 서로 다른 등록이면 각자 1편뿐이다.
    const labels = draftLabels([
      job({ jobId: "a", sequence: 0, seriesId: "s1" }),
      job({ jobId: "b", sequence: 1, seriesId: "s2" }),
      job({ jobId: "c", sequence: 2, seriesId: "s3" }),
    ]);

    expect(labels).toEqual({});
  });

  it("옛 작업(묶음 id 없음)은 예전처럼 소재로 묶는다", () => {
    const labels = draftLabels([
      job({ jobId: "a", sequence: 0 }),
      job({ jobId: "b", sequence: 1 }),
    ]);

    expect(labels).toEqual({ a: "1편째", b: "2편째" });
  });

  it("묶음이 있는 작업과 없는 작업이 섞여도 서로 새지 않는다", () => {
    const labels = draftLabels([
      job({ jobId: "old1", sequence: 0 }),
      job({ jobId: "old2", sequence: 1 }),
      job({ jobId: "new1", sequence: 2, seriesId: "s1" }),
      job({ jobId: "new2", sequence: 3, seriesId: "s1" }),
    ]);

    expect(labels).toEqual({
      old1: "1편째",
      old2: "2편째",
      new1: "1편째",
      new2: "2편째",
    });
  });
});
