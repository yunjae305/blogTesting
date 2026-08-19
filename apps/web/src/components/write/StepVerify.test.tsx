import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BlogTask, IntentCandidate, TrendRecommendation } from "../../api/types";

/**
 * 작성 전 검증 팝업은 디자인만 다시 그렸다. 화면이 바뀌어도 검색 중/검증 완료 두 상태가
 * 갈리는 기준, 방향 선택, 자료 체크 해제와 사용 개수, 확인하고 계속이 보내는 payload가
 * 예전 그대로인지 확인한다.
 */
const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  store: {
    task: null as BlogTask | null,
    recommendation: null as TrendRecommendation | null,
    setTask: vi.fn(),
    draftRounds: [] as {
      title?: string;
      keywords: string[];
      intentId?: string;
      intentTitle?: string;
      /** 고른 방향 전체. 서버가 자리번호로는 되찾을 수 없는 값이다(2026-08-12). */
      intent?: IntentCandidate;
    }[],
    setDraftRounds: vi.fn(),
    setSelectedTrendKeywordIds: vi.fn(),
    setTopicCandidates: vi.fn(),
    setStep: vi.fn(),
    draftAutoStart: false,
    setDraftAutoStart: vi.fn(),
    showToast: vi.fn(),
    reportError: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ request: mocks.request }));
vi.mock("../../store", () => ({ useStore: () => mocks.store }));

import { WRITE_STEP } from "../../resume";
import { StepVerify } from "./StepVerify";

function candidate(index: number, sources: number): IntentCandidate {
  return {
    intentId: `intent_${index}`,
    title: `방향 제목 ${index}`,
    targetReader: `독자 ${index}`,
    rationale: `근거 ${index}`,
    keywords: [],
    sources: Array.from({ length: sources }, (_, i) => ({
      title: `자료 제목 ${index}-${i}`,
      url: `https://startupdaily.kr/${index}/${i}`,
      snippet: `자료 설명 ${index}-${i}`,
      sourceType: "NEWS" as const,
      relevanceScore: 90 - i,
    })),
  };
}

function taskWith(
  candidates: IntentCandidate[],
  collectedSourceCount?: number,
): BlogTask {
  return {
    postId: "post_1",
    userId: "user_1",
    status: "SEARCH_ANALYZING",
    version: 1,
    createdAt: "2026-07-29T00:00:00.000Z",
    updatedAt: "2026-07-29T00:00:00.000Z",
    statusHistory: [],
    input: {
      topic: "AIONA",
      purpose: ["트렌드·이슈 소개"],
      keywords: ["트렌드·이슈 소개"],
      referenceMaterials: [],
    },
    postingLogs: [],
    trendSelection: {
      finalTopic: "AI 개발사 지형이 달라지고 있어요",
      selectedTrendKeywordIds: [],
      skipped: false,
      selectedAt: "2026-07-29T00:01:00.000Z",
    },
    intentValidationResult: candidates.length
      ? {
          promptVersion: "v1",
          provider: "anthropic",
          model: "claude",
          analyzedAt: "2026-07-29T00:02:00.000Z",
          intentCandidates: candidates,
          ...(collectedSourceCount === undefined ? {} : { collectedSourceCount }),
        }
      : undefined,
  };
}

