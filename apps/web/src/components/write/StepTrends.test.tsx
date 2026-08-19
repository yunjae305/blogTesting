import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BlogTask, TrendMode, TrendRecommendation } from "../../api/types";

/**
 * 제목 단계는 디자인만 다시 그렸다. 화면이 바뀌어도 두 탭이 각자의 API를 그대로 부르고,
 * 키워드 선택 → 제목 추천 → 제목 저장 → 소재만으로 작성이 예전과 같은 요청을 보내는지
 * 확인한다 — 시안을 따라가다 기능이 섞이거나 사라지는 것을 막는 것이 이 파일의 목적이다.
 */
const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  store: {
    task: null as BlogTask | null,
    settings: null,
    recommendation: null as TrendRecommendation | null,
    setRecommendation: vi.fn(),
    // 보기 방식은 목록(recommendation)과 같은 자리, 즉 store가 들고 있다. 화면의 지역
    // 상태였을 때는 소재 단계에 다녀오면 탭만 최신순으로 되돌아갔다(2026-08-11 신고).
    // 여기서도 store가 원본이므로, 전환 뒤의 화면을 볼 때는 recommendation과 마찬가지로
    // 테스트가 값을 바꿔 다시 그린다.
    trendMode: "TRENDING" as TrendMode,
    setTrendMode: vi.fn(),
    setTopicCandidates: vi.fn(),
    selectedTrendKeywordIds: [] as string[],
    trendKeywordSelectionTouched: false,
    selectTrendKeyword: vi.fn(),
    setSelectedTrendKeywordIds: vi.fn(),
    setTask: vi.fn(),
    draftRounds: [] as {
      title?: string;
      keywords: string[];
      intentId?: string;
      intentTitle?: string;
    }[],
    setDraftRounds: vi.fn(),
    showToast: vi.fn(),
    reportError: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ request: mocks.request }));
vi.mock("../../store", () => ({
  useStore: () => mocks.store,
  // 실제 구현과 같은 규칙: 방향 확정 전(SEARCH_ANALYZING 포함)에는 제목을 고를 수 있다.
  canSelectTrendTopic: (task: BlogTask | null) =>
    task?.status === "REFERENCE_PROCESSING" ||
    (task?.status === "SEARCH_ANALYZING" && !task.selectedIntent),
}));

import { StepTrends } from "./StepTrends";

const TASK: BlogTask = {
  postId: "post_1",
  userId: "user_1",
  status: "REFERENCE_PROCESSING",
  version: 1,
  createdAt: "2026-07-29T00:00:00.000Z",
  updatedAt: "2026-07-29T00:00:00.000Z",
  statusHistory: [],
  input: {
    topic: "롯데리아 메뉴",
    purpose: ["정보 전달"],
    keywords: ["정보 전달"],
    referenceMaterials: [],
  },
  postingLogs: [],
};

function recommendation(overrides: Partial<TrendRecommendation> = {}): TrendRecommendation {
  return {
    postId: "post_1",
    mode: "TRENDING",
    trendKeywords: [
      {
        trendKeywordId: "trend_1",
        keyword: "폭염 쪽방촌",
        source: "GOOGLE_TRENDS",
        sources: ["GOOGLE_TRENDS", "NAVER_DATALAB"],
        rank: 1,
        score: 90,
        trendScore: 90,
        collectedAt: "2026-07-29T00:00:00.000Z",
      },
      {
        trendKeywordId: "trend_2",
        keyword: "다이소",
        source: "GOOGLE_TRENDS",
        rank: 2,
        score: 70,
        trendScore: 70,
        collectedAt: "2026-07-29T00:00:00.000Z",
      },
    ],
    topicCandidates: [],
    generatedAt: "2026-07-29T00:00:00.000Z",
    ...overrides,
  };
}

