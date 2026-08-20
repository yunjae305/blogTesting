"""LLM provider 인터페이스(Protocol). 어댑터는 이 모양만 지키면 된다."""

from typing import Awaitable, Callable, Protocol

from pydantic import Field, model_validator

from app.shared import (
    normalized_subject_fields,
    BlogTaskInput,
    BrandLink,
    BrandUseCase,
    CamelModel,
    CardBrief,
    CardDesignSystem,
    DraftGenerationInput,
    DraftGenerationResult,
    DraftGenerationSettings,
    FinalPost,
    GeneratedPostImage,
    IntentValidationResult,
    RelationType,
    SelectedIntentForDraft,
    ThumbnailLayoutPlan,
    TopicCandidate,
    TrendKeyword,
    TrendMode,
    WebPhoto,
    WebSearchAnalysisInput,
)


class TrendFetchInput(CamelModel):
    post_id: str
    user_id: str
    input: BlogTaskInput
    # 추천어(TRENDING) / 소재 관련어(MATERIAL_RELATED). 같은 수집 풀을 공유하되 랭킹·필터·
    # 노출이력이 갈린다. 기본은 추천어(사용자가 처음 보는 탭).
    mode: TrendMode = TrendMode.TRENDING
    country: str | None = None
    category: str | None = None
    max_keywords: int | None = None
    # 이 사용자에게 이미 보여준 키워드. 새로고침 시 클라이언트가 보내 같은 키워드를
    # 다시 내주지 않는다. 정규화 후 비교하므로 띄어쓰기·대소문자만 다른 변형도 제외된다.
    exclude_keywords: list[str] = Field(default_factory=list)
    # 수집하기 버튼: 캐시가 신선해도 소스를 다시 호출해 새 키워드를 수집하고, 기존
    # 풀에 합쳐 저장한다. 새로고침(수집 없이 풀 안에서 회전)과 반대되는 요청이다.
    force_collect: bool = False
    # 최신순 '다른 후보 보기': 노출 이력·exclude를 무시하고 저장된 풀 전체에서 무작위
    # 표본을 뽑는다(중복 노출 허용). 이력 제외 방식은 풀을 다 돌면 후보가 말라붙기
    # 때문. TRENDING에서만 의미가 있고, 소재 관련순은 결정적 순서를 유지한다.
    shuffle: bool = False
    # 사용자 설정의 페르소나(프롬프트 텍스트로 푼 것). 소재 관련순의 AND 관련성 판정
    # (소재 AND 목적 AND 페르소나)에 쓴다. 검색 API의 질의로는 쓰지 않는다 — 페르소나는
    # 화자·문체 설정이라 검색어가 아니고, 의미 판단(LLM)에서만 쓸모가 있다.
    persona: str | None = None
    # 소재 관련순 '다른 후보 보기'의 위치. 서버가 만든 불투명 값이며, 클라이언트는
    # 받은 nextCursor를 그대로 돌려보내기만 한다.
    cursor: str | None = None
    # 소재 관련순 보충 수집(서버 내부 전용, 클라이언트가 보내지 않는다). 첫 수집으로
    # 화면을 채우지 못했을 때 발굴 질의를 소재 중심으로 한 번 더 넓힌다 — 질의만 넓힐 뿐
    # 후보의 자격 기준은 그대로다. 캐시 키에도 들어가 첫 수집 결과를 되받지 않게 한다.
    widen_material: bool = False
    # 문맥 한정 모드의 문맥 키(§13). 최초 조회에서 서버가 만들어 응답에 실어 주고,
    # 클라이언트는 '다른 후보 보기'·'수집하기'에 그대로 돌려보낸다. 그래야 매 요청이



