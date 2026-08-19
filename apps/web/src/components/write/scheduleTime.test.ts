import { describe, expect, it } from "vitest";

import { toLocalInputValue, toUtcIso } from "./StepTopic";

/**
 * 원고 작업 시각의 시간대 변환(2026-08-11).
 *
 * 사용자는 자기 시계로 시각을 고르고 서버는 UTC로만 저장한다. 이 변환이 한 곳에서만
 * 일어나야 날짜가 하루 밀리는 종류의 버그가 생기지 않는다 — 예약 포스팅이 이미 같은
 * 규칙을 쓴다(ScheduledJob.publish_at은 UTC, timezone은 표시용).
 */
describe("작업 시각 변환", () => {
  it("비어 있으면 예약이 없다는 뜻이다", () => {
    // 단일 글 작성이 그대로 유지되는 지점 — 값이 없으면 서버에 아예 보내지 않는다.
    expect(toUtcIso("")).toBeNull();
    expect(toUtcIso("   ")).toBeNull();
    expect(toLocalInputValue(undefined)).toBe("");
    expect(toLocalInputValue(null)).toBe("");
  });

  it("고른 시각을 UTC로 옮겨 보낸다", () => {
    const utc = toUtcIso("2026-08-13T15:00");

    expect(utc).not.toBeNull();
    // 로컬 15:00이 그대로 UTC 15:00이 되면 안 된다 — 실제 순간이 같아야 한다.
    expect(new Date(utc!).getTime()).toBe(new Date("2026-08-13T15:00").getTime());
  });

  it("저장된 UTC를 다시 열면 고를 때 본 시각 그대로다", () => {
    // 돌아왔을 때 시각이 몇 시간 밀려 보이면 사용자는 다시 골라야 한다.
    const utc = toUtcIso("2026-08-13T15:00")!;

    expect(toLocalInputValue(utc)).toBe("2026-08-13T15:00");
  });

  it("시각이 아닌 값은 예약으로 치지 않는다", () => {
    expect(toUtcIso("내일 3시")).toBeNull();
    expect(toLocalInputValue("어제")).toBe("");
  });
});
