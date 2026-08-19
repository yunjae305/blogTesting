import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BlogTask, BlogTaskListItem } from "../api/types";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  writeText: vi.fn(async () => undefined),
  store: {
    session: { user: { userId: "user_1" } },
    openPost: vi.fn(async () => undefined),
    /** 예약 포스팅이 만든 글의 postId. 기본은 비어 있다(= 새 글 작성으로 만든 글). */
    scheduledPostIds: new Set<string>(),
    showToast: vi.fn(),
    reportError: vi.fn(),
  },
}));

vi.mock("../api/client", () => ({ request: mocks.request }));
vi.mock("../store", () => ({ useStore: () => mocks.store }));

import { PostCard } from "./PostCard";

const ITEM: BlogTaskListItem = {
  postId: "post_1",
  userId: "user_1",
  status: "READY_TO_PUBLISH",
  version: 3,
  createdAt: "2026-08-04T00:00:00.000Z",
  updatedAt: "2026-08-04T01:00:00.000Z",
  title: "가벼운 목록 카드",
  topic: "소재",
  purposes: ["정보 전달"],
  hasFinalPost: true,
};

const DETAIL = {
  postId: "post_1",
  userId: "user_1",
  status: "READY_TO_PUBLISH",
  version: 3,
  createdAt: ITEM.createdAt,
  updatedAt: ITEM.updatedAt,
  statusHistory: [],
  input: { topic: "소재", keywords: [], referenceMaterials: [] },
  postingLogs: [],
  finalPost: {
    title: "가벼운 목록 카드",
    body: "본문",
    hashtags: ["테스트"],
    htmlContent: "<h1>가벼운 목록 카드</h1><p>본문</p>",
  },
} as BlogTask;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

describe("PostCard summary 동작", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.session = { user: { userId: "user_1" } };
    mocks.store.scheduledPostIds = new Set<string>();
    location.hash = "#/posts";
    vi.stubGlobal("ClipboardItem", undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: mocks.writeText },
    });
    mocks.request.mockResolvedValue(DETAIL);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  /** 카드의 첫 버튼 — '이어서 쓰기' / '발행하기' / '예약 작업 보기'가 서는 자리. */
  function openButton(): HTMLButtonElement {
    const found = container.querySelector<HTMLButtonElement>(".actions button");
    if (!found) throw new Error("카드의 열기 버튼을 찾지 못했습니다.");
    return found;
  }

  it("예약이 아직 만들고 있는 글은 작업 큐로 보낸다", async () => {
    // 2026-08-06 사용자 지적 — 새 글 작성과 예약 포스팅은 하는 일이 다르다. 예약이
    // 만들고 있는 글의 진행은 그 글만 뚝 떼어 놓은 새 글 작성 3단계가 아니라, 나머지
    // 예약까지 함께 보이는 작업 큐에서 봐야 한다.
    const 만드는중: BlogTaskListItem = {
      ...ITEM,
      status: "GENERATING",
      hasFinalPost: false,
    };
    mocks.store.scheduledPostIds = new Set(["post_1"]);
    await act(async () => root.render(<PostCard task={만드는중} />));

    expect(openButton().textContent).toBe("예약 작업 보기");
    await act(async () => openButton().click());

    expect(location.hash).toBe("#/scheduled/queue");
    // 새 글 작성으로는 열지 않는다.
    expect(mocks.store.openPost).not.toHaveBeenCalled();
  });

  it("예약 글이라도 원고가 나온 뒤에는 그대로 글을 연다", async () => {
    // 그때는 '발행하기'가 원고를 확인하고 손으로 올리는 자리다 — 예약 글에도 쓸모가 있다.
    mocks.store.scheduledPostIds = new Set(["post_1"]);
    await act(async () => root.render(<PostCard task={ITEM} />));

    expect(openButton().textContent).toBe("발행하기");
    await act(async () => openButton().click());

    expect(mocks.store.openPost).toHaveBeenCalledWith("post_1");
  });

  it("새 글 작성으로 만든 글은 만드는 중이어도 그대로 글을 연다", async () => {
    const 만드는중: BlogTaskListItem = {
      ...ITEM,
      status: "GENERATING",
      hasFinalPost: false,
    };
    await act(async () => root.render(<PostCard task={만드는중} />));

    expect(openButton().textContent).toBe("생성 진행 보기");
    await act(async () => openButton().click());

    expect(mocks.store.openPost).toHaveBeenCalledWith("post_1");
  });

  it("예약으로 만든 글에는 어디서 왔는지 표식이 붙는다", async () => {
    // 표식이 없으면 같은 목록에서 버튼 행선이 갈리는 이유가 화면에 없다.
    mocks.store.scheduledPostIds = new Set(["post_1"]);
    await act(async () => root.render(<PostCard task={ITEM} />));
    // '예약'만으로는 무엇의 예약인지 알 수 없어 2026-08-07에 이름을 폈다.
    expect(container.querySelector(".post-origin")?.textContent).toBe("예약 포스팅");

    mocks.store.scheduledPostIds = new Set<string>();
    await act(async () => root.render(<PostCard task={ITEM} />));
    expect(container.querySelector(".post-origin")).toBeNull();
  });

  it("원고 복사를 누를 때만 상세 글을 받아 복사한다", async () => {
    await act(async () => root.render(<PostCard task={ITEM} layout="previous-grid" />));
    expect(mocks.request).not.toHaveBeenCalled();

    const copyButton = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("원고 복사"),
    );
    expect(copyButton).toBeDefined();

    await act(async () => copyButton?.click());

    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1");
    expect(mocks.writeText).toHaveBeenCalled();
    expect(mocks.store.showToast).toHaveBeenCalledWith(
      "글을 복사했습니다. 에디터에 그대로 붙여넣으세요.",
    );
  });

  it("리스트에서는 원고 복사가 아이콘이고, 눌러도 하는 일은 같다", async () => {
    // 글자로 두면 '원고 보기 · 원고 복사 · 발행글 열기' 셋이 한 줄에 안 들어가
    // 줄이 두 줄로 깨졌다(2026-08-06 사용자 요청).
    await act(async () => root.render(<PostCard task={ITEM} layout="list" />));

    const copyButton = container.querySelector<HTMLButtonElement>(".post-card-copy");
    expect(copyButton).not.toBeNull();
    // 아이콘만 있으므로 이름은 따로 붙여야 한다 — 없으면 읽어 주는 쪽에 빈 버튼이다.
    expect(copyButton?.getAttribute("aria-label")).toBe("원고 복사");
    expect(copyButton?.textContent).toBe("");

    await act(async () => copyButton?.click());

    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1");
    expect(mocks.writeText).toHaveBeenCalled();
  });

  it("상세 응답 전에 계정이 바뀌면 이전 계정 원고를 복사하지 않는다", async () => {
    const pending = deferred<BlogTask>();
    mocks.request.mockReturnValue(pending.promise);
    await act(async () => root.render(<PostCard task={ITEM} layout="previous-grid" />));

    const copyButton = [...container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("원고 복사"),
    );
    await act(async () => copyButton?.click());
    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1");

    mocks.store.session = { user: { userId: "user_2" } };
    await act(async () => root.render(<PostCard task={ITEM} layout="previous-grid" />));

    pending.resolve(DETAIL);
    await act(async () => {
      await pending.promise;
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
    });

    expect(mocks.writeText).not.toHaveBeenCalled();
    expect(mocks.store.showToast).not.toHaveBeenCalled();
  });
});
