import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BlogTask } from "../../api/types";
import {
  MAX_DRAFT_COUNT,
  SAMPLE_INPUT,
  SUBJECT_CATEGORIES,
  WRITING_PURPOSES,
  READER_AGE_RANGES,
} from "../../constants";
import { DRAFT_STEP_SECONDS } from "../../draftProgress";
import {
  MAX_REFERENCE_FILE_BYTES,
  MAX_REFERENCE_FILES_BYTES,
} from "../../utils";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  collectReferenceMaterials: vi.fn(),
  store: {
    task: null as BlogTask | null,
    setTask: vi.fn(),
    setStep: vi.fn(),
    setRecommendation: vi.fn(),
    showToast: vi.fn(),
    reportError: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ request: mocks.request }));
vi.mock("../../store", () => ({ useStore: () => mocks.store }));
// URL 판정은 흉내 내지 않고 **진짜를 쓴다.** 화면이 실제로 막는지가 시험 대상이라,
// 여기서 가짜로 바꾸면 무엇을 통과시키는지 알 수 없다.
vi.mock("../../utils", async () => ({
  ...(await vi.importActual<typeof import("../../utils")>("../../utils")),
  collectReferenceMaterials: mocks.collectReferenceMaterials,
}));

import { StepTopic } from "./StepTopic";

