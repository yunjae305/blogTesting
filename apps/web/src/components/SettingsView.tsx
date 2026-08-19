import { useEffect, useRef, useState, type FormEvent } from "react";

import { request } from "../api/client";
import type { ArticleLength, BlendMode, UserSettings } from "../api/types";
import {
  BLEND_MODE_OPTIONS,
  DEFAULT_BLEND_MODE,
} from "../constants";
import { CUSTOM_PERSONA_ID, resolvePersonaSelection } from "../personas";
import { useStore } from "../store";
import { NaverConnect } from "./NaverConnect";
import { ThreadsConnect } from "./ThreadsConnect";

// Must match the backend cap (app/shared/user_settings.py). A settings doc saved
// under the old limit can still hold more, so a loaded value is clamped to this —
// otherwise the slider sits past its max and the save is rejected as out of range.
const MAX_HASHTAG_COUNT = 10;

// 원고 목표 분량. 값은 백엔드 ARTICLE_LENGTHS와 같아야 하고, 목표 글자수는
// prompts.py의 ARTICLE_LENGTH_TARGETS·ARTICLE_LENGTH_IMAGE_RANGES와 맞춘 안내값이다.
// 글자 수만 적어 두면 길이 선택이 이미지 장수까지 정한다는 것이 화면에 안 드러난다.
const ARTICLE_LENGTH_OPTIONS: { id: ArticleLength; label: string; hint: string }[] = [
  // '길게'는 2026-07-31 사용자 결정으로 제거됐다. 저장된 옛 "long" 값은 로드 시
  // medium으로 정규화한다(아래 setArticleLength) — 서버도 long을 medium으로 취급한다.
  // 2026-08-03 사용자 결정으로 목표 분량을 올리고 medium의 화면 이름을 '중간'으로 바꿨다.
  // 저장 값은 그대로 "medium"이라 옛 설정을 다시 읽는 데 영향이 없다.
  // 이미지 장수는 2026-08-07 사용자 결정으로 고정됐다(짧게 2장·중간 3장, 썸네일 포함).
  { id: "short", label: "짧게", hint: "800~1,200자 (공백 포함) · 이미지 2장" },
  { id: "medium", label: "중간", hint: "1,800~2,300자 · 이미지 3장" },
];

/**
 * 페르소나 카드의 작은 장식 아이콘. 카탈로그에는 아이콘 정보가 없으므로 목록 순서로
 * 돌려 쓴다 — 뜻을 담은 값이 아니라 카드끼리 구분되게 하는 장식이라 aria-hidden이다.
 */
function PersonaGlyph({ index }: { index: number }) {
  const common = {
    viewBox: "0 0 24 24",
    "aria-hidden": true,
    focusable: false,
  } as const;
  const shapes = [
    <path key="0" d="m5 17 1-4 8.5-8.5a2.1 2.1 0 0 1 3 3L9 16l-4 1Zm8.5-11.5 3 3" />,
    <path key="1" d="M5 6.5h9.5a2 2 0 0 1 2 2V19l-3.5-2.2L9.5 19l-3.5-2.2L5 18Z" />,
    <path key="2" d="M4 8h7v12H4zM13 4h7v16h-7M6.5 12h2M15.5 9h2" />,
    <path key="3" d="M12 4a5 5 0 0 1 3 9v2H9v-2a5 5 0 0 1 3-9ZM10 19h4" />,
    <path key="4" d="M6 5h12v14l-6-3-6 3zM9.5 10.5 12 13l3.5-4" />,
    <path key="5" d="M5 18V6M5 18h14M8 14l3-3 3 2 4-5M15 8h3v3" />,
    <path key="6" d="M7 4h10v16l-5-3-5 3zM10 9h4" />,
    <path key="7" d="M12 4.5 19 8v8l-7 3.5L5 16V8Zm0 0v15" />,
    <path key="8" d="M12 20s6-5.3 6-9.5A6 6 0 0 0 6 10.5C6 14.7 12 20 12 20Z M12 11h.01" />,
  ];
  return <svg {...common}>{shapes[index % shapes.length]}</svg>;
}