describe("StepTrends 제목 단계", () => {
  let container: HTMLDivElement;
  let root: Root;

  async function render(
    handlers: { onChosen?: () => Promise<void>; onReopenVerify?: () => void } = {},
  ) {
    await act(async () => {
      root.render(
        <StepTrends
          onChosen={handlers.onChosen ?? (async () => {})}
          onReopenVerify={handlers.onReopenVerify ?? (() => {})}
        />,
      );
    });
  }

  function click(selector: string) {
    const element = container.querySelector<HTMLElement>(selector);
    expect(element, `Missing ${selector}`).not.toBeNull();
    return act(async () => {
      element?.click();
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
    mocks.store.task = TASK;
    mocks.store.recommendation = null;
    mocks.store.trendMode = "TRENDING";
    mocks.store.selectedTrendKeywordIds = [];
    mocks.store.trendKeywordSelectionTouched = false;
    mocks.store.draftRounds = [];
    mocks.request.mockResolvedValue(recommendation());
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("최신순으로 열리며 그 모드의 후보를 수집한다", async () => {
    await render();

    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1/trends/recommend", {
      method: "POST",
      body: expect.objectContaining({ mode: "TRENDING", shuffle: false }),
    });
    expect(
      container.querySelector('[role="tab"][aria-selected="true"]')?.textContent,
    ).toContain("최신순");
  });

  it("소재 관련순으로 바꾸면 이전 결과를 비우고 그 모드로 다시 요청한다", async () => {
    mocks.store.recommendation = recommendation();
    // 응답을 붙잡아 두고 전환 직후의 화면(로딩 상태)을 확인한다.
    let settle: (value: TrendRecommendation) => void = () => {};
    mocks.request.mockImplementation(
      () => new Promise<TrendRecommendation>((resolve) => (settle = resolve)),
    );
    await render();

    await click('[role="tab"][id="title-trend-tab-MATERIAL_RELATED"]');

    // 이전 탭의 카드를 남겨 두거나 흐리게 보여주지 않는다.
    expect(mocks.store.setRecommendation).toHaveBeenCalledWith(null);
    // 고른 보기 방식은 화면이 아니라 store에 남는다 — 단계를 오가도 살아남는 자리다.
    expect(mocks.store.setTrendMode).toHaveBeenCalledWith("MATERIAL_RELATED");
    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1/trends/recommend", {
      method: "POST",
      body: expect.objectContaining({ mode: "MATERIAL_RELATED" }),
    });

    mocks.store.recommendation = null;
    mocks.store.trendMode = "MATERIAL_RELATED";
    await render();
    const loading = container.querySelector(".title-loading-state");
    expect(loading).not.toBeNull();
    expect(loading?.getAttribute("aria-live")).toBe("polite");
    expect(loading?.textContent).toContain("키워드 확인 및 수집 중");

    await act(async () => settle(recommendation({ mode: "MATERIAL_RELATED" })));
  });

  /**
   * 2026-08-11 사용자 신고 — 소재 관련순을 눌러 두고 소재 단계에 다녀오면, 탭만
   * '최신순'으로 되돌아가고 카드는 소재 관련순으로 모은 것이 그대로 남았다. 화면이
   * 다시 그려지면서 지역 상태였던 보기 방식만 초기화된 것이고, 사용자는 자기가 고른
   * 보기를 매번 다시 눌러야 했다.
   */
  it("소재 단계에 다녀와도 고른 보기 방식이 카드와 어긋나지 않는다", async () => {
    mocks.store.trendMode = "MATERIAL_RELATED";
    mocks.store.recommendation = recommendation({ mode: "MATERIAL_RELATED" });

    // 단계를 오가면 이 화면은 통째로 다시 만들어진다 — 새 root가 그 상황이다.
    await render();

    expect(
      container.querySelector('[role="tab"][aria-selected="true"]')?.textContent,
    ).toContain("소재 관련순");
    // 이미 목록이 있으므로 다시 모으지 않는다(고른 보기가 그대로 남아 있을 뿐이다).
    expect(mocks.request).not.toHaveBeenCalled();
  });

  it("목록이 비어 있으면 기본값이 아니라 고른 보기 방식으로 다시 모은다", async () => {
    // 소재를 고쳐 저장하면 옛 소재로 모은 목록은 버려진다(StepTopic). 그때 보기 방식까지
    // 최신순으로 되돌리면 사용자는 방금 고른 소재 관련순을 또 눌러야 한다.
    mocks.store.trendMode = "MATERIAL_RELATED";
    mocks.store.recommendation = null;

    await render();

    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1/trends/recommend", {
      method: "POST",
      body: expect.objectContaining({ mode: "MATERIAL_RELATED" }),
    });
  });

  it("키워드를 고르면 그 키워드로만 제목 추천을 요청한다", async () => {
    mocks.store.recommendation = recommendation();
    await render();

    await click(".title-keyword-grid button");
    expect(mocks.store.selectTrendKeyword).toHaveBeenCalledWith("trend_1");

    mocks.store.selectedTrendKeywordIds = ["trend_1"];
    mocks.store.trendKeywordSelectionTouched = true;
    mocks.request.mockResolvedValue({
      postId: "post_1",
      trendKeywordId: "trend_1",
      topicCandidates: [],
      generatedAt: "2026-07-29T00:00:00.000Z",
    });
    await render();

    // 선택은 칩으로 정리되고, 제목 후보 제목줄에는 소재가 그대로 남는다.
    expect(container.querySelector(".title-keyword-pill")?.textContent).toContain("폭염 쪽방촌");
    expect(container.querySelector(".title-candidate-heading h3")?.textContent).toBe(
      "'롯데리아 메뉴' 제목 후보",
    );

    await act(async () => buttonByText("제목 추천").click());
    expect(mocks.request).toHaveBeenLastCalledWith("/posts/post_1/trends/topics", {
      method: "POST",
      body: {
        trendKeywordId: "trend_1",
        keyword: "폭염 쪽방촌",
        source: "GOOGLE_TRENDS",
        excludeTitles: [],
        // 화면에 후보가 없는 첫 생성이라 되돌려 보낼 관점도 없고, 회차는 0이다.
        excludeAngles: [],
        regenerationCount: 0,
      },
    });
  });

  it("제목 추천을 다시 누르면 이전 후보의 관점과 회차를 함께 보낸다", async () => {
    // 제목 문자열만 보내면 모델이 같은 후킹·같은 유형으로 표현만 바꿔 온다. 재생성은 문장
    // 교체가 아니라 관점 이동이어야 하므로 hookType·titleType과 회차를 함께 보낸다.
    // 화면의 후보는 recommendation.topicCandidates에서 온다(store가 아니라).
    mocks.store.recommendation = {
      ...recommendation(),
      topicCandidates: [
        {
          topicCandidateId: "topic_1",
          title: "폭염 쪽방촌, 롯데리아 메뉴로 버티는 방법",
          description: "정보형",
          trendKeywordIds: ["trend_1"],
          recommended: true,
          hookType: "CURIOSITY",
        },
      ],
    };
    mocks.store.selectedTrendKeywordIds = ["trend_1"];
    mocks.store.trendKeywordSelectionTouched = true;
    await render();

    await act(async () => buttonByText("제목 추천").click());

    const body = mocks.request.mock.calls.at(-1)?.[1]?.body as Record<string, unknown>;
    expect(body.regenerationCount).toBe(1);
    expect(body.excludeAngles).toEqual([
      {
        title: "폭염 쪽방촌, 롯데리아 메뉴로 버티는 방법",
        hookType: "CURIOSITY",
        titleType: "정보형",
      },
    ]);
  });

  it("선택 칩의 ×는 기존 해제 경로로 선택과 제목을 함께 비운다", async () => {
    mocks.store.recommendation = recommendation();
    mocks.store.selectedTrendKeywordIds = ["trend_1"];
    mocks.store.trendKeywordSelectionTouched = true;
    await render();

    await click(".title-keyword-pill-remove");

    expect(mocks.store.setSelectedTrendKeywordIds).toHaveBeenCalledWith([], true);
    expect(mocks.store.setTopicCandidates).toHaveBeenCalledWith([], "");
  });

  it("제목 후보의 사용하기는 고른 제목을 저장한다", async () => {
    mocks.store.recommendation = recommendation({
      topicCandidates: [
        {
          topicCandidateId: "topic_1",
          title: "롯데리아 신메뉴 3종 솔직 후기",
          description: "",
          trendKeywordIds: ["trend_1"],
          recommended: true,
          reason: "루브릭 최고점",
          hookType: "CURIOSITY",
        },
      ],
    });
    mocks.store.selectedTrendKeywordIds = ["trend_1"];
    mocks.store.trendKeywordSelectionTouched = true;
    mocks.request.mockResolvedValue(TASK);
    await render();

    // 추천 배지는 서버가 recommended로 표시한 후보에만 붙는다.
    expect(container.querySelectorAll(".title-candidate-badge")).toHaveLength(1);

    await click(".title-candidate-item");

    expect(mocks.request).toHaveBeenLastCalledWith("/posts/post_1/trends/select", {
      method: "POST",
      body: {
        topicCandidateId: "topic_1",
        finalTopic: "롯데리아 신메뉴 3종 솔직 후기",
        selectedTrendKeywordIds: ["trend_1"],
        // 고른 키워드의 문자열도 함께 보낸다. 키워드 목록은 서버에 저장되지 않아,
        // 여기서 보내지 않으면 원본 검색어가 원고 단계까지 가지 못한다.
        selectedKeywords: ["폭염 쪽방촌"],
        skipped: false,
        hookType: "CURIOSITY",
      },
    });
  });

  it("트렌드 없이 소재만으로 작성이 그대로 남아 있다", async () => {
    mocks.store.recommendation = recommendation();
    mocks.request.mockResolvedValue(TASK);
    await render();

    await click("#skipTrend");

    expect(mocks.request).toHaveBeenLastCalledWith("/posts/post_1/trends/select", {
      method: "POST",
      body: { skipped: true, selectedTrendKeywordIds: [] },
    });
  });

  it("소재만으로 작성은 키워드·제목 화면에 남지 않고 곧장 검증으로 간다", async () => {
    // 2026-08-07 사용자 요청: 방금 '키워드 없이 가겠다'고 누른 사람에게 키워드·제목
    // 추천 UI를 계속 보여 주면 안 된다. 검증(1분)이 끝나기를 기다리지 않고 넘어간다.
    mocks.store.recommendation = recommendation();
    mocks.request.mockResolvedValue(TASK);
    const onReopenVerify = vi.fn();
    let resolveChosen: () => void = () => {};
    const onChosen = vi.fn(() => new Promise<void>((resolve) => (resolveChosen = resolve)));
    await render({ onChosen, onReopenVerify });

    const clicked = click("#skipTrend");
    resolveChosen();
    await clicked;

    expect(onReopenVerify).toHaveBeenCalledOnce();
    expect(onChosen).toHaveBeenCalledOnce();
  });

  it("제목을 이미 고른 뒤(SEARCH_ANALYZING)에도 다른 제목을 다시 고를 수 있다", async () => {
    // 예전에는 제목을 한 번 고르면 글이 SEARCH_ANALYZING으로 넘어가면서 후보 버튼이
    // 전부 잠겼다 — 검증 팝업의 '수정하기'로 돌아와도 바꿀 방법이 없었다.
    mocks.store.task = {
      ...TASK,
      status: "SEARCH_ANALYZING",
      trendSelection: {
        topicCandidateId: "topic_1",
        finalTopic: "첫 제목",
        selectedTrendKeywordIds: ["trend_1"],
        skipped: false,
        selectedAt: "2026-08-05T00:00:00.000Z",
      },
    };
    mocks.store.recommendation = recommendation({
      topicCandidates: [
        {
          topicCandidateId: "topic_2",
          title: "두 번째 제목",
          description: "",
          trendKeywordIds: ["trend_1"],
          recommended: false,
        },
      ],
    });
    mocks.store.selectedTrendKeywordIds = ["trend_1"];
    mocks.store.trendKeywordSelectionTouched = true;
    mocks.request.mockResolvedValue(TASK);
    await render();

    const candidate = container.querySelector<HTMLButtonElement>(".title-candidate-item");
    expect(candidate?.disabled).toBe(false);

    await click(".title-candidate-item");

    expect(mocks.request).toHaveBeenLastCalledWith("/posts/post_1/trends/select", {
      method: "POST",
      body: expect.objectContaining({
        topicCandidateId: "topic_2",
        finalTopic: "두 번째 제목",
      }),
    });
  });

  it("더 보기·다른 키워드 보기·새 키워드 찾기이 서로 다른 요청을 유지한다", async () => {
    mocks.store.recommendation = recommendation();
    await render();

    await act(async () => buttonByText("다른 키워드 보기").click());
    expect(mocks.request).toHaveBeenLastCalledWith("/posts/post_1/trends/recommend", {
      method: "POST",
      body: expect.objectContaining({ mode: "TRENDING", shuffle: true }),
    });

    await act(async () => buttonByText("새 키워드 찾기").click());
    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1/trends/recommend", {
      method: "POST",
      body: expect.objectContaining({ forceCollect: true }),
    });
  });
  it("제목을 고른 뒤에도 다음으로 가는 것은 사용자다", async () => {
    // 2026-08-07 사용자 요청 — 후보의 '사용하기'를 누른 것뿐인데 화면이 통째로 검증
    // 단계로 넘어가, 고른 제목을 확인할 틈도 다른 후보를 다시 볼 기회도 없었다.
    // 이제 저장만 하고 단계는 그대로 두며, 넘어가는 것은 이 버튼이다.
    mocks.store.task = { ...TASK, status: "SEARCH_ANALYZING" };
    const onReopenVerify = vi.fn();
    await render({ onReopenVerify });

    const cta = container.querySelector<HTMLButtonElement>(".title-primary-cta");
    expect(cta?.disabled).toBe(false);
    expect(cta?.textContent).toContain("작성 전 검증으로");

    await click(".title-primary-cta");

    expect(onReopenVerify).toHaveBeenCalledTimes(1);
  });

  it("제목을 아직 고르지 않았으면 다음 버튼이 눌리지 않는다", async () => {
    mocks.store.task = { ...TASK, status: "REFERENCE_PROCESSING" };
    await render();

    const cta = container.querySelector<HTMLButtonElement>(".title-primary-cta");
    expect(cta?.disabled).toBe(true);
  });

  it("카드는 대표 출처의 실제 수집 근거 3줄을 그리고, 보조 출처 로고만 아래에 남긴다", async () => {
    mocks.store.recommendation = recommendation({
      trendKeywords: [
        {
          trendKeywordId: "trend_1",
          keyword: "폭염 쪽방촌",
          source: "GOOGLE_TRENDS",
          sources: ["GOOGLE_TRENDS", "NAVER_DATALAB"],
          rank: 1,
          score: 90,
          trendScore: 90,
          collectedAt: "2026-07-29T00:00:00.000Z",
          evidenceBySource: {
            GOOGLE_TRENDS: {
              source: "GOOGLE_TRENDS",
              observedAt: new Date(Date.now() - 3 * 60_000).toISOString(),
              dataOrigin: "SERPAPI",
              google: {
                active: true,
                searchVolume: 80_000,
                increasePercentage: 320,
                startedAt: new Date(Date.now() - 3 * 3600_000).toISOString(),
              },
            },
            // 보조 출처의 근거도 함께 오지만, 카드의 3줄은 대표 출처 것만 쓴다.
            NAVER_DATALAB: {
              source: "NAVER_DATALAB",
              observedAt: new Date(Date.now() - 4 * 60_000).toISOString(),
              dataOrigin: "NAVER_SEARCH_API",
              naver: { recentNewsCount: 12, collectedBlogCount: 3, collectedRelatedContentCount: 20 },
            },
          },
        },
      ],
    });
    await render();

    const card = container.querySelector(".title-keyword-card")!;
    const rows = card.querySelectorAll(".title-evidence-row");
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("현재 급상승 중");
    expect(rows[1].textContent).toBe("8만+ 검색 · +320%");
    expect(rows[2].textContent).toContain("상승 시작");
    // 대표 출처 수치와 보조 출처 수치를 섞지 않는다 — 네이버 문서 수가 구글 카드에 없다.
    expect(card.textContent).not.toContain("뉴스 12건");
    // 대표 출처 로고는 지표 머리줄에 있고, 아래 보조 로고 줄에는 네이버만 남는다.
    const signals = card.querySelector(".title-keyword-signals")!;
    expect(signals.querySelector(".title-keyword-source--naver")).not.toBeNull();
    expect(signals.querySelector(".title-keyword-source--google")).toBeNull();
    // 카드는 여전히 클릭으로 선택하는 버튼이다.
    await click(".title-keyword-card");
    expect(mocks.store.selectTrendKeyword).toHaveBeenCalledWith("trend_1");
  });

  it("근거가 없는 옛 응답도 깨지지 않는다 — 가짜 0 없이 중립 문구와 추천 배지 유지", async () => {
    // 기본 recommendation() 픽스처에는 evidenceBySource가 없다(구버전 응답과 같은 모양).
    mocks.store.recommendation = recommendation();
    await render();

    const cards = container.querySelectorAll(".title-keyword-card");
    expect(cards).toHaveLength(2);
    for (const card of cards) {
      expect(card.textContent).toContain("상세 지표는 새 수집 후 표시됩니다");
      expect(card.textContent).not.toMatch(/0[%건개]/);
    }
    // 추천 배지는 기존 규칙 그대로 트렌드 점수 1위에 하나만 붙는다.
    expect(container.querySelectorAll(".title-keyword-reco")).toHaveLength(1);
    expect(cards[0].textContent).toContain("추천");
  });

  it("카드에 hover 툴팁을 두지 않는다 — 내부 점수를 띄우면 오히려 헷갈린다", async () => {
    mocks.store.recommendation = recommendation();
    await render();

    const cards = container.querySelectorAll<HTMLElement>(".title-keyword-card");
    expect(cards.length).toBeGreaterThan(0);
    for (const card of cards) {
      // '트렌드 점수 56 · 단일 출처'처럼 뜻을 알 수 없는 숫자가 떠 있던 자리.
      expect(card.getAttribute("title")).toBeNull();
    }
  });

  it("카드 목록 아래에 공통 안내와 출처별 측정 기준 툴팁이 있다", async () => {
    mocks.store.recommendation = recommendation();
    await render();

    const footnote = container.querySelector(".title-evidence-footnote");
    expect(footnote?.textContent).toContain("출처마다 측정 기준이 다릅니다");
    const info = footnote?.querySelector<HTMLElement>(".title-evidence-info");
    expect(info?.getAttribute("title")).toContain("Naver: 이번 검색 API 수집 표본");
    expect(info?.getAttribute("title")).toContain("Google: 급상승 검색량과 상승률");
    expect(info?.getAttribute("title")).toContain("YouTube: 영상 누적 조회수");
    // 근거가 없으면 수집 시각을 말하지 않는다. generatedAt은 저장분을 그대로 보여준
    // 응답에서도 늘 '방금'이라, 며칠 전 수집분을 "가장 최근 수집 방금 전"이라고 적게 된다.
    expect(footnote?.textContent).not.toContain("가장 최근 수집");
  });

  it("관측 시각이 있으면 그 시각으로 '가장 최근 수집'을 말한다", async () => {
    mocks.store.recommendation = recommendation({
      trendKeywords: [
        {
          trendKeywordId: "trend_1",
          keyword: "폭염",
          source: "NAVER_DATALAB",
          rank: 1,
          score: 90,
          collectedAt: "2026-07-29T00:00:00.000Z",
          evidenceBySource: {
            NAVER_DATALAB: {
              source: "NAVER_DATALAB",
              observedAt: new Date(Date.now() - 2 * 3600_000).toISOString(),
              dataOrigin: "NAVER_SEARCH_API",
              naver: {
                recentNewsCount: 184,
                collectedBlogCount: 63,
                collectedRelatedContentCount: 247,
              },
            },
          },
        },
      ],
    });
    await render();

    expect(container.querySelector(".title-evidence-footnote")?.textContent).toContain(
      "가장 최근 수집 약 2시간 전",
    );
  });
});

