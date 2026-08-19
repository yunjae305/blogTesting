"""트렌드 키워드·주제 후보 모델."""

from enum import StrEnum

from .base import CamelModel


class TrendSource(StrEnum):
    # 사용하는 트렌드 소스는 넷: 구글 트렌드·유튜브·네이버·인스타그램. (카카오는 쓰지 않기로 결정.)
    GOOGLE_TRENDS = "GOOGLE_TRENDS"
    YOUTUBE = "YOUTUBE"
    NAVER_DATALAB = "NAVER_DATALAB"
    INSTAGRAM = "INSTAGRAM"
    # 외부 API가 아니라 LLM이 소재에서 파생한 검색 의도형 키워드(소재 관련순 전용 폴백).
    # 별도 값으로 두는 이유는 정직함이다: 네이버·유튜브가 실제로 관측한 키워드와 모델이
    # 만들어 낸 키워드는 근거의 무게가 다르므로, 화면에 '소재 확장'으로 따로 표시한다.
    # 출처를 네이버·유튜브로 위장하면 사용자가 검증된 수요로 오인한다.
    RELATED_EXPANSION = "RELATED_EXPANSION"


class RelationType(StrEnum):
    """키워드가 소재와 맺는 관계의 종류. 점수와 별개로 판단하는 축이다.

    점수 하나로는 "50점"이 '약하게 관련'인지 '억지로 갖다 붙임'인지 구분되지 않는다.
    관계 유형은 그 구분을 명시적으로 만들고, 소재 관련순의 **노출 게이트**로 쓴다:
    앞의 셋만 통과하고 뒤의 셋은 점수와 무관하게 제외한다.

    - DIRECT: 소재 자체이거나 소재의 구성 요소·기능·버전.
    - ADJACENT: 소재를 쓰는 사람이 자연히 함께 찾는 인접 주제.
    - CONTEXTUAL: 소재를 둘러싼 상황·계절·사건으로 이어지는 주제.
    - FORCED: 억지로 갖다 붙여야만 이어지는 주제.
    - NONE: 아무 상관 없음.
    - AMBIGUOUS: 동음이의 등으로 어느 쪽인지 확정할 수 없음.
    """

    DIRECT = "DIRECT"
    ADJACENT = "ADJACENT"
    CONTEXTUAL = "CONTEXTUAL"
    FORCED = "FORCED"
    NONE = "NONE"
    AMBIGUOUS = "AMBIGUOUS"


# 소재 관련순에 노출할 수 있는 관계 유형과, 각 유형에 요구하는 소재 관련도 하한.
#
# **관계 유형이 게이트이고, 점수는 어긋남을 잡는 안전장치다.** 의미 판단은 이미 유형이
# 했다(FORCED·NONE·AMBIGUOUS는 여기 없으므로 점수와 무관하게 제외된다). 점수 하한은
# "DIRECT라고 해 놓고 소재 점수가 바닥"인 자기모순만 걸러 내는 용도이지, 관련도를 한 번 더
# 재는 자리가 아니다.
#
# 하한을 낮춘 이유(2026-07-27) — 실제 채점 분포와 어긋나 있었다. 저장된 6개 소재 풀 640건을
# 세어 보니 ADJACENT 100건의 점수가 35~82에 걸쳐 있고 최빈값이 45다. 옛 하한 55는 관계가
# 있다고 판정된 인접 후보의 **절반 가까이**를 잘랐다. 프롬프트 루브릭(SCORING_GUIDE)도
# 30~54를 "연결 고리가 약함"으로 둘 뿐 "무관"은 30 미만이라고 정의한다 — 30이 유일한
# 의미 경계이고 55·45는 근거 없는 숫자였다.
#
# 니치 소재에서 특히 파괴적이었다. 소재 'AIONA'는 DIRECT가 2건뿐이고(그마저 소재 메아리
# 필터에 걸린다) 나머지 관련 후보 31건이 전부 ADJACENT 35~55에 몰려 있어, 게이트 통과가
# 1건이었다 — 화면에 카드 한 장만 뜨고 '트렌드 새로 수집'을 눌러도 새 후보가 전부 같은
# 구간에 떨어져 영원히 그 한 장이었다. 완화 후 같은 풀에서 31건이 통과한다.
#
# 넓은 소재는 거의 그대로다(배틀그라운드 37→39, 빵 45→49, 델타포스 47→50) — 이 완화는
# 후보가 말라붙는 쪽만 살린다. 예전 60/50/40 AND 게이트를 폐기했을 때(2026-07-22)와 같은
# 실패를 유형별 하한이라는 다른 얼굴로 되풀이하고 있었다.
MATERIAL_RELATION_MIN_SUBJECT: dict[RelationType, float] = {
    RelationType.DIRECT: 70.0,
    RelationType.ADJACENT: 40.0,
    RelationType.CONTEXTUAL: 30.0,
}


