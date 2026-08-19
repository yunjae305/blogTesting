import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ImageSourceInfo } from "../../api/types";

import { ImageSourceNote } from "./ImageSourceNote";

/**
 * 출처 표시(2026-08-11 사용자 지시).
 *
 * 여기서 지키는 것은 세 가지다.
 * - 확인하지 못한 이용 조건을 '사용 가능'이라고 적지 않는다.
 * - AI 생성 이미지에는 외부 웹사이트 출처를 붙이지 않는다.
 * - 출처명이 길어도 전체를 확인할 방법이 있어야 하고(hover·클릭), 전체 URL을 글자로
 *   늘어놓지 않는다.
 */
describe("ImageSourceNote", () => {
  let container: HTMLDivElement;
  let root: Root;

  async function render(source: ImageSourceInfo | null | undefined) {
    await act(async () => {
      root.render(<ImageSourceNote source={source} />);
    });
  }

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("CASE 6: 출처 정보가 없는 옛 이미지에는 아무것도 그리지 않는다", async () => {
    await render(undefined);
    expect(container.querySelector(".image-source-note")).toBeNull();

    await render(null);
    expect(container.querySelector(".image-source-note")).toBeNull();
  });

  it("CASE 1: 출처명과 원문 링크를 보여주고 URL은 링크 뒤에 둔다", async () => {
    await render({
      sourceType: "external",
      sourceName: "연합뉴스",
      sourcePageUrl: "https://www.yna.co.kr/view/AKR20260811",
      usageStatus: "unknown",
    });

    expect(container.querySelector(".image-source-label")?.textContent).toBe("출처");
    expect(container.querySelector(".image-source-name")?.textContent).toBe("연합뉴스");

    const link = container.querySelector<HTMLAnchorElement>(".image-source-link");
    expect(link?.textContent).toContain("원문 보기");
    expect(link?.getAttribute("href")).toBe("https://www.yna.co.kr/view/AKR20260811");
    // 새 탭으로 열되 opener를 넘기지 않는다.
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
    // 전체 주소가 글자로 늘어서 있으면 안 된다.
    expect(container.textContent).not.toContain("https://www.yna.co.kr");
  });

  it("CASE 2: 긴 출처명은 접되 hover와 클릭으로 전체를 볼 수 있다", async () => {
    const long = "아주아주긴이름의지역신문사닷컴 서울경기강원충청전라경상제주 종합뉴스";
    await render({
      sourceType: "external",
      sourceName: long,
      sourcePageUrl: "https://long.example/article",
      usageStatus: "unknown",
    });

    const name = container.querySelector<HTMLButtonElement>(".image-source-name");
    // 접힌 상태에서도 전체 이름은 DOM과 title에 그대로 있다 — 잘라 버리지 않는다.
    expect(name?.textContent).toBe(long);
    expect(name?.getAttribute("title")).toBe(long);
    expect(name?.className).not.toContain("is-expanded");
    expect(name?.getAttribute("aria-expanded")).toBe("false");

    // hover가 없는 화면(모바일)에서는 눌러서 펼친다.
    await act(async () => {
      name?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const expanded = container.querySelector(".image-source-name");
    expect(expanded?.className).toContain("is-expanded");
    expect(expanded?.getAttribute("aria-expanded")).toBe("true");
  });

  it("CASE 4: 라이선스를 확인하지 못하면 '이용 조건 미확인'이다", async () => {
    await render({
      sourceType: "external",
      sourceName: "imgnews.pstatic.net",
      usageStatus: "unknown",
    });

    expect(container.querySelector(".image-source-usage")?.textContent).toBe("이용 조건 미확인");
    // 단정하는 문구를 쓰지 않는다.
    expect(container.textContent).not.toContain("사용 가능");
    expect(container.textContent).not.toContain("저작권 문제 없음");
    // 원문 페이지를 모르면 링크도 만들지 않는다 — 없는 주소를 지어내지 않는다.
    expect(container.querySelector(".image-source-link")).toBeNull();
  });

  it("라이선스 확인 페이지가 있으면 그 링크를 제공한다", async () => {
    await render({
      sourceType: "external",
      sourceName: "전과자 공식 채널",
      sourcePageUrl: "https://www.youtube.com/watch?v=abc",
      license: "크리에이티브 커먼즈 저작자 표시(CC BY)",
      licenseUrl: "https://creativecommons.org/licenses/by/3.0/",
      usageStatus: "allowed",
    });

    const links = [...container.querySelectorAll<HTMLAnchorElement>(".image-source-link")];
    expect(links.map((link) => link.textContent?.trim())).toEqual([
      "원문 보기 ↗",
      "이용 조건 확인 ↗",
    ]);
    expect(links[1].getAttribute("href")).toBe("https://creativecommons.org/licenses/by/3.0/");
  });

  it("CASE 5: AI 생성 이미지에는 외부 출처 UI를 붙이지 않는다", async () => {
    await render({ sourceType: "generated", sourceName: "", usageStatus: "allowed" });

    expect(container.querySelector(".image-source-kind")?.textContent).toBe("AI 생성 이미지");
    expect(container.querySelector(".image-source-label")).toBeNull();
    expect(container.querySelector(".image-source-link")).toBeNull();
    expect(container.querySelector(".image-source-usage")).toBeNull();
  });

  it("사이트 이름조차 모르면 지어내지 않고 모른다고 적는다", async () => {
    await render({ sourceType: "external", sourceName: "", usageStatus: "unknown" });

    const name = container.querySelector(".image-source-name");
    expect(name?.textContent).toBe("출처 정보 없음");
    expect(name?.className).toContain("is-unknown");
  });
});
