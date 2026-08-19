import type { ScheduledJob } from "../../api/types";

/**
 * 실패한 작업의 사유를 **사람이 다음에 무엇을 할지 정할 수 있는 한 줄**로 바꾼다.
 *
 * 서버가 주는 `errorMessage`는 그대로 화면에 올릴 것이 못 된다. 실제로 저장돼 있던 것들:
 *
 * - `M4 requires INTENT_SELECTED, received GENERATING` — 내부 상태 이름
 * - `blogTask post_20260806_061442_170_83a0c312486c not found` — 내부 식별자
 * - Selenium의 `InvalidSessionIdException` 스택 **1,520자**
 *
 * 마지막 것은 발행 내역 한 줄이 화면 절반을 붉게 덮었다(2026-08-06 신고 — "너무
 * 지저분하고 사용자가 뭘 확인하라는건지 몰라"). 그렇다고 **버리지는 않는다**: 원문은
 * `detail`로 남겨 화면이 접어 두고, 필요하면 펼쳐 보게 한다.
 *
 * 짝을 맞추는 기준은 `errorCode`를 먼저 보고, 코드가 뭉뚱그려져 있을 때만 문구를 본다.
 */
type FailureReason = {
  /** 표에 그대로 적는 한 줄. */
  summary: string;
  /** 사용자가 할 수 있는 일. 없으면 표시하지 않는다. */
  hint?: string;
  /** 서버가 준 원문. 접어 두었다가 '자세히'로 펼친다. */
  detail?: string;
};

/** 원문에서 알아볼 수 있는 신호. 스택트레이스가 섞여 있어도 앞부분에 남아 있다. */
function has(message: string, needle: string): boolean {
  return message.toLowerCase().includes(needle.toLowerCase());
}

export function failureReason(job: ScheduledJob): FailureReason | null {
  const raw = (job.errorMessage ?? "").trim();
  const code = job.errorCode ?? "";
  if (!raw && !code) return null;
  // 원문이 짧고 이미 한국어면 그것이 곧 요약이다 — 두 번 적지 않는다.
  const detail = raw || undefined;

  if (job.status === "NEEDS_HUMAN" || code === "NAVER_NEEDS_HUMAN") {
    return {
      summary: "로그인 확인이 필요해 멈췄습니다",
      hint: "열려 있는 브라우저에서 인증을 마친 뒤 「예약 재개」를 눌러 주세요.",
      detail,
    };
  }

  /**
   * `INVALID_STATUS_TRANSITION`은 **한 가지 사건이 아니다.**
   *
   * 예전에는 이 코드를 전부 "다른 작업이 같은 글을 쓰고 있어"로 적었는데, 그것은 그
   * 코드가 말해 주지 않는 사실이다. 같은 코드로 오는 것이 최소 셋이다:
   * 원고를 이미 쓰고 있음 · 글이 기대한 단계에 있지 않음 · 발행할 수 없는 상태.
   * 화면이 짐작으로 하나를 골라 적으면, 실제 원인이 다른 사람은 엉뚱한 곳을 보게 된다
   * (2026-08-06 — 실제로 그랬다). 원문에 단서가 있을 때만 그것을 말한다.
   */
  if (code === "INVALID_STATUS_TRANSITION" || has(raw, "requires INTENT_SELECTED")) {
    if (has(raw, "이미 원고를 생성")) {
      return {
        summary: "이 글의 원고를 이미 쓰고 있어 다시 시작하지 못했습니다",
        hint: "그 작업이 끝나면 원고는 완성됩니다. 잠시 뒤 재시도해 주세요.",
        detail,
      };
    }
    return {
      summary: "글이 원고를 만들 수 있는 단계가 아니었습니다",
      hint: "재시도를 누르면 지금 상태에서 이어서 진행합니다.",
      detail,
    };
  }

  if (code === "DRAFT_FAILED" || code === "DRAFT_IN_PROGRESS") {
    return {
      summary: "원고 생성에 실패했습니다",
      hint: "재시도를 누르면 같은 소재로 원고부터 다시 만듭니다.",
      detail,
    };
  }

  if (code === "CONTENT_POLICY_VIOLATION") {
    return {
      summary: "콘텐츠 정책에 걸려 발행하지 않았습니다",
      hint: "소재나 표현을 바꿔 새로 예약해 주세요.",
      detail,
    };
  }

  if (code === "INTENT_VALIDATION_FAILED") {
    return {
      summary: "검색 의도 검증에서 쓸 만한 자료를 얻지 못했습니다",
      hint: "재시도하거나, 더 구체적인 소재로 바꿔 보세요.",
      detail,
    };
  }

  if (code === "NOT_FOUND" || has(raw, "not found")) {
    return {
      summary: "원고가 남아 있지 않아 이어서 진행하지 못했습니다",
      hint: "글이 지워진 작업입니다. 재시도하면 처음부터 새로 만듭니다.",
      detail,
    };
  }

  // 발행 단계의 실패. 셀레니움 예외는 여기로 온다 — 종류별로 할 일이 다르다.
  if (has(raw, "InvalidSessionId") || has(raw, "browser has closed")) {
    return {
      summary: "발행 도중 브라우저 창이 닫혀 중단됐습니다",
      hint: "창을 닫지 않은 채로 재시도해 주세요. 원고는 그대로 있어 발행만 다시 합니다.",
      detail,
    };
  }
  if (has(raw, "에디터 제목이 계획과 다릅니다")) {
    return {
      summary: "네이버 에디터에 제목이 들어가지 않았습니다",
      hint: "네이버 화면이 바뀌었을 수 있습니다. 재시도해도 같으면 직접 발행해 주세요.",
      detail,
    };
  }
  if (code === "THREADS_PUBLISH_FAILED") {
    return {
      summary: raw.includes("네이버에는 발행됨")
        ? "스레드 발행만 실패했습니다(네이버에는 올라갔습니다)"
        : "스레드 발행에 실패했습니다",
      hint: "재시도하면 스레드에만 다시 올립니다.",
      detail,
    };
  }
  if (code === "PUBLISH_FAILED" || code === "NAVER_PUBLISH_FAILED") {
    return {
      summary: "발행에 실패했습니다",
      hint: "원고는 그대로 있습니다. 재시도하면 발행만 다시 합니다.",
      detail,
    };
  }
  if (code === "NAVER_NOT_CONNECTED" || code === "THREADS_NOT_CONNECTED") {
    return {
      summary: "발행 계정이 저장돼 있지 않습니다",
      hint: "설정에서 계정을 저장한 뒤 재시도해 주세요.",
      detail,
    };
  }

  // 여기까지 왔으면 우리가 모르는 실패다. **지어내지 않는다** — 원문의 첫 줄만 보여 주고
  // 나머지는 접어 둔다. 한 줄조차 없으면 코드라도 적는다(빈 칸은 고장으로 읽힌다).
  const firstLine = raw.split("\n")[0]?.trim() ?? "";
  return {
    summary: firstLine ? shorten(firstLine) : `실패했습니다 (${code})`,
    detail,
  };
}

/** 표 한 칸을 넘지 않게 자른다. 원문은 `detail`에 그대로 있다. */
const MAX_SUMMARY = 90;
function shorten(text: string): string {
  return text.length <= MAX_SUMMARY ? text : `${text.slice(0, MAX_SUMMARY)}…`;
}
