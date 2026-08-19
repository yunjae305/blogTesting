import type { ArticleLength } from "../../api/types";
import { readerAgeLabel } from "../../constants";
import { personaLabel } from "../../personas";
import { useStore } from "../../store";
import { formatDate } from "../../utils";
import type { BriefPreview } from "./StepTopic";

// 숫자는 백엔드 ARTICLE_LENGTH_TARGETS(prompts.py)의 (최소~최대) 범위와 맞춘 안내값이다.
// '길게'는 제거됐다 — 옛 설정의 "long"은 사용처의 ?? 폴백으로 '중간'으로 표시된다.
const ARTICLE_LENGTH_LABELS: Partial<Record<ArticleLength, string>> = {
  short: "짧게 (800~1,200자)",
  medium: "중간 (1,800~2,300자)",
};

function SummaryIcon({ name }: { name: string }) {
  const common = {
    viewBox: "0 0 24 24",
    "aria-hidden": true,
    focusable: false,
  } as const;

  if (name === "소재") {
    return (
      <svg {...common}>
        <circle cx="12" cy="8" r="3" />
        <path d="M6.5 19c.4-4 2.2-6 5.5-6s5.1 2 5.5 6" />
      </svg>
    );
  }
  if (name === "선택 키워드") {
    // 검색어를 고른 것이므로 값표(태그) 모양 — 소재(사람)·목적(연필)과 구분된다.
    return (
      <svg {...common}>
        <path d="M4 11V5a1 1 0 0 1 1-1h6l8 8-7 7-8-8Z" />
        <circle cx="8" cy="8" r="1.3" />
      </svg>
    );
  }
  if (name === "카테고리") {
    // 분류함(폴더) 모양 — 소재(사람)·키워드(값표)와 한눈에 갈린다.
    return (
      <svg {...common}>
        <path d="M4 7a1 1 0 0 1 1-1h4l2 2h8a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7Z" />
      </svg>
    );
  }
  if (name === "목적") {
    return (
      <svg {...common}>
        <path d="m5 17 1-4 8.5-8.5a2.1 2.1 0 0 1 3 3L9 16l-4 1Z" />
        <path d="m13.5 5.5 3 3M5 17l3-1" />
      </svg>
    );
  }
  if (name === "연령대") {
    return (
      <svg {...common}>
        <rect x="5" y="5" width="12" height="12" rx="2" />
        <path d="M9 9h10v10H9" />
      </svg>
    );
  }
  if (name === "해시태그 수") {
    return (
      <svg {...common}>
        <path d="M9 4 7 20M17 4l-2 16M4 9h16M3 15h16" />
      </svg>
    );
  }
  if (name === "원고 길이") {
    return (
      <svg {...common}>
        <path d="M6 19h12M8 17V7M12 17V4M16 17v-6" />
      </svg>
    );
  }
  if (name === "마지막 수정") {
    return (
      <svg {...common}>
        <path d="M7 5h10v15H7zM9 3h6v4H9zM10 11h4M10 15h4" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="12" cy="8" r="3" />
      <path d="M6.5 19c.4-4 2.2-6 5.5-6s5.1 2 5.5 6" />
    </svg>
  );
}

type SummaryProps = {
  brief?: boolean;
  draft?: BriefPreview | null;
};

export function Summary({ brief = false, draft = null }: SummaryProps) {
  const { task, settings, personas, personaCatalogLoading, draftRounds } = useStore();
  const input = task?.input;
  const topic = draft ? draft.topic : input?.topic;
  const purpose = draft ? draft.purpose : (input?.purpose ?? input?.keywords)?.join(", ");
  const readerAgeRange = draft ? draft.readerAgeRange : input?.readerAgeRange;
  // 소재 분야(2026-08-11). 옛 글에는 없고, 그때는 "-"다 — 지어내지 않는다.
  const subjectCategory = draft ? draft.subjectCategory : input?.subjectCategory;

  // 사용자가 트렌드 단계에서 고른 검색어. 건너뛴 글·예전 문서에는 없고(selectedKeywords가
  // 나중에 생긴 필드다), 그때는 소재만 보이면 된다.
  const selectedKeywords = (task?.trendSelection?.selectedKeywords ?? [])
    .map((keyword) => keyword.trim())
    .filter(Boolean);

  /**
   * 여러 편을 만들 때는 편마다 고른 키워드를 **따로** 보여 준다(2026-08-12 사용자 요청).
   *
   * 글 하나에는 트렌드 선택 자리가 하나뿐이라, 2편째 ②를 지나면 1편째 키워드가 그 자리에서
   * 사라진다. 그래서 라운드마다 고른 것을 화면이 배열로 들고 있고(store.draftRounds)
   * 여기서 "원고1 / 원고2"로 편다 — 앞서 무엇을 골랐는지 보이지 않으면 다음 편에서
   * 겹치는지 판단할 수 없다.
   */
  const draftCount = Math.max(1, task?.input?.draftCount ?? 1);
  // 지금 고르는 중인 편은 **끝난 라운드 수**로 센다 — 배열 길이가 아니다. 한 라운드는
  // 방향까지 골라야 끝나는데, ②만 지난 편도 배열에는 이미 들어 있어 길이로 세면 아직
  // 고르는 중인 편이 '-'가 되고 다음 편이 '고르는 중'으로 앞서 나갔다.
  const activeRound = draftRounds.filter((round) => round.intentId).length;
  const keywordRows: [string, string][] =
    draftCount > 1
      ? Array.from({ length: draftCount }, (_, index) => {
          const round = draftRounds[index];
          const picked = (round?.keywords ?? []).filter(Boolean);
          return [
            `원고${index + 1} 키워드`,
            // 트렌드 없이 소재만으로 간 편은 고른 키워드가 없다 — 아직 안 고른 것과
            // 구분해 적는다. '-'로 두면 빠뜨린 칸처럼 보인다.
            picked.length
              ? picked.join(", ")
              : round
                ? "트렌드 없이 소재만"
                : index === activeRound
                  ? "고르는 중"
                  : "-",
          ] as [string, string];
        })
      : [["선택 키워드", selectedKeywords.length ? selectedKeywords.join(", ") : "-"]];

  const rows: [string, string][] = [
    ["페르소나", personaLabel(settings, personas, personaCatalogLoading)],
    ["소재", topic || "-"],
    ...keywordRows,
    ["카테고리", subjectCategory || "-"],
    ["목적", purpose || "-"],
    // 저장값은 "30s"지만 화면에는 "30대"로 보여야 한다 — 저장 키를 그대로 내보내고
    // 있었다(2026-08-05 사용자 지적). 모르는 값이면 저장값을 그대로 두어, 라벨을 못 찾은
    // 것을 '전체'로 감추지 않는다.
    ["연령대", readerAgeLabel(readerAgeRange)],
    ["해시태그 수", settings ? `${settings.hashtagCount}개` : "설정 전"],
    ["원고 길이", settings ? ARTICLE_LENGTH_LABELS[settings.articleLength] ?? "중간" : "설정 전"],
    ["마지막 수정", task ? formatDate(task.updatedAt) : "-"],
  ];

  return (
    <section className="panel summary-note">
      <span className="summary-tape" aria-hidden="true" />
      <div className="panel-header">
        <div className="panel-heading-copy">
          <p className="panel-kicker">WRITING NOTE</p>
          <h2 className="panel-title">이 글 요약</h2>
        </div>
        <svg className="summary-smile" viewBox="0 0 36 36" aria-hidden="true">
          <circle cx="18" cy="18" r="15" fill="none" stroke="currentColor" strokeWidth="1.8" />
          <path d="M11.5 20.5c2.5 6 10.5 6 13 0M12 13.5h.1M24 13.5h.1" fill="none" stroke="currentColor" strokeLinecap="round" strokeWidth="2.3" />
        </svg>
      </div>
      <div className="panel-body">
        <dl className="summary-list">
          {rows.map(([key, value]) => (
            <div className="summary-row" key={key}>
              <dt className="summary-key">
                {brief && <SummaryIcon name={key} />}
                <span>{key}</span>
              </dt>
              <dd className="summary-value">{value}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}