class TrendFetchResult(CamelModel):
    trend_keywords: list[TrendKeyword]
    collected_at: str
    mode: TrendMode = TrendMode.TRENDING
    cache_status: str | None = None
    refreshing: bool = False
    # 이 결과가 어디서 왔는지: "database"(저장된 풀에서 바로) 또는 "external_api"
    # (저장분에 적격 후보가 없어 소스를 새로 수집). 소재 관련순에서만 채워진다.
    source: str | None = None
    # --- 소재 관련순 커서 로테이션 ---
    # 다음 배치를 받을 위치. 끝까지 갔다가 순환한 경우에도 값이 있다(빈 화면 대신 순환).
    next_cursor: str | None = None
    # 게이트를 통과한 소재 관련 후보의 총 개수. 화면이 "몇 개 중 몇 개를 보고 있는지"를
    # 정직하게 말할 수 있게 한다.
    pool_size: int | None = None
    # 아직 보지 않은 후보가 남았는가.
    has_more: bool | None = None
    # 이번 배치가 풀을 한 바퀴 돌아 처음으로 되돌아온 것인가. 오류가 아니라 정상 순환이다.
    cycled: bool | None = None
    

class ExcludedAngle(CamelModel):
    """직전 배치가 이미 써 버린 관점 하나.

    제목 문자열 + 그 제목이 쓴 후킹 유형·기본 유형이다. 시작 패턴과 핵심 명사는 제목에서
    코드가 뽑으므로 클라이언트가 보내지 않는다.
    """

    title: str
    hook_type: str | None = None
    title_type: str | None = None


class TopicGenerationInput(CamelModel):
    """제목은 사용자가 고른 키워드 하나만을 위해 쓴다.

    예전 provider는 키워드마다 제목을 만들어, 패널 아래 목록이 실제로 선택한
    카드와 무관했다.
    """

    post_id: str
    input: BlogTaskInput
    trend_keyword: TrendKeyword
    # 사용자가 설정에서 지정한 페르소나. 제목은 이 화자의 말투로 쓰므로 사실상
    # 필수이고, 아직 저장된 것이 없을 때만 비어 있다.
    settings: DraftGenerationSettings | None = None
    # 이미 화면에 있는 제목. 제목 추천을 다시 눌러도 같은 다섯 개를 내주면 안 되므로
    # 이미 쓴 것을 모델에 알려준다.
    exclude_titles: list[str] = Field(default_factory=list)
    # 직전 후보가 **어떤 관점을 썼는지**. 제목 문자열만 넘기면 모델은 같은 후킹·같은 유형으로
    # 표현만 바꿔 온다(재생성은 문장 교체가 아니라 관점 이동이어야 한다). 옛 클라이언트는
    # 보내지 않으므로 비어 있을 수 있고, 그때는 예전과 같은 프롬프트가 나간다.
    exclude_angles: list["ExcludedAngle"] = Field(default_factory=list)
    # '제목 추천 다시'를 몇 번째 누른 것인지. 0이면 첫 생성이다. 이 값으로 코드가 이번 회차의
    # 방향을 결정적으로 고른다(난수를 쓰지 않는다 — title_variation 참고).
    regeneration_count: int = 0


class TopicRecommendationResult(CamelModel):
    topic_candidates: list[TopicCandidate]
    generated_at: str