class TrendMode(StrEnum):
    """트렌드 키워드를 제공하는 두 보기 방식. 수집 풀은 공유하지만 관련도 채점 여부와
    랭킹·필터·노출 이력이 갈려 서로 다른 후보 풀을 만든다.

    - TRENDING(최신순): 소재와 무관하게 DB에 저장된 공용 풀(trend_keywords)을 인기순으로
      보여준다. 소재 관련도 모델을 호출하지 않는다 — 같은 시점이면 소재가 무엇이든 결과가
      같다. 소스 API를 부르는 것은 저장분이 아예 없을 때(첫 실행)와 '수집하기'뿐이며, 소재를
      되풀이한 메아리·기계적 조합만 제외한다.
    - MATERIAL_RELATED(소재 관련순): LLM 소재 관련도 30 이상인 키워드만 관련도 내림차순으로
      보여준다. 저장된 풀에 적격 후보가 없을 때만 소스 API를 새로 수집해 DB에 upsert하고 같은
      검증을 거친다. 무관 키워드로 채우지 않는다 — 적격 후보가 적으면 그만큼만(없으면 빈 패널).
      소재명 반복·재배열, 기계적 조합(빵추천 등)도 제외한다.
    """

    TRENDING = "TRENDING"
    MATERIAL_RELATED = "MATERIAL_RELATED"


class TrendEvidenceOrigin(StrEnum):
    """근거 데이터가 실제로 어느 경로에서 왔는지. 화면이 경로별로 다른 문구를 쓴다.

    SERPAPI·GOOGLE_RSS는 **이미 저장된 문서를 읽기 위해서만** 남아 있다. 구글 수집은
    2026-08-07부터 트렌드 페이지 크롤링(GOOGLE_TRENDS_WEB) 하나뿐이다 — 두 값을 지우면
    그 시절에 저장된 근거가 역직렬화에 걸려 통째로 사라진다.
    """

    GOOGLE_TRENDS_WEB = "GOOGLE_TRENDS_WEB"
    SERPAPI = "SERPAPI"
    GOOGLE_RSS = "GOOGLE_RSS"
    YOUTUBE_API = "YOUTUBE_API"
    NAVER_SEARCH_API = "NAVER_SEARCH_API"


class GoogleTrendEvidence(CamelModel):
    """구글 트렌드 응답에서 실제로 받은 값만 담는다. 없는 값은 만들지 않는다 —
    RSS 폴백에는 상승률·시작 시각이 없으므로 그 필드는 None으로 남는다."""

    # SerpApi trending_now의 급상승 여부. RSS에는 없다.
    active: bool | None = None
    # SerpApi search_volume(검색 규모). RSS에는 없다.
    search_volume: float | None = None
    # SerpApi increase_percentage(상승률 %).
    increase_percentage: float | None = None
    # SerpApi start_timestamp를 UTC ISO로 변환한 상승 시작 시각.
    started_at: str | None = None
    # 공식 RSS의 <ht:approx_traffic>("5,000+" → 5000). SerpApi 경로에는 없다.
    approximate_traffic: float | None = None
    feed_type: TrendEvidenceOrigin | None = None


