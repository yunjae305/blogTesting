/** Wire types. Mirrors apps/api/app/shared (which mirrors the Mongo documents). */

export type BlogTaskStatus =
  | "INPUT"
  | "REFERENCE_PROCESSING"
  | "SEARCH_ANALYZING"
  | "INTENT_SELECTED"
  | "GENERATING"
  | "READY_TO_PUBLISH"
  | "POSTING"
  | "POSTED"
  | "POSTING_NEEDS_HUMAN"
  | "FAILED"
  | "CONTENT_POLICY_VIOLATION";

export type ReferenceMaterialType = "IMAGE" | "PDF" | "TEXT" | "URL";
export type PostingMethod = "copy" | "draft" | "auto";
export type PostingChannel = "naver" | "threads";

export interface PublicUser {
  userId: string;
  email: string;
  nickname: string;
  createdAt: string;
  updatedAt: string;
}

export interface AuthSession {
  user: PublicUser;
  accessToken: string;
  issuedAt: string;
  expiresAt: string;
}

export type ArticleLength = "short" | "medium" | "long";

/** 제목에서 소재와 트렌드 키워드 중 무엇을 중심에 둘지. 예전의 3:7 고정 비율을 대체한다. */
export type BlendMode = "subject" | "balanced" | "trend";

export interface UserSettings {
  userId: string;
  hashtagCount: number;
  articleLength: ArticleLength;
  blendMode: BlendMode;
  defaultPersona: string;
  customPersonaName?: string;
  customPersonaDescription?: string;
  customPersona?: string;
  autoPostingEnabled: boolean;
  createdAt: string;
  updatedAt: string;
}

/** GET /personas가 반환하는 공용 카탈로그 항목. custom의 실제 값은 사용자 설정에 저장한다. */
export interface PersonaCatalogEntry {
  personaId: string;
  kind: "preset" | "custom";
  name: string;
  description: string;
  /** preset에는 필수이며 custom 응답에서는 생략될 수 있다. */
  prompt?: string | null;
}

export interface ReferenceMaterial {
  type: ReferenceMaterialType;
  value: string;
  name?: string;
  /**
   * 이 자료를 누가 넣었는가(2026-08-11). "brand"는 서버가 고른 브랜드의 자료에서 펼쳐
   * 넣은 것이고, 없으면 사용자가 직접 올린 것이다. 소재 화면의 '추가된 참고자료' 목록은
   * 사용자 것만 보여 준다 — 브랜드 자료 수십 개가 그 목록을 뒤덮으면 안 되고, 거기서
   * 지울 수 있어서도 안 된다(브랜드 자료 편집에서 지우는 값이다).
   */
  origin?: string | null;
}

