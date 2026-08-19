/// <reference types="node" />

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * 1걸음(소재·플랫폼 선택)의 **배치**를 CSS 글로 확인한다.
 *
 * 여기 있는 것들은 화면을 띄워 봐야 알 수 있는 종류라 DOM 테스트로는 잡히지 않는다.
 * 실제로 한 번 어긋났다: 소재 줄을 여러 열로 나눠 놓은 채 줄마다 플랫폼 버튼 두 개와
 * 삭제 버튼이 붙어, 입력칸이 글자 몇 자 폭으로 찌그러졌다(2026-08-05). 그 모양이
 * 돌아오지 않게 규칙 자체를 붙잡아 둔다.
 */
const appCss = readFileSync(resolve(process.cwd(), "src", "app.css"), "utf8");

function declarationsFor(selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [...appCss.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "g"))];
  expect(matches.length, `${selector} 규칙이 없다`).toBeGreaterThan(0);
  return matches.map((match) => match[1]).join("\n");
}

describe("1걸음 소재·플랫폼 선택 배치", () => {
  it("소재 줄은 한 줄에 하나씩 세로로 쌓는다", () => {
    const declarations = declarationsFor(".scheduled-topic-rows");
    // 열을 나누는 순간 [번호][소재][네이버][쓰레드][×]가 한 칸에 들어가지 못한다.
    expect(declarations).not.toMatch(/grid-template-columns/i);
  });

  it("소재 줄은 흰 카드 한 장이고, 그 안의 입력칸은 테를 두르지 않는다", () => {
    const row = declarationsFor(".scheduled-topic-row");
    expect(row).toMatch(/border\s*:\s*1px solid var\(--line\)/i);
    expect(row).toMatch(/background\s*:\s*var\(--surface\)/i);

    // 카드 안에 또 상자를 그리면 한 줄에 테두리가 둘로 보인다.
    const line = declarationsFor(".scheduled-topic-row .scheduled-topic-line");
    expect(line).toMatch(/border\s*:\s*0/i);
    expect(line).toMatch(/background\s*:\s*transparent/i);
  });

});