class PostImageGenerationInput(CamelModel):
    post_id: str
    user_id: str
    input: BlogTaskInput
    selected_intent: SelectedIntentForDraft
    final_post: FinalPost
    prompt_version: str
    image_index: int
    total_images: int
    # 본문의 [[IMAGE: ...]] 태그에서 뽑아낸, 그 이미지에 한정된 영어 장면 묘사.
    content_prompt: str | None = None
    # 태그의 alt= 필드에서 뽑은 한국어 대체 텍스트. 장면 묘사는 이미지 모델용 영어라,
    # 독자·검색이 보는 alt는 이쪽을 쓴다. alt 필드 도입 전 원고에는 없다(None).
    content_alt: str | None = None
    # M2에서 고른 트렌드 제목. 이미지가 소재뿐 아니라 이 글이 연결한 트렌드의 맥락도
    # 담게 한다. 트렌드를 건너뛴 글에는 None.
    trend_title: str | None = None
    # 첫 장은 네이버 대표 썸네일이다. 프레이밍 규칙도, 뒤에 얹는 텍스트 레이어도 본문
    # 이미지와 다르다.
    is_thumbnail: bool = False
    # 세 장의 색감과 광원을 묶는 한 줄. 글 하나당 하나를 골라 세 장 모두에 넣는다.
    visual_style: str | None = None
    # 썸네일 문구. 모델에게 그리라고 시키지 않는다 — 자리를 비우라고만 하고, 실제
    # 글자는 생성 후 imaging.render_thumbnail이 올린다.
    thumbnail_copy: list[str] = Field(default_factory=list)
    # 로컬 제목 렌더러가 소재 핵심어에 쓸 색 계열. 이 입력 모델은 생성 중에만 쓰며
    # FinalPost나 DB 문서에는 저장하지 않는다.
    thumbnail_accent_family: str | None = None
    # 카드 브리프(원고 완성 후 카드 계획이 만든 상세 설계)와 글 공통 디자인 시스템.
    # 있으면 §8 카드 배경 템플릿으로 장면만 생성하고, 라벨·한글 제목·아이콘·순번은
    # cards.render_card가 합성한다. 없으면(구형 원고·계획 실패 폴백) 기존 경로.
    card: CardBrief | None = None
    design: CardDesignSystem | None = None
    # 사용자가 올린 참고 이미지(data URL). 있으면 이 이미지를 시각 기준으로 삼아
    # image-to-image(편집)로 장면을 생성한다 — 생성 결과가 참고 이미지를 닮게 한다.
    # 대표 썸네일과 '참고 이미지와 관련 있다'고 표시된 카드에만 실린다. None이면 기존
    # 텍스트→이미지 생성. 여러 장을 올렸으면 카드의 reference_id가 가리키는 장이 실린다.
    reference_image: str | None = None
    # 대표 썸네일의 피사체·문구 배치. 이미지 모델에는 '피사체를 어느 쪽에 두라'만 전달되고,
    # 실제 글자는 생성 뒤 imaging.render_thumbnail이 이 계획대로 얹는다. None이면 예전
    # 동작(중앙 배치).
    thumbnail_layout: ThumbnailLayoutPlan | None = None
    # 이 글의 촬영 언어(카테고리에서 온다). 사진들이 한 글의 것으로 보이게 한다.
    photo_language: str | None = None
    # 확인된 대상(참고자료의 제품명·색상·형태, 또는 소재가 가리키는 캐릭터명·인물명)과
    # 보존해야 할 특징. 생성 이미지가 다른 대상으로 바뀌지 않게 프롬프트에 그대로 실린다.
    subject_identity: str | None = None
    fidelity_requirements: list[str] = Field(default_factory=list)
    # 그 대상이 어떤 종류인가(VISUAL_SUBJECT_KINDS)와, 본인이 반드시 화면에 보여야 하는가.
    # 고유 캐릭터·실제 인물이면 프롬프트가 '주변 소품·문양·배경으로 대신하지 말라'는 강한
    # 지시로 바뀐다. 옛 호출부·옛 카드에는 없으므로 기본값은 예전 동작 그대로다.
    subject_kind: str = "NON_PERSON"
    must_show_subject: bool = False
    identity_confidence: float = 0.0
    # 이 인물이 누구인지 확인시키는 참고 이미지(data URL). 실존 인물은 이름만으로는 얼굴이
    # 재현되지 않으므로, 있으면 image-to-image의 기준으로 함께 보낸다. 첫 장이 편집 기준이
    # 되고 나머지는 프롬프트에 '같은 사람'이라는 근거로 함께 실린다.
    reference_person_images: list[str] = Field(default_factory=list)
    # 1차 생성이 실패해 더 단순한 인물 중심 구도로 다시 시도하는 중인가. 정체성은 그대로
    # 두고 배경·행동만 걷어낸다 — 실패를 일반 인물 사진으로 메우지 않기 위한 재시도다.
    simplified_identity_retry: bool = False
    # 웹에서 찾아온 소재의 실제 사진. 있으면 이미지 모델을 **부르지 않고** 이 사진을 결과로
    # 쓴다(썸네일은 문구만 얹고, 본문은 규격만 맞춘다). reference_person_images와는 쓰임이
    # 정반대다 — 저쪽은 '이 사람을 닮게 그려라'의 근거이고, 이쪽은 그리기를 그만두는 것이다.
    # 실존 인물은 아무리 잘 지시해도 모델이 닮은 남을 그리기 때문이다.
    web_photo: WebPhoto | None = None

    @model_validator(mode="after")
    def _normalize_subject(self) -> "PostImageGenerationInput":
        kind, identity, must_show = normalized_subject_fields(
            self.subject_kind, self.subject_identity, self.must_show_subject
        )
        object.__setattr__(self, "subject_kind", kind)
        object.__setattr__(self, "subject_identity", identity)
        object.__setattr__(self, "must_show_subject", must_show)
        return self
    # 참고 이미지에 실제 브랜드 표식이 있고 그것이 글의 핵심 대상인가. True면 '로고를 새로
    # 그리지 말고 원본의 표식을 보존하라'로 규칙이 바뀐다(가짜 로고 금지는 그대로).
    preserve_brand_marks: bool = False
    # 안전 폴백 전용(2026-08-10): 소재명 자체가 실존 인물·저작권 캐릭터라, 이름을 내려놓은
    # 재시도에서는 프롬프트의 소재 앵커("...about: {topic}")에도 그 이름을 싣지 않는다 —
    # 남기면 그 줄 하나로 또 차단돼 대표 이미지가 통째로 비었다(실측: 이미지 0장 완성).
    # 소재는 생성 뒤 코드가 얹는 한글 제목 문구가 말한다. 기본값 False = 예전 동작 그대로.
    suppress_topic_anchor: bool = False


