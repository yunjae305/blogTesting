import { describe, expect, it } from "vitest";

import {
  MIN_PUBLISH_GAP_MINUTES,
  formatPublishAt,
  isPast,
  isoToLocalInput,
  localInputAfterMinutes,
  localInputToIso,
  toLocalInputValue,
  tooCloseIndex,
} from "./schedule";

/**
 * 예약 시각의 화면 값 ↔ 저장 값 변환.
 *
 * 이 파일이 시간대 변환이 일어나는 **유일한 자리**다(서버는 변환하지 않는다). 그래서
 * 여기서 확인하는 것도 하나다: 사용자가 자기 시계로 고른 그 순간이, 저장했다 되불러도
 * 같은 순간인가. 날짜가 하루 밀리는 종류의 버그는 전부 이 경계에서 생긴다.
 */
describe("예약 시각 변환", () => {
  it("입력칸 값을 절대 시각으로 옮기고 되돌려도 같은 값이다", () => {
    const value = "2026-08-06T15:00";

    const iso = localInputToIso(value);

    expect(iso).not.toBeNull();
    // 절대 시각은 UTC로 저장된다(끝이 Z).
    expect(iso!.endsWith("Z")).toBe(true);
    // 되돌리면 사용자가 고른 그 로컬 시각 그대로다 — 날짜도 시각도 밀리지 않는다.
    expect(isoToLocalInput(iso!)).toBe(value);
  });

  it("자정 직전·직후에도 날짜가 밀리지 않는다", () => {
    // 시간대 변환에서 날짜가 하루 어긋나는 것은 대개 이 두 지점에서 드러난다.
    for (const value of ["2026-08-06T00:00", "2026-08-06T23:59"]) {
      expect(isoToLocalInput(localInputToIso(value)!)).toBe(value);
    }
  });

  it("입력칸이 비었거나 읽을 수 없으면 null이다", () => {
    // 반쯤 입력된 값을 임의로 채워 보내면 사용자가 고르지 않은 시각에 글이 올라간다.
    expect(localInputToIso("")).toBeNull();
    expect(localInputToIso("어제쯤")).toBeNull();
  });

  it("저장된 시각이 없으면 입력칸은 빈 값이다", () => {
    expect(isoToLocalInput(undefined)).toBe("");
    expect(isoToLocalInput("")).toBe("");
    expect(isoToLocalInput("nope")).toBe("");
  });

  it("지금부터 몇 분 뒤를 초 단위 없이 채운다", () => {
    const now = new Date("2026-08-06T15:00:40");

    // 초를 버리므로 15:30:00이다. 초가 남아 있으면 '지금보다 뒤'가 흔들린다.
    expect(localInputAfterMinutes(30, now)).toBe("2026-08-06T15:30");
  });

  it("기본값이 날짜 경계를 넘어가면 다음 날로 넘어간다", () => {
    // 소재를 여러 개 넣으면 두 번째 줄부터 한 시간씩 뒤로 채운다. 자정을 넘길 때
    // 날짜가 따라 넘어가는지가 이 함수에서 유일하게 틀릴 수 있는 지점이다.
    const now = new Date("2026-08-06T23:10");

    expect(localInputAfterMinutes(60, now)).toBe("2026-08-07T00:10");
  });

  it("지난 시각을 알아본다", () => {
    const now = new Date("2026-08-06T15:00");

    expect(isPast("2026-08-06T14:59", now)).toBe(true);
    expect(isPast("2026-08-06T15:01", now)).toBe(false);
    // 아직 고르지 않은 칸은 '지났다'가 아니다 — 그건 다른 안내가 맡는다.
    expect(isPast("", now)).toBe(false);
  });

  it("예약 시각을 사람이 읽는 로컬 시간으로 적는다", () => {
    const iso = localInputToIso("2026-08-06T15:05")!;

    expect(formatPublishAt(iso)).toBe("8월 6일(목) 오후 3:05");
  });

  it("정오와 자정은 12시로 적는다", () => {
    expect(formatPublishAt(localInputToIso("2026-08-06T12:00")!)).toContain("오후 12:00");
    expect(formatPublishAt(localInputToIso("2026-08-06T00:00")!)).toContain("오전 12:00");
  });

  it("시각이 없으면 없다고 적는다 — 지어내지 않는다", () => {
    expect(formatPublishAt(undefined)).toBe("시각 미정");
    expect(formatPublishAt("nope")).toBe("시각 미정");
  });

  it("작업 시각이 서로 12분도 안 떨어져 있으면 그 칸을 짚는다", () => {
    // 원고 작업과 발행은 한 번에 하나씩 돈다 — 촘촘하면 뒤 글이 반드시 자기 시각을
    // 넘긴다. 12분은 2026-08-11 사용자 결정이다(그전 10분).
    expect(MIN_PUBLISH_GAP_MINUTES).toBe(12);
    expect(tooCloseIndex(["2026-08-06T15:00", "2026-08-06T15:11"])).toBe(1);
    expect(tooCloseIndex(["2026-08-06T15:00", "2026-08-06T15:12"])).toBe(-1);
    // 같은 시각도 어긴 것이다.
    expect(tooCloseIndex(["2026-08-06T15:00", "2026-08-06T15:00"])).toBe(1);
  });

  it("간격은 입력 순서가 아니라 시각 순으로 재고, 뒤에 오는 칸을 짚는다", () => {
    // 거꾸로 입력해도 실제로 올라가는 순서는 이른 것부터다.
    expect(tooCloseIndex(["2026-08-06T15:05", "2026-08-06T15:00"])).toBe(0);
    expect(tooCloseIndex(["2026-08-06T17:00", "2026-08-06T15:00"])).toBe(-1);
  });

  it("아직 고르지 않은 칸은 간격 검사에서 건너뛴다", () => {
    // 빈 칸은 다른 안내가 맡는다. 여기서 붙잡으면 두 가지 이유가 겹쳐 보인다.
    expect(tooCloseIndex(["", "2026-08-06T15:00", ""])).toBe(-1);
    expect(tooCloseIndex(["nope", "2026-08-06T15:00", "2026-08-06T15:02"])).toBe(2);
  });

  it("Date를 입력칸 값으로 옮긴다(한 자리 수는 0을 채운다)", () => {
    expect(toLocalInputValue(new Date("2026-01-02T03:04"))).toBe("2026-01-02T03:04");
  });
});
