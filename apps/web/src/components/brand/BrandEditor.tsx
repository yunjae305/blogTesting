import { useEffect, useState, type ReactNode } from "react";

import { request } from "../../api/client";
import { DEFAULT_BRAND_ID } from "../../constants";
import { FileDropZone } from "../FileDropZone";
import { useStore } from "../../store";
import { referenceUrlProblem } from "../../utils";
// 삭제 아이콘은 새로 그리지 않는다. 예약 화면의 작업 삭제와 **같은 휴지통**을 쓴다 —
// 같은 뜻의 버튼이 화면마다 다른 그림이면 사용자가 매번 다시 읽어야 한다.
import { TrashIcon } from "../scheduled/icons";
import { AudiencePicker } from "./AudiencePicker";
import type {
  BrandAudience,
  BrandClosing,
  BrandDocument,
  BrandImage,
  BrandLink,
  BrandProfile,
  BrandUseCase,
} from "./types";

/* 화면 아이콘. 외부 아이콘 묶음을 들이지 않고 필요한 것만 인라인 SVG로 둔다 —
   글자 색을 그대로 따르고(currentColor) 번들이 늘지 않는다. */

const IconPencil = () => (
  <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
  </svg>
);

const IconDoc = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
    <path d="M14 2v6h6M8 13h8M8 17h5" />
  </svg>
);

const IconUsers = () => (
  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
  </svg>
);

/* 자료 등록 탭과 '추가된 참고자료' 목록 배지의 아이콘. 원고 작성 화면(StepTopic)의
   참고자료 칸과 **같은 그림**이어야 해서 같은 path를 쓴다 — 크기·선 굵기는
   `.brief-reference-tabs svg` / `.brief-reference-kind svg`가 준다. */

const IconDocMark = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M6 3h8l4 4v14H6z" />
    <path d="M14 3v5h5M9 12h6M9 16h6" />
  </svg>
);

const IconUrlMark = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M9.5 14.5 14.5 9M7.2 16.8l-1 1a3.5 3.5 0 0 1-5-5l3.2-3.2a3.5 3.5 0 0 1 5 0M16.8 7.2l1-1a3.5 3.5 0 0 1 5 5l-3.2 3.2a3.5 3.5 0 0 1-5 0" />
  </svg>
);

const IconImageMark = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <circle cx="8.5" cy="9" r="1.5" />
    <path d="m5 17 4.2-4.2 3.1 3.1 2.2-2.2L19 18" />
  </svg>
);

const IconUploadMark = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M12 15V3m0 0L7.5 7.5M12 3l4.5 4.5M5 13.5V20h14v-6.5" />
  </svg>
);

const IconRemove = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5" />
  </svg>
);

const IconSave = () => (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z" />
    <path d="M17 21v-8H7v8M7 3v5h8" />
  </svg>
);

/** 구분선 + 아이콘 + 제목 + 옆에 붙는 설명 한 줄. 아래 묶음의 머리글이다. */
function BrandSection({
  icon,
  title,
  hint,
  children,
}: {
  icon: ReactNode;
  title: string;
  hint: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="brand-section">
      <h3 className="brand-section-title">
        <span className="brand-section-icon" aria-hidden="true">
          {icon}
        </span>
        {title}
        <span className="brand-section-hint">{hint}</span>
      </h3>
      {children}
    </section>
  );
}

/**
 * 글 **맨 마지막에 언제나 붙는** 마무리(2026-08-19).
 *
 * 본문은 광고가 아니어야 하지만, 글 끝에는 "여기서 보면 된다"는 자리가 하나 있어야
 * 한다. 그 둘은 충돌하지 않는다 — 본문에서 권유하지 않기 때문에 마지막 한 줄이 오히려
 * 신뢰를 얻는다.
 *
 * 여기 적는 것은 **사실**이다. 이 글자는 검수를 거치지 않고 그대로 발행되므로, 가입
 * 조건·크레딧 수가 바뀌면 여기서 고쳐야 한다.
 */
function ClosingFields({
  value,
  onChange,
}: {
  value: BrandClosing;
  onChange: (next: BrandClosing) => void;
}) {
  const set = <K extends keyof BrandClosing>(key: K, next: BrandClosing[K]) =>
    onChange({ ...value, [key]: next });

  return (
    <div className="brand-field brand-closing-field">
      <span className="brand-field-label">글 마지막에 붙일 안내</span>
      <span className="brand-field-hint">
        모든 글의 <strong>맨 끝</strong>에 그대로 붙습니다. 본문은 광고가 되지 않게 쓰이고,
        권유는 이 한 줄에서만 합니다. 안내 문구와 주소를 <strong>둘 다</strong> 채워야
        저장됩니다 — 비워 두면 아무것도 붙지 않습니다.
      </span>

      <input
        aria-label="마지막 안내 문구"
        placeholder="안내 문구 — 예: 가입은 무료, 웰컴 크레딧 100 지급, 카드 등록 없음."
        value={value.note}
        onChange={(event) => set("note", event.target.value)}
      />
      <div className="brand-closing-row">
        <input
          aria-label="링크에 보일 글자"
          placeholder="링크 글자 — 예: aiona.kr"
          value={value.label}
          onChange={(event) => set("label", event.target.value)}
        />
        <input
          aria-label="링크 주소"
          placeholder="주소 — https://aiona.kr"
          value={value.url}
          onChange={(event) => set("url", event.target.value)}
        />
      </div>
      <input
        aria-label="함께 붙일 이미지 이름"
        placeholder="함께 붙일 이미지 이름(선택) — 아래에 올린 이미지의 설명과 같게"
        value={value.imageLabel ?? ""}
        onChange={(event) => set("imageLabel", event.target.value)}
      />
    </div>
  );
}

