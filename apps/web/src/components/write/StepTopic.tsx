import { useEffect, useRef, useState, type FormEvent } from "react";

import { request } from "../../api/client";
import type { BlogTask, ReferenceMaterial, ReferenceMaterialType } from "../../api/types";
// AIONA 앱스튜디오에서 들어온 사람을 알아본다(2026-08-19).
import { currentCampaign } from "../../campaign";
import {
  MAX_DRAFT_COUNT,
  MAX_REFERENCE_MATERIALS,
  MAX_TOPIC_CHARS,
  SAMPLE_INPUT,
  SUBJECT_CATEGORIES,
  WRITING_PURPOSES,
} from "../../constants";
// 예상 소요 시간은 원고 단계의 **실측 소요**에서 그대로 온다(2026-08-11 사용자 실측).
import { DRAFT_STEP_SECONDS } from "../../draftProgress";
import { WRITE_STEP } from "../../resume";
import { useStore } from "../../store";
import {
  MAX_REFERENCE_FILE_BYTES,
  MAX_REFERENCE_FILES_BYTES,
  collectReferenceMaterials,
  referenceTypeForFile,
  referenceUrlProblem,
} from "../../utils";
// 파일 올리는 칸은 브랜드 자료 편집과 같은 것을 쓴다(2026-08-07 통일).
import { FileDropZone } from "../FileDropZone";
// 브랜드 글쓰기가 이 화면으로 들어왔다(2026-08-11). 별도 메뉴였을 때와 같은 편집기를
// 같은 자리에서 연다 — 화면을 옮기면 적던 소재·목적·참고자료가 통째로 날아간다.
import { BrandPicker } from "./BrandPicker";
import { platformMark } from "../scheduled/PlatformToggle";
// 작업 시각 고르개. 달력과 오전/오후·시·분을 직접 그린다(2026-08-12).
// 자동 포스팅의 줄마다 있는 시각 칸도 같은 것을 쓴다 — 그래서 화면 폴더 밖에 둔다.
import { SchedulePicker } from "../SchedulePicker";
// 글 목적·대상 연령 선택지.
import { AudienceChoices, AudienceNote, PurposeChoices } from "./BriefChoices";
import {
  boltIcon,
  calendarIcon,
  clockIcon,
  docIcon,
  sendIcon,
} from "./BriefIcons";

/** 원고 한 편에 걸리는 시간(분). 실측 단계 소요를 더해 반올림한다. */
const DRAFT_MINUTES = Math.round(
  DRAFT_STEP_SECONDS.reduce((total, seconds) => total + seconds, 0) / 60,
);

export type BriefPreview = {
  topic: string;
  purpose: string;
  readerAgeRange: string | null;
  subjectCategory: string | null;
};

type ReferenceTab = "memo" | "url" | "file";

type ReferenceEntry =
  | { id: string; kind: "saved"; material: ReferenceMaterial }
  | { id: string; kind: "memo"; value: string }
  | { id: string; kind: "url"; value: string }
  | { id: string; kind: "file"; file: File };

type ReferenceEntryView = {
  type: ReferenceMaterialType;
  badge: string;
  tone: "memo" | "url" | "pdf" | "image" | "text-file";
  detail: string;
  bytes?: number;
};

const MAX_REFERENCE_MEMO_CHARS = 1_000;

/**
 * 저장된 UTC 시각 → `datetime-local` 입력이 읽는 로컬 문자열("2026-08-13T15:00").
 *
 * 사용자는 자기 시계로 시각을 고르고 서버는 UTC로만 저장한다. 변환을 한 곳에서만 하지
 * 않으면 날짜가 하루 밀리는 종류의 버그가 생긴다(예약 포스팅이 같은 규칙을 쓴다).
 */
export function toLocalInputValue(iso: string | null | undefined): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "";
  const shifted = new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

/** `datetime-local` 로컬 문자열 → 서버에 보낼 UTC ISO. 비었으면 null(예약 없음). */
export function toUtcIso(localValue: string): string | null {
  const trimmed = (localValue || "").trim();
  if (!trimmed) return null;
  const parsed = new Date(trimmed);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString();
}

function dataUrlBytes(value: string): number {
  const comma = value.indexOf(",");
  if (comma < 0 || !value.slice(0, comma).includes(";base64")) return 0;
  const payload = value.slice(comma + 1).replace(/\s/g, "");
  const padding = payload.endsWith("==") ? 2 : payload.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor((payload.length * 3) / 4) - padding);
}

function textBytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function formatFileBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    const value = bytes / 1024 / 1024;
    return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)}MB`;
  }
  return `${Math.max(1, Math.ceil(bytes / 1024))}KB`;
}

function viewOfReference(entry: ReferenceEntry): ReferenceEntryView {
  if (entry.kind === "memo") {
    return { type: "TEXT", badge: "메모", tone: "memo", detail: entry.value };
  }
  if (entry.kind === "url") {
    return { type: "URL", badge: "URL", tone: "url", detail: entry.value };
  }
  if (entry.kind === "file") {
    const type = referenceTypeForFile(entry.file) ?? "TEXT";
    return {
      type,
      badge: type === "PDF" ? "PDF" : type === "IMAGE" ? "이미지" : "TXT",
      tone: type === "PDF" ? "pdf" : type === "IMAGE" ? "image" : "text-file",
      detail: entry.file.name,
      bytes: entry.file.size,
    };
  }

  const { material } = entry;
  if (material.type === "URL") {
    return { type: "URL", badge: "URL", tone: "url", detail: material.value };
  }
  if (material.type === "TEXT" && !material.name) {
    return { type: "TEXT", badge: "메모", tone: "memo", detail: material.value };
  }
  const type = material.type;
  const bytes =
    type === "TEXT" ? textBytes(material.value) : dataUrlBytes(material.value);
  return {
    type,
    badge: type === "PDF" ? "PDF" : type === "IMAGE" ? "이미지" : "TXT",
    tone: type === "PDF" ? "pdf" : type === "IMAGE" ? "image" : "text-file",
    detail:
      material.name ||
      (type === "PDF" ? "PDF 파일" : type === "IMAGE" ? "이미지 파일" : "텍스트 파일"),
    bytes,
  };
}

function ReferenceIcon({ type }: { type: ReferenceMaterialType }) {
  if (type === "URL") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M9.5 14.5 14.5 9M7.2 16.8l-1 1a3.5 3.5 0 0 1-5-5l3.2-3.2a3.5 3.5 0 0 1 5 0M16.8 7.2l1-1a3.5 3.5 0 0 1 5 5l-3.2 3.2a3.5 3.5 0 0 1-5 0" />
      </svg>
    );
  }
  if (type === "IMAGE") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <circle cx="8.5" cy="9" r="1.5" />
        <path d="m5 17 4.2-4.2 3.1 3.1 2.2-2.2L19 18" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M6 3h8l4 4v14H6z" />
      <path d="M14 3v5h5M9 12h6M9 16h6" />
    </svg>
  );
}

/**
 * 브랜드가 이 글에서 맡을 역할을 고르는 칸(2026-08-19).
 *
 * 소재 칸과 브랜드 칸의 잠금을 없애면서 생긴 자리다. 둘을 함께 고를 수 있게 된 순간
 * 같은 조합이 **정반대의 두 글**을 뜻하게 됐고, 그 차이는 화면 어디에도 드러나지 않는다.
 * 사용자가 기대한 글과 나온 글이 다르면 원고 한 편을 다 만들고 나서야 알게 된다.
 *
 * 기본값을 활용으로 두는 이유: 소재를 적고 브랜드까지 고르는 사람이 원하는 것은 대개
 * "트렌드 글에 우리 서비스를 자연스럽게" 쪽이고, 브랜드가 주인공인 글은 **소재를 비우는
 * 것**으로 이미 표현할 수 있다.
 */
function BrandRoleChoice({
  brandName,
  role,
  locked,
  topic,
  onChange,
}: {
  brandName: string;
  role: "UTILITY" | "FOCUS";
  /** 소재가 비어 있어 고를 것이 없는 상태. 브랜드가 주인공일 수밖에 없다. */
  locked: boolean;
  topic: string;
  onChange: (next: "UTILITY" | "FOCUS") => void;
}) {
  const name = brandName || "브랜드";
  if (locked) {
    return (
      <p className="field-desc brand-role-note">
        소재를 비웠으므로 <strong>{name}</strong>가 글의 주인공이 됩니다. 트렌드 글에
        곁들여 쓰려면 위에 소재를 적어 주세요.
      </p>
    );
  }

  const choices = [
    {
      value: "UTILITY" as const,
      label: "활용한 도구로",
      hint: `'${topic}'가 글의 주인공이고, ${name}는 그 과정에서 쓴 도구로 등장합니다.`,
    },
    {
      value: "FOCUS" as const,
      label: "글의 주인공으로",
      hint: `${name} 자체를 소개하는 글을 씁니다.`,
    },
  ];

  return (
    <div className="brand-role">
      <div className="brand-role-choices" role="radiogroup" aria-label="브랜드의 역할">
        {choices.map((choice) => (
          <button
            key={choice.value}
            type="button"
            role="radio"
            aria-checked={role === choice.value}
            className={`brand-role-choice${role === choice.value ? " is-on" : ""}`}
            onClick={() => onChange(choice.value)}
          >
            <span className="brand-role-choice-label">{choice.label}</span>
            <span className="brand-role-choice-hint">{choice.hint}</span>
          </button>
        ))}
      </div>

    </div>
  );
}

type StepTopicProps = {
  onPreviewChange?: (preview: BriefPreview) => void;
  /**
   * 소재가 이미 정해진 흐름에서 쓴다(브랜드 글쓰기 — 트렌드 키워드가 소재다).
   * 칸을 숨기고, 소재는 부모가 준 값을 그대로 보낸다.
   */
  fixedTopic?: string;
  /**
   * 저장을 부모가 대신 한다. 주면 `/posts`·`/posts/{id}/input`을 부르지 않는다.
   *
   * 브랜드 글쓰기에서 필요하다 — 여기서 그냥 저장하면 참고자료를 통째로 갈아치워
   * **브랜드 자료(TEXT·URL)가 날아간다.** 부모가 브랜드 자료와 합쳐 보내야 한다.
   */
  onSubmitInput?: (body: Record<string, unknown>) => Promise<void>;
  submitLabel?: string;
};

export function StepTopic({
  onPreviewChange,
  fixedTopic,
  onSubmitInput,
  submitLabel,
}: StepTopicProps = {}) {
  const { task, setTask, setStep, setRecommendation, showToast, reportError } = useStore();

  // Re-entering this step to fix something (수정하기 in the verify popup) has to
  // show what was written, not an empty form.
  const saved = task?.input;
  const savedPurpose = saved?.purpose?.[0] ?? saved?.keywords?.[0] ?? "";
  const picked = WRITING_PURPOSES.includes(savedPurpose);

  // 브랜드 자료는 서버가 펼쳐 넣은 것이라(origin="brand") 사용자의 '추가된 참고자료'
  // 목록에 섞으면 안 된다 — 수십 개가 목록을 뒤덮고, 거기서 지울 수 있어서도 안 된다
  // (브랜드 자료 편집에서 지우는 값이다). 저장할 때도 서버가 다시 채운다.
  const savedMaterials = (saved?.referenceMaterials ?? []).filter(
    (material) => material.origin !== "brand",
  );

  const [topic, setTopic] = useState(fixedTopic ?? saved?.topic ?? "");
  const [purpose, setPurpose] = useState(picked ? savedPurpose : "");
  const [customPurpose, setCustomPurpose] = useState(picked ? "" : savedPurpose);
  // 소재 분야. null은 "아직 고르지 않음"이다 — 필수라 고르지 않으면 넘어가지 못한다.
  // 옛 글에는 이 값이 없으므로(undefined) 다시 고르게 된다.
  const [subjectCategory, setSubjectCategory] = useState<string | null>(
    saved?.subjectCategory ?? null,
  );
  // 고른 브랜드. 빈 문자열이 "브랜드 없이 쓰기"다 — 브랜드는 선택이다.
  const [brandId, setBrandId] = useState(saved?.brandId ?? "");
  // 고른 브랜드의 이름. 저장은 서버가 하지만(brandName), 요약 칸이 '소재' 자리에
  // 무엇을 그릴지는 화면이 지금 알아야 한다.
  const [brandName, setBrandName] = useState(saved?.brandName ?? "");
  // 대상 연령은 필수다. null은 "아직 고르지 않음", ""은 "전체"를 뜻하는 실제 선택이라
  // 둘을 구분한다 — 전체도 하나의 유효한 답이므로 명시적으로 골라야 넘어간다.
  //
  // **이미 저장된 글이면 '전체'로 되살린다.** 화면은 전체를 ""로 두는데 서버는 빈 문자열을
  // 받지 않아(readerAgeRange must be a non-empty string) 전체를 고르면 아예 보내지 않는다.
  // 그래서 저장된 글에는 이 값이 없고, 돌아왔을 때 '고르지 않음'과 구분되지 않아 연령만
  // 풀린 채로 보였다 — 나머지는 다 채워져 있는데 이것만 다시 눌러야 했다(2026-08-11 신고).
  // 저장은 연령을 고르지 않으면 막히므로, 글이 있다는 것은 그때 골랐다는 뜻이다. 연령을
  // 묻기 전에 만들어진 옛 글도 서버가 '전체'로 다루므로(reader_age_label(None)) 답은 같다.
  const [ageRange, setAgeRange] = useState<string | null>(
    saved?.readerAgeRange ?? (task ? "" : null),
  );
  // 원고 작업 시각(2026-08-11). 화면은 사용자의 로컬 시간으로 다루고(datetime-local),
  // 보낼 때만 UTC로 옮긴다 — 서버는 UTC 한 가지로 저장하고 변환하지 않는다.
  const [scheduledRunAt, setScheduledRunAt] = useState(() =>
    toLocalInputValue(saved?.scheduledRunAt),
  );
  /**
   * 지금 바로 만들 것인가, 시각을 걸어 둘 것인가(2026-08-12 사용자 시안).
   *
   * 값 자체는 여전히 `scheduledRunAt` 하나다 — 비어 있으면 지금 바로다. 이 상태는
   * **화면의 것**이다: 예약을 고르고 아직 시각을 안 골랐을 때 날짜 칸을 열어 두려면
   * '비어 있음'과 '예약을 고름'을 갈라 두어야 한다.
   */
  const [scheduleMode, setScheduleMode] = useState<"now" | "later">(
    saved?.scheduledRunAt ? "later" : "now",
  );
  /**
   * 고를 수 있는 가장 이른 시각. **한 번 계산해 두면 안 된다**(2026-08-12 사용자 신고).
   *
   * 화면을 9시에 열고 9시 10분을 고른 뒤, 편수·카테고리를 고르는 동안 그 시각이 지나면
   * 저장할 때 서버가 거부한다. 사용자는 방금 고른 값이 왜 거부되는지 알 수 없다. 그래서
   * 달력을 열 때마다 지금을 다시 잰다.
   */
  const [minScheduleValue, setMinScheduleValue] = useState(() =>
    toLocalInputValue(new Date().toISOString()),
  );
  const refreshScheduleFloor = () =>
    setMinScheduleValue(toLocalInputValue(new Date().toISOString()));
  /**
   * 이 소재로 만들 원고 수(2026-08-12). 1편이면 지금까지와 똑같다.
   *
   * 2편 이상이면 **작업이 줄지어 돈다** — 첫 편이 끝나야 다음 편이 시작한다. 편마다
   * 시각을 따로 받지 않는 이유다. 한 편이 5~8분 걸리므로 시각을 따로 받게 하면
   * 사용자가 그 시간을 계산해 간격을 띄워야 한다.
   */
  const [draftCount, setDraftCount] = useState(saved?.draftCount ?? 1);
  /**
   * 원고를 다 만들면 **그대로 발행까지** 할지(2026-08-12 사용자 요청).
   *
   * 기본은 네이버 켬이고, **둘 다 끄고는 넘어갈 수 없다**(2026-08-13 사용자 지시).
   * 그날 아침에 잠깐 둘 다 꺼짐으로 두었는데, 그러면 원고만 만들고 아무 데도 올리지
   * 않는 글이 작업 큐에 서고 화면·진행바·로그가 전부 그 예외를 설명해야 했다.
   * 올릴 곳을 반드시 고르게 하는 편이 단순하다는 사용자 결정이다.
   *
   * 예약 화면·재예약은 예전부터 같은 규칙이었다(발행할 플랫폼 하나 이상).
   */
  const [autoNaver, setAutoNaver] = useState(saved?.autoPublishNaver ?? true);
  const [autoThreads, setAutoThreads] = useState(saved?.autoPublishThreads ?? false);
  const autoPublish = autoNaver || autoThreads;

  /**
   * 지금 설정이 뜻하는 것을 한 문장으로. 시각·편수·자동 발행 셋이 서로 얽혀 있어서,
   * 칸마다 따로 설명하면 합쳤을 때 무엇이 되는지는 여전히 알 수 없다.
   */
  /** "2026-08-13T14:00" → "8월 13일 오후 2시". 로컬 입력값 그대로 읽는다. */
  function formatScheduleMoment(value: string): string {
    const at = new Date(value);
    if (Number.isNaN(at.getTime())) return "정한 시각";
    return at.toLocaleString("ko-KR", {
      month: "long",
      day: "numeric",
      hour: "numeric",
      minute: at.getMinutes() === 0 ? undefined : "2-digit",
    });
  }

  const scheduleSummary = (() => {
    const when = scheduledRunAt
      ? `${formatScheduleMoment(scheduledRunAt)}에 시작해`
      : "지금 바로";
    const what =
      draftCount === 1
        ? "원고 1편을 만듭니다"
        : `같은 소재를 서로 다른 방향 ${draftCount}가지로 ${draftCount}편 만듭니다`;
    const where = [autoNaver && "네이버", autoThreads && "쓰레드"]
      .filter(Boolean)
      .join("와 ");
    // 올릴 곳을 고르지 않으면 저장 자체가 막힌다(2026-08-13). 그러니 여기서도 '발행을
    // 안 한다'가 아니라 **아직 고르지 않았다**고 말해야 한다.
    const then = autoPublish
      ? draftCount === 1
        ? ` 만들어지면 ${where}에 올립니다.`
        : ` 만들어지는 대로 1편부터 차례로 ${where}에 올립니다.`
      : " 올릴 곳을 하나 이상 골라 주세요.";
    return `${when} ${what}.${then}`;
  })();
  const [referenceTab, setReferenceTab] = useState<ReferenceTab>("memo");
  const [memoDraft, setMemoDraft] = useState("");
  const [urlDraft, setUrlDraft] = useState("");
  const referenceSequence = useRef(savedMaterials.length);
  const [referenceEntries, setReferenceEntries] = useState<ReferenceEntry[]>(() =>
    savedMaterials.map((material, index) => ({
      id: `saved-reference-${index}`,
      kind: "saved" as const,
      material,
    })),
  );

  const urlProblem = referenceUrlProblem(urlDraft);
  const hasPendingReferenceDraft = Boolean(memoDraft.trim() || urlDraft.trim());
  const chosenReferenceCount = referenceEntries.length;
  const chosenFileBytes = referenceEntries.reduce((total, entry) => {
    const view = viewOfReference(entry);
    return total + (view.bytes ?? 0);
  }, 0);
  const [busy, setBusy] = useState(false);

  const editing = Boolean(task);

  // --- 소재와 브랜드는 **함께** 고른다(2026-08-19 사용자 지시) ---
  //
  // 2026-08-11에는 둘이 서로를 잠갔다. 브랜드 글쓰기는 등록해 둔 소개·핵심 기능으로
  // 쓰므로 소재가 필요 없고, 소재를 적은 글에 브랜드를 얹으면 방향이 엉킨다는 판단이었다.
  //
  // 그런데 이 저장소가 실제로 만들려는 글이 바로 그 "엉킨다"고 봤던 조합이다: **트렌드가
  // 주인공이고 브랜드는 그 상황에서 쓴 도구인 글.** 검색해서 들어온 사람에게 먼저 답을
  // 주고 그 과정에서 브랜드를 발견하게 하는 글이라, 소재와 브랜드가 둘 다 있어야 한다.
  // 방향이 엉키던 원인은 두 칸이 함께 있는 것이 아니라 **프롬프트가 브랜드를 언제나
  // 주인공으로 두던 것**이었고, 그쪽을 고쳤다(llm/prompts.brand_utility_rules).
  //
  // 그래서 잠금을 없앤다. 대신 무엇이 주인공인지를 **고르게** 한다(brandMode).
  const brandPicked = Boolean(brandId);
  const hasTopic = Boolean(topic.trim());
  /**
   * 브랜드가 이 글에서 맡는 역할. 소재 없이 브랜드만 고르면 브랜드가 주인공일 수밖에
   * 없으므로(소재 자리를 브랜드 이름이 채운다) 그때는 값이 무엇이든 FOCUS다.
   *
   * 저장된 글에는 서버가 확정한 값이 들어 있다. 처음 여는 화면은 UTILITY가 기본이다 —
   * 소재를 적고 브랜드를 고르는 사람이 원하는 것은 대개 그 글이고, 브랜드가 주인공인
   * 글은 소재를 비우는 것으로 이미 표현된다.
   */
  const [brandRole, setBrandRole] = useState<"UTILITY" | "FOCUS">(
    saved?.brandMode === "FOCUS" ? "FOCUS" : "UTILITY",
  );
  const brandMode: "UTILITY" | "FOCUS" = hasTopic ? brandRole : "FOCUS";
  /** 브랜드가 주인공인 글인가 — 소재 칸을 브랜드 이름이 대신하는 그 글이다. */
  const writingWithBrand = brandPicked && brandMode === "FOCUS";

  function fillSample() {
    setTopic(SAMPLE_INPUT.topic);
    setPurpose(SAMPLE_INPUT.purpose);
    setCustomPurpose("");
    setAgeRange(SAMPLE_INPUT.readerAgeRange);
    setSubjectCategory(SAMPLE_INPUT.subjectCategory);
    showToast("샘플 입력값을 채웠습니다.");
  }

  // A typed purpose wins over a picked one, as in the original.
  const purposes = customPurpose.trim() ? [customPurpose.trim()] : purpose ? [purpose] : [];

  // 소재를 다 적고 다음 칸(글 목적·대상 연령·참고 자료)을 만지기 시작하면 소재 관련
  // 키워드 수집을 미리 시작한다(2026-08-10 사용자 요청) — 남은 입력 시간이 곧 수집과
  // 관련도 판정이 도는 시간이 된다. 같은 소재로는 한 번만 보내고, 실패는 조용히
  // 버린다(가속 장치일 뿐, 트렌드 화면의 요청이 어차피 다시 모은다). 브랜드 흐름
  // (fixedTopic)은 트렌드 키워드가 곧 소재라 데울 것이 없다.
  const warmedTopics = useRef<Set<string>>(new Set());
  function warmTrendKeywords(nextPurpose?: string) {
    const trimmed = topic.trim();
    if (!trimmed || fixedTopic || warmedTopics.current.has(trimmed)) return;
    warmedTopics.current.add(trimmed);
    const chosen = nextPurpose?.trim() ? [nextPurpose.trim()] : purposes;
    void request("/trends/prefetch", {
      method: "POST",
      body: { topic: trimmed, purpose: chosen },
    }).catch(() => {});
  }

  useEffect(() => {
    onPreviewChange?.({
      // 브랜드로 쓰는 글은 브랜드가 곧 소재다 — 요약 칸이 빈 줄을 보여 주면 안 된다.
      topic: writingWithBrand ? brandName : topic,
      purpose: purposes[0] ?? "",
      readerAgeRange: ageRange,
      subjectCategory,
    });
  }, [
    ageRange,
    brandName,
    customPurpose,
    onPreviewChange,
    purpose,
    subjectCategory,
    topic,
    writingWithBrand,
  ]);

  function nextReferenceId(prefix: string) {
    referenceSequence.current += 1;
    return `${prefix}-${referenceSequence.current}`;
  }

  function referenceRoom(): boolean {
    if (chosenReferenceCount < MAX_REFERENCE_MATERIALS) return true;
    showToast(`참고자료는 최대 ${MAX_REFERENCE_MATERIALS}개까지 추가할 수 있습니다.`, true);
    return false;
  }

  function addMemo() {
    const value = memoDraft.trim();
    if (!value || !referenceRoom()) return;
    setReferenceEntries((prev) => [
      ...prev,
      { id: nextReferenceId("memo"), kind: "memo", value },
    ]);
    setMemoDraft("");
    warmTrendKeywords();
  }

  function addUrl() {
    const value = urlDraft.trim();
    if (!value) return;
    if (urlProblem) {
      showToast(`참고 URL을 확인해 주세요 — ${urlProblem}`, true);
      return;
    }
    const duplicate = referenceEntries.some((entry) => {
      const view = viewOfReference(entry);
      return view.type === "URL" && view.detail === value;
    });
    if (duplicate) {
      showToast("이미 추가한 URL입니다.", true);
      return;
    }
    if (!referenceRoom()) return;
    setReferenceEntries((prev) => [
      ...prev,
      { id: nextReferenceId("url"), kind: "url", value },
    ]);
    setUrlDraft("");
    warmTrendKeywords();
  }

  function addFiles(picked: File[]) {
    if (!picked.length) return;
    warmTrendKeywords();

    const accepted: ReferenceEntry[] = [];
    let count = chosenReferenceCount;
    let totalBytes = chosenFileBytes;
    let rejectedType = false;
    let rejectedSize = false;
    let rejectedTotal = false;
    let rejectedCount = false;

    for (const file of picked) {
      if (!referenceTypeForFile(file)) {
        rejectedType = true;
        continue;
      }
      if (file.size > MAX_REFERENCE_FILE_BYTES) {
        rejectedSize = true;
        continue;
      }
      if (count >= MAX_REFERENCE_MATERIALS) {
        rejectedCount = true;
        continue;
      }
      if (totalBytes + file.size > MAX_REFERENCE_FILES_BYTES) {
        rejectedTotal = true;
        continue;
      }
      accepted.push({ id: nextReferenceId("file"), kind: "file", file });
      count += 1;
      totalBytes += file.size;
    }

    if (accepted.length) setReferenceEntries((prev) => [...prev, ...accepted]);
    if (rejectedType) showToast("TXT, PNG, JPG, JPEG, PDF 파일만 추가할 수 있습니다.", true);
    else if (rejectedSize) showToast(
        `파일 하나는 최대 ${MAX_REFERENCE_FILE_BYTES / 1024 / 1024}MB까지 올릴 수 있습니다.`,
        true,
      );
    else if (rejectedTotal) showToast(
        `첨부 파일 합계는 최대 ${MAX_REFERENCE_FILES_BYTES / 1024 / 1024}MB입니다.`,
        true,
      );
    else if (rejectedCount) {
      showToast(`참고자료는 최대 ${MAX_REFERENCE_MATERIALS}개까지 추가할 수 있습니다.`, true);
    }
  }

  function removeReference(id: string) {
    setReferenceEntries((prev) => prev.filter((entry) => entry.id !== id));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();

    if (purposes.length === 0) {
      showToast("글 목적을 하나 선택하거나 입력해 주세요.", true);
      return;
    }
    // 소재 분야는 필수다. 같은 이름의 다른 분야로 글이 새는 것을 막는 값이라, 비워 두면
    // 그 판단을 다시 모델에게 떠넘기는 셈이 된다.
    if (subjectCategory === null) {
      showToast("카테고리를 선택해 주세요. 같은 이름의 다른 분야로 글이 새는 것을 막아줍니다.", true);
      return;
    }
    if (ageRange === null) {
      showToast("대상 연령을 선택해 주세요. 상관없으면 '전체'를 골라주세요.", true);
      return;
    }
    // 탭 안에 작성만 하고 '추가'하지 않은 값은 오른쪽 목록에 없으므로 저장 대상이 아니다.
    // 조용히 버리지 않고 무엇을 눌러야 하는지 알려 준다.
    if (hasPendingReferenceDraft) {
      showToast("작성 중인 메모나 URL은 먼저 '추가' 버튼을 눌러주세요.", true);
      return;
    }
    // **올릴 곳이 하나도 없으면 넘어가지 않는다**(2026-08-13 사용자 지시). 서버도 같은
    // 것을 거부하지만(blog_task/validation.py), 여기서 먼저 잡아야 무엇을 고쳐야 하는지
    // 그 자리에서 알 수 있다 — 다른 필수 칸(카테고리·연령)과 같은 방식이다.
    if (!autoPublish) {
      showToast("원고를 올릴 곳을 하나 이상 골라 주세요(네이버·쓰레드).", true);
      return;
    }

    setBusy(true);
    try {
      const savedReferences = referenceEntries
        .filter(
          (entry): entry is Extract<ReferenceEntry, { kind: "saved" }> =>
            entry.kind === "saved",
        )
        .map((entry) => entry.material);
      const newMemos = referenceEntries
        .filter(
          (entry): entry is Extract<ReferenceEntry, { kind: "memo" }> => entry.kind === "memo",
        )
        .map((entry) => entry.value);
      const newUrls = referenceEntries
        .filter(
          (entry): entry is Extract<ReferenceEntry, { kind: "url" }> => entry.kind === "url",
        )
        .map((entry) => entry.value);
      const newFiles = referenceEntries
        .filter(
          (entry): entry is Extract<ReferenceEntry, { kind: "file" }> => entry.kind === "file",
        )
        .map((entry) => entry.file);
      const collected = await collectReferenceMaterials({
        text: newMemos,
        url: newUrls,
        files: newFiles,
        existingCount: savedReferences.length,
      });

      const body: Record<string, unknown> = {
        // 브랜드가 **주인공**인 글은 소재를 비워 보낸다 — 서버가 그 브랜드 이름으로
        // 채운다. 화면이 채워 보내면 브랜드를 지운 뒤에도 그 이름이 소재로 남을 수 있다.
        // 브랜드를 도구로 쓰는 글은 소재가 곧 글의 주인공이므로 그대로 보낸다.
        topic: writingWithBrand ? "" : topic.trim(),
        purpose: purposes,
        keywords: purposes,
        subjectCategory,
        // 브랜드 자료는 여기서 싣지 않는다 — brandId만 보내면 서버가 저장된 자료를
        // 펼쳐 넣는다. 화면이 실어 보내면 base64 이미지까지 왕복하고, 브랜드를 바꿨을
        // 때 옛 자료를 걷어낼 방법도 없다.
        brandId: brandId || undefined,
        // 브랜드가 맡을 역할(2026-08-19). 브랜드를 안 골랐으면 보내지 않는다.
        //
        // 서버가 확인하고 다시 정한다 — 소재가 비어 있으면 UTILITY는 성립하지 않으므로
        // (소재 자리를 브랜드 이름이 채운다) 그때는 FOCUS로 되돌린다. 여기서 보내는 것은
        // **소재와 브랜드가 둘 다 있을 때 사용자가 고른 쪽**이다.
        brandMode: brandId ? brandMode : undefined,
        referenceMaterials: [...savedReferences, ...collected],
      };
      // "전체"("")와 미선택(null)은 연령 조건 없이 보낸다 — 백엔드는 연령을 안 받으면
      // 전체로 다룬다. 특정 연령을 골랐을 때만 값을 실어 보낸다.
      if (ageRange) body.readerAgeRange = ageRange;

      // 원고 작업 시각. 비워 두면 아예 보내지 않는다 — 서버는 값이 없으면 예전 그대로
      // 방향을 고르는 즉시 원고를 만든다(단일 글 작성).
      // 1편은 보내지 않는다 — 서버는 값이 없으면 예전 그대로 한 편만 만든다.
      if (draftCount > 1) body.draftCount = draftCount;
      // 서버 기본(네이버만 켬)과 다를 때만 실어 보낸다. 둘 다 끈 조합은 위에서 막혔으니
      // 여기 오는 `autoPublishNaver: false`는 언제나 '쓰레드만'이다.
      if (!autoNaver) body.autoPublishNaver = false;
      if (autoThreads) body.autoPublishThreads = true;

      const scheduledUtc = toUtcIso(scheduledRunAt);
      // 예약을 골라 놓고 시각을 안 고른 채로 저장하면 **말없이 '지금 바로'가 된다**
      // (값은 비어 있으므로). 고른 것과 저장되는 것이 다르므로 여기서 막는다.
      if (scheduleMode === "later" && !scheduledUtc) {
        showToast("예약 발행 일시를 골라 주세요.", true);
        return;
      }
      // 고르는 동안 그 시각이 지나 버렸는가. 서버도 같은 것을 거부하지만, 여기서 먼저
      // 잡아야 **무엇을 고쳐야 하는지 그 자리에서** 알 수 있다(2026-08-12 사용자 신고).
      if (scheduledUtc && new Date(scheduledUtc).getTime() <= Date.now()) {
        refreshScheduleFloor();
        showToast("원고 작업 예정 시각이 이미 지났습니다. 다시 골라 주세요.", true);
        return;
      }
      if (scheduledUtc) {
        body.scheduledRunAt = scheduledUtc;
        // 표시·감사용. 계산에는 쓰지 않는다(서버는 UTC로만 잰다).
        body.scheduledTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      }

      // 저장을 부모가 맡는 흐름(브랜드 글쓰기)에서는 여기서 글을 만들지 않는다.
      // 그냥 저장하면 참고자료를 통째로 갈아치워 브랜드 자료가 날아간다.
      if (onSubmitInput) {
        await onSubmitInput(body);
        return;
      }

      // Coming back to fix the input rewrites the post in place. Posting to /posts
      // again would quietly leave a second, abandoned post behind.
      const saved = task
        ? await request<BlogTask>(`/posts/${task.postId}/input`, { method: "PUT", body })
        : await request<BlogTask>("/posts", { method: "POST", body });

      setTask(saved);
      // 화면에 남아 있던 트렌드 키워드는 **옛 소재로 모은 것**이다. 서버도 같은 판단을
      // 해서 입력을 다시 쓰면 글을 흐름의 처음으로 되돌리고 키워드를 다시 모은다
      // (update_blog_task_input). 여기서 비우지 않으면 제목 단계가 이미 목록이 있다고
      // 보고 다시 모으지 않아, 고친 소재와 상관없는 카드가 그대로 남는다. 비워 두면
      // 제목 단계가 지금 고른 보기 방식(trendMode) 그대로 새 소재의 후보를 모은다.
      setRecommendation(null);
      showToast(task ? "소재를 수정했습니다. 제목부터 다시 골라주세요." : "소재를 저장했습니다.");
      setStep(WRITE_STEP.TITLE);
    } catch (error) {
      // collectReferenceMaterials throws plain Errors with user-facing messages.
      if (error instanceof Error && !("status" in error)) showToast(error.message, true);
      else reportError(error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="write-step--brief topic-panel">
      <form className="brief-form" id="topicForm" onSubmit={submit}>
        <section className="brief-section brief-section--basics" aria-labelledby="brief-basics-title">
          <header className="brief-section-head">
            <span className="brief-section-number" aria-hidden="true">
              01
            </span>
            <div>
              <h3 id="brief-basics-title">원고 기본 설정</h3>
              <p>글의 핵심 소재와 목적을 먼저 정리해 주세요.</p>
            </div>
            {/* 샘플 채우기는 상단 툴바로 포털을 태워 보냈더니 소재 화면 안에서는 보이지
                않았다. 채워지는 입력 바로 위에 두어야 눈에 들어온다. */}
            {!fixedTopic && (
            <button
              className="button small brief-sample-button"
              type="button"
              id="fillSampleInput"
              onClick={fillSample}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M9 18h6M10 21h4M8.5 14.5a6 6 0 1 1 7 0c-.8.7-1.2 1.3-1.4 2h-4.2c-.2-.7-.6-1.3-1.4-2Z" />
              </svg>
              샘플 채우기
            </button>
            )}
          </header>

          <div className="brief-basics-grid">
            {/* 왼쪽 열은 '무엇에 대한 글인가'다 — 소재와, 곁들일 브랜드. 오른쪽 열의
                목적과 나란히 서야 하므로 둘을 한 칸에 담는다(그러지 않으면 2열 격자에서
                브랜드가 목적 자리를 밀어낸다). */}
            <div className="brief-basics-primary">
            {/* 소재가 이미 정해진 흐름에서는 칸 대신 무엇으로 쓰는지만 보여 준다.
                고칠 수 있게 두면 트렌드 키워드 선택과 어긋난다. */}
            {fixedTopic ? (
              <div className="field brief-topic-field">
                <div className="field-label-row">
                  <span className="brief-field-label">소재</span>
                </div>
                <p className="field-desc">고른 트렌드 키워드로 정해졌습니다.</p>
                <p className="brief-fixed-topic">{fixedTopic}</p>
              </div>
            ) : (
              <div className="field brief-topic-field">
                <div className="field-label-row">
                  <label htmlFor="topic">소재</label>
                  {/* **소재와 브랜드 중 하나는 있어야** 넘어간다. 둘 다 '선택'으로 적어
                      두었더니 아무것도 안 채워도 되는 줄 알았다는 지적이 있었다
                      (2026-08-20). 그렇다고 소재만 '필수'로 두면 이번엔 브랜드만 골라
                      쓰는 길이 안 보인다 — 그래서 두 칸에 같은 배지를 달고, 아래 한 줄이
                      무슨 뜻인지 말한다.

                      한쪽이 채워지면 다른 쪽은 정말 선택이므로 그때는 '선택'으로 바뀐다. */}
                  <span className={`field-badge ${brandPicked ? "opt" : "req"}`}>
                    {brandPicked ? "선택" : "둘 중 하나"}
                  </span>
                </div>
                <p className="field-desc">
                  {brandPicked
                    ? "독자가 검색해서 들어올 말입니다. 비워 두면 아래 브랜드가 글의 주인공이 됩니다."
                    : `독자가 검색해서 들어올 말입니다. 이름 하나여도 되고, 한 문장으로 적어도 됩니다(최대 ${MAX_TOPIC_CHARS}자).`}
                </p>
                {/* 길이 제한을 **적는 자리에서** 막는다. 없으면 다 적고 나서 저장할 때
                    서버에 거절당한다.

                    브랜드를 고르면 required를 뗀다: 소재를 비우는 것이 "브랜드를 주인공으로
                    쓰겠다"는 뜻이라, 그 조합을 브라우저 검사가 막으면 안 된다. */}
                <input
                  id="topic"
                  required={!brandPicked}
                  maxLength={MAX_TOPIC_CHARS}
                  placeholder="예: 빼빼로 신제품 / 스파이더맨 4편을 보고 느낀 점"
                  value={topic}
                  onChange={(event) => setTopic(event.target.value)}
                />
              </div>
            )}

            {/* 활용할 브랜드·서비스(2026-08-11 통합, 2026-08-19 잠금 해제). 소재를 브랜드가
                정하지 않는다 — 소재는 위 칸에서 사용자가 적고, 브랜드는 자기 자료를
                참고자료로 얹는다. 브랜드 흐름(fixedTopic)에서는 숨긴다. */}
            {!fixedTopic && (
              <BrandPicker
                brandId={brandId}
                /* 소재가 비어 있으면 이 칸이 그 자리를 대신하므로 '둘 중 하나'다.
                   소재를 적었으면 브랜드는 정말 곁들이는 것이라 '선택'이다. */
                badge={hasTopic ? "선택" : "둘 중 하나"}
                /* 앱스튜디오에서 들어왔으면 그 브랜드를 미리 골라 둔다. **새 글일 때만**
                   보낸다 — 저장된 글에도 보내면, 브랜드를 일부러 뺀 글을 다시 열었을 때
                   그 브랜드가 되붙는다. */
                campaign={task ? "" : currentCampaign()}
                onChange={(nextId, nextName) => {
                  setBrandId(nextId);
                  setBrandName(nextName);
                }}
              >
                {/* 브랜드를 고른 뒤에만 나온다. 무엇을 고를지 정하기 전에는 물을 것이 없다. */}
                {brandPicked && (
                  <BrandRoleChoice
                    brandName={brandName}
                    /* 소재가 없으면 고를 것이 없다 — 소재 자리를 브랜드가 채우므로
                       브랜드가 주인공일 수밖에 없다. 그것을 그대로 보여 준다. */
                    role={brandMode}
                    locked={!hasTopic}
                    topic={topic.trim()}
                    onChange={setBrandRole}
                  />
                )}

                {/* 배지의 '둘 중 하나'가 무슨 뜻인지 한 줄로 말한다. 두 칸 사이가 아니라
                    **아래**에 두는 이유: 두 칸을 다 본 뒤에 읽어야 짝 관계로 읽힌다. */}
                {!hasTopic && !brandPicked && (
                  <p className="field-desc brief-pair-note">
                    소재와 브랜드 중 <strong>하나는 있어야</strong> 다음 단계로 넘어갑니다.
                    소재를 적으면 그 소재가 글의 주인공이고, 비우고 브랜드만 고르면 그
                    브랜드를 소개하는 글이 됩니다.
                  </p>
                )}
              </BrandPicker>
            )}

            <div className="field brief-purpose-field">
              <div className="field-label-row">
                <span className="brief-field-label">글 목적</span>
                <span className="field-badge req">필수</span>
              </div>
              <p className="field-desc">
                이 글을 통해 독자에게 전달할 결과를 선택합니다. 글의 종류와 구성을 결정합니다.
              </p>
              <PurposeChoices
                purpose={purpose}
                customPurpose={customPurpose}
                onPurpose={(value) => {
                  setPurpose(value);
                  warmTrendKeywords(value);
                }}
                onCustomPurpose={(value) => {
                  setCustomPurpose(value);
                  warmTrendKeywords();
                }}
              />
            </div>
            </div>

            {/* 원고 작업 시작(2026-08-11 사용자 지시·시안). **비워 두면 지금 그대로다** —
                방향을 고르는 즉시 원고를 만드는 단일 글 작성이다. 시각을 넣으면 방향까지
                고른 뒤 예약으로 넘어가, 그 시각에 자료를 새로 모아 원고를 만든다.

                소재·브랜드와 같은 카드에 두는 이유: 이것도 '이 글을 어떻게 쓸까'의 설정이고,
                아래 참고 자료 뒤에 두면 다 적고 나서야 눈에 들어온다. */}
            <div className="brief-schedule" aria-labelledby="brief-schedule-title">
              {/* 제목보다 **위**다(2026-08-12 사용자 지시). 고르는 것이 아니라 지금
                  설정이 뜻하는 사실이라, 고르는 칸 사이에 끼면 눌러야 하는 것처럼 보인다.
                  이름과 값을 **한 줄에** 적는다 — 위아래로 쌓았더니 좁은 칸에서
                  "예상 소요 / 시간"으로 접혔다(사용자 지적).

                  나란히 있던 '자동 발행 ON'은 뺐다(같은 날 사용자 지적: "어차피 누를 수가
                  없는데 무슨 차이야"). 바로 아래 '원고가 끝나면 그대로 발행'을 그대로
                  되비추기만 했다 — 누를 수 없는 칸이 같은 말을 두 번 하고 있었다. */}
              <div className="brief-facts">
                <div className="brief-fact">
                  {clockIcon}
                  <span className="brief-fact-label">예상 소요 시간</span>
                  {/* 실측이다 — draftProgress의 단계별 소요를 그대로 더한다(2026-08-11
                      사용자 실측). 글자로 박아 두면 그 값을 고칠 때 여기만 옛말이 된다. */}
                  <strong>원고 1편에 약 {DRAFT_MINUTES}분</strong>
                </div>
              </div>

              <h4 id="brief-schedule-title" className="brief-schedule-title">
                언제, 몇 편, 어디에
              </h4>

              {/* 걸음 하나: **언제 만들까.** 예전에는 날짜 칸 하나뿐이라 "비우면 지금"이
                  글로만 적혀 있었다 — 비어 있는 칸이 선택이라는 것을 읽어야만 알았다.
                  두 단추로 나누면 지금 무엇을 고른 상태인지 눈으로 보인다. */}
              <div className="brief-block">
                <p className="brief-block-title">언제 만들까요?</p>
                <p className="brief-block-desc">비워 두면 지금 바로 만듭니다.</p>
                <div className="brief-mode" role="group" aria-label="언제 만들까요?">
                  <button
                    type="button"
                    className={`brief-mode-option${scheduleMode === "now" ? " is-on" : ""}`}
                    aria-pressed={scheduleMode === "now"}
                    onClick={() => {
                      setScheduleMode("now");
                      // 고른 시각을 지운다 — 남겨 두면 '지금 바로'인데 예약으로 저장된다.
                      setScheduledRunAt("");
                    }}
                  >
                    {boltIcon}
                    지금 바로
                  </button>
                  <button
                    type="button"
                    className={`brief-mode-option${scheduleMode === "later" ? " is-on" : ""}`}
                    aria-pressed={scheduleMode === "later"}
                    onClick={() => {
                      setScheduleMode("later");
                      refreshScheduleFloor();
                    }}
                  >
                    {calendarIcon}
                    예약 발행
                  </button>
                </div>
              </div>

              {/* 날짜 칸은 예약을 고른 뒤에만 나온다(2026-08-12 사용자 시안). 고르개 자체는
                  쓰던 것 그대로다 — 브라우저 기본 선택창은 분까지 골라도 밖을 눌러야
                  적용됐다(SchedulePicker에 적어 두었다). */}
              {scheduleMode === "later" && (
                <div className="brief-block">
                  <p className="brief-block-title">예약 발행 일시</p>
                  <div className="brief-schedule-field">
                    <SchedulePicker
                      label="원고를 만들기 시작할 시각"
                      value={scheduledRunAt}
                      min={minScheduleValue}
                      onChange={setScheduledRunAt}
                      onOpen={refreshScheduleFloor}
                    />
                  </div>
                </div>
              )}

              {/* 만들 원고 수(2026-08-12 사용자 결정, 최대 3편). */}
              <div className="brief-block">
                <p className="brief-block-title" id="brief-count-label">
                  만들 원고 수
                </p>
                <div className="brief-count-control" role="group" aria-labelledby="brief-count-label">
                  <button
                    type="button"
                    aria-label="원고 수 줄이기"
                    disabled={draftCount <= 1}
                    onClick={() => setDraftCount((current) => Math.max(1, current - 1))}
                  >
                    −
                  </button>
                  <strong aria-live="polite">{draftCount}편</strong>
                  <button
                    type="button"
                    aria-label="원고 수 늘리기"
                    disabled={draftCount >= MAX_DRAFT_COUNT}
                    onClick={() =>
                      setDraftCount((current) => Math.min(MAX_DRAFT_COUNT, current + 1))
                    }
                  >
                    +
                  </button>
                </div>
              </div>

              {/* 플랫폼마다 따로 고른다(2026-08-12 사용자 요청). 하나로 묶어 두면
                  "네이버에만 올리고 쓰레드에는 안 올린다"를 표현할 수 없었다.
                  표식은 다른 화면과 같은 것(.platform-mark)을 쓴다. */}
              <div className="brief-block">
                <p className="brief-block-title">원고가 끝나면 그대로 발행</p>
                <div className="brief-autopublish" role="group" aria-label="자동 발행">
                  <label className={`brief-autopublish-option${autoNaver ? " is-on" : ""}`}>
                    <input
                      type="checkbox"
                      checked={autoNaver}
                      onChange={(event) => setAutoNaver(event.target.checked)}
                    />
                    <span className="brief-autopublish-check" aria-hidden="true" />
                    {platformMark("naver")}
                    <span>네이버</span>
                  </label>
                  <label className={`brief-autopublish-option${autoThreads ? " is-on" : ""}`}>
                    <input
                      type="checkbox"
                      checked={autoThreads}
                      onChange={(event) => setAutoThreads(event.target.checked)}
                    />
                    <span className="brief-autopublish-check" aria-hidden="true" />
                    {platformMark("threads")}
                    <span>쓰레드</span>
                  </label>
                </div>
              </div>

              {/* 고른 값이 **무엇을 뜻하는지 그대로 되돌려 준다**(2026-08-12 사용자
                  지적: "처음 보는 사용자는 감을 못 잡을 것 같아"). 세 줄은 고른 것을
                  하나씩 되짚고, 마지막 한 문장은 그것이 합쳐지면 무엇이 되는지를 말한다 —
                  줄만 있으면 3편일 때 순서가, 문장만 있으면 무엇을 고쳤는지가 안 보인다.
                  '발행 요약'이라는 제목 줄은 뺐다(2026-08-12 사용자 지시) — 상자 안의
                  세 줄이 스스로 무엇인지 말하고 있어 제목은 한 겹 더한 이름표였다. */}
              <div className="brief-outcome-box">
                <ul className="brief-outcome-list">
                  <li>
                    {scheduleMode === "later" ? calendarIcon : boltIcon}
                    <span>
                      {scheduleMode === "later"
                        ? scheduledRunAt
                          ? `예약 발행: ${formatScheduleMoment(scheduledRunAt)}`
                          : "예약 발행: 시각을 골라 주세요"
                        : "지금 바로 시작"}
                    </span>
                  </li>
                  <li>
                    {docIcon}
                    <span>원고 {draftCount}편 생성</span>
                  </li>
                  <li>
                    {autoPublish ? (
                      platformMark(autoNaver ? "naver" : "threads")
                    ) : (
                      sendIcon
                    )}
                    <span>
                      {autoPublish
                        ? `${[autoNaver && "네이버", autoThreads && "쓰레드"]
                            .filter(Boolean)
                            .join("·")} 발행 예정`
                        : // 이 줄은 저장을 막는 상태다(2026-08-13). '안 한다'가 아니라
                          // **아직 안 골랐다**고 말해야 다음에 할 일이 보인다.
                          "올릴 곳을 골라 주세요"}
                    </span>
                  </li>
                </ul>
                <p className="brief-outcome">{scheduleSummary}</p>
              </div>

              {/* '진행 단계' 세 칸과 그 아래 파란 안내는 뺐다(2026-08-12 사용자 지시).
                  다음에 무슨 일이 일어나는지는 화면 위의 걸음 표시줄과 아래 안내 줄이
                  이미 말한다 — 같은 것을 세 번째로 적던 자리였다. */}
            </div>
          </div>
        </section>

        {/* 대상 연령과 카테고리는 **한 줄에 나란히** 둔다(2026-08-11 사용자 요청).
            둘 다 '이 글을 어떤 자리에 놓을 것인가'를 고르는 짧은 선택이라, 세로로 쌓으면
            화면만 길어지고 참고 자료가 접힌 아래로 밀린다. 좁은 화면에서는 자동으로
            위아래로 접힌다(.brief-section-row). */}
        <div className="brief-section-row">
        <section
          className="brief-section brief-section--audience"
          aria-labelledby="brief-audience-title"
        >
          <header className="brief-section-head">
            <span className="brief-section-number" aria-hidden="true">
              02
            </span>
            <div>
              <span className="brief-section-title-row">
                <h3 id="brief-audience-title">대상 연령</h3>
                <span className="field-badge req">필수</span>
              </span>
              {/* 설명 줄("고른 연령에 맞춰 설명 수준과 사례가 달라집니다…")은 뺐다
                  (2026-08-11 사용자 요청). 고르기 전에만 쓸모 있는 일반론이 자리를
                  차지하고, 정작 **고른 뒤에** 무엇이 달라지는지는 카드 아래 저 멀리
                  적혀 있었다. 그 안내를 이 자리로 올린다 — 같은 것을 말하는 두 문장이
                  머리와 발치에 나뉘어 있을 이유가 없다. */}
              <AudienceNote ageRange={ageRange} />
            </div>
          </header>

          <AudienceChoices
            ageRange={ageRange}
            onChange={(value) => {
              setAgeRange(value);
              warmTrendKeywords();
            }}
          />
        </section>

        {/* 소재 분야(2026-08-11). '오디세이'는 영화이고 게임이고 모니터다 — 어느
            분야인지 여기서 못 박지 않으면 모델이 스스로 고르고, 제목·자료·이미지가
            전부 그 판단 위에 얹혀 뒤에서 되돌릴 수 없다. */}
        <section
          className="brief-section brief-section--category"
          aria-labelledby="brief-category-title"
        >
          <header className="brief-section-head">
            <span className="brief-section-number" aria-hidden="true">
              03
            </span>
            <div>
              <span className="brief-section-title-row">
                <h3 id="brief-category-title">카테고리 선택</h3>
                <span className="field-badge req">필수</span>
              </span>
              {/* 두 문장을 한 줄에 잇지 않는다(2026-08-11 사용자 요청). 앞은 '무엇을
                  하라'이고 뒤는 '왜 필요한가'라, 이어 붙이면 지시가 이유에 묻힌다.
                  줄바꿈은 화면 폭이 아니라 문장 단위로 준다. */}
              <p>
                글 내용과 가장 잘 맞는 카테고리를 선택해 주세요.
                <span className="field-desc-line">
                  같은 이름의 다른 분야로 글이 새는 것을 막아줍니다.
                </span>
              </p>
            </div>
          </header>

          <div
            className="brief-category-grid"
            role="radiogroup"
            aria-label="소재 카테고리"
          >
            {SUBJECT_CATEGORIES.map((category) => (
              <button
                key={category}
                type="button"
                role="radio"
                aria-checked={subjectCategory === category}
                className={`brief-category-choice${
                  subjectCategory === category ? " selected" : ""
                }`}
                onClick={() => {
                  setSubjectCategory(category);
                  warmTrendKeywords();
                }}
              >
                {category}
              </button>
            ))}
          </div>
        </section>
        </div>

        <section
          className="brief-section brief-section--references"
          aria-labelledby="brief-references-title"
        >
          <header className="brief-section-head">
            <span className="brief-section-number" aria-hidden="true">
              04
            </span>
            <div>
              {/* '선택' 배지는 여기 하나만 둔다. 셋 다 선택이라 칸마다 붙이면 같은 말을
                  세 번 하는 셈이고, 그만큼 입력칸이 아래로 밀린다(2026-08-06 사용자 요청). */}
              <span className="brief-section-title-row">
                <h3 id="brief-references-title">참고 자료</h3>
                <span className="field-badge opt">선택</span>
              </span>
              <p>원고 작성에 참고할 메모, URL 또는 파일을 추가해 주세요.</p>
            </div>
          </header>

          <div className="brief-reference-grid">
            <div className="brief-reference-compose">
              <div className="brief-reference-tabs" role="tablist" aria-label="참고자료 종류">
                <button
                  type="button"
                  id="reference-tab-memo"
                  role="tab"
                  aria-selected={referenceTab === "memo"}
                  aria-controls="reference-panel-memo"
                  onClick={() => setReferenceTab("memo")}
                >
                  <ReferenceIcon type="TEXT" />
                  메모
                </button>
                <button
                  type="button"
                  id="reference-tab-url"
                  role="tab"
                  aria-selected={referenceTab === "url"}
                  aria-controls="reference-panel-url"
                  onClick={() => setReferenceTab("url")}
                >
                  <ReferenceIcon type="URL" />
                  URL
                </button>
                <button
                  type="button"
                  id="reference-tab-file"
                  role="tab"
                  aria-selected={referenceTab === "file"}
                  aria-controls="reference-panel-file"
                  onClick={() => setReferenceTab("file")}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                    <path d="M12 15V3m0 0L7.5 7.5M12 3l4.5 4.5M5 13.5V20h14v-6.5" />
                  </svg>
                  파일 업로드
                </button>
              </div>

              <div
                className="brief-reference-panel brief-reference-panel--memo"
                id="reference-panel-memo"
                role="tabpanel"
                aria-labelledby="reference-tab-memo"
                hidden={referenceTab !== "memo"}
              >
                <label className="sr-only" htmlFor="referenceText">
                  참고 메모
                </label>
                <div className="brief-reference-textarea">
                  <textarea
                    id="referenceText"
                    maxLength={MAX_REFERENCE_MEMO_CHARS}
                    placeholder={"예: 무료 체험 링크를 포함하고,\n지난달 업데이트 내용을 강조해 주세요."}
                    value={memoDraft}
                    onChange={(event) => setMemoDraft(event.target.value)}
                  />
                  <span aria-live="polite">
                    {memoDraft.length.toLocaleString()}/{MAX_REFERENCE_MEMO_CHARS.toLocaleString()}자
                  </span>
                </div>
                <div className="brief-reference-panel-actions">
                  <button
                    type="button"
                    className="button primary brief-reference-add"
                    disabled={!memoDraft.trim() || chosenReferenceCount >= MAX_REFERENCE_MATERIALS}
                    onClick={addMemo}
                  >
                    메모 추가
                  </button>
                </div>
              </div>

              <div
                className="brief-reference-panel brief-reference-panel--url"
                id="reference-panel-url"
                role="tabpanel"
                aria-labelledby="reference-tab-url"
                hidden={referenceTab !== "url"}
              >
                <label className="sr-only" htmlFor="referenceUrl">
                  참고 URL
                </label>
                <div className={`brief-url-input${urlProblem ? " has-error" : ""}`}>
                  <ReferenceIcon type="URL" />
                  <input
                    id="referenceUrl"
                    type="url"
                    aria-invalid={urlProblem ? true : undefined}
                    aria-describedby={urlProblem ? "referenceUrl-problem" : undefined}
                    placeholder="https://example.com/report"
                    value={urlDraft}
                    onChange={(event) => setUrlDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addUrl();
                      }
                    }}
                  />
                </div>
                {urlProblem && (
                  <p className="field-error" id="referenceUrl-problem" role="alert">
                    {urlProblem}
                  </p>
                )}
                <div className="brief-reference-panel-actions">
                  <button
                    type="button"
                    className="button primary brief-reference-add"
                    disabled={
                      !urlDraft.trim() ||
                      Boolean(urlProblem) ||
                      chosenReferenceCount >= MAX_REFERENCE_MATERIALS
                    }
                    onClick={addUrl}
                  >
                    URL 추가
                  </button>
                </div>
              </div>

              <div
                className="brief-reference-panel brief-reference-panel--file"
                id="reference-panel-file"
                role="tabpanel"
                aria-labelledby="reference-tab-file"
                hidden={referenceTab !== "file"}
              >
                {/* 브랜드 자료 편집과 **같은 칸**을 쓴다(2026-08-07 사용자 요청 — 두 화면을
                    통일). 끌어 놓기도 그대로 받는다. 파일 검사·개수 세기는 addFiles가
                    그대로 맡는다. */}
                <FileDropZone
                  id="referenceFiles"
                  accept=".txt,.png,.jpg,.jpeg,.pdf,text/plain,application/pdf,image/png,image/jpeg"
                  disabled={chosenReferenceCount >= MAX_REFERENCE_MATERIALS}
                  ariaLabel="참고 자료 파일"
                  title="파일을 드래그하거나 선택하여 업로드"
                  hint="TXT, PNG, JPG, JPEG, PDF 파일"
                  onFiles={(files) => addFiles(files)}
                />
              </div>
            </div>

            <div className="brief-reference-collection">
              <div className="brief-reference-collection-head">
                <h4>추가된 참고자료</h4>
                <span aria-live="polite">
                  {/* 개수 상한을 보여 주지 않는다(2026-08-11) — 제한은 용량뿐이다. */}
                  {chosenReferenceCount}개
                </span>
              </div>

              {referenceEntries.length ? (
                <ul className="brief-reference-list">
                  {referenceEntries.map((entry) => {
                    const view = viewOfReference(entry);
                    return (
                      <li key={entry.id}>
                        <span className={`brief-reference-kind is-${view.tone}`}>
                          <ReferenceIcon type={view.type} />
                          {view.badge}
                        </span>
                        <span className="brief-reference-detail" title={view.detail}>
                          {view.detail}
                          {view.bytes !== undefined && (
                            <small> · {formatFileBytes(view.bytes)}</small>
                          )}
                        </span>
                        <button
                          type="button"
                          className="brief-reference-remove"
                          aria-label={`${view.badge} 참고자료 삭제`}
                          onClick={() => removeReference(entry.id)}
                        >
                          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                            <path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5" />
                          </svg>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <div className="brief-reference-empty">
                  <span>
                    <ReferenceIcon type="TEXT" />
                  </span>
                  <p>아직 추가된 참고자료가 없습니다.</p>
                  <small>왼쪽에서 메모, URL 또는 파일을 추가해 주세요.</small>
                </div>
              )}

              <div className="brief-reference-limits">
                {/* 숫자를 글자로 박아 두면 상한을 올릴 때 안내만 옛말이 된다(2026-08-11). */}
                <span>
                  파일당 {MAX_REFERENCE_FILE_BYTES / 1024 / 1024}MB · 전체 파일{" "}
                  {MAX_REFERENCE_FILES_BYTES / 1024 / 1024}MB
                </span>
                <span>
                  지원 형식: TXT, PNG, JPG, JPEG, PDF
                  {chosenFileBytes > 0 && ` · 현재 ${formatFileBytes(chosenFileBytes)}`}
                </span>
              </div>
            </div>
          </div>

          <footer className="brief-action-bar">
            <p>
              {hasPendingReferenceDraft
                ? "작성 중인 메모나 URL은 '추가' 버튼을 눌러 목록에 담아주세요."
                : scheduledRunAt
                  ? "방향까지 고르면 예약으로 넘어갑니다. 원고는 지정한 시각에 만듭니다."
                  : "입력 내용은 다음 단계에서도 다시 수정할 수 있어요."}
            </p>
            <button
              className="button primary"
              type="submit"
              disabled={busy || hasPendingReferenceDraft}
            >
              {busy ? (
                <>
                  <span className="spinner" aria-hidden="true" /> 처리 중
                </>
              ) : (
                <>
                  {submitLabel ?? (editing ? "수정하고 다음" : "소재 저장하고 다음")}
                  <span aria-hidden="true">→</span>
                </>
              )}
            </button>
          </footer>
        </section>
      </form>
    </section>
  );
}
