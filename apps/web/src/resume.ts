/**
 * 글 하나가 "어디까지 왔는지"를 판단하는 단 하나의 규칙.
 *
 * 예전에는 이 판단이 store.maxStep 한 곳에 status만 보고 들어 있었고, 목록 카드의 버튼
 * 문구는 또 따로 status를 봤다. 두 곳이 어긋나면 카드에는 "이어서 쓰기"라고 적혀 있는데
 * 열어 보면 다른 단계가 나온다. 판단을 여기 한 곳으로 모으고, 화면·store가 모두 이것을
 * 부른다.
 *
 * status만으로는 부족하다. 예전에 만들어진 글은 status가 지금 규칙보다 뒤처져 있거나
 * 중간 단계에서 멈춘 채 결과 데이터만 남아 있을 수 있다. 그래서 status를 먼저 보되
 * 실제로 저장된 결과(finalPost / selectedIntent / intentValidationResult / trendSelection)를
 * 함께 확인한다. 둘이 충돌하면 "결과가 있으면 그 단계는 이미 지났다"는 쪽이 이긴다 —
 * 원고가 저장돼 있는 글을 다시 검증 화면으로 보내는 것이 사용자가 겪은 바로 그 버그다.
 */

import type { BlogTask, BlogTaskListItem } from "./api/types";

/** constants.STEPS와 같은 순서. 소재 → 제목 → 검증 → 원고 → 발행. */
export const WRITE_STEP = {
  /** 소재·참고자료 입력 */
  TOPIC: 0,
  /** 제목(트렌드 키워드) */
  TITLE: 1,
  /** 작성 전 검증 — 자료를 확인하고 글의 방향을 고른다.
   *
   * 예전에는 제목 단계 위에 뜨는 팝업이었다. 하는 일은 한 단계인데 막대에 안 보여서,
   * 지금 어디쯤인지 알 수 없고 되돌아올 수도 없었다(2026-08-06 사용자 요청). */
  VERIFY: 2,
  /** 원고 생성 진행·재시도 */
  DRAFT: 3,
  /** 발행 */
  PUBLISH: 4,
} as const;

/** 최종 원고가 이미 있는가. 있으면 어떤 status든 발행 단계다. */
function hasFinalDraft(task: BlogTask): boolean {
  return Boolean(task.finalPost?.htmlContent || task.finalPost?.body);
}

/**
 * 이 글을 다시 열었을 때 서야 할 단계.
 *
 * 스테퍼가 열어 주는 최대 단계이기도 하다 — 저장된 결과가 허락하지 않는 단계로는
 * 어차피 갈 수 없다(발행 단계에 원고 없이 들어가면 빈 화면만 나온다).
 */
export function resumeStep(task: BlogTask | null | undefined): number {
  if (!task) return WRITE_STEP.TOPIC;

  // 1. 원고가 나온 글은 무조건 발행 단계다. 발행 중·발행 완료·발행 실패 모두 포함한다.
  if (hasFinalDraft(task)) return WRITE_STEP.PUBLISH;

  switch (task.status) {
    case "READY_TO_PUBLISH":
    case "POSTING":
    case "POSTED":
    case "POSTING_NEEDS_HUMAN":
      // status는 발행 단계인데 원고가 없는 문서. 발행 화면은 보여 줄 것이 없으므로
      // 원고 단계에서 다시 만들게 한다.
      return WRITE_STEP.DRAFT;
    case "GENERATING":
    case "INTENT_SELECTED":
      return WRITE_STEP.DRAFT;
    default:
      break;
  }

  // 2. 남은 것은 INPUT / REFERENCE_PROCESSING / SEARCH_ANALYZING / FAILED /
  //    CONTENT_POLICY_VIOLATION, 그리고 status를 신뢰할 수 없는 예전 문서다.
  //    저장된 결과를 순서대로 짚어 가장 멀리 간 지점을 찾는다.
  if (task.selectedIntent) return WRITE_STEP.DRAFT;
  // 검증이 돌고 있거나 결과가 나온 글은 검증 단계에 선다. 예전에는 둘 다 제목 단계로
  // 보내고 그 위에 팝업을 띄웠다.
  if (task.status === "SEARCH_ANALYZING") return WRITE_STEP.VERIFY;
  if (task.intentValidationResult) return WRITE_STEP.VERIFY;
  if (task.trendSelection) return WRITE_STEP.TITLE;

  // 3. 소재만 있는 글. 글은 소재 없이 만들어지지 않으므로 여기까지 오면 제목 단계이고,
  //    소재를 고치고 싶으면 스테퍼로 되돌아갈 수 있다.
  return task.input?.topic ? WRITE_STEP.TITLE : WRITE_STEP.TOPIC;
}

/**
 * 목록 카드 버튼에 적을 말. 눌렀을 때 실제로 열리는 단계와 같은 말을 해야 한다 —
 * "이어서 쓰기"라고 적혀 있는데 발행 화면이 열리면 버튼이 거짓말을 한 것이다.
 */
export function resumeActionLabel(task: BlogTask | BlogTaskListItem): string {
  if (task.status === "POSTED") return "원고 보기";
  if (task.status === "GENERATING") return "생성 진행 보기";
  if (task.status === "FAILED" || task.status === "CONTENT_POLICY_VIOLATION") {
    return "다시 시도";
  }
  if ("hasFinalPost" in task) return task.hasFinalPost ? "발행하기" : "이어서 쓰기";
  if (resumeStep(task) === WRITE_STEP.PUBLISH) return "발행하기";
  return "이어서 쓰기";
}

/**
 * 「새 글 작성」 탭을 눌렀을 때 무엇을 할 것인가(2026-08-13 사용자 지시).
 *
 * - `"resume"` — 지금 원고를 만드는 중이다. 새 소재로 가지 말고 **그 진행을 보여 준다.**
 *   다른 탭에 가 있어도 생성은 백그라운드에서 계속되지만, 탭 이름이 '새 글 작성'이라
 *   눌러 보면 만들던 글이 사라진 것처럼 보인다.
 * - `"restart"` — 예전 그대로. 탭은 새 글의 첫 단계로 간다.
 *
 * 만들던 것을 두고 다른 소재로 시작하려면 작업실 오른쪽 위의 '새 글로 시작'이 그 자리다.
 */
export function writingTabAction(
  task: BlogTask | null | undefined,
): "resume" | "restart" {
  return task?.status === "GENERATING" ? "resume" : "restart";
}
