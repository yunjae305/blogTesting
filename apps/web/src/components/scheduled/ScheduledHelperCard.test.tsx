import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ScheduledHelperCard } from "./ScheduledHelperCard";

/**
 * 「작업 관리」의 도우미(2026-08-12 사용자 요청).
 *
 * 카드의 생김새는 다른 두 화면과 같은 것을 쓴다. 여기서 지키는 것은 **담는 내용**이다 —
 * 이 화면은 아무것도 만들지 않으므로, 만드는 화면의 안내를 옮겨 오면 잘못 안내하게 된다.
 */
describe("작업 관리 도우미", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(async () => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<ScheduledHelperCard />));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("칸마다 그림 하나에 이름과 설명이 붙는다", () => {
    const rows = [...container.querySelectorAll(".helper-note-row")];

    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row.querySelector(".helper-note-icon svg")).not.toBeNull();
      expect(row.querySelector(".helper-note-key")?.textContent).toBeTruthy();
      expect(row.querySelector(".helper-note-value")?.textContent).toBeTruthy();
    }
  });

  it("두 탭이 무엇을 담는지 가른다", () => {
    // 처음 온 사람이 가장 먼저 막히는 곳이다 — 큐에 없는 작업을 잃어버린 줄 안다.
    const text = container.textContent ?? "";

    expect(text).toContain("작업 큐");
    expect(text).toContain("남은 일");
    expect(text).toContain("발행 내역");
    expect(text).toContain("끝난 일");
  });

  it("예약을 거는 곳은 여기가 아니라고 말한다", () => {
    // 2026-08-11에 이 화면에서 '새 예약 만들기' 두 걸음을 없앴다. 그 사실을 말해 두지
    // 않으면 처음 온 사람은 여기서 걸 방법을 찾다 만다.
    const text = container.textContent ?? "";

    expect(text).toContain("새 글 작성");
    expect(text).toContain("자동 포스팅");
  });

  it("설명은 칸마다 한 문장이다", () => {
    const values = [...container.querySelectorAll<HTMLElement>(".helper-note-value")];

    expect(values.length).toBeGreaterThan(0);
    for (const value of values) {
      const text = value.textContent ?? "";
      expect(text.split(".").filter((part) => part.trim()).length).toBe(1);
    }
  });
});