/** 기준표 줄 수 상한. 서버 검증(BrandLimits.MAX_USE_CASES)과 같은 값이어야 한다. */
const MAX_USE_CASES = 30;

/**
 * "이런 상황이면 이 기능" 기준표(2026-08-19).
 *
 * 왜 서술 칸(핵심 기능·서비스)과 따로 두는가. 소재가 '빼빼로'인 글에서 모델이 알아야
 * 하는 것은 "이 브랜드가 무엇을 하는가"가 아니라 **"이 상황에서 쓸 기능이 무엇인가"**다.
 * 서술 칸에는 기능이 줄글로 섞여 있어서, 모델이 매번 그중 하나를 골라 붙이거나(자주 같은
 * 것만 고른다) 없는 기능명을 지어낸다.
 *
 * 이 표가 두 곳에서 쓰인다: 원고 프롬프트가 **실제로 있는 기능명**을 쓰게 하고, 소재와
 * 브랜드의 결합 가능성(A·B·C)을 재는 자다.
 */
function UseCaseTable({
  rows,
  onChange,
}: {
  rows: BrandUseCase[];
  onChange: (next: BrandUseCase[]) => void;
}) {
  const update = (index: number, patch: Partial<BrandUseCase>) =>
    onChange(rows.map((row, at) => (at === index ? { ...row, ...patch } : row)));

  return (
    <div className="brand-field brand-usecase-field">
      <span className="brand-field-label">이런 상황이면 이 기능</span>
      <span className="brand-field-hint">
        트렌드 소재에 이 브랜드를 <strong>활용 도구로</strong> 얹을 때, 어떤 상황에서 어떤
        기능을 쓸지 정해 둡니다. 원고는 여기 적힌 기능 이름을 그대로 씁니다 — 비워 두면
        모델이 줄글에서 기능을 짐작합니다.
      </span>

      {rows.length > 0 && (
        <ul className="brand-usecase-list">
          {rows.map((row, index) => (
            // 줄의 신원은 자리다. 값으로 key를 만들면 글자를 칠 때마다 칸이 다시 그려져
            // 커서가 튄다.
            <li key={index} className="brand-usecase-row">
              <input
                aria-label={`${index + 1}번째 상황`}
                placeholder="상황 — 예: 어떤 정보를 알아보고 싶을 때"
                value={row.situation}
                onChange={(event) => update(index, { situation: event.target.value })}
              />
              <input
                aria-label={`${index + 1}번째 기능`}
                placeholder="기능 이름 — 예: 자료 조사"
                value={row.feature}
                onChange={(event) => update(index, { feature: event.target.value })}
              />
              <input
                aria-label={`${index + 1}번째 닿는 소재`}
                placeholder="닿는 소재(쉼표로) — 예: 다이어트, 칼로리, 성분"
                value={row.keywords.join(", ")}
                onChange={(event) =>
                  update(index, {
                    keywords: event.target.value
                      .split(",")
                      .map((word) => word.trim())
                      .filter(Boolean),
                  })
                }
              />
              <button
                type="button"
                className="icon-button"
                aria-label={`${index + 1}번째 줄 삭제`}
                onClick={() => onChange(rows.filter((_row, at) => at !== index))}
              >
                <TrashIcon />
              </button>
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        className="button small"
        disabled={rows.length >= MAX_USE_CASES}
        onClick={() => onChange([...rows, { situation: "", feature: "", keywords: [] }])}
      >
        + 줄 추가
      </button>
    </div>
  );
}

/** 이미지 한 장의 상한. 서버 검증(BrandLimits.MAX_IMAGE_BYTES)과 같은 값이어야 한다. */
const MAX_IMAGE_BYTES = 1024 * 1024;
/** 이미지 장수 상한. 열 장이 한 요청에 통째로 실려 가므로 서버가 세기 전에 여기서 막는다. */
const MAX_IMAGES = 10;
/** 문서 개수와 크기. 서버 검증(BrandLimits)과 같은 값이어야 한다. */
const MAX_DOCUMENTS = 5;
const MAX_PDF_BYTES = 4 * 1024 * 1024;
const MAX_TEXT_LENGTH = 20000;
/** 이미지와 PDF를 **합쳐** 이만큼까지. 낱개만 재면 합이 요청 상한을 넘는다. */
const MAX_ATTACHMENT_TOTAL_BYTES = 10 * 1024 * 1024;
/** 메모 한 건의 글자 수. 원고 작성 화면의 참고 메모와 같은 값이다(화면이 같아 보이므로
    받아 주는 양도 같아야 한다). 문서 상한(MAX_TEXT_LENGTH)보다 훨씬 작아 겹치지 않는다. */
const MAX_MEMO_CHARS = 1_000;
/** 자료 등록 칸의 탭. */
type ReferenceTab = "memo" | "url" | "file";
/** 메모로 넣은 문서를 알아보는 이름 규칙(addMemo가 붙인다). 목록에서 배지를 가른다. */
const MEMO_NAME_PATTERN = /^메모 \d+$/;

/** data URL이 담고 있는 실제 바이트 수(base64는 4글자가 3바이트다). */
function decodedBytes(dataUrl: string): number {
  return Math.floor((dataUrl.length * 3) / 4);
}

/** 목록에 적는 크기. 원고 작성 화면과 같은 눈금을 쓴다. */
function formatFileBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    const value = bytes / 1024 / 1024;
    return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)}MB`;
  }
  return `${Math.max(1, Math.ceil(bytes / 1024))}KB`;
}

/** '추가된 참고자료' 목록의 한 줄. 문서·주소·이미지를 한 모양으로 그리기 위한 것이다. */
type CollectedItem = {
  key: string;
  badge: string;
  /** 배지 색(`.brief-reference-kind.is-*`). 원고 작성 화면과 같은 어휘다. */
  tone: "memo" | "url" | "pdf" | "image" | "text-file";
  icon: ReactNode;
  detail: string;
  bytes?: number;
  /** 이미지 미리보기(있으면 이름 앞에 붙는다). */
  thumbnail?: string;
  /** 줄 안에 더 들어가는 입력(이미지의 '사진 설명' 칸). */
  extra?: ReactNode;
  remove: () => void;
};

type Props = {
  /** 고칠 자료. 새로 만들 때는 null. */
  brand: BrandProfile | null;
  /**
   * 첨부(이미지·문서). **null이면 아직 서버에서 오는 중**이다.
   *
   * 전체 브랜드 문서는 2MB라 Atlas에서 20초 넘게 걸린다(2026-08-07 실측). 그동안
   * 화면을 막지 않으려고 텍스트 필드(brand)와 첨부를 나눠 받는다 — 첨부가 오기 전에는
   * 그 칸에 안내를 보여주고, 저장을 잠근다(빈 첨부로 저장하면 자료가 통째로 지워진다).
   */
  attachments: { images: BrandImage[]; documents: BrandDocument[] } | null;
  /** 첨부를 기다리는 동안 보여줄 개수(목록 요약에서 안다). 모르면 생략. */
  attachmentCounts?: { images: number; documents: number };
  onSaved: (brand: BrandProfile) => void;
  onCancel: () => void;
  /**
   * 이 자료를 지웠다. 주지 않으면 삭제 버튼이 나오지 않는다 — 지운 뒤 목록을 고치고
   * 고른 값을 풀어 줄 곳이 없으면, 화면에는 없는 브랜드가 골라진 채로 남는다.
   */
  onDeleted?: (brandId: string) => void;
};

type Draft = {
  name: string;
  description: string;
  features: string;
  useCases: BrandUseCase[];
  closing: BrandClosing;
  audiences: BrandAudience[];
  links: BrandLink[];
  documents: BrandDocument[];
  images: BrandImage[];
};

function toDraft(brand: BrandProfile | null): Draft {
  return {
    name: brand?.name ?? "",
    description: brand?.description ?? "",
    features: brand?.features ?? "",
    useCases: brand?.useCases ?? [],
    // 빈 칸으로 시작한다 — 서버는 안내 문구와 주소가 **둘 다** 있을 때만 저장한다.
    closing: brand?.closing ?? { note: "", label: "", url: "", imageLabel: "" },
    audiences: brand?.audiences ?? [],
    links: brand?.links ?? [],
    documents: brand?.documents ?? [],
    images: brand?.images ?? [],
  };
}

/**
 * 브랜드 자료 편집.
 *
 * 설정 화면에 넣지 않았다. 설정은 "모든 글에 적용되는 기본값"이고 브랜드 자료는 "이
 * 브랜드로 쓰는 글의 재료"라 성격이 다르고, 자료를 고치는 이유가 결국 그 브랜드로 글을
 * 쓰기 위해서라 같은 화면에 있는 편이 오가기 쉽다.
 */
export function BrandEditor({
  brand,
  attachments,
  attachmentCounts,
  onSaved,
  onCancel,
  onDeleted,
}: Props) {
  const { showToast, reportError } = useStore();
  const [draft, setDraft] = useState<Draft>(() => toDraft(brand));
  const [busy, setBusy] = useState(false);
  const [referenceTab, setReferenceTab] = useState<ReferenceTab>("memo");
  const [memoDraft, setMemoDraft] = useState("");
  const [urlDraft, setUrlDraft] = useState("");

  // 첨부가 뒤늦게 도착하면 그 두 칸만 채운다. 텍스트 필드는 건드리지 않는다 —
  // 기다리는 동안 사용자가 이미 고치고 있을 수 있다.
  const attachmentsPending = attachments === null;
  useEffect(() => {
    if (attachments === null) return;
    setDraft((prev) => ({
      ...prev,
      images: attachments.images,
      documents: attachments.documents,
    }));
  }, [attachments]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }));

  /**
   * 지울 수 있는 자료인가. 이미 저장된 자료이고, 부모가 뒤처리(목록 갱신·선택 해제)를
   * 할 수 있으면 된다.
   *
   * 2026-08-20까지는 기본 브랜드(AIONA)를 뺐다. 지워도 다음 조회에서 되살아났기
   * 때문인데, 그건 서버를 고칠 일이지 버튼을 숨길 일이 아니었다 — 이제 기본 브랜드도
   * **지우면 지워진 채로 남는다**(BrandService.delete_brand).
   */
  const deletable = Boolean(brand && onDeleted);

  /** 첨부 칸에 보여줄 대기 안내 한 줄. 개수를 알면 함께 말한다. */
  const pendingNote = attachmentCounts
    ? `저장된 첨부 자료를 불러오는 중이에요… (이미지 ${attachmentCounts.images}장 · 문서 ${attachmentCounts.documents}개)`
    : "저장된 첨부 자료를 불러오는 중이에요…";

  /** 적는 동안 본다. 다 적고 누른 뒤에 알려 주면 어디가 틀렸는지 찾아야 한다. */
  const urlProblem = referenceUrlProblem(urlDraft);

  async function addImages(files: File[]) {
    const loaded: BrandImage[] = [];
    for (const file of files) {
      if (draft.images.length + loaded.length >= MAX_IMAGES) {
        showToast(`이미지는 최대 ${MAX_IMAGES}장까지 넣을 수 있습니다.`, true);
        break;
      }
      if (file.size > MAX_IMAGE_BYTES) {
        showToast(`${file.name}은(는) 1MB를 넘어 넣지 못했습니다.`, true);
        continue;
      }
      // 서버는 data URL만 받는다. 바깥 주소는 발행 뒤 깨진다(원고 이미지와 같은 이유).
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      loaded.push({ label: file.name, dataUrl });
    }
    if (loaded.length) set("images", [...draft.images, ...loaded]);
  }

  /** 이미지와 PDF의 합. 낱개만 재면 합이 요청 상한을 넘어 저장 버튼에서 통째로 실패한다. */
  function attachmentBytes(images: BrandImage[], documents: BrandDocument[]): number {
    return (
      images.reduce((sum, image) => sum + decodedBytes(image.dataUrl), 0) +
      documents.reduce((sum, doc) => sum + (doc.kind === "PDF" ? decodedBytes(doc.value) : 0), 0)
    );
  }

  async function addDocuments(section: BrandDocument["section"], files: File[]) {
    const loaded: BrandDocument[] = [];
    for (const file of files) {
      if (draft.documents.length + loaded.length >= MAX_DOCUMENTS) {
        showToast(`문서는 최대 ${MAX_DOCUMENTS}개까지 넣을 수 있습니다.`, true);
        break;
      }
      const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
      if (isPdf) {
        if (file.size > MAX_PDF_BYTES) {
          showToast(`${file.name}은(는) 4MB를 넘어 넣지 못했습니다.`, true);
          continue;
        }
        // PDF는 data URL로 보낸다. 텍스트는 프롬프트를 만들 때 서버가 뽑는다.
        const dataUrl = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result));
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(file);
        });
        loaded.push({ section, name: file.name, kind: "PDF", value: dataUrl });
        continue;
      }
      // 텍스트는 글자 그대로 보낸다 — 프롬프트에 바로 실린다.
      const text = await file.text();
      if (text.length > MAX_TEXT_LENGTH) {
        showToast(
          `${file.name}이(가) ${MAX_TEXT_LENGTH}자를 넘습니다. 필요한 부분만 남겨 주세요.`,
          true,
        );
        continue;
      }
      loaded.push({ section, name: file.name, kind: "TEXT", value: text });
    }
    if (!loaded.length) return;

    const next = [...draft.documents, ...loaded];
    if (attachmentBytes(draft.images, next) > MAX_ATTACHMENT_TOTAL_BYTES) {
      showToast(
        `이미지와 PDF를 합쳐 ${MAX_ATTACHMENT_TOTAL_BYTES / (1024 * 1024)}MB까지 넣을 수 있습니다.`,
        true,
      );
      return;
    }
    set("documents", next);
  }

  /** 한 칸으로 받은 파일을 형식으로 가른다 — 이미지는 브랜드 이미지로, 나머지는 자료
      문서로 간다(2026-08-10 사용자 요청, 올리는 칸 통합). 검사·상한은 각자의 기존
      함수가 그대로 맡는다. */
  async function addAttachments(files: File[]) {
    const isImage = (file: File) =>
      file.type.startsWith("image/") || /\.(png|jpe?g|webp|gif|bmp|avif)$/i.test(file.name);
    const images = files.filter(isImage);
    const documents = files.filter((file) => !isImage(file));
    if (images.length) await addImages(images);
    if (documents.length) await addDocuments("description", documents);
  }

  /** 메모는 **자료 문서**로 넣는다(kind "TEXT"). 저장 모델에 메모 칸을 새로 파지 않았다 —
      서버가 이미 텍스트 문서를 프롬프트에 그대로 싣고 있어 새 필드가 필요 없다. */
  function addMemo() {
    const value = memoDraft.trim();
    if (!value) return;
    if (draft.documents.length >= MAX_DOCUMENTS) {
      showToast(`자료는 최대 ${MAX_DOCUMENTS}개까지 넣을 수 있습니다.`, true);
      return;
    }
    // 이름이 겹치면 목록의 key와 '빼기'가 두 건을 같은 것으로 본다 — 번호를 이어 붙인다.
    const used = draft.documents
      .map((doc) => /^메모 (\d+)$/.exec(doc.name))
      .map((match) => (match ? Number(match[1]) : 0));
    const name = `메모 ${Math.max(0, ...used) + 1}`;
    set("documents", [...draft.documents, { section: "description", name, kind: "TEXT", value }]);
    setMemoDraft("");
  }

  /** URL은 예전 '관련 주소'와 **같은 곳**(links)에 들어간다. 표만 없어졌을 뿐 저장되는
      자리는 그대로다 — 이름 칸은 없앴으므로 비워 둔다(서버도 빈 이름을 받는다). */
  function addUrl() {
    const value = urlDraft.trim();
    if (!value) return;
    if (urlProblem) {
      showToast(`주소를 확인해 주세요 — ${urlProblem}`, true);
      return;
    }
    if (draft.links.some((link) => link.url.trim() === value)) {
      showToast("이미 추가한 주소입니다.", true);
      return;
    }
    set("links", [...draft.links, { label: "", url: value }]);
    setUrlDraft("");
  }

  /**
   * 이 자료를 지운다. **되돌릴 수 없다** — 올려 둔 이미지·문서까지 함께 사라진다.
   *
   * 기본 브랜드는 지울 수 없어서 버튼 자체가 나오지 않는다(아래 `deletable`). 서버도
   * 같은 것을 거부하지만, 눌러 봐야 오류만 나는 버튼을 보여 줄 이유가 없다.
   *
   * 이 글에 그 브랜드가 골라져 있었다면 부모가 선택을 푼다(`onDeleted`). 여기서 하지
   * 않는 이유는 고른 값을 들고 있는 곳이 부모이기 때문이다.
   */
  async function remove() {
    if (!brand || !onDeleted) return;
    const attached =
      brand.images.length || brand.documents.length
        ? ` 올려 둔 이미지 ${brand.images.length}장과 문서 ${brand.documents.length}개도 함께 지워집니다.`
        : "";
    // 기본 브랜드는 되살리는 길이 다르다(스크립트). 지우기 전에 그것을 알려 준다 —
    // 다시 만들 수 있다고 생각하고 지웠다가 못 되돌리면 안 된다.
    const restorable =
      brand.brandId === DEFAULT_BRAND_ID
        ? " 기본 제공 자료라 화면에서는 다시 만들 수 없습니다(관리자 스크립트로만 복구)."
        : "";
    if (
      !window.confirm(
        `'${brand.name}' 자료를 삭제할까요? 되돌릴 수 없습니다.${attached}${restorable}`,
      )
    ) {
      return;
    }
    setBusy(true);
    try {
      await request(`/brands/${encodeURIComponent(brand.brandId)}`, { method: "DELETE" });
      showToast(`${brand.name} 자료를 삭제했습니다.`);
      onDeleted(brand.brandId);
    } catch (error) {
      reportError(error);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    try {
      const body = {
        name: draft.name.trim(),
        description: draft.description.trim() || null,
        features: draft.features.trim() || null,
        // 상황·기능이 둘 다 있는 줄만 보낸다. 한 칸만 채운 줄은 화면에서 쉽게 생기는데,
        // 그대로 저장하면 프롬프트에 반쪽짜리 지시가 실린다(서버도 같은 것을 버린다).
        useCases: draft.useCases.filter(
          (item) => item.situation.trim() && item.feature.trim(),
        ),
        // 안내 문구와 주소가 둘 다 있을 때만 보낸다. 하나만 채운 것은 안 쓰겠다는 뜻이다.
        closing:
          draft.closing.note.trim() && draft.closing.url.trim()
            ? {
                note: draft.closing.note.trim(),
                label: draft.closing.label.trim(),
                url: draft.closing.url.trim(),
                imageLabel: draft.closing.imageLabel?.trim() || null,
              }
            : null,
        audiences: draft.audiences,
        links: draft.links.filter((link) => link.url.trim()),
        documents: draft.documents,
        images: draft.images,
      };
      const saved = brand
        ? await request<BrandProfile>(`/brands/${brand.brandId}`, { method: "PUT", body })
        : await request<BrandProfile>("/brands", { method: "POST", body });
      showToast(`${saved.name} 자료를 저장했습니다.`);
      onSaved(saved);
    } catch (error) {
      reportError(error);
    } finally {
      setBusy(false);
    }
  }

  /** 넣어 둔 자료를 **한 목록으로** 편다 — 문서(메모·텍스트·PDF) → 주소 → 이미지 순서.
      올리는 칸이 하나뿐이라 구획을 나누지 않는다. 예전에 다른 구획으로 올려 둔 파일도
      여기서 뺄 수 있어야 한다. */
  function collectedItems(): CollectedItem[] {
    const items: CollectedItem[] = [];

    for (const doc of draft.documents) {
      const isMemo = doc.kind === "TEXT" && MEMO_NAME_PATTERN.test(doc.name);
      items.push({
        key: `doc-${doc.section}-${doc.name}`,
        badge: isMemo ? "메모" : doc.kind === "PDF" ? "PDF" : "텍스트",
        tone: isMemo ? "memo" : doc.kind === "PDF" ? "pdf" : "text-file",
        icon: <IconDocMark />,
        // 메모는 파일이 아니라 적은 글이다 — 이름('메모 1')보다 내용이 알아보기 쉽다.
        detail: isMemo ? doc.value : doc.name,
        bytes: doc.kind === "PDF" ? decodedBytes(doc.value) : undefined,
        remove: () =>
          set(
            "documents",
            draft.documents.filter(
              (item) => !(item.section === doc.section && item.name === doc.name),
            ),
          ),
      });
    }

    // 주소는 예전 '관련 주소' 표를 대신한다 — 표는 없앴지만(2026-08-10 사용자 요청)
    // 넣은 주소를 보지도 빼지도 못하면 안 된다. 이름을 달아 둔 옛 주소는 함께 보여준다.
    draft.links.forEach((link, index) => {
      items.push({
        key: `link-${index}`,
        badge: "URL",
        tone: "url",
        icon: <IconUrlMark />,
        detail: link.label ? `${link.label} · ${link.url}` : link.url,
        remove: () => set("links", draft.links.filter((_, i) => i !== index)),
      });
    });

    draft.images.forEach((image, index) => {
      items.push({
        key: `image-${index}`,
        badge: "이미지",
        tone: "image",
        icon: <IconImageMark />,
        detail: image.label,
        bytes: decodedBytes(image.dataUrl),
        thumbnail: image.dataUrl,
        // 사진 설명은 이미지마다 따로 적는다 — 줄 안에 그대로 둔다(예전 이미지 목록의
        // 입력칸을 그 자리 그대로 옮겨 왔다).
        extra: (
          <input
            className="brand-reference-caption"
            aria-label={`이미지 ${index + 1} 설명`}
            placeholder="사진 설명"
            value={image.caption ?? ""}
            onChange={(event) =>
              set(
                "images",
                draft.images.map((item, i) =>
                  i === index ? { ...item, caption: event.target.value } : item,
                ),
              )
            }
          />
        ),
        remove: () => set("images", draft.images.filter((_, i) => i !== index)),
      });
    });

    return items;
  }

  const collected = collectedItems();
  // 목록 아래에 적는 '현재' 합계. 저장에서 막는 것과 같은 값이라야 한다.
  const attachedBytes = attachmentBytes(draft.images, draft.documents);

  return (
    <div className="brand-editor-page">
      {/* 화면 머리글. 브랜드 경로에는 상단 제목줄이 없어(작성 화면만 쓴다) 여기서 낸다. */}
      <header className="brand-editor-hero">
        <span className="brand-editor-hero-icon" aria-hidden="true">
          <IconPencil />
        </span>
        <div>
          <h1>브랜드 글 작성하기</h1>
          <p>브랜드 정보를 정리해 글 작성에 활용할 자료를 등록하세요.</p>
        </div>
      </header>

      <section className="panel brand-editor" aria-labelledby="brand-editor-title">
        {/* 머리글은 한 줄이다: 왼쪽에 아이콘+제목, 오른쪽 끝에 취소·저장.
            panel-header가 space-between이라 아이콘과 제목을 묶지 않으면 둘이 카드
            양 끝으로 떨어진다 — 실제로 그렇게 벌어져 있었다(2026-08-07 사용자 지적). */}
        <div className="panel-header brand-editor-head">
          <div className="brand-editor-head-title">
            <span className="brand-editor-head-icon" aria-hidden="true">
              <IconDoc />
            </span>
            <h2 className="panel-title" id="brand-editor-title">
              {brand ? `${brand.name} 자료` : "새 브랜드 자료"}
            </h2>
          </div>
          <div className="brand-editor-actions">
            {/* 첨부가 오기 전에 저장하면 빈 첨부가 그대로 저장돼 이미지·문서가 통째로
                지워진다 — 저장은 첨부가 도착한 뒤에만 연다. 텍스트는 그동안 고칠 수 있다. */}
            {attachmentsPending && (
              <span className="brand-field-hint" role="status">
                첨부 자료를 불러온 뒤 저장할 수 있어요.
              </span>
            )}
            {/* 취소·저장과 같은 묶음 오른쪽에 선다(2026-08-20 사용자 지시). 잘못 누르는
                것은 확인 창이 막고, 여백을 조금 띄워 취소에 바로 붙지는 않게 한다.

                새로 만드는 중에는 지울 것이 없어 나오지 않는다. */}
            {deletable && (
              <button
                className="button danger brand-delete-button"
                type="button"
                onClick={() => void remove()}
                disabled={busy}
              >
                <TrashIcon />
                자료 삭제
              </button>
            )}
            <button className="button" type="button" onClick={onCancel} disabled={busy}>
              취소
            </button>
            <button
              className="button primary brand-save-button"
              id="saveBrand"
              type="button"
              onClick={() => void save()}
              disabled={busy || attachmentsPending}
            >
              <IconSave />
              {busy ? "저장 중" : "저장"}
            </button>
          </div>
        </div>
        <div className="panel-body brand-editor-body">
          {/* 두 칸으로 나눈 윗단. 왼쪽은 사람이 적는 글(이름·소개·핵심 기능), 오른쪽은
              자료를 넣는 칸이다(2026-08-10 사용자 요청 — 적는 것과 넣는 것을 갈랐다). */}
          <div className="brand-editor-grid">
            <div className="brand-editor-column">
              <label className="brand-field" htmlFor="brandName">
                <span className="brand-field-label">
                  브랜드 이름 <span className="brand-required">*</span>
                </span>
                <input
                  id="brandName"
                  value={draft.name}
                  onChange={(event) => set("name", event.target.value)}
                  placeholder="AIONA"
                />
              </label>

              <div className="brand-field">
                <label className="brand-field-label" htmlFor="brand-description">
                  브랜드 소개
                </label>
                <span className="brand-field-hint">무엇을 하는 곳인지 줄글로 적습니다.</span>
                <textarea
                  id="brand-description"
                  className="brand-description-input"
                  rows={8}
                  value={draft.description}
                  placeholder="AIONA는 AI 교육과 도입 컨설팅을 하는 회사입니다. ..."
                  onChange={(event) => set("description", event.target.value)}
                />
              </div>

              <div className="brand-field">
                <label className="brand-field-label" htmlFor="brand-features">
                  핵심 기능·서비스
                </label>
                <textarea
                  id="brand-features"
                  rows={3}
                  value={draft.features}
                  placeholder="실무 중심 AI 교육, 사내 챗봇 구축, 도입 컨설팅"
                  onChange={(event) => set("features", event.target.value)}
                />
              </div>

              <UseCaseTable
                rows={draft.useCases}
                onChange={(next) => set("useCases", next)}
              />

              <ClosingFields
                value={draft.closing}
                onChange={(next) => set("closing", next)}
              />
            </div>

            <div className="brand-editor-column">
              {/* 자료를 넣는 칸. 원고 작성 화면의 참고자료 칸과 **같은 모양**이어야 해서
                  같은 `brief-reference-*` 클래스를 그대로 쓴다(2026-08-10 사용자 요청 —
                  "그림과 똑같이"). 두 화면을 통일하는 방향은 전에도 같았다: 원고 쪽이
                  브랜드의 FileDropZone을 가져다 썼다(2026-08-07). */}
              <div className="brand-field brand-upload-field">
                <div className="brief-reference-compose brand-reference-compose">
                  <div className="brief-reference-tabs" role="tablist" aria-label="브랜드 자료 종류">
                    <button
                      type="button"
                      id="brand-reference-tab-memo"
                      role="tab"
                      aria-selected={referenceTab === "memo"}
                      aria-controls="brand-reference-panel-memo"
                      onClick={() => setReferenceTab("memo")}
                    >
                      <IconDocMark />
                      메모
                    </button>
                    <button
                      type="button"
                      id="brand-reference-tab-url"
                      role="tab"
                      aria-selected={referenceTab === "url"}
                      aria-controls="brand-reference-panel-url"
                      onClick={() => setReferenceTab("url")}
                    >
                      <IconUrlMark />
                      URL
                    </button>
                    <button
                      type="button"
                      id="brand-reference-tab-file"
                      role="tab"
                      aria-selected={referenceTab === "file"}
                      aria-controls="brand-reference-panel-file"
                      onClick={() => setReferenceTab("file")}
                    >
                      <IconUploadMark />
                      파일 업로드
                    </button>
                  </div>

                  <div
                    className="brief-reference-panel brief-reference-panel--memo"
                    id="brand-reference-panel-memo"
                    role="tabpanel"
                    aria-labelledby="brand-reference-tab-memo"
                    hidden={referenceTab !== "memo"}
                  >
                    <label className="sr-only" htmlFor="brandMemo">
                      브랜드 메모
                    </label>
                    <div className="brief-reference-textarea">
                      <textarea
                        id="brandMemo"
                        maxLength={MAX_MEMO_CHARS}
                        disabled={attachmentsPending}
                        placeholder={"예: 무료 체험 링크를 포함하고,\n지난달 업데이트 내용을 강조해 주세요."}
                        value={memoDraft}
                        onChange={(event) => setMemoDraft(event.target.value)}
                      />
                      <span aria-live="polite">
                        {memoDraft.length.toLocaleString()}/{MAX_MEMO_CHARS.toLocaleString()}자
                      </span>
                    </div>
                    <div className="brief-reference-panel-actions">
                      <button
                        type="button"
                        className="button primary brief-reference-add"
                        disabled={!memoDraft.trim() || attachmentsPending}
                        onClick={addMemo}
                      >
                        메모 추가
                      </button>
                    </div>
                  </div>

                  <div
                    className="brief-reference-panel brief-reference-panel--url"
                    id="brand-reference-panel-url"
                    role="tabpanel"
                    aria-labelledby="brand-reference-tab-url"
                    hidden={referenceTab !== "url"}
                  >
                    <label className="sr-only" htmlFor="brandUrl">
                      브랜드 관련 주소
                    </label>
                    <div className={`brief-url-input${urlProblem ? " has-error" : ""}`}>
                      <IconUrlMark />
                      <input
                        id="brandUrl"
                        type="url"
                        aria-invalid={urlProblem ? true : undefined}
                        aria-describedby={urlProblem ? "brandUrl-problem" : undefined}
                        placeholder="https://example.com"
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
                      <p className="field-error" id="brandUrl-problem" role="alert">
                        {urlProblem}
                      </p>
                    )}
                    <div className="brief-reference-panel-actions">
                      <button
                        type="button"
                        className="button primary brief-reference-add"
                        disabled={!urlDraft.trim() || Boolean(urlProblem)}
                        onClick={addUrl}
                      >
                        URL 추가
                      </button>
                    </div>
                  </div>

                  <div
                    className="brief-reference-panel brief-reference-panel--file"
                    id="brand-reference-panel-file"
                    role="tabpanel"
                    aria-labelledby="brand-reference-tab-file"
                    hidden={referenceTab !== "file"}
                  >
                    {/* 자료 파일과 이미지를 한 칸으로 받는다(2026-08-10 사용자 요청 "그냥
                        합쳐"). 문서인지 이미지인지는 사람이 고르지 않고 파일 형식으로
                        가른다. 상한은 시안(10MB)이 아니라 **실제로 막는 값**을 적는다 —
                        화면이 받아 준다고 한 파일이 저장에서 거부되면 안 된다. */}
                    <FileDropZone
                      id="brand-attachments"
                      accept=".txt,.md,.markdown,text/plain,text/markdown,application/pdf,image/*"
                      disabled={attachmentsPending}
                      ariaLabel="브랜드 자료 파일·이미지"
                      title="파일이나 이미지를 드래그하거나 선택하여 업로드"
                      hint={`텍스트(.txt, .md), PDF, 이미지 JPG·PNG·WEBP (이미지 한 장 최대 ${MAX_IMAGE_BYTES / (1024 * 1024)}MB · 최대 ${MAX_IMAGES}장)`}
                      onFiles={(files) => void addAttachments(files)}
                    />
                  </div>
                </div>

                {/* 넣어 둔 자료 목록. 원고 작성 화면의 '추가된 참고자료' 칸과 같은
                    모양이고, 그 화면과 달리 옆이 아니라 **등록 칸 바로 아래**에 있다
                    (2026-08-10 사용자 요청 — 오른쪽 한 칸에 세로로 쌓는 배치라서). */}
                <div className="brief-reference-collection brand-reference-collection">
                  <div className="brief-reference-collection-head">
                    <h4>추가된 참고자료</h4>
                    {/* 상한은 하나가 아니다 — 문서와 이미지가 따로 막힌다. 실제로 막는
                        값을 그대로 적는다(주소는 개수 제한이 없어 세지 않는다). */}
                    <span aria-live="polite">
                      문서 {draft.documents.length}/{MAX_DOCUMENTS} · 이미지{" "}
                      {draft.images.length}/{MAX_IMAGES}
                    </span>
                  </div>

                  {collected.length ? (
                    <ul className="brief-reference-list">
                      {collected.map((item) => (
                        <li key={item.key}>
                          <span className={`brief-reference-kind is-${item.tone}`}>
                            {item.icon}
                            {item.badge}
                          </span>
                          <span className="brief-reference-detail" title={item.detail}>
                            {item.thumbnail && (
                              <img className="brand-reference-thumb" src={item.thumbnail} alt="" />
                            )}
                            {item.detail}
                            {item.bytes !== undefined && (
                              <small> · {formatFileBytes(item.bytes)}</small>
                            )}
                            {item.extra}
                          </span>
                          <button
                            type="button"
                            className="brief-reference-remove"
                            aria-label={`${item.badge} 빼기`}
                            onClick={item.remove}
                          >
                            <IconRemove />
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="brief-reference-empty">
                      <span>
                        <IconDocMark />
                      </span>
                      {/* 첨부가 오기 전에 새 파일을 받으면 도착한 첨부가 그 위를 덮어쓴다.
                          그래서 입력을 잠그고, 비어 있는 이유를 여기서 말한다. */}
                      {attachmentsPending ? (
                        <p role="status">{pendingNote}</p>
                      ) : (
                        <>
                          <p>아직 추가된 참고자료가 없습니다.</p>
                          <small>위에서 메모, URL 또는 파일을 추가해 주세요.</small>
                        </>
                      )}
                    </div>
                  )}

                  {/* 시안의 숫자가 아니라 **실제로 막는 값**을 적는다 — 화면이 받아 준다고
                      한 파일이 저장에서 거부되면 안 된다. */}
                  <div className="brief-reference-limits">
                    <span>
                      문서 최대 {MAX_DOCUMENTS}개 · 이미지 최대 {MAX_IMAGES}장 · 이미지 한 장{" "}
                      {MAX_IMAGE_BYTES / (1024 * 1024)}MB · PDF {MAX_PDF_BYTES / (1024 * 1024)}MB ·
                      이미지+PDF 합계 {MAX_ATTACHMENT_TOTAL_BYTES / (1024 * 1024)}MB
                    </span>
                    <span>
                      지원 형식: TXT, MD, PDF, JPG, PNG, WEBP
                      {attachedBytes > 0 && ` · 현재 ${formatFileBytes(attachedBytes)}`}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <BrandSection
            icon={<IconUsers />}
            title="주요 고객"
            hint="브랜드가 주로 상대하는 고객 유형을 단계별로 선택해 주세요. (복수 선택 가능)"
          >
            <AudiencePicker value={draft.audiences} onChange={(next) => set("audiences", next)} />
          </BrandSection>

        </div>
      </section>
    </div>
  );
}
