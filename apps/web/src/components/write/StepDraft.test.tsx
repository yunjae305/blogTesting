import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import type { BlogTask } from "../../api/types";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  store: {
    task: null as BlogTask | null,
    followTask: vi.fn(),
    setTask: vi.fn(),
    setStep: vi.fn(),
    // 이 화면의 시험 대상은 **생성이 도는 동안**의 동작이다. 검증에서 넘어온 것과
    // 같은 상태로 둔다(2026-08-12에 목록에서 연 글은 저절로 시작하지 않게 됐다).
    draftAutoStart: true,
    setDraftAutoStart: vi.fn(),
    showToast: vi.fn(),
    reportError: vi.fn(),
  },
}));

vi.mock("../../api/client", async () => {
  const actual = await vi.importActual<typeof import("../../api/client")>("../../api/client");
  return { request: mocks.request, ApiError: actual.ApiError };
});
vi.mock("../../store", () => ({ useStore: () => mocks.store }));

import { StepDraft } from "./StepDraft";

function task(status: BlogTask["status"], extra: Partial<BlogTask> = {}): BlogTask {
  return { postId: "post_1", status, ...extra } as BlogTask;
}

describe("StepDraft", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("resumes polling without starting a second generation after re-entry", async () => {
    const completed = task("READY_TO_PUBLISH", {
      finalPost: { title: "완성 원고" } as BlogTask["finalPost"],
    });
    mocks.store.task = task("GENERATING", {
      progress: {
        phase: "DRAFT",
        step: 3,
        totalSteps: 4,
        label: "카드 이미지 생성",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt: "2026-07-28T00:00:00Z",
        updatedAt: "2026-07-28T00:04:00Z",
      },
    });
    mocks.store.followTask.mockResolvedValue(completed);

    await act(async () => {
      root.render(<StepDraft />);
    });

    expect(mocks.store.followTask).toHaveBeenCalledOnce();
    expect(mocks.store.followTask).toHaveBeenCalledWith("post_1");
    expect(mocks.request).not.toHaveBeenCalled();
    expect(mocks.store.showToast).toHaveBeenCalledWith("원고를 만들었습니다.");
  });

  it("shows how far along the run is while it is still generating", async () => {
    // 서버는 3/4단계에 있다고만 말한다. 화면은 그 안에서도 얼마나 왔는지 보여야 한다.
    const startedAt = new Date(Date.now() - 100_000).toISOString();
    const updatedAt = new Date(Date.now() - 20_000).toISOString();
    mocks.store.task = task("GENERATING", {
      progress: {
        phase: "DRAFT",
        step: 3,
        totalSteps: 4,
        label: "카드 이미지 생성",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt,
        updatedAt,
      },
    });
    // 이 테스트가 보는 것은 렌더 결과다 — 폴링은 끝나지 않은 채로 둔다.
    mocks.store.followTask.mockReturnValue(new Promise(() => {}));

    await act(async () => {
      root.render(<StepDraft />);
    });

    const bar = container.querySelector('[role="progressbar"]');
    const percent = Number(bar?.getAttribute("aria-valuenow"));

    // 앞선 두 단계는 끝났으므로 그 몫만큼은 확정이고, 아직 100%는 아니다.
    // 두 칸의 몫은 실측 가중치에서 나온다(115+50 / 414 ≈ 40%) — 2026-08-11에 상수를
    // 실측으로 바꾸면서 이 숫자도 함께 움직였다.
    expect(percent).toBeGreaterThan(35);
    expect(percent).toBeLessThan(100);
    // "1/4 · 지금 하는 일" 요약 줄은 없다(2026-08-10 사용자 요청) — 작업 현황 로그와
    // 단계 표시줄이 이미 같은 말을 한다. 현재 단계는 표시줄의 aria-current로 확인한다.
    expect(container.textContent).not.toContain("3/4 ·");
    expect(
      container.querySelector('.generate-stepper .step[aria-current="step"]')?.textContent,
    ).toContain("카드 이미지 생성");
    // 단계 표시줄은 상단 위저드와 같은 문법이다(2026-08-10) — 네 단계가 한 줄로 보인다.
    expect(container.querySelectorAll(".generate-stepper .step")).toHaveLength(4);
  });

  it("작업 현황 로그가 줄 단위로 보이고 마지막 줄이 강조된다", async () => {
    // 2026-08-10 사용자 요청 — 기다리는 동안 지금 무슨 일이 도는지 터미널 로그처럼 보여
    // 준다. 줄은 status 폴링이 병합해 준 task.activityLog에서 온다.
    mocks.store.task = task("GENERATING", {
      progress: {
        phase: "DRAFT",
        step: 1,
        totalSteps: 4,
        label: "원고 구조 설계",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      },
      activityLog: [
        { at: "2026-08-10T00:00:00.000Z", message: "원고 생성을 시작했어요" },
        { at: "2026-08-10T00:00:01.000Z", message: "서론·본론·결론 뼈대를 짜는 중이에요…" },
      ],
    });
    mocks.store.followTask.mockReturnValue(new Promise(() => {}));

    vi.useFakeTimers();
    try {
      await act(async () => {
        root.render(<StepDraft />);
      });

      expect(container.textContent).toContain("작업 현황");
      // 같은 폴링에 여러 줄이 실려 와도 한꺼번에 뜨지 않는다(2026-08-10 사용자 지적) —
      // 한 줄씩 차례로 드러난다.
      expect(container.querySelectorAll(".generate-activity-list li")).toHaveLength(0);
      await act(async () => {
        vi.advanceTimersByTime(400);
      });
      expect(container.querySelectorAll(".generate-activity-list li")).toHaveLength(1);
      await act(async () => {
        vi.advanceTimersByTime(400);
      });
      const lines = container.querySelectorAll(".generate-activity-list li");
      expect(lines).toHaveLength(2);
      expect(lines[1].className).toContain("latest");
      expect(lines[1].textContent).toContain("서론·본론·결론 뼈대를 짜는 중이에요…");
    } finally {
      vi.useRealTimers();
    }
  });

  it("fills the bar only once the draft actually exists", async () => {
    mocks.store.task = task("READY_TO_PUBLISH", {
      finalPost: { title: "완성 원고" } as BlogTask["finalPost"],
    });

    await act(async () => {
      root.render(<StepDraft />);
    });

    const bar = container.querySelector('[role="progressbar"]');

    expect(bar?.getAttribute("aria-valuenow")).toBe("100");
    expect(container.textContent).toContain("원고 생성 완료");
  });

  it("marks every stage as done / current / pending exactly once per state", async () => {
    mocks.store.task = task("GENERATING", {
      progress: {
        phase: "DRAFT",
        step: 3,
        totalSteps: 4,
        label: "카드 이미지 생성",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt: new Date(Date.now() - 100_000).toISOString(),
        updatedAt: new Date(Date.now() - 20_000).toISOString(),
      },
    });
    mocks.store.followTask.mockReturnValue(new Promise(() => {}));

    await act(async () => {
      root.render(<StepDraft />);
    });

    // 3단계가 진행 중이면 앞의 둘은 완료, 뒤의 하나는 대기다.
    expect(container.querySelectorAll(".generate-stage-item--done")).toHaveLength(2);
    expect(container.querySelectorAll(".generate-stage-item--current")).toHaveLength(1);
    expect(container.querySelectorAll(".generate-stage-item--pending")).toHaveLength(1);
    expect(
      container.querySelector(".generate-stage-item--current")?.getAttribute("aria-current"),
    ).toBe("step");
    // 상태는 색이 아니라 글자로도 갈린다.
    expect(container.querySelector(".generate-stage-item--pending")?.textContent).toContain(
      "대기 중",
    );
    expect(container.textContent).toContain("2/4 단계 완료");
    expect(container.textContent).toContain("AI가 초안을 만들고 있어요");
    expect(container.querySelector(".generate-tip")).not.toBeNull();
    // 진행 중 기본 안내는 두지 않는다 — 진행률·단계 이름·배지가 이미 하는 말이라
    // 자리만 차지했다(2026-08-07 사용자 결정). 빈 문단도 남기지 않는다.
    expect(container.textContent).not.toContain("조금만 기다려주세요");
    expect(container.querySelector(".panel-subtitle")).toBeNull();
  });

  it("switches the board to its finished face when the draft lands", async () => {
    mocks.store.task = task("READY_TO_PUBLISH", {
      finalPost: { title: "완성 원고" } as BlogTask["finalPost"],
      progress: {
        phase: "DRAFT",
        step: 4,
        totalSteps: 4,
        label: "사실 검수·문장 다듬기",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt: new Date(Date.now() - 100_000).toISOString(),
        updatedAt: new Date(Date.now() - 5_000).toISOString(),
      },
    });

    await act(async () => {
      root.render(<StepDraft />);
    });

    expect(container.querySelector(".panel-title")?.textContent).toBe("원고 생성 완료");
    expect(container.querySelectorAll(".generate-stage-item--done")).toHaveLength(4);
    // 끝나면 도중의 단계 카운트("4/4 단계 완료") 대신 실제로 걸린 총 시간을 보여준다.
    expect(container.textContent).not.toContain("단계 완료");
    expect(container.textContent).toContain("총 소요 시간");
    // 다 끝난 화면에서 "기다려 주세요"는 남아 있으면 안 된다.
    expect(container.querySelector(".generate-tip")).toBeNull();
    expect(container.querySelector("#goPublish")).not.toBeNull();
  });

  it("stops the board where it failed and offers a retry", async () => {
    mocks.store.task = task("FAILED", {
      progress: {
        phase: "DRAFT",
        step: 2,
        totalSteps: 4,
        label: "본문 원고 작성",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt: new Date(Date.now() - 100_000).toISOString(),
        updatedAt: new Date(Date.now() - 20_000).toISOString(),
      },
    });

    await act(async () => {
      root.render(<StepDraft />);
    });

    expect(container.querySelector(".panel-title")?.textContent).toBe("원고 생성 실패");
    expect(container.querySelectorAll(".generate-stage-item--error")).toHaveLength(1);
    // 요약 줄("N/4 · …", 멈춤 문구)은 없앴다(2026-08-10) — 멈춤은 부제가 말한다.
    expect(container.textContent).toContain("지금은 아무 작업도 진행되고 있지 않습니다");
    expect(container.querySelector("#retryDraft")).not.toBeNull();
    // 멈춘 화면이 계속 도는 것처럼 보이면 안 된다.
    expect(container.querySelector(".generate-tip--slow")).toBeNull();
    expect(container.textContent).not.toContain("잠시만 기다려 주세요");
    // 실패했다고 해서 스스로 다시 돌리지는 않는다.
    expect(mocks.request).not.toHaveBeenCalled();
  });

  it("says the run stopped when the server quietly went back to INTENT_SELECTED", async () => {
    // 서버가 재시작하면 복구 스위퍼가 GENERATING을 INTENT_SELECTED로 되돌리고 진행 상황을
    // 지운다. 예전에는 이 화면이 0%인 채로 영원히 '생성 준비 중'이었다 — 이미 시작한 뒤라
    // 자동 재시작도 없고, 다시 시도 버튼은 FAILED에만 있었기 때문이다.
    mocks.store.task = task("GENERATING", {
      progress: {
        phase: "DRAFT",
        step: 2,
        totalSteps: 4,
        label: "본문 원고 작성",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt: new Date(Date.now() - 100_000).toISOString(),
        updatedAt: new Date(Date.now() - 20_000).toISOString(),
      },
    });
    // 폴링은 '더 이상 돌지 않는 글'을 그대로 돌려준다(원고 없음).
    mocks.store.followTask.mockResolvedValue(task("INTENT_SELECTED"));

    await act(async () => {
      root.render(<StepDraft />);
    });
    // 폴링 결과가 화면 상태로 반영된다.
    await act(async () => {
      mocks.store.task = task("INTENT_SELECTED");
      root.render(<StepDraft />);
    });

    expect(container.querySelector(".panel-title")?.textContent).toBe("원고 생성 멈춤");
    expect(container.textContent).toContain("지금은 아무 작업도 진행되고 있지 않습니다");
    expect(container.querySelector(".generate-tip--stopped")?.textContent).toContain(
      "서버에서 생성이 중단되었습니다",
    );
    // 사용자가 곧바로 다시 시작할 수 있어야 한다.
    expect(container.querySelector("#retryDraft")).not.toBeNull();
    // '기다려 주세요'가 남아 있으면 도는 중인지 멈춘 것인지 다시 헷갈린다.
    expect(container.textContent).not.toContain("잠시만 기다려 주세요");
  });

  it("retries from the stopped state with a fresh generate request", async () => {
    mocks.store.task = task("GENERATING", {
      progress: {
        phase: "DRAFT",
        step: 1,
        totalSteps: 4,
        label: "원고 구조 설계",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt: new Date(Date.now() - 60_000).toISOString(),
        updatedAt: new Date(Date.now() - 10_000).toISOString(),
      },
    });
    mocks.store.followTask.mockResolvedValue(task("INTENT_SELECTED"));

    await act(async () => {
      root.render(<StepDraft />);
    });
    await act(async () => {
      mocks.store.task = task("INTENT_SELECTED");
      root.render(<StepDraft />);
    });

    const started = task("GENERATING");
    mocks.request.mockResolvedValue(started);
    mocks.store.followTask.mockResolvedValue(
      task("READY_TO_PUBLISH", { finalPost: { title: "완성" } as BlogTask["finalPost"] }),
    );

    await act(async () => {
      container.querySelector<HTMLButtonElement>("#retryDraft")?.click();
    });

    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1/draft/generate", {
      method: "POST",
      body: { format: "html" },
    });
    expect(mocks.store.showToast).toHaveBeenCalledWith("원고를 만들었습니다.");
  });

  it("오래 그대로인 단계에서는 사실만 말하고 다시 시작할 길을 준다", async () => {
    mocks.store.task = task("GENERATING", {
      progress: {
        phase: "DRAFT",
        step: 3,
        totalSteps: 4,
        label: "카드 이미지 생성",
        steps: ["원고 구조 설계", "본문 원고 작성", "카드 이미지 생성", "사실 검수·문장 다듬기"],
        startedAt: new Date(Date.now() - 400_000).toISOString(),
        // 이 단계에 4분 넘게 머물러 있다.
        updatedAt: new Date(Date.now() - 250_000).toISOString(),
      },
    });
    mocks.store.followTask.mockReturnValue(new Promise(() => {}));

    await act(async () => {
      root.render(<StepDraft />);
    });

    expect(container.textContent).toContain("예상보다 오래 걸리는 중");
    // **'아직 작업 중'이라고 단정하지 않는다.** 서버가 재시작하면 잡은 죽는데 글은
    // '생성 중'에 남는다 — 화면은 그 둘을 구분할 수 없다(2026-08-06 신고: 진행 기록이
    // 13분째 멈춰 있는데 화면은 돌고 있다고 말했다).
    const 안내 = container.querySelector(".generate-tip--slow")?.textContent ?? "";
    expect(안내).toContain("그대로예요");
    expect(안내).toContain("서버가 다시 시작돼 멈춘 것일 수도 있어요");
    // 이제 다시 시작할 수 있다. 서버가 아무도 돌리지 않는 GENERATING을 되살리므로
    // 누를 수 있는데 실패하는 버튼이 아니다(2026-08-06).
    expect(container.querySelector("#retryDraft")).not.toBeNull();
    expect(container.querySelector(".panel-title")?.textContent).toBe("원고 생성 중");
  });

  it("does not call a normal start 'stopped' while the POST is still in flight", async () => {
    // 실사례(2026-08-03): 멈춤 판정이 startedRef(중복 방지용 표시)에 걸려 있어서,
    // generate()가 POST를 보내기 전에 세우는 그 ref 때문에 202가 돌아오기까지의
    // **정상 구간 내내** 화면이 "원고 생성 멈춤 / 확인 필요 / 0% / 다시 시도해 주세요"를
    // 보여 줬다. 그러면서 그 버튼은 busy라 눌리지도 않았다.
    mocks.store.task = task("INTENT_SELECTED");
    // 202가 아직 안 왔다 — 이 구간이 문제였던 자리다.
    mocks.request.mockReturnValue(new Promise(() => {}));

    await act(async () => {
      root.render(<StepDraft />);
    });

    expect(container.querySelector(".panel-title")?.textContent).not.toBe("원고 생성 멈춤");
    expect(container.textContent).not.toContain("진행이 멈췄습니다");
    expect(container.querySelector("#retryDraft")).toBeNull();
    // 생성 요청은 실제로 나갔다.
    expect(mocks.request).toHaveBeenCalledWith("/posts/post_1/draft/generate", {
      method: "POST",
      body: { format: "html" },
    });
  });

  it("follows the running job instead of erroring when the server says it is already generating", async () => {
    // 서버는 이미 이 글을 쓰고 있어 409로 거절한다("이 글은 이미 원고를 생성하고 있습니다").
    // 그건 오류가 아니라 진행 중이라는 소식이다 — 예전에는 토스트만 띄우고 폴링을 붙이지
    // 않아서 화면이 멈춤에 남았고, 사용자는 다시 누르고 또 409를 받았다(409 세 번 연속).
    const completed = task("READY_TO_PUBLISH", {
      finalPost: { title: "완성 원고" } as BlogTask["finalPost"],
    });
    mocks.store.task = task("INTENT_SELECTED");
    mocks.request.mockImplementation((path: string) => {
      if (path.endsWith("/draft/generate")) {
        return Promise.reject(new ApiError("이미 생성 중", 409, "INVALID_STATUS_TRANSITION"));
      }
      return Promise.resolve(task("GENERATING"));
    });
    mocks.store.followTask.mockResolvedValue(completed);

    await act(async () => {
      root.render(<StepDraft />);
    });

    // 409를 오류로 알리지 않는다.
    expect(mocks.store.reportError).not.toHaveBeenCalled();
    // 돌고 있는 작업에 다시 붙는다.
    expect(mocks.store.followTask).toHaveBeenCalledWith("post_1");
    expect(mocks.store.showToast).toHaveBeenCalledWith("원고를 만들었습니다.");
  });

  it("says why it stopped when the start itself fails", async () => {
    // 시작이 실패하면 사유를 남겨야 다시 시도 버튼이 나온다. 예전에는 startedRef가
    // 그 구멍을 덮고 있었는데, 그 덮개가 정상 구간까지 멈춤으로 만들던 원인이었다.
    mocks.store.task = task("INTENT_SELECTED");
    mocks.request.mockRejectedValue(new ApiError("서버 오류", 500));

    await act(async () => {
      root.render(<StepDraft />);
    });

    expect(mocks.store.reportError).toHaveBeenCalled();
    expect(container.querySelector(".panel-title")?.textContent).toBe("원고 생성 멈춤");
    expect(container.querySelector("#retryDraft")).not.toBeNull();
  });
});

