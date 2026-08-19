import { HELPER_ICON, HelperCard, type HelperRow } from "../HelperCard";

/**
 * 「자동 포스팅」 화면의 도우미 — 이 화면을 어떻게 쓰는지 옆에 두고 읽는 자리
 * (2026-08-12 사용자 요청).
 *
 * 원래는 입력 패널 위에 문단 하나로 있었다. 줄에 시각 칸이 붙으면서 설명할 것이 넷이
 * 되자 그 문단이 길어졌고, 정작 눈이 가야 할 입력칸 위를 글자가 덮었다 —
 * *"자동포스팅의 설명에서 뭔가 지저분해보여서"*.
 *
 * 카드의 생김새는 `HelperCard`가 갖고 있다. 여기는 **무엇을 적을지**만 정한다.
 *
 * 내용은 **칸 이름 그대로** 늘어놓는다. 줄에서 본 이름과 여기 적힌 이름이 같아야
 * 눈으로 맞춰 읽을 수 있다.
 */

/**
 * 칸 이름 → 그 칸이 하는 일. 줄에 보이는 순서와 같다.
 *
 * **한 줄에 한 문장이다**(2026-08-12 사용자 지적: "칸 하나당 줄바꿈이 이상해").
 * 좁은 칸에서 두세 문장이 접히면 어디까지가 한 이야기인지 눈으로 끊기지 않는다.
 * 그래서 문장을 하나로 줄이고, 조건은 괄호로 붙인다.
 *
 * 분야 설명에서 '오디세이' 예시를 뺐다 — 예시는 그 낱말을 아는 사람에게만 통하고,
 * 모르는 사람에게는 설명이 하나 더 늘어난다. 하는 일을 그대로 적는 편이 낫다.
 */
const ROWS: HelperRow[] = [
  { key: "줄 하나", value: "글 한 편이에요(최대 20편).", icon: HELPER_ICON.page },
  { key: "작업 시작", value: "비우면 앞 글이 발행된 뒤 이어서 올라가요.", icon: HELPER_ICON.play },
  { key: "소재", value: "무엇에 대해 쓸지 적어요(꼭 필요해요).", icon: HELPER_ICON.bulb },
  {
    key: "분야",
    value: "이름이 겹치는 것과 헷갈리지 않게 해요(비우면 자동).",
    icon: HELPER_ICON.tag,
  },
  // 새 줄의 기본값이 네이버다(useScheduledPosting) — 그 사실을 화면이 말하지 않아
  // 사용자가 직접 켜야 하는 줄 알았다(2026-08-12 사용자 요청).
  { key: "발행 플랫폼", value: "안 고르면 네이버로 올라가요.", icon: HELPER_ICON.globe },
  { key: "시작하면", value: "제목·자료·원고를 알아서 만들어 발행해요.", icon: HELPER_ICON.wand },
  { key: "진행 상황", value: "예약작업 관리 탭에서 볼 수 있어요.", icon: HELPER_ICON.chart },
];

export function BulkHelperCard() {
  return <HelperCard rows={ROWS} />;
}
