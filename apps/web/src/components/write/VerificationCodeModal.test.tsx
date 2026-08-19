import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 2단계 인증 코드 입력창. 발행 자동화가 코드를 기다리며 멈춰 있을 때 뜬다.
 *
 * 여기서 막는 것: 코드를 서버로 안 보내는 것, 취소했는데 자동화를 계속 기다리게 두는 것,
 * 그리고 화면이 받은 코드를 어딘가에 흘리는 것.
 */
const mocks = vi.hoisted(() => ({ request: vi.fn() }));

vi.mock("../../api/client", () => ({ request: mocks.request }));

import { VerificationCodeModal, type PendingVerification } from "./VerificationCodeModal";

function pending(overrides: Partial<PendingVerification> = {}): PendingVerification {
  return {
    postId: "post_1",
    channel: "threads",
    prompt: "스레드(인스타그램) 2단계 인증 코드를 입력해 주세요.",
    attempt: 1,
    maxAttempts: 3,
    waitingSeconds: 4.2,
    ...overrides,
  };
}

describe("VerificationCodeModal", () => {
  let container: HTMLDivElement;
  let root: Root;
  const onDone = vi.fn();

  beforeEach(() => {
    mocks.request.mockReset();
    mocks.request.mockResolvedValue({ accepted: true });
    onDone.mockReset();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function render(value: PendingVerification = pending()) {
    act(() => {
      root.render(<VerificationCodeModal pending={value} onDone={onDone} />);
    });
  }

  function input() {
    return container.querySelector<HTMLInputElement>("#verificationCode")!;
  }

  function buttonByText(text: string) {
    return [...container.querySelectorAll("button")].find((b) => b.textContent?.includes(text))!;
  }

  function type(value: string) {
    const field = input();
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  it("서버가 준 안내 문구를 그대로 보여준다", () => {
    render();
    // 어느 채널이 왜 막혔는지 모르면 무엇을 찾아 넣을지 알 수 없다.
    expect(container.textContent).toContain("Threads 2단계 인증");
    expect(container.textContent).toContain("2단계 인증 코드를 입력해 주세요");
  });

  it("코드를 넣고 완료를 누르면 서버로 넘기고 창을 닫는다", async () => {
    render();
    type("123456");
    await act(async () => buttonByText("완료").click());

    expect(mocks.request).toHaveBeenCalledWith("/posting/verification", {
      method: "POST",
      body: { code: "123456" },
    });
    expect(onDone).toHaveBeenCalled();
  });

  it("코드가 비어 있으면 완료를 누를 수 없다", () => {
    render();
    // 빈 값을 보내면 서버가 400을 주고 사용자는 이유를 모른 채 실패를 본다.
    expect(buttonByText("완료").disabled).toBe(true);
  });

  it("취소하면 서버의 대기를 끝내고 창을 닫는다", async () => {
    render();
    await act(async () => buttonByText("취소").click());

    // 취소를 알리지 않으면 자동화가 시간이 다 갈 때까지 브라우저를 붙들고 있다.
    expect(mocks.request).toHaveBeenCalledWith("/posting/verification", { method: "DELETE" });
    expect(onDone).toHaveBeenCalled();
  });

  it("서버가 거절하면 창을 닫지 않고 이유를 보여준다", async () => {
    mocks.request.mockRejectedValue(new Error("지금 입력을 기다리는 인증 요청이 없습니다."));
    render();
    type("999999");
    await act(async () => buttonByText("완료").click());

    expect(onDone).not.toHaveBeenCalled();
    expect(container.textContent).toContain("기다리는 인증 요청이 없습니다");
  });

  it("다시 물어보면 입력칸을 비우고 몇 번째인지 알려준다", () => {
    render();
    type("111111");
    expect(input().value).toBe("111111");

    // 코드가 틀려 자동화가 다시 물어본 상황.
    render(pending({ attempt: 2, prompt: "코드가 맞지 않습니다. 새로 받은 코드를 다시 입력해 주세요." }));
    expect(input().value).toBe("");
    expect(container.textContent).toContain("2/3번째 시도");
  });
});