/**
 * 한 소재로 여러 편을 만들 때, 이 화면은 편수만큼 다시 밟는다.
 *
 * 2026-08-12 사용자 신고 — 3편째 키워드 단계에서 '트렌드 없이 소재만으로'를 눌렀더니
 * "선택한 트렌드 키워드 'AIONA 마블'가 해제됩니다"라고 물었다. 2편째에 고른 것이다.
 * 글 하나에는 트렌드 선택 자리가 하나뿐이라 서버의 trendSelection에는 앞 편의 것이 남는데,
 * 화면이 그것을 이 편의 선택으로 읽고 있었다.
 */
describe("StepTrends 여러 편 라운드", () => {
  let container: HTMLDivElement;
  let root: Root;

  /** 1편째를 끝내고 2편째 제목 단계에 막 들어선 상태. */
  function secondRoundTask(): BlogTask {
    return {
      ...TASK,
      status: "SEARCH_ANALYZING",
      input: { ...TASK.input, draftCount: 2 },
      trendSelection: {
        finalTopic: "1편째로 고른 제목",
        selectedTrendKeywordIds: ["trend_1"],
        selectedKeywords: ["폭염 쪽방촌"],
        skipped: false,
        selectedAt: "2026-07-29T00:01:00.000Z",
      },
    } as BlogTask;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = secondRoundTask();
    mocks.store.recommendation = recommendation();
    mocks.store.trendMode = "TRENDING";
    mocks.store.selectedTrendKeywordIds = [];
    mocks.store.trendKeywordSelectionTouched = false;
    mocks.store.draftRounds = [
      { title: "1편째로 고른 제목", keywords: ["폭염 쪽방촌"], intentId: "intent_1" },
    ];
    mocks.request.mockResolvedValue(recommendation());
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function render() {
    await act(async () => {
      root.render(<StepTrends onChosen={async () => {}} onReopenVerify={() => {}} />);
    });
  }

  it("앞 편에서 고른 키워드가 이 편에서 '선택됨'으로 남지 않는다", async () => {
    await render();

    expect(container.querySelector(".title-keyword-card--selected")).toBeNull();
    // 해제 확인 팝업은 '선택한 키워드'가 있을 때만 뜬다 — 그 칩 자체가 없어야 한다.
    expect(container.querySelector(".title-selected-keywords")).toBeNull();
  });

  it("앞 편의 제목을 이 편의 '선택한 제목'으로 보여 주지 않는다", async () => {
    await render();

    const chosen = container.querySelector(".title-selected-result");
    expect(chosen?.textContent).toContain("선택 없음");
    expect(chosen?.textContent).not.toContain("1편째로 고른 제목");
  });

  it("이 편을 고르기 전에는 다음 단계로 넘어갈 수 없다", async () => {
    // 상태(SEARCH_ANALYZING)는 앞 편에서 이미 올라가 있다. 그것만 보면 아무것도 고르지
    // 않은 편에서도 검증으로 넘어갈 수 있었다.
    await render();

    const cta = container.querySelector<HTMLButtonElement>(".title-primary-cta");
    expect(cta?.disabled).toBe(true);
    expect(cta?.textContent).toContain("제목을 먼저 고르세요");
  });

  it("앞 편에서 고른 것은 화면에 그대로 남아 있다", async () => {
    // 2026-08-12 사용자 지시: "두번째편에서 선택한다고해서 첫번째 내용이 삭제되면 안되는거야".
    await render();

    const note = container.querySelector(".title-round-note");
    expect(note?.textContent).toContain("2편 중 2편째");
    expect(note?.textContent).toContain("원고1");
    expect(note?.textContent).toContain("폭염 쪽방촌");
    expect(note?.textContent).toContain("1편째로 고른 제목");
  });

  it("이 편의 제목을 고르고 나면 다음 단계가 열린다", async () => {
    mocks.store.draftRounds = [
      { title: "1편째로 고른 제목", keywords: ["폭염 쪽방촌"], intentId: "intent_1" },
      { title: "2편째로 고른 제목", keywords: ["다이소"] },
    ];
    await render();

    const cta = container.querySelector<HTMLButtonElement>(".title-primary-cta");
    expect(cta?.disabled).toBe(false);
    expect(cta?.textContent).toContain("작성 전 검증으로");
  });
});