class PhotoSearch(Protocol):
    """소재의 실제 사진을 웹에서 찾는다. 이미지 **생성**과 별개의 관심사다 — 한쪽은
    없는 그림을 만들고, 이쪽은 있는 사진을 찾는다. 자격 증명이 없으면 None으로 두며,
    그때는 예전처럼 생성만 한다."""

    async def find_photos(self, query: str, limit: int = 1) -> list[WebPhoto]: ...


class TrendProvider(Protocol):
    """키워드를 수집한다. 거기서 제목을 쓰는 일은 별도 provider가 맡는 별개의 관심사다
    — 한쪽은 검색 API, 다른 쪽은 LLM이다."""

    async def fetch_trends(self, input: TrendFetchInput) -> TrendFetchResult: ...


class TopicGenerator(Protocol):
    async def generate_topics(self, input: TopicGenerationInput) -> TopicRecommendationResult: ...


class TitleEvaluationInput(CamelModel):
    """생성된 제목들을 루브릭의 '의미 판단' 항으로 채점하기 위한 입력.

    생성과 평가를 분리한다: 제목을 쓰는 호출과 별개로, 이미 나온 제목들을 한 번의 배치 호출로
    평가한다. 소재 관련성·목적 부합·독자 관심은 규칙으로 판단할 수 없어 모델이 맡는다.
    """

    input: BlogTaskInput
    trend_keyword: TrendKeyword | None = None
    titles: list[str] = Field(default_factory=list)
    # 직전 배치의 제목. 관점이 겹치는 후보를 채점에서 낮게 주기 위한 것이다 — 재생성이 관점을
    # 옮겼는지 판정하는 것은 생성 쪽만의 일이 아니다. 첫 생성에는 비어 있다.
    exclude_titles: list[str] = Field(default_factory=list)


class TitleJudgment(CamelModel):
    """제목 하나에 대한 의미 판단(각 0-100) + 추천 근거 한 줄."""

    relevance: float
    trend_reflection: float
    purpose_match: float
    audience_interest: float
    reason: str | None = None


class TopicEvaluator(Protocol):
    async def evaluate_titles(self, input: TitleEvaluationInput) -> dict[str, TitleJudgment]:
        """title -> 그 제목의 관련성·트렌드 반영·목적 부합·독자 관심 점수와 근거."""
        ...




