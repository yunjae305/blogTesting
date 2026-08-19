import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BlogTask } from "../../api/types";

const mocks = vi.hoisted(() => ({
  store: {
    task: null as BlogTask | null,
    settings: null,
    personas: [],
    personaCatalogLoading: false,
    draftRounds: [] as {
      title?: string;
      keywords: string[];
      intentId?: string;
      intentTitle?: string;
    }[],
  },
}));

vi.mock("../../store", () => ({ useStore: () => mocks.store }));

import { Summary } from "./Summary";

function summaryValue(container: ParentNode, label: string): string | null {
  const row = [...container.querySelectorAll(".summary-row")].find(
    (candidate) =>
      candidate.querySelector(".summary-key")?.textContent?.trim() === label,
  );
  return row?.querySelector(".summary-value")?.textContent?.trim() ?? null;
}

describe("Summary", () => {
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
    mocks.store.task = null;
  });

  it("keeps the entered subject after a recommended title is selected", async () => {
    mocks.store.task = {
      postId: "post_1",
      userId: "user_1",
      status: "INTENT_SELECTED",
      version: 1,
      createdAt: "2026-07-29T00:00:00.000Z",
      updatedAt: "2026-07-29T00:00:00.000Z",
      statusHistory: [],
      input: {
        topic: "토스뱅크 통장이 골 파킹통장으로 바뀐 이유",
        keywords: [],
        referenceMaterials: [],
      },
      postingLogs: [],
      trendSelection: {
        topicCandidateId: "topic_1",
        finalTopic: "토스뱅크 통장이 골 파킹통장, 지금 주목받는 이유",
        selectedTrendKeywordIds: ["trend_1"],
        skipped: false,
        selectedAt: "2026-07-29T00:01:00.000Z",
      },
    };

    await act(async () => {
      root.render(<Summary />);
    });

    expect(summaryValue(container, "소재")).toBe(
      "토스뱅크 통장이 골 파킹통장으로 바뀐 이유",
    );
    expect(summaryValue(container, "소재")).not.toBe(
      mocks.store.task.trendSelection?.finalTopic,
    );
  });

  describe("선택 키워드", () => {
    function taskWith(selectedKeywords?: string[]): BlogTask {
      return {
        postId: "post_1",
        userId: "user_1",
        status: "INTENT_SELECTED",
        version: 1,
        createdAt: "2026-08-04T00:00:00.000Z",
        updatedAt: "2026-08-04T00:00:00.000Z",
        statusHistory: [],
        input: { topic: "델타포스", keywords: [], referenceMaterials: [] },
        postingLogs: [],
        trendSelection: {
          finalTopic: "델타포스 비콘 에어리어",
          selectedTrendKeywordIds: ["trend_1"],
          selectedKeywords,
          skipped: false,
          selectedAt: "2026-08-04T00:01:00.000Z",
        },
      } as BlogTask;
    }

    it("사용자가 고른 검색어를 소재 아래에 보여준다", async () => {
      mocks.store.task = taskWith(["델타포스", "비콘 에어리어"]);

      await act(async () => {
        root.render(<Summary />);
      });

      expect(summaryValue(container, "선택 키워드")).toBe("델타포스, 비콘 에어리어");
      // 소재와 다른 값이다 — 소재는 사용자가 입력한 것, 키워드는 트렌드에서 고른 것.
      expect(summaryValue(container, "소재")).toBe("델타포스");
    });

    it("트렌드를 건너뛴 글·옛 문서에는 자리만 비운다", async () => {
      // selectedKeywords는 나중에 생긴 필드라 예전 문서에는 없다.
      mocks.store.task = taskWith(undefined);

      await act(async () => {
        root.render(<Summary />);
      });

      expect(summaryValue(container, "선택 키워드")).toBe("-");
    });

    it("빈 문자열은 걸러 낸다", async () => {
      mocks.store.task = taskWith(["델타포스", "", "  "]);

      await act(async () => {
        root.render(<Summary />);
      });

      expect(summaryValue(container, "선택 키워드")).toBe("델타포스");
    });
  });
});


/**
 * 여러 편을 만들 때는 편마다 고른 키워드를 따로 보여 준다(2026-08-12 사용자 요청).
 *
 * 글 하나에는 트렌드 선택 자리가 하나뿐이라, 2편째 ②를 지나면 1편째 키워드가 사라진다.
 * 앞서 무엇을 골랐는지 보이지 않으면 다음 편에서 겹치는지 판단할 수 없다.
 */
describe("여러 편일 때의 키워드", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.draftRounds = [];
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  function render(draftCount: number) {
    mocks.store.task = {
      postId: "post_1",
      input: { topic: "마이쮸", draftCount },
      trendSelection: { selectedKeywords: ["옛 키워드"] },
    } as unknown as BlogTask;
    return act(async () => root.render(<Summary />));
  }

  it("한 편이면 예전 그대로 '선택 키워드' 한 줄이다", async () => {
    await render(1);

    expect(summaryValue(container, "선택 키워드")).toBe("옛 키워드");
    expect(summaryValue(container, "원고1 키워드")).toBeNull();
  });

  it("두 편이면 편마다 줄이 생긴다", async () => {
    mocks.store.draftRounds = [{ keywords: ["마이쮸", "토마토맛"] }];
    await render(2);

    expect(summaryValue(container, "원고1 키워드")).toBe("마이쮸, 토마토맛");
    // 1편째는 아직 방향을 안 골라 끝나지 않았다 — 2편째는 시작도 안 했으므로 '-'다.
    // 예전에는 배열 길이로 세어 여기서 벌써 '고르는 중'이라고 했다(2026-08-12).
    expect(summaryValue(container, "원고2 키워드")).toBe("-");
  });

  it("한 편이 방향까지 끝나야 다음 편이 '고르는 중'이 된다", async () => {
    mocks.store.draftRounds = [
      { keywords: ["마이쮸", "토마토맛"], intentId: "intent_1" },
    ];
    await render(2);

    expect(summaryValue(container, "원고2 키워드")).toBe("고르는 중");
  });

  it("트렌드 없이 간 편은 빈칸이 아니라 그 사실을 적는다", async () => {
    mocks.store.draftRounds = [{ keywords: [], intentId: "intent_1" }];
    await render(2);

    expect(summaryValue(container, "원고1 키워드")).toBe("트렌드 없이 소재만");
  });

  it("앞 편의 키워드가 뒤 편에 덮어써지지 않는다", async () => {
    mocks.store.draftRounds = [
      { keywords: ["마이쮸"] },
      { keywords: ["꿀타래"] },
    ];
    await render(2);

    expect(summaryValue(container, "원고1 키워드")).toBe("마이쮸");
    expect(summaryValue(container, "원고2 키워드")).toBe("꿀타래");
  });
});
