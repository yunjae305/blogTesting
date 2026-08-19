/**
 * 발행 일정 요약 계산.
 *
 * 화면 문구를 만드는 곳이 아니라 **숫자와 사실을 세는 곳**이다. 컴포넌트가 직접 세면
 * 요약 박스와 하단 바가 서로 다른 숫자를 말하게 된다(둘 다 여기서 받아 쓴다).
 *
 * 여기서 지키는 규칙 하나: **모르는 것은 말하지 않는다.** '매일 오후 2시'는 날짜가 실제로
 * 하루씩 이어지고 시각이 모두 같을 때만 쓴다. 아니면 첫·마지막만 적는다.
 */

import type { PostingChannel } from "../../api/types";

/** 플랫폼별 발행 작업 수. 한 글을 두 곳에 올리면 2건이다. */
type PublishJobCounts = {
  naver: number;
  threads: number;
  /** 전체 발행 작업 수 = 소재마다 고른 플랫폼 수의 합. */
  total: number;
};

/**
 * 발행 작업 수를 센다 — **글 수가 아니라 플랫폼 수의 합**이다.
 *
 * 예: 2곳 + 1곳 + 2곳 + 1곳 = 6건. 소재는 4개지만 발행 작업은 6건이다.
 */
export function countPublishJobs(
  platformsList: PostingChannel[][],
  count: number,
): PublishJobCounts {
  const counts: PublishJobCounts = { naver: 0, threads: 0, total: 0 };
  for (const platforms of platformsList.slice(0, count)) {
    // 같은 플랫폼이 두 번 들어 있어도 한 건으로 센다.
    for (const platform of new Set(platforms)) {
      if (platform !== "naver" && platform !== "threads") continue;
      counts[platform] += 1;
      counts.total += 1;
    }
  }
  return counts;
}

// 간격 방식만 따로 세던 `countIntervalJobs`는 없앴다(2026-08-06). 그 함수가 있던 이유는
// "간격 방식은 줄마다의 플랫폼이 서버로 나가지 않는다"였는데, 이제 두 방식 모두 줄의
// 선택을 그대로 보낸다. 남겨 두면 같은 것을 두 가지로 세는 길이 다시 열린다.
