import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * 「자동 포스팅」 탭 — 여러 소재를 한 번에 거는 화면(2026-08-12).
 *
 * 여기서 보는 것은 **화면이 서버와 맺는 계약**이다: 무엇을 막고, 어떤 몸통을 보내고,
 * 성공하면 어디로 가는가. 글을 만들고 올리는 일 자체는 백엔드 테스트가 본다.
 */
const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  store: {
    session: {
      user: { userId: "user_1", email: "a@b.c", nickname: "wu" },
      accessToken: "t",
    } as unknown,
    setRoute: vi.fn(),
    showToast: vi.fn(),
    reportError: vi.fn(),
    openPost: vi.fn(),
  },
}));

vi.mock("../../api/client", () => ({ request: mocks.request }));
vi.mock("../../store", () => ({ useStore: () => mocks.store }));

import { BulkScheduleView } from "./BulkScheduleView";

const CONNECTED = {
  configured: true,
  blogId: "myblog",
  saved: true,
  savedUsername: "naver_id",
  hasSession: true,
};

/** 계정은 저장돼 있고 도는 배치는 없다 — 이 화면의 평상시 상태다. */
function respond(naver: unknown = CONNECTED, threads: unknown = CONNECTED) {
  mocks.request.mockImplementation((path: string) => {
    if (path === "/naver/status") return Promise.resolve(naver);
    if (path === "/threads/status") return Promise.resolve(threads);
    if (path === "/scheduled/naver/batches/active") return Promise.resolve(null);
    if (path === "/scheduled/naver/jobs") return Promise.resolve({ items: [] });
    // 브랜드 고르기 칸이 목록을 부른다(2026-08-19). 하나만 등록돼 있는 상태다.
    if (path.startsWith("/brands")) {
      return Promise.resolve([
        {
          brandId: "brand_1",
          userId: "user_1",
          name: "AIONA",
          linkCount: 0,
          documentCount: 0,
          imageCount: 0,
          createdAt: "2026-08-19T00:00:00.000Z",
          updatedAt: "2026-08-19T00:00:00.000Z",
        },
      ]);
    }
    // 시작 요청. 서버는 만들어진 배치를 돌려준다.
    return Promise.resolve({ batch: { batchId: "batch_1" }, jobs: [] });
  });
}

