import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ScheduledBatch,
  ScheduledBatchView,
  ScheduledJob,
} from "../../api/types";

/**
 * 예약 포스팅 화면이 **실제 API**와 붙어 있는지 확인한다.
 *
 * 여기서 보는 것은 오케스트레이션이 아니라 화면의 계약이다: 무엇을 조회하고, 어떤 몸통을
 * 보내고, 서버가 준 상태를 어떻게 읽는가. 글 생성·발행 자체는 백엔드 테스트가 본다.
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

import { clockLabel } from "./PublishHistoryCard";
import { ScheduledView } from "./ScheduledView";

const NAVER_CONNECTED = {
  configured: true,
  blogId: "myblog",
  saved: true,
  savedUsername: "naver_id",
  hasSession: true,
};

function job(overrides: Partial<ScheduledJob> = {}): ScheduledJob {
  return {
    jobId: "job_1",
    batchId: "batch_1",
    userId: "user_1",
    platform: "naver",
    sequence: 0,
    topic: "여름 휴가 추천 여행지 5곳",
    variantIndex: 0,
    status: "WAITING",
    stage: "CREATE_POST",
    retryCount: 0,
    createdAt: "2026-08-04T01:00:00.000Z",
    updatedAt: "2026-08-04T01:00:00.000Z",
    ...overrides,
  };
}

function batch(overrides: Partial<ScheduledBatch> = {}): ScheduledBatch {
  return {
    batchId: "batch_1",
    userId: "user_1",
    platform: "naver",
    topicMode: "multi",
    status: "RUNNING",
    targetCount: 3,
    intervalSeconds: 300,
    totalCount: 3,
    completedCount: 0,
    failedCount: 0,
    canceledCount: 0,
    pauseRequested: false,
    stopRequested: false,
    createdAt: "2026-08-04T01:00:00.000Z",
    updatedAt: "2026-08-04T01:00:00.000Z",
    logs: [],
    ...overrides,
  };
}

function view(b: Partial<ScheduledBatch> = {}, jobs: ScheduledJob[] = []): ScheduledBatchView {
  return { batch: batch(b), jobs };
}

/**
 * 배치 뷰 → '내 예약 전부'(/scheduled/naver/jobs) 응답.
 *
 * 작업 큐 탭은 활성 배치가 아니라 이 목록을 읽는다(배치가 끝나도 남아야 하기 때문이다).
 * 그래서 큐를 보는 테스트의 가짜 서버는 두 조회에 **같은 작업**을 담아 줘야 한다.
 */
function jobList(current: ScheduledBatchView): { items: { job: ScheduledJob }[] } {
  return { items: current.jobs.map((job) => ({ job })) };
}

/**
 * 첫 로드의 두 조회에 답하고, 제어(일시정지·재개·정지) 요청에는 **갱신된 배치 뷰**를
 * 돌려준다. 서버가 실제로 그렇게 답하기 때문이다 — null을 돌려주면 화면이 배치를 잃고
 * 다음 버튼이 잠긴다.
 */
function respond(
  naver: unknown,
  active: ScheduledBatchView | null,
  threads: unknown = null,
) {
  mocks.request.mockImplementation((path: string) => {
    if (path === "/naver/status") return Promise.resolve(naver);
    if (path === "/threads/status") return Promise.resolve(threads);
    if (path === "/scheduled/naver/batches/active") return Promise.resolve(active);
    // '내 예약 전부'. 실제 서버도 활성 배치의 작업을 여기에 함께 담아 주고, 배치가
    // 끝난 뒤에도 남긴다 — 작업 큐·발행 내역 탭이 이 조회를 읽는다(2026-08-06).
    if (path === "/scheduled/naver/jobs") {
      return Promise.resolve({ items: (active?.jobs ?? []).map((job) => ({ job })) });
    }
    if (path.startsWith("/scheduled/naver/batches/") || path.startsWith("/scheduled/naver/jobs/")) {
      return Promise.resolve(active);
    }
    return Promise.resolve(null);
  });
}