class KeywordRelevanceInput(CamelModel):
    """수집한 키워드 중 사용자가 쓰려는 소재에 실제로 맞는 것이 무엇인지.

    지금 뜨는 키워드가 곧 추천은 아니다. Google은 지금 온 국민이 검색하는 것을
    돌려주는데, 이 글과는 아무 상관이 없다 — 패널이 AIONA를 쓰는 사람에게 참교육을
    추천했다. 관련도는 의미에 대한 판단이라 모델이 매긴다.
    """

    input: BlogTaskInput
    keywords: list[str]
    # 오늘 날짜(한국 기준). 모델이 현재 계절·시점에 맞는 키워드를 높게 보게 한다.
    # 프롬프트에 박지 않고 데이터로 두어, "지금 뭐가 제철인가"를 하드코딩한 달이 아니라
    # 실제 날짜로 판단하게 한다(§10).
    as_of: str | None = None
    # 사용자 페르소나(화자 설명). 소재 관련순의 페르소나 부합 점수에 쓴다.
    # 없으면 페르소나 항은 판정에서 빠진다(자동 통과).
    persona: str | None = None


class KeywordJudgment(CamelModel):
    """관련도 모델이 키워드 하나에 대해 내린 판단.

    분야(category)는 소재 관련순 카드의 설명 정보로 같은 모델 호출에서 함께 받는다.

    점수별 소비처(2026-07-22 개편 이후):
    - subject_relevance — 소재 관련순의 노출 게이트(하한 미만 = "아무 상관 없음" 제외)
      이자 정렬 축. 예전의 소재 AND 목적 AND 페르소나 게이트는 니치 소재에서 후보를
      말려 죽여 소재 축 하나로 축소했다.
    - purpose_relevance / persona_relevance — 게이트에서는 빠졌지만 툴팁 표기용으로
      계속 채점한다(같은 호출이라 추가 비용 없음).
    - blendability — 이전 관련도 캐시와의 역호환을 위해 남겨 둔 선택 필드. 최신순은
      소재별 채점을 호출하지 않으므로 현재 노출 필터에는 사용하지 않는다.
    - relevance(종합) — 소재 관련순 카드의 종합 관련도 정보.
    구버전 캐시에는 새 축이 없어 None(관련도 캐시 키 v4 승격으로 자연 재채점)."""

    relevance: float
    category: str | None = None
    subject_relevance: float | None = None
    purpose_relevance: float | None = None
    persona_relevance: float | None = None
    blendability: float | None = None
    # 소재와 맺는 관계의 종류. 예전에는 점수 상한을 씌우는 데만 쓰고 버렸는데, 그러면
    # "소재 점수 50"이 '약하게 관련(CONTEXTUAL)'인지 '억지로 갖다 붙임(FORCED)'인지
    # 구분할 수 없어 노출 게이트가 점수 하나에만 의존하게 된다. 소재 관련순은 이 값을
    # 필수 게이트로 쓰므로 판정과 함께 보존하고 DB에도 저장한다.
    relation_type: RelationType | None = None


class KeywordRelevanceRanker(Protocol):
    async def rank_keywords(self, input: KeywordRelevanceInput) -> dict[str, KeywordJudgment]:
        """keyword -> 그 키워드의 관련도(0-100)와 분야."""
        ...


# 검색 절반이 끝나고 요약 절반이 시작될 때 호출된다. M3는 두 모델 호출로 나뉜 약 1분
# 짜리 작업이고, 호출부에서는 그 이음매가 보이지 않는다 — 그래서 analyzer가 그 지점을
# 넘을 때 알려준다.
OnResearchCollected = Callable[[], Awaitable[None]]

# 수집 중에 **사람이 읽을 한 줄**을 호출부로 흘려보내는 통로(2026-08-11 사용자 요청:
# "터미널에서 뜨는 내용들을 보이게 해서 좀 더 자세하게 실시간 작업 현황을 보이게").
# LLM 계층은 화면을 알 필요가 없다 — 문장 하나만 넘기고, 그것을 진행 표시로 만들지는
# 호출부가 정한다. URL·식별자·예외 원문은 여기 싣지 않는다(ActivityEntry의 계약).
OnResearchNote = Callable[[str], Awaitable[None]]


class WebSearchAnalyzer(Protocol):
    async def search_and_analyze(
        self,
        input: WebSearchAnalysisInput,
        on_collected: OnResearchCollected | None = None,
        on_note: "OnResearchNote | None" = None,
    ) -> IntentValidationResult: ...


