/**
 * 어디에서 들어왔는가(2026-08-19).
 *
 * AIONA 앱스튜디오의 카드가 이 화면을 연다. 그 사람이 하려는 일은 정해져 있다 — AIONA를
 * 활용하는 트렌드 글을 쓰는 것이다. 그런데 화면은 아무것도 모른 채 빈 폼을 띄우고,
 * 사용자는 매번 같은 브랜드를 목록에서 다시 골라야 한다.
 *
 * 그래서 주소에 한 글자를 싣는다:
 *
 *     https://blog-it.example/?campaign=aiona#/write
 *     https://blog-it.example/#/write?campaign=aiona
 *
 * **하는 일은 하나뿐이다: 이름이 같은 브랜드를 미리 골라 준다.** 로그인·계정 연동 같은
 * 것은 하지 않는다 — 그것은 이 값이 감당할 수 있는 신뢰가 아니고(주소는 누구나 적는다),
 * 브랜드 자료는 어차피 사용자 자기 것 안에서만 찾는다.
 *
 * 두 자리를 모두 읽는 이유: 이 앱은 해시 라우팅이라 사람이 링크를 만들 때 물음표가 해시
 * 앞에 붙기도 하고 뒤에 붙기도 한다. 어느 쪽이든 같은 뜻이므로 둘 다 받는다.
 */

/** 주소에 실린 캠페인 값. 없으면 빈 문자열이다. */
export function campaignFrom(search: string, hash: string): string {
  const fromSearch = new URLSearchParams(search).get("campaign");
  if (fromSearch?.trim()) return fromSearch.trim();

  // "#/write?campaign=aiona" — 해시 안의 물음표 뒤가 질의 문자열이다.
  const query = hash.indexOf("?");
  if (query < 0) return "";
  const fromHash = new URLSearchParams(hash.slice(query + 1)).get("campaign");
  return fromHash?.trim() ?? "";
}

/** 지금 이 창의 캠페인 값. */
export function currentCampaign(): string {
  if (typeof window === "undefined") return "";
  return campaignFrom(window.location.search, window.location.hash);
}

/**
 * 캠페인 이름과 브랜드 이름이 같은 것인가.
 *
 * 주소에는 소문자 한 단어("aiona")를 적고, 브랜드 이름은 사람이 보는 표기("AIONA")다.
 * 공백·대소문자만 다른 것은 같은 것으로 본다 — 그 차이로 미리 고르기가 조용히 실패하면
 * 사용자는 이유를 알 수 없다.
 */
export function matchesCampaign(campaign: string, brandName: string): boolean {
  const normalize = (value: string) => value.replace(/[\s_-]/g, "").toLowerCase();
  const wanted = normalize(campaign);
  return Boolean(wanted) && normalize(brandName) === wanted;
}