export function SettingsView() {
  const {
    session,
    settings,
    personas,
    personaCatalogLoading,
    reloadPersonaCatalog,
    setSettings,
    setRoute,
    showToast,
    reportError,
    markOnboardingSeen,
  } = useStore();

  const [hashtagCount, setHashtagCount] = useState(5);
  const [articleLength, setArticleLength] = useState<ArticleLength>("medium");
  const [blendMode, setBlendMode] = useState<BlendMode>(DEFAULT_BLEND_MODE);
  const [customName, setCustomName] = useState("");
  const [customDescription, setCustomDescription] = useState("");
  const [customPrompt, setCustomPrompt] = useState("");
  const [personaId, setPersonaId] = useState("");
  const [busy, setBusy] = useState(false);
  const resolvedPersonaKey = useRef<string | null>(null);
  const personaEdited = useRef(false);
  const personaIdRef = useRef(personaId);
  personaIdRef.current = personaId;
  const customCatalogEntry = personas.find((persona) => persona.kind === "custom");
  const catalogComplete = Boolean(
    customCatalogEntry && personas.some((persona) => persona.kind === "preset"),
  );

  // 저장 설정 자체가 바뀔 때만 폼 값을 복원한다. API 카탈로그가 늦게 도착했다는 이유로
  // 사용자가 방금 입력한 해시태그·커스텀 문구를 다시 덮어쓰면 안 된다.
  useEffect(() => {
    if (!settings) {
      personaEdited.current = false;
      resolvedPersonaKey.current = null;
      return;
    }

    setHashtagCount(Math.min(settings.hashtagCount, MAX_HASHTAG_COUNT));
    // 옛 설정의 "long"은 더 이상 옵션이 아니다 — 보통으로 읽고, 다음 저장부터 medium이 된다.
    setArticleLength(
      settings.articleLength === "long" ? "medium" : (settings.articleLength ?? "medium"),
    );
    setBlendMode(settings.blendMode ?? DEFAULT_BLEND_MODE);
    setCustomName(settings.customPersonaName ?? "");
    setCustomDescription(settings.customPersonaDescription ?? "");
    setCustomPrompt(settings.customPersona ?? "");
    personaEdited.current = false;
    resolvedPersonaKey.current = null;
  }, [settings]);

  // ID/레거시 전문 해석만 카탈로그에 의존한다. 같은 저장 문서에 대해 한 번만 실행하므로
  // 캐시 뒤에 최신 API 응답이 도착해도 편집 중인 입력을 건드리지 않는다.
  useEffect(() => {
    const key = settings
      ? `${settings.userId}\u0000${settings.updatedAt}\u0000${settings.defaultPersona}\u0000${settings.customPersona ?? ""}`
      : "new-settings";
    const catalogKey = personas
      .map((persona) => `${persona.personaId}\u0000${persona.kind}\u0000${persona.prompt ?? ""}`)
      .join("\u0001");
    const resolutionKey = `${key}\u0002${catalogKey}`;
    if (resolvedPersonaKey.current === resolutionKey) return;
    if (personaEdited.current) {
      const currentPersonaId = personaIdRef.current;
      if (
        personas.length &&
        currentPersonaId &&
        currentPersonaId !== CUSTOM_PERSONA_ID &&
        !personas.some((persona) => persona.personaId === currentPersonaId)
      ) {
        setPersonaId("");
      }
      resolvedPersonaKey.current = resolutionKey;
      return;
    }

    if (!personas.length) {
      if (settings?.defaultPersona?.trim() === CUSTOM_PERSONA_ID) {
        setPersonaId(CUSTOM_PERSONA_ID);
      }
      resolvedPersonaKey.current = resolutionKey;
      return;
    }

    const resolved = resolvePersonaSelection(settings, personas);
    if (resolved.resetCustomMetadata) {
      setCustomName("이전 커스텀 페르소나");
      setCustomDescription("");
    }
    setCustomPrompt(resolved.customPrompt);
    setPersonaId(resolved.personaId);
    resolvedPersonaKey.current = resolutionKey;
  }, [personas, settings]);

  function selectPersona(id: string) {
    if (id === CUSTOM_PERSONA_ID && !customPrompt.trim()) {
      showToast("커스텀 프롬프트 내용을 먼저 입력해 주세요.", true);
      document.getElementById("customPersona")?.focus();
      return;
    }
    personaEdited.current = true;
    setPersonaId(id);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!session) return;
    if (!personaId) {
      showToast("사용할 페르소나를 다시 선택해 주세요.", true);
      return;
    }
    if (personaId === CUSTOM_PERSONA_ID && !customName.trim()) {
      showToast("커스텀 페르소나 이름을 입력해 주세요.", true);
      document.getElementById("customPersonaName")?.focus();
      return;
    }
    if (personaId === CUSTOM_PERSONA_ID && !customPrompt.trim()) {
      showToast("커스텀 프롬프트 내용을 입력해 주세요.", true);
      document.getElementById("customPersona")?.focus();
      return;
    }

    const body: Record<string, unknown> = {
      hashtagCount,
      articleLength,
      blendMode,
      // 프롬프트 텍스트가 아니라 페르소나 id를 저장한다("custom"이면 아래 customPersona로 텍스트 전달).
      defaultPersona: personaId,
      autoPostingEnabled: true,
    };
    // Blank fields are left out so the server clears them.
    if (customName.trim()) body.customPersonaName = customName.trim();
    if (customDescription.trim()) body.customPersonaDescription = customDescription.trim();
    if (customPrompt.trim()) body.customPersona = customPrompt.trim();

    setBusy(true);
    try {
      const saved = await request<UserSettings>(
        `/users/${encodeURIComponent(session.user.userId)}/settings`,
        { method: "PUT", body },
      );
      setSettings(saved);
      markOnboardingSeen();
      showToast("저장이 되었습니다.");
      // Settings exist to be used, so send them straight into writing.
      setRoute("write");
    } catch (error) {
      reportError(error);
    } finally {
      setBusy(false);
    }
  }

  const personaOptions = personas.map((persona) =>
    persona.kind === "custom"
      ? {
          ...persona,
          // 이름을 지어도 카드가 무엇인지는 남는다: 커스텀 페르소나(내가 지은 이름).
          // 이름으로 통째로 갈아 끼우면 프리셋 카드와 구분이 사라진다(사용자 지적).
          // 괄호 안 따옴표는 잡음이라 뺐다(2026-08-04, personas.ts와 같은 형식).
          name: customName.trim()
            ? `${persona.name}(${customName.trim()})`
            : persona.name,
          description: customDescription.trim() || persona.description,
          prompt: customPrompt.trim() || persona.prompt,
        }
      : persona,
  );

  return (
    <section className="settings-dashboard" aria-labelledby="settings-panel-title">
      <header className="settings-dashboard__header">
        <div className="settings-dashboard__heading">
          <span className="settings-dashboard__mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" focusable="false">
              <circle cx="12" cy="12" r="3.2" />
              <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0V21a1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15H2.9a2 2 0 1 1 0-4H3a1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4V2.9a2 2 0 1 1 4 0V3a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 10h.1a2 2 0 1 1 0 4H21a1.7 1.7 0 0 0-1.6 1Z" />
            </svg>
          </span>
          <div className="settings-dashboard__title">
            <p className="section-kicker">SETTINGS</p>
            <h2 className="panel-title" id="settings-panel-title">
              설정
            </h2>
          </div>
        </div>
        <div className="settings-save-controls">
          <div className="settings-status" aria-live="polite">
            <span className={`badge ${settings ? "ok" : ""}`}>
              {settings ? "저장됨" : "저장 전"}
            </span>
          </div>
          {/* Outside the form, so `form=` is what still submits it. */}
          <button
            className="button primary settings-save-button"
            type="submit"
            form="settingsForm"
            disabled={busy || !personaId}
          >
            {busy ? (
              <>
                <span className="spinner" aria-hidden="true" /> 처리 중
              </>
            ) : (
              "설정 저장"
            )}
          </button>
        </div>
        <p className="settings-dashboard__lede">
          새 원고를 시작할 때 사용할 기본값과 발행 계정을 관리합니다.
        </p>
      </header>

      {/* 왼쪽은 '어떻게 쓸지'(기본값·페르소나), 오른쪽은 '어디로 내보낼지'와 직접 쓰는 지침.
          네 카드가 한 화면에 함께 보여야 지금 설정 상태를 한눈에 훑을 수 있다. */}
      <form id="settingsForm" className="settings-dashboard__grid" onSubmit={save}>
        <div className="settings-dashboard__col">
          <section className="settings-card" aria-labelledby="writing-defaults-title">
            <header className="settings-card__header">
              <span className="settings-card__index" aria-hidden="true">
                01
              </span>
              <div className="settings-card__heading">
                <h3 id="writing-defaults-title">글 생성 기본값</h3>
                <p>원고 길이와 콘텐츠 구성 방식을 미리 정해둡니다.</p>
              </div>
            </header>

            <div className="settings-card__body">
              <div className="settings-rows">
                <div className="settings-row">
                  <div className="settings-row-copy">
                    <label htmlFor="hashtagCount">해시태그 수</label>
                    <span className="settings-row-description">
                      완성된 원고에 제안할 해시태그 개수
                    </span>
                  </div>
                  <div className="settings-row-control">
                    <div className="range-control">
                      <input
                        id="hashtagCount"
                        type="range"
                        min={1}
                        max={MAX_HASHTAG_COUNT}
                        step={1}
                        value={hashtagCount}
                        onChange={(event) => setHashtagCount(Number(event.target.value))}
                      />
                      <output htmlFor="hashtagCount">{hashtagCount}개</output>
                    </div>
                  </div>
                </div>

                <div className="settings-row">
                  <div className="settings-row-copy">
                    <span className="settings-row-label" id="article-length-label">
                      원고 길이
                    </span>
                    <span className="settings-row-description">내용의 깊이와 예상 글자 수</span>
                  </div>
                  <div className="settings-row-control settings-row-control-wide">
                    <div
                      className="segmented segmented--article-length"
                      role="group"
                      aria-labelledby="article-length-label"
                    >
                      {ARTICLE_LENGTH_OPTIONS.map((option) => (
                        <button
                          key={option.id}
                          type="button"
                          className={`segmented-option${articleLength === option.id ? " selected" : ""}`}
                          aria-pressed={articleLength === option.id}
                          onClick={() => setArticleLength(option.id)}
                        >
                          <span className="segmented-label">{option.label}</span>
                          <span className="segmented-hint">{option.hint}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                </div>

                <div className="settings-row">
                  <div className="settings-row-copy">
                    <span className="settings-row-label" id="blend-mode-label">
                      소재·트렌드 결합
                    </span>
                    <span className="settings-row-description">
                      내 아이디어와 추천 트렌드의 반영 비율
                    </span>
                  </div>
                  <div className="settings-row-control settings-row-control-wide">
                    <div className="segmented" role="group" aria-labelledby="blend-mode-label">
                      {BLEND_MODE_OPTIONS.map((option) => (
                        <button
                          key={option.id}
                          type="button"
                          className={`segmented-option${blendMode === option.id ? " selected" : ""}`}
                          aria-pressed={blendMode === option.id}
                          onClick={() => setBlendMode(option.id)}
                        >
                          <span className="segmented-label">{option.label}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              </div>

              {/* 고른 비율이 실제로 무엇을 바꾸는지 한 줄. 행 안에 두면 세그먼트와 겹쳐 읽힌다. */}
              <p className="settings-card__note">
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                  <path d="M9 18h6M10 21h4M8.5 14.5a6 6 0 1 1 7 0c-.8.7-1.2 1.3-1.4 2h-4.2c-.2-.7-.6-1.3-1.4-2Z" />
                </svg>
                {BLEND_MODE_OPTIONS.find((option) => option.id === blendMode)?.description}
              </p>
            </div>
          </section>

          <section
            className="settings-card settings-card--personas"
            aria-labelledby="persona-settings-title"
          >
            <header className="settings-card__header">
              <span className="settings-card__index" aria-hidden="true">
                02
              </span>
              <div className="settings-card__heading">
                <h3 id="persona-settings-title">기본 페르소나</h3>
                <p>글 전체에 적용할 관점과 말투를 선택합니다.</p>
              </div>
              {!catalogComplete && (
                <div className="settings-catalog-status">
                  <p className="hint">
                    {personaCatalogLoading
                      ? "페르소나 카탈로그를 불러오는 중입니다."
                      : "완전한 페르소나 카탈로그를 불러오지 못했습니다."}
                  </p>
                  {!personaCatalogLoading && (
                    <button
                      className="button small"
                      type="button"
                      onClick={() => void reloadPersonaCatalog()}
                    >
                      다시 불러오기
                    </button>
                  )}
                </div>
              )}
            </header>

            <div className="settings-card__body">
              <div className="persona-grid" role="group" aria-labelledby="persona-settings-title">
                {personaOptions.map((persona, index) => {
                  const chosen = persona.personaId === personaId;
                  return (
                    <button
                      key={persona.personaId}
                      className="persona-card"
                      type="button"
                      aria-pressed={chosen}
                      data-persona-id={persona.personaId}
                      onClick={() => selectPersona(persona.personaId)}
                    >
                      {/* 아이콘을 제목 옆이 아니라 왼쪽 칸으로 빼면 제목이 카드 폭을
                          온전히 쓸 수 있어 이름이 서너 줄로 쪼개지지 않는다. */}
                      <span className="persona-card-glyph" aria-hidden="true">
                        <PersonaGlyph index={index} />
                      </span>
                      <span className="persona-card-body">
                        <span className="persona-card-head">
                          <strong>{persona.name}</strong>
                          {/* 선택을 색만으로 알리지 않도록 체크 표시를 함께 둔다. */}
                          {chosen && (
                            <span className="persona-card-check" aria-hidden="true">
                              <svg viewBox="0 0 24 24" focusable="false">
                                <path d="m5 12.5 4.5 4.5L19 7.5" />
                              </svg>
                            </span>
                          )}
                        </span>
                        <span className="persona-card-desc">{persona.description}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          </section>
        </div>

        <div className="settings-dashboard__col">

          {/* 자동 발행에만 쓰이고, 이 폼과는 별개로 즉시 연결한다. */}
          <NaverConnect />

          <ThreadsConnect />

          <section className="settings-card" aria-labelledby="custom-persona-title">
            <header className="settings-card__header">
              <span className="settings-card__index" aria-hidden="true">
                05
              </span>
              <div className="settings-card__heading">
                <h3 id="custom-persona-title">
                  {customCatalogEntry?.name || "커스텀 페르소나"}
                </h3>
                <p>
                  {customCatalogEntry?.description ||
                    "카탈로그를 불러오면 커스텀 페르소나를 설정할 수 있습니다."}
                </p>
              </div>
            </header>

            <div className="settings-card__body">
              <div className="settings-field-grid">
                <div className="field">
                  <label htmlFor="customPersonaName">이름</label>
                  <input
                    id="customPersonaName"
                    maxLength={80}
                    placeholder="실무 경험이 풍부한 IT 전문가"
                    value={customName}
                    onChange={(event) => {
                      personaEdited.current = true;
                      setCustomName(event.target.value);
                    }}
                  />
                </div>
                <div className="field">
                  <label htmlFor="customPersonaDescription">설명 (선택)</label>
                  <input
                    id="customPersonaDescription"
                    maxLength={200}
                    placeholder="이 프롬프트에 대한 간단한 설명"
                    value={customDescription}
                    onChange={(event) => {
                      personaEdited.current = true;
                      setCustomDescription(event.target.value);
                    }}
                  />
                </div>
                <div className="field full">
                  <label htmlFor="customPersona">프롬프트 내용</label>
                  <textarea
                    id="customPersona"
                    maxLength={1200}
                    placeholder="답변의 관점, 말투, 구성 방식, 포함해야 할 내용 등을 적어주세요."
                    value={customPrompt}
                    onChange={(event) => {
                      personaEdited.current = true;
                      setCustomPrompt(event.target.value);
                    }}
                  />
                </div>
              </div>
              <p className="settings-field-note">
                여기에 적은 내용은 위 <strong>커스텀 페르소나</strong>를 골랐을 때 글 작성에
                그대로 쓰입니다.
              </p>
            </div>
          </section>
        </div>
      </form>
    </section>
  );
}
