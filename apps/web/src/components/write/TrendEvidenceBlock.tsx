/**
 * 트렌드 카드 안의 출처별 3줄 지표.
 *
 * 대표 출처(item.source)의 실제 수집 근거만 그린다 — 다른 플랫폼의 점수를 섞거나
 * 수치를 합산하지 않는다. 근거가 없는 카드(옛 캐시·근거 없는 출처)는 수치를 지어내는
 * 대신 중립 문구 한 줄을 그리되, 레이아웃 공간은 그대로 차지해 카드 높이가 제각각
 * 되지 않게 한다.
 *
 * 아이콘은 기존 카드 버튼들과 같은 방식의 인라인 SVG다(외부 이미지 요청 없음).
 * 첫 줄의 아이콘은 그림이 아니라 출처 로고(TrendSourceBadge) — 색만으로 의미를
 * 전달하지 않도록 문구와 로고(sr-only 이름 포함)를 함께 쓴다.
 */

import type { TrendMode, TrendSourceEvidence } from "../../api/types";
import { TrendSourceBadge } from "./TrendSourceBadge";
import {
  EVIDENCE_FALLBACK,
  EVIDENCE_SOURCES,
  NAVER_SCOPE_NOTE,
  evidenceRows,
} from "./trendEvidence";

const SOURCE_VARIANTS: Record<string, string> = {
  GOOGLE_TRENDS: "google",
  NAVER_DATALAB: "naver",
  YOUTUBE: "youtube",
};

/** 읽어 주는 쪽에 쓰는 이름. 로고만으로는 출처가 전달되지 않는다. */
const SOURCE_NAMES: Record<string, string> = {
  GOOGLE_TRENDS: "Google",
  NAVER_DATALAB: "네이버",
  YOUTUBE: "YouTube",
};

/** 지표 행 아이콘. 전부 stroke 기반이라 CSS의 currentColor를 따른다. */
function EvidenceIcon({ icon }: { icon: string }) {
  const common = {
    viewBox: "0 0 24 24",
    "aria-hidden": true,
    focusable: false,
  } as const;

  switch (icon) {
    case "chart": // 검색량·증가율 — 막대그래프
      return (
        <svg {...common}>
          <path d="M5 19V12M12 19V5M19 19v-9" />
          <path d="M4 19h16" />
        </svg>
      );
    case "clock": // 시작·수집 시각
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 7.5V12l3 2" />
        </svg>
      );
    case "news": // 뉴스 — 신문
      return (
        <svg {...common}>
          <rect x="4" y="5" width="16" height="14" rx="2" />
          <path d="M8 9.5h8M8 13h8M8 16.5h4" />
        </svg>
      );
    case "speech": // 블로그 — 말풍선
      return (
        <svg {...common}>
          <path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H12l-4.5 4v-4h-1A2.5 2.5 0 0 1 4 13.5Z" />
        </svg>
      );
    case "docs": // 관련 콘텐츠 — 문서 묶음
      return (
        <svg {...common}>
          <rect x="7" y="7" width="13" height="13" rx="2" />
          <path d="M17 7V5.5A1.5 1.5 0 0 0 15.5 4h-10A1.5 1.5 0 0 0 4 5.5v10A1.5 1.5 0 0 0 5.5 17H7" />
        </svg>
      );
    case "video": // 게시 시각·최근 영상 수 — 영상
      return (
        <svg {...common}>
          <rect x="3.5" y="6" width="13" height="12" rx="2.5" />
          <path d="m16.5 10.5 4-2.5v8l-4-2.5" />
        </svg>
      );
    case "rate": // 시간당 평균 — 상승 그래프
      return (
        <svg {...common}>
          <path d="M4 17.5 10 11l3.5 3L20 7" />
          <path d="M15.5 7H20v4.5" />
        </svg>
      );
    default:
      return null;
  }
}

export function TrendEvidenceBlock({
  mode,
  source,
  evidence,
  suggestedBy,
  now,
}: {
  mode: TrendMode;
  /** 지표를 그린 출처 — 실제로 그 수치를 잰 곳이다. */
  source: string;
  evidence?: TrendSourceEvidence | null;
  /**
   * 이 키워드를 내놓은 출처가 따로 있으면 그 값. 구글 자동완성이 소재 연관 검색어를
   * 제안하고 네이버가 수치를 잰 경우가 그렇다 — 두 로고를 함께 보여, 제안한 곳과 잰 곳을
   * 바꿔치기하지 않는다.
   */
  suggestedBy?: string | null;
  /** 테스트가 결정적 상대 시각을 만들 수 있게 주입한다. 화면에서는 생략(현재 시각). */
  now?: number;
}) {
  // Instagram·소재 확장은 이번 지표 대상이 아니다 — 기존 로고 표시를 유지하고,
  // 지표를 만들어내지 않는다(블록 자체를 그리지 않는다).
  if (!EVIDENCE_SOURCES.has(source)) return null;

  const variant = SOURCE_VARIANTS[source];
  const rows = evidenceRows(mode, source, evidence, now);
  const showsBoth = Boolean(suggestedBy && suggestedBy !== source);

  const head = (
    <>
      {showsBoth && <TrendSourceBadge source={suggestedBy as string} />}
      <TrendSourceBadge source={source} />
    </>
  );

  if (!rows) {
    // 근거가 생기기 전의 데이터: 로고는 그대로 두고 수치 대신 중립 문구를 쓴다.
    return (
      <span className={`title-evidence title-evidence--${variant}`}>
        <span className="title-evidence-row title-evidence-head">
          <TrendSourceBadge source={suggestedBy || source} />
          <span className="title-evidence-text title-evidence-muted">{EVIDENCE_FALLBACK}</span>
        </span>
      </span>
    );
  }

  return (
    <span className={`title-evidence title-evidence--${variant}`}>
      {rows.map((row, index) => (
        <span
          key={row.id}
          className={`title-evidence-row${index === 0 ? " title-evidence-head" : ""}`}
        >
          {index === 0 ? (
            head
          ) : (
            <span className="title-evidence-icon" aria-hidden="true">
              <EvidenceIcon icon={row.icon} />
            </span>
          )}
          <span className="title-evidence-text">
            {row.segments.map((segment, segmentIndex) =>
              segment.tone ? (
                <em key={segmentIndex} className={`title-evidence-${segment.tone}`}>
                  {segment.text}
                </em>
              ) : (
                <span key={segmentIndex}>{segment.text}</span>
              ),
            )}
          </span>
        </span>
      ))}
      {showsBoth && (
        // 색과 로고만으로는 "제안한 곳과 잰 곳이 다르다"가 전달되지 않는다.
        <span className="sr-only">
          검색어는 {SOURCE_NAMES[suggestedBy as string] ?? suggestedBy}에서 제안했고, 수치는{" "}
          {SOURCE_NAMES[source] ?? source}에서 확인했습니다.
        </span>
      )}
      {source === "NAVER_DATALAB" && <span className="sr-only">{NAVER_SCOPE_NOTE}</span>}
    </span>
  );
}
