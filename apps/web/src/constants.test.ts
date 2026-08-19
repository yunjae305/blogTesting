import { describe, expect, it } from "vitest";

import { MAX_TOPIC_CHARS, READER_AGE_RANGES, readerAgeLabel } from "./constants";

describe("연령대 표시", () => {
  it("저장값이 아니라 화면 이름으로 보여준다", () => {
    // 요약 카드에 "30s"가 그대로 떴다(2026-08-05 사용자 지적).
    expect(readerAgeLabel("30s")).toBe("30대");
    expect(readerAgeLabel("20s")).toBe("20대");
  });

  it("고르지 않았으면 '전체'", () => {
    expect(readerAgeLabel("")).toBe("전체");
    expect(readerAgeLabel(null)).toBe("전체");
    expect(readerAgeLabel(undefined)).toBe("전체");
  });

  it("50대와 60대 이상은 하나로 합쳐졌다", () => {
    expect(READER_AGE_RANGES.map((range) => range.value)).toEqual([
      "",
      "10s",
      "20s",
      "30s",
      "40s",
      "50plus",
    ]);
    expect(READER_AGE_RANGES.at(-1)?.label).toBe("50대 이상");
  });

  it("선택지에서 사라진 옛 저장값도 계속 읽힌다", () => {
    // 이미 저장된 글이 연령대를 잃으면 안 된다.
    expect(readerAgeLabel("50s")).toBe("50대 이상");
    expect(readerAgeLabel("60plus")).toBe("50대 이상");
  });

  it("여러 개를 고른 값은 이어서 보여준다", () => {
    expect(readerAgeLabel("20s,30s")).toBe("20대, 30대");
  });

  it("모르는 값은 감추지 않고 그대로 둔다", () => {
    // '전체'로 바꿔 버리면 무엇이 저장돼 있는지 화면에서 알 수 없다.
    expect(readerAgeLabel("70s")).toBe("70s");
  });
});

describe("연령을 고르면 글이 어떻게 달라지는지", () => {
  /**
   * 여기 적는 연령은 **글을 읽는 사람**의 나이다. 글쓴이의 나이가 아니다 — 그것을
   * 말하지 않았더니 제목에 '20대의 시각으로 본 후기'가 나왔다(2026-08-07 신고).
   *
   * 설명은 카드마다 깔지 않고 **고른 뒤에** 하나만 보여 준다. 그래서 길이 제한이
   * 없다 — 한 줄에 욱여넣을 이유가 사라졌다.
   */
  it("연령대마다 설명이 있다", () => {
    for (const range of READER_AGE_RANGES) {
      expect(range.description, `${range.label}에 설명이 없다`).toBeTruthy();
    }
  });

  it("설명은 한 줄로 읽히는 길이다", () => {
    // 처음엔 2~3문장씩 적었더니 길다는 지적을 받았다(2026-08-07). 고른 뒤에 한 번만
    // 보여 주는 자리라 자세히 적을 수 있지만, 그렇다고 문단이 되면 안 읽는다.
    for (const range of READER_AGE_RANGES) {
      expect(range.description.length, `${range.label} 설명이 너무 길다`).toBeLessThanOrEqual(45);
    }
  });

  it("서버가 연령별로 실제 적용하는 지침과 짝이 맞는다", () => {
    // description은 apps/api/app/llm/prompts.py의 READER_AGE_GUIDES를 줄인 것이다.
    // 키가 어긋나면 화면이 서버가 하지 않는 일을 약속하게 된다.
    const withGuides = READER_AGE_RANGES.filter((range) => range.value !== "");
    expect(withGuides.map((range) => range.value)).toEqual(["10s", "20s", "30s", "40s", "50plus"]);
  });

  it("설명이 연령마다 다르다", () => {
    // 연령대별로 확실하게 달라야 한다(2026-08-07 사용자 요청). 두 연령에 같은 설명을
    // 쓰면 고르는 사람이 무엇이 달라지는지 알 수 없다.
    const descriptions = READER_AGE_RANGES.map((range) => range.description);
    expect(new Set(descriptions).size).toBe(descriptions.length);
  });

  it("설명이 독자의 나이라는 것을 흐리지 않는다", () => {
    // '20대의 시각으로'처럼 글쓴이가 그 나이인 것으로 읽히는 표현을 쓰지 않는다.
    for (const range of READER_AGE_RANGES) {
      expect(range.description).not.toContain("시각으로");
    }
  });
});


describe("소재 길이 제한", () => {
  /**
   * 소재는 단어만이 아니다 — 한 문장일 수도 있다(2026-08-06 사용자 지적). 화면이
   * 서버보다 크게 잡으면 다 적고 나서 저장할 때 거절당하고, 작게 잡으면 서버가
   * 받아 주는 것을 못 적는다.
   */
  it("서버의 MAX_TOPIC_CHARS와 같은 값이다", () => {
    // apps/api/app/modules/blog_task/validation.py
    expect(MAX_TOPIC_CHARS).toBe(300);
  });

  it("한 문장이 들어갈 만큼은 된다", () => {
    expect(MAX_TOPIC_CHARS).toBeGreaterThan(100);
  });
});