describe("StepVerify 작성 전 검증", () => {
  let container: HTMLDivElement;
  let root: Root;
  const onReanalyze = vi.fn(async () => null);
  const onBackToTitle = vi.fn();

  async function render(analyzing = false) {
    await act(async () => {
      root.render(
        <StepVerify
          onReanalyze={onReanalyze}
          onBackToTitle={onBackToTitle}
          analyzing={analyzing}
        />,
      );
    });
  }

  function text(selector: string): string {
    return container.querySelector(selector)?.textContent?.trim() ?? "";
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = taskWith([]);
    mocks.store.recommendation = null;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("검색 중에는 입력정보와 진행 안내를 보여준다", async () => {
    await render(true);

    // 입력정보는 펼쳐진 상태고, 선택 제목 카드가 강조된다.
    // 8칸 — 2026-08-11에 '작업 시각'이 늘었다(비어 있으면 '즉시 생성').
    expect(container.querySelectorAll(".prewriting-input-card")).toHaveLength(8);
    expect(text(".prewriting-input-grid")).toContain("즉시 생성");
    expect(text(".prewriting-input-card--title")).toContain(
      "AI 개발사 지형이 달라지고 있어요",
    );
    expect(container.querySelector(".prewriting-input-fold")).toBeNull();

    const progress = container.querySelector(".prewriting-search-progress");
    expect(progress?.textContent).toContain("관련 자료를 검색하고 있습니다");
    expect(progress?.getAttribute("aria-live")).toBe("polite");

    // 검색 중에는 다음으로 넘어갈 수 없다.
    expect(container.querySelector<HTMLButtonElement>(".prewriting-confirm")?.disabled).toBe(
      true,
    );
    expect(text(".prewriting-status")).toContain("자료 검색");
  });

  it("검증이 끝나면 방향 후보와 참고자료를 보여준다", async () => {
    mocks.store.task = taskWith([candidate(1, 3), candidate(2, 2), candidate(3, 1)]);
    await render();

    expect(text(".prewriting-status")).toContain("검증 완료");
    // 후보 개수는 서버 결과 그대로 — 프론트에서 늘리거나 줄이지 않는다.
    expect(container.querySelectorAll(".prewriting-direction-card")).toHaveLength(3);
    // 첫 후보가 기본 선택이고, 그 후보의 자료가 목록에 뜬다.
    expect(text(".prewriting-direction-card--selected")).toContain("방향 1");
    expect(container.querySelectorAll(".prewriting-source-item")).toHaveLength(3);
    expect(text(".prewriting-source-count")).toBe("3/3개 사용");
    // 입력정보는 사라지지 않고 접혀 있다.
    expect(container.querySelector(".prewriting-input-fold")).not.toBeNull();
    // 출처는 url에서 뽑은 도메인으로 표시한다.
    expect(text(".prewriting-source-domain")).toBe("startupdaily.kr");
  });

  it("검색이 더 찾았으면 총 몇 개인지 목록 아래에 적는다", async () => {
    // 2026-08-07 사용자 신고 — 방향 하나에 붙는 자료는 상한이 있어 다 보이지 않는데,
    // 그 사실을 안 적으면 "자료를 이만큼밖에 못 찾았나"로 읽힌다.
    mocks.store.task = taskWith([candidate(1, 3), candidate(2, 2), candidate(3, 1)], 9);
    await render();

    const more = text(".prewriting-source-more");
    expect(more).toContain("총 9개");
    // **버려진다고 적지 않는다.** 안 보이는 자료는 다른 방향이 쓰고 있을 수 있고,
    // 사용자가 방향을 바꾸면 그 자료가 올라온다(2026-08-07 사용자 지적).
    expect(more).toContain("이 방향에 관련도가 높은 3개");
    expect(more).not.toContain("쓰지 않습니다");
  });

  it("총 개수가 보이는 자료 수와 같으면 아무것도 적지 않는다", async () => {
    mocks.store.task = taskWith([candidate(1, 3), candidate(2, 2), candidate(3, 1)], 3);
    await render();

    expect(container.querySelector(".prewriting-source-more")).toBeNull();
  });

  it("총 개수가 없는 옛 검증 결과에는 아무것도 적지 않는다", async () => {
    // 이 필드가 생기기 전에 저장된 글이다. 없는 숫자를 지어내지 않는다.
    mocks.store.task = taskWith([candidate(1, 3), candidate(2, 2), candidate(3, 1)]);
    await render();

    expect(container.querySelector(".prewriting-source-more")).toBeNull();
  });

  it("다른 방향을 고르면 그 방향의 자료로 바뀐다", async () => {
    mocks.store.task = taskWith([candidate(1, 3), candidate(2, 2)]);
    await render();

    const second = container.querySelectorAll<HTMLInputElement>(
      '.prewriting-direction-card input[type="radio"]',
    )[1];
    await act(async () => {
      second.click();
    });

    expect(text(".prewriting-direction-card--selected")).toContain("방향 2");
    expect(container.querySelectorAll(".prewriting-source-item")).toHaveLength(2);
    expect(text(".prewriting-source-count")).toBe("2/2개 사용");
  });

  it("자료 체크를 해제하면 사용 개수가 줄고 확인 시 제외 목록으로 나간다", async () => {
    mocks.store.task = taskWith([candidate(1, 3)]);
    mocks.request.mockResolvedValue(mocks.store.task);
    await render();

    const first = container.querySelector<HTMLInputElement>(
      '.prewriting-source-item input[type="checkbox"]',
    );
    await act(async () => {
      first?.click();
    });

    expect(text(".prewriting-source-count")).toBe("2/3개 사용");
    // 해제한 자료를 목록에서 지우지는 않는다.
    expect(container.querySelectorAll(".prewriting-source-item")).toHaveLength(3);
    expect(container.querySelectorAll(".prewriting-source-item.is-excluded")).toHaveLength(1);

    await act(async () => {
      container.querySelector<HTMLButtonElement>(".prewriting-confirm")?.click();
    });

    expect(mocks.request).toHaveBeenLastCalledWith("/posts/post_1/intents/select", {
      method: "POST",
      body: {
        intentId: "intent_1",
        excludedSourceUrls: ["https://startupdaily.kr/1/0"],
      },
    });
    expect(mocks.store.setStep).toHaveBeenCalledWith(WRITE_STEP.DRAFT);
  });

  it("제목 다시 고르기는 돌고 있는 검증을 멈추는 쪽에 맡긴다", async () => {
    // 팝업일 때는 '닫기'였다. 단계가 된 뒤로는 닫아 봐야 갈 데가 없다 — 되돌아갈 곳은
    // 제목 단계다. 그리고 **돌아가면서 검증을 멈춘다**(2026-08-07 사용자 지적):
    // 그 검증의 결과는 어느 쪽이든 쓰이지 않는데 그대로 두면 검색과 LLM을 끝까지 쓴다.
    // 단계 이동만 여기서 하면 그 중단을 부를 자리가 없어, 한 handler에 함께 맡긴다.
    mocks.store.task = taskWith([candidate(1, 1)]);
    await render();

    const back = [...container.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === "제목 다시 고르기",
    );
    await act(async () => {
      back?.click();
    });

    expect(onBackToTitle).toHaveBeenCalledTimes(1);
    // 이 화면이 직접 단계를 옮기지 않는다 — 멈춤과 이동은 한 곳에서 함께 일어난다.
    expect(mocks.store.setStep).not.toHaveBeenCalledWith(WRITE_STEP.TITLE);
  });

  it("단계가 되었으니 닫기 버튼은 없다", async () => {
    mocks.store.task = taskWith([candidate(1, 1)]);
    await render();

    expect(container.querySelector(".prewriting-modal__close")).toBeNull();
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it("검증 결과가 없으면 계속 진행을 막고 다시 검증을 안내한다", async () => {
    await render();

    expect(container.querySelector(".prewriting-direction-grid")).toBeNull();
    expect(container.querySelector<HTMLButtonElement>(".prewriting-confirm")?.disabled).toBe(
      true,
    );
    expect(text(".prewriting-source-empty")).toContain("아직 검증 결과가 없습니다");

    await act(async () => {
      container.querySelector<HTMLButtonElement>(".prewriting-retry")?.click();
    });
    expect(onReanalyze).toHaveBeenCalledTimes(1);
  });
});

/**
 * 한 소재로 여러 편 — ②③을 편수만큼 돈다(2026-08-12 사용자 결정).
 *
 * 라운드마다 서버에 저장하지 않는다. 글 하나에는 제목·방향 자리가 하나뿐이라 다음
 * 라운드가 덮어쓰기 때문이다 — 화면이 배열로 들고 있다가 마지막에 한 번에 보낸다.
 */
describe("StepVerify 여러 편 라운드", () => {
  let container: HTMLDivElement;
  let root: Root;

  function multiTask(draftCount: number): BlogTask {
    const base = taskWith([candidate(1, 1), candidate(2, 1), candidate(3, 1)]);
    return { ...base, input: { ...base.input, draftCount } } as BlogTask;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.draftRounds = [];
    // 확정 직전에 최신 글을 한 번 더 읽는다(고른 방향이 아직 있는지) — 그 응답이다.
    mocks.request.mockResolvedValue(
      taskWith([candidate(1, 1), candidate(2, 1), candidate(3, 1)]),
    );
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function confirm() {
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".prewriting-confirm")!.click();
    });
  }

  it("마지막 편이 아니면 제목 단계로 돌아간다 — 서버에 저장하지 않는다", async () => {
    mocks.store.task = multiTask(2);
    await act(async () => {
      root.render(
        <StepVerify onReanalyze={vi.fn()} onBackToTitle={vi.fn()} analyzing={false} />,
      );
    });

    await confirm();

    expect(mocks.store.setStep).toHaveBeenCalledWith(WRITE_STEP.TITLE);
    // 라운드 중에는 방향을 **저장하지** 않는다 — 다음 라운드가 덮어쓴다. 최신 글을
    // 한 번 읽는 것은 저장이 아니다(고른 방향이 아직 있는지 보는 확인이다).
    const wrote = mocks.request.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(wrote).toHaveLength(0);
  });

  it("고른 방향을 라운드 배열에 담는다", async () => {
    mocks.store.task = multiTask(2);
    await act(async () => {
      root.render(
        <StepVerify onReanalyze={vi.fn()} onBackToTitle={vi.fn()} analyzing={false} />,
      );
    });

    await confirm();

    const saved = mocks.store.setDraftRounds.mock.calls.at(-1)?.[0];
    expect(saved).toHaveLength(1);
    expect(saved[0].intentId).toBeTruthy();
  });

  it("마지막 편이면 짝 전부를 작업 큐로 보낸다", async () => {
    mocks.store.task = multiTask(2);
    mocks.store.draftRounds = [
      { title: "1편 제목", keywords: ["가"], intentId: "intent_1" },
    ];
    await act(async () => {
      root.render(
        <StepVerify onReanalyze={vi.fn()} onBackToTitle={vi.fn()} analyzing={false} />,
      );
    });

    await confirm();

    const call = mocks.request.mock.calls.find(([path]) =>
      String(path).endsWith("/schedule"),
    );
    expect(call).toBeTruthy();
    // 첫 편은 원본 글에, 나머지는 복제로 — 서버가 그렇게 가른다.
    expect(call![1].body.primaryDraft.intentId).toBe("intent_1");
    expect(call![1].body.additionalDrafts).toHaveLength(1);
  });
});

/**
 * 라운드 번호를 **끝난 라운드 수로** 센다(2026-08-12 사용자 신고).
 *
 *     "첫번째꺼 글 쓰는 건데 이렇게 2번째 글 쓰는 거로 기록되는 것 같아"
 *
 * 배열 길이로 세면, ②가 1편째 제목을 담는 순간 길이가 1이 되어 ③이 그것을 2편째로
 * 읽는다. 한 라운드는 **방향까지 골라야** 끝난다.
 */
/**
 * 자료 수집이 실패하면 서버가 실패 사유를 담은 후보 **한 장**을 보낸다
 * (`{postId}_intent_failed`). 그것을 개수로만 세면 화면이 '검증 완료'라고 말하면서 실패
 * 안내를 '방향 1'이라는 고를 수 있는 카드로 그렸다 — 사용자는 기능이 망가진 것으로 읽었다
 * (2026-08-12 신고: "방향 4가지 보여주는거 어디갔어").
 */
describe("StepVerify 검증 실패", () => {
  let container: HTMLDivElement;
  let root: Root;

  function failedTask(): BlogTask {
    const failure: IntentCandidate = {
      intentId: "post_1_intent_failed",
      title: "롯데리아",
      targetReader: "블로그 독자",
      rationale:
        "AI가 자료는 찾았지만 정리한 내용을 돌려주지 않았습니다. '다시 검증'을 눌러 주세요.",
      keywords: [],
      sources: [],
    };
    return taskWith([failure]);
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = failedTask();
    mocks.store.recommendation = null;
    mocks.store.draftRounds = [];
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function render() {
    await act(async () => {
      root.render(
        <StepVerify onReanalyze={vi.fn()} onBackToTitle={vi.fn()} analyzing={false} />,
      );
    });
  }

  it("'검증 완료'라고 말하지 않는다", async () => {
    await render();

    expect(container.querySelector(".prewriting-status")?.textContent).toContain("검증 실패");
  });

  it("실패 안내를 고를 수 있는 방향 카드로 그리지 않는다", async () => {
    await render();

    expect(container.querySelector(".prewriting-direction-card")).toBeNull();
    expect(container.querySelector('input[name="intent"]')).toBeNull();
  });

  it("후보가 줄어든 것이 아니라 만들지 못했다고 말한다", async () => {
    await render();

    const failed = container.querySelector(".prewriting-failed");
    expect(failed?.textContent).toContain("글의 방향을 만들지 못했습니다");
    expect(failed?.textContent).toContain("방향 후보 4개");
    expect(failed?.textContent).toContain("정리한 내용을 돌려주지 않았습니다");
  });

  it("그대로 진행하면 자료 없이 쓴다는 것을 버튼이 말한다", async () => {
    await render();

    expect(container.querySelector(".prewriting-confirm")?.textContent).toContain(
      "자료 없이 계속",
    );
  });

  it("정상 검증은 예전 그대로다", async () => {
    mocks.store.task = taskWith([candidate(1, 2), candidate(2, 1)]);
    await render();

    expect(container.querySelector(".prewriting-failed")).toBeNull();
    expect(container.querySelector(".prewriting-status")?.textContent).toContain("검증 완료");
    expect(container.querySelectorAll(".prewriting-direction-card").length).toBe(2);
  });
});

describe("StepVerify 라운드 번호", () => {
  let container: HTMLDivElement;
  let root: Root;

  function multiTask(draftCount: number): BlogTask {
    const base = taskWith([candidate(1, 1), candidate(2, 1), candidate(3, 1)]);
    return { ...base, input: { ...base.input, draftCount } } as BlogTask;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.request.mockResolvedValue(
      taskWith([candidate(1, 1), candidate(2, 1), candidate(3, 1)]),
    );
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function render(draftCount: number) {
    mocks.store.task = multiTask(draftCount);
    await act(async () => {
      root.render(
        <StepVerify onReanalyze={vi.fn()} onBackToTitle={vi.fn()} analyzing={false} />,
      );
    });
  }

  it("제목만 담긴 라운드는 아직 1편째다", async () => {
    // ②가 1편째 제목을 담았을 뿐 방향은 안 골랐다 — 배열 길이는 1이지만 1편째다.
    mocks.store.draftRounds = [{ title: "1편 제목", keywords: ["가"] }];
    await render(3);

    expect(container.textContent).toContain("1번째 글");
    expect(container.textContent).not.toContain("2번째 글");
  });

  it("방향까지 고른 라운드가 하나면 2편째다", async () => {
    mocks.store.draftRounds = [
      { title: "1편 제목", keywords: ["가"], intentId: "intent_1" },
      { title: "2편 제목", keywords: ["나"] },
    ];
    await render(3);

    expect(container.textContent).toContain("2번째 글");
  });

  it("다음 편으로 넘어갈 때 앞 편의 트렌드 선택을 화면에서 비운다", async () => {
    // 2026-08-12 사용자 신고: 3편째 키워드 단계에서 '트렌드 없이 소재만으로'를 눌렀더니
    // "선택한 트렌드 키워드 'AIONA 마블'가 해제됩니다"라고 물었다 — 2편째에 고른 것이다.
    mocks.store.draftRounds = [{ title: "1편 제목", keywords: ["가"], intentId: "intent_1" }];
    await render(3);

    await act(async () => {
      container.querySelector<HTMLButtonElement>(".prewriting-confirm")!.click();
    });

    expect(mocks.store.setSelectedTrendKeywordIds).toHaveBeenCalledWith([], true);
    expect(mocks.store.setTopicCandidates).toHaveBeenCalledWith([], "");
    expect(mocks.store.setStep).toHaveBeenCalledWith(WRITE_STEP.TITLE);
  });

  it("화면을 비워도 앞 편의 기록은 지우지 않는다", async () => {
    // 2026-08-12 사용자 지시: "두번째편에서 선택한다고해서 첫번째 내용이 삭제되면 안되는거야".
    mocks.store.draftRounds = [{ title: "1편 제목", keywords: ["가"], intentId: "intent_1" }];
    await render(3);

    await act(async () => {
      container.querySelector<HTMLButtonElement>(".prewriting-confirm")!.click();
    });

    const saved = mocks.store.setDraftRounds.mock.calls.at(-1)?.[0];
    expect(saved[0]).toEqual({ title: "1편 제목", keywords: ["가"], intentId: "intent_1" });
    expect(saved[1].intentId).toBe("intent_1");
  });

  it("고른 방향을 통째로 보낸다", async () => {
    // 2026-08-12 사용자 신고: "3번째 편에 대해서 글 방향 선택하고 다음방향 가니까
    // 순간에러가 났어" (POST /schedule 400). intentId는 자리번호라 편마다 다시 매겨진다 —
    // 서버가 앞 편이 무엇을 골랐는지 되찾으려면 방향 자체가 있어야 한다.
    mocks.store.draftRounds = [
      {
        title: "1편 제목",
        keywords: ["가"],
        intentId: "intent_1",
        intent: { intentId: "intent_1", title: "1편이 고른 방향" } as never,
      },
      { title: "2편 제목", keywords: ["나"] },
    ];
    await render(2);

    await act(async () => {
      container.querySelector<HTMLButtonElement>(".prewriting-confirm")!.click();
    });

    const [, options] = mocks.request.mock.calls.find(([path]) =>
      String(path).endsWith("/schedule"),
    )!;
    expect(options.body.primaryDraft.intent).toEqual({
      intentId: "intent_1",
      title: "1편이 고른 방향",
    });
    // 마지막 편은 이 화면에서 방금 고른 것이다 — 그 방향도 함께 실린다.
    expect(options.body.additionalDrafts[0].intent.intentId).toBe("intent_1");
    expect(options.body.additionalDrafts[0].intent.title).toBe("방향 제목 1");
  });

  it("마지막 편은 방향을 고르면 작업 큐로 간다", async () => {
    mocks.store.draftRounds = [
      { title: "1편 제목", keywords: ["가"], intentId: "intent_1" },
      { title: "2편 제목", keywords: ["나"] },
    ];
    await render(2);

    await act(async () => {
      container.querySelector<HTMLButtonElement>(".prewriting-confirm")!.click();
    });

    const call = mocks.request.mock.calls.find(([path]) =>
      String(path).endsWith("/schedule"),
    );
    expect(call).toBeTruthy();
  });
});

/**
 * **자료를 지금 보여 주는가**(2026-08-13 사용자 지적: "수집한 자료가 보여져야지").
 *
 * 가르는 것은 작업 시각을 정했는가 하나다. 편수 조건은 걷어냈다 — 시각을 정하지 않은
 * 여러 편은 곧바로 함께 돌아, 여기서 모은 자료가 그대로 원고에 쓰인다.
 *
 * 서버의 `_collects_sources_now`와 **같은 규칙이어야 한다.**
 */
describe("StepVerify 자료를 언제 모으는가", () => {
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

  async function render(extra: Partial<BlogTask["input"]>) {
    const base = taskWith([candidate(1, 2)]);
    mocks.store.task = { ...base, input: { ...base.input, ...extra } } as BlogTask;
    await act(async () => {
      root.render(
        <StepVerify onReanalyze={vi.fn()} onBackToTitle={vi.fn()} analyzing={false} />,
      );
    });
  }

  function text(): string {
    return container.textContent ?? "";
  }

  it("여러 편이어도 시각을 정하지 않았으면 모은 자료를 보여 준다", async () => {
    await render({ draftCount: 3 });

    expect(text()).not.toContain("자료가 함께 수집됩니다");
    expect(text()).toContain("선택한 자료를 바탕으로 원고를 생성합니다");
    expect(text()).toContain("자료 제목 1-0");
  });

  it("한 편도 예전 그대로 보여 준다", async () => {
    await render({});

    expect(text()).toContain("자료 제목 1-0");
  });

  it("시각을 정한 글만 나중에 모은다고 말한다", async () => {
    await render({ scheduledRunAt: "2026-08-20T09:00:00.000Z" });

    expect(text()).toContain("작업 예정 시각에 자료가 함께 수집됩니다.");
    expect(text()).toContain("고른 방향으로 원고를 생성합니다");
    expect(text()).not.toContain("자료 제목 1-0");
  });
});
