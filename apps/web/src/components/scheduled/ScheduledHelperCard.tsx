import { HELPER_ICON, HelperCard, type HelperRow } from "../HelperCard";

/**
 * 「작업 관리」 화면의 도우미 — 오른쪽 칸에 선다(2026-08-12 사용자 요청:
 * *"처음 이용하는 사용자가 이해할 수 있게"*).
 *
 * 이 화면은 **아무것도 만들지 않는다.** 그래서 다른 두 도우미와 말하는 것이 다르다.
 *
 * - 자동 포스팅 도우미 = 줄에 있는 **칸 이름**(무엇을 적는 칸인가).
 * - 새 글 작성 도우미 = 다섯 **걸음**(지금 무엇을 골라야 하는가).
 * - 여기 = **어디서 무엇을 보는가.** 처음 온 사람이 막히는 곳은 "작업 큐와 발행 내역이
 *   뭐가 다르지"와 "여기서 예약을 거는 건가"다. 이 화면에는 거는 자리가 없다.
 */

/**
 * **한 줄에 한 문장이다**(세 도우미가 같은 규칙을 쓴다). 조건은 괄호로 붙인다.
 */
const ROWS: HelperRow[] = [
  {
    key: "여기서 보는 것",
    value: "걸어 둔 작업의 진행과 발행 결과예요.",
    icon: HELPER_ICON.chart,
  },
  // 이 화면에는 예약을 거는 자리가 없다(2026-08-11에 없앴다). 그것부터 말해 두지 않으면
  // 처음 온 사람은 여기서 걸 방법을 찾다 만다.
  {
    key: "작업 거는 곳",
    value: "새 글 작성과 자동 포스팅에서 걸어요.",
    icon: HELPER_ICON.wand,
  },
  { key: "작업 큐", value: "아직 남은 일이 차례대로 서 있어요.", icon: HELPER_ICON.page },
  {
    key: "발행 내역",
    value: "끝난 일이 모여요(완료·실패·취소).",
    icon: HELPER_ICON.clock,
  },
  // worker.py: "발행은 언제나 하나씩이다"(크롬 프로필 잠금).
  {
    key: "발행 차례",
    value: "글이 겹치지 않게 하나씩 올라가요.",
    icon: HELPER_ICON.steps,
  },
  {
    key: "멈춤과 이어서",
    value: "도는 작업을 멈췄다가 다시 이을 수 있어요.",
    icon: HELPER_ICON.play,
  },
  { key: "실패했을 때", value: "발행 내역에서 다시 시도해요.", icon: HELPER_ICON.retry },
];

export function ScheduledHelperCard() {
  return <HelperCard rows={ROWS} />;
}
