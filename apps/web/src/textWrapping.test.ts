/// <reference types="node" />

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const appCss = readFileSync(resolve(process.cwd(), "src", "app.css"), "utf8");

function declarationsFor(selector: string) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [
    ...appCss.matchAll(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`, "g")),
  ];
  expect(matches.length, `Missing CSS rule for ${selector}`).toBeGreaterThan(0);
  return matches.map((match) => match[1]).join("\n");
}

describe("long user-facing text", () => {
  it("does not reintroduce line-clamp truncation", () => {
    expect(appCss).not.toMatch(/(?:-webkit-)?line-clamp\s*:/i);
  });

  it("reserves ellipsis for the one row that must keep a fixed height", () => {
    const ellipsisRules = [
      ...appCss.matchAll(/([^{}]+)\{[^{}]*text-overflow\s*:\s*ellipsis[^{}]*\}/gi),
    ];

    // 말줄임은 **여기 적힌 자리에서만** 쓴다. 긴 글을 잘라 내는 것은 내용을 감추는 일이라,
    // 늘어날 때마다 이 목록에 이유를 함께 적는다.
    //
    // 예약 확인 표(.review-topic)는 2026-08-06에 화면과 함께 없어졌다 — 확인 걸음이
    // 사라지고 그 자리를 작업 큐가 대신한다.
    expect(ellipsisRules).toHaveLength(3);

    // 선택자 순서는 파일 안 위치를 따를 뿐이라 목록으로 확인한다.
    const selectors = ellipsisRules.map((rule) => rule[1]);

    // 기억된 계정 줄 — 좁은 칸에 아이디가 들어간다.
    const account = selectors.find((selector) => selector.includes(".auth-account-copy"));
    expect(account).toContain(".auth-account-copy strong");
    expect(account).toContain(".auth-account-copy > span");

    // 이미지 출처명(2026-08-11 사용자 지시). 여기서는 말줄임이 **내용을 감추지 않는다** —
    // 전체 이름이 DOM과 title에 그대로 있고, hover와 클릭(is-expanded) 두 방법으로
    // 펼쳐 볼 수 있다(ImageSourceNote.test.tsx의 CASE 2). 대신 접지 않으면 긴 사이트
    // 이름이 사진 아래에서 여러 줄로 늘어져 캡션·본문과 뒤섞인다. font-size를 줄여
    // 맞추는 방식은 쓰지 않는다는 조건도 함께 지킨다.
    expect(selectors.some((selector) => selector.includes(".image-source-name"))).toBe(true);

    // 대상 연령 안내(2026-08-11 사용자 요청 "한 줄로 표시하고"). 이 줄은 **높이가
    // 고정돼야 하는 자리**다 — 줄 수가 상태에 따라 달라지면 그만큼 아래 연령 버튼들이
    // 오르내려, 누르려던 자리가 움직인다(같은 지적을 세 번 받은 자리). 잘린 문장은
    // title로 전체를 볼 수 있고, 문장 자체가 짧아 실제로 잘리는 일은 드물다.
    expect(selectors.some((selector) => selector.includes(".brief-audience-note"))).toBe(true);
  });

  it("접은 출처명은 펼칠 수 있어야 한다 — 잘라 놓고 끝내지 않는다", () => {
    // 위 목록에 자리를 하나 더 내준 근거. 펼침 규칙이 사라지면 그 순간 진짜 '감추기'가 된다.
    const expanded = declarationsFor(".preview .image-source-name.is-expanded");
    expect(expanded).toMatch(/white-space\s*:\s*normal/i);
    expect(expanded).toMatch(/overflow\s*:\s*visible/i);
    expect(expanded).toMatch(/overflow-wrap\s*:\s*anywhere/i);
  });

  it("출처 줄은 좁은 화면에서 카드 밖으로 나가지 않는다", () => {
    // CASE 7. 고정 폭을 강제하지 않고 접히게 두는 것이 이 줄의 잘림 방지 방식이다.
    const note = declarationsFor(".preview .image-source-note");
    expect(note).toMatch(/flex-wrap\s*:\s*wrap/i);
    expect(note).toMatch(/max-width\s*:\s*100%/i);

    // 이름 칸이 남는 폭 안에서 줄어들려면 min-width: 0이 있어야 한다(없으면 flex 항목이
    // 내용보다 작아지지 않아 줄 전체가 부모를 밀어낸다 — 카드 밖으로 튀어나가던 원인).
    const name = declarationsFor(".preview .image-source-name");
    expect(name).toMatch(/min-width\s*:\s*0/i);
  });

  it.each([
    ["step description", ".step-copy > span"],
    ["file upload summary", ".file-upload-summary"],
    ["post title", ".post-card h3"],
    ["post subject", ".post-card-subject"],
  ])("shows the full %s by wrapping", (_label, selector) => {
    const declarations = declarationsFor(selector);
    expect(declarations).toMatch(/white-space\s*:\s*normal/i);
    expect(declarations).toMatch(/overflow\s*:\s*visible/i);
    expect(declarations).toMatch(/text-overflow\s*:\s*clip/i);
    expect(declarations).toMatch(/overflow-wrap\s*:\s*anywhere/i);
  });

  it("keeps the header account row (name, switch, sign-out) on one line", () => {
    const declarations = declarationsFor(".user-chip strong");
    expect(declarations).toMatch(/white-space\s*:\s*nowrap/i);
  });

  it("does not hide overflowing purpose keywords", () => {
    const listDeclarations = declarationsFor(".post-card-purpose-cell .keyword-list");
    const chipDeclarations = declarationsFor(".chip");

    expect(listDeclarations).toMatch(/max-height\s*:\s*none/i);
    expect(listDeclarations).toMatch(/overflow\s*:\s*visible/i);
    expect(chipDeclarations).toMatch(/white-space\s*:\s*normal/i);
    expect(chipDeclarations).toMatch(/overflow-wrap\s*:\s*anywhere/i);
  });
});