describe("StepTopic writing brief", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = null;
    mocks.collectReferenceMaterials.mockResolvedValue([]);
    mocks.request.mockResolvedValue({
      postId: "post_1",
      userId: "user_1",
      status: "INPUT",
      version: 1,
      createdAt: "2026-07-29T00:00:00.000Z",
      updatedAt: "2026-07-29T00:00:00.000Z",
      statusHistory: [],
      input: {
        topic: SAMPLE_INPUT.topic,
        purpose: [SAMPLE_INPUT.purpose],
        keywords: [SAMPLE_INPUT.purpose],
        referenceMaterials: [],
      },
      postingLogs: [],
    } satisfies BlogTask);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });


  /** 실제 입력처럼 값과 change 이벤트를 함께 보낸다(React 제어 컴포넌트). */
  async function type(input: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )?.set;
    await act(async () => {
      setter?.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("keeps the existing controls and saves the sample through the original flow", async () => {
    const onPreviewChange = vi.fn();
    await act(async () => {
      root.render(<StepTopic onPreviewChange={onPreviewChange} />);
    });

    // 소재·목적 / 대상 연령 / 카테고리 / 참고 자료 — 카테고리가 2026-08-11에 늘었다.
    expect(container.querySelectorAll(".brief-section")).toHaveLength(4);
    // 프리셋 8개 + 직접 기입(기타) 카드 1개가 같은 그리드에 있다.
    expect(container.querySelectorAll("[data-purpose]")).toHaveLength(
      WRITING_PURPOSES.length + 1,
    );
    expect(container.querySelector('[data-purpose="입문·소개"]')).not.toBeNull();
    expect(
      container.querySelector('[data-purpose="기타"] input#customPurpose'),
    ).not.toBeNull();
    expect(container.querySelectorAll("[data-reader-age-range]")).toHaveLength(
      READER_AGE_RANGES.length,
    );

    await act(async () => {
      container.querySelector<HTMLButtonElement>("#fillSampleInput")?.click();
    });

    expect(container.querySelector<HTMLInputElement>("#topic")?.value).toBe(
      SAMPLE_INPUT.topic,
    );
    expect(
      container
        .querySelector(`[data-purpose="${SAMPLE_INPUT.purpose}"]`)
        ?.getAttribute("aria-checked"),
    ).toBe("true");
    expect(
      container
        .querySelector(`[data-reader-age-range="${SAMPLE_INPUT.readerAgeRange}"]`)
        ?.getAttribute("aria-checked"),
    ).toBe("true");
    expect(onPreviewChange).toHaveBeenLastCalledWith({
      topic: SAMPLE_INPUT.topic,
      purpose: SAMPLE_INPUT.purpose,
      readerAgeRange: SAMPLE_INPUT.readerAgeRange,
      subjectCategory: SAMPLE_INPUT.subjectCategory,
    });

    await act(async () => {
      container
        .querySelector<HTMLFormElement>("#topicForm")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    // 왼쪽 입력은 오른쪽 목록에 추가한 뒤에만 저장 대상이 된다.
    expect(mocks.collectReferenceMaterials).toHaveBeenCalledWith({
      text: [],
      url: [],
      files: [],
      existingCount: 0,
    });
    expect(mocks.request).toHaveBeenCalledWith("/posts", {
      method: "POST",
      body: {
        topic: SAMPLE_INPUT.topic,
        purpose: [SAMPLE_INPUT.purpose],
        keywords: [SAMPLE_INPUT.purpose],
        subjectCategory: SAMPLE_INPUT.subjectCategory,
        // 브랜드를 고르지 않았으면 아예 보내지 않는다.
        brandId: undefined,
        referenceMaterials: [],
      },
    });
    expect(mocks.store.setTask).toHaveBeenCalledOnce();
    expect(mocks.store.setStep).toHaveBeenCalledWith(1);
  });

  it("typing in 기타 selects it over presets, and picking a preset clears it", async () => {
    const onPreviewChange = vi.fn();
    await act(async () => {
      root.render(<StepTopic onPreviewChange={onPreviewChange} />);
    });

    await act(async () => {
      const node = container.querySelector<HTMLInputElement>("#customPurpose");
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")
        ?.set?.call(node, "채용 브랜딩 콘텐츠");
      node?.dispatchEvent(new Event("input", { bubbles: true }));
    });

    const custom = container.querySelector('[data-purpose="기타"]');
    expect(custom?.getAttribute("aria-checked")).toBe("true");
    for (const item of WRITING_PURPOSES) {
      expect(
        container.querySelector(`[data-purpose="${item}"]`)?.getAttribute("aria-checked"),
      ).toBe("false");
    }
    expect(onPreviewChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ purpose: "채용 브랜딩 콘텐츠" }),
    );

    await act(async () => {
      container
        .querySelector<HTMLButtonElement>(`[data-purpose="${WRITING_PURPOSES[0]}"]`)
        ?.click();
    });

    expect(container.querySelector<HTMLInputElement>("#customPurpose")?.value).toBe("");
    expect(custom?.getAttribute("aria-checked")).toBe("false");
    expect(
      container
        .querySelector(`[data-purpose="${WRITING_PURPOSES[0]}"]`)
        ?.getAttribute("aria-checked"),
    ).toBe("true");
  });

  it("주소가 올바르지 않으면 그 칸에 이유를 적고 다음으로 못 넘어간다", async () => {
    // 예전에는 제출한 뒤에야 알려 줬고, 어느 칸이 문제인지도 말해 주지 않았다
    // (2026-08-07 사용자 요청).
    await act(async () => {
      root.render(<StepTopic />);
    });

    await act(async () => {
      container.querySelector<HTMLButtonElement>("#reference-tab-url")?.click();
    });
    const url = container.querySelector<HTMLInputElement>("#referenceUrl")!;
    await type(url, "example.com");

    const problem = container.querySelector(".field-error");
    expect(problem?.textContent).toContain("http");
    // 색만으로 알리지 않는다 — 칸과 설명이 이어져 있어야 읽어 주는 쪽에도 전달된다.
    expect(url.getAttribute("aria-invalid")).toBe("true");
    expect(url.getAttribute("aria-describedby")).toBe(problem?.id);

    const add = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
      (button) => button.textContent?.trim() === "URL 추가",
    )!;
    expect(add.disabled).toBe(true);

    // 고치면 추가 버튼이 열리고, 목록에 담은 뒤 원고 저장도 다시 열린다.
    await type(url, "https://example.com");
    expect(container.querySelector(".field-error")).toBeNull();
    expect(add.disabled).toBe(false);
    await act(async () => add.click());
    const submit = container.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    expect(submit.disabled).toBe(false);
  });

  it("참고 URL을 여러 개 추가해 통합 목록으로 보낸다", async () => {
    await act(async () => {
      root.render(<StepTopic />);
    });

    await act(async () => {
      container.querySelector<HTMLButtonElement>("#reference-tab-url")?.click();
    });
    const url = container.querySelector<HTMLInputElement>("#referenceUrl")!;
    const add = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
      (button) => button.textContent?.trim() === "URL 추가",
    )!;

    await type(url, "https://example.com/a");
    await act(async () => add.click());
    expect(container.querySelector("#referenceUrl")?.getAttribute("value")).toBe("");
    expect(container.querySelectorAll(".brief-reference-list > li")).toHaveLength(1);

    await type(url, "https://example.com/b");
    await act(async () => add.click());
    expect(container.querySelectorAll(".brief-reference-list > li")).toHaveLength(2);
    expect(container.querySelector(".brief-reference-collection-head")?.textContent).toContain(
      "2개",
    );

    // 목적·대상 연령은 필수라 채우지 않으면 제출이 막힌다.
    await act(async () => {
      container.querySelector<HTMLButtonElement>("#fillSampleInput")?.click();
    });
    await act(async () => {
      container
        .querySelector<HTMLFormElement>("#topicForm")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(mocks.collectReferenceMaterials).toHaveBeenCalledWith(
      expect.objectContaining({
        url: ["https://example.com/a", "https://example.com/b"],
      }),
    );
  });

  it("추가한 URL을 목록에서 삭제할 수 있다", async () => {
    await act(async () => {
      root.render(<StepTopic />);
    });

    await act(async () => {
      container.querySelector<HTMLButtonElement>("#reference-tab-url")?.click();
    });
    const url = container.querySelector<HTMLInputElement>("#referenceUrl")!;
    await type(url, "https://example.com/a");
    await act(async () => {
      [...container.querySelectorAll<HTMLButtonElement>("button")]
        .find((button) => button.textContent?.trim() === "URL 추가")
        ?.click();
    });
    expect(container.querySelectorAll(".brief-reference-list > li")).toHaveLength(1);

    await act(async () => {
      container.querySelector<HTMLButtonElement>(".brief-reference-remove")?.click();
    });

    expect(container.querySelector(".brief-reference-list")).toBeNull();
    expect(container.querySelector(".brief-reference-empty")?.textContent).toContain(
      "아직 추가된 참고자료가 없습니다.",
    );
  });

  it("저장된 글을 다시 열면 저장돼 있던 URL이 모두 보인다", async () => {
    mocks.store.task = {
      postId: "post_1",
      input: {
        topic: "소재",
        purpose: ["정보 전달"],
        keywords: ["정보 전달"],
        referenceMaterials: [
          { type: "URL", value: "https://example.com/a" },
          { type: "URL", value: "https://example.com/b" },
        ],
      },
    } as unknown as BlogTask;

    await act(async () => {
      root.render(<StepTopic />);
    });

    const values = [...container.querySelectorAll(".brief-reference-detail")].map(
      (item) => item.textContent,
    );
    expect(values).toEqual(["https://example.com/a", "https://example.com/b"]);
    expect(container.querySelector(".brief-reference-collection-head")?.textContent).toContain(
      "2개",
    );
  });
});