export interface BlogTaskInput {
  topic: string;
  subject?: string;
  /**
   * 사용자가 고른 소재 분야(constants.SUBJECT_CATEGORIES 중 하나). 동명이의어를 가르는
   * 값이다 — '오디세이'가 영화인지 게임인지. 옛 글에는 없다.
   */
  subjectCategory?: string;
  /**
   * 이 글에 엮은 브랜드. 소재 단계로 돌아왔을 때 어느 브랜드를 골랐었는지 화면이 이 값을
   * 읽는다. 브랜드를 쓰지 않은 글과 옛 글에는 없다.
   */
  brandId?: string;
  /**
   * 그 브랜드의 이름. **서버가 채운다** — 화면은 brandId만 보내고, 서버가 브랜드를 확인한
   * 뒤 이름을 적는다. 프롬프트가 이 값을 읽어 "곁들일 브랜드이지 소재가 아니다"라고
   * 말하므로, 화면이 보낸 값을 믿으면 없는 브랜드 이름이 글에 실린다.
   */
  brandName?: string;
  /**
   * 그 브랜드가 이 글에서 맡는 역할(2026-08-19). **서버가 정한다.**
   *
   * - `FOCUS` — 브랜드가 글의 주인공이다. 소재를 비우고 브랜드만 골랐을 때.
   * - `UTILITY` — 소재·트렌드가 주인공이고, 브랜드는 그 상황에서 쓴 도구로만 나온다.
   *
   * 화면은 소재를 적었는지로 어느 쪽이 될지 미리 보여 주고(사용자가 직접 바꿀 수도
   * 있다), 저장하면 서버가 확정한 값이 이 자리에 돌아온다.
   */
  brandMode?: "FOCUS" | "UTILITY";
  /**
   * 소재와 브랜드가 자연스럽게 닿는가 — `A`(바로 연결) / `B`(상황을 만들면) /
   * `C`(억지 연결). 브랜드를 도구로 쓰는 글에만 있다.
   */
  brandFitGrade?: "A" | "B" | "C";
  /** 이 소재에 닿은 기준표 줄들("상황 → 기능"). 원고 프롬프트가 그대로 읽는다. */
  brandUseCases?: string[];
  purpose?: string[];
  keywords: string[];
  targetReader?: string;
  readerAgeRange?: string;
  readerKnowledgeLevel?: string;
  referenceMaterials: ReferenceMaterial[];
  /**
   * 사용자가 고른 **원고 작업 시각**(UTC ISO). 없으면 예전 그대로 — 방향을 고르는 즉시
   * 원고를 만든다. 있으면 방향까지 고른 뒤 예약으로 넘어가, 그 시각에 자료를 새로 모아
   * 원고를 만든다(2026-08-11).
   */
  scheduledRunAt?: string;
  /** 이 소재로 만들 원고 수(1~3). 1이면 보내지 않는다 — 예전 그대로다. */
  draftCount?: number;
  /** 원고를 다 만들면 네이버에 올릴지. 없으면 올린다(예전 그대로). */
  autoPublishNaver?: boolean;
  /** 원고를 다 만들면 쓰레드에 올릴지. 없으면 올리지 않는다. */
  autoPublishThreads?: boolean;
  /** 시각을 고를 때 쓰던 시간대(IANA). 표시용이며 계산에는 쓰지 않는다. */
  scheduledTimezone?: string;
}

/** 자료 성격. OFFICIAL=공식자료, NEWS=뉴스, BLOG=블로그·후기, REPORT=통계·보고서, CASE=활용 사례. */
export type SourceType = "OFFICIAL" | "NEWS" | "BLOG" | "REPORT" | "CASE";

export interface SearchSource {
  title: string;
  url: string;
  snippet: string;
  /** 서버가 분류한 자료 성격. 예전 자료에는 없어 빈 문자열일 수 있다. */
  sourceType?: SourceType | "";
  /** 소재·트렌드·독자 관련도 0-100. */
  relevanceScore?: number;
}

export interface IntentCandidate {
  intentId: string;
  title: string;
  targetReader: string;
  rationale: string;
  keywords: string[];
  sources: SearchSource[];
}

export interface IntentValidationResult {
  promptVersion: string;
  provider: string;
  model: string;
  analyzedAt: string;
  intentCandidates: IntentCandidate[];
  /**
   * 검색이 실제로 찾아 온 자료의 총 개수. 방향 하나에 붙는 자료는 상한이 있어 화면에
   * 다 보이지 않으므로, 그 아래에 '외 N개'로 적는다.
   *
   * 이 필드가 생기기 전에 저장된 검증 결과에는 값이 없다(0 또는 undefined) — 그때는
   * 아무것도 적지 않는다. 없는 숫자를 지어내지 않는다.
   */
  collectedSourceCount?: number;
}

/**
 * 근거 데이터가 온 실제 경로. 구글 수집은 트렌드 페이지 크롤링(GOOGLE_TRENDS_WEB)
 * 하나이며, SERPAPI·GOOGLE_RSS는 그 전에 저장된 데이터를 읽을 때만 나타난다
 * (RSS 근거에는 상승률·시작 시각이 없어 화면 문구가 다르다).
 */
export type TrendEvidenceOrigin =
  | "GOOGLE_TRENDS_WEB"
  | "SERPAPI"
  | "GOOGLE_RSS"
  | "YOUTUBE_API"
  | "NAVER_SEARCH_API";

/** 구글 트렌드 응답에서 실제로 받은 값만 있다. 없는 값은 서버가 만들지 않는다. */
export interface GoogleTrendEvidence {
  active?: boolean | null;
  searchVolume?: number | null;
  increasePercentage?: number | null;
  /** 상승 시작 시각(UTC ISO). SerpApi 경로에만 있다. */
  startedAt?: string | null;
  /** 공식 RSS의 approx_traffic. RSS 폴백에만 있다. */
  approximateTraffic?: number | null;
  feedType?: TrendEvidenceOrigin | string | null;
}

