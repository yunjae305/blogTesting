import { describe, expect, it } from "vitest";

import { CUSTOM_PERSONA_ID, personaLabel } from "./personas";
import type { PersonaCatalogEntry, UserSettings } from "./api/types";

function settings(overrides: Partial<UserSettings> = {}): UserSettings {
  return {
    userId: "user_1",
    hashtagCount: 5,
    articleLength: "medium",
    blendMode: "trend",
    defaultPersona: CUSTOM_PERSONA_ID,
    autoPostingEnabled: false,
    createdAt: "2026-08-03T00:00:00.000Z",
    updatedAt: "2026-08-03T00:00:00.000Z",
    ...overrides,
  } as UserSettings;
}

const CATALOG: PersonaCatalogEntry[] = [
  {
    personaId: "p_1",
    kind: "preset",
    name: "트렌드 에디터",
    description: "d",
    prompt: "트렌드 에디터처럼 씁니다.",
  },
  {
    personaId: CUSTOM_PERSONA_ID,
    kind: "custom",
    name: "커스텀 페르소나",
    description: "d",
    prompt: null,
  },
];

describe("personaLabel", () => {
  it("지은 이름은 커스텀 페르소나를 대체하지 않고 괄호로 붙는다", () => {
    // 이름이 통째로 갈아 끼워지면 화면만 봐서는 프리셋인지 내가 만든 것인지 모른다.
    const label = personaLabel(
      settings({ customPersonaName: "IT 전문가" }),
      CATALOG,
    );

    // 괄호 안 따옴표는 잡음이다(2026-08-04) — 괄호만으로 지은 이름임이 드러난다.
    expect(label).toBe("커스텀 페르소나(IT 전문가)");
  });

  it("이름을 짓지 않았으면 그대로 커스텀 페르소나다", () => {
    expect(personaLabel(settings(), CATALOG)).toBe("커스텀 페르소나");
  });

  it("프리셋은 그 프리셋 이름 그대로다", () => {
    const label = personaLabel(
      settings({ defaultPersona: "p_1", customPersonaName: "IT 전문가" }),
      CATALOG,
    );

    expect(label).toBe("트렌드 에디터");
  });
});
