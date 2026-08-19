import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BlogTaskListItem } from "../api/types";
import { STATUS_LABELS } from "../constants";

const mocks = vi.hoisted(() => ({
  store: {
    posts: [] as BlogTaskListItem[],
    postsLoading: true,
    deletePosts: vi.fn(async () => undefined),
    postsFilter: null,
    setPostsFilter: vi.fn(),
    /** 화면을 열 때 목록을 다시 읽는다 — 예약이 만든 글이 새로고침 없이 나타나게. */
    reloadPosts: vi.fn(async () => undefined),
    /** 예약 포스팅이 만든 글의 postId. 이 화면의 시험은 전부 새 글 작성 쪽이다. */
    scheduledPostIds: new Set<string>(),
    openPost: vi.fn(async () => undefined),
    showToast: vi.fn(),
    reportError: vi.fn(),
  },
}));

vi.mock("../store", () => ({ useStore: () => mocks.store }));

import { PostsView } from "./PostsView";

const ITEM: BlogTaskListItem = {
  postId: "post_1",
  userId: "user_1",
  status: "INPUT",
  version: 1,
  createdAt: "2026-08-04T00:00:00.000Z",
  updatedAt: "2026-08-04T00:00:00.000Z",
  title: "저장된 글",
  topic: "소재",
  purposes: [],
  hasFinalPost: false,
};

