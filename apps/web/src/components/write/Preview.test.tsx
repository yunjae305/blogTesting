import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BlogTask } from "../../api/types";

/**
 * 3단계에서 원고가 나오기 전까지 이 패널은 '앞으로 여기'라는 자리표시고, 원고가 나오면
 * 같은 자리에서 실제 글로 바뀐다. 가짜 본문을 미리 그리지 않는지 확인한다.
 */
const mocks = vi.hoisted(() => ({
  store: {
    session: { accessToken: "token" },
    task: null as BlogTask | null,
    setTask: vi.fn(),
    showToast: vi.fn(),
    reportError: vi.fn(),
  },
}));

vi.mock("../../store", () => ({ useStore: () => mocks.store }));
vi.mock("../../api/client", () => ({
  ApiError: class ApiError extends Error {},
  requestWithSessionToken: vi.fn(),
}));
// 편집기는 읽기 모드에서 렌더되지 않지만, import만으로 무거운 의존성을 끌고 온다.
vi.mock("./DraftEditor", () => ({ DraftEditor: () => null }));

import { Preview } from "./Preview";

describe("Preview", () => {
  let container: HTMLDivElement;
  let root: Root;

  async function render() {
    await act(async () => {
      root.render(<Preview />);
    });
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = null;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("원고가 없으면 자리표시만 두고 가짜 본문을 그리지 않는다", async () => {
    mocks.store.task = { postId: "post_1", status: "GENERATING" } as BlogTask;
    await render();

    expect(container.querySelector(".preview-coming-badge")?.textContent).toBe("COMING SOON");
    expect(container.querySelector(".preview-panel")?.className).toContain("is-awaiting");
    expect(container.querySelector(".preview-empty")?.textContent).toContain(
      "여기에 원고가 표시됩니다",
    );
    expect(container.textContent).toContain("모든 단계를 완료하면 실제 원고를 미리 볼 수 있어요");
    // 원고 본문 자리는 비어 있어야 한다.
    expect(container.querySelector(".preview-article-header")).toBeNull();
  });

  it("원고가 생기면 같은 자리에서 실제 글로 바뀐다", async () => {
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: "<p>본문</p>",
        markdownContent: "본문 문단입니다.",
        hashtags: ["태그"],
        images: [],
      },
    } as unknown as BlogTask;
    await render();

    expect(container.querySelector(".preview-coming-badge")).toBeNull();
    expect(container.querySelector(".preview-empty")).toBeNull();
    expect(container.querySelector(".preview-panel")?.className).not.toContain("is-awaiting");
    expect(container.querySelector(".preview-article-header h2")?.textContent).toBe("완성된 제목");
  });

  it("AI가 그린 사진에만 'AI이미지'를 붙인다", async () => {
    // 2026-08-05 사용자 요청. 코드로 그린 도표(rendered)·웹 사진(web)·사용자 업로드
    // (reference)는 AI 생성물이 아니므로 붙이지 않는다.
    const madeByAi = "data:image/png;base64,AI";
    const drawnByCode = "data:image/png;base64,CHART";
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: "<p>본문</p>",
        // 본문에 이미지 자리가 없으면 orphanImages 경로로 그려진다.
        markdownContent: "본문 문단입니다.",
        hashtags: [],
        images: [
          { dataUrl: madeByAi, altText: "AI 사진", source: "generated" },
          { dataUrl: drawnByCode, altText: "차트", source: "rendered" },
        ],
      },
    } as unknown as BlogTask;
    await render();

    const badges = [...container.querySelectorAll(".preview-ai-badge")];
    expect(badges).toHaveLength(1);
    expect(badges[0].textContent).toBe("AI이미지");

    // 배지가 붙은 figure가 AI 사진 쪽이어야 한다.
    const withBadge = badges[0].closest("figure");
    expect(withBadge?.querySelector("img")?.getAttribute("src")).toBe(madeByAi);
  });

  it("본문 사진의 출처를 캡션 아래 한 줄로 붙인다", async () => {
    // 2026-08-11. 캡션 문자열만으로는 원문 페이지로 갈 수 없다 — 구조화된 출처를
    // 그 사진 바로 아래에 붙여 링크·이용 조건까지 확인할 수 있게 한다.
    const photo = "data:image/png;base64,PHOTO";
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: "<p>본문</p>",
        markdownContent: `앞 문단입니다.\n\n![기사 사진](${photo})\n*출처: 연합뉴스*\n\n뒤 문단입니다.`,
        hashtags: [],
        images: [
          {
            dataUrl: photo,
            altText: "기사 사진",
            source: "web",
            imageSource: {
              sourceType: "external",
              sourceName: "연합뉴스",
              sourcePageUrl: "https://www.yna.co.kr/view/AKR1",
              usageStatus: "unknown",
            },
          },
        ],
      },
    } as unknown as BlogTask;
    await render();

    // 캡션 → 출처 줄 순서로, 그림 바로 아래에 한 묶음으로 놓인다.
    const caption = container.querySelector(".visual-caption");
    expect(caption?.textContent).toBe("출처: 연합뉴스");
    expect(caption?.nextElementSibling?.className).toContain("image-source-note");
    expect(container.querySelector(".image-source-name")?.textContent).toBe("연합뉴스");
    expect(
      container.querySelector<HTMLAnchorElement>(".image-source-link")?.getAttribute("href"),
    ).toBe("https://www.yna.co.kr/view/AKR1");
    // 출처 줄이 두 번 그려지면 안 된다(캡션이 있을 때는 캡션 아래에만).
    expect(container.querySelectorAll(".image-source-note")).toHaveLength(1);
  });

  it("캡션이 없는 사진에는 그림 바로 아래에 출처 줄을 붙인다", async () => {
    const photo = "data:image/png;base64,NOCAPTION";
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: "<p>본문</p>",
        markdownContent: `![사진](${photo})\n\n본문 문단입니다.`,
        hashtags: [],
        images: [
          {
            dataUrl: photo,
            altText: "사진",
            source: "web",
            imageSource: {
              sourceType: "external",
              sourceName: "example.com",
              usageStatus: "unknown",
            },
          },
        ],
      },
    } as unknown as BlogTask;
    await render();

    const figure = container.querySelector("figure");
    expect(figure?.nextElementSibling?.className).toContain("image-source-note");
    expect(container.querySelector(".image-source-usage")?.textContent).toBe("이용 조건 미확인");
  });

  it("CASE 6: 출처 필드가 없는 옛 게시글도 그대로 열린다", async () => {
    // 기존 DB 문서에는 imageSource가 없다. 강제 migration 없이 예전처럼 그려져야 한다.
    const photo = "data:image/png;base64,OLD";
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "옛 게시글",
        htmlContent: "<p>본문</p>",
        markdownContent: `![옛 사진](${photo})\n*출처: old.example*\n\n본문 문단입니다.`,
        hashtags: [],
        images: [{ dataUrl: photo, altText: "옛 사진" }],
      },
    } as unknown as BlogTask;
    await render();

    expect(container.querySelector(".preview-article-header h2")?.textContent).toBe("옛 게시글");
    expect(container.querySelector("figure img")?.getAttribute("src")).toBe(photo);
    // 캡션은 예전 그대로 보이고, 없는 출처 줄을 만들어 넣지 않는다.
    expect(container.querySelector(".visual-caption")?.textContent).toBe("출처: old.example");
    expect(container.querySelector(".image-source-note")).toBeNull();
  });

  it("검수 결과를 요약만 보여주고 판정 원문은 내지 않는다", async () => {
    // 사용자에게 내부 LLM 프롬프트나 원시 JSON을 그대로 노출하지 않는다(2026-08-05 스펙).
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: "<p>본문</p>",
        markdownContent: "본문 문단입니다.",
        hashtags: [],
        images: [],
      },
      draftGenerationResult: {
        finalReview: {
          reviewedAt: "2026-08-05T00:00:00.000Z",
          rounds: 2,
          overallStatus: "warning",
          overallScore: 88,
          checks: {
            factualUncertainty: {
              status: "fail",
              reason: "가격 근거가 자료에 없습니다",
              affectedSections: ["section-2"],
            },
          },
          issues: [{ kind: "unsupported", severity: "critical", reason: "근거 없음" }],
          revisionTargets: [],
          applied: 3,
          removedImages: 1,
        },
      },
    } as unknown as BlogTask;
    await render();

    const notes = [...container.querySelectorAll(".preview-review-note")].map(
      (node) => node.textContent,
    );
    // '품질 검수 완료'는 넣지 않는다 — 아무 행동도 요구하지 않는 표시다.
    expect(notes).toEqual([
      "일부 표현 자동 수정 3건",
      "관련 없는 이미지 1장 제외",
      "확인이 필요한 표현 1건",
    ]);
    // 판정 근거·항목 키·점수 같은 내부 값은 화면에 나가지 않는다.
    expect(container.textContent).not.toContain("가격 근거가 자료에 없습니다");
    expect(container.textContent).not.toContain("factualUncertainty");
    expect(container.textContent).not.toContain("88");
  });

  it("문제가 하나도 없으면 검수 칸이 아예 안 나온다", async () => {
    // '품질 검수 완료'만 남던 자리다. 아무 행동도 요구하지 않는 표시라 뺐고,
    // 그러면 남는 항목이 없어 칸 자체가 그려지지 않아야 한다(2026-08-06 사용자 요청).
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: "<p>본문</p>",
        markdownContent: "본문 문단입니다.",
        hashtags: [],
        images: [],
      },
      draftGenerationResult: {
        finalReview: {
          reviewedAt: "2026-08-05T00:00:00.000Z",
          rounds: 1,
          overallStatus: "pass",
          overallScore: 95,
          checks: {},
          issues: [],
          revisionTargets: [],
          applied: 0,
          removedImages: 0,
        },
      },
    } as unknown as BlogTask;
    await render();

    expect(container.querySelector(".preview-review-summary")).toBeNull();
    expect(container.textContent).not.toContain("품질 검수 완료");
  });

  it("검수가 실패해도 배지를 띄우지 않는다 — 사용자가 할 수 있는 행동이 없다", async () => {
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: "<p>본문</p>",
        markdownContent: "본문 문단입니다.",
        hashtags: [],
        images: [],
      },
      draftGenerationResult: {
        finalReview: {
          reviewedAt: "2026-08-05T00:00:00.000Z",
          rounds: 1,
          overallStatus: "warning",
          overallScore: 0,
          checks: {},
          issues: [],
          revisionTargets: [],
          applied: 0,
          removedImages: 0,
          error: "RuntimeError: provider down",
        },
      },
    } as unknown as BlogTask;
    await render();

    // 2026-08-07 사용자 결정: 실패 배지도 그리지 않는다. 실패 사유는 저장 문서와
    // 서버 로그에 남고, 화면에는 행동을 요구하는 것만 남긴다.
    expect(container.querySelector(".preview-review-summary")).toBeNull();
    expect(container.textContent).not.toContain("품질 검수를 마치지 못했습니다");
    // 사용자에게 예외 이름을 그대로 보여주지 않는다.
    expect(container.textContent).not.toContain("RuntimeError");
  });

  it("검수 기록이 없는 옛 글에는 요약 줄이 아예 없다", async () => {
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: "<p>본문</p>",
        markdownContent: "본문 문단입니다.",
        hashtags: [],
        images: [],
      },
    } as unknown as BlogTask;
    await render();

    expect(container.querySelector(".preview-review-summary")).toBeNull();
  });

  it("발행·복사되는 HTML에는 AI이미지 표시가 들어가지 않는다", async () => {
    // 배지는 이 화면이 React로 그리는 것이고, 발행은 서버가 만든 htmlContent를 그대로
    // 쓴다. 그래서 표시가 발행물로 새어 나갈 길이 없다 — 사용자가 명시한 조건이다.
    const html = '<p>본문</p><figure><img src="data:image/png;base64,AI" /></figure>';
    mocks.store.task = {
      postId: "post_1",
      status: "READY_TO_PUBLISH",
      finalPost: {
        title: "완성된 제목",
        htmlContent: html,
        markdownContent: "본문 문단입니다.",
        hashtags: [],
        images: [{ dataUrl: "data:image/png;base64,AI", altText: "AI 사진", source: "generated" }],
      },
    } as unknown as BlogTask;
    await render();

    expect(container.querySelector(".preview-ai-badge")).not.toBeNull();
    expect(html).not.toContain("AI이미지");
  });
});