/**
 * 목록에서 반쯤 만든 글을 열었을 때(2026-08-12 사용자 신고).
 *
 * 예전에는 이 화면에 오기만 하면 원고 생성이 시작됐다. 그래서 서버가 꺼졌다 켜진 뒤
 * 그 글을 열면 중단된 자리에서 저절로 이어 돌았고, 새 글을 쓰는 동안 옛 글의 생성이
 * 함께 도는 일이 생겼다.
 */
describe("StepDraft 목록에서 연 글", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.draftAutoStart = false;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("저절로 생성을 시작하지 않는다", async () => {
    mocks.store.task = task("INTENT_SELECTED");

    await act(async () => root.render(<StepDraft />));

    expect(mocks.request).not.toHaveBeenCalled();
  });

  it("대신 '원고 생성 시작' 버튼을 준다 — 없으면 0% 앞에서 할 일이 없다", async () => {
    mocks.store.task = task("INTENT_SELECTED");

    await act(async () => root.render(<StepDraft />));

    expect(container.querySelector("#startDraft")).not.toBeNull();
  });

  it("그 버튼을 누르면 그때 시작한다", async () => {
    mocks.store.task = task("INTENT_SELECTED");
    mocks.request.mockResolvedValue(task("GENERATING"));

    await act(async () => root.render(<StepDraft />));
    await act(async () => {
      container.querySelector<HTMLButtonElement>("#startDraft")!.click();
    });

    expect(mocks.request).toHaveBeenCalled();
  });

  it("서버가 이미 만들고 있으면 버튼 없이 따라붙는다", async () => {
    // 도는 일을 화면이 모른 척하면 진행 상황을 볼 수 없다. 따라붙는 것은 새 일을
    // 시작하지 않으므로 자동 시작 여부와 무관하다.
    mocks.store.task = task("GENERATING");
    mocks.store.followTask.mockResolvedValue(task("READY_TO_PUBLISH"));

    await act(async () => root.render(<StepDraft />));

    expect(mocks.store.followTask).toHaveBeenCalledWith("post_1");
    expect(container.querySelector("#startDraft")).toBeNull();
  });
});
