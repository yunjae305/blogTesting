/**
 * 대상 연령 선택 — 고른 뒤에 그 연령의 설명 하나만 보여 준다.
 *
 * 예전에는 카드마다 한 줄씩 붙여 뒀는데, 다섯 개를 한꺼번에 깔면 고르기 전에 읽을
 * 것이 너무 많다(2026-08-07 사용자 요청).
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { READER_AGE_RANGES } from "../../constants";
import { AudienceChoices, AudienceNote } from "./BriefChoices";

describe("대상 연령 설명", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  async function render(ageRange: string | null) {
    await act(async () =>
      root.render(
        <>
          <AudienceChoices ageRange={ageRange} onChange={() => {}} />
          <AudienceNote ageRange={ageRange} />
        </>,
      ),
    );
  }

  it("고르기 전에는 설명이 하나도 없고, 자리만 비워 둔다", async () => {
    await render(null);

    // 2026-08-11: 자리는 **같은 크기의 한 줄**로 잡아 둔다 — 안 그리면 고르는 순간
    // 안내가 끼어들며 아래 버튼들이 통째로 내려가, 누르려던 자리가 움직인다.
    const note = container.querySelector(".brief-audience-note");
    expect(note?.classList.contains("is-empty")).toBe(true);
    expect(note?.textContent).toContain("고른 세대에 맞춰");
    // 카드에도 설명이 딸려 있지 않다 — 다섯 개가 한꺼번에 깔리면 안 된다.
    for (const range of READER_AGE_RANGES) {
      expect(container.textContent).not.toContain(range.description);
    }
  });

  it("고르면 그 연령의 설명만 나온다", async () => {
    const twenties = READER_AGE_RANGES.find((range) => range.value === "20s")!;
    const thirties = READER_AGE_RANGES.find((range) => range.value === "30s")!;

    await render("20s");

    const note = container.querySelector(".brief-audience-note")?.textContent ?? "";
    expect(note).toContain(twenties.description);
    expect(note).not.toContain(thirties.description);
  });

  it("전체를 골라도 설명이 나온다", async () => {
    // ""는 "아직 안 고름"이 아니라 "전체"라는 실제 선택이다.
    await render("");

    const all = READER_AGE_RANGES.find((range) => range.value === "")!;
    expect(container.querySelector(".brief-audience-note")?.textContent).toContain(
      all.description,
    );
  });

  it("'전체' 안내가 다른 연령보다 길지 않다", async () => {
    // 이 안내는 대상 연령 카드의 좁은 머리글 자리에 놓인다. 여섯 개 중 하나만 길면
    // 거기서만 두 줄로 끊긴다 — '전체'가 그랬다(2026-08-11 사용자 지적).
    //
    // 픽셀이 아니라 글자 수로 잰다. 한글은 글자 폭이 같아 공백을 뺀 글자 수가 폭에
    // 비례하고, 이 시험이 잡으려는 것은 '한 줄에 들어가는가'가 아니라 **하나만
    // 유독 길어지는 것**이다. 절대 상한을 박으면 화면 폭이 바뀔 때마다 틀린 숫자가 된다.
    const width = (item: { label: string; description: string }) =>
      `${item.label}${item.description}`.replace(/\s/g, "").length;
    const all = READER_AGE_RANGES.find((range) => range.value === "")!;
    const others = READER_AGE_RANGES.filter((range) => range.value !== "");

    expect(width(all)).toBeLessThanOrEqual(Math.max(...others.map(width)));
  });
});