class DraftGenerator(Protocol):
    async def generate_draft(self, input: DraftGenerationInput) -> DraftGenerationResult: ...

class PostImageGenerator(Protocol):
    async def generate_post_image(self, input: PostImageGenerationInput) -> GeneratedPostImage: ...


# ---------------------------------------------------------------------------
# 브랜드 자료를 **사이트에서 읽어 온다**(2026-08-20 사용자 결정).
#
# 그때까지 브랜드 자료는 사람이 손으로 채우는 것뿐이었다. 그래서 두 가지가 늘 늦었다 —
# 처음 채우는 품이 컸고, 신기능이 나와도 누군가 자료를 고쳐 주기 전에는 글에 나오지
# 않았다. 사이트에는 그 내용이 이미 다 있는데.
#
# **자료를 대신하지는 않는다.** 읽어 온 것은 *제안*이고, 저장은 사람이 확인하고 누른다.
# 사이트가 말하지 않는 기능 이름이 있기 때문이다(aiona.kr 첫 화면은 큰 기능 6개만 말하고,
# 기준표에는 28줄이 있다). 읽어 온 것으로 통째로 덮으면 그 이름들이 조용히 사라지고,
# 모델은 없어진 이름 대신 **지어낸다** — 기준표를 만든 이유가 바로 그것이었다.
# ---------------------------------------------------------------------------


class SiteReadInput(CamelModel):
    """읽을 것. 주소든 붙여넣은 글이든, 또는 둘 다.

    **붙여넣기 통로가 필요한 이유**(2026-08-20): AIONA의 업데이트 공지는
    `winz.aiona.kr/support?tab=announcements`에 올라오는데 그 페이지는 **로그인 뒤에**
    있다. 서버가 열면 로그인 폼만 보인다. 자격 증명을 서버에 심는 것은 별개의 결정이라,
    그때까지는 사용자가 공지 내용을 복사해 붙이면 같은 길로 흘러가게 해 둔다.

    공개 페이지(aiona.kr)는 주소만으로 읽힌다 — 그쪽은 이 통로가 필요 없다.
    """

    brand_name: str
    urls: list[str] = []
    #: 붙여넣은 글. 있으면 이것도 함께 읽는다.
    text: str = ""


class BrandDraft(CamelModel):
    """사이트에서 읽어 낸 브랜드 자료 **제안**. 저장 모양(`validate_brand_body`)과 같다.

    비어 있는 칸은 "사이트에서 못 찾았다"는 뜻이다. 화면은 채워진 칸만 덮어쓴다 —
    못 찾은 것으로 이미 있는 자료를 지우면 안 된다.
    """

    description: str = ""
    features: str = ""
    use_cases: list[BrandUseCase] = []
    links: list[BrandLink] = []
    #: 실제로 읽힌 주소와 못 읽은 주소. 화면이 "무엇을 보고 채웠는지" 보여 줄 근거다.
    read_urls: list[str] = []
    failed_urls: list[str] = []


class FeatureBrief(CamelModel):
    """신기능 한 가지를 소개하는 글의 출발점(2026-08-20).

    신기능 페이지 주소만 주면 그 글의 **소재**가 된다. 기능 이름을 사람이 알고 있어야
    하는 문제가 여기서 없어진다 — 자료가 아직 그 기능을 모르더라도 글은 써진다.
    """

    #: 글의 소재가 될 기능 이름. 사이트에 적힌 그대로다.
    name: str
    #: 무엇이 새로운지 두세 줄.
    summary: str = ""
    #: 이 기능으로 무엇을 할 수 있는지. 본문 뼈대가 된다.
    highlights: list[str] = []
    #: 검색해 들어올 만한 말들. 제목·해시태그의 출발점이다.
    keywords: list[str] = []
    read_urls: list[str] = []


class SiteReader(Protocol):
    """브랜드 사이트를 읽는 쪽. 없으면(자격 증명 없음) 화면이 '지금은 못 쓴다'고 알린다."""

    async def read_brand(self, input: SiteReadInput) -> BrandDraft: ...

    async def read_feature(self, input: SiteReadInput) -> FeatureBrief: ...