class NaverEvidenceBasis(StrEnum):
    """네이버 수치가 **무엇을 잰 것인지**. 두 경로가 서로 다른 것을 재므로 반드시 함께 저장한다.

    - SEARCH_API_SAMPLE: 계절·소재 질의로 모아 온 이번 수집 표본에서, 그 키워드가 실제로
      등장한 고유 문서 수. 표본 크기(sampled_document_count)에 갇힌 값이다.
    - SEARCH_API_TOTAL: 그 키워드 자체를 질의로 넣어 받은 **검색 결과 총수**(응답의 total).
      표본이 아니라 네이버가 세어 준 값이라 키워드끼리 비교가 된다.

    화면 문구가 이 값에 따라 갈린다 — 둘을 같은 문장으로 적으면 어느 쪽도 사실이 아니게 된다.
    """

    SEARCH_API_SAMPLE = "SEARCH_API_SAMPLE"
    SEARCH_API_TOTAL = "SEARCH_API_TOTAL"


class NaverTrendEvidence(CamelModel):
    """네이버 검색 API가 이 키워드에 대해 말해 주는 것. 기준은 basis가 밝힌다."""

    # --- SEARCH_API_SAMPLE(발굴 경로): 이번 수집 표본에서 확인된 고유 문서 수 ---
    # kind=news이고 pubDate가 관측 시각 기준 최근 24시간 이내인 고유 문서 수.
    recent_news_count: int | None = None
    # kind=blog 중 키워드가 등장한 고유 문서 수(블로그 postdate는 날짜 단위라 24시간 필터를 걸지 않는다).
    collected_blog_count: int | None = None
    # blog·news·cafearticle·kin을 합쳐 키워드가 등장한 고유 문서 수(링크 기준 중복 제거).
    collected_related_content_count: int | None = None
    # 이번 수집이 읽은 전체 표본 문서 수(키워드 무관). 표본 크기를 정직하게 밝히기 위한 값.
    sampled_document_count: int | None = None

    # --- SEARCH_API_TOTAL(보강 경로): 키워드 자체를 검색해 받은 결과 총수 ---
    # 네이버가 세어 준 값이라 표본 상한에 갇히지 않는다.
    total_news_count: int | None = None
    total_blog_count: int | None = None
    # 최근 24시간 안에 올라온 새 글 수. 날짜순 응답에서 세므로 표본 상한에 걸릴 수 있고,
    # 그때는 recent_hit_cap이 True다("50건+"로 표기해야 한다는 뜻).
    recent_document_count: int | None = None
    recent_hit_cap: bool | None = None

    basis: NaverEvidenceBasis | str | None = None


class YouTubeTrendEvidence(CamelModel):
    """수집한 영상 묶음에서 계산한 조회 지표. 키워드마다 추가 API를 부르지 않는다."""

    top_video_id: str | None = None
    top_video_title: str | None = None
    top_view_count: int | None = None
    top_video_published_at: str | None = None
    # 누적 조회수 ÷ 게시 후 경과시간. 실시간 조회 속도가 아니므로 화면은 반드시
    # '업로드 후 시간당 평균'이라고 표현한다.
    average_views_per_hour: float | None = None
    # 최근 recent_window_days일 이내 게시된, 키워드가 확인된 고유 영상 수(소재 관련순).
    recent_video_count: int | None = None
    recent_window_days: int | None = None


class TrendSourceEvidence(CamelModel):
    """출처 하나가 이 키워드에 대해 관측한 근거. 출처마다 척도가 다르므로 절대 서로
    더하거나 합성하지 않고, 출처별로 따로 보관해 화면이 대표 출처 것만 보여준다."""

    source: TrendSource
    # 실제 출처 데이터를 관측(API 응답 처리)한 시각. 추천 응답을 만든 generatedAt과 다르다.
    observed_at: str | None = None
    data_origin: TrendEvidenceOrigin | None = None
    google: GoogleTrendEvidence | None = None
    naver: NaverTrendEvidence | None = None
    youtube: YouTubeTrendEvidence | None = None


