import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { WriteHelperCard } from "./WriteHelperCard";

/**
 * 「새 글 작성」의 도우미(2026-08-12 사용자 요청: "처음 이용하는 사용자가 잘 이해할 수 있게").
 *
 * 카드의 생김새는 자동 포스팅과 같은 것을 쓴다. 여기서 지키는 것은 **담는 내용**이다 —
 * 두 화면은 하는 일이 다르므로 같은 글을 붙여 두면 오히려 잘못 안내한다.
 */
describe("새 글 작성 도우미", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(async () => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root.render(<WriteHelperCard />));
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

  it("먼저 다섯 걸음을 말한다", () => {
    // 처음 여는 사람이 막히는 곳은 "지금 뭘 채워야 다음으로 넘어가지"다. 흐름을 맨 위에서
    // 알려 주지 않으면 칸 설명만 읽고도 어디쯤인지 알 수 없다.
    const first = container.querySelector(".helper-note-row");

    expect(first?.querySelector(".helper-note-key")?.textContent).toBe("다섯 걸음");
    const flow = first?.querySelector(".helper-note-value")?.textContent ?? "";
    for (const step of ["소재", "제목", "검증", "원고", "발행"]) {
      expect(flow).toContain(step);
    }
  });

  it("이 화면에서 정해야 하는 것을 필수·선택으로 나눠 말한다", () => {
    const text = container.textContent ?? "";

    for (const key of ["소재", "글 목적", "대상 연령", "카테고리"]) {
      expect(text).toContain(key);
    }
    expect(text).toContain("꼭 필요해요");
    expect(text).toContain("선택이에요");
  });

  it("자동 포스팅의 안내를 그대로 옮겨 오지 않는다", () => {
    // 그 화면은 소재만 적으면 끝까지 알아서 돈다. 새 글 작성은 사람이 직접 고르므로
    // 같은 문장을 붙여 두면 잘못 안내하는 것이 된다.
    const text = container.textContent ?? "";

    expect(text).not.toContain("발행 플랫폼");
    expect(text).not.toContain("최대 20편");
  });

  it("설명은 칸마다 한 문장이다", () => {
    const values = [...container.querySelectorAll<HTMLElement>(".helper-note-value")];

    for (const value of values) {
      const text = value.textContent ?? "";
      expect(text.split(".").filter((part) => part.trim()).length).toBe(1);
    }
  });
});