/**
 * 네이버가 이 키워드에 대해 말해 주는 것. **무엇을 잰 값인지는 basis가 가른다.**
 * - SEARCH_API_SAMPLE(발굴): 이번 수집 표본에서 키워드가 확인된 고유 문서 수. 표본 크기에
 *   갇힌 값이며 네이버 전체 검색량이 아니다.
 * - SEARCH_API_TOTAL(보강): 키워드 자체를 검색해 받은 결과 총수. 표본 상한이 없다.
 */
export interface NaverTrendEvidence {
  /* SEARCH_API_SAMPLE */
  recentNewsCount?: number | null;
  collectedBlogCount?: number | null;
  collectedRelatedContentCount?: number | null;
  sampledDocumentCount?: number | null;
  /* SEARCH_API_TOTAL */
  totalNewsCount?: number | null;
  totalBlogCount?: number | null;
  recentDocumentCount?: number | null;
  /** 최근 글 수가 표본 상한에 걸렸는가. 그러면 "N건+"로 적어야 한다. */
  recentHitCap?: boolean | null;
  basis?: "SEARCH_API_SAMPLE" | "SEARCH_API_TOTAL" | string | null;
}

/** 수집한 영상 묶음에서 계산한 조회 지표. 평균은 누적 조회수 ÷ 게시 후 경과시간이라
    '실시간 조회 속도'가 아니다 — 화면은 반드시 '업로드 후 시간당 평균'으로 쓴다. */
export interface YouTubeTrendEvidence {
  topVideoId?: string | null;
  topVideoTitle?: string | null;
  topViewCount?: number | null;
  topVideoPublishedAt?: string | null;
  averageViewsPerHour?: number | null;
  recentVideoCount?: number | null;
  recentWindowDays?: number | null;
}

/** 출처 하나가 이 키워드에 대해 관측한 근거. 출처마다 척도가 달라 절대 합산하지 않는다. */
export interface TrendSourceEvidence {
  source: string;
  /** 실제 출처 데이터를 관측한 시각. 추천 응답을 만든 generatedAt과 다르다. */
  observedAt?: string | null;
  dataOrigin?: TrendEvidenceOrigin | string | null;
  google?: GoogleTrendEvidence | null;
  naver?: NaverTrendEvidence | null;
  youtube?: YouTubeTrendEvidence | null;
}

export interface TrendKeyword {
  trendKeywordId: string;
  keyword: string;
  normalizedKeyword?: string;
  tokens?: string[];
  tokenSetSignature?: string;
  clusterId?: string;
  source: string;
  sources?: string[];
  rank: number;
  score: number;
  trendScore?: number;
  /** 정규화된 실시간 상승도(점수식의 hotness 항). "최신순" 정렬이 이 값을 쓴다. */
  hotness?: number;
  qualityScore?: number;
  finalScore?: number;
  trendReason?: string;
  connectionIdea?: string;
  period?: string;
  /**
   * 0-100: how naturally this keyword ties into the user's subject, judged by the
   * M2 model. Absent when the scoring call failed, in which case the panel falls
   * back to ranking by how hot the keyword is.
   */
  relevance?: number;
  /** 관련도 판정의 축별 점수(각 0-100). 소재 축이 소재 관련순의 게이트·정렬, 나머지는 툴팁용. */
  subjectRelevance?: number;
  purposeRelevance?: number;
  personaRelevance?: number;
  /** 소재 관련순의 노출 조건(관계 유형 게이트 + 유형별 점수 하한)을 통과했는지. 판정 전이면 없음. */
  isEligible?: boolean;
  /**
   * 소재와 맺는 관계의 종류. 점수와 별개의 게이트이며, 화면은 이 값으로 "왜 관련 있다고
   * 판단했는지"를 설명한다. DIRECT=소재 자체, ADJACENT=함께 찾는 주제, CONTEXTUAL=상황으로 연결.
   * FORCED/NONE/AMBIGUOUS는 서버가 이미 걸러 화면까지 오지 않는다.
   */
  relationType?: "DIRECT" | "ADJACENT" | "CONTEXTUAL" | string;
  /** The keyword's field (스포츠·대회, 뷰티·패션·쇼핑, …), used to keep the four cards
   *  from clustering in one category. Absent when scoring was skipped. */
  category?: string;
  /**
   * 출처별 실제 수집 근거(키 = TrendSource 값). 카드의 3줄 지표는 대표 출처(source)의
   * 근거만 그린다. 근거가 생기기 전의 응답·저장 데이터에는 없다 — 그때 화면은 지표 대신
   * 중립 문구를 쓰고, 없는 수치를 지어내지 않는다.
   */
  evidenceBySource?: Record<string, TrendSourceEvidence>;
  collectedAt: string;
}