class TrendKeyword(CamelModel):
    trend_keyword_id: str
    keyword: str
    normalized_keyword: str | None = None
    tokens: list[str] | None = None
    token_set_signature: str | None = None
    cluster_id: str | None = None
    source: TrendSource
    sources: list[TrendSource] | None = None
    rank: int
    score: float
    trend_score: float | None = None
    # 정규화된 실시간 상승도(점수식의 hotness 항, 0-100). 클라이언트의 "최신순" 정렬이
    # 합성 점수 대신 이 값을 쓴다. 채점 전이거나 노출하지 않으면 None.
    hotness: float | None = None
    quality_score: float | None = None
    final_score: float | None = None
    trend_reason: str | None = None
    connection_idea: str | None = None
    period: str | None = None
    # 0-100: 이 키워드가 사용자의 주제와 얼마나 자연스럽게 연결되는지, M2 모델이 판단한
    # 값. 채점 호출이 실패하면 None이며, 이때 클라이언트는 키워드의 인기도 순위로
    # 대체한다.
    relevance: float | None = None
    # 관련도 판정의 축별 부분 점수(각 0-100). subjectRelevance=소재 직접 관련(소재 관련순의
    # 게이트·정렬 축), purposeRelevance=글 목적 부합, personaRelevance=페르소나(화자)가
    # 자연스럽게 다룰 수 있는지 — 뒤의 둘은 툴팁 표기용. 채점 전·구버전 캐시에는 None.
    subject_relevance: float | None = None
    purpose_relevance: float | None = None
    persona_relevance: float | None = None
    # 소재 관련순의 노출 조건을 통과했는가(관계 유형 게이트 + 유형별 소재 점수 하한).
    # 판정 전이면 None.
    is_eligible: bool | None = None
    # 소재와 맺는 관계의 종류. 점수와 별개의 게이트이며, 화면 툴팁에서 "왜 이 키워드가
    # 관련 있다고 판단했는지"를 설명하는 근거로도 쓴다. 판정 전이면 None.
    relation_type: RelationType | None = None
    # 키워드의 분야(스포츠·대회, 뷰티·패션·쇼핑, …), 같은 M2 판단에서 나온다.
    # 최종 4개를 여러 카테고리에 고루 분산하는 데 쓴다. 채점을 건너뛰었거나 모델이
    # 쓸 만한 카테고리를 주지 못하면 None.
    category: str | None = None
    # 출처별 실제 수집 근거. 키는 TrendSource 값(GOOGLE_TRENDS 등)이다. 카드의 3줄
    # 지표는 대표 출처(source)의 근거만 보여주고, 보조 출처 근거도 여기 함께 남는다.
    # 근거가 생기기 전에 저장된 문서·캐시에는 없으므로 None — 그때 화면은 지표 대신
    # 중립 문구를 쓴다(없는 수치를 지어내지 않는다).
    evidence_by_source: dict[str, TrendSourceEvidence] | None = None
    collected_at: str


class TitleHookType(StrEnum):
    """제목에 얹는 후킹(주목 유도)의 종류. 별도 제목 체계가 아니라, 검색 친화 기본 제목
    위에 필요한 경우에만 더하는 각도다.

    NONE은 후킹 없는 순수 정보형(가장 안전). 나머지 여덟은 글의 실제 내용이 뒷받침될 때만
    쓴다 — 특히 AUTHORITY·STORY·REVERSAL은 실제 근거(공식자료·경험·반대 결과)가 있어야
    하는데, 제목은 본문보다 먼저 M2에서 만들어지므로 참고자료로만 근거를 확인할 수 있다.
    근거가 없으면 이 셋을 쓰지 않고 CURIOSITY·COMPARISON·LOSS_AVERSION·기본형으로 내린다.
    """

    NONE = "NONE"
    CURIOSITY = "CURIOSITY"  # 결과·이유·핵심 포인트를 알고 싶게
    LOSS_AVERSION = "LOSS_AVERSION"  # 실수·손실·실패를 피하고 싶게
    FOMO = "FOMO"  # 지금 주목받는 흐름(트렌드 근거 필요)
    AUTHORITY = "AUTHORITY"  # 연구·데이터·전문가(출처 필요)
    REVERSAL = "REVERSAL"  # 상식과 다른 실제 결과(반대 근거 필요)
    COMPARISON = "COMPARISON"  # 두 선택지·전후 차이(비교 대상 필요)
    IDENTITY = "IDENTITY"  # 독자가 자기 유형·상황을 확인하고 싶게
    STORY = "STORY"  # 실제 경험·변화 과정(경험 근거 필요)


