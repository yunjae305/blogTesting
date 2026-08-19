import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { SchedulePicker } from "./SchedulePicker";

/**
 * 작업 시각 고르개(2026-08-12 사용자 신고: "분까지 입력해도 다른 곳을 클릭해야 적용이 돼").
 *
 * 브라우저 기본 선택창을 우리 것으로 바꾼 이유가 여기 다 들어 있다 — **누르는 즉시
 * 적용되는가**, **분에서 닫히는가**, **지난 시각을 막는가**. 셋 중 하나라도 무너지면
 * 예전 증상으로 돌아간다.
 */

/** 값을 실제로 들고 있는 껍데기. 화면이 controlled라 이것이 있어야 눌린 값이 되비친다. */
function Harness({
  min,
  initial = "",
  clearLabel,
}: {
  min: string;
  initial?: string;
  clearLabel?: string;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SchedulePicker
      label="원고를 만들기 시작할 시각"
      value={value}
      min={min}
      clearLabel={clearLabel}
      onChange={setValue}
    />
  );
}

describe("작업 시각 고르개", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  function field(): HTMLInputElement {
    return container.querySelector("input")!;
  }

  function panel(): HTMLElement | null {
    return container.querySelector(".when-picker-panel");
  }

  /** aria-label로 찾는다 — 화면에 보이는 글자는 "01"이지만 읽히는 이름은 "1시"다. */
  function labelled(name: string): HTMLButtonElement {
    const found = [...container.querySelectorAll("button")].find(
      (element) => element.getAttribute("aria-label") === name,
    );
    if (!found) throw new Error(`'${name}' 단추를 찾지 못했다`);
    return found as HTMLButtonElement;
  }

  /** 오전·오후는 글자 그대로다(읽어 줄 다른 이름이 필요 없다). */
  function meridiem(text: "오전" | "오후"): HTMLButtonElement {
    const found = [...container.querySelectorAll<HTMLButtonElement>(".when-picker-options button")]
      .find((element) => element.textContent === text);
    if (!found) throw new Error(`'${text}' 단추를 찾지 못했다`);
    return found;
  }

  async function open(min: string, initial = "") {
    await act(async () => root.render(<Harness min={min} initial={initial} />));
    await act(async () => labelled("원고를 만들기 시작할 시각 선택창 열기").click());
  }

  it("열의 차례는 오전·오후 → 시 → 분이다", async () => {
    // 2026-08-12 사용자 지시. 왼쪽에서 오른쪽으로 눌러 가면 마지막이 분이 되고,
    // 분에서 창이 닫히는 규칙과 맞물린다.
    await open("2026-08-12T09:00");

    const titles = [...container.querySelectorAll(".when-picker-column-title")].map(
      (element) => element.textContent,
    );

    expect(titles).toEqual(["오전·오후", "시", "분"]);
  });

  it("분을 고르면 그 자리에서 값이 들어오고 창이 닫힌다", async () => {
    // 예전에는 여기서 밖을 한 번 더 눌러야 값이 적용됐다. 그 증상이 이 시험이다.
    await open("2026-08-12T09:00");

    await act(async () => labelled("30분").click());

    expect(field().value).toBe("2026-08-12T09:30");
    expect(panel()).toBeNull();
  });

  it("오전·오후와 시는 값만 바꾸고 창을 닫지 않는다", async () => {
    // 아직 분이 남아 있다. 여기서 닫으면 "닫혔다 = 다 골랐다"가 어긋난다.
    await open("2026-08-12T09:00");

    await act(async () => meridiem("오후").click());
    expect(field().value).toBe("2026-08-12T21:10");
    expect(panel()).not.toBeNull();

    await act(async () => labelled("3시").click());
    expect(field().value).toBe("2026-08-12T15:10");
    expect(panel()).not.toBeNull();
  });

  it("날짜도 누르는 즉시 들어오고 창은 열려 있다", async () => {
    await open("2026-08-12T09:00");

    await act(async () => labelled("2026년 8월 20일").click());

    expect(field().value).toBe("2026-08-20T09:10");
    expect(panel()).not.toBeNull();
  });

  it("아무것도 고르지 않았으면 위 칸은 비어 있다", async () => {
    // 창을 열기만 한 것으로 예약이 걸리면 안 된다 — 비워 두면 '지금 바로'다.
    await act(async () => root.render(<Harness min="2026-08-12T09:00" />));

    expect(field().value).toBe("");
  });

  it("지난 날과 지난 시간대는 눌리지 않는다", async () => {
    await open("2026-08-12T09:00");

    expect(labelled("2026년 8월 11일").disabled).toBe(true);
    expect(labelled("2026년 8월 13일").disabled).toBe(false);
    // 오전 1시는 59분까지 봐도 이미 지났다. 오전 9시는 9시 59분이 남아 있다.
    expect(labelled("1시").disabled).toBe(true);
    expect(labelled("9시").disabled).toBe(false);
  });

  it("오전이 통째로 지났으면 오전을 고를 수 없다", async () => {
    await open("2026-08-12T13:00");

    expect(meridiem("오전").disabled).toBe(true);
    expect(meridiem("오후").disabled).toBe(false);
  });

  it("고르다 지난 시각이 되면 가장 이른 시각 바로 뒤로 밀어 준다", async () => {
    // 지금이 9시 30분인데 분이 10분으로 남아 있다. 오전으로 바꾸면 2시 10분, 거기서
    // 9시를 고르면 9시 10분 — 둘 다 이미 지났다. 그렇다고 '오전'이나 '오전 9시'를
    // 막으면 왜 안 눌리는지 알 수 없다. 눌리게 두고 값을 민다.
    await open("2026-08-12T09:30", "2026-08-12T14:10");

    await act(async () => meridiem("오전").click());
    expect(field().value).toBe("2026-08-12T09:31");

    await act(async () => labelled("9시").click());
    expect(field().value).toBe("2026-08-12T09:31");
  });

  it("이미 고른 시각을 열면 그 자리가 강조된다", async () => {
    await open("2026-08-12T09:00", "2026-08-13T14:25");

    const picked = [...container.querySelectorAll(".when-picker-options button.is-picked")].map(
      (element) => element.textContent,
    );

    // 오후 · 2시 · 25분.
    expect(picked).toEqual(["오후", "02", "25"]);
    expect(labelled("2026년 8월 13일").className).toContain("is-picked");
  });

  it("비우기 단추는 넘긴 화면에만 붙는다", async () => {
    // 새 글 작성은 되돌리는 단추가 칸 옆에 따로 있어 넘기지 않는다.
    await open("2026-08-12T09:00");

    expect(container.querySelector(".when-picker-foot")).toBeNull();
  });

  it("비우기를 누르면 값이 지워지고 창이 닫힌다", async () => {
    // 자동 포스팅은 비워 두는 것이 '앞 글 뒤에 이어서'라는 뜻이라 되돌릴 길이 필요하다.
    await act(async () =>
      root.render(
        <Harness min="2026-08-12T09:00" initial="2026-08-13T14:25" clearLabel="비우기" />,
      ),
    );
    await act(async () => labelled("원고를 만들기 시작할 시각 선택창 열기").click());
    await act(async () =>
      container.querySelector<HTMLButtonElement>(".when-picker-foot button")!.click(),
    );

    expect(field().value).toBe("");
    expect(panel()).toBeNull();
  });

  it("밖을 누르면 닫힌다", async () => {
    await open("2026-08-12T09:00");

    await act(async () => {
      document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    });

    expect(panel()).toBeNull();
  });

  it("위 칸에 직접 친 시각도 선택창에 그대로 비친다", async () => {
    // 키보드로 치던 사람의 길을 막지 않는다.
    await act(async () => root.render(<Harness min="2026-08-12T09:00" />));

    const input = field();
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, "2026-08-15T16:45");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => labelled("원고를 만들기 시작할 시각 선택창 열기").click());

    const picked = [...container.querySelectorAll(".when-picker-options button.is-picked")].map(
      (element) => element.textContent,
    );

    expect(picked).toEqual(["오후", "04", "45"]);
  });
});