export interface TopicCandidate {
  topicCandidateId: string;
  title: string;
  description: string;
  trendKeywordIds: string[];
  recommended: boolean;
  /** 루브릭 총점(0-100). 추천은 최고점 제목. 채점 전이면 없음. */
  score?: number;
  /** 추천/점수 근거 한 줄. 사용자에게 표시. */
  reason?: string;
  /**
   * 이 제목이 쓴 후킹 유형·강도. 화면에는 표시하지 않는다(디자인 변경 아님) — 서버가
   * 생성·채점의 내부 정보로만 쓰고, 선택된 제목의 약속을 원고 단계에 넘긴다. 예전 후보에는 없음.
   */
  hookType?: TitleHookType;
  hookStrength?: "LOW" | "MEDIUM" | "HIGH";
}

/** 제목에 얹은 후킹의 종류. 제목 선택 시 서버로 되돌려 보내 원고 단계까지 전달된다. */
export type TitleHookType =
  | "NONE"
  | "CURIOSITY"
  | "LOSS_AVERSION"
  | "FOMO"
  | "AUTHORITY"
  | "REVERSAL"
  | "COMPARISON"
  | "IDENTITY"
  | "STORY";

/** 트렌드 키워드를 제공하는 두 목적. TRENDING=추천어(지금 뜨는 트렌드), MATERIAL_RELATED=소재 관련어. */
export type TrendMode = "TRENDING" | "MATERIAL_RELATED";

export interface TrendRecommendation {
  postId: string;
  mode?: TrendMode;
  trendKeywords: TrendKeyword[];
  topicCandidates: TopicCandidate[];
  generatedAt: string;
  cacheStatus?: "fresh" | "stale" | "miss" | string;
  refreshing?: boolean;
  /** 소재 관련순 결과의 출처: 저장된 풀(database) / 후보가 모자라 새로 수집(external_api). */
  source?: "database" | "external_api" | string;
  /**
   * 소재 관련순 로테이션. '다른 키워드 보기'는 받은 nextCursor를 그대로 돌려보내기만 한다
   * (서버가 만든 불투명 값이라 클라이언트가 해석하지 않는다). 최신순에서는 전부 없음.
   */
  nextCursor?: string;
  /** 게이트를 통과한 소재 관련 후보의 총 개수. */
  poolSize?: number;
  /** 아직 보지 않은 후보가 남았는가. */
  hasMore?: boolean;
  /** 풀을 한 바퀴 돌아 처음으로 되돌아온 배치인가. 오류가 아니라 정상 순환이다. */
  cycled?: boolean;
}

/** Titles regenerated for one keyword, without re-collecting the keywords. */
export interface TrendTopicResult {
  postId: string;
  trendKeywordId: string;
  topicCandidates: TopicCandidate[];
  generatedAt: string;
}

export interface TrendSelection {
  topicCandidateId?: string;
  finalTopic: string;
  selectedTrendKeywordIds: string[];
  /**
   * 고른 트렌드 키워드의 문자열(원본 검색어). id만으로는 원고 단계에서 그 키워드를 알 수
   * 없어 함께 저장한다. 건너뛴 글·예전 문서에는 없다.
   */
  selectedKeywords?: string[];
  skipped: boolean;
  selectedAt: string;
  /** 고른 제목이 쓴 후킹 유형. 건너뛴 글·예전 문서에는 없다. */
  hookType?: TitleHookType;
}