describe("예약 포스팅 화면", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    // 삭제는 되돌릴 수 없어 확인창을 거친다. 기본은 '예'로 두고, 취소했을 때는
    // 아래 전용 테스트가 따로 확인한다.
    vi.stubGlobal("confirm", () => true);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    respond(NAVER_CONNECTED, null);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  async function render() {
    await act(async () => {
      root.render(<ScheduledView />);
    });
  }

  // 정확히 일치하는 것만 찾는다 — '정지'로 부분 일치를 하면 '일시정지'가 먼저 잡힌다.
  function button(text: string): HTMLButtonElement {
    const found = [...container.querySelectorAll("button")].find(
      (el) => el.textContent?.trim() === text,
    );
    if (!found) throw new Error(`버튼을 찾지 못했습니다: ${text}`);
    return found as HTMLButtonElement;
  }


  /** 소재별 한 편 모드의 줄 입력칸들. 번호 순서 그대로다. */
  function topicRows(): HTMLInputElement[] {
    return [...container.querySelectorAll<HTMLInputElement>(".scheduled-topic-line")];
  }

  function singleTopicInput(): HTMLInputElement | null {
    return container.querySelector<HTMLInputElement>(".scheduled-topic-single");
  }

  /**
   * 소재를 입력한다. 소재별 한 편 모드는 줄마다 칸이 따로라 줄 수만큼 나눠 넣고,
   * 소재 하나 모드는 입력칸이 하나뿐이라 그대로 넣는다.
   */
  async function type(value: string) {
    const single = singleTopicInput();
    if (single) {
      await setInput(single, value);
      return;
    }
    const lines = value.split("\n");
    for (let index = 0; index < lines.length; index += 1) {
      const row = topicRows()[index];
      if (!row) break;
      await setInput(row, lines[index]);
    }
  }

  /**
   * 만들기 걸음을 옮긴다. **입력하는 걸음은 둘뿐이다**(소재 → 일정). 마지막 걸음은
   * 화면이 아니라 '작업 큐' 탭이라 openQueue()로 연다(2026-08-06).
   *
   * 소재가 비어 있으면 1걸음을 넘어갈 수 없으므로 기본 소재를 한 줄 채운다 — 그 테스트가
   * 보려는 것은 소재 입력이 아니기 때문이다.
   */
  function currentStep(): number {
    const marker = container.querySelector(".reservation-step.is-current .reservation-step-number");
    return Number(marker?.textContent ?? "1");
  }

  async function goToStep(target: 1 | 2) {
    // 지나온 걸음으로는 진행 표시를 눌러 돌아간다(작업 큐에서 돌아올 때도 같은 길이다).
    while (currentStep() > target) {
      const back = [
        ...container.querySelectorAll<HTMLButtonElement>(".reservation-step-button"),
      ][target - 1];
      if (!back || back.disabled) break;
      await act(async () => back.click());
    }
    while (currentStep() < target) {
      const rows = topicRows();
      if (rows.length > 0 && rows.every((row) => row.value.trim() === "")) {
        await setInput(rows[0], "기본 소재");
      }
      const next = [...container.querySelectorAll("button")].find(
        (element) => element.textContent?.trim() === "다음: 발행 방식 설정",
      ) as HTMLButtonElement | undefined;
      if (!next || next.disabled) break;
      await act(async () => next.click());
    }
  }

  /** 작업 큐 탭을 연다. 탭 이름 뒤에 남은 작업 수가 붙어 있어 앞글자로 찾는다. */
  async function openQueue() {
    const tab = [...container.querySelectorAll("button")].find((element) =>
      element.textContent?.trim().startsWith("작업 큐"),
    ) as HTMLButtonElement | undefined;
    if (!tab) throw new Error("'작업 큐' 탭을 찾지 못했습니다.");
    await act(async () => tab.click());
  }




  /** 숫자 칸에 값을 넣는다. React가 가로챈 setter를 우회해 실제 입력처럼 만든다. */
  async function setInput(input: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    await act(async () => {
      setter?.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  it("페이지를 열면 네이버 연결 상태와 활성 배치를 조회한다", async () => {
    await render();
    const paths = mocks.request.mock.calls.map((call) => call[0]);
    expect(paths).toContain("/naver/status");
    expect(paths).toContain("/scheduled/naver/batches/active");
  });

  // 2026-08-06: '게시 플랫폼' 카드를 없앴다. 게시 대상을 정하는 곳은 1걸음의 소재 줄
  // 하나뿐이고, 간격 방식도 그 선택을 그대로 보낸다.

  it("예약을 시작한 직후에도 작업 큐가 비어 있지 않다", async () => {
    // 2026-08-06 사용자 신고: 작업 큐에 "예약 작업 2건이 생성되었습니다"라는 로그와
    // 빈 표가 나란히 있었다. 큐가 목록 조회(/scheduled/naver/jobs)만 읽었는데, 방금 만든
    // 작업이 아직 거기 담겨 오지 않았다. 활성 배치에는 들어 있으므로 합쳐 본다.
    mocks.request.mockImplementation((path: string) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/threads/status") return Promise.resolve(null);
      if (path === "/scheduled/naver/batches/active") {
        return Promise.resolve(
          view({ status: "RUNNING" }, [
            job({ jobId: "j1", sequence: 0, status: "WAITING", topic: "방금 만든 소재" }),
            job({ jobId: "j2", sequence: 1, status: "WAITING", topic: "그다음 소재" }),
          ]),
        );
      }
      // 목록 조회는 아직 한 박자 늦다.
      if (path === "/scheduled/naver/jobs") return Promise.resolve({ items: [] });
      return Promise.resolve(null);
    });
    await render();
    await openQueue();

    const topics = [...container.querySelectorAll(".scheduled-queue-topic")].map(
      (el) => el.textContent,
    );
    expect(topics).toEqual(["방금 만든 소재", "그다음 소재"]);
    // 탭 배지도 같은 수를 센다.
    expect(container.querySelector(".reservation-tab-count")?.textContent).toBe("2");
  });

  it("작업 큐는 먼저 입력한 소재를 위에 둔다", async () => {
    // 2026-08-06 사용자 신고: GS25 · 세븐일레븐 순으로 넣었는데 큐 맨 위에 세븐일레븐이
    // 섰다. 한 배치의 작업은 createdAt·publishAt이 전부 같아 서버 정렬이 동점이었고,
    // 화면은 두 조회를 이어 붙이면서 순서를 한 번 더 흐트러뜨렸다. 둘 다 고쳤다.
    mocks.request.mockImplementation((path: string) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/threads/status") return Promise.resolve(null);
      const jobs = [
        job({ jobId: "j_gs", sequence: 0, status: "RUNNING", topic: "GS25" }),
        job({ jobId: "j_seven", sequence: 1, status: "WAITING", topic: "세븐일레븐" }),
      ];
      if (path === "/scheduled/naver/batches/active") {
        return Promise.resolve(view({ status: "RUNNING" }, jobs));
      }
      // 서버가 뒤집힌 순서로 내려줘도 화면이 다시 세운다.
      if (path === "/scheduled/naver/jobs") {
        return Promise.resolve({ items: [{ job: jobs[1] }, { job: jobs[0] }] });
      }
      return Promise.resolve(null);
    });
    await render();
    await openQueue();

    const topics = [...container.querySelectorAll(".scheduled-queue-topic")].map((el) =>
      el.textContent?.trim(),
    );
    expect(topics).toEqual(["GS25", "세븐일레븐"]);
  });

  it("작업 큐는 상태 배지만 적고 원고 생성 안쪽의 칸은 적지 않는다", async () => {
    // 2026-08-06에는 '4/4 사실 검수·문장 다듬기'를 배지 아래 덧붙였는데, 2026-08-07
    // 사용자 요청으로 뺐다 — 이 표는 '무엇이 언제 올라가는가'를 보는 자리다.
    const 쓰는중 = job({
      jobId: "j1",
      status: "RUNNING",
      stage: "DRAFT_GENERATION",
      topic: "GS25",
    });
    mocks.request.mockImplementation((path: string) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/threads/status") return Promise.resolve(null);
      if (path === "/scheduled/naver/batches/active") {
        return Promise.resolve(view({ status: "RUNNING" }, [쓰는중]));
      }
      if (path === "/scheduled/naver/jobs") {
        return Promise.resolve({
          items: [
            {
              job: 쓰는중,
              progress: {
                phase: "DRAFT",
                step: 4,
                totalSteps: 4,
                label: "사실 검수·문장 다듬기",
                steps: [
                  "원고 구조 설계",
                  "본문 원고 작성",
                  "카드 이미지 생성",
                  "사실 검수·문장 다듬기",
                ],
                startedAt: "2026-08-06T09:40:00.000Z",
                updatedAt: "2026-08-06T09:46:00.000Z",
              },
            },
          ],
        });
      }
      return Promise.resolve(null);
    });
    await render();
    await openQueue();

    expect(container.querySelector(".scheduled-state")?.textContent).toBe("원고 생성 중");
    expect(container.querySelector(".scheduled-queue-progress")).toBeNull();
    expect(container.textContent).not.toContain("사실 검수·문장 다듬기");
  });

  it("작업 큐는 남은 작업만 상태 배지와 함께 보여 준다", async () => {
    // 2026-08-06 사용자 요청 — 끝난 것(완료·실패·취소)은 '발행 내역'으로 간다.
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({ jobId: "j1", sequence: 0, status: "COMPLETED", topic: "완료된 소재" }),
        job({ jobId: "j2", sequence: 1, status: "RUNNING", stage: "DRAFT_GENERATION" }),
        job({ jobId: "j3", sequence: 2, status: "WAITING", topic: "대기 소재" }),
      ]),
    );
    await render();
    await openQueue();

    const rows = [...container.querySelectorAll(".scheduled-queue tbody tr")];
    expect(rows).toHaveLength(2);
    // 진행 중인 작업은 지금 어느 단계인지까지 보여 준다.
    expect(rows[0].querySelector(".scheduled-state")?.textContent).toBe("원고 생성 중");
    expect(rows[1].querySelector(".scheduled-state")?.textContent).toBe("대기");
    expect(container.textContent).not.toContain("완료된 소재");
  });

  it("진행률은 끝난 작업 수로 계산한다", async () => {
    respond(
      NAVER_CONNECTED,
      view({ totalCount: 4, completedCount: 1, failedCount: 1, canceledCount: 0 }),
    );
    await render();
    await openQueue();
    // (1 + 1 + 0) / 4 = 50%
    expect(container.querySelector(".scheduled-progress-value")?.textContent).toBe("50%");
    expect(
      (container.querySelector(".scheduled-progress-fill") as HTMLElement).style.width,
    ).toBe("50%");
  });

  it("진행 중인 작업은 단계까지 진행률에 반영한다", async () => {
    // 글 단위로만 세면 첫 글이 끝날 때까지 0%에 머문다(2026-08-04 사용자 보고).
    respond(
      NAVER_CONNECTED,
      view({ totalCount: 3, completedCount: 0, failedCount: 0, canceledCount: 0 }, [
        job({ jobId: "job_1", status: "RUNNING", stage: "DRAFT_GENERATION" }),
        job({ jobId: "job_2" }),
        job({ jobId: "job_3" }),
      ]),
    );
    await render();
    await openQueue();
    // (0 + 원고 생성 단계 몫 0.6) / 3 = 20%
    expect(container.querySelector(".scheduled-progress-value")?.textContent).toBe("20%");
  });

  it("대기 중인 작업은 진행률에 보태지 않는다", async () => {
    // 대기 작업의 stage는 기본값(CREATE_POST)이라, 세면 시작도 안 한 작업이 부푼다.
    respond(
      NAVER_CONNECTED,
      view({ totalCount: 3, completedCount: 1, failedCount: 0, canceledCount: 0 }, [
        job({ jobId: "job_1", status: "COMPLETED", stage: "DONE" }),
        job({ jobId: "job_2" }),
        job({ jobId: "job_3" }),
      ]),
    );
    await render();
    await openQueue();
    // 완료 1 / 3 = 33% — 대기 2건은 0으로 친다.
    expect(container.querySelector(".scheduled-progress-value")?.textContent).toBe("33%");
  });

  it("작업이 전부 실패하면 진행률이 100%여도 발행 완료 0건이라고 적는다", async () => {
    // 2026-08-06 사용자 신고 — "발행이 안됬는데 발행됬다고 나와". 진행률은 (완료+실패+
    // 취소)/전체라 두 건 모두 실패한 배치가 100%였고, 막대만 보고 다 됐다고 읽었다.
    // 막대는 그대로 두되 무엇이 몇 건인지를 그 아래에 적는다.
    respond(
      NAVER_CONNECTED,
      view({ totalCount: 2, completedCount: 0, failedCount: 2, canceledCount: 0 }, [
        job({ jobId: "j1", sequence: 0, status: "FAILED", topic: "GS25" }),
        job({ jobId: "j2", sequence: 1, status: "FAILED", topic: "세븐일레븐" }),
      ]),
    );
    await render();
    await openQueue();

    const tally = container.querySelector(".scheduled-tally")?.textContent ?? "";
    expect(tally).toContain("발행 완료 0건");
    expect(tally).toContain("실패 2건");
    expect(tally).toContain("남은 작업 0건");
  });

  it("작업 현황의 개수는 배치 집계가 아니라 실제 작업에서 센다", async () => {
    // 저장된 배치에 어긋난 집계가 그대로 남아 있을 수 있다(작업 2건에 완료 1 · 실패 2).
    // 화면은 눈앞의 작업을 세므로 그 값에 끌려가지 않는다.
    respond(
      NAVER_CONNECTED,
      view({ totalCount: 2, completedCount: 1, failedCount: 2, canceledCount: 0 }, [
        job({ jobId: "j1", sequence: 0, status: "COMPLETED" }),
        job({ jobId: "j2", sequence: 1, status: "WAITING" }),
      ]),
    );
    await render();
    await openQueue();

    const tally = container.querySelector(".scheduled-tally")?.textContent ?? "";
    expect(tally).toContain("발행 완료 1건");
    expect(tally).not.toContain("실패");
    expect(tally).toContain("남은 작업 1건");
    // 완료 1 / 전체 2 = 50%. 어긋난 집계(3/2)를 그대로 썼다면 100%였다.
    expect(container.querySelector(".scheduled-progress-value")?.textContent).toBe("50%");
  });

  it("로그는 서버가 준 것을 최신순으로 보여 준다", async () => {
    respond(
      NAVER_CONNECTED,
      view({
        logs: [
          { at: "2026-08-04T01:00:00.000Z", message: "예약 작업 3건이 생성되었습니다.", tone: "info" },
          { at: "2026-08-04T01:05:00.000Z", message: "네이버 발행이 완료되었습니다.", tone: "success" },
        ],
      }),
    );
    await render();
    await openQueue();
    const lines = [...container.querySelectorAll(".scheduled-log-line")];
    expect(lines).toHaveLength(2);
    expect(lines[0].textContent).toContain("네이버 발행이 완료되었습니다.");
    expect(lines[0].className).toContain("success");
  });

  it("일시정지가 pause API를 부른다", async () => {
    respond(NAVER_CONNECTED, view({ status: "RUNNING" }));
    await render();
    await openQueue();

    await act(async () => button("일시정지").click());
    expect(mocks.request).toHaveBeenCalledWith(
      "/scheduled/naver/batches/batch_1/pause",
      expect.objectContaining({ method: "POST" }),
    );
  });

  describe("새 예약 시작 (예전 '정지')", () => {

    it("확인창에서 취소하면 아무것도 부르지 않는다", async () => {
      vi.stubGlobal("confirm", () => false);
      respond(NAVER_CONNECTED, view({ status: "RUNNING" }));
      await render();
    await openQueue();

      await act(async () => button("새 예약 시작").click());

      const discards = mocks.request.mock.calls.filter(([path]) =>
        String(path).endsWith("/discard"),
      );
      expect(discards).toHaveLength(0);
    });
  });

  it("일시정지된 배치에서는 일시정지 자리가 '계속'이 되고 재개 API를 부른다", async () => {
    // 2026-08-04 사용자 요청: 일시정지 ↔ 계속이 같은 자리의 한 버튼이다.
    respond(NAVER_CONNECTED, view({ status: "PAUSED" }));
    await render();
    await openQueue();

    // 새 배치를 만들지 않는다 — 같은 batchId로 재개해 멈춘 지점부터 잇는다.
    await act(async () => button("계속").click());
    expect(mocks.request).toHaveBeenCalledWith(
      "/scheduled/naver/batches/batch_1/resume",
      expect.objectContaining({ method: "POST" }),
    );
    const posts = mocks.request.mock.calls.filter(
      ([path]) => path === "/scheduled/naver/batches",
    );
    expect(posts).toHaveLength(0);
  });

  it("일시정지를 누르면 버튼이 '계속'으로 바뀌고, 계속을 누르면 '일시정지'로 돌아온다", async () => {
    respond(NAVER_CONNECTED, view({ status: "RUNNING" }));
    await render();
    await openQueue();

    // 제어 응답은 갱신된 배치 뷰다 — 일시정지 응답은 PAUSED를 돌려준다.
    mocks.request.mockResolvedValueOnce(view({ status: "PAUSED" }));
    await act(async () => button("일시정지").click());
    expect(button("계속").disabled).toBe(false);

    mocks.request.mockResolvedValueOnce(view({ status: "RUNNING" }));
    await act(async () => button("계속").click());
    expect(button("일시정지").disabled).toBe(false);
  });

  it("인증이 필요한 배치도 재개할 수 있다", async () => {
    respond(NAVER_CONNECTED, view({ status: "NEEDS_HUMAN" }));
    await render();
    await openQueue();
    expect(button("예약 재개").disabled).toBe(false);
  });

  describe("다음 작업 카운트다운", () => {
    // 화면 시계로 1초마다 세므로 시각을 고정해야 값이 결정된다.
    function countdown() {
      return container.querySelector(".scheduled-countdown");
    }

    it("다음 작업을 기다리는 중이면 남은 시간을 분·초로 보여 준다", async () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-08-04T01:00:00.000Z"));
      respond(
        NAVER_CONNECTED,
        view({ status: "RUNNING", nextRunAt: "2026-08-04T01:04:32.000Z" }),
      );
      await render();
    await openQueue();

      expect(countdown()?.textContent).toBe("다음 원고 생성까지 4분 32초 남음");

      // 실시간이다 — 폴링을 기다리지 않고 1초마다 줄어든다.
      await act(async () => {
        vi.advanceTimersByTime(2000);
      });
      expect(countdown()?.textContent).toBe("다음 원고 생성까지 4분 30초 남음");
    });

    it("작업이 돌고 있는 중에는 보이지 않는다 — 다음 시작이 아니라 지금 진행이다", async () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-08-04T01:00:00.000Z"));
      respond(
        NAVER_CONNECTED,
        view({
          status: "RUNNING",
          currentJobId: "job_1",
          nextRunAt: "2026-08-04T01:04:32.000Z",
        }),
      );
      await render();
    await openQueue();

      expect(countdown()).toBeNull();
    });

    it("일시정지된 배치에는 보이지 않는다 — 재개 전까지 시작되지 않는다", async () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-08-04T01:00:00.000Z"));
      respond(
        NAVER_CONNECTED,
        view({ status: "PAUSED", nextRunAt: "2026-08-04T01:04:32.000Z" }),
      );
      await render();
    await openQueue();

      expect(countdown()).toBeNull();
    });

    it("시각이 지났으면 '곧 시작됩니다'로 바뀐다", async () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-08-04T01:05:00.000Z"));
      respond(
        NAVER_CONNECTED,
        view({ status: "RUNNING", nextRunAt: "2026-08-04T01:04:32.000Z" }),
      );
      await render();
    await openQueue();

      expect(countdown()?.textContent).toBe("다음 원고 생성이 곧 시작됩니다");
    });
  });

  it("활성 배치가 돌면 주기적으로 다시 조회한다", async () => {
    vi.useFakeTimers();
    respond(NAVER_CONNECTED, view({ status: "RUNNING" }));
    await render();
    const before = mocks.request.mock.calls.filter(
      ([path]) => path === "/scheduled/naver/batches/active",
    ).length;

    await act(async () => {
      vi.advanceTimersByTime(2100);
    });
    const after = mocks.request.mock.calls.filter(
      ([path]) => path === "/scheduled/naver/batches/active",
    ).length;
    expect(after).toBeGreaterThan(before);
  });

  it("끝난 배치는 더 이상 폴링하지 않는다", async () => {
    vi.useFakeTimers();
    respond(NAVER_CONNECTED, view({ status: "COMPLETED" }));
    await render();
    const before = mocks.request.mock.calls.filter(
      ([path]) => path === "/scheduled/naver/batches/active",
    ).length;

    await act(async () => {
      vi.advanceTimersByTime(10000);
    });
    const after = mocks.request.mock.calls.filter(
      ([path]) => path === "/scheduled/naver/batches/active",
    ).length;
    expect(after).toBe(before);
  });

  it("화면을 떠나면 폴링 타이머를 정리한다", async () => {
    vi.useFakeTimers();
    respond(NAVER_CONNECTED, view({ status: "RUNNING" }));
    await render();
    act(() => root.unmount());
    const before = mocks.request.mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(10000);
    });
    expect(mocks.request.mock.calls.length).toBe(before);

    // afterEach의 두 번째 unmount를 막는다.
    root = createRoot(document.createElement("div"));
  });

  it("미리보기는 원고가 만들어진 뒤에만 열린다", async () => {
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({ jobId: "j1", sequence: 0, postId: "post_1", generatedAt: undefined }),
        job({
          jobId: "j2",
          sequence: 1,
          postId: "post_2",
          generatedAt: "2026-08-04T01:10:00.000Z",
          // 완료된 작업은 이제 '발행 내역'으로 빠진다 — 여기서는 발행을 기다리는 작업으로.
          status: "READY_TO_PUBLISH",
        }),
      ]),
    );
    await render();
    await openQueue();

    const buttons = [...container.querySelectorAll(".scheduled-preview")] as HTMLButtonElement[];
    // 글은 있지만 원고가 아직 없다 → 볼 것이 없다.
    expect(buttons[0].disabled).toBe(true);
    expect(buttons[1].disabled).toBe(false);

    // 새 미리보기 렌더러를 만들지 않고 기존 '글 보기' 흐름을 그대로 쓴다.
    await act(async () => buttons[1].click());
    expect(mocks.store.openPost).toHaveBeenCalledWith("post_2");
  });

  it("실패한 작업은 발행 내역에서 재시도를 부른다", async () => {
    // 2026-08-06 — 실패도 '끝난 일'이라 작업 큐가 아니라 발행 내역에 있다.
    respond(
      NAVER_CONNECTED,
      view({}, [job({ jobId: "j1", status: "FAILED", errorMessage: "네이버 발행에 실패했습니다." })]),
    );
    await render();
    await act(async () => button("발행 내역").click());
    expect(container.textContent).toContain("네이버 발행에 실패했습니다.");

    await act(async () => button("재시도").click());
    expect(mocks.request).toHaveBeenCalledWith(
      "/scheduled/naver/jobs/j1/retry",
      expect.objectContaining({ method: "POST" }),
    );
  });

  // ------------------------------------------------------------------ 발행 내역
  //
  // 2026-08-06 사용자 요청 — "너무 지저분하고 사용자가 뭘 확인하라는건지 몰라.
  // 직접 깔끔하게 관리할수 있게".

  async function openHistory() {
    await act(async () => button("발행 내역").click());
  }

  it("발행 내역은 셀레니움 스택을 한 줄로 줄이고 원문은 접어 둔다", async () => {
    // 저장돼 있던 실제 사유는 1,520자였고, 그 한 줄이 화면 절반을 붉게 덮었다.
    const 스택 =
      "네이버 자동 발행에 실패했습니다: [InvalidSessionIdException] Message: invalid " +
      "session id: session deleted as the browser has closed the connection " +
      "undetected_chromedriver!GetHandleVerifier [0xd95843+10883] ".repeat(12);
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({ jobId: "j1", status: "FAILED", topic: "선풍기", errorCode: "PUBLISH_FAILED", errorMessage: 스택 }),
      ]),
    );
    await render();
    await openHistory();

    // 요약이 자리를 대신하고, 원문은 아직 화면에 없다.
    expect(container.textContent).toContain("발행 도중 브라우저 창이 닫혀 중단됐습니다");
    expect(container.querySelector(".history-detail")).toBeNull();

    await act(async () => button("자세히").click());
    expect(container.querySelector(".history-detail")?.textContent).toContain(
      "InvalidSessionIdException",
    );
  });

  it("발행 내역은 예정 시각이 아니라 실제 발행 시각과 글 주소를 보여 준다", async () => {
    // 큐의 표를 그대로 쓰던 동안에는 이미 올라간 글의 시각 칸에 '간격에 따라'가 적혀 있었다.
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({
          jobId: "j1",
          status: "COMPLETED",
          topic: "커피",
          postId: "post_1",
          generatedAt: "2026-08-06T07:00:00.000Z",
          publishedAt: "2026-08-06T07:30:02.443Z",
          postUrl: "https://blog.naver.com/wuyung027/224370149776",
        }),
      ]),
    );
    await render();
    await openHistory();

    expect(container.textContent).not.toContain("간격에 따라");
    expect(container.querySelector(".history-time-actual")?.textContent).toBe(
      `게시 ${clockLabel("2026-08-06T07:30:02.443Z")}`,
    );
    const link = container.querySelector<HTMLAnchorElement>(".history-actions a");
    expect(link?.href).toBe("https://blog.naver.com/wuyung027/224370149776");
    expect(link?.textContent).toContain("발행된 글 열기");
  });

  it("절대 시각 예약은 예약 시각과 게시 시각을 나란히 보여 준다", async () => {
    // 2026-08-07 사용자 신고 — 예약 01:34짜리 글이 01:36으로 찍혀 "예약이 안 지켜졌다"로
    // 읽혔다. 예약 시각은 발행을 **시작하는** 시각이고, 게시가 끝나기까지 채널마다
    // 30초~2분이 더 걸린다(실측). 둘을 함께 적어야 그 차이가 설명된다.
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({
          jobId: "j1",
          status: "COMPLETED",
          topic: "삼성전자",
          postId: "post_1",
          publishAt: "2026-08-06T16:34:00.000Z",
          publishedAt: "2026-08-06T16:36:06.052Z",
        }),
      ]),
    );
    await render();
    await openHistory();

    expect(container.querySelector(".history-time-planned")?.textContent).toBe(
      `예약 ${clockLabel("2026-08-06T16:34:00.000Z")}`,
    );
    expect(container.querySelector(".history-time-actual")?.textContent).toBe(
      `게시 ${clockLabel("2026-08-06T16:36:06.052Z")}`,
    );
  });

  it("간격 방식 예약에는 예약 시각 줄이 없다", async () => {
    // publishAt이 없는 배치다. 없는 것을 지어내지 않는다.
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({
          jobId: "j1",
          status: "COMPLETED",
          topic: "커피",
          postId: "post_1",
          publishedAt: "2026-08-06T07:30:00.000Z",
        }),
      ]),
    );
    await render();
    await openHistory();

    expect(container.querySelector(".history-time-planned")).toBeNull();
  });

  it("발행 내역은 소재가 아니라 만들어진 글 제목을 보여 준다", async () => {
    const 완료 = job({
      jobId: "j1",
      status: "COMPLETED",
      topic: "커피",
      postId: "post_1",
      publishedAt: "2026-08-06T07:30:00.000Z",
    });
    mocks.request.mockImplementation((path: string) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/threads/status") return Promise.resolve(null);
      if (path === "/scheduled/naver/batches/active") return Promise.resolve(view({}, [완료]));
      if (path === "/scheduled/naver/jobs") {
        return Promise.resolve({
          items: [{ job: 완료, title: "커피 원두 고르는 법 5가지" }],
        });
      }
      return Promise.resolve(null);
    });
    await render();
    await openHistory();

    expect(container.querySelector(".history-title")?.textContent).toBe(
      "커피 원두 고르는 법 5가지",
    );
    // 어떤 소재로 쓴 글인지도 함께 남긴다.
    expect(container.querySelector(".history-topic")?.textContent).toBe("소재: 커피");
  });

  it("작업이 실패여도 글이 발행돼 있으면 발행 내역이 그렇다고 말한다", async () => {
    // 2026-08-06 사용자 신고 — "발행내역에서는 실패라고 뜨는데 내 글 목록에서는 글이
    // 완성되어 있고 몇 개는 발행까지 됐다". 작업의 상태는 그 실행이 끝났을 때의 마지막
    // 기억이고, 같은 글이 그 뒤에 다른 경로로 발행될 수 있다.
    const 실패 = job({ jobId: "j1", status: "FAILED", topic: "선풍기", postId: "post_1" });
    mocks.request.mockImplementation((path: string) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/threads/status") return Promise.resolve(null);
      if (path === "/scheduled/naver/batches/active") return Promise.resolve(view({}, [실패]));
      if (path === "/scheduled/naver/jobs") {
        return Promise.resolve({
          items: [
            {
              job: 실패,
              title: "선풍기 청소, 분해부터 조립까지",
              postStatus: "POSTED",
              publishedUrl: "https://blog.naver.com/me/1",
            },
          ],
        });
      }
      return Promise.resolve(null);
    });
    await render();
    await openHistory();

    expect(container.querySelector(".history-article-note")?.textContent).toContain(
      "실제로 발행되어 있습니다",
    );
    // 실패한 줄에서도 그 글로 바로 갈 수 있어야 한다.
    const link = container.querySelector<HTMLAnchorElement>(".history-actions a");
    expect(link?.href).toBe("https://blog.naver.com/me/1");
  });

  it("작업이 실패여도 원고가 완성돼 있으면 그렇다고 말한다", async () => {
    const 실패 = job({ jobId: "j1", status: "FAILED", topic: "녹차", postId: "post_1" });
    mocks.request.mockImplementation((path: string) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/threads/status") return Promise.resolve(null);
      if (path === "/scheduled/naver/batches/active") return Promise.resolve(view({}, [실패]));
      if (path === "/scheduled/naver/jobs") {
        return Promise.resolve({
          items: [{ job: 실패, title: "녹차효능 정리", postStatus: "READY_TO_PUBLISH" }],
        });
      }
      return Promise.resolve(null);
    });
    await render();
    await openHistory();

    expect(container.querySelector(".history-article-note")?.textContent).toContain(
      "원고는 완성돼 있습니다",
    );
  });

  it("발행 내역은 결과별로 거를 수 있다", async () => {
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({ jobId: "j1", sequence: 0, status: "COMPLETED", topic: "올라간 소재" }),
        job({ jobId: "j2", sequence: 1, status: "FAILED", topic: "실패한 소재" }),
        job({ jobId: "j3", sequence: 2, status: "CANCELED", topic: "취소한 소재" }),
      ]),
    );
    await render();
    await openHistory();

    expect(container.querySelectorAll(".history-row")).toHaveLength(3);

    await act(async () => {
      const 실패필터 = [...container.querySelectorAll<HTMLButtonElement>(".history-filter")].find(
        (el) => el.textContent?.startsWith("실패"),
      );
      실패필터?.click();
    });

    const rows = [...container.querySelectorAll(".history-row")];
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain("실패한 소재");
  });

  it("발행된 기록도 내역에서 지울 수 있고, 활성 배치 화면을 덮어쓰지 않는다", async () => {
    // 서버는 지운 작업이 속한 **옛 배치**를 돌려준다. 그것을 활성 배치 자리에 넣으면
    // 끝난 배치를 지금 도는 예약으로 착각하고 소재 입력칸까지 덮어쓴다.
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({ jobId: "j1", sequence: 0, status: "COMPLETED", topic: "올라간 소재" }),
      ]),
    );
    await render();
    await openHistory();

    const 지우기 = container.querySelector<HTMLButtonElement>(".history-actions .scheduled-remove");
    await act(async () => 지우기?.click());

    expect(mocks.request).toHaveBeenCalledWith(
      "/scheduled/naver/jobs/j1",
      expect.objectContaining({ method: "DELETE" }),
    );
    // 지운 뒤 목록을 다시 읽는다.
    const 목록조회 = mocks.request.mock.calls.filter(
      ([path]) => path === "/scheduled/naver/jobs",
    );
    expect(목록조회.length).toBeGreaterThan(1);
  });

  // ------------------------------------------------------------------ 작업 삭제
  //
  // 소재 1·2·3을 넣어 두고 2만 빼면 1·3만 이어서 쓰이게 하는 것이 목적이다.

  /** 삭제 버튼은 아이콘만 그린다 — button("텍스트")로는 찾히지 않아 이름표로 집는다. */
  function removeButton(topic: string): HTMLButtonElement {
    const found = [...container.querySelectorAll<HTMLButtonElement>(".scheduled-remove")].find(
      (el) => el.getAttribute("aria-label")?.includes(topic),
    );
    if (!found) throw new Error(`삭제 버튼을 찾지 못했습니다: ${topic}`);
    return found;
  }

  it("대기 중인 작업은 삭제 버튼으로 큐에서 뺀다", async () => {
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({ jobId: "j1", sequence: 0, status: "WAITING", topic: "소재 1" }),
        job({ jobId: "j2", sequence: 1, status: "WAITING", topic: "소재 2" }),
        job({ jobId: "j3", sequence: 2, status: "WAITING", topic: "소재 3" }),
      ]),
    );
    await render();
    await openQueue();

    // 대기 중인 세 줄 모두 뺄 수 있어야 한다.
    expect(container.querySelectorAll(".scheduled-remove")).toHaveLength(3);

    await act(async () => removeButton("소재 2").click());

    expect(mocks.request).toHaveBeenCalledWith(
      "/scheduled/naver/jobs/j2",
      expect.objectContaining({ method: "DELETE" }),
    );
    // 누른 것 하나만 지운다 — 배치를 정지시키거나 옆 작업을 건드리지 않는다.
    const deletes = mocks.request.mock.calls.filter(
      ([, options]) => options?.method === "DELETE",
    );
    expect(deletes.map(([path]) => path)).toEqual(["/scheduled/naver/jobs/j2"]);
  });

  it("발행 중인 작업에는 삭제 버튼이 없다", async () => {
    // 도는 중인 Selenium을 버리면 네이버에 올라갔는지 알 수 없는 글이 남는다.
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({
          jobId: "j2",
          sequence: 1,
          status: "PUBLISHING",
          stage: "NAVER_PUBLISH",
          topic: "지금 올리는 중인 글",
        }),
        job({ jobId: "j3", sequence: 2, status: "WAITING", topic: "아직 안 쓴 글" }),
      ]),
    );
    await render();
    await openQueue();

    const rows = [...container.querySelectorAll(".scheduled-queue tbody tr")];
    expect(rows[0].querySelector(".scheduled-remove")).toBeNull();
    expect(rows[1].querySelector(".scheduled-remove")).not.toBeNull();
  });

  it("이미 올라간 글도 발행 내역에서 지울 수 있다", async () => {
    // 2026-08-06 사용자 요청으로 바뀌었다. 예전에는 여기서 지우기를 막아 뒀는데,
    // 그러면 기록이 쌓인 뒤 화면을 정리할 방법이 아예 없었다. 지워지는 것은 예약 기록
    // 한 줄뿐이고 게시물과 원고는 그대로 남는다(백엔드 테스트가 그것을 본다).
    respond(
      NAVER_CONNECTED,
      view({}, [job({ jobId: "j1", status: "COMPLETED", topic: "이미 올라간 글" })]),
    );
    await render();
    await act(async () => button("발행 내역").click());

    const row = container.querySelector(".history-row") as HTMLElement;
    expect(row.textContent).toContain("이미 올라간 글");
    expect(row.querySelector(".scheduled-remove")).not.toBeNull();
  });

  it("실패한 작업에는 재시도와 지우기가 둘 다 있다", async () => {
    // 다시 시도할지 아예 뺄지는 사용자가 고른다. 하나만 두면 실패한 소재를 지우려고
    // 배치를 통째로 정지해야 한다.
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({
          jobId: "j1",
          status: "FAILED",
          topic: "실패한 소재",
          errorMessage: "네이버 발행에 실패했습니다.",
        }),
      ]),
    );
    await render();
    await act(async () => button("발행 내역").click());

    const row = container.querySelector(".history-row") as HTMLElement;
    expect(row.querySelector(".scheduled-preview")?.textContent?.trim()).toBe("재시도");
    expect(row.querySelector(".scheduled-remove")).not.toBeNull();

    await act(async () => removeButton("실패한 소재").click());
    expect(mocks.request).toHaveBeenCalledWith(
      "/scheduled/naver/jobs/j1",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("삭제 버튼의 이름표가 어느 소재인지 알려 준다", async () => {
    // 아이콘만 있는 버튼이라 이름표가 없으면 스크린리더에서 전부 그냥 '버튼'이 된다.
    respond(
      NAVER_CONNECTED,
      view({}, [
        job({ jobId: "j1", sequence: 0, status: "WAITING", topic: "소재 1" }),
        job({ jobId: "j2", sequence: 1, status: "WAITING", topic: "소재 2" }),
      ]),
    );
    await render();
    await openQueue();

    const labels = [...container.querySelectorAll(".scheduled-remove")].map((el) =>
      el.getAttribute("aria-label"),
    );
    expect(labels).toHaveLength(2);
    expect(labels[0]).toContain("소재 1");
    expect(labels[1]).toContain("소재 2");
    expect(labels[0]).toContain("삭제");
    // 글자가 없다는 것도 함께 못 박는다 — 이름표가 유일한 설명이다.
    expect(container.querySelector(".scheduled-remove")?.textContent?.trim()).toBe("");
  });

  it("삭제한 뒤에는 서버가 준 남은 작업만 이어서 보여 준다", async () => {
    // 사용자가 바라는 결과는 "2를 빼면 1·3만 계속 쓰인다"다. 화면은 서버가 돌려준
    // 뷰를 그대로 반영해야 한다 — 다음 폴링까지 지운 줄이 남아 있으면 안 된다.
    const before = view({ totalCount: 3 }, [
      job({ jobId: "j1", sequence: 0, status: "WAITING", topic: "소재 1" }),
      job({ jobId: "j2", sequence: 1, status: "WAITING", topic: "소재 2" }),
      job({ jobId: "j3", sequence: 2, status: "WAITING", topic: "소재 3" }),
    ]);
    const after = view({ totalCount: 2, targetCount: 2 }, [
      job({ jobId: "j1", sequence: 0, status: "WAITING", topic: "소재 1" }),
      job({ jobId: "j3", sequence: 2, status: "WAITING", topic: "소재 3" }),
    ]);
    // 지운 뒤에는 목록 조회도 남은 작업만 준다 — 서버가 그렇게 답한다.
    let current = before;
    mocks.request.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/scheduled/naver/jobs/j2" && options?.method === "DELETE") {
        current = after;
        return Promise.resolve(after);
      }
      if (path === "/scheduled/naver/jobs") return Promise.resolve(jobList(current));
      return Promise.resolve(current);
    });
    await render();
    await openQueue();

    await act(async () => removeButton("소재 2").click());

    const topics = [...container.querySelectorAll(".scheduled-queue-topic")].map((el) =>
      el.textContent,
    );
    expect(topics).toEqual(["소재 1", "소재 3"]);
  });
  it("확인창에서 취소하면 삭제하지 않는다", async () => {
    // 되돌릴 수 없는 동작이라 한 번 묻는다. '아니오'가 실제로 막아야 의미가 있다.
    vi.stubGlobal("confirm", () => false);
    respond(
      NAVER_CONNECTED,
      view({}, [job({ jobId: "j1", status: "WAITING", topic: "소재 1" })]),
    );
    await render();
    await openQueue();

    await act(async () => removeButton("소재 1").click());

    const deletes = mocks.request.mock.calls.filter(
      ([, options]) => options?.method === "DELETE",
    );
    expect(deletes).toHaveLength(0);
  });
  // ------------------------------------------------------ 소재 하나로 여러 편


  // ------------------------------------------------------ 소재 입력 UI

  // ------------------------------------------------ 발행 시각 예약(2026-08-05)






  // ------------------------------- 발행 시간 목록(2026-08-05)
  //
  // 브라우저 기본 목록은 시와 분을 다 골라도 닫히지 않아, 값이 들어갔는지 확인하려면
  // 다른 곳을 눌러 목록을 치워야 했다. 목록을 직접 그리는 이유가 그것이다.




  it("2걸음 하단에 요약 바가 없다 — 이전 단계·시작 버튼이 화면 아래에 뜨지 않는다", async () => {
    // 2026-08-07 사용자 결정. 시작은 카드의 요약 줄에서, 뒤로 가기는 진행 표시와
    // 왼쪽 카드의 '이전 단계로 돌아가세요'가 맡는다.
    await render();
    await goToStep(2);

    expect(container.querySelector(".reservation-bottom")).toBeNull();
    expect(
      [...container.querySelectorAll("button")].some(
        (element) => element.textContent?.trim() === "이전 단계",
      ),
    ).toBe(false);
  });


  it("날짜·시각 지정에서는 게시 플랫폼 카드와 작업 간격 칸이 보이지 않는다", async () => {
    await render();
    await type("소재 A");
    await goToStep(2);

    // 플랫폼은 줄마다 고르므로 같은 선택을 두 곳에서 받지 않는다.
    expect(container.querySelector(".scheduled-platform-row")).toBeNull();
    // 원고 작업 간격은 서버 사정이라 기본값으로 두고 화면에서 감춘다.
    expect(container.querySelector("#scheduled-interval-minutes")).toBeNull();
    expect(container.querySelector("#scheduled-target")).toBeNull();
  });

  // -------------------- 예약 확인 걸음을 없앤 뒤(2026-08-06 사용자 요청)
  //
  // "3번째 페이지에서는 바로 작업큐로 가게 만들어. 새예약 페이지는 같은 내용이라 없어도
  // 될것 같고 스케쥴 시작 UI를 작업큐에 같이 넣는거야."
  //
  // 확인 표(.review-*)와 예약 요약 카드는 화면에서 빠졌다. 그 카드들이 하던 일 가운데
  // 남길 것은 두 가지이고, 둘 다 이제 2걸음에 있다: 줄을 빼는 것과, 뺀 결과가 시작을
  // 막는 것.

  it("확인 표와 예약 요약 카드는 더 이상 없다", async () => {
    respond(NAVER_CONNECTED, null, { saved: true, savedUsername: "boo", hasSession: true });
    await render();
    await type("소재 A\n소재 B");
    await goToStep(2);

    expect(container.querySelector(".review-table")).toBeNull();
    expect(container.querySelector(".review-summary")).toBeNull();
    const titles = [...container.querySelectorAll(".panel-title")].map((title) =>
      title.textContent?.trim(),
    );
    expect(titles).not.toContain("3. 예약 일정 확인");
    expect(titles).not.toContain("예약 요약");
  });

  it("예약이 도는 중에는 작업 큐 맨 위가 제어 카드다", async () => {
    // 제어 카드를 다른 화면에 두면 도는 예약을 손댈 길이 큐에서 사라진다.
    respond(NAVER_CONNECTED, view({ status: "RUNNING" }, [job()]));
    await render();
    await openQueue();

    // 일이 놓이는 것은 **왼쪽 칸**이다. 오른쪽은 도우미가 선다(2026-08-12).
    const titles = [
      ...container.querySelectorAll(".reservation-column:first-child .panel-title"),
    ].map((title) => title.textContent?.trim());
    expect(titles).toEqual(["스케줄 시작", "작업 큐", "작업 현황"]);

    // 도우미는 일의 순서에 끼어들지 않는다 — 옆 칸에서 읽는 것이다.
    const aside = container.querySelector(".reservation-column:last-child .panel-title");
    expect(aside?.textContent?.trim()).toBe("도우미");
  });

  it("예약 목록 카드는 없다 — 작업 큐가 그 자리를 대신한다", async () => {
    await render();

    const titles = [...container.querySelectorAll(".panel-title")].map((title) =>
      title.textContent?.trim(),
    );
    expect(titles).not.toContain("예약 목록");
    // **조회는 한다.** 예전에는 카드를 지우면서 조회까지 뺐는데, 그 바람에 작업 큐가
    // 활성 배치만 보게 되어 배치가 끝나는 순간 통째로 비었다(2026-08-06). 카드는 그대로
    // 없고, 그 조회 결과는 작업 큐·발행 내역 탭이 읽는다.
    expect(mocks.request.mock.calls.map((call) => call[0])).toContain(
      "/scheduled/naver/jobs",
    );
  });

  it("작업 큐는 각 작업의 예약 시각을 함께 보여 준다", async () => {
    respond(
      NAVER_CONNECTED,
      view({ scheduleMode: "absolute" }, [
        job({ publishAt: "2026-08-06T06:00:00.000Z" }),
      ]),
    );
    await render();
    await openQueue();

    const queue = container.querySelector("[aria-labelledby='scheduled-queue-title']")!;
    // 로컬 시간으로 적는다 — 어느 시간대에서 돌려도 '월 일(요일)' 꼴이 나온다.
    expect(queue.textContent).toMatch(/\d+월 \d+일\(.\)/);
  });

  // ------------------- 배치가 끝나도 작업 큐·발행 내역은 남는다(2026-08-06)
  //
  // 사용자 신고: "예약 시작 누르고 기다리는데 갑자기 튕겨지면서 작업큐와 발행 내역에서
  // 만든 글이 사라짐". 배치가 COMPLETED가 되면 /batches/active가 null을 주는데, 두 탭이
  // 그 응답만 읽고 있었다. 이제 '내 예약 전부'(/scheduled/naver/jobs)를 읽는다.

  it("배치가 끝나 활성 배치가 없어도 남은 예약이 작업 큐에 보인다", async () => {
    mocks.request.mockImplementation((path: string) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/threads/status") return Promise.resolve(THREADS_CONNECTED);
      // 끝난 배치라 활성 배치는 없다.
      if (path === "/scheduled/naver/batches/active") return Promise.resolve(null);
      if (path === "/scheduled/naver/jobs") {
        return Promise.resolve({
          items: [
            { job: job({ topic: "AI 툴 소개", status: "WAITING" }) },
            {
              job: job({
                jobId: "job_2",
                sequence: 1,
                topic: "Claude 사용법",
                status: "RUNNING",
                stage: "DRAFT_GENERATION",
              }),
            },
            {
              job: job({
                jobId: "job_3",
                sequence: 2,
                topic: "이미 올라간 글",
                status: "COMPLETED",
                stage: "DONE",
              }),
            },
          ],
        });
      }
      return Promise.resolve(null);
    });
    await render();
    await openQueue();

    const queue = container.querySelector("[aria-labelledby='scheduled-queue-title']")!;
    expect(queue.textContent).toContain("AI 툴 소개");
    expect(queue.textContent).toContain("Claude 사용법");
    // 끝난 것은 '발행 내역'으로 빠진다. 배지 숫자도 남은 일의 수(2)다.
    expect(queue.textContent).not.toContain("이미 올라간 글");
    expect(queue.textContent).not.toContain("예약을 시작하면 작업이 여기에 표시됩니다");
  });

  it("발행 내역은 끝난 작업(완료·실패)을 보여 준다", async () => {
    mocks.request.mockImplementation((path: string) => {
      if (path === "/naver/status") return Promise.resolve(NAVER_CONNECTED);
      if (path === "/threads/status") return Promise.resolve(THREADS_CONNECTED);
      if (path === "/scheduled/naver/batches/active") return Promise.resolve(null);
      if (path === "/scheduled/naver/jobs") {
        return Promise.resolve({
          items: [
            { job: job({ topic: "올라간 글", status: "COMPLETED", stage: "DONE" }) },
            { job: job({ jobId: "job_2", topic: "실패한 글", status: "FAILED" }) },
          ],
        });
      }
      return Promise.resolve(null);
    });
    await render();
    await act(async () => button("발행 내역").click());

    // 2026-08-06 — 발행 내역은 큐의 표를 떠나 자기 화면(PublishHistoryCard)을 갖는다.
    const history = container.querySelector("[aria-labelledby='publish-history-title']")!;
    expect(history.textContent).toContain("올라간 글");
    // 실패도 '끝난 일'이라 여기 있다(재시도도 여기서 누른다).
    expect(history.textContent).toContain("실패한 글");
  });

  // -------------------------------- 작업 큐의 '플랫폼' 칸(2026-08-06)

  it("쓰레드에만 올리는 작업은 큐에서도 Threads만 적는다", async () => {
    respond(
      NAVER_CONNECTED,
      view({ publishNaver: false, publishThreads: true }, [
        job({ publishNaver: false, publishThreads: true }),
      ]),
    );
    await render();
    await openQueue();

    const cell = container.querySelector(".scheduled-queue-platform")!;
    expect(cell.textContent).toContain("Threads");
    // 고르지 않은 플랫폼을 적으면 화면이 실제 발행과 다른 말을 한다.
    expect(cell.textContent).not.toContain("Naver");
  });

  it("publishNaver가 없는 옛 작업은 네이버로 읽는다", async () => {
    // 그때는 네이버가 언제나 발행 대상이었다 — 그 배치는 지금도 네이버에 올린다.
    respond(NAVER_CONNECTED, view({}, [job({ publishThreads: true })]));
    await render();
    await openQueue();

    const cell = container.querySelector(".scheduled-queue-platform")!;
    expect(cell.textContent).toContain("Naver");
    expect(cell.textContent).toContain("Threads");
  });

  it("플랫폼이 둘이면 덩어리로 나뉜다 — 넘쳐서 옆 칸을 덮지 않게", async () => {
    // 표가 table-layout: fixed라 넘친 글자는 '발행 시각' 위에 겹쳐 찍혔다.
    // 표식+이름을 한 덩어리로 묶어야 그 사이에서 줄을 바꿀 수 있다.
    respond(
      NAVER_CONNECTED,
      view({}, [job({ publishNaver: true, publishThreads: true })]),
    );
    await render();
    await openQueue();

    const chunks = container.querySelectorAll(".scheduled-queue-channel");
    expect(chunks).toHaveLength(2);
    // 표식(네이버는 'N' 글자)과 이름이 **한 덩어리** 안에 함께 있어야 한다.
    expect(chunks[0].textContent).toContain("Naver");
    expect(chunks[0].querySelector(".scheduled-mark")).not.toBeNull();
    expect(chunks[1].textContent).toContain("Threads");
    expect(chunks[1].querySelector(".scheduled-mark")).not.toBeNull();
  });
  // ------------------------------------ 1걸음 소재·플랫폼 선택 화면(2026-08-05)
  // ------------------------------------------- 소재별 플랫폼 선택(2026-08-05)

  const THREADS_CONNECTED = { saved: true, savedUsername: "boo", hasSession: true };

  it("소재 목록 위에 '모든 소재에 일괄 적용' 줄은 없다", async () => {
    // 2026-08-05 사용자 요청으로 뺐다 — 같은 선택을 줄과 일괄 줄 두 곳에서 받던 자리다.
    await render();
    await type("소재 A\n소재 B");

    expect(container.querySelector(".platform-bulk")).toBeNull();
    expect(container.textContent).not.toContain("모든 소재에 플랫폼 일괄 적용");
  });

  // ------------------------- 줄 번호와 소재 순서의 어긋남(2026-08-05)
  //
  // 플랫폼 선택은 빈 줄·중복을 뺀 **소재 순서**로 저장되는데, 화면은 **줄 번호**로
  // 그것을 고치고 있었다. 위에서부터 빈 줄 없이 채우면 두 번호가 같아 드러나지 않지만,
  // 아래 상황에서는 선택이 다른 소재에 붙거나 사라졌다.
});
