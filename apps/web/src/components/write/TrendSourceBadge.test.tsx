import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TrendSourceBadge } from "./TrendSourceBadge";

describe("TrendSourceBadge", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  function render(source: string) {
    act(() => root.render(<TrendSourceBadge source={source} />));
    return container.querySelector(".title-keyword-source, .title-keyword-chip") as HTMLElement;
  }

  it("화면에는 로고만, 이름은 읽어 주는 쪽에 남긴다", () => {
    // 글자와 알약 배경은 뺐다(2026-08-07 사용자 요청). 그렇다고 이름을 지우면 색과
    // 그림만 남아, 색을 구분하지 못하는 사용자에게는 출처가 통째로 사라진다.
    for (const [source, name] of [
      ["GOOGLE_TRENDS", "Google"],
      ["YOUTUBE", "YouTube"],
      ["NAVER_DATALAB", "NAVER"],
      ["INSTAGRAM", "Instagram"],
    ]) {
      const mark = render(source);

      // 이름은 있다 — 다만 화면에서 감춘 자리에 있다.
      const hidden = mark.querySelector(".sr-only");
      expect(hidden?.textContent, `${source}의 이름이 없다`).toBe(name);
      expect(mark.querySelector("svg")).not.toBeNull();
    }
  });

  it("알약 배경과 테두리를 쓰던 칩 클래스가 남아 있지 않다", () => {
    // .title-keyword-chip에는 border와 background가 붙어 있다. 그대로 두면 로고만
    // 남겨도 타원형 배경이 계속 보인다.
    for (const source of ["GOOGLE_TRENDS", "YOUTUBE", "NAVER_DATALAB"]) {
      expect(render(source).className).not.toContain("title-keyword-chip");
    }
  });

  it("서비스마다 자기 클래스를 갖는다", () => {
    // 크기 조정처럼 서비스별로 달리 줄 것이 남아 있다(유튜브는 가로로 넓다).
    expect(render("YOUTUBE").className).toContain("title-keyword-source--youtube");
    expect(render("NAVER_DATALAB").className).toContain("title-keyword-source--naver");
    expect(render("GOOGLE_TRENDS").className).toContain("title-keyword-source--google");
  });

  it("각 로고가 실제로 그려진다", () => {
    // 사용자가 준 SVG로 갈아 끼웠다(2026-08-07). 경로를 붙여넣다 빠뜨리면 빈 칸만
    // 남는데, 이름이 화면에 없으므로 눈치채기가 더 어렵다.
    for (const source of ["GOOGLE_TRENDS", "YOUTUBE", "NAVER_DATALAB", "INSTAGRAM"]) {
      const mark = render(source);
      const svg = mark.querySelector("svg");
      expect(svg, `${source} 로고가 없다`).not.toBeNull();
      expect(svg?.querySelectorAll("path, rect, circle").length ?? 0).toBeGreaterThan(0);
    }
  });

  it("네이버 로고의 그러데이션이 실제로 정의돼 있다", () => {
    // url(#...)만 있고 정의가 없으면 초록이 아니라 검게 칠해진다.
    const mark = render("NAVER_DATALAB");

    const rect = mark.querySelector("rect");
    const fill = rect?.getAttribute("fill") ?? "";
    const id = /url\(#(.+?)\)/.exec(fill)?.[1];
    expect(id, `채우기가 그러데이션이 아니다: ${fill}`).toBeTruthy();
    expect(mark.querySelector(`#${id}`)).not.toBeNull();
  });
  it("leaves a source with no brand on the plain chip", () => {
    // 소재 확장은 외부 서비스가 아니다. 로고를 지어 붙이면 수집한 출처처럼 보인다.
    const chip = render("RELATED_EXPANSION");
    expect(chip.textContent).toBe("소재 확장");
    expect(chip.querySelector("svg")).toBeNull();
    expect(chip.className).toBe("title-keyword-chip source");
  });
});
