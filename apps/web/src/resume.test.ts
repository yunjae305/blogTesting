import { describe, expect, it } from "vitest";

import type { BlogTask, BlogTaskStatus, FinalPost } from "./api/types";
import { resumeActionLabel, resumeStep, writingTabAction, WRITE_STEP } from "./resume";

function task(status: BlogTaskStatus, extra: Partial<BlogTask> = {}): BlogTask {
  return {
    postId: "post_1",
    userId: "user_1",
    status,
    version: 1,
    createdAt: "2026-07-29T00:00:00.000Z",
    updatedAt: "2026-07-29T00:00:00.000Z",
    statusHistory: [],
    input: { topic: "AIONA", keywords: [], referenceMaterials: [] },
    postingLogs: [],
    ...extra,
  } as BlogTask;
}

const FINAL_POST = {
  title: "완성된 제목",
  body: "본문",
  hashtags: [],
  htmlContent: "<p>본문</p>",
} as FinalPost;

describe("resumeStep", () => {
  it("소재만 있는 글은 제목 단계에서 이어진다", () => {
    expect(resumeStep(task("INPUT"))).toBe(WRITE_STEP.TITLE);
    expect(resumeStep(task("REFERENCE_PROCESSING"))).toBe(WRITE_STEP.TITLE);
  });

  it("검증 중이거나 검증 결과만 있는 글은 검증 단계에서 이어진다", () => {
    // 작성 전 검증은 제목 단계 위에 뜨는 팝업이었다. 이제 한 단계다 —
    // 제목을 고른 뒤, 원고를 만들기 전(2026-08-06 사용자 요청).
    expect(resumeStep(task("SEARCH_ANALYZING"))).toBe(WRITE_STEP.VERIFY);
    expect(
      resumeStep(
        task("REFERENCE_PROCESSING", {
          intentValidationResult: {
            promptVersion: "v1",
            provider: "p",
            model: "m",
            analyzedAt: "2026-07-29T00:00:00.000Z",
            intentCandidates: [],
          },
        }),
      ),
    ).toBe(WRITE_STEP.VERIFY);
  });

  it("방향을 고른 글과 생성 중인 글은 원고 단계로 간다", () => {
    expect(resumeStep(task("INTENT_SELECTED"))).toBe(WRITE_STEP.DRAFT);
    expect(resumeStep(task("GENERATING"))).toBe(WRITE_STEP.DRAFT);
  });

  it("원고가 준비된 글은 곧바로 발행 단계로 간다", () => {
    // 사용자가 겪은 버그: 원고가 끝난 글에서 '이어서 쓰기'가 검증 화면을 열었다.
    expect(resumeStep(task("READY_TO_PUBLISH", { finalPost: FINAL_POST }))).toBe(
      WRITE_STEP.PUBLISH,
    );
    expect(resumeStep(task("POSTING", { finalPost: FINAL_POST }))).toBe(WRITE_STEP.PUBLISH);
    expect(resumeStep(task("POSTED", { finalPost: FINAL_POST }))).toBe(WRITE_STEP.PUBLISH);
    expect(resumeStep(task("POSTING_NEEDS_HUMAN", { finalPost: FINAL_POST }))).toBe(
      WRITE_STEP.PUBLISH,
    );
  });

  it("status가 뒤처진 옛 글도 저장된 원고를 보고 발행 단계로 간다", () => {
    // 결과 데이터와 status가 어긋나면 '이미 나온 결과'가 이긴다.
    expect(resumeStep(task("SEARCH_ANALYZING", { finalPost: FINAL_POST }))).toBe(
      WRITE_STEP.PUBLISH,
    );
  });

  it("원고 없이 실패한 글은 재시도 UI가 있는 원고 단계로 간다", () => {
    // 예전에는 FAILED가 곧장 발행 단계(3)로 갔고, 발행 화면에는 보여 줄 원고가 없었다.
    expect(
      resumeStep(
        task("FAILED", {
          selectedIntent: {
            intentId: "i1",
            title: "t",
            targetReader: "r",
            rationale: "why",
          },
        }),
      ),
    ).toBe(WRITE_STEP.DRAFT);
    expect(resumeStep(task("CONTENT_POLICY_VIOLATION"))).toBe(WRITE_STEP.TITLE);
  });

  it("발행 중 실패해 원고가 남아 있는 글은 발행 단계를 유지한다", () => {
    expect(resumeStep(task("FAILED", { finalPost: FINAL_POST }))).toBe(WRITE_STEP.PUBLISH);
  });

  it("원고가 없는데 status만 발행 단계인 문서는 원고 단계로 되돌린다", () => {
    expect(resumeStep(task("READY_TO_PUBLISH"))).toBe(WRITE_STEP.DRAFT);
  });

  it("글이 없으면 소재 단계다", () => {
    expect(resumeStep(null)).toBe(WRITE_STEP.TOPIC);
  });
});

describe("resumeActionLabel", () => {
  it("버튼 문구가 실제로 열리는 단계와 같은 말을 한다", () => {
    expect(resumeActionLabel(task("REFERENCE_PROCESSING"))).toBe("이어서 쓰기");
    expect(resumeActionLabel(task("GENERATING"))).toBe("생성 진행 보기");
    expect(resumeActionLabel(task("READY_TO_PUBLISH", { finalPost: FINAL_POST }))).toBe(
      "발행하기",
    );
    expect(resumeActionLabel(task("POSTED", { finalPost: FINAL_POST }))).toBe("원고 보기");
    expect(resumeActionLabel(task("FAILED"))).toBe("다시 시도");
  });
});

describe("writingTabAction", () => {
  it("원고를 만드는 중이면 그 진행으로 데려간다", () => {
    expect(writingTabAction(task("GENERATING"))).toBe("resume");
    // 데려가는 자리는 원고 단계다 — 탭이 여는 단계와 같은 말을 해야 한다.
    expect(resumeStep(task("GENERATING"))).toBe(WRITE_STEP.DRAFT);
  });

  it("열린 글이 없으면 새 글로 시작한다", () => {
    expect(writingTabAction(null)).toBe("restart");
    expect(writingTabAction(undefined)).toBe("restart");
  });

  it("만들던 글이 있어도 생성 중이 아니면 새 글로 시작한다", () => {
    // 반쯤 쓰던 글은 내 글 목록에서 이어 쓴다 — 탭의 뜻은 '새 글'이다.
    expect(writingTabAction(task("INTENT_SELECTED"))).toBe("restart");
    expect(writingTabAction(task("READY_TO_PUBLISH"))).toBe("restart");
    expect(writingTabAction(task("FAILED"))).toBe("restart");
  });
});
