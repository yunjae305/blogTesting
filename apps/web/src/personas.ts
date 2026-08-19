import type { PersonaCatalogEntry, UserSettings } from "./api/types";

export const CUSTOM_PERSONA_ID = "custom";

const PERSONA_CACHE_KEY = "blog-it:persona-catalog";
const PRESET_ID_LIKE = /^p(?:[_-].*|\d.*)$/i;

function normalizePersonaEntry(value: unknown): PersonaCatalogEntry | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.personaId !== "string" ||
    !candidate.personaId.trim() ||
    typeof candidate.name !== "string" ||
    !candidate.name.trim() ||
    typeof candidate.description !== "string"
  ) {
    return null;
  }

  const personaId = candidate.personaId.trim();
  // 배포 전 캐시에는 kind가 없다. ID로 한 번 변환해 캐시를 버리지 않고 이어 쓴다.
  const kind =
    candidate.kind === undefined
      ? personaId === CUSTOM_PERSONA_ID
        ? "custom"
        : "preset"
      : candidate.kind;

  if (kind === "preset") {
    if (
      personaId === CUSTOM_PERSONA_ID ||
      typeof candidate.prompt !== "string" ||
      !candidate.prompt.trim()
    ) {
      return null;
    }
    return {
      personaId,
      kind,
      name: candidate.name,
      description: candidate.description,
      prompt: candidate.prompt,
    };
  }

  if (
    kind !== "custom" ||
    personaId !== CUSTOM_PERSONA_ID ||
    (candidate.prompt !== null &&
      candidate.prompt !== undefined &&
      typeof candidate.prompt !== "string")
  ) {
    return null;
  }

  return {
    personaId,
    kind,
    name: candidate.name,
    description: candidate.description,
    prompt: typeof candidate.prompt === "string" ? candidate.prompt : null,
  };
}

/** API·캐시 입력에서 유효한 항목만 남기고 프리셋 다음에 custom이 오도록 정렬한다. */
export function normalizePersonaCatalog(value: unknown): PersonaCatalogEntry[] {
  if (!Array.isArray(value)) return [];

  const unique = new Map<string, PersonaCatalogEntry>();
  for (const item of value) {
    const persona = normalizePersonaEntry(item);
    if (!persona || unique.has(persona.personaId)) continue;
    unique.set(persona.personaId, persona);
  }

  return [...unique.values()].sort((left, right) => {
    if (left.kind !== right.kind) return left.kind === "preset" ? -1 : 1;
    return left.personaId.localeCompare(right.personaId, undefined, { numeric: true });
  });
}

export function loadCachedPersonaCatalog(): PersonaCatalogEntry[] {
  try {
    return normalizePersonaCatalog(JSON.parse(localStorage.getItem(PERSONA_CACHE_KEY) ?? "null"));
  } catch {
    return [];
  }
}

export function cachePersonaCatalog(personas: PersonaCatalogEntry[]): void {
  try {
    localStorage.setItem(PERSONA_CACHE_KEY, JSON.stringify(personas));
  } catch {
    // 캐시는 선택 사항이다. 저장 공간이 막혀도 API 목록으로 계속 동작한다.
  }
}

interface ResolvedPersonaSelection {
  personaId: string;
  customPrompt: string;
  resetCustomMetadata: boolean;
}

/** 현재 ID와 과거 프롬프트 전문 저장 형식을 설정 폼의 선택값으로 복원한다. */
export function resolvePersonaSelection(
  settings: UserSettings | null,
  personas: PersonaCatalogEntry[],
): ResolvedPersonaSelection {
  const presets = personas.filter((persona) => persona.kind === "preset");
  if (!settings) {
    return {
      personaId: presets[0]?.personaId ?? "",
      customPrompt: "",
      resetCustomMetadata: false,
    };
  }

  const stored = settings.defaultPersona?.trim() ?? "";
  const savedCustomPrompt = settings.customPersona?.trim() ?? "";

  if (stored === CUSTOM_PERSONA_ID) {
    return {
      personaId: CUSTOM_PERSONA_ID,
      customPrompt: savedCustomPrompt,
      resetCustomMetadata: false,
    };
  }

  const byId = presets.find((persona) => persona.personaId === stored);
  if (byId) {
    return {
      personaId: byId.personaId,
      customPrompt: savedCustomPrompt,
      resetCustomMetadata: false,
    };
  }

  const byPrompt = presets.find((persona) => persona.prompt === stored);
  if (byPrompt) {
    return {
      personaId: byPrompt.personaId,
      customPrompt: savedCustomPrompt,
      resetCustomMetadata: false,
    };
  }

  if (stored) {
    if (PRESET_ID_LIKE.test(stored)) {
      return { personaId: "", customPrompt: stored, resetCustomMetadata: false };
    }
    return {
      personaId: CUSTOM_PERSONA_ID,
      // defaultPersona가 현재 실제 선택이고 customPersona는 보관 중인 별도 초안일 수 있다.
      customPrompt: stored,
      resetCustomMetadata: Boolean(savedCustomPrompt && savedCustomPrompt !== stored),
    };
  }

  return {
    personaId: presets[0]?.personaId ?? "",
    customPrompt: savedCustomPrompt,
    resetCustomMetadata: false,
  };
}

export function personaLabel(
  settings: UserSettings | null,
  personas: PersonaCatalogEntry[],
  catalogLoading = false,
): string {
  if (!settings) return "설정 전";

  const stored = settings.defaultPersona?.trim() ?? "";
  if (!stored) return "지정 안 함";
  const custom = personas.find((persona) => persona.kind === "custom");
  if (stored === CUSTOM_PERSONA_ID) {
    return customPersonaLabel(settings, custom?.name);
  }

  if (!personas.length && PRESET_ID_LIKE.test(stored)) {
    return catalogLoading ? "불러오는 중" : `확인 필요 (${stored})`;
  }

  const preset = personas.find(
    (persona) =>
      persona.kind === "preset" &&
      (persona.personaId === stored || persona.prompt === stored),
  );
  if (preset) return preset.name;

  if (PRESET_ID_LIKE.test(stored)) return `확인 필요 (${stored})`;

  const dormantCustomPrompt = settings.customPersona?.trim() ?? "";
  if (dormantCustomPrompt && dormantCustomPrompt !== stored) {
    return "이전 커스텀 페르소나";
  }

  return customPersonaLabel(settings, custom?.name);
}

/**
 * 커스텀 페르소나의 표시 이름: `커스텀 페르소나(내가 지은 이름)`.
 *
 * 예전에는 지은 이름이 "커스텀 페르소나"를 통째로 대체해서, 화면만 보면 그것이 프리셋인지
 * 내가 만든 것인지 구분이 안 됐다(사용자 지적, 2026-08-03). 괄호 안 따옴표는 잡음이라
 * 뺐다(사용자 지적, 2026-08-04) — 괄호만으로 지은 이름임이 드러난다.
 */
function customPersonaLabel(
  settings: UserSettings,
  catalogName: string | undefined,
): string {
  const base = catalogName?.trim() || "커스텀 페르소나";
  const given = settings.customPersonaName?.trim();
  return given ? `${base}(${given})` : base;
}
