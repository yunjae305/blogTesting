import { describe, expect, it } from "vitest";

import { campaignFrom, matchesCampaign } from "./campaign";

/**
 * AIONA 앱스튜디오에서 들어온 사람을 알아보는 것(2026-08-19).
 *
 * 하는 일은 하나뿐이다 — 이름이 같은 브랜드를 미리 골라 준다. 그러니 지켜야 할 것도
 * 하나다: **읽지 못해서 조용히 아무 일도 안 일어나는 경우가 없을 것.** 미리 고르기가
 * 실패하면 사용자는 이유를 알 수 없고, 그냥 빈 폼을 보게 된다.
 */
describe("들어온 경로", () => {
  it("물음표가 해시 앞에 있어도 읽는다", () => {
    expect(campaignFrom("?campaign=aiona", "#/write")).toBe("aiona");
  });

  it("물음표가 해시 뒤에 있어도 읽는다", () => {
    // 해시 라우팅이라 사람이 링크를 만들 때 이쪽으로 적기도 한다. 뜻은 같다.
    expect(campaignFrom("", "#/write?campaign=aiona")).toBe("aiona");
  });

  it("다른 값이 함께 실려 있어도 캠페인만 꺼낸다", () => {
    expect(campaignFrom("?utm_source=appstudio&campaign=aiona", "#/write")).toBe("aiona");
  });

  it("없으면 빈 문자열이다", () => {
    expect(campaignFrom("", "#/write")).toBe("");
    expect(campaignFrom("?campaign=", "#/write")).toBe("");
    expect(campaignFrom("?other=1", "#/write?x=2")).toBe("");
  });
});

describe("캠페인과 브랜드 이름 대조", () => {
  it("대소문자·공백만 다른 것은 같은 것으로 본다", () => {
    // 주소에는 소문자 한 단어를 적고, 브랜드 이름은 사람이 보는 표기다.
    expect(matchesCampaign("aiona", "AIONA")).toBe(true);
    expect(matchesCampaign("AIONA", "aiona")).toBe(true);
    expect(matchesCampaign("blog-it", "Blog it")).toBe(true);
  });

  it("다른 브랜드를 고르지 않는다", () => {
    expect(matchesCampaign("aiona", "다른 회사")).toBe(false);
    // 이름의 일부만 같은 것은 다른 브랜드다 — 잘못 고르면 남의 자료가 글에 실린다.
    expect(matchesCampaign("aiona", "AIONA 파트너스")).toBe(false);
  });

  it("캠페인이 없으면 아무것도 고르지 않는다", () => {
    expect(matchesCampaign("", "AIONA")).toBe(false);
  });
});
