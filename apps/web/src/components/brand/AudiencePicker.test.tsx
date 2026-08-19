import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 주요 고객 계층형 선택.
 *
 * 자유 입력(textarea)을 걷어낸 이유는 둘이다. 사람마다 "중소기업"·"중기"·"SMB"로 달리
 * 적어 프롬프트가 들쭉날쭉해졌고, 무엇을 적어야 할지 몰라 비워 두는 칸이 됐다.
 */
const mocks = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../../api/client", () => ({ request: mocks.request }));

import { AudiencePicker } from "./AudiencePicker";
import type { BrandAudience } from "./types";

const OPTIONS = {
  otherLabel: "기타",
  categories: [
    { category: "기업·사업자", types: ["중소기업", "스타트업", "기타"] },
    { category: "교육기관", types: ["대학·대학원", "기타"] },
  ],
};

describe("주요 고객 선택", () => {
  let container: HTMLDivElement;
  let root: Root;
  let value: BrandAudience[];

  async function render(next: BrandAudience[] = value) {
    value = next;
    await act(async () => {
      root.render(<AudiencePicker value={value} onChange={(v) => (value = v)} />);
    });
  }

  /** 선택은 부모가 들고 있다. 누른 뒤 그 값으로 다시 그려야 화면이 실제와 맞는다. */
  async function click(label: string) {
    const button = [...container.querySelectorAll("button")].find(
      (node) => node.textContent === label,
    ) as HTMLButtonElement;
    await act(async () => button.click());
    await render(value);
  }

  const labels = () => [...container.querySelectorAll("button")].map((n) => n.textContent);

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.request.mockResolvedValue(OPTIONS);
    value = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("처음에는 대분류만 보여준다", async () => {
    await render();

    expect(labels()).toEqual(["기업·사업자", "교육기관"]);
    // 유형을 처음부터 다 펼치면 화면이 길어지고 무엇을 고른 상태인지 알기 어렵다.
    expect(container.textContent).not.toContain("중소기업");
  });

  it("대분류를 고르면 그 아래 유형이 펼쳐진다", async () => {
    await render();

    await click("기업·사업자");

    expect(container.textContent).toContain("중소기업");
    // 고르지 않은 대분류의 유형은 나오지 않는다.
    expect(container.textContent).not.toContain("대학·대학원");
  });

  it("대분류와 유형 모두 여러 개 고를 수 있다", async () => {
    await render();

    await click("기업·사업자");
    await click("중소기업");
    await click("스타트업");
    await click("교육기관");
    await click("대학·대학원");

    expect(value).toEqual([
      { category: "기업·사업자", types: ["중소기업", "스타트업"], other: undefined },
      { category: "교육기관", types: ["대학·대학원"], other: undefined },
    ]);
  });

  it("'기타'를 골랐을 때만 직접 입력 칸이 나온다", async () => {
    await render();
    await click("기업·사업자");

    expect(container.querySelector(".audience-other")).toBeNull();

    await click("기타");

    expect(container.querySelector(".audience-other")).not.toBeNull();
  });

  it("'기타'를 끄면 거기 적은 글자도 함께 버린다", async () => {
    // 남겨 두면 화면에 보이지 않는 값이 저장돼, 사용자가 모르는 문장이 프롬프트에 실린다.
    await render([{ category: "기업·사업자", types: ["기타"], other: "협동조합" }]);

    await click("기타");

    expect(value).toEqual([{ category: "기업·사업자", types: [], other: undefined }]);
  });

  it("대분류를 끄면 그 갈래가 통째로 빠진다", async () => {
    await render([{ category: "기업·사업자", types: ["중소기업"] }]);

    await click("기업·사업자");

    expect(value).toEqual([]);
  });

  it("선택지는 화면이 아니라 서버에서 받는다", async () => {
    // 화면이 목록을 따로 들고 있으면 서버 검증과 어긋나, 고를 수는 있는데 저장은
    // 거부되는 값이 생긴다.
    await render();

    expect(mocks.request).toHaveBeenCalledWith("/brands/audience-options");
  });
});