/**
 * 이미지 한 장의 출처(2026-08-11). 화면에 뿌릴 문자열이 아니라 **구조화된 사실**이다.
 *
 * 여기 있는 값은 전부 서버가 실제로 확인한 것만 담긴다. 원문 페이지를 알 수 없으면
 * sourcePageUrl이 없고, 라이선스를 확인하지 못하면 usageStatus는 "unknown"이다 —
 * 비어 있는 것을 '사용 가능'으로 채우지 않는다.
 *
 * `imageUrl`에 해당하는 값은 여기 없다. 화면에 실제로 쓰는 주소는 GeneratedPostImage의
 * dataUrl이고, 밖에서도 열리는 주소는 originalImageUrl이다.
 */
export interface ImageSourceInfo {
  /** external=밖에서 가져온 이미지, generated=이미지 모델이 그린 것. 둘을 같게 다루지 않는다. */
  sourceType: "external" | "generated";
  /** 실제로 이미지가 게시된 사이트·채널 이름. 검색 서비스 이름(네이버·구글)이 아니다. */
  sourceName: string;
  /** 이미지가 실려 있던 원문 페이지. 확인된 경우에만 있다. */
  sourcePageUrl?: string | null;
  /** 밖에서도 열리는 원본 이미지 주소. 확인할 수 없으면 없다. */
  originalImageUrl?: string | null;
  /** 출처가 스스로 밝힌 라이선스 표기와 확인 페이지. 밝히지 않았으면 둘 다 없다. */
  license?: string | null;
  licenseUrl?: string | null;
  /** 이용 가능 여부. allowed는 라이선스를 실제로 확인했을 때만 온다. */
  usageStatus: "allowed" | "restricted" | "unknown";
}

export interface GeneratedPostImage {
  dataUrl: string;
  altText: string;
  prompt: string;
  provider: string;
  model: string;
  generatedAt: string;
  mimeType: string;
  /**
   * generated=이미지 모델, reference=사용자 업로드, rendered=코드 렌더링(차트·과정도 등),
   * web=웹에서 찾아온 실제 사진(캡션에 출처가 들어 있다).
   */
  source?: "generated" | "reference" | "rendered" | "web";
  /**
   * 화면에서 이 이미지를 어떻게 보여줄지. 사진과 도표는 같은 테두리·모서리를 쓰면 안 된다
   * — 도표는 테두리가 이미 그림 안에 있다. 옛 문서에는 없으므로 없으면 사진으로 다룬다.
   */
  mediaKind?: "cover" | "photo" | "reference" | "visual" | "screenshot";
  /** 자료 캡션(출처·기준시점 포함). 본문에는 이미지 아래 별도 문단으로 들어간다. */
  caption?: string;
  /**
   * 웹에서 가져온 사진(source="web")의 원본 이미지 주소. 원고 복사가 로컬 서버 주소
   * 대신 이것을 써서, 네이버·벨로그 등 바깥 에디터에서도 이미지가 보인다(2026-08-10).
   * 생성·업로드·렌더링 이미지와 옛 문서에는 없다.
   */
  sourceUrl?: string | null;
  /**
   * 구조화된 출처(2026-08-11). caption 문자열과 같은 사실을 값으로 들고 있다 — 화면이
   * 원문 링크·이용 조건까지 안정적으로 그리려면 문자열 하나로는 되지 않는다.
   * 코드로 그린 도표·사용자 업로드·옛 문서에는 없으며, 그때는 caption만 보여준다.
   */
  imageSource?: ImageSourceInfo | null;
}

export interface FinalPost {
  title: string;
  body: string;
  hashtags: string[];
  /** 대표 썸네일에 얹힌 문구. 최대 2줄, 한 줄 12자. 이미지 안에 이미 구워져 있다. */
  thumbnailCopy?: string[];
  images?: GeneratedPostImage[];
  /** 대표 썸네일. 항상 images[0]이고 1536×864다(본문 이미지는 1280×720). */
  featuredImage?: GeneratedPostImage;
  htmlContent: string;
  markdownContent?: string;
}

export interface PostingLog {
  logId: string;
  postId: string;
  userId: string;
  method: PostingMethod;
  /** 발행 목적지. 채널 개념이 없던 옛 로그에는 없고, 그때는 전부 네이버였다. */
  channel?: PostingChannel;
  result: "success" | "fail" | "needs_human";
  postUrl?: string;
  errorMessage?: string;
  /** 어느 PC가 발행했는가(에이전트 발행). 서버 발행과 옛 로그에는 없다. */
  createdAt: string;
}