describe("자동 포스팅 화면(여러 소재)", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    location.hash = "#/bulk";
    respond();
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function render() {
    await act(async () => {
      root.render(<BulkScheduleView />);
    });
  }

  function button(text: string): HTMLButtonElement {
    const found = [...container.querySelectorAll("button")].find(
      (el) => el.textContent?.trim() === text,
    );
    if (!found) throw new Error(`버튼을 찾지 못했습니다: ${text}`);
    return found as HTMLButtonElement;
  }

  function topicRows(): HTMLInputElement[] {
    return [...container.querySelectorAll<HTMLInputElement>(".scheduled-topic-line")];
  }

  function categorySelects(): HTMLSelectElement[] {
    return [...container.querySelectorAll<HTMLSelectElement>(".scheduled-topic-category")];
  }

  function whenInputs(): HTMLInputElement[] {
    return [...container.querySelectorAll<HTMLInputElement>(".scheduled-topic-when")];
  }

  /** 지금부터 얼마 뒤를 datetime-local 값으로. 화면이 받는 형식과 같다(로컬 시간). */
  function inMinutes(minutes: number): string {
    const target = new Date(Date.now() + minutes * 60_000);
    target.setSeconds(0, 0);
    const pad = (value: number) => String(value).padStart(2, "0");
    return (
      `${target.getFullYear()}-${pad(target.getMonth() + 1)}-${pad(target.getDate())}` +
      `T${pad(target.getHours())}:${pad(target.getMinutes())}`
    );
  }

  async function setValue(field: HTMLInputElement | HTMLSelectElement, value: string) {
    const prototype =
      field instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    await act(async () => {
      setter?.call(field, value);
      field.dispatchEvent(new Event("change", { bubbles: true }));
      if (field instanceof HTMLInputElement) {
        field.dispatchEvent(new Event("input", { bubbles: true }));
      }
    });
  }

  /** 소재를 줄마다 적는다. 플랫폼은 기본이 네이버다. */
  async function typeTopics(...topics: string[]) {
    for (let index = 0; index < topics.length; index += 1) {
      const row = topicRows()[index];
      if (!row) throw new Error(`${index + 1}번째 줄이 없습니다`);
      await setValue(row, topics[index]);
    }
  }

  /** 시작 요청의 몸통. */
  function startBody(): Record<string, unknown> {
    const call = mocks.request.mock.calls.find(
      (entry) => entry[0] === "/scheduled/naver/batches",
    );
    if (!call) throw new Error("예약 시작 요청이 없습니다");
    return (call[1] as { body: Record<string, unknown> }).body;
  }

  it("계정 상태를 읽고 소재 입력칸을 세 줄 깔아 둔다", async () => {
    await render();

    const paths = mocks.request.mock.calls.map((call) => call[0]);
    expect(paths).toContain("/naver/status");
    // 처음 보여 주는 칸은 세 줄이다(2026-08-05 결정). 모자라면 Enter로 늘어난다.
    expect(topicRows()).toHaveLength(3);
  });

  it("설명은 입력칸 위가 아니라 옆의 「도우미」 카드에 있다", async () => {
    await render();

    // 카드는 새 글 작성의 「이 글 요약」과 같은 모양을 쓴다 — 새로 만들지 않는다.
    const helper = container.querySelector(".summary-note");
    expect(helper).not.toBeNull();
    expect(helper?.textContent).toContain("도우미");
    // 줄의 칸을 순서대로 늘어놓아, 화면을 보면서 맞춰 읽을 수 있어야 한다.
    for (const key of ["작업 시작", "소재", "분야", "발행 플랫폼"]) {
      expect(helper?.textContent).toContain(key);
    }
    // 입력 패널 위의 안내 문단은 없앴다(2026-08-12) — 옆으로 옮겼기 때문이다.
    expect(container.textContent).not.toContain("줄 하나가 글 한 편입니다");
  });

  it("도우미의 설명은 칸마다 한 문장이다", async () => {
    await render();
    const values = [...container.querySelectorAll<HTMLElement>(".helper-note-value")].map(
      (el) => el.textContent ?? "",
    );

    expect(values.length).toBeGreaterThan(0);
    for (const value of values) {
      // 마침표가 둘이면 문장이 둘이다 — 좁은 칸에서 접히면 어디까지가 한 이야기인지
      // 눈으로 끊기지 않는다(2026-08-12 사용자 지적).
      expect(value.split(".").filter((part) => part.trim()).length).toBe(1);
    }
  });

  it("도우미의 칸은 그림 하나에 이름과 설명이 붙는다", async () => {
    // 2026-08-12 사용자 시안. 이름과 값을 좌우로 벌리던 요약 카드 배치와 달라진 지점이라,
    // 되돌아가지 않게 여기서 잡아 둔다.
    await render();
    const rows = [...container.querySelectorAll(".helper-note-row")];

    expect(rows).toHaveLength(7);
    for (const row of rows) {
      expect(row.querySelector(".helper-note-icon svg")).not.toBeNull();
      expect(row.querySelector(".helper-note-key")?.textContent).toBeTruthy();
      expect(row.querySelector(".helper-note-value")?.textContent).toBeTruthy();
    }
  });

  it("플랫폼을 고르지 않으면 네이버로 간다고 알려 준다", async () => {
    await render();
    const helper = container.querySelector(".summary-note");

    // 새 줄의 기본값이 네이버인데(useScheduledPosting) 화면이 그 사실을 말하지 않아,
    // 직접 켜야 하는 줄 알았다(2026-08-12 사용자 요청).
    expect(helper?.textContent).toContain("네이버");
    // 예시로 설명하지 않는다 — 그 낱말을 아는 사람에게만 통한다.
    expect(helper?.textContent).not.toContain("오디세이");
  });

  it("소재를 적기 전에는 시작할 수 없다", async () => {
    await render();

    expect(button("자동 포스팅 시작").disabled).toBe(true);
    expect(container.textContent).toContain("소재를 한 개 이상 입력해 주세요");
  });

  it("소재와 플랫폼만 정하면 발행 시각 없이 예약이 걸린다", async () => {
    await render();
    await typeTopics("오디세이", "손흥민");

    await act(async () => button("자동 포스팅 시작").click());

    const body = startBody();
    const schedules = body.schedules as Record<string, unknown>[];
    expect(schedules.map((item) => item.topic)).toEqual(["오디세이", "손흥민"]);
    // **발행 시각을 보내지 않는다.** 이 값이 실리면 서버가 절대 시각 방식으로 읽고,
    // 글 사이 최소 간격 규칙에 걸려 거절한다.
    expect(schedules.every((item) => !("publishAt" in item))).toBe(true);
    expect(body.topicMode).toBe("multi");
    expect(body.targetCount).toBe(2);
    // 기본 플랫폼은 네이버다.
    expect(schedules[0].publishNaver).toBe(true);
  });

  it("고른 분야를 소재마다 실어 보내고, 안 고른 줄은 키를 빼고 보낸다", async () => {
    await render();
    await typeTopics("오디세이", "손흥민");
    await setValue(categorySelects()[0], "게임");

    await act(async () => button("자동 포스팅 시작").click());

    const schedules = startBody().schedules as Record<string, unknown>[];
    expect(schedules[0].subjectCategory).toBe("게임");
    // 빈 값을 보내면 서버가 목록 밖의 값이라고 거절한다 — 키 자체가 없어야 한다.
    expect("subjectCategory" in schedules[1]).toBe(false);
  });

  it("분야 선택은 소재를 따라간다 — 가운데 줄을 지워도 물려받지 않는다", async () => {
    await render();
    await typeTopics("오디세이", "손흥민", "올리브영");
    await setValue(categorySelects()[2], "제품·쇼핑·리뷰");
    // 가운데 소재를 지운다. 남은 줄이 한 칸씩 당겨진다.
    await setValue(topicRows()[1], "");

    await act(async () => button("자동 포스팅 시작").click());

    const schedules = startBody().schedules as Record<string, unknown>[];
    expect(schedules.map((item) => item.topic)).toEqual(["오디세이", "올리브영"]);
    expect(schedules[0].subjectCategory).toBeUndefined();
    expect(schedules[1].subjectCategory).toBe("제품·쇼핑·리뷰");
  });

  it("성공하면 작업 관리의 작업 큐로 옮긴다", async () => {
    await render();
    await typeTopics("오디세이");

    await act(async () => button("자동 포스팅 시작").click());

    expect(location.hash).toBe("#/scheduled/queue");
  });

  it("작업 시각 칸은 비어 있는 채로 시작한다", async () => {
    await render();
    await typeTopics("오디세이");

    // 비어 있음이 곧 '앞 글이 발행되면 이어서'다. 미리 채워 두면 사용자가 정하지 않은
    // 약속이 걸린다.
    expect(whenInputs()[0].value).toBe("");
    expect(button("자동 포스팅 시작").disabled).toBe(false);
  });

  it("적은 줄만 시각을 싣고, 비운 줄은 키를 빼고 보낸다", async () => {
    await render();
    await typeTopics("오디세이", "손흥민");
    await setValue(whenInputs()[0], inMinutes(60));

    await act(async () => button("자동 포스팅 시작").click());

    const schedules = startBody().schedules as Record<string, unknown>[];
    expect(typeof schedules[0].publishAt).toBe("string");
    // 비운 줄은 서버가 앞 줄에 매단다 — 시각을 보내면 그 뜻이 사라진다.
    expect("publishAt" in schedules[1]).toBe(false);
    // 한 줄이라도 시각이 있으면 시간대를 함께 보낸다(표시·감사용).
    expect(typeof startBody().timezone).toBe("string");
  });

  it("고른 작업 시각을 그대로 보낸다 — 더하고 빼는 여유가 없다", async () => {
    // 2026-08-12 사용자 지시: "그 20분 차이 굳이 없어도 되는거잖아. 아까 내가 지우라고
    // 했을텐데?" 예전에는 화면이 20분을 더해 보내고 워커가 그만큼 앞서 시작했다. 그
    // 여유를 서버에서 없앤 뒤에도 화면의 보정만 남아, 오후 1시 21분에 건 작업이 큐에
    // '오후 1:01'로 찍혔다.
    await render();
    await typeTopics("오디세이");
    const workStart = inMinutes(60);
    await setValue(whenInputs()[0], workStart);

    await act(async () => button("자동 포스팅 시작").click());

    const schedules = startBody().schedules as Record<string, unknown>[];
    const publishAt = new Date(schedules[0].publishAt as string).getTime();
    expect(publishAt).toBe(new Date(workStart).getTime());
  });

  it("지난 시각을 고르면 시작을 막는다", async () => {
    await render();
    await typeTopics("오디세이");
    await setValue(whenInputs()[0], inMinutes(-30));

    expect(button("자동 포스팅 시작").disabled).toBe(true);
    expect(container.textContent).toContain("이미 지났습니다");
  });

  it("적은 시각끼리 너무 붙어 있으면 시작을 막는다", async () => {
    await render();
    await typeTopics("오디세이", "손흥민");
    await setValue(whenInputs()[0], inMinutes(60));
    await setValue(whenInputs()[1], inMinutes(65));

    expect(button("자동 포스팅 시작").disabled).toBe(true);
    expect(container.textContent).toContain("12분 이상 떨어뜨려");
  });

  it("비운 줄은 간격 검사에 걸리지 않는다", async () => {
    await render();
    await typeTopics("오디세이", "손흥민", "올리브영");
    await setValue(whenInputs()[0], inMinutes(60));
    await setValue(whenInputs()[2], inMinutes(200));

    expect(button("자동 포스팅 시작").disabled).toBe(false);
  });

  it("네이버 계정이 없으면 시작을 막고 이유를 적는다", async () => {
    respond({ ...CONNECTED, saved: false });
    await render();
    await typeTopics("오디세이");

    expect(button("자동 포스팅 시작").disabled).toBe(true);
    expect(container.textContent).toContain("저장된 Naver 계정이 없습니다");
  });
  /**
   * 큐 전체에 브랜드를 얹는다(2026-08-19).
   *
   * AIONA 유입용 콘텐츠는 한 편씩 만드는 것이 아니라 6~10편을 큐로 걸어 돌린다. 여기에
   * 브랜드를 고를 자리가 없으면 그 큐로 나간 글만 브랜드 없이 나간다.
   */
  it("브랜드를 고르면 시작 요청에 실어 보낸다", async () => {
    await render();
    await typeTopics("다이어트 간식");

    const picker = container.querySelector<HTMLSelectElement>("#brandId")!;
    await setValue(picker, "brand_1");
    await act(async () => {
      button("자동 포스팅 시작").click();
    });

    expect(startBody().brandId).toBe("brand_1");
  });

  it("브랜드를 고르지 않으면 그 값을 아예 보내지 않는다", async () => {
    // 브랜드를 쓰지 않는 예약은 예전과 한 글자도 달라지지 않아야 한다.
    await render();
    await typeTopics("다이어트 간식");
    await act(async () => {
      button("자동 포스팅 시작").click();
    });

    expect(startBody()).not.toHaveProperty("brandId");
  });

  it("브랜드 칸은 줄마다가 아니라 배치에 하나다", async () => {
    await render();
    await typeTopics("다이어트 간식", "빼빼로 신제품");

    // 줄이 둘이어도 고르는 칸은 하나다 — 이 큐를 거는 이유가 "이 소재들을 우리
    // 서비스와 엮어 쓴다"라, 줄마다 다른 브랜드를 고르는 일이 없다.
    expect(container.querySelectorAll("#brandId")).toHaveLength(1);
  });
});