describe("PostsView 목록 로딩 상태", () => {
  let container: HTMLDivElement;
  let root: Root;

  async function render() {
    await act(async () => root.render(<PostsView />));
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.posts = [];
    mocks.store.postsLoading = true;
    mocks.store.postsFilter = null;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("최초 요청 중에는 실제 빈 목록이라고 말하지 않는다", async () => {
    await render();

    expect(container.querySelector('[role="status"]')?.textContent).toContain(
      "저장된 글을 불러오는 중",
    );
    expect(container.textContent).not.toContain("아직 쓴 글이 없습니다.");

    mocks.store.postsLoading = false;
    await render();

    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(container.textContent).toContain("아직 쓴 글이 없습니다.");
  });

  it("기존 목록을 갱신할 때는 카드를 지우지 않는다", async () => {
    mocks.store.posts = [ITEM];
    await render();

    expect(container.textContent).toContain("저장된 글");
    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(container.querySelector(".posts-card-grid")?.getAttribute("aria-busy")).toBe("true");
  });

  it("화면을 열면 목록을 서버에서 다시 읽는다", async () => {
    // 목록은 로그인할 때 한 번만 읽고 아무도 다시 읽지 않았다. 그래서 예약 포스팅이
    // 만든 글은 새로고침 전까지 여기 나타나지 않았다(2026-08-06 신고).
    await render();

    expect(mocks.store.reloadPosts).toHaveBeenCalledTimes(1);
  });
});


describe("PostsView 보기 방식과 필터", () => {
  let container: HTMLDivElement;
  let root: Root;

  const post = (postId: string, status: BlogTaskListItem["status"], title: string) =>
    ({ ...ITEM, postId, status, title }) as BlogTaskListItem;

  async function render() {
    await act(async () => root.render(<PostsView />));
  }

  function click(label: string) {
    const button = [...container.querySelectorAll("button")].find(
      (node) => node.textContent?.trim() === label || node.getAttribute("aria-label") === label,
    );
    return act(async () => button?.click());
  }

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mocks.store.postsLoading = false;
    mocks.store.postsFilter = null;
    mocks.store.posts = [
      post("post_1", "READY_TO_PUBLISH", "먼저 만든 글"),
      post("post_2", "POSTED", "나중에 만든 글"),
    ];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("리스트로 보기를 누르면 목록이 리스트가 된다", async () => {
    await render();
    expect(container.querySelector(".posts-card-grid")).not.toBeNull();

    await click("리스트로 보기");

    expect(container.querySelector(".posts-list")).not.toBeNull();
    expect(container.querySelector(".posts-card-grid")).toBeNull();
  });

  it("고른 보기 방식은 화면을 다시 열어도 남는다", async () => {
    // 목록을 볼 때마다 다시 고르게 하지 않는다.
    await render();
    await click("리스트로 보기");

    await act(async () => root.unmount());
    root = createRoot(container);
    await render();

    expect(container.querySelector(".posts-list")).not.toBeNull();
  });

  it("리스트에는 어떤 칸인지 알려 주는 머리글이 있다", async () => {
    await render();
    await click("리스트로 보기");

    const head = [...container.querySelectorAll(".posts-list-head span")].map(
      (node) => node.textContent,
    );

    expect(head).toEqual(["순번", "소재", "제목", "상태", "글의 목적", "생성 시각", ""]);
  });

  it("리스트의 줄마다 몇 번째인지 적는다", async () => {
    // 2026-08-07 사용자 요청. 글에 붙은 번호가 아니라 **지금 화면의 순서**다 —
    // 정렬이나 필터를 바꾸면 같은 글의 번호가 달라진다.
    mocks.store.postsLoading = false;
    mocks.store.posts = [ITEM, { ...ITEM, postId: "post_2" }, { ...ITEM, postId: "post_3" }];
    await render();
    await click("리스트로 보기");

    const numbers = [...container.querySelectorAll(".post-card-index-cell")].map(
      (node) => node.textContent,
    );
    expect(numbers).toEqual(["1", "2", "3"]);
  });

  it("카드형에는 순번을 붙이지 않는다", async () => {
    // 카드는 격자로 놓여 있어 '위에서 몇 번째'가 한 줄로 세어지지 않는다.
    mocks.store.postsLoading = false;
    mocks.store.posts = [ITEM, { ...ITEM, postId: "post_2" }];
    await render();

    expect(container.querySelector(".post-card-index-cell")).toBeNull();
  });

  it("보여 줄 글이 없으면 머리글도 없다", async () => {
    mocks.store.posts = [];
    await render();
    await click("리스트로 보기");

    expect(container.querySelector(".posts-list-head")).toBeNull();
  });

  it("오래된 순은 순서를 뒤집는다", async () => {
    await render();
    await click("정렬과 상태");
    const titles = () => [...container.querySelectorAll("h3")].map((node) => node.textContent);
    expect(titles()).toEqual(["먼저 만든 글", "나중에 만든 글"]);

    await click("오래된 순");

    expect(titles()).toEqual(["나중에 만든 글", "먼저 만든 글"]);
  });

  it("글에 붙는 상태 이름이 하나도 빠짐없이 필터에 있다", async () => {
    // 한때 셋으로 줄였더니 '소재 준비됨'·'원고 만드는 중' 같은 글이 어느 항목에도
    // 안 잡혔다 — 화면에는 그 이름이 떠 있는데 필터에는 없으니 고를 방법이 없었다
    // (2026-08-06 사용자 지적).
    await render();
    await click("정렬과 상태");

    const options = [...container.querySelectorAll(".posts-filter-option")].map(
      (node) => node.textContent ?? "",
    );

    expect(options.slice(0, 3)).toEqual(["최신순", "오래된 순", "전체"]);
    for (const label of Object.values(STATUS_LABELS)) {
      expect(options, `${label.text}를 고를 수 없다`).toContain(label.text);
    }
  });
  it("제목 옆에 지금 목록에 보이는 글 수를 적는다", async () => {
    // 2026-08-07 사용자 요청 — '내 글'만 있으면 몇 편인지 알 수 없다.
    mocks.store.postsLoading = false;
    mocks.store.posts = [
      ITEM,
      { ...ITEM, postId: "post_2" },
      { ...ITEM, postId: "post_3" },
    ];
    await render();

    expect(container.querySelector(".posts-count")?.textContent).toBe("3편");
  });

  it("처음 불러오는 동안에는 수를 적지 않는다", async () => {
    // 그때 posts는 아직 비어 있다. '0편'은 "글이 없다"가 아니라 "아직 모른다"이다.
    mocks.store.postsLoading = true;
    mocks.store.posts = [];
    await render();

    expect(container.querySelector(".posts-count")).toBeNull();
  });

  it("필터가 걸려 있으면 걸러진 수를 적는다", async () => {
    // 아래 줄 수와 같은 말을 해야 한다. 무엇으로 걸렀는지는 옆의 필터 배지가 말한다.
    mocks.store.postsLoading = false;
    mocks.store.posts = [
      ITEM,
      { ...ITEM, postId: "post_2", status: "READY_TO_PUBLISH" },
      { ...ITEM, postId: "post_3", status: "READY_TO_PUBLISH" },
    ];
    mocks.store.postsFilter = { type: "status", status: "READY_TO_PUBLISH" } as never;
    await render();

    expect(container.querySelector(".posts-count")?.textContent).toBe("2편");
  });

  it("글이 하나도 없으면 0편이라고 적는다", async () => {
    // 다 불러왔는데 비어 있는 것은 사실이다 — 그때는 숨기지 않는다.
    mocks.store.postsLoading = false;
    mocks.store.posts = [];
    await render();

    expect(container.querySelector(".posts-count")?.textContent).toBe("0편");
  });
});