export interface SelectedIntent {
  intentId: string;
  title: string;
  targetReader: string;
  rationale: string;
  /** 고른 의도의 검색 키워드. 예전 문서에는 없다. */
  keywords?: string[];
  sources?: SearchSource[];
}

/**
 * Where a long-running phase has got to. Present only while one is running.
 *
 * The step labels come from the server, not from a list here: the server is what
 * knows which step is actually running, and the client used to animate a fixed
 * list on a timer, which told the user nothing.
 */
export interface TaskProgress {
  phase: "SEARCH" | "DRAFT";
  step: number;
  totalSteps: number;
  label: string;
  steps: string[];
  startedAt: string;
  updatedAt: string;
  /**
   * 단계 안에서 **몇 개를 끝냈는지**(2026-08-11). 이미지 생성처럼 처리할 개수가 시작할
   * 때 정해지는 단계에만 있다 — 그때는 시간으로 짐작하지 않고 이 비율로 칸을 채운다.
   * 없으면(옛 문서·보고하지 않는 단계) 예전처럼 머문 시간으로 채운다.
   */
  unitsDone?: number;
  unitsTotal?: number;
  /**
   * 이 환경에서 **실제로 잰** 단계별 소요(초). 있으면 막대의 단계 몫을 이 값으로
   * 나눈다 — 없으면(첫 실행·서버 재시작 직후) 기본 상수를 쓴다.
   */
  stepSeconds?: number[];
}

/** 생성 중 '작업 현황' 로그 한 줄(2026-08-10). status 폴링 응답에만 실려 오고,
    서버 프로세스 메모리가 원본이라 DB에는 저장되지 않는다. */
export interface ActivityEntry {
  at: string;
  message: string;
}

/** 최종 검수(M4 4단계)가 항목별로 내린 판정. 서버의 FinalReviewCheck과 같은 모양이다. */
export interface FinalReviewCheck {
  status: "pass" | "warning" | "fail" | "skipped";
  reason: string;
  affectedSections: string[];
}

/** 검수가 **실제로 손댄 것**. 모델의 제안이 아니라 코드가 반영한 결과다. */
export interface FinalReviewTarget {
  kind: "paragraph" | "image";
  reference: string;
  action: "rewritten" | "removed";
  note: string;
}

/**
 * 최종 검수 결과.
 *
 * 화면에는 여기서 **요약만** 보여준다 — 내부 프롬프트나 원시 JSON을 그대로 노출하지 않는다.
 * 검수를 돌지 않았거나(설정으로 껐거나) 이 단계가 생기기 전의 글에는 없다.
 */
export interface FinalReview {
  reviewedAt: string;
  rounds: number;
  overallStatus: "pass" | "warning" | "revise";
  overallScore: number;
  checks: Partial<Record<string, FinalReviewCheck>>;
  /** 마지막 회차까지 남은 문제. 비어 있으면 더 고칠 것이 없다. */
  issues: { kind: string; severity: string; reason: string }[];
  revisionTargets: FinalReviewTarget[];
  applied: number;
  removedImages: number;
  /** 검수 자체가 실패했을 때의 사유. 그때도 원고는 그대로 쓴다. */
  error?: string | null;
}

export interface DraftGenerationResult {
  finalReview?: FinalReview;
}

export interface BlogTask {
  postId: string;
  userId: string;
  status: BlogTaskStatus;
  version: number;
  createdAt: string;
  updatedAt: string;
  statusHistory: { from: string; to: string; at: string; by: string }[];
  input: BlogTaskInput;
  postingLogs: PostingLog[];
  trendSelection?: TrendSelection;
  intentValidationResult?: IntentValidationResult;
  selectedIntent?: SelectedIntent;
  finalPost?: FinalPost;
  /** 원고 생성 결과. 화면은 그중 최종 검수 요약만 읽는다. */
  draftGenerationResult?: DraftGenerationResult;
  progress?: TaskProgress;
  /** 생성 중 작업 현황 로그. 전체 조회 응답에는 없고 status 폴링이 병합해 준다
      (store.applyTaskStatus) — 생성 화면의 '작업 현황' 패널이 읽는다. */
  activityLog?: ActivityEntry[];
}

