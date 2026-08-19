import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BlogTask } from "../../api/types";

/**
 * 발행 화면은 디자인만 다시 그렸다. 복사 두 종류·이미지 다운로드·자동 발행이 각자
 * 예전 그대로 동작하는지, 그리고 이 화면에 계정 자격 정보가 새어 나오지 않는지 확인한다.
 */
const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  copyRichHtml: vi.fn(),
  downloadPostImages: vi.fn(() => 2),
  articleHtmlForClipboard: vi.fn(() => "<p>html</p>"),
  articleMarkdownForClipboard: vi.fn(() => "markdown"),
  store: {
    task: null as BlogTask | null,
    setTask: vi.fn(),
    setRoute: vi.fn(),
    showToast: vi.fn(),
    reportError: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ request: mocks.request }));
vi.mock("../../store", () => ({ useStore: () => mocks.store }));
// 발행 중에 뜨는 라이브 뷰 패널은 자체 테스트(LiveSessionsPanel.test.tsx)가 있다.
// 여기서 실물을 그리면 패널의 /live/sessions 폴링이 발행 요청 뒤에 끼어들어
// '마지막 요청' 단정이 흔들린다 — 이 화면의 계약(발행 요청 모양)만 본다.
vi.mock("../LiveSessionsPanel", () => ({ LiveSessionsPanel: () => null }));
vi.mock("../../utils", () => ({
  copyRichHtml: mocks.copyRichHtml,
  downloadPostImages: mocks.downloadPostImages,
  articleHtmlForClipboard: mocks.articleHtmlForClipboard,
  articleMarkdownForClipboard: mocks.articleMarkdownForClipboard,
}));

import { StepPublish } from "./StepPublish";

function task(status: BlogTask["status"], images = 2): BlogTask {
  return {
    postId: "post_1",
    status,
    postingLogs: [],
    finalPost: {
      title: "완성 원고",
      images: Array.from({ length: images }, (_, i) => ({ dataUrl: `data:${i}` })),
    },
  } as unknown as BlogTask;
}

