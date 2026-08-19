import { HELPER_ICON, HelperCard, type HelperRow } from "../HelperCard";

/**
 * 「새 글 작성」의 도우미 — 「이 글 요약」 아래에 붙는다(2026-08-12 사용자 요청:
 * *"처음 이용하는 사용자가 잘 이해할 수 있게"*).
 *
 * 자동 포스팅의 도우미와 **담는 것이 다르다.** 그쪽은 줄에 있는 칸 이름을 그대로 늘어놓는다
 * — 소재만 적으면 끝까지 알아서 도는 화면이라 설명할 것이 칸뿐이기 때문이다.
 *
 * 새 글 작성은 사람이 다섯 걸음을 직접 밟는다. 그래서 여기서는 **먼저 흐름을 말하고**,
 * 그다음 이번 걸음에서 정해야 하는 것을 필수·선택으로 나눠 적는다. 처음 여는 사람이
 * 막히는 곳은 "지금 뭘 채워야 다음으로 넘어가지"이지 칸의 이름이 아니다.
 *
 * 요약 카드가 **지금 고른 값**을 보여 주는 자리라면, 이 카드는 **무엇을 골라야 하는지**를
 * 말한다. 둘이 위아래로 붙어 "무엇을 정할지 → 무엇을 정했는지"로 읽힌다.
 */

/**
 * 흐름 하나 + 필수 셋 + 선택 둘 + 걸음마다 하는 일 둘.
 *
 * **한 줄에 한 문장이다**(자동 포스팅 도우미와 같은 규칙). 좁은 칸에서 두세 문장이 접히면
 * 어디까지가 한 이야기인지 눈으로 끊기지 않는다. 조건은 괄호로 붙인다.
 */
const ROWS: HelperRow[] = [
  {
    key: "다섯 걸음",
    value: "소재 → 제목 → 검증 → 원고 → 발행 순서로 나아가요.",
    icon: HELPER_ICON.steps,
  },
  { key: "소재", value: "무엇에 대해 쓸지 적어요(꼭 필요해요).", icon: HELPER_ICON.bulb },
  {
    key: "글 목적",
    value: "글의 종류와 짜임이 여기서 정해져요(꼭 필요해요).",
    icon: HELPER_ICON.target,
  },
  {
    key: "대상 연령",
    value: "누가 읽느냐에 따라 설명 수준이 달라져요(꼭 필요해요).",
    icon: HELPER_ICON.people,
  },
  {
    key: "카테고리",
    value: "이름이 겹치는 것과 헷갈리지 않게 해요(꼭 필요해요).",
    icon: HELPER_ICON.tag,
  },
  {
    key: "브랜드·참고 자료",
    value: "정해 둔 말투와 자료를 원고에 실어요(선택이에요).",
    icon: HELPER_ICON.clip,
  },
  {
    key: "언제, 몇 편, 어디에",
    value: "비우면 지금 바로 한 편을 만들어요(선택이에요).",
    icon: HELPER_ICON.clock,
  },
  {
    key: "제목과 방향",
    value: "만들어 온 후보 중에서 직접 골라요.",
    icon: HELPER_ICON.checklist,
  },
  {
    key: "고쳐 쓰기",
    value: "지난 걸음으로 돌아가 언제든 다시 고쳐요.",
    icon: HELPER_ICON.wand,
  },
];

export function WriteHelperCard() {
  return <HelperCard rows={ROWS} />;
}