/**
 * Lightweight data for the home and "내 글" cards.
 *
 * A full BlogTask can contain megabytes of article HTML and embedded image data.
 * List screens fetch this projection and load the full task only when the user
 * opens or copies one post.
 */
export interface BlogTaskListItem {
  postId: string;
  userId: string;
  status: BlogTaskStatus;
  version: number;
  createdAt: string;
  updatedAt: string;
  title: string;
  topic: string;
  subject?: string;
  purposes: string[];
  postUrl?: string;
  hasFinalPost: boolean;
}

/* ------------------------------------------------------- 예약 포스팅 (네이버) */

export type SchedulePlatform = "naver";

/** 소재를 글로 나누는 방식. multi=소재별 한 편, single=소재 하나로 여러 편. */
export type ScheduleTopicMode = "multi" | "single";

export type ScheduledBatchStatus =
  | "READY"
  | "RUNNING"
  | "PAUSE_REQUESTED"
  | "PAUSED"
  | "NEEDS_HUMAN"
  | "STOP_REQUESTED"
  | "STOPPED"
  | "COMPLETED"
  | "FAILED";

export type ScheduledJobStatus =
  | "WAITING"
  | "RUNNING"
  | "READY_TO_PUBLISH"
  | "PUBLISHING"
  | "COMPLETED"
  | "FAILED"
  | "NEEDS_HUMAN"
  | "CANCELED";

export type ScheduledJobStage =
  | "CREATE_POST"
  | "TREND_RECOMMENDATION"
  | "TITLE_GENERATION"
  | "SEARCH_ANALYSIS"
  | "INTENT_SELECTION"
  | "DRAFT_GENERATION"
  | "NAVER_PUBLISH"
  | "THREADS_PUBLISH"
  | "DONE";

export interface ScheduledLogEntry {
  at: string;
  message: string;
  /** 화면의 점 색만 정한다. */
  tone: "success" | "info" | "muted";
  jobId?: string;
}

export interface ScheduledJob {
  jobId: string;
  batchId: string;
  userId: string;
  platform: SchedulePlatform;
  sequence: number;
  topic: string;
  /** 같은 소재의 몇 번째 글인가(0부터). */
  variantIndex: number;
  /**
   * 이 소재가 어느 분야의 것인가(SUBJECT_CATEGORIES 중 하나). 「예약 포스팅」 탭에서
   * 줄마다 고를 수 있고, 비워 두면 없다 — 옛 작업에도 없다.
   */
  subjectCategory?: string;
  /**
   * 이 원고를 Naver에 올릴지. **옛 작업에는 없고, 없으면 true다** — 예전에는 Naver가
   * 언제나 발행 대상이었다. false면 Threads 단독 예약이다.
   */
  publishNaver?: boolean;
  /** 이 원고를 Threads에도 올릴지. 옛 작업에는 없다(그때는 Naver만). */
  publishThreads?: boolean;
  /**
   * **한 번에 건 묶음**의 id(2026-08-13). '1편째·2편째'는 이 묶음 안에서 센다.
   *
   * 배치는 묶음이 아니다 — 새 글 작성에서 건 예약은 돌고 있는 배치에 계속 붙는다.
   * 옛 작업에는 없다(그때는 화면이 소재로 묶었다).
   */
  seriesId?: string;
  /** 이 작업이 만든 글. 미리보기는 이 값이 있어야 열린다. */
  postId?: string;
  status: ScheduledJobStatus;
  stage: ScheduledJobStage;
  /**
   * 이 글을 실제로 게시할 절대 시각(UTC ISO). 절대 시각 예약에만 있다.
   * 화면 표시는 이 값을 브라우저 로컬 시간으로 옮겨서 한다 — 서버는 변환하지 않는다.
   */
  publishAt?: string;
  /**
   * 이 작업보다 **먼저 끝나야 하는** 작업의 id. 한 소재로 여러 편을 만들 때 2편·3편이
   * 1편을 가리킨다. 원고는 함께 만들지만 **발행은 순서대로** 하기 때문이다.
   */
  afterJobId?: string;
  /** 사용자가 시각을 고를 때 쓰던 시간대(IANA). 표시·감사용이다. */
  timezone?: string;
  /** 마지막으로 발행을 **시도한** 시각. 성공 시각(publishedAt)과 다르다. */
  lastAttemptAt?: string;
  /** 자동 발행 재시도 횟수. 사용자가 누른 재시도(retryCount)와 별개다. */
  publishAttempts?: number;
  /** 작업이 시작 가능해진 시각(표시용). 발행 시각이 아니다 — 그것은 publishAt이다. */
  scheduledAt?: string;
  startedAt?: string;
  generatedAt?: string;
  publishedAt?: string;
  postUrl?: string;
  errorCode?: string;
  errorMessage?: string;
  retryCount: number;
  createdAt: string;
  updatedAt: string;
}

