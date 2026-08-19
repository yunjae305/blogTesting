import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 첫 방문 안내 팝업(2026-08-12 사용자 지시).
 *
 *     "나중에 할게요 버튼 없애."
 *     "설정에 보면 내용을 선택 및 입력을 할 수가 있어. 그거 반영해서 팝업창 ui 수정해"
 *
 * 예전 팝업은 설정에 **없는** '포스팅 방식'을 안내하고, 정작 계정 두 칸은 빠뜨렸다.
 * 안내가 안내할 화면과 다른 말을 하면 안 된다 — 여기서 두 화면을 묶어 둔다.
 */
const mocks = vi.hoisted(() => ({
  store: {
    onboardingOpen: true,
    dismissOnboarding: vi.fn(),
    setRoute: vi.fn(),
  },
}));

vi.mock("../store", () => ({ useStore: () => mocks.store }));

import { OnboardingModal } from "./OnboardingModal";

describe("첫 방문 설정 안내", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.onboardingOpen = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function render() {
    await act(async () => root.render(<OnboardingModal />));
  }

  function buttonTexts(): string[] {
    return [...container.querySelectorAll(".verify-dialog-actions button")].map(
      (button) => button.textContent?.trim() ?? "",
    );
  }

  it("'나중에 할게요'를 두지 않는다", async () => {
    await render();

    expect(buttonTexts()).toEqual(["설정하러 가기"]);
  });

  it("설정 화면에 실제로 있는 다섯 칸을 그대로 안내한다", async () => {
    await render();

    const items = [...container.querySelectorAll(".onboarding-benefits li strong")].map(
      (item) => item.textContent?.trim(),
    );
    expect(items).toEqual([
      "글 생성 기본값",
      "기본 페르소나",
      "Naver 계정",
      "Threads 계정",
      "커스텀 페르소나",
    ]);
  });

  it("설정에 없는 항목을 지어내지 않는다", async () => {
    await render();

    // '포스팅 방식'은 설정 화면에 없는 칸이다. 계정 두 칸이 그 자리를 대신한다.
    expect(container.textContent).not.toContain("포스팅 방식");
  });

  it("번호는 설정 화면의 번호와 같다", async () => {
    await render();

    const numbers = [...container.querySelectorAll(".onboarding-benefits li > span")].map(
      (item) => item.textContent?.trim(),
    );
    expect(numbers).toEqual(["01", "02", "03", "04", "05"]);
  });

  it("계정 정보가 어디에 저장되는지 먼저 말해 준다", async () => {
    await render();

    expect(container.querySelector(".onboarding-note")?.textContent).toContain(
      "이 PC에만 암호화해 보관합니다",
    );
  });

  it("'설정하러 가기'는 설정 화면을 연다", async () => {
    await render();

    await act(async () => {
      container.querySelector<HTMLButtonElement>(".button.primary")!.click();
    });

    expect(mocks.store.setRoute).toHaveBeenCalledWith("settings");
    expect(mocks.store.dismissOnboarding).toHaveBeenCalled();
  });

  it("바깥을 누르면 닫힌다 — 닫을 길 자체를 없애지는 않았다", async () => {
    await render();

    await act(async () => {
      const overlay = container.querySelector<HTMLElement>(".verify-overlay")!;
      overlay.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(mocks.store.dismissOnboarding).toHaveBeenCalled();
  });
});