describe("StepPublish", () => {
  let container: HTMLDivElement;
  let root: Root;

  async function render() {
    await act(async () => {
      root.render(<StepPublish />);
    });
  }

  function buttonByText(text: string): HTMLButtonElement {
    const match = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes(text),
    );
    expect(match, `Missing button ${text}`).toBeTruthy();
    return match as HTMLButtonElement;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = task("READY_TO_PUBLISH");
    mocks.copyRichHtml.mockResolvedValue(undefined);
    mocks.downloadPostImages.mockReturnValue(2);
    // 첫 요청은 /naver/status.
    mocks.request.mockResolvedValue({ saved: true, blogId: "blog-it-marketing" });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("정상 연결이면 연결 배너를 그리지 않는다 — 문제가 있을 때만 말한다", async () => {
    await render();

    expect(mocks.request).toHaveBeenCalledWith("/naver/status");
    // 2026-08-07 사용자 결정: '연결 완료' 배너는 아무 행동도 요구하지 않으면서 화면을
    // 차지했다. 정상이면 칸을 통째로 그리지 않는다.
    expect(container.querySelector(".publish-connection")).toBeNull();
    expect(container.textContent).not.toContain("Naver 계정 연결 완료");
    // 입력창·비밀번호가 이 화면에 있으면 안 된다.
    expect(container.querySelector("input")).toBeNull();
    expect(container.textContent).not.toContain("비밀번호");
  });

  it("미연결이면 연결이 필요하다는 안내만 보여준다", async () => {
    mocks.request.mockResolvedValue({ saved: false, blogId: null });
    await render();

    const connection = container.querySelector(".publish-connection");
    expect(connection?.textContent).toContain("Naver 계정 연결 필요");
    // 색만으로 상태를 알리지 않는다.
    expect(connection?.textContent).toContain("미연결");
    expect(container.querySelector("input")).toBeNull();
  });

  it("HTML·Markdown 복사와 이미지 다운로드가 각각 살아 있다", async () => {
    await render();

    await act(async () => buttonByText("HTML(서식) 복사").click());
    expect(mocks.copyRichHtml).toHaveBeenLastCalledWith("<p>html</p>", "markdown");

    await act(async () => buttonByText("Markdown 복사").click());
    expect(mocks.copyRichHtml).toHaveBeenLastCalledWith("<p>html</p>", "markdown");

    await act(async () => container.querySelector<HTMLButtonElement>("#saveImages")?.click());
    expect(mocks.downloadPostImages).toHaveBeenCalledOnce();
    expect(buttonByText("이미지 다운로드").textContent).toContain("(2장)");
  });

  it("네이버 발행은 자동 발행 요청을 네이버 채널로 보낸다", async () => {
    await render();
    mocks.request.mockResolvedValue({ ...task("POSTED"), status: "POSTED" });

    await act(async () => buttonByText("Naver 발행").click());

    expect(mocks.request).toHaveBeenLastCalledWith("/posts/post_1/publish", {
      method: "POST",
      body: { method: "auto", channel: "naver" },
    });
    expect(mocks.store.setTask).toHaveBeenCalledOnce();
    expect(mocks.store.showToast).toHaveBeenCalledWith("Naver에 발행했습니다.");
  });

  it("스레드 발행은 같은 발행 요청을 threads 채널로 보낸다", async () => {
    await render();
    mocks.request.mockResolvedValue({ ...task("POSTED"), status: "POSTED" });

    await act(async () => buttonByText("Threads 발행").click());

    expect(mocks.request).toHaveBeenLastCalledWith("/posts/post_1/publish", {
      method: "POST",
      body: { method: "auto", channel: "threads" },
    });
    expect(mocks.store.showToast).toHaveBeenCalledWith(
      "Threads에 발행했습니다. 발행된 글을 브라우저로 엽니다.",
    );
  });

  it("스레드 연결이 안 됐으면 발행 로그의 사유를 그대로 보여준다", async () => {
    await render();
    const failed = task("POSTING_NEEDS_HUMAN");
    failed.postingLogs = [
      {
        method: "auto",
        channel: "threads",
        result: "needs_human",
        errorMessage: "스레드 연결 정보가 없습니다.",
      } as never,
    ];
    mocks.request.mockResolvedValue(failed);

    await act(async () => buttonByText("Threads 발행").click());

    expect(mocks.store.showToast).toHaveBeenCalledWith("스레드 연결 정보가 없습니다.", true);
  });

  it("네이버가 연결되지 않으면 네이버 버튼만 막고 스레드 발행은 살려 둔다", async () => {
    mocks.request.mockResolvedValue({ saved: false, blogId: null });
    await render();

    expect(container.querySelector(".publish-connection")?.className).not.toContain(
      "is-connected",
    );
    expect(container.textContent).toContain("Naver 계정 연결 필요");
    expect(buttonByText("Naver 발행").disabled).toBe(true);
    expect(buttonByText("Naver에 임시저장").disabled).toBe(true);
    // 스레드는 서버의 API 토큰으로 가므로 네이버 세션과 무관하다. 복사도 마찬가지.
    expect(buttonByText("Threads 발행").disabled).toBe(false);
    expect(buttonByText("HTML(서식) 복사").disabled).toBe(false);

    await act(async () => buttonByText("연결 설정").click());
    expect(mocks.store.setRoute).toHaveBeenCalledWith("settings");
  });

  it("스레드에 발행된 글이라도 네이버 발행은 막히지 않는다", async () => {
    const posted = task("POSTED");
    posted.postingLogs = [
      { method: "auto", channel: "threads", result: "success" } as never,
    ];
    mocks.store.task = posted;
    await render();

    expect(buttonByText("Naver 발행").disabled).toBe(false);
    // 같은 채널로는 중복 발행을 막는다.
    expect(buttonByText("Threads 발행").disabled).toBe(true);
  });
});