/**
 * 발행 시점을 정하는 방식.
 * - interval: 앞 글이 발행된 뒤 간격만큼 지나면 다음 글(옛 방식·옛 배치의 기본값)
 * - absolute: 글마다 정해 둔 절대 시각에 발행
 */
export type ScheduleMode = "interval" | "absolute";

export interface ScheduledBatch {
  batchId: string;
  userId: string;
  platform: SchedulePlatform;
  topicMode: ScheduleTopicMode;
  /** 옛 배치에는 없다 — 그때는 간격 방식이다. */
  scheduleMode?: ScheduleMode;
  /** 사용자가 시각을 고른 시간대(IANA). */
  timezone?: string;
  status: ScheduledBatchStatus;
  /** 배치의 기본 게시 대상. publishNaver는 옛 배치에 없고, 없으면 true다. */
  publishNaver?: boolean;
  publishThreads?: boolean;
  /**
   * 이 배치의 글에 활용할 브랜드(2026-08-19). 옛 배치에는 없다.
   *
   * 실제로 글을 만들 때 읽는 값은 작업의 `brandId`이고, 여기 있는 것은 화면이 돌고
   * 있는 배치를 다시 그릴 때 무엇으로 걸었는지 보여 주기 위한 것이다.
   */
  brandId?: string;
  targetCount: number;
  intervalSeconds: number;
  totalCount: number;
  completedCount: number;
  failedCount: number;
  canceledCount: number;
  currentJobId?: string;
  nextRunAt?: string;
  pauseRequested: boolean;
  stopRequested: boolean;
  clientRequestId?: string;
  createdAt: string;
  startedAt?: string;
  pausedAt?: string;
  completedAt?: string;
  updatedAt: string;
  logs: ScheduledLogEntry[];
}

export interface ScheduledBatchView {
  batch: ScheduledBatch;
  jobs: ScheduledJob[];
}

/** 예약 목록의 한 줄 — 배치를 넘나드는 '내 예약 전부'. */
export interface ScheduledJobListItem {
  job: ScheduledJob;
  /** 원고가 있으면 그 제목. 없으면 화면이 소재를 대신 쓴다. */
  title?: string;
  batchStatus?: ScheduledBatchStatus;
  /**
   * 이 작업이 만든 글의 **지금 상태**. 글이 지워졌으면 없다.
   *
   * `job.status`와 **다를 수 있고, 다른 것이 정상이다.** 작업의 상태는 그 실행이
   * 끝났을 때의 마지막 기억이고, 같은 글이 그 뒤에 다른 경로로 완성되거나 발행될 수
   * 있다(2026-08-06 신고 — "발행내역에서는 실패인데 내 글 목록에는 글이 완성되어 있다").
   */
  postStatus?: BlogTaskStatus;
  /** 그 글이 **실제로 올라가 있으면** 그 주소. 작업이 실패로 남아 있어도 값이 있을 수 있다. */
  publishedUrl?: string;
  /** 오래 걸리는 단계가 지금 어느 칸인지(예: 4/4 사실 검수·문장 다듬기). */
  progress?: TaskProgress;
  /**
   * 이 글의 '작업 현황' 줄들. 새 글 작성 화면이 보여 주는 것과 **같은 목록**이다
   * (2026-08-10 사용자 요청). 서버 프로세스 메모리에서 오므로 재시작하면 빈다.
   */
  activityLog?: ActivityEntry[];
}

export interface ScheduledJobList {
  items: ScheduledJobListItem[];
}