describe("StepTopic 참고 자료 칸", () => {
  let container: HTMLDivElement;
  let root: Root;

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

  async function render() {
    await act(async () => root.render(<StepTopic />));
  }

  async function typeMemo(value: string) {
    const input = container.querySelector<HTMLTextAreaElement>("#referenceText")!;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )?.set;
    await act(async () => {
      setter?.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  it("메모·URL·파일 탭과 통합 목록을 보여준다", async () => {
    await render();

    const tabs = [...container.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
    expect(tabs.map((tab) => tab.textContent?.trim())).toEqual(["메모", "URL", "파일 업로드"]);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(container.querySelector(".brief-reference-collection-head")?.textContent).toContain(
      "0개",
    );
    expect(container.querySelector(".brief-reference-textarea > span")?.textContent).toBe(
      "0/1,000자",
    );
    // 파일 칸은 브랜드 자료 편집과 같은 것을 쓴다(2026-08-07 통일) — 두 화면이 각자
    // 그리면 같은 일을 하는 자리가 서로 다르게 보인다.
    const drop = container.querySelector("#reference-panel-file .brand-dropzone");
    expect(drop?.textContent).toContain("파일을 드래그하거나 선택하여 업로드");
    expect(drop?.querySelector("#referenceFiles")).not.toBeNull();
  });

  it("작성한 메모는 추가 버튼을 눌러 목록에 담는다", async () => {
    await render();
    await typeMemo("지난달 업데이트 내용을 강조해 주세요.");

    await act(async () => {
      [...container.querySelectorAll<HTMLButtonElement>("button")]
        .find((button) => button.textContent?.trim() === "메모 추가")
        ?.click();
    });

    expect(container.querySelector<HTMLTextAreaElement>("#referenceText")?.value).toBe("");
    expect(container.querySelector(".brief-reference-kind")?.textContent).toContain("메모");
    expect(container.querySelector(".brief-reference-detail")?.textContent).toBe(
      "지난달 업데이트 내용을 강조해 주세요.",
    );
    expect(container.querySelector(".brief-reference-collection-head")?.textContent).toContain(
      "1개",
    );
  });
  /**
   * 입력칸 3개를 위해 글자가 15줄 넘게 깔려 있었다 — 칸마다 '선택' 배지와 설명이
   * 붙고, 제한 문구가 세 군데에 흩어져 있었다(2026-08-06 사용자 지적).
   */
  it("칸마다 붙던 설명과 배지가 없다", async () => {
    await render();

    // 설명 문구는 입력칸 안의 예시가 대신한다.
    expect(container.textContent).not.toContain("원고에 꼭 포함할 내용을 자유롭게");
    expect(container.textContent).not.toContain("사실 확인에 쓸 파일이나 이미지입니다");
    expect(container.textContent).not.toContain("글의 근거로 삼을 웹페이지 주소입니다");
  });

  it("제한은 흩어지지 않고 한 줄에 모여 있다", async () => {
    await render();

    const limits = container.querySelector(".brief-reference-limits")?.textContent ?? "";
    // 개수 상한은 더 이상 적지 않는다(2026-08-11 사용자 요청) — 제한은 용량뿐이다.
    expect(limits).not.toContain("최대");
    // 상한 숫자는 상수에서 읽는다(2026-08-11 5/10MB → 20/20MB).
    expect(limits).toContain(`${MAX_REFERENCE_FILE_BYTES / 1024 / 1024}MB`);
    expect(limits).toContain(`${MAX_REFERENCE_FILES_BYTES / 1024 / 1024}MB`);

    // 같은 말이 다른 자리에 또 있으면 안 된다.
    expect(container.textContent).not.toContain("참고자료는 최대 10개까지 저장합니다");
  });

  it("소재를 적고 글 목적을 고르면 소재 키워드 수집을 미리 요청한다", async () => {
    // 2026-08-10 사용자 요청 — 목적·연령·참고 자료를 채우는 남은 시간이 곧 수집과
    // 관련도 판정이 도는 시간이 된다. '다음'(저장)까지 기다리지 않는다.
    await render();

    const topicInput = container.querySelector<HTMLInputElement>("#topic")!;
    // React 제어 컴포넌트라 값과 input 이벤트를 함께 보낸다(위 describe의 type 헬퍼와 동일).
    const valueSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    )?.set;
    await act(async () => {
      valueSetter?.call(topicInput, "아이언맨");
      topicInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const purposeButton = container.querySelector<HTMLButtonElement>(
      `[data-purpose="${WRITING_PURPOSES[0]}"]`,
    )!;
    await act(async () => purposeButton.click());

    const prefetches = mocks.request.mock.calls.filter(
      ([path]) => path === "/trends/prefetch",
    );
    expect(prefetches).toHaveLength(1);
    expect(prefetches[0][1]).toMatchObject({
      method: "POST",
      body: { topic: "아이언맨", purpose: [WRITING_PURPOSES[0]] },
    });

    // 같은 소재로는 다시 보내지 않는다 — 목적을 바꿔 눌러도 재요청이 없다.
    const secondPurpose = container.querySelector<HTMLButtonElement>(
      `[data-purpose="${WRITING_PURPOSES[1]}"]`,
    )!;
    await act(async () => secondPurpose.click());
    expect(
      mocks.request.mock.calls.filter(([path]) => path === "/trends/prefetch"),
    ).toHaveLength(1);
  });

  it("소재가 비어 있으면 미리 수집을 요청하지 않는다", async () => {
    await render();

    const purposeButton = container.querySelector<HTMLButtonElement>(
      `[data-purpose="${WRITING_PURPOSES[0]}"]`,
    )!;
    await act(async () => purposeButton.click());

    expect(
      mocks.request.mock.calls.filter(([path]) => path === "/trends/prefetch"),
    ).toHaveLength(0);
  });
});

/**
 * 소재 분야와 브랜드(2026-08-11).
 *
 * 카테고리는 '오디세이'가 영화인지 게임인지를 사용자에게 직접 묻는 값이다 — 비워 두면
 * 그 판단이 다시 모델에게 넘어가고, 제목·자료·이미지가 전부 그 위에 얹힌다.
 *
 * 브랜드는 반대로 **선택**이다. 별도 메뉴였던 브랜드 글쓰기가 이 칸으로 들어왔고,
 * 브랜드 자료는 화면이 실어 보내지 않는다 — brandId만 보내면 서버가 펼쳐 넣는다.
 */
describe("StepTopic 소재 분야와 브랜드", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = null;
    mocks.collectReferenceMaterials.mockResolvedValue([]);
    mocks.request.mockImplementation(async (path: string) =>
      path.startsWith("/brands")
        ? [
            {
              brandId: "brand_1",
              userId: "user_1",
              name: "AIONA",
              linkCount: 0,
              documentCount: 0,
              imageCount: 0,
              createdAt: "2026-08-11T00:00:00.000Z",
              updatedAt: "2026-08-11T00:00:00.000Z",
            },
          ]
        : { postId: "post_1", input: {} },
    );
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function render(task: BlogTask | null = null) {
    mocks.store.task = task;
    await act(async () => root.render(<StepTopic />));
  }

  async function fillTopicAndPurpose() {
    const topic = container.querySelector<HTMLInputElement>("#topic")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")
        ?.set?.call(topic, "오디세이");
      topic.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      container
        .querySelector<HTMLButtonElement>(`[data-purpose="${WRITING_PURPOSES[0]}"]`)
        ?.click();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[data-reader-age-range=""]')?.click();
    });
  }

  async function submit() {
    await act(async () => {
      container
        .querySelector<HTMLFormElement>("#topicForm")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
  }

  function savedBody() {
    const call = mocks.request.mock.calls.find(([path]) => path === "/posts");
    return call?.[1]?.body as Record<string, unknown> | undefined;
  }

  it("12개 카테고리를 모두 그린다", async () => {
    await render();

    const choices = [...container.querySelectorAll(".brief-category-choice")];
    expect(choices.map((node) => node.textContent)).toEqual([...SUBJECT_CATEGORIES]);
  });

  it("카테고리를 안 고르면 저장하지 않고 이유를 말한다", async () => {
    await render();
    await fillTopicAndPurpose();
    await submit();

    expect(savedBody()).toBeUndefined();
    expect(mocks.store.showToast).toHaveBeenCalledWith(
      expect.stringContaining("카테고리를 선택해 주세요"),
      true,
    );
  });

  it("고른 카테고리를 저장에 싣는다", async () => {
    await render();
    await fillTopicAndPurpose();
    await act(async () => {
      container
        .querySelectorAll<HTMLButtonElement>(".brief-category-choice")[1]
        ?.click();
    });
    await submit();

    expect(savedBody()?.subjectCategory).toBe(SUBJECT_CATEGORIES[1]);
  });

  async function pickBrand(value = "brand_1") {
    const select = container.querySelector<HTMLSelectElement>("#brandId")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")
        ?.set?.call(select, value);
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  it("브랜드를 고르면 brandId만 보낸다 — 소재와 브랜드 자료는 서버가 채운다", async () => {
    await render();
    await act(async () => {
      container
        .querySelector<HTMLButtonElement>(`[data-purpose="${WRITING_PURPOSES[0]}"]`)
        ?.click();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[data-reader-age-range=""]')?.click();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".brief-category-choice")?.click();
    });

    const select = container.querySelector<HTMLSelectElement>("#brandId")!;
    expect([...select.options].map((option) => option.value)).toEqual(["", "brand_1"]);
    await pickBrand();
    await submit();

    expect(savedBody()?.brandId).toBe("brand_1");
    // 소재는 비워 보낸다 — 서버가 브랜드 이름으로 채운다.
    expect(savedBody()?.topic).toBe("");
    // base64 이미지까지 왕복시키지 않는다.
    expect(savedBody()?.referenceMaterials).toEqual([]);
  });

  async function typeTopic(value: string) {
    const topic = container.querySelector<HTMLInputElement>("#topic")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")
        ?.set?.call(topic, value);
      topic.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  /**
   * 소재와 브랜드를 **함께** 고를 수 있다(2026-08-19 사용자 지시).
   *
   * 2026-08-11에는 둘이 서로를 잠갔다. 그런데 이 저장소가 실제로 만들려는 글이 바로 그
   * 조합이다 — 트렌드가 주인공이고 브랜드는 그 상황에서 쓴 도구인 글. 잠금이 있으면
   * 그 글을 아예 만들 수 없었다.
   */
  it("소재를 적어도 브랜드를 고를 수 있고, 브랜드를 골라도 소재를 적을 수 있다", async () => {
    await render();

    await typeTopic("빼빼로 신제품");
    // 소재를 적었다고 브랜드 칸이 잠기지 않는다.
    expect(container.querySelector<HTMLSelectElement>("#brandId")?.disabled).toBe(false);

    await pickBrand();
    // 브랜드를 골랐다고 소재 칸이 잠기지 않는다.
    const topic = container.querySelector<HTMLInputElement>("#topic")!;
    expect(topic.disabled).toBe(false);
    expect(topic.value).toBe("빼빼로 신제품");
    // 브랜드가 있으면 소재는 선택이다 — 비우는 것이 "브랜드를 주인공으로"라는 뜻이다.
    expect(topic.required).toBe(false);
  });

  it("소재와 브랜드가 둘 다 있으면 브랜드는 '활용한 도구'가 기본이다", async () => {
    await render();
    await typeTopic("빼빼로 신제품");
    await pickBrand();

    const chosen = container.querySelector(".brand-role-choice.is-on");
    expect(chosen?.textContent).toContain("활용한 도구로");
    expect(chosen?.getAttribute("aria-checked")).toBe("true");
  });

  it("소재를 비우면 역할을 고를 수 없고, 브랜드가 주인공이라고 말한다", async () => {
    await render();
    await pickBrand();

    expect(container.querySelector(".brand-role-choice")).toBeNull();
    expect(container.querySelector(".brand-role-note")?.textContent).toContain(
      "글의 주인공이 됩니다",
    );
  });

  it("역할을 저장에 실어 보낸다 — 서버가 프롬프트를 그 값으로 가른다", async () => {
    await render();
    await typeTopic("빼빼로 신제품");
    await pickBrand();
    await act(async () => {
      container
        .querySelector<HTMLButtonElement>(`[data-purpose="${WRITING_PURPOSES[0]}"]`)
        ?.click();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[data-reader-age-range=""]')?.click();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".brief-category-choice")?.click();
    });
    await submit();

    expect(savedBody()?.brandMode).toBe("UTILITY");
    // 브랜드를 도구로 쓰는 글은 소재가 곧 주인공이므로 그대로 보낸다.
    expect(savedBody()?.topic).toBe("빼빼로 신제품");
  });

  it("'글의 주인공으로'를 고르면 소재를 비워 보낸다 — 서버가 브랜드 이름으로 채운다", async () => {
    await render();
    await typeTopic("빼빼로 신제품");
    await pickBrand();
    await act(async () => {
      const focus = [...container.querySelectorAll<HTMLButtonElement>(".brand-role-choice")].find(
        (node) => node.textContent?.includes("글의 주인공으로"),
      );
      focus?.click();
    });
    await act(async () => {
      container
        .querySelector<HTMLButtonElement>(`[data-purpose="${WRITING_PURPOSES[0]}"]`)
        ?.click();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>('[data-reader-age-range=""]')?.click();
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".brief-category-choice")?.click();
    });
    await submit();

    expect(savedBody()?.brandMode).toBe("FOCUS");
    expect(savedBody()?.topic).toBe("");
  });

  it("고른 브랜드를 지우면 선택이 풀리고 목록에서도 빠진다", async () => {
    // 지운 브랜드가 골라진 채로 남으면 저장할 때 서버가 404를 낸다.
    await render();
    await pickBrand();
    expect(container.querySelector<HTMLSelectElement>("#brandId")?.value).toBe("brand_1");

    // 편집기를 열고 삭제까지 가는 대신, 편집기가 부르는 콜백을 그대로 부른다 —
    // 여기서 보려는 것은 **지운 뒤 이 화면이 정리되는가**이고, 삭제 자체는
    // BrandDelete.test.tsx가 본다.
    await act(async () => {
      const manage = [...container.querySelectorAll<HTMLButtonElement>(
        ".brief-brand-actions button",
      )].find((node) => node.textContent?.includes("브랜드 관리"));
      manage?.click();
    });
    await act(async () => {});
    const remove = container.querySelector<HTMLButtonElement>(".brand-delete-button");
    // 편집기가 조각으로 늦게 올 수 있다. 버튼이 있을 때만 눌러 본다.
    if (remove) {
      const originalConfirm = window.confirm;
      window.confirm = () => true;
      try {
        await act(async () => remove.click());
      } finally {
        window.confirm = originalConfirm;
      }

      const select = container.querySelector<HTMLSelectElement>("#brandId")!;
      expect(select.value).toBe("");
      expect([...select.options].map((option) => option.value)).toEqual([""]);
    }
  });

  it("브랜드를 고르지 않으면 brandId를 보내지 않고 소재를 그대로 보낸다", async () => {
    await render();
    await fillTopicAndPurpose();
    await act(async () => {
      container.querySelector<HTMLButtonElement>(".brief-category-choice")?.click();
    });
    await submit();

    expect(savedBody()?.brandId).toBeUndefined();
    expect(savedBody()?.topic).toBe("오디세이");
  });

  it("저장돼 있던 브랜드 자료는 '추가된 참고자료' 목록에 넣지 않는다", async () => {
    // 브랜드 자료 수십 개가 사용자 목록을 뒤덮으면 안 되고, 거기서 지울 수 있어서도 안 된다.
    await render({
      postId: "post_1",
      input: {
        topic: "오디세이",
        purpose: [WRITING_PURPOSES[0]],
        keywords: [WRITING_PURPOSES[0]],
        subjectCategory: SUBJECT_CATEGORIES[1],
        brandId: "brand_1",
        referenceMaterials: [
          { type: "TEXT", value: "AIONA 브랜드 자료", origin: "brand" },
          { type: "URL", value: "https://aiona.kr/", origin: "brand" },
          { type: "TEXT", value: "내가 적은 메모" },
        ],
      },
    } as unknown as BlogTask);

    const listed = [...container.querySelectorAll(".brief-reference-detail")].map(
      (node) => node.textContent,
    );
    expect(listed).toHaveLength(1);
    expect(listed[0]).toContain("내가 적은 메모");
    // 저장돼 있던 선택은 그대로 되살아난다.
    expect(container.querySelector<HTMLSelectElement>("#brandId")?.value).toBe("brand_1");
    expect(
      container
        .querySelectorAll(".brief-category-choice")[1]
        ?.getAttribute("aria-checked"),
    ).toBe("true");
  });
});

/**
 * 제목 단계에 갔다가 소재 단계로 돌아왔을 때(2026-08-11 사용자 신고).
 *
 * 이 화면은 단계를 옮기면 통째로 사라졌다가 다시 그려진다 — 값은 전부 저장된 글에서
 * 되살린다. 되살리지 못한 칸 하나만 빈 채로 남으면, 나머지가 다 채워져 있는 만큼
 * 사용자에게는 그것이 고장으로 보인다.
 */
describe("StepTopic 단계를 오간 뒤", () => {
  let container: HTMLDivElement;
  let root: Root;

  const SAVED_INPUT = {
    topic: "오디세이",
    purpose: [WRITING_PURPOSES[0]],
    keywords: [WRITING_PURPOSES[0]],
    subjectCategory: SUBJECT_CATEGORIES[1],
    referenceMaterials: [],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = null;
    mocks.collectReferenceMaterials.mockResolvedValue([]);
    mocks.request.mockImplementation(async (path: string) =>
      path.startsWith("/brands") ? [] : { postId: "post_1", input: SAVED_INPUT },
    );
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function render(task: BlogTask | null) {
    mocks.store.task = task;
    await act(async () => root.render(<StepTopic />));
  }

  function ageChecked(value: string): string | null | undefined {
    return container
      .querySelector(`[data-reader-age-range="${value}"]`)
      ?.getAttribute("aria-checked");
  }

  it("'전체'로 저장한 글로 돌아오면 대상 연령이 풀리지 않는다", async () => {
    // 화면은 전체를 ""로 두는데 서버는 빈 문자열을 받지 않아 저장할 때 아예 보내지
    // 않는다. 그래서 저장된 글에는 이 값이 없고, 예전에는 '고르지 않음'과 구분되지
    // 않아 이 칸만 풀린 채로 돌아왔다.
    await render({ postId: "post_1", input: SAVED_INPUT } as unknown as BlogTask);

    expect(ageChecked("")).toBe("true");
    // 나머지 선택도 그대로다 — 연령만 다르게 취급하지 않는다.
    expect(container.querySelector<HTMLInputElement>("#topic")?.value).toBe("오디세이");
    expect(
      container.querySelectorAll(".brief-category-choice")[1]?.getAttribute("aria-checked"),
    ).toBe("true");
  });

  it("특정 연령으로 저장한 글은 그 연령이 그대로 선택돼 있다", async () => {
    await render({
      postId: "post_1",
      input: { ...SAVED_INPUT, readerAgeRange: "20s" },
    } as unknown as BlogTask);

    expect(ageChecked("20s")).toBe("true");
    expect(ageChecked("")).toBe("false");
  });

  it("아직 저장하지 않은 새 글은 연령을 고르지 않은 채로 시작한다", async () => {
    // '전체'를 되살리는 규칙이 새 글까지 번지면 필수 선택이 사라진다 — 아무것도
    // 고르지 않았는데 전체가 골라진 것처럼 보이고, 저장을 막던 검사도 통과해 버린다.
    await render(null);

    for (const item of READER_AGE_RANGES) {
      expect(ageChecked(item.value)).toBe("false");
    }
  });

  it("소재를 다시 저장하면 옛 소재로 모은 트렌드 키워드를 버린다", async () => {
    // 서버도 같은 판단을 한다(update_blog_task_input이 글을 흐름의 처음으로 되돌리고
    // 키워드를 다시 모은다). 화면에만 남겨 두면 제목 단계가 이미 목록이 있다고 보고
    // 다시 모으지 않아, 고친 소재와 상관없는 카드가 그대로 남는다.
    await render({ postId: "post_1", input: SAVED_INPUT } as unknown as BlogTask);

    await act(async () => {
      container
        .querySelector<HTMLFormElement>("#topicForm")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1/input", {
      method: "PUT",
      body: expect.objectContaining({ topic: "오디세이" }),
    });
    expect(mocks.store.setRecommendation).toHaveBeenCalledWith(null);
    expect(mocks.store.setStep).toHaveBeenCalledWith(1);
  });
});

/**
 * 만들 원고 수(2026-08-12 사용자 결정, 최대 3편).
 *
 * 예약 화면이 하던 "하나의 소재로 여러 편"이 이쪽으로 옮겨 왔다. 여기서 지키는 것은
 * **상한과 하한이 화면에서 먼저 막힌다**는 것과, 1편일 때는 값을 아예 보내지 않아
 * 옛 서버·옛 흐름이 그대로 동작한다는 것이다.
 */
describe("만들 원고 수", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = null;
    mocks.collectReferenceMaterials.mockResolvedValue([]);
    mocks.request.mockResolvedValue({ postId: "post_1" });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  function step(label: string): HTMLButtonElement {
    return [...container.querySelectorAll("button")].find(
      (element) => element.getAttribute("aria-label") === label,
    )! as HTMLButtonElement;
  }

  function shown(): string {
    return container.querySelector(".brief-count-control strong")!.textContent ?? "";
  }

  it("기본은 한 편이고, 줄이기는 더 눌리지 않는다", async () => {
    await act(async () => root.render(<StepTopic />));

    expect(shown()).toBe("1편");
    expect(step("원고 수 줄이기").disabled).toBe(true);
  });

  it("+로 늘리다 상한에서 멈춘다", async () => {
    await act(async () => root.render(<StepTopic />));

    for (let i = 0; i < 5; i += 1) {
      await act(async () => step("원고 수 늘리기").click());
    }

    expect(shown()).toBe(`${MAX_DRAFT_COUNT}편`);
    expect(step("원고 수 늘리기").disabled).toBe(true);
  });

  it("편수를 바꾸면 안내 문구가 따라 바뀐다", async () => {
    // 문구의 내용은 아래 '설정 요약 문장'이 따로 본다. 여기서는 편수를 바꿨을 때
    // **그 자리가 함께 바뀌는지**만 확인한다.
    await act(async () => root.render(<StepTopic />));
    const hint = () => container.querySelector(".brief-outcome")!.textContent ?? "";
    const before = hint();

    await act(async () => step("원고 수 늘리기").click());

    expect(hint()).not.toBe(before);
    expect(hint()).toContain("2편");
  });
});


/**
 * 지금 설정이 뜻하는 것을 한 문장으로(2026-08-12 사용자 지적).
 *
 * 날짜 칸과 ± 버튼만 있으면 비웠을 때 어떻게 되는지, 3편이면 언제 무엇이 도는지가
 * 어디에도 적혀 있지 않다. 세 설정이 얽혀 있어서 칸마다 따로 설명해도 합쳤을 때
 * 무엇이 되는지는 여전히 알 수 없다.
 */
describe("설정 요약 문장", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = null;
    mocks.collectReferenceMaterials.mockResolvedValue([]);
    mocks.request.mockResolvedValue({ postId: "post_1" });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  function summary(): string {
    return container.querySelector(".brief-outcome")!.textContent ?? "";
  }

  function step(label: string): HTMLButtonElement {
    return [...container.querySelectorAll("button")].find(
      (element) => element.getAttribute("aria-label") === label,
    )! as HTMLButtonElement;
  }

  it("시각을 비우면 지금 만든다고 말한다", async () => {
    await act(async () => root.render(<StepTopic />));

    expect(summary()).toContain("지금 바로");
    expect(summary()).toContain("1편");
  });

  it("여러 편이면 차례로 올린다는 것까지 말한다", async () => {
    await act(async () => root.render(<StepTopic />));

    await act(async () => step("원고 수 늘리기").click());

    expect(summary()).toContain("2편");
    expect(summary()).toContain("차례로");
  });

  function platformBox(index: number): HTMLInputElement {
    return container.querySelectorAll<HTMLInputElement>(".brief-autopublish input")[index];
  }

  // 기본은 네이버 켬이다. 2026-08-13 아침에 잠깐 둘 다 꺼짐으로 두었다가 같은 날
  // 되돌렸다 — 아무 데도 올리지 않는 글이 작업 큐에 서면 화면·진행바·로그가 전부
  // 그 예외를 설명해야 했다. 올릴 곳을 반드시 고르게 하는 편이 단순하다는 결정이다.
  it("기본은 네이버만 올린다", async () => {
    await act(async () => root.render(<StepTopic />));

    expect(platformBox(0).checked).toBe(true);
    expect(platformBox(1).checked).toBe(false);
    expect(summary()).toContain("네이버에 올립니다");
    expect(summary()).not.toContain("쓰레드");
  });

  it("둘 다 켜면 둘 다 말한다", async () => {
    await act(async () => root.render(<StepTopic />));

    await act(async () => platformBox(1).click());

    expect(summary()).toContain("네이버와 쓰레드");
  });

  it("쓰레드만 켜면 쓰레드만 말한다", async () => {
    await act(async () => root.render(<StepTopic />));

    await act(async () => platformBox(0).click());
    await act(async () => platformBox(1).click());

    expect(summary()).toContain("쓰레드에 올립니다");
    expect(summary()).not.toContain("네이버");
  });

  it("둘 다 끄면 골라 달라고 말한다", async () => {
    // '발행하지 않음'이 아니다 — 이 상태로는 저장이 막힌다.
    await act(async () => root.render(<StepTopic />));

    await act(async () => platformBox(0).click());

    expect(summary()).toContain("올릴 곳을 하나 이상 골라 주세요");
  });
});

/**
 * 「언제, 몇 편, 어디에」의 두 걸음(2026-08-12 사용자 시안).
 *
 * 여태 이 상자는 날짜 칸 하나였고, **비어 있는 것**이 곧 '지금 바로'였다. 이제 그것을
 * 두 단추로 갈라 눈에 보이게 한다. 값 자체는 여전히 하나(`scheduledRunAt`)이므로,
 * 고른 것과 저장되는 것이 어긋나지 않는지가 여기서 볼 것이다.
 */
describe("언제 만들까요", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.task = null;
    mocks.collectReferenceMaterials.mockResolvedValue([]);
    mocks.request.mockResolvedValue({ postId: "post_1" });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  function modeButton(text: "지금 바로" | "예약 발행"): HTMLButtonElement {
    const found = [...container.querySelectorAll<HTMLButtonElement>(".brief-mode-option")].find(
      (element) => element.textContent?.includes(text),
    );
    if (!found) throw new Error(`'${text}' 단추를 찾지 못했다`);
    return found;
  }

  function whenInput(): HTMLInputElement | null {
    return container.querySelector<HTMLInputElement>(".brief-schedule-field input");
  }

  function outcomeLines(): string[] {
    return [...container.querySelectorAll(".brief-outcome-list li")].map(
      (line) => line.textContent ?? "",
    );
  }

  /** 지금부터 얼마 뒤를 datetime-local 값으로. */
  function inMinutes(minutes: number): string {
    const at = new Date(Date.now() + minutes * 60_000);
    at.setSeconds(0, 0);
    const pad = (value: number) => String(value).padStart(2, "0");
    return (
      `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}` +
      `T${pad(at.getHours())}:${pad(at.getMinutes())}`
    );
  }

  async function typeWhen(value: string) {
    const field = whenInput()!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        field,
        value,
      );
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  async function fillRequired() {
    const topic = container.querySelector<HTMLInputElement>("#topic")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(
        topic,
        "오디세이",
      );
      topic.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () =>
      container
        .querySelector<HTMLButtonElement>(`[data-purpose="${WRITING_PURPOSES[0]}"]`)
        ?.click(),
    );
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-reader-age-range=""]')?.click(),
    );
    // 카테고리도 필수다 — 고르지 않으면 시각을 보기 전에 저장이 막힌다.
    await act(async () =>
      container.querySelector<HTMLButtonElement>(".brief-category-choice")?.click(),
    );
  }

  async function submit() {
    await act(async () => {
      container
        .querySelector<HTMLFormElement>("#topicForm")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
  }

  it("올릴 곳을 하나도 고르지 않으면 다음 단계로 넘어가지 않는다", async () => {
    // 2026-08-13 사용자 지시. 예약 화면·재예약은 예전부터 같은 규칙이었고, 새 글 작성만
    // 비어 있었다 — 아무 데도 올라가지 않는 글이 작업 큐에 서는 길이 열려 있었다.
    await act(async () => root.render(<StepTopic />));
    await fillRequired();
    await act(async () =>
      container.querySelectorAll<HTMLInputElement>(".brief-autopublish input")[0].click(),
    );

    await submit();

    expect(mocks.request.mock.calls.find(([path]) => path === "/posts")).toBeUndefined();
    expect(mocks.store.showToast).toHaveBeenCalledWith(
      expect.stringContaining("올릴 곳을 하나 이상"),
      true,
    );
  });

  it("쓰레드만 골라도 넘어간다", async () => {
    await act(async () => root.render(<StepTopic />));
    await fillRequired();
    const boxes = container.querySelectorAll<HTMLInputElement>(".brief-autopublish input");
    await act(async () => boxes[0].click());
    await act(async () => boxes[1].click());

    await submit();

    const call = mocks.request.mock.calls.find(([path]) => path === "/posts");
    expect(call?.[1]?.body).toMatchObject({
      autoPublishNaver: false,
      autoPublishThreads: true,
    });
  });

  it("처음에는 '지금 바로'이고 날짜 칸이 없다", async () => {
    await act(async () => root.render(<StepTopic />));

    expect(modeButton("지금 바로").getAttribute("aria-pressed")).toBe("true");
    expect(modeButton("예약 발행").getAttribute("aria-pressed")).toBe("false");
    // 쓰지 않는 칸을 미리 펼쳐 두면 무엇을 고른 상태인지 흐려진다.
    expect(whenInput()).toBeNull();
  });

  it("'예약 발행'을 누르면 날짜 칸이 나온다", async () => {
    await act(async () => root.render(<StepTopic />));

    await act(async () => modeButton("예약 발행").click());

    expect(whenInput()).not.toBeNull();
    expect(modeButton("예약 발행").getAttribute("aria-pressed")).toBe("true");
  });

  it("'지금 바로'로 돌아가면 골라 둔 시각을 지운다", async () => {
    // 남겨 두면 화면은 '지금 바로'인데 서버에는 예약으로 저장된다.
    await act(async () => root.render(<StepTopic />));
    await act(async () => modeButton("예약 발행").click());
    await typeWhen(inMinutes(90));

    await act(async () => modeButton("지금 바로").click());
    await fillRequired();
    await submit();

    const call = mocks.request.mock.calls.find(([path]) => path === "/posts");
    expect(call?.[1]?.body).not.toHaveProperty("scheduledRunAt");
  });

  it("예약을 골라 놓고 시각을 비워 두면 저장을 막는다", async () => {
    // 값이 비어 있으면 '지금 바로'로 저장된다 — 고른 것과 저장되는 것이 달라진다.
    await act(async () => root.render(<StepTopic />));
    await act(async () => modeButton("예약 발행").click());
    await fillRequired();

    await submit();

    expect(mocks.store.showToast).toHaveBeenCalledWith("예약 발행 일시를 골라 주세요.", true);
    expect(mocks.request.mock.calls.some(([path]) => path === "/posts")).toBe(false);
  });

  it("고른 시각은 그대로 예약으로 저장된다", async () => {
    await act(async () => root.render(<StepTopic />));
    await act(async () => modeButton("예약 발행").click());
    const when = inMinutes(120);
    await typeWhen(when);
    await fillRequired();

    await submit();

    const call = mocks.request.mock.calls.find(([path]) => path === "/posts");
    const body = call?.[1]?.body as Record<string, unknown>;
    expect(new Date(body.scheduledRunAt as string).getTime()).toBe(new Date(when).getTime());
  });

  it("요약 상자가 고른 값을 세 줄로 되짚는다", async () => {
    await act(async () => root.render(<StepTopic />));

    const lines = outcomeLines();
    expect(lines).toHaveLength(3);
    expect(lines[0]).toContain("지금 바로 시작");
    expect(lines[1]).toContain("원고 1편 생성");
    expect(lines[2]).toContain("네이버 발행 예정");

    // 끄면 '안 올린다'가 아니라 **골라 달라**고 말한다 — 이 상태로는 저장이 막힌다.
    await act(async () =>
      container.querySelectorAll<HTMLInputElement>(".brief-autopublish input")[0].click(),
    );
    expect(outcomeLines()[2]).toContain("올릴 곳을 골라 주세요");

    await act(async () => modeButton("예약 발행").click());
    // 아직 시각을 안 골랐다는 것을 요약에서도 말한다.
    expect(outcomeLines()[0]).toContain("시각을 골라 주세요");
  });

  it("예상 소요 시간은 실측 단계 소요에서 나온다", async () => {
    // 글자로 박아 두면 그 값을 고칠 때 여기만 옛말이 된다(2026-08-11 실측).
    await act(async () => root.render(<StepTopic />));

    const minutes = Math.round(
      DRAFT_STEP_SECONDS.reduce((total, seconds) => total + seconds, 0) / 60,
    );
    const facts = [...container.querySelectorAll(".brief-fact")].map((f) => f.textContent ?? "");
    // 하나뿐이다 — 나란히 있던 '자동 발행 ON'은 아래 체크박스를 되비추기만 해서 뺐다
    // (2026-08-12 사용자 지적: "어차피 누를 수가 없는데 무슨 차이야").
    expect(facts).toHaveLength(1);
    expect(facts[0]).toContain(`약 ${minutes}분`);
  });
});
