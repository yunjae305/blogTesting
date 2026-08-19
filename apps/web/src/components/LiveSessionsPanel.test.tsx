import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 라이브 뷰 패널: 중계 중인 화면이 있을 때만 나타나고, 채널 필터가 지켜지는지.
 * 스트림 자체(openLiveStream)는 api/live.test.ts가 검증한다 — 여기서는 대역이다.
 */
const mocks = vi.hoisted(() => ({
  fetchLiveSessions: vi.fn(async () => [] as unknown[]),
  openLiveStream: vi.fn(() => () => {}),
  sendLiveInput: vi.fn(async () => {}),
}));

vi.mock("../api/live", () => ({
  fetchLiveSessions: mocks.fetchLiveSessions,
  openLiveStream: mocks.openLiveStream,
  sendLiveInput: mocks.sendLiveInput,
}));

import { LiveSessionsPanel } from "./LiveSessionsPanel";

describe("LiveSessionsPanel", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => {
      root.unmount();
    });
    container.remove();
  });

  async function render(channels?: string[]) {
    await act(async () => {
      root.render(<LiveSessionsPanel channels={channels} />);
    });
    // fetchLiveSessions 응답이 상태에 반영될 때까지 한 번 더 흘린다.
    await act(async () => {});
  }

  it("중계 중인 화면이 없으면 아무것도 그리지 않는다", async () => {
    await render();
    expect(container.querySelector(".live-sessions")).toBeNull();
  });

  it("활성 세션이 있으면 그 채널의 화면을 그린다", async () => {
    mocks.fetchLiveSessions.mockResolvedValue([
      { channel: "naver", label: "네이버 발행", active: true, startedAt: 0 },
    ]);
    await render();
    const view = container.querySelector(".live-browser");
    expect(view).not.toBeNull();
    expect(view?.getAttribute("data-channel")).toBe("naver");
    expect(container.textContent).toContain("네이버 발행");
  });

  it("채널 필터에 맞지 않는 세션은 그리지 않는다", async () => {
    mocks.fetchLiveSessions.mockResolvedValue([
      { channel: "naver", label: "네이버 발행", kind: "publish", active: true, startedAt: 0 },
      { channel: "threads", label: "스레드 로그인", kind: "login", active: true, startedAt: 0 },
    ]);
    await render(["threads"]);
    const views = [...container.querySelectorAll(".live-browser")];
    expect(views).toHaveLength(1);
    expect(views[0].getAttribute("data-channel")).toBe("threads");
  });

  it("종류(kind) 필터에 맞지 않는 세션은 그리지 않는다 — 로그인 중계가 발행 탭에 뜨면 안 된다", async () => {
    mocks.fetchLiveSessions.mockResolvedValue([
      { channel: "naver", label: "네이버 로그인", kind: "login", active: true, startedAt: 0 },
    ]);
    await act(async () => {
      root.render(<LiveSessionsPanel kinds={["publish"]} />);
    });
    await act(async () => {});
    expect(container.querySelector(".live-browser")).toBeNull();
  });

  it("죽은 세션은 그리지 않는다", async () => {
    mocks.fetchLiveSessions.mockResolvedValue([
      { channel: "naver", label: "네이버 발행", active: false, startedAt: 0 },
    ]);
    await render();
    expect(container.querySelector(".live-browser")).toBeNull();
  });
});