class TitleHookStrength(StrEnum):
    """후킹의 세기. 기본값은 MEDIUM이고, HIGH는 본문 근거가 충분할 때만 허용한다.

    - LOW: 정보 전달이 중심, 약한 궁금증만. 검색형 블로그에 가장 안전.
    - MEDIUM: 차이·이유·실수·결과를 구체적으로 강조. 정보성과 주목도의 균형.
    - HIGH: 반전·강한 손실 회피·예상 밖 결과. 참고자료에 실제 근거가 있을 때만.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TopicCandidate(CamelModel):
    topic_candidate_id: str
    title: str
    description: str
    trend_keyword_ids: list[str]
    recommended: bool
    # 루브릭 총점(0-100). 추천은 이 점수가 가장 높은 제목이다 — index==0 같은 임의 기준이 아니라.
    # 채점 전이면 None.
    score: float | None = None
    # 왜 이 제목이 추천/이 점수인지 한 줄 근거. 사용자에게 표시한다(상세 점수는 감춰도 됨).
    reason: str | None = None
    # 이 제목이 쓴 후킹 유형·강도. 화면에는 표시하지 않고(디자인 변경 아님), 생성·채점의
    # 내부 정보로만 쓴다. 근거 없는 후킹을 걸러내고, 선택된 제목이 어떤 약속(비교·이유·
    # 경험 등)을 했는지 원고 단계(M4)에 넘겨 본문이 그 약속을 지키게 하기 위한 것이다.
    # 예전 후보(후킹 도입 전)에는 없으므로 None.
    hook_type: TitleHookType | None = None
    hook_strength: TitleHookStrength | None = None


class TrendRecommendationResult(CamelModel):
    post_id: str
    # 어떤 목적으로 모은 키워드인지. 프론트는 [추천어]/[소재 관련어] 탭이 요청한 모드를 그대로 받는다.
    mode: TrendMode = TrendMode.TRENDING
    trend_keywords: list[TrendKeyword]
    topic_candidates: list[TopicCandidate]
    generated_at: str
    cache_status: str | None = None
    refreshing: bool = False
    # 소재 관련순 결과의 출처: "database"(저장된 풀) / "external_api"(적격 후보가 없어
    # 새로 수집). 최신순에서는 None.
    source: str | None = None
    # 소재 관련순 커서 로테이션. 최신순에서는 전부 None이다.
    next_cursor: str | None = None
    pool_size: int | None = None
    has_more: bool | None = None
    cycled: bool | None = None


class TrendTopicResult(CamelModel):
    """사용자가 고른 하나의 키워드에 대한 제목들. 키워드를 다시 수집하지 않고도 제목
    목록을 재생성할 수 있도록 따로 반환한다."""

    post_id: str
    trend_keyword_id: str
    topic_candidates: list[TopicCandidate]
    generated_at: str


class TrendSelection(CamelModel):
    topic_candidate_id: str | None = None
    final_topic: str
    selected_trend_keyword_ids: list[str]
    # 사용자가 고른 트렌드 키워드의 **문자열**. id만 저장하던 시절에는 그 키워드가 M4에
    # 닿지 않았다 — 키워드 목록은 저장되지 않으므로 선택 시점에 함께 담지 않으면 사라진다
    # (hook_type과 같은 이유). 원본 검색어와 '글에 쓸 표현'을 분리하려면 원본이 있어야 한다.
    # 옛 문서·옛 클라이언트에는 없으므로 기본값은 빈 목록이고, 그때는 예전과 같이 동작한다.
    selected_keywords: list[str] = []
    skipped: bool
    selected_at: str
    # 고른 제목이 쓴 후킹 유형. TopicCandidate.hook_type의 주석이 원래 의도한 대로 "선택된
    # 제목이 어떤 약속을 했는지"를 원고 단계(M4)까지 실어 나르기 위한 필드다 — 후보 목록은
    # 저장되지 않으므로 선택 시점에 여기 옮겨 담지 않으면 그 정보가 사라진다.
    # 건너뛴 글, 후킹 도입 전 후보, 옛 문서에서는 None.
    hook_type: TitleHookType | None = None
