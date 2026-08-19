"""초안 모델.

DraftGenerationInput은 llm_io.py에 있다 — BlogTaskInput에 의존하기 때문에,
여기서 빼두어야 임포트가 순환하지 않는다.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .base import CamelModel
from .image_privacy import PrivateRegion
from .intent import SearchSource


class DraftFormat(StrEnum):
    HTML = "html"
    MARKDOWN = "markdown"


class SelectedIntentForDraft(CamelModel):
    intent_id: str
    title: str
    target_reader: str
    rationale: str
    # M3 후보가 뽑은 검색 키워드. 예전에는 여기서 떨어져 나가 원고 단계가 사용자가 고른
    # 의도의 키워드를 보지 못했다. 옛 호출·테스트 호환을 위해 기본값은 빈 목록이다.
    keywords: list[str] = []
    sources: list[SearchSource] | None = None


# 제목이 취한 검색 전략. 콘텐츠 설계 캐시 키에 들어가므로(설계가 이 전략을 따라야 한다)
# 자유 문자열이 아니라 닫힌 집합으로 둔다 — 모델이 매번 다른 말로 쓰면 같은 제목인데도
# 캐시가 계속 어긋난다.
TITLE_STRATEGIES = (
    "SEARCH_INTENT",  # 검색 질문에 그대로 답하는 정보형
    "PROBLEM_SOLVING",  # 독자의 문제 해결
    "HOW_TO",  # 방법·절차 안내
    "COMPARISON",  # 선택지 비교
    "AUDIENCE",  # 대상 독자 지목
    "QUESTION",  # 질문형
    "TREND_CONNECTION",  # 현재 흐름과 연결(트렌드를 고른 글에만)
)


class TitlePlan(CamelModel):
    """원고보다 **먼저** 확정하는 제목 계획.

    지금까지 제목은 두 갈래로 정해졌다: 트렌드를 고른 글은 M2에서 확정됐고, 건너뛴 글은
    원고 LLM이 본문과 함께 즉석에서 지었다. 뒤쪽은 사용자가 M3에서 고른 의도와 결과가
    어긋날 여지를 남긴다 — 제목이 본문보다 나중에 정해지면 무엇을 약속했는지 아무도 모른다.

    이 계획이 확정되면 원고는 제목을 만들지 않고 `primary_title`을 그대로 쓴다. h1도 코드가
    이 값으로 세우므로(parsing.final_post_from_json의 forced_title) 제목과 H1이 어긋날 수
    없다. 계획을 만들지 못한 글은 None이고, 그러면 예전 동작 그대로 원고가 제목을 짓는다.
    """

    # 이 글의 확정 제목. finalPost.title과 마크다운 H1이 이 값이 된다.
    primary_title: str
    # 채택하지 않은 후보들. 사용자가 나중에 제목을 바꾸고 싶을 때 보여줄 선택지로 남긴다.
    alternative_titles: list[str] = []
    # 마크다운 H1. 규격상 primary_title과 같아야 하며, 코드가 그렇게 강제한다.
    h1: str
    # 이 제목이 노리는 핵심 검색 구문. primary_title 안에 실제로 들어 있어야 한다.
    primary_keyword: str
    # TITLE_STRATEGIES 중 하나.
    title_strategy: str


class SeoKeywordPlan(CamelModel):
    """원고를 쓰기 **전에** 확정하는 SEO 키워드 계획.

    제목 계획(TitlePlan)이 '이 글의 제목·핵심 검색 구문'을 정한다면, 이 계획은 그 위에서
    본문 전체의 검색 키워드 전략을 세운다: 중심 키워드(primary), 이를 보완하는 관련
    키워드(secondary), 그리고 본문에서 피해야 하는 표현(avoid). 원고 프롬프트에 실려
    "제목·첫 문단에 primary를 자연스럽게, secondary는 문맥에 맞게 분산, avoid는 배제"라는
    규칙으로 전달된다.

    primary는 제목이 노리는 핵심 검색 구문과 일치해야 한다 — 그래야 생성 후 검증
    (seo_primary_in_title)이 항상 통과한다. 그래서 title_plan이 있으면 코드가 primary를
    title_plan.primary_keyword에 맞춰 고정한다(parsing.seo_keyword_plan_from_json). 계획을
    만들지 못한 글은 None이고, 그러면 원고 프롬프트와 검증은 예전과 똑같이 동작한다.
    """

    # 글에서 가장 중심이 되는 SEO 키워드. 제목과 첫 문단에 반드시 들어간다.
    primary: str
    # primary를 보완하는 관련 검색어(일반적으로 3~8개). 본문에 자연스럽게 분산한다.
    secondary: list[str] = []
    # 본문에서 사용하지 않아야 하는 표현(동음이의어·다른 카테고리·과장 등).
    avoid: list[str] = []


class IntentAnchor(CamelModel):
    """원고가 끝까지 붙잡아야 할 방향. 사용자가 M3에서 고른 검색 의도와 M2에서 고른 제목의
    후킹 유형을 한 덩어리로 묶은 것이다.

    이 모델은 순수하게 원고 프롬프트에 실어 보내기 위한 것이다 — 저장하지도, 캐시 키에
    넣지도, 검증에 쓰지도 않는다. 흩어져 있던 값(선택 의도 제목·의도 키워드·제목 후킹)을
    한 곳에 모아 "글의 방향"으로 한 번에 전달하는 역할만 한다.
    """

    # 사용자가 M3에서 고른 검색 의도(SelectedIntent.title). 글의 각도가 여기서 벗어나면 안 된다.
    intent: str
    keywords: list[str] = []
    # 트렌드 제목을 골랐을 때만 있다(TrendSelection.hook_type). 건너뛴 글에는 None.
    hook_type: str | None = None


class DraftGenerationSettings(CamelModel):
    hashtag_count: int
    # 원고 목표 분량(short/medium/long). 프롬프트가 목표 글자수로 옮긴다.
    article_length: str = "medium"
    # 소재 vs 트렌드 키워드 결합 방향(subject/balanced/trend). 제목 프롬프트가 사용한다.
    blend_mode: str = "trend"
    default_persona: str | None = None
    # 저장된 페르소나 선택값(프리셋 id 또는 "custom"). default_persona는 그 id를 해석한
    # **프롬프트 전문**이라, 어느 프리셋이었는지 알 수 없다. 표현 강도 표를 이름으로 뒤지던
    # 예전 방식은 커스텀 이름에 프리셋 이름이 들어 있으면 그 프리셋 규칙을 잘못 적용했다.
    # 옛 호출·옛 문서에는 없으므로 None일 수 있고, 그때는 표현 강도 줄이 빠진다(예전 동작).
    default_persona_id: str | None = None
    custom_persona_name: str | None = None
    custom_persona_description: str | None = None
    custom_persona: str | None = None


# 사진의 출처 유형. 공식 영상 썸네일은 '가져온 사실 그 자체'라, 생성 이미지보다 먼저이고
# 위에 제목 박스를 다시 얹지 않는다(원본에 이미 문구·인물이 들어 있다).
WEB_PHOTO_SOURCE_WEB_IMAGE = "WEB_IMAGE"
WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL = "YOUTUBE_THUMBNAIL"
WEB_PHOTO_SOURCE_TYPES = (
    WEB_PHOTO_SOURCE_WEB_IMAGE,
    WEB_PHOTO_SOURCE_YOUTUBE_THUMBNAIL,
)


# --- 이미지 출처 메타데이터(2026-08-11) ---
#
# 왜 필요한가. 지금까지 출처는 캡션 **문자열 하나**("출처: imgnews.pstatic.net")로만 남았다.
# 문자열이 되는 순간 원문 페이지가 어디인지·이용 조건을 확인할 수 있는지가 사라지고, 화면은
# 그 한 줄을 자르는 것 말고 할 수 있는 일이 없다. 그래서 출처를 **값으로** 들고 다닌다.
#
# 판정할 수 없는 것을 판정하지 않는다는 원칙은 여기서도 같다: 원문 페이지를 확실히 알 수
# 없으면 source_page_url은 None이고, 라이선스를 확인하지 못하면 usage_status는 UNKNOWN이다.
# 비어 있는 것을 '사용 가능'으로 채우지 않는다.
IMAGE_SOURCE_TYPE_EXTERNAL = "external"
IMAGE_SOURCE_TYPE_GENERATED = "generated"
IMAGE_SOURCE_TYPES = (IMAGE_SOURCE_TYPE_EXTERNAL, IMAGE_SOURCE_TYPE_GENERATED)

# 이용 가능 여부. ALLOWED는 **라이선스를 실제로 확인했을 때만** 쓴다.
IMAGE_USAGE_ALLOWED = "allowed"
IMAGE_USAGE_RESTRICTED = "restricted"
IMAGE_USAGE_UNKNOWN = "unknown"
IMAGE_USAGE_STATUSES = (
    IMAGE_USAGE_ALLOWED,
    IMAGE_USAGE_RESTRICTED,
    IMAGE_USAGE_UNKNOWN,
)


class ImageSourceInfo(CamelModel):
    """이미지 한 장의 출처. 화면에 뿌릴 문자열이 아니라 구조화된 사실이다.

    ``imageUrl``에 해당하는 값은 여기 두지 않는다 — 화면에 실제로 쓰는 주소는
    ``GeneratedPostImage.data_url``이고, 밖에서도 보이는 주소는 ``original_image_url``이다.
    같은 값을 세 번 들고 있으면(그것도 base64 데이터 URL로) 문서만 무거워진다.
    """

    # external=밖에서 가져온 이미지, generated=이미지 모델이 그린 것. 둘을 같은 값으로
    # 다루지 않는다 — 생성 이미지에 웹사이트 출처를 붙이면 없는 출처를 지어내는 셈이다.
    source_type: str = IMAGE_SOURCE_TYPE_GENERATED
    # 실제로 이미지가 게시된 사이트·채널 이름. 검색 서비스 이름(네이버·구글)을 쓰지 않는다.
    source_name: str = ""
    # 이미지가 실려 있던 원문 페이지. **확인된 경우에만** 채운다.
    source_page_url: str | None = None
    # 원본 이미지 주소(밖에서도 열리는 것). 확인할 수 없으면 None.
    original_image_url: str | None = None
    # 라이선스 표기와 확인 페이지. 출처가 명시하지 않으면 둘 다 None이다.
    license: str | None = None
    license_url: str | None = None
    usage_status: str = IMAGE_USAGE_UNKNOWN


class WebPhoto(CamelModel):
    """웹에서 찾아온 소재의 실제 사진. 생성 입력이 아니라 결과 사진 그 자체로 쓴다.

    이미지 모델은 실존 인물의 얼굴을 재현하지 못하므로, 소재가 특정 인물이면 그리는 대신
    가져온다. DB에 이 모델 자체를 저장하지는 않는다 — 생성 중에만 오가고, 남는 것은
    GeneratedPostImage의 사진과 출처 캡션이다.
    """

    data_url: str
    # 이미지 URL. 네이버 이미지 검색은 이미지가 실린 페이지 URL을 주지 않는다.
    source_url: str
    source_host: str
    title: str = ""
    width: int = 0
    height: int = 0
    # 이 사진을 찾을 때 쓴 질의. 인물명으로 못 찾아 소재로 넓혔는지 로그·캡션에서 구분한다.
    query: str = ""
    # 발행 규격(크기 하한)을 통과했는가. False면 원고에 직접 싣지 않고 **이미지 생성의
    # 참고 이미지**로만 쓴다(2026-08-03 사용자 결정: 생성조차 웹 검색 결과를 참고한다).
    meets_spec: bool = True
    # 어디서 온 사진인가. WEB_IMAGE는 네이버 이미지 검색 결과이고, YOUTUBE_THUMBNAIL은
    # 유튜브 영상의 썸네일이다. 호스트명으로도 짐작할 수 있지만, '이 사진 위에 제목 박스를
    # 얹어도 되는가'처럼 종류에 따라 갈리는 처리가 있어 값으로 들고 있는다.
    # 기본값이 WEB_IMAGE라 기존 경로·옛 데이터는 예전과 똑같이 동작한다.
    source_type: str = WEB_PHOTO_SOURCE_WEB_IMAGE
    # 유튜브 썸네일일 때의 채널명과 영상 id. 어느 회차를 썼는지 로그·검증이 추적한다.
    # 영상 페이지 URL은 따로 두지 않는다 — source_url이 이미 watch?v= 주소다.
    channel_title: str = ""
    video_id: str = ""

    # --- 원출처(2026-08-11) ---
    #
    # 여기 있는 값은 전부 **확인된 것만** 담는다. 검색 응답이 원문 페이지를 주지 않는
    # 경로에서는 source_page_url이 None으로 남는다. 유튜브는 영상 페이지가 곧 원문이라
    # 채워지고, 네이버 뉴스 사진은 이미지 주소에 언론사 코드·기사 번호가 들어 있어
    # 기사 주소를 되만들 수 있는데 — **되만든 주소를 실제로 열어 200을 확인한 경우에만**
    # 채운다(image_origin.naver_news_origin, 2026-08-11 후속 지시). 확인하지 못하면
    # 비워 둔다는 원칙은 그대로다.
    #
    # source_name은 '어느 사이트에 실려 있었나'다. 검색 서비스 이름이 아니라 이미지가
    # 실린 사이트·채널·서비스이며, 확인할 수 없으면 빈 문자열이다. CDN 호스트를 그대로
    # 적지는 않는다 — imgnews.pstatic.net은 사이트 이름이 아니다.
    source_name: str = ""
    source_page_url: str | None = None
    # 라이선스는 출처가 스스로 밝힌 것만 적는다. 유튜브 videos.list의 status.license가
    # 유일하게 값을 주는 경로이고, 그 밖에는 None → usage_status는 unknown이다.
    license: str | None = None
    license_url: str | None = None
    usage_status: str = IMAGE_USAGE_UNKNOWN

    # 출처 캡션: 2026-08-10 사용자 지시(네이버 이미지 출처 표기 규칙 준수)로 자동 표기를
    # 한다 — live_adapters._captioned_with_source가 유튜브 썸네일은 채널명+영상 주소,
    # 웹 이미지는 source_name(없으면 호스트명)을 GeneratedPostImage.caption에 싣는다.
    # (2026-08-03의 '자동 표기 안 함' 결정은 2026-08-10 지시로, 그때의 "CDN 호스트명뿐"
    # 이라는 한계는 2026-08-11 원출처 되찾기로 각각 대체됐다.)


class GeneratedPostImage(CamelModel):
    data_url: str
    alt_text: str
    prompt: str
    provider: str
    model: str
    generated_at: str
    mime_type: str
    # generated=이미지 모델 생성, reference=사용자 업로드, rendered=코드 렌더링(표·그래프·
    # 과정도·인포그래픽 — 한글이 필요해 이미지 모델 대신 PIL로 그린 것),
    # web=웹에서 찾아온 실제 사진(출처 캡션 필수).
    source: Literal["generated", "reference", "rendered", "web"] | None = None
    # 자료 캡션(출처·기준시점 포함). 시각자료 아래 별도 문단으로 붙는다. 없으면 캡션 생략.
    caption: str | None = None
    # 화면·발행 HTML에서 이 이미지가 어떤 종류인지. 사진과 도표는 같은 여백·테두리를 쓰면
    # 안 된다 — 사진은 살짝 둥근 모서리가 어울리고, 데이터 그림은 테두리가 이미 그림 안에
    # 있다. 옛 문서에는 없으므로 None이고, 그때는 source로 유추한다(media_kind_of).
    media_kind: str | None = None
    # 웹에서 가져온 사진(source="web")의 **원본 이미지 주소**(2026-08-10). 원고 복사가
    # 이미지 주소를 로컬 서버(localhost)로 적으면 네이버·벨로그 등 바깥에서는 전부
    # 깨진다 — 원본 주소가 있으면 복사본이 그것을 쓴다. 유튜브 썸네일은 영상 주소가
    # 아니라 i.ytimg 이미지 주소다. 생성·렌더링·업로드 이미지는 원본이 없으므로 None이고,
    # 옛 문서도 None이라 예전과 똑같이 동작한다.
    source_url: str | None = None
    # 구조화된 출처(2026-08-11). caption 문자열과 **같은 사실**을 값으로 들고 있는 것이고,
    # caption은 발행 본문에 실리는 표기라 그대로 둔다 — 이쪽은 화면이 링크·이용 조건까지
    # 안정적으로 그릴 수 있게 하려고 있다. 옛 문서·코드로 그린 도표·사용자 업로드에는
    # 없으므로 None이며, 그때 화면은 예전처럼 caption만 보여준다.
    image_source: ImageSourceInfo | None = None


# 콘텐츠 설계의 섹션 목적. 문제 제기→근거→해결→기능→활용→결론 흐름의 어느 역할인지.
SECTION_PURPOSES = (
    "문제 제기",
    "근거 제시",
    "해결책 설명",
    "기능 설명",
    "활용 사례",
    "비교 분석",
    "경험 서술",
    "결론",
)

# 글 유형. 목적·자료 유무에 따라 콘텐츠 설계가 고른다. 사용자 경험 자료가 없으면
# REVIEW 대신 INFORMATION으로 판단하도록 프롬프트가 강제한다(가상 체험 금지).
ARTICLE_TYPES = (
    "INFORMATION",
    "REVIEW",
    "COMPARISON",
    "PROMOTION",
    "HOW_TO",
    "TREND_CONNECTION",
    "EXPERIENCE",
)

# 섹션별 시각자료 유형. NONE이 기본값이다 — 시각자료는 장식이 아니라 설명 도구라,
# 필요 근거(visualReason)가 있을 때만 계획한다.
# ILLUSTRATION은 2026-07-22에 제거 — 이미지 프롬프트가 일러스트를 금지해 설계가 골라도
# 결과는 사진이었다. 옛 설계에 남은 값은 파싱 단계에서 NONE으로 강등된다.
VISUAL_TYPES = (
    "NONE",
    "SCREENSHOT",
    "PHOTO",
    "TABLE",
    "BAR_CHART",
    "LINE_CHART",
    "PIE_CHART",
    "PROCESS_DIAGRAM",
    "INFOGRAPHIC",
    "CHECKLIST",
)


# ---------------------------------------------------------------------------
# 편집 스타일 계획(2026-07-28)
#
# 지금까지 글의 시각 정체성은 두 가지로만 정해졌다: 도표는 네 개 프리셋, 사진은 post_id로
# 고른 네 개 팔레트. 그 결과 뷰티 후기든 벤치마크 비교든 같은 흰 바탕·파란 포인트로 나왔다.
#
# 여기 있는 것은 '글 하나의 편집 방향'이다. 카테고리(무엇에 대한 글인가)와 아키타입(어떤
# 형태의 글인가)을 먼저 정하고, 나머지 시각 선택(테마·레이아웃·팔레트)은 그 위에서
# variation_seed로 결정적으로 뽑는다 — 무작위 색을 무제한으로 만들지 않고, 검증된 후보
# 안에서만 고른다. 같은 글을 다시 열면 같은 디자인이고(계획을 결과에 저장한다), 다시
# 생성하면 generation_revision이 달라져 같은 카테고리 안의 다른 변형이 나온다.
# ---------------------------------------------------------------------------

# 글의 소재 카테고리. 페르소나가 아니라 목적·소재·참고자료로 판단한다 —
# 같은 '체험 후기 리뷰어'라도 화장품은 BEAUTY, 러닝화는 FITNESS_SPORTS다.
CONTENT_CATEGORIES = (
    "DAILY_LIFE",
    "BEAUTY",
    "FASHION",
    "FOOD",
    "TRAVEL",
    "FITNESS_SPORTS",
    "TECH_IT",
    "GAMING_ESPORTS",
    "BUSINESS_FINANCE",
    "EDUCATION",
    "LOCAL_LIFE",
    "BRAND_PRODUCT",
    "TREND_NEWS",
    "OTHER",
)

# 글의 형태. 도입·전개·결론의 골격이 여기서 갈린다(prompts.ARCHETYPE_STRUCTURES).
EDITORIAL_ARCHETYPES = (
    "DAILY_JOURNAL",
    "PERSONAL_EPISODE",
    "FIELD_REVIEW",
    "PRODUCT_TEST_LOG",
    "COMPARISON_LAB",
    "EXPERT_EXPLAINER",
    "STEP_BY_STEP_TUTORIAL",
    "ISSUE_BRIEF",
    "TREND_COMMENTARY",
    "BRAND_STORY",
    "LOCAL_GUIDE",
    "FAQ_GUIDE",
)

# 실제 경험 근거가 필요한 아키타입. 참고자료에 사용자의 경험이 없으면 코드가 강등한다.
EXPERIENCE_REQUIRED_ARCHETYPES = (
    "DAILY_JOURNAL",
    "PERSONAL_EPISODE",
    "FIELD_REVIEW",
    "PRODUCT_TEST_LOG",
)

VOICE_MODES = (
    "WARM_PERSONAL",
    "DIRECT_EXPERT",
    "CALM_OBSERVER",
    "FRIENDLY_COACH",
    "BRAND_VOICE",
)

VISUAL_DENSITY_LEVELS = ("NONE", "LOW", "MEDIUM", "HIGH")
EMOJI_LEVELS = ("NONE", "MINIMAL", "LIGHT")
DECORATION_LEVELS = ("LOW", "MEDIUM", "HIGH")

# 글이 흐르는 방식. 모든 글이 "현재 상황 → 불편 → 질문 → 소재 소개"로 시작하지 않게 한다.
ARTICLE_RHYTHMS = (
    "SCENE_FIRST",  # 한 장면에서 시작해 시간 순으로
    "ANSWER_FIRST",  # 결론을 먼저 주고 근거를 편다
    "PROBLEM_FIRST",  # 문제 규정 → 해결
    "TIMELINE",  # 시간·단계 흐름
    "CRITERIA_FIRST",  # 기준을 먼저 공개하고 항목별로
    "QUESTION_ANSWER",  # 질문 하나에 답하고 다음 질문으로
    "FACT_THEN_MEANING",  # 사실 제시 → 왜 지금인가 → 한계
)

# 대표 썸네일 레이아웃. 피사체 영역과 문구 영역이 겹치지 않는 조합만 있다.
THUMBNAIL_LAYOUTS = (
    "COPY_LEFT_SUBJECT_RIGHT",
    "COPY_RIGHT_SUBJECT_LEFT",
    "COPY_TOP_SUBJECT_BOTTOM",
    "COPY_BOTTOM_SUBJECT_TOP",
    "SMALL_LABEL_TOP_LEFT",
    "SMALL_LABEL_BOTTOM_LEFT",
    "CENTER_COPY_ON_NEGATIVE_SPACE",
    "PRODUCT_CUTOUT_WITH_SIDE_COPY",
    "NO_COPY_EDITORIAL_PHOTO",
)

THUMBNAIL_COPY_MODES = ("NONE", "SHORT_LABEL", "TWO_LINE_HEADLINE")

BODY_HIGHLIGHT_STYLES = (
    "MINIMAL",
    "BOLD_ONLY",
    "BOLD_AND_HIGHLIGHT",
    "BOLD_AND_CALLOUT",
)

# 도표 테마. visuals.py의 _THEMES와 값이 같아야 한다 — 렌더러가 이 이름으로 팔레트를 찾는다.
VISUAL_THEMES = (
    "BEAUTY_EDITORIAL",
    "LIFESTYLE_JOURNAL",
    "FITNESS_PERFORMANCE",
    "TECH_BENCHMARK_LIGHT",
    "TECH_BENCHMARK_DARK",
    "GAMING_ESPORTS",
    "FINANCE_REPORT",
    "FOOD_TRAVEL",
    "EDUCATION_GUIDE",
    "BRAND_MINIMAL",
    "TREND_MAGAZINE",
    "EDITORIAL_NEUTRAL",
)

TABLE_VARIANTS = (
    "STANDARD_GRID",
    "FEATURE_MATRIX",
    "WINNER_HIGHLIGHT",
    "TWO_PRODUCT_SPLIT",
    "COMPACT_MOBILE",
    "SPEC_SHEET",
    "PROS_CONS_CARDS",
)

PROCESS_VARIANTS = (
    "HORIZONTAL_STEPS",
    "VERTICAL_TIMELINE",
    "SNAKE_FLOW",
    "CHECKPOINT_FLOW",
    "INPUT_OUTPUT_FLOW",
)

INFOGRAPHIC_VARIANTS = (
    "HUB_AND_SPOKE",
    "STACKED_SECTIONS",
    "TWO_COLUMN_EDITORIAL",
    "KEYWORD_CLUSTER",
    "BEFORE_AFTER",
    "CAUSE_AND_EFFECT",
)

# 본문 사진 한 장이 맡는 정보 역할. 같은 제품을 정면·정면·정면으로 세 번 만들지 않으려면
# 사진마다 역할이 달라야 한다.
PHOTO_ROLES = (
    "PRODUCT_HERO",
    "PRODUCT_DETAIL",
    "IN_USE_SCENE",
    "BEFORE_AFTER_EVIDENCE",
    "PLACE_ATMOSPHERE",
    "WORK_PROCESS",
    "SCREENSHOT_EVIDENCE",
    "RECEIPT_EVIDENCE",
    "EVENT_CONTEXT",
    "EQUIPMENT_SETUP",
)

# 참고 이미지 한 장이 이 글에서 맡는 역할.
REFERENCE_IMAGE_ROLES = (
    "PRODUCT_ANCHOR",
    "SCENE_ANCHOR",
    "DETAIL_ANCHOR",
    "SCREENSHOT_EVIDENCE",
    "RECEIPT_EVIDENCE",
    "CONTEXT_ONLY",
)

PHOTO_SOURCE_MODES = ("GENERATED", "REUSED")

# 카드 한 장의 사진을 어디서 구할지 계획 모델이 고른다(2026-08-03).
#   WEB_PHOTO:         실존 대상의 실제 사진이어야 설득력 있는 장면 — 네이버 이미지 검색
#   YOUTUBE_THUMBNAIL: 영상·방송·무대·게임 플레이 등 영상 콘텐츠의 장면 — 유튜브 썸네일
#   AI_GENERATED:      특정 실존 대상이 필요 없는 일상·개념·연출 장면 — 이미지 모델 생성
# 검색으로 못 구하면 코드가 자동으로 다음 소스→생성으로 폴백한다.
IMAGE_SOURCES = ("WEB_PHOTO", "YOUTUBE_THUMBNAIL", "AI_GENERATED")

# 이 글의 핵심 시각 대상이 무엇인가. 사진이 '주제와 관련된 일반적인 장면'으로 미끄러지는
# 것을 막는 값이다 — 스파이더맨 글에서 거미줄과 도시 야경만 나오던 원인이 여기 없었다.
#   FICTIONAL_CHARACTER: 이름이 명시된 허구 캐릭터(스파이더맨·배트맨)
#   REAL_NAMED_PERSON:   이름이 명시된 실제 인물·공인·역사적 인물(손흥민·아이유·세종대왕)
#   GENERIC_PERSON_ROLE: 특정 개인이 아닌 직업·역할(헬스 트레이너·개발자)
#   NON_PERSON:          제품·장소·음식·개념
VISUAL_SUBJECT_KINDS = (
    "FICTIONAL_CHARACTER",
    "REAL_NAMED_PERSON",
    "GENERIC_PERSON_ROLE",
    "NON_PERSON",
)

# 고유한 이름을 가진 대상. 이 둘만 '그 대상 본인이 반드시 화면에 있어야 한다'가 된다.
NAMED_SUBJECT_KINDS = ("FICTIONAL_CHARACTER", "REAL_NAMED_PERSON")

# 사진 한 장을 '얼마나 넓게 잡는가'(2026-08-05). photo_role이 '무엇을 보여 주는가'라면
# 이쪽은 구도다 — 둘은 다른 축이라, 같은 PRODUCT_HERO도 전체 실루엣일 수도 반신일 수도
# 있었고 그래서 대표 사진에 손잡이만 나오는 일이 생겼다.
#   FULL_SUBJECT: 대상의 전체 형태가 프레임 안에 온전히 들어온다(주위 여백 포함).
#   MEDIUM:       대상과 주변 맥락이 함께 보인다. 잘려도 되는 것은 배경뿐이다.
#   CLOSE_UP:     의도적인 부분 확대. 문단이 그 디테일을 설명할 때만 허용한다.
PHOTO_FRAMINGS = ("FULL_SUBJECT", "MEDIUM", "CLOSE_UP")

# 부분 확대를 허용하는 사진 역할. 이 역할이 아닌 카드가 CLOSE_UP을 골라 오면 코드가
# 되돌린다 — 문단이 소재·질감·마감을 설명하지 않는데 확대하면 그 사진은 대상을
# 알아볼 수 없는 그림이 된다.
CLOSE_UP_PHOTO_ROLES = ("PRODUCT_DETAIL",)

# 대상의 전체 형태를 반드시 보여야 하는 역할. 대표 썸네일은 종류와 무관하게 여기 든다.
FULL_SUBJECT_PHOTO_ROLES = ("PRODUCT_HERO", "EQUIPMENT_SETUP", "BEFORE_AFTER_EVIDENCE")


def normalized_framing(
    framing: str | None, photo_role: str, *, is_thumbnail: bool = False
) -> str:
    """구도 값의 정규화. 계획 모델이 무엇을 내려도 아래 규칙이 이긴다.

    - 대표 썸네일은 언제나 FULL_SUBJECT다. 표지 한 장으로 '무엇에 대한 글인가'를
      말해야 하는데 부분 확대로는 그 일을 못 한다.
    - 전체 형태가 곧 그 역할인 사진(제품 대표컷·장비 배치·전후 비교)도 FULL_SUBJECT다.
      "그것이 무엇인지 알려 주는 한 장"이 PRODUCT_HERO의 정의다.
    - CLOSE_UP은 PRODUCT_DETAIL에서만 남는다. 다른 역할에서 왔으면 MEDIUM으로 내린다.
    - 값이 없거나 모르는 값이면 MEDIUM — 예전 계획·옛 저장 문서의 기본 구도다.
    """
    value = (framing or "").strip().upper()
    if value not in PHOTO_FRAMINGS:
        value = ""
    if is_thumbnail or photo_role in FULL_SUBJECT_PHOTO_ROLES:
        return "FULL_SUBJECT"
    if value == "CLOSE_UP" and photo_role not in CLOSE_UP_PHOTO_ROLES:
        return "MEDIUM"
    return value or "MEDIUM"


def normalized_subject_fields(
    subject_kind: str, subject_identity: str | None, must_show_subject: bool
) -> tuple[str, str | None, bool]:
    """핵심 시각 대상 세 값의 정규화·검증. 저장 모델과 이미지 입력이 같은 규칙을 쓴다.

    - 모르는 종류는 NON_PERSON으로 되돌린다(옛 데이터·오타 방어).
    - 고유한 이름을 가진 대상이면 must_show_subject는 모델이 뭐라 하든 True다.
    - 실존 인물인데 이름이 없으면 만들 수 있는 그림이 '그 직업의 아무나'뿐이라 거부한다.
    """
    kind = (subject_kind or "NON_PERSON").strip().upper()
    if kind not in VISUAL_SUBJECT_KINDS:
        kind = "NON_PERSON"
    identity = (subject_identity or "").strip() or None
    must_show = bool(must_show_subject)
    if kind in NAMED_SUBJECT_KINDS:
        must_show = True
    if kind == "REAL_NAMED_PERSON" and not identity:
        raise ValueError("REAL_NAMED_PERSON requires subject_identity")
    return kind, identity, must_show


class ReferenceImageEvidence(CamelModel):
    """참고 이미지 한 장을 '무엇이 보이는가 / 무엇에 쓸 수 있는가 / 무엇을 단정하면 안 되는가'로
    나눈 구조. 사진이 있다는 사실만으로 구매·사용을 단정하지 않게 하는 것이 목적이다."""

    reference_id: str
    role: str = "CONTEXT_ONLY"
    # 이미지에서 실제로 보이는 대상(한국어 한 줄). 추정이 아니라 관찰이다.
    subject: str = ""
    # 이 이미지를 어떻게 쓸 수 있는가(원본 재사용·썸네일 배경 확장·제품 중심 크롭 등).
    allowed_uses: list[str] = []
    # 이 이미지에서 끌어내면 안 되는 추정(가격·사용 기간·착화감 등).
    forbidden_inferences: list[str] = []
    # 개인을 특정할 수 있는 글자가 보이는 자리(번호판·전화번호·생년월일 등). 글에 실을 때
    # 검게 덮는다(shared/image_privacy.py). **기본값이 빈 목록이므로 옛 문서는 그대로 읽힌다.**
    private_regions: list[PrivateRegion] = []
    # 빈 regions는 "검사했고 없음"과 "검사하지 못함"을 구분하지 못한다. 모델이 이 이미지의
    # 역할 레코드를 실제로 반환한 경우에만 True로 세워 게시 경로가 fail-closed할 수 있게 한다.
    privacy_scanned: bool = False


# ---------------------------------------------------------------------------
# 콘텐츠 엔티티(2026-08-03)
#
# 사용자가 고른 검색 키워드는 '검색어 조합'이지 문장 속 명사가 아니다("창섭 전과자").
# 그 조합을 그대로 문장에 붙여 넣으면 어떤 프롬프트 규칙으로도 고칠 수 없는 비문이 되고,
# 소재가 실제 영상 콘텐츠일 때는 프로그램의 핵심 포맷 대신 보조 장면(학식·이동)이 글의
# 중심으로 올라간다. 그래서 파이프라인에서 셋을 분리한다.
#
#   1) 원본 검색 키워드(raw_keyword)  — 검색과 의도 파악에만 쓴다.
#   2) 글에서 쓸 자연스러운 표현(natural_phrases)  — 문장·제목에 실제로 들어간다.
#   3) 검색으로 확인된 콘텐츠 정보(canonical_name·core_format·…)  — 사실의 근거다.
#
# 값은 전부 기본값을 가지므로, 이 프로필이 없는 옛 문서·구형 어댑터는 예전과 똑같이 동작한다.
# ---------------------------------------------------------------------------

# 이 글이 어느 블로그 카테고리에 속하는가. 글의 **구조 전체**가 여기서 갈린다 — 같은
# '실제 대상 소개'라도 책은 저자·출간·목차를, 자동차는 트림·제원·가격을, 여행지는 동선·
# 운영시간·계절을 먼저 말해야 한다. 네이버 블로그의 카테고리 체계를 그대로 쓴다(발행처가
# 그쪽이고, 사용자가 이미 아는 이름이라 따로 배울 것이 없다).
BLOG_CATEGORIES = (
    "문학·책",
    "영화",
    "미술·디자인",
    "공연·전시",
    "음악",
    "드라마",
    "스타·연예인",
    "만화·애니",
    "방송",
    "일상·생각",
    "육아·결혼",
    "반려동물",
    "좋은글·이미지",
    "패션·미용",
    "인테리어·DIY",
    "요리·레시피",
    "상품리뷰",
    "원예·재배",
    "게임",
    "스포츠",
    "사진",
    "자동차",
    "취미",
    "국내여행",
    "세계여행",
    "맛집",
    "IT·컴퓨터",
    "사회·정치",
    "건강·의학",
    "비즈니스·경제",
    "어학·외국어",
    "교육·학문",
)

# 소재가 무엇인가. 일반 명사와 작품명이 같은 소재('전과자')는 검색 결과와 사용자의 키워드를
# 함께 보고 확정해야 한다 — 유형이 갈리면 글의 종류·사실 근거·이미지 경로가 모두 갈린다.
#
# 카테고리(BLOG_CATEGORIES)가 '글의 구조'를 정한다면 이 유형은 '사실과 이미지의 규칙'을
# 정한다. 둘은 다른 축이다: 같은 상품리뷰라도 자동차(CAR_MODEL)는 연식·트림을 구분해야
# 하고 프랜차이즈 메뉴(BRAND_MENU_ITEM)는 브랜드와 정식 메뉴명을 구분해야 한다.
#
# 뒤쪽 두 값(MOVIE_OR_DRAMA·PLACE)은 **옛 문서 호환**이다. 지금은 MOVIE/DRAMA_SERIES와
# RESTAURANT/TRAVEL_DESTINATION/EXHIBITION으로 나뉘지만, 이미 저장된 글이 그 값을 들고
# 있으므로 계속 읽힌다(플래그 판정에도 그대로 포함된다).
CONTENT_ENTITY_TYPES = (
    "GENERAL_TOPIC",
    # 사람·그룹
    "REAL_NAMED_PERSON",
    "IDOL_GROUP",
    "MUSIC_ARTIST",
    "SPORTS_PLAYER",
    "SPORTS_TEAM",
    # 작품·콘텐츠
    "BOOK",
    "MOVIE",
    "DRAMA_SERIES",
    "TV_PROGRAM",
    "YOUTUBE_PROGRAM",
    "WEB_SERIES",
    "ANIMATION",
    "COMIC",
    "GAME",
    # 상품·서비스
    "PRODUCT_OR_SERVICE",
    "PRODUCT_MODEL",
    "BRAND_MENU_ITEM",
    "FOOD_PRODUCT",
    "BEAUTY_PRODUCT",
    "FASHION_ITEM",
    "TECH_PRODUCT",
    "CAR_MODEL",
    # 장소·행사
    "RESTAURANT",
    "TRAVEL_DESTINATION",
    "EXHIBITION",
    "PERFORMANCE",
    "SPORTS_EVENT",
    # 주제
    "RECIPE",
    "EDUCATIONAL_TOPIC",
    "MEDICAL_TOPIC",
    "SOCIAL_ISSUE",
    "ECONOMIC_TOPIC",
    "LANGUAGE_TOPIC",
    # 옛 문서 호환(지금은 위 값들로 나뉜다)
    "MOVIE_OR_DRAMA",
    "PLACE",
)

# --- 특수 플래그 ------------------------------------------------------------
#
# 카테고리와 **별개로** 켜지는 규칙 묶음이다. 카테고리가 '무엇을 어떤 순서로 쓰는가'라면
# 이 플래그들은 '무엇을 지어내면 안 되고 어떤 이미지를 써야 하는가'를 정한다. 카테고리가
# 상품리뷰든 맛집이든, 대상이 실제로 파는 물건이면 같은 실물 이미지 규칙이 걸려야 한다.

# 실제 영상 콘텐츠. '핵심 포맷 vs 보조 장면' 규칙과 1인칭 시청 후기 금지가 켜진다.
MEDIA_CONTENT_ENTITY_TYPES = (
    "YOUTUBE_PROGRAM",
    "TV_PROGRAM",
    "WEB_SERIES",
    "MOVIE",
    "DRAMA_SERIES",
    "ANIMATION",
    "MOVIE_OR_DRAMA",
)

# 실제로 파는 물건·서비스. 같은 종류의 일반 이미지로 대체하면 안 되고, 먹어보지·써보지
# 않은 맛과 성능을 단정하면 안 된다.
REAL_PRODUCT_ENTITY_TYPES = (
    "PRODUCT_OR_SERVICE",
    "PRODUCT_MODEL",
    "BRAND_MENU_ITEM",
    "FOOD_PRODUCT",
    "BEAUTY_PRODUCT",
    "FASHION_ITEM",
    "TECH_PRODUCT",
    "CAR_MODEL",
)

# 실존 인물·그룹. 생성 이미지로 얼굴을 만들지 않고, 사생활·건강·불화를 추정하지 않는다.
REAL_PERSON_ENTITY_TYPES = (
    "REAL_NAMED_PERSON",
    "IDOL_GROUP",
    "MUSIC_ARTIST",
    "SPORTS_PLAYER",
    "SPORTS_TEAM",
)

# 실제 장소·행사. 비슷한 도시·일반 매장 이미지로 대체하지 않고, 지점을 구분한다.
REAL_PLACE_ENTITY_TYPES = (
    "RESTAURANT",
    "TRAVEL_DESTINATION",
    "EXHIBITION",
    "PERFORMANCE",
    "SPORTS_EVENT",
    "PLACE",
)

# 틀리면 독자가 손해를 보는 주제. 최신성·출처를 최우선으로 하고 단정을 금지한다.
HIGH_STAKES_ENTITY_TYPES = ("MEDICAL_TOPIC", "SOCIAL_ISSUE", "ECONOMIC_TOPIC")
HIGH_STAKES_CATEGORIES = ("건강·의학", "사회·정치", "비즈니스·경제", "세계여행")

# 유튜브에 공식 영상이 있는 유형. 공식 영상 썸네일을 생성 이미지보다 먼저 찾는다.
# 영화·드라마는 여기 없다 — 그쪽의 공식 이미지는 포스터·스틸컷이라 일반 이미지 검색이
# 유튜브 썸네일보다 잘 찾는다.
OFFICIAL_YOUTUBE_ENTITY_TYPES = ("YOUTUBE_PROGRAM", "WEB_SERIES", "TV_PROGRAM")

# 어떤 종류의 실물 이미지를 구해야 하는가. 생성 이미지로 대체할 수 없는 자리를 표시한다.
# NONE이면 실물 이미지 요구가 없는 글이다(개념·일상·감성 — 생성 이미지가 적합하다).
REAL_IMAGE_TYPES = (
    "NONE",
    "OFFICIAL_VIDEO_THUMBNAIL",  # 공식 영상 썸네일(유튜브 프로그램·웹예능·방송)
    "OFFICIAL_POSTER_OR_STILL",  # 공식 포스터·스틸컷(영화·드라마·애니·공연)
    "OFFICIAL_PRODUCT_IMAGE",  # 공식 제품·메뉴 이미지
    "OFFICIAL_PERSON_PHOTO",  # 소속사 공식 프로필·보도 사진
    "OFFICIAL_PLACE_PHOTO",  # 실제 장소·매장·전시장 사진
    "OFFICIAL_COVER_ART",  # 책 표지·앨범 커버
    "OFFICIAL_SCREENSHOT",  # 게임·소프트웨어 공식 화면
)

# 유형별 기본 실물 이미지 종류. 모델이 realImageType을 비워 두거나 엉뚱하게 채워도
# 코드가 유형에서 되짚을 수 있어야 한다 — 이미지 경로가 모델의 한 필드에 걸리면 안 된다.
_DEFAULT_REAL_IMAGE_TYPE: dict[str, str] = {
    **{t: "OFFICIAL_VIDEO_THUMBNAIL" for t in OFFICIAL_YOUTUBE_ENTITY_TYPES},
    "MOVIE": "OFFICIAL_POSTER_OR_STILL",
    "DRAMA_SERIES": "OFFICIAL_POSTER_OR_STILL",
    "MOVIE_OR_DRAMA": "OFFICIAL_POSTER_OR_STILL",
    "ANIMATION": "OFFICIAL_POSTER_OR_STILL",
    "COMIC": "OFFICIAL_POSTER_OR_STILL",
    "PERFORMANCE": "OFFICIAL_POSTER_OR_STILL",
    "EXHIBITION": "OFFICIAL_POSTER_OR_STILL",
    "BOOK": "OFFICIAL_COVER_ART",
    "GAME": "OFFICIAL_SCREENSHOT",
    **{t: "OFFICIAL_PRODUCT_IMAGE" for t in REAL_PRODUCT_ENTITY_TYPES},
    **{t: "OFFICIAL_PERSON_PHOTO" for t in REAL_PERSON_ENTITY_TYPES},
    "RESTAURANT": "OFFICIAL_PLACE_PHOTO",
    "TRAVEL_DESTINATION": "OFFICIAL_PLACE_PHOTO",
    "SPORTS_EVENT": "OFFICIAL_PLACE_PHOTO",
    "PLACE": "OFFICIAL_PLACE_PHOTO",
}


class RelatedPerson(CamelModel):
    """콘텐츠와 관계가 확인된 사람. 이름만 알면 '창섭 전과자'가 다시 만들어지므로,
    무엇으로 엮이는지(출연자·진행자·제작자)를 함께 들고 있어야 문장으로 풀 수 있다."""

    name: str
    # 이 콘텐츠와의 관계(출연자·진행자·제작자 등). 확인되지 않으면 빈 문자열.
    relation: str = ""


class ContentEntityProfile(CamelModel):
    """소재가 실제로 무엇인지, 그것을 문장에서 어떻게 부를지, 그리고 어떤 글로 쓸지.

    세 축이 한 모델에 있다.

    - **카테고리**(primary_category) — 글의 구조. 무엇을 어떤 순서로 말하는가.
    - **유형**(entity_type) — 사실과 이미지의 규칙. 무엇을 지어내면 안 되는가.
    - **표현**(canonical_name·natural_phrases) — 검색어가 아니라 문장에 쓸 이름.

    한 번의 판정에서 셋을 함께 받는다. 같은 질문("이 글이 붙잡아야 할 대상은 무엇인가")에
    대한 답이고, 호출을 나누면 세 답이 서로 어긋난다.
    """

    entity_type: str = "GENERAL_TOPIC"
    # 이 글의 메인 카테고리(BLOG_CATEGORIES 중 하나). 글의 전체 구조를 결정한다.
    # 빈 문자열이면 카테고리 지침이 통째로 빠지고 예전과 같이 동작한다.
    primary_category: str = ""
    # 보조 카테고리. 문체·정보·이미지 지침을 **보완**하는 용도로만 쓴다. 두 카테고리의
    # 구조를 섞지 않는다 — 글의 중심은 하나다.
    secondary_category: str = ""
    # 이 글을 어떤 형태로 쓰는가(신제품 정보형·시청 포인트형·방문 전 참고형 등).
    # 자유 문자열이다. 경험 근거가 없으면 정보형 계열이어야 한다.
    writing_mode: str = ""
    # 검색으로 확인된 정식 명칭. 확인되지 않으면 빈 문자열이고, 그때는 소재를 그대로 쓴다.
    canonical_name: str = ""
    # 확인된 브랜드·제작 주체. 상품·메뉴 글에서 '어느 브랜드의 무엇'인지 갈리는 값이다.
    brand: str = ""
    # 사용자가 고른 원본 검색어. 문장에 그대로 복사하면 안 되는 값이라 따로 들고 있는다.
    raw_keyword: str = ""
    platform: str = ""
    official_channel: str = ""
    related_people: list[RelatedPerson] = []
    # 매 회차 반복되는 핵심 포맷 한 줄. 글의 도입부와 첫 핵심 섹션이 이것을 설명해야 한다.
    core_format: str = ""
    # 회차마다 반복되는 핵심 활동. 글의 중심이다.
    primary_activities: list[str] = []
    # 보조적으로 등장하는 활동. 설명은 할 수 있지만 글의 중심이 되면 안 된다.
    secondary_activities: list[str] = []
    # 중심으로 다루면 프로그램을 잘못 설명하게 되는 부수 장면.
    background_scenes: list[str] = []
    # 공식 영상 검색에 쓸 검색어(정식명·정식명+출연자·정식명+채널).
    official_video_queries: list[str] = []
    # 문장·제목에서 쓸 수 있는 자연스러운 표현('이창섭이 출연하는 전과자').
    natural_phrases: list[str] = []
    # 이 소재에서 쓰면 안 되는 표현(원본 검색어를 명사처럼 쓴 형태 등).
    forbidden_phrases: list[str] = []
    # 날짜·가격·사양·일정처럼 시점에 따라 달라지는 사실이 글의 중심인가. True인데 확인된
    # 출처가 없으면 단정하지 않고 '확인 필요'로 쓴다.
    requires_fresh_research: bool = False
    # 이 자리를 생성 이미지로 채우면 안 되는가. REAL_IMAGE_TYPES 중 무엇을 구해야 하는지는
    # real_image_type이 말한다. 모델이 비워 두면 코드가 유형에서 되짚는다(effective_*).
    requires_real_images: bool = False
    real_image_type: str = "NONE"
    # 이 판정의 근거 세기(0~1). 낮아도 버리지 않고 기록만 한다.
    confidence: float = 0.0

    # --- 특수 플래그 -------------------------------------------------------
    #
    # 전부 유형(entity_type)에서 파생된다. 모델이 따로 채우는 값이 아니다 — 플래그를
    # 모델에 맡기면 유형과 플래그가 어긋난 조합(영상 콘텐츠인데 실물 이미지 불필요)이
    # 그대로 내려간다.

    @property
    def is_media_content(self) -> bool:
        """실제 영상 콘텐츠인가. 핵심 포맷 규칙과 1인칭 시청 후기 금지가 여기서 갈린다."""
        return self.entity_type in MEDIA_CONTENT_ENTITY_TYPES

    @property
    def is_real_product(self) -> bool:
        """실제로 파는 물건·서비스인가. 같은 종류의 일반 이미지 대체가 금지된다."""
        return self.entity_type in REAL_PRODUCT_ENTITY_TYPES

    @property
    def is_real_person_or_group(self) -> bool:
        """실존 인물·그룹인가. 얼굴 생성 금지와 사생활 추정 금지가 켜진다."""
        return self.entity_type in REAL_PERSON_ENTITY_TYPES

    @property
    def is_real_place(self) -> bool:
        """실제 장소·행사인가. 비슷한 다른 장소로 대체하는 것이 금지된다."""
        return self.entity_type in REAL_PLACE_ENTITY_TYPES

    @property
    def is_high_stakes(self) -> bool:
        """틀리면 독자가 손해를 보는 주제인가. 유형과 카테고리 어느 쪽이든 걸리면 켜진다 —
        '건강·의학' 카테고리의 일반 주제 글도 단정을 피해야 한다."""
        return (
            self.entity_type in HIGH_STAKES_ENTITY_TYPES
            or self.primary_category in HIGH_STAKES_CATEGORIES
            or self.secondary_category in HIGH_STAKES_CATEGORIES
        )

    @property
    def is_real_entity(self) -> bool:
        """실제로 존재하는 대상을 다루는 글인가(영상·상품·인물·장소 중 하나)."""
        return (
            self.is_media_content
            or self.is_real_product
            or self.is_real_person_or_group
            or self.is_real_place
            or self.entity_type in ("BOOK", "COMIC", "GAME")
        )

    @property
    def effective_real_image_type(self) -> str:
        """실제로 구해야 할 이미지 종류. 모델 값이 유효하면 그것이 이기고, 아니면 유형에서
        되짚는다. 대상 이름을 모르면(canonical_name 없음) 무엇을 찾을지 알 수 없으므로 NONE."""
        if not self.canonical_name.strip():
            return "NONE"
        declared = (self.real_image_type or "").strip().upper()
        if declared in REAL_IMAGE_TYPES and declared != "NONE":
            return declared
        return _DEFAULT_REAL_IMAGE_TYPE.get(self.entity_type, "NONE")

    @property
    def wants_real_image(self) -> bool:
        """이 글의 사진 자리를 생성 이미지로 채우면 안 되는가.

        모델의 requires_real_images를 그대로 믿지 않는다 — 실존 대상이라는 판정 자체가
        이미 그 답이다. 모델은 True로 올릴 수만 있고 내리지는 못한다.
        """
        if self.effective_real_image_type == "NONE":
            return False
        return self.is_real_entity or self.requires_real_images

    @property
    def wants_official_youtube_thumbnail(self) -> bool:
        """공식 유튜브 영상 썸네일을 생성 이미지보다 먼저 찾아야 하는가."""
        return self.entity_type in OFFICIAL_YOUTUBE_ENTITY_TYPES and bool(
            self.canonical_name.strip()
        )

    @property
    def person_names(self) -> list[str]:
        return [p.name.strip() for p in self.related_people if p.name.strip()]

    @property
    def subject_label(self) -> str:
        """이 글이 실제로 다루는 대상 한 줄. 브랜드가 이름에 없으면 앞에 붙인다 —
        '리아 두툼새우 버거'만으로는 어느 브랜드의 메뉴인지 검색도 검증도 되지 않는다."""
        name = self.canonical_name.strip()
        brand = self.brand.strip()
        if not name:
            return brand
        if brand and brand not in name:
            return f"{brand} {name}"
        return name

    def search_queries(self) -> list[str]:
        """실물 이미지 검색어를 정밀한 것부터. 모델이 준 것이 먼저이고, 없으면 코드가 만든다.

        영상 콘텐츠만의 규칙이 아니다. 상품은 브랜드+정식명이 '같은 종류의 다른 제품'을
        걸러 내는 유일한 장치이고, 인물은 그룹명+이름이 동명이인을 줄인다.
        """
        name = self.canonical_name.strip()
        queries = [q.strip() for q in self.official_video_queries if q.strip()]
        if name:
            brand = self.brand.strip()
            people = self.person_names
            if brand and brand not in name:
                queries.append(f"{brand} {name}")
            if people:
                queries.append(f"{name} {people[0]}")
            channel = self.official_channel.strip()
            if channel and channel != name and channel != brand:
                queries.append(f"{name} {channel}")
            queries.append(name)
        seen: set[str] = set()
        return [q for q in queries if not (q in seen or seen.add(q))]


class ReferenceEvidenceProfile(CamelModel):
    """참고 URL·이미지·PDF·메모를 첨부물 목록이 아니라 '근거 정보'로 바꾼 것.

    원고와 이미지 계획이 같은 사실을 본다: 무엇이 이 글의 대상인가(primary_entity),
    무엇이 확인됐는가(confirmed_attributes), 무엇을 지어내면 안 되는가(forbidden_claims).
    참고자료가 하나도 없으면 has_references=False이고, 모든 목록이 비어 있다.
    """

    has_references: bool = False
    # 사용자가 실제 경험을 글로 적어 두었는가. 후기·체험 서술의 허용 여부가 여기서 갈린다.
    has_user_experience_evidence: bool = False
    primary_entity: str | None = None
    brand: str | None = None
    product_category: str | None = None
    confirmed_attributes: list[str] = []
    confirmed_use_scenes: list[str] = []
    reference_image_roles: list[ReferenceImageEvidence] = []
    # 참고 URL·검색 출처에서 확인된 사실(한 줄씩).
    source_facts: list[str] = []
    # 참고자료에 근거가 없어 쓰면 안 되는 표현.
    forbidden_claims: list[str] = []
    # 소재가 실제로 무엇인가(콘텐츠 유형·정식명·핵심 포맷). 옛 문서에는 없으므로 None이고,
    # 그때 관련 프롬프트 블록과 검증은 통째로 빠진다(예전과 같은 동작).
    content_entity: ContentEntityProfile | None = None

    @property
    def anchor(self) -> str:
        """이 글이 붙잡아야 할 대상 한 줄. 없으면 빈 문자열."""
        parts = [part for part in (self.brand, self.primary_entity) if part]
        # 브랜드가 대상 이름에 이미 들어 있으면 두 번 쓰지 않는다.
        if len(parts) == 2 and parts[0] in parts[1]:
            return parts[1]
        return " ".join(parts)


class VisualBudget(CamelModel):
    """이 글이 만들 수 있는 시각자료의 상한. **최소 개수가 아니다** — 전부 0이어도 정상이다."""

    thumbnail: int = 1
    reference_images_max: int = 2
    body_photos_max: int = 3
    rendered_visuals_max: int = 1


class WritingDirection(CamelModel):
    """원고를 쓸 때 그대로 실행하는 편집 지시 11항목.

    편집 계획이 '친근하게'·'전문적으로' 같은 형용사만 남기면 원고 단계에서 아무것도
    달라지지 않는다. 여기 값은 전부 **실행할 수 있는 문장**이어야 한다 —
    '읽기 좋게 쓴다'(x) / '핵심 판단을 한두 문장 안에 먼저 제시하고 조건은 뒤 문단에서
    설명한다'(o).

    도입·결말은 코드가 고른 회전 축(article_rhythm, title_variation.closing_mode)을
    이 글에 맞게 풀어 쓴 것이다. 축 자체를 모델이 고르지는 않는다.
    """

    voice_distance: str = ""
    reader_relationship: str = ""
    sentence_density: str = ""
    opening_mode: str = ""
    rhythm_profile: str = ""
    transition_style: str = ""
    detail_focus: str = ""
    first_person_policy: str = ""
    certainty_policy: str = ""
    closing_mode: str = ""
    avoid_patterns: list[str] = []


class EditorialStylePlan(CamelModel):
    """글 하나의 편집·시각 스타일 계획. 글 안에서는 일관되고, 글끼리는 달라진다."""

    content_category: str = "OTHER"
    editorial_archetype: str = "EXPERT_EXPLAINER"
    voice_mode: str = "DIRECT_EXPERT"
    visual_density: str = "LOW"
    emoji_level: str = "NONE"
    decoration_level: str = "LOW"
    article_rhythm: str = "ANSWER_FIRST"
    photo_language: str = "NATURAL_DAILY"
    thumbnail_layout: str = "COPY_LEFT_SUBJECT_RIGHT"
    thumbnail_copy_mode: str = "SHORT_LABEL"
    body_highlight_style: str = "BOLD_ONLY"
    chart_theme: str = "EDITORIAL_NEUTRAL"
    table_theme: str = "STANDARD_GRID"
    accent_family: str = "CYAN_NAVY"
    allowed_visual_types: list[str] = []
    forbidden_visual_types: list[str] = []
    visual_budget: VisualBudget = VisualBudget()
    # 같은 카테고리 안에서 어떤 변형을 고를지 정하는 결정적 씨앗. 저장되므로 새로고침해도
    # 디자인이 바뀌지 않고, '다시 생성하기'에서만 달라진다.
    variation_seed: str = ""
    # 이 글이 몇 번째 생성인가(0부터). variation_seed 계산에 들어간다.
    generation_revision: int = 0
    # 원고 단계가 따르는 편집 지시 11항목. 예전 계획에는 없던 필드라 기본값은 None이고,
    # 없으면 원고 프롬프트가 공통 규칙만 쓴다(저장된 옛 계획도 그대로 읽힌다).
    writing_direction: WritingDirection | None = None


class ThumbnailLayoutPlan(CamelModel):
    """대표 썸네일의 피사체·문구 배치. '피사체 중앙 + 문구 중앙'을 대체한다.

    subject_zone과 copy_zone은 겹치지 않는다 — 겹치면 코드가 문구를 반대편으로 옮긴다
    (imaging.resolve_thumbnail_layout). show_copy=False면 글자를 아예 얹지 않는다.
    """

    layout: str = "CENTER_COPY_ON_NEGATIVE_SPACE"
    subject_zone: str = "CENTER"
    copy_zone: str = "CENTER"
    copy_alignment: str = "CENTER"
    copy_mode: str = "SHORT_LABEL"
    copy_lines: list[str] = []
    scrim_style: str = "LOCAL_ROUNDED"
    accent_style: str = "NONE"
    show_copy: bool = True


class ContentPlanSection(CamelModel):
    """원고를 쓰기 전에 정하는 섹션 하나의 설계."""

    section_id: str
    heading: str
    # 이 섹션이 해결할 독자 질문. 소제목이 서로 다른 질문을 다루는지 설계 단계에서 검증한다.
    question: str
    purpose: str
    # 이 섹션에 반드시 들어가야 하는 구체적 정보.
    key_points: list[str] = []
    # 이 섹션이 인용할 검색/검증 출처 id(research sources의 순번 라벨).
    evidence_ids: list[str] = []
    visual_type: str = "NONE"
    visual_reason: str | None = None
    # 아래 여섯은 '소제목 목록'을 '무엇을 어디까지 쓸지'로 바꾸는 항목이다. 설계가 이걸
    # 정해 두지 않으면 원고 단계에서 매 섹션이 같은 분량·같은 구성으로 수렴한다.
    # 옛 설계에도 없던 필드라 전부 기본값이 있다(저장된 설계를 그대로 읽는다).
    #
    # 작성자의 판단이 필요한 지점. 자료를 나열만 하면 검색 결과 요약이 된다.
    interpretation: str = ""
    # 여기서는 설명하지 않고 넘어갈 배경. 섹션끼리 같은 배경을 되풀이하는 것을 막는다.
    omit_background: str = ""
    # 앞 섹션과 이어지는 이유. 없으면 섹션이 독립된 카드처럼 끊긴다.
    connection: str = ""
    # 전체 본문 대비 권장 분량 비중("25~35%" 형태). 목표가 아니라 배분 방향이다.
    length_share: str = ""
    # 이 섹션에서 화자가 드러낼 수 있는 관찰·디테일. 페르소나는 여기까지만 개입한다.
    persona_detail: str = ""
    # 이 섹션에서 하면 안 되는 주장(자료 밖 수치·경험 단정 등).
    forbidden_claims: list[str] = []
    # 아래 둘은 '이 섹션이 누구를 위한 것인가'를 설계에 못박는다(2026-08-05 미팅 2-2).
    # 연령대·독자 정보가 프롬프트에는 들어가는데 설계 결과에는 남지 않아, 원고 단계에서
    # 그 섹션이 누구의 무엇을 풀기로 했는지 알 길이 없었다. 옛 설계에는 없으므로 기본값은
    # 빈 문자열이고, 그때는 예전과 같이 동작한다.
    #
    # 이 섹션이 대상 독자의 어떤 필요를 푸는가(연령대 관심축 중 하나여야 한다).
    target_reader_need: str = ""
    # 이 섹션의 어조. 같은 글 안에서도 절차 설명과 판단 근거는 말투가 달라야 한다.
    tone_direction: str = ""


class ContentPlan(CamelModel):
    """본문 생성 전에 만드는 콘텐츠 설계. LLM이 곧바로 전체 본문을 쓰지 않고, 먼저 독자·
    문제·약속·섹션 구조·시각자료 계획을 정한 뒤 그 설계를 따라 원고를 쓴다."""

    target_reader: str
    reader_problem: str
    reader_question: str
    article_promise: str
    content_angle: str
    article_type: str = "INFORMATION"
    tone: str | None = None
    sections: list[ContentPlanSection] = []


class VisualDataPoint(CamelModel):
    label: str
    value: float


class VisualGroup(CamelModel):
    name: str
    items: list[str] = []


class VisualStep(CamelModel):
    """과정도의 한 단계.

    label은 무엇을 하는 단계인지(짧게), detail은 그 단계의 실제 값·계산식이다. 계산 과정을
    설명하는 그림에서 detail이 없으면 '작업 이름 나열'이 되어 독자가 숫자를 못 따라간다 —
    예: label='kW로 변환', detail='1,500 ÷ 1,000 = 1.5kW'.
    """

    label: str
    detail: str | None = None


class VisualTableRow(CamelModel):
    """비교표의 한 행. cells는 columns와 같은 순서·같은 길이여야 한다."""

    name: str
    cells: list[str] = []


class PlannedVisual(CamelModel):
    """코드로 렌더링할 시각자료(그래프·과정도·인포그래픽)의 구조화 데이터.

    정확한 한글이 필요한 자료는 이미지 모델에 텍스트를 맡기지 않는다 — 여기 담긴 데이터를
    PIL로 렌더링해 PNG로 만든다. 데이터를 함께 저장하므로 어떤 수치로 그렸는지 추적된다.
    그래프(BAR/LINE/PIE)는 실제 출처(source)와 수치(data)가 있을 때만 생성된다."""

    visual_id: str
    type: str
    title: str
    caption: str | None = None
    alt_text: str | None = None
    section_id: str | None = None
    # 그래프용 수치. 출처 자료의 실측값만 허용 — 서비스가 렌더링 전에 검증한다.
    data: list[VisualDataPoint] | None = None
    unit: str | None = None
    x_axis_label: str | None = None
    y_axis_label: str | None = None
    # 그래프에서 독자가 가져가야 할 결론 한 줄. 수치만 있고 해석이 없으면 그림이 일을 덜 한다.
    conclusion: str | None = None
    # 과정도용 단계 목록.
    steps: list[VisualStep] | None = None
    # 인포그래픽용 중심 주제와 그룹.
    center_topic: str | None = None
    groups: list[VisualGroup] | None = None
    # 비교표용 비교 기준(열)과 비교 대상(행). 모든 행이 같은 기준으로 채워져야 한다.
    columns: list[str] | None = None
    rows: list[VisualTableRow] | None = None
    source: str | None = None
    published_at: str | None = None
    # 색·톤 테마(VISUAL_THEMES). 편집 스타일 계획의 chart_theme이 기본이고, 모델이 자료마다
    # 다른 테마를 고르면 코드가 같은 글 안에서 하나로 통일한다(한 글은 한 계열).
    # 옛 문서의 EDITORIAL_NEUTRAL·LIFESTYLE_SOFT·TECH_MINIMAL·PROFESSIONAL_DATA도 계속 읽힌다.
    style: str | None = None
    # 같은 유형이라도 데이터 모양에 따라 다른 그림이 되게 하는 변형 이름. 비어 있으면
    # 렌더러가 데이터를 보고 고른다(짧은 라벨 → 세로 막대, 긴 라벨 → 가로 막대 등).
    layout_variant: str | None = None
    # conclusion과 연결된 항목. 최댓값을 무조건 강조하지 않고 여기 지정된 것을 짚는다.
    highlight_labels: list[str] = []
    # 여백·글자 크기의 밀도(COMFORTABLE/COMPACT). 없으면 유형별 기본값.
    density: str | None = None
    # 이 자료가 왜 필요한지(§3 루브릭의 근거). "한눈에 보여주려고" 같은 문장은 이유가 아니라
    # 판정에서 제외된다(visual_policy.is_vague_reason). 옛 데이터·구형 어댑터에는 없다(None).
    visual_reason: str | None = None
    # 시각자료 필요성 점수(0~100, §3 루브릭). 85점 미만은 만들지 않는다. 0은 '채점 안 함'이라
    # 점수만으로 버리지 않는다 — 옛 데이터와 스텁이 조용히 전멸하지 않게 한다.
    necessity_score: float = 0.0

    @field_validator("steps", mode="before")
    @classmethod
    def _accept_plain_steps(cls, value: Any) -> Any:
        """단계가 문자열이던 시절의 데이터도 그대로 읽는다.

        이미 저장된 원고 문서에는 steps가 문자열 배열로 들어 있다. 여기서 거르면 예전 글을
        불러오는 것 자체가 실패한다 — 형식이 바뀌었다고 남의 글이 안 열리면 안 된다.
        """
        if not isinstance(value, list):
            return value
        return [{"label": item} if isinstance(item, str) else item for item in value]


# 카드 아이콘 종류. PIL 렌더러(app/llm/cards.py)가 그릴 수 있는 것만 — 모델이 자유
# 문자열을 내면 그릴 수 없는 아이콘이 조용히 빠지므로 스키마가 이 집합으로 제약한다.
CARD_ICON_TYPES = (
    "info",
    "tip",
    "warning",
    "check",
    "compare",
    "money",
    "time",
    "location",
    "star",
    "search",
)

# 저장 호환성을 유지하는 사진 역할. THUMBNAIL은 글마다 정확히 1장,
# SECTION_CARD는 필요한 경우에만 만드는 본문 편집 사진이다. 이름은 기존 저장 문서와의
# 호환 때문에 유지하지만, 더 이상 카드뉴스 합성을 뜻하지 않는다.
CARD_TYPES = ("THUMBNAIL", "SECTION_CARD")


class CardScene(CamelModel):
    """카드 배경 장면의 상세 설계. 이미지 모델은 이 장면만 그린다 — 글자·아이콘·순번은
    전부 코드가 합성하므로, 여기에는 '무엇이 어디서 무엇을 하고 있는가'만 담는다."""

    main_subject: str
    secondary_subjects: list[str] = []
    action: str | None = None
    setting: str | None = None
    time_of_day: str | None = None
    weather: str | None = None
    foreground_objects: list[str] = []
    background_objects: list[str] = []
    supporting_props: list[str] = []
    camera_angle: str | None = None
    camera_distance: str | None = None
    # 구형 카드뉴스 데이터와의 저장 호환 필드. 자연 사진 경로에서는 사용하지 않는다.
    subject_position: str = "right"
    lighting: str | None = None
    color_mood: str | None = None
    material_details: list[str] = []
    environment_details: list[str] = []
    must_include: list[str] = []
    must_avoid: list[str] = []


class CardBrief(CamelModel):
    """카드 한 장의 상세 브리프. 원고가 완성된 뒤 시각자료 계획 단계가 만든다.

    articleClaim은 반드시 실제 원고에서 확인할 수 있는 문장이어야 한다 — 코드가 원고
    본문과 대조해 어긋나면 그 카드를 버린다(원고에 없는 주장을 이미지로 만들지 않는다).
    cardIndex/totalCards는 여기 없다: 최종 선정이 끝나 전체 장수가 확정된 뒤에 코드가
    배치 순서대로 매긴다.
    """

    card_id: str
    card_type: str = "SECTION_CARD"
    section_id: str | None = None
    section_heading: str | None = None
    article_claim: str
    visual_purpose: str
    # 아래 카드 문구·아이콘 필드는 저장된 구형 계획을 읽기 위한 호환 필드다. 신규 사진
    # 계획은 이 값을 만들지 않고, 썸네일 문구는 FinalPost.thumbnail_copy를 사용한다.
    eyebrow: str = ""
    headline_lines: list[str] = []
    emphasis_words: list[str] = []
    summary_lines: list[str] = []
    icon_type: str = "info"
    scene: CardScene
    # 장면 사실 확인용 검색어(2~4개, 구체적 장면 서술형). 검색 어댑터가 없으면 기록만 남는다.
    search_queries: list[str] = []
    # 검색/출처에서 확인된 시각적 사실 요약. 이미지 프롬프트의 factual grounding으로 쓴다.
    visual_reference_summary: str | None = None
    alt_text: str | None = None
    caption: str | None = None
    # 시각자료 필요성 점수(0~100, §3 루브릭). 75 미만은 선정하지 않고, 예산 초과 시
    # 낮은 것부터 제외한다.
    necessity_score: float = 0.0
    # 이 카드가 사용자가 올린 참고 이미지와 같은 대상을 그리는가. True면 그 참고 이미지를
    # 시각 기준으로 삼아 image-to-image로 장면을 생성한다(참고 이미지를 닮게). 참고 이미지가
    # 없으면 항상 False. 카드 계획 모델이 참고 이미지를 보고 판단한다.
    uses_reference: bool = False
    # --- 참고자료 충실도·사진 역할(2026-07-28) ---
    # 어느 참고 이미지를 기준으로 삼는가(reference-image-1 형식). 여러 장을 올렸을 때
    # 첫 장만 쓰지 않기 위한 값이다. 없으면 uses_reference가 True여도 첫 장을 쓴다.
    reference_id: str | None = None
    # 이 사진이 맡는 정보 역할(PHOTO_ROLES). 같은 대상을 다른 각도로만 반복하지 않게 한다.
    photo_role: str = "IN_USE_SCENE"
    # --- 구도(2026-08-05) ---
    # 이 사진이 실제로 보여 줘야 하는 구체적 대상 한 줄. 소재·선택 키워드·확정 제목·
    # 이 카드의 소제목과 문단을 함께 읽고 정한 결과다("디올"이 아니라 "레이디 디올 핸드백
    # 전체"). 비어 있으면 예전처럼 scene.main_subject만 쓴다(옛 계획 호환).
    visual_subject: str = ""
    # 얼마나 넓게 잡는가(PHOTO_FRAMINGS). 옛 계획에는 없으므로 기본값은 MEDIUM이고,
    # 검증에서 역할·썸네일 여부에 맞게 정규화된다(normalized_framing).
    framing: str = "MEDIUM"
    # 대상의 전체 형태가 프레임 안에 온전히 들어와야 하는가. framing에서 파생되므로
    # 계획 모델에 따로 묻지 않는다 — 두 값이 어긋날 자리를 만들지 않기 위해서다.
    show_complete_subject: bool = True
    # 이 사진을 어디서 구할지(IMAGE_SOURCES). 옛 문서·옛 계획에는 없으므로 기본값은
    # 빈 문자열 — 코드가 기본 사다리(네이버 검색 → 생성)로 처리한다.
    image_source: str = ""
    # 참고자료에서 확인된 대상의 정체(제품명·색상·형태). 참고자료가 없어도 소재·제목이
    # 특정 캐릭터·실제 인물을 가리키면 그 정확한 이름이 들어간다. 생성 이미지가 다른
    # 대상으로 바뀌지 않게 이미지 프롬프트에 그대로 실린다.
    subject_identity: str | None = None
    # --- 핵심 시각 대상(2026-07-31) ---
    # 이 사진의 핵심 대상이 어떤 종류인가(VISUAL_SUBJECT_KINDS). 옛 문서·옛 카드 계획에는
    # 없으므로 기본값은 NON_PERSON이다(예전과 같은 그림).
    subject_kind: str = "NON_PERSON"
    # 그 고유 대상 본인이 반드시 화면에 보여야 하는가. 고유 캐릭터·실제 인물이면 코드가
    # True로 정규화한다 — 모델이 false로 내려도 정체성을 잃지 않게 한다.
    must_show_subject: bool = False
    # 실존 인물·캐릭터 판정의 신뢰도(0~1). 낮은 값도 버리지 않고 기록만 한다 — 무엇을
    # 근거로 그 사람을 그렸는지 추적할 수 있어야 한다.
    identity_confidence: float = 0.0
    # 이 인물의 정체성 확인에 쓸 참고 이미지. 문서가 커지지 않도록 **이미지 자체가 아니라
    # 참고 이미지 id**(reference-image-1 형식)만 담는다. 실제 data URL은 생성 시점에
    # 서비스가 붙여 PostImageGenerationInput.reference_person_images로 넘긴다.
    reference_person_images: list[str] = []

    @model_validator(mode="after")
    def _normalize_subject(self) -> "CardBrief":
        kind, identity, must_show = normalized_subject_fields(
            self.subject_kind, self.subject_identity, self.must_show_subject
        )
        # model_validator(after)에서의 대입은 재검증을 부르지 않는다(무한 재귀 없음).
        object.__setattr__(self, "subject_kind", kind)
        object.__setattr__(self, "subject_identity", identity)
        object.__setattr__(self, "must_show_subject", must_show)
        # 구도도 같은 자리에서 못 박는다. 계획 모델이 대표 썸네일에 CLOSE_UP을 골라 와도
        # 표지에 손잡이만 실리지 않게 하는 것이 목적이다.
        framing = normalized_framing(
            self.framing, self.photo_role, is_thumbnail=self.card_type == "THUMBNAIL"
        )
        object.__setattr__(self, "framing", framing)
        object.__setattr__(self, "show_complete_subject", framing != "CLOSE_UP")
        return self
    # 반드시 보존해야 하는 제품 특징(색상·실루엣·소재·패키지 등).
    product_fidelity_requirements: list[str] = []
    # 이 사진이 보완하는 문단의 주장. article_claim과 달리 '무엇을 보여 주는가'다.
    section_claim: str | None = None
    # 앞뒤 사진과의 연속성(같은 공간·같은 광원 등). 한 글의 사진이 따로 놀지 않게 한다.
    visual_continuity: str | None = None
    # 생성인가 원본 재사용인가. REUSED면 이미지 모델을 부르지 않고 참고 이미지를 그대로 쓴다.
    generated_or_reused: str = "GENERATED"
    # 이 사진을 근거로 단정하면 안 되는 것.
    forbidden_inference: list[str] = []


class CardDesignSystem(CamelModel):
    """글 하나의 모든 카드가 공유하는 디자인 시스템. 카드마다 장면은 달라도 색·패널·
    아이콘 스타일은 같아야 한 글의 카드 시리즈로 읽힌다. 색은 소재·분위기에 맞춰 글마다
    정한다 — 참고 예시의 남색·노란색을 모든 글에 그대로 쓰지 않는다."""

    primary_color: str = "#1F2A44"
    secondary_color: str = "#2E3E63"
    accent_color: str = "#FFC845"
    headline_color: str = "#FFFFFF"
    panel_style: str = "gradient-mask"
    background_texture: str = "none"
    headline_font: str = "bold"
    body_font: str = "regular"
    icon_style: str = "line"
    divider_style: str = "accent-bar"
    progress_panel_style: str = "pill"
    image_mask_style: str = "gradient"


class VisualCardPlan(CamelModel):
    """원고 완성 후 만드는 사진 계획의 저장 호환 모델.

    기존 Mongo 문서의 cardPlan 형태를 바꾸지 않기 위해 이름과 필드를 유지한다. 신규 경로는
    designSystem과 카드 문구를 사용하지 않으며, 썸네일 1장과 실제로 필요한 본문 사진 0~N장만
    담는다.
    """

    design_system: CardDesignSystem = CardDesignSystem()
    cards: list[CardBrief] = []


class FinalPost(CamelModel):
    title: str
    body: str
    hashtags: list[str]
    images: list[GeneratedPostImage] | None = None
    featured_image: GeneratedPostImage | None = None
    # 대표 썸네일에 얹힌 문구. 이미지 안에 이미 구워져 있지만, 무엇이 적혔는지 화면에서
    # 확인하고 발행 시 요약으로도 쓸 수 있게 글자 자체를 따로 들고 있는다.
    # 문구 없는 썸네일(NO_COPY_EDITORIAL_PHOTO)도 정상이므로 빈 목록이 될 수 있다.
    thumbnail_copy: list[str] | None = None
    html_content: str
    markdown_content: str | None = None


#: 최종 검수가 잡아내는 문제의 종류.
#:
#: - ``fact``: 자료와 어긋나는 사실 진술(숫자·날짜·기능 설명이 근거와 다르다)
#: - ``unsupported``: 자료 어디에도 근거가 없는데 단정한 진술
#: - ``offtopic``: 소재와 이름만 같은 다른 대상 등, 이 글에 있을 이유가 없는 내용
#: - ``image``: 본문·자료와 맞지 않는 이미지
#: - ``missing``: 사용자가 준 입력(목적·연령대·선택한 방향·참고자료)이 본문에 반영되지 않음
#: - ``flow``: 문맥이 끊기거나 같은 말이 반복되거나 문장이 부자연스러움
#: - ``tone``: 블로그 글이 아니라 AI 답변·보고서처럼 읽히는 문구
FINAL_REVIEW_ISSUE_KINDS = (
    "fact",
    "unsupported",
    "offtopic",
    "image",
    "missing",
    "flow",
    "tone",
)


class FinalReviewIssue(CamelModel):
    """최종 검수가 찾은 문제 하나와, 그것을 고칠 방법.

    ``quote``/``replacement``가 핵심이다. 검수 모델이 '무엇이 틀렸다'까지만 말하면 그것을
    고치려고 원고를 통째로 다시 써야 한다 — 이미 만든 이미지와 구성까지 잃는다. 고칠 문장을
    함께 받아 그 자리만 바꾸면 모델 호출 한 번으로 검수와 교정이 끝난다.
    """

    kind: str
    #: critical만 교정한다. minor는 기록만 남긴다 — 취향 차이로 원고를 건드리지 않는다.
    severity: str
    reason: str
    #: 본문에 그대로 있는 연속된 문장. kind가 image면 비어 있다.
    quote: str = ""
    #: quote를 대신할 문장. 비우면 그 문장을 삭제한다는 뜻이다.
    replacement: str = ""
    #: kind가 image일 때, 문제가 된 이미지의 순번(0부터).
    image_index: int | None = None


#: 검수 항목. 2026-08-05 미팅 2-1이 적은 일곱 가지를 그대로 옮긴 것이고, 순서도 같다.
#:
#: **camelCase인 이유**: 이 값들은 모델 필드가 아니라 dict의 *키*다. pydantic의 별칭은
#: 필드에만 붙으므로 키는 여기 적은 그대로 저장되고 그대로 프런트에 나간다 — 저장·통신
#: 표기(camelCase)에 맞춰야 다른 필드와 어긋나지 않는다. 이름을 바꾸면 옛 문서가 읽히지
#: 않으므로 바꾸지 않는다.
FINAL_REVIEW_CHECK_KEYS = (
    "sentenceNaturalness",  # 1. 문장이 자연스러운지
    "paragraphCoherence",  # 2. 단락 간 연결이 어색하지 않은지
    "topicRelevance",  # 3. 소재와 무관한 내용이 포함되지 않았는지
    "titleBodyAlignment",  # 4. 제목에서 제시한 관점이 본문에 반영됐는지
    "imageRelevance",  # 5. 이미지가 원고 내용 및 해당 단락과 관련 있는지
    "factualUncertainty",  # 6. 사실관계가 불확실한 표현이 들어갔는지
    "aiLikeExpression",  # 7. AI가 작성한 것처럼 부자연스러운 표현
)

#: 항목별 판정. skipped는 '검사할 것이 없었다'다(이미지가 없는 글의 image_relevance).
#: 검사하지 못한 것과 통과한 것을 구분해야, 통과율만 보고 안심하는 일이 생기지 않는다.
FINAL_REVIEW_CHECK_STATUSES = ("pass", "warning", "fail", "skipped")

#: 글 전체 판정. revise는 '고칠 것이 있다'이지 '못 쓴다'가 아니다 — 원고는 언제나 나온다.
FINAL_REVIEW_OVERALL_STATUSES = ("pass", "warning", "revise")


class FinalReviewCheck(CamelModel):
    """검수 항목 하나의 판정.

    ``issues``가 '무엇을 어떻게 고칠지'라면 이쪽은 '항목별로 어땠는지'다. 둘 다 필요하다 —
    고칠 것이 없어도 어떤 항목을 무슨 근거로 통과시켰는지는 남아야 하고, 그래야 검수가
    실제로 돌았는지 결과만 보고 알 수 있다.
    """

    status: str = "skipped"
    #: 사람이 읽을 판정 근거 한 문장. 통과면 비어 있을 수 있다.
    reason: str = ""
    #: 문제가 걸린 자리. 섹션 id, 또는 이미지면 ``image-0`` 형태.
    affected_sections: list[str] = Field(default_factory=list)


class FinalReviewReport(CamelModel):
    """검수 **한 회차**가 돌려주는 것.

    저장 모델(``FinalReviewResult``)과 나눈 이유: 회차 수·실제 교정 결과·손댄 목록은 모델이
    아니라 코드가 아는 값이다. 한 그릇에 담으면 모델이 "3회 돌았다"고 적어 오는 것을 그대로
    저장하게 된다.
    """

    overall_status: str = "pass"
    overall_score: int = 100
    checks: dict[str, FinalReviewCheck] = Field(default_factory=dict)
    issues: list[FinalReviewIssue] = Field(default_factory=list)


class FinalReviewTarget(CamelModel):
    """검수가 **실제로 손댄 것**. 모델의 제안(issues)이 아니라 코드가 반영한 결과다.

    제안과 결과를 나누는 이유: 인용한 문장을 원고에서 못 찾으면 그 제안은 적용되지 않는다.
    그때 제안만 남기면 고쳐지지 않은 것을 고쳐졌다고 읽게 된다.
    """

    #: paragraph(본문 교정) | image(이미지 제외)
    kind: str
    #: 무엇을 손댔는지 알아볼 수 있는 짧은 표시(고친 문장 앞부분, 또는 image-N).
    reference: str
    #: rewritten(문장 교체) | removed(삭제)
    action: str
    #: 왜 손댔는지. 검수가 준 사유를 그대로 옮긴다.
    note: str = ""


class FinalReviewResult(CamelModel):
    """최종 검수 결과. 몇 회차를 돌았고 무엇을 보고 무엇을 고쳤는지 전부 남긴다."""

    reviewed_at: str
    provider: str
    model: str
    #: 실제로 돈 검수 횟수.
    rounds: int
    #: 글 전체 판정(pass | warning | revise). 옛 문서에는 없으므로 기본값을 둔다.
    overall_status: str = "pass"
    #: 0~100. 항목별 판정에서 코드가 계산한다 — 모델이 매기는 점수는 회차마다 흔들린다.
    overall_score: int = 100
    #: 항목별 판정. 키는 FINAL_REVIEW_CHECK_KEYS.
    checks: dict[str, FinalReviewCheck] = Field(default_factory=dict)
    #: 마지막 회차까지 **남은** 문제. 비어 있으면 더 고칠 것이 없다는 뜻이다.
    issues: list[FinalReviewIssue] = Field(default_factory=list)
    #: 실제로 손댄 것들. 화면의 '일부 표현 자동 수정'이 이 목록에서 나온다.
    revision_targets: list[FinalReviewTarget] = Field(default_factory=list)
    #: 실제로 원고에 반영한 교정 수.
    applied: int = 0
    #: 본문·자료와 맞지 않아 최종본에서 뺀 이미지 수.
    removed_images: int = 0
    #: 검수 자체가 실패했으면 그 사유. 원고는 그대로 쓰고 이 값만 남긴다.
    error: str | None = None
    #: 어떤 경로의 검수였는지. "critique-rewrite"는 비평 → 통합 재작성이 실제로 원고를
    #: 다시 쓴 경우다 — 그 재작성은 표현 다듬기 지침까지 안고 돌므로, 뒤의 별도 문장
    #: 다듬기 호출을 건너뛰는 근거가 된다(2026-08-07, 4단계가 4분 넘게 걸린 원인 중 하나).
    #: 옛 문서·예전 경로에는 없으므로 기본값 None이고, 그때는 다듬기가 예전대로 돈다.
    mode: str | None = None


#: 문장 다듬기(M4 5단계)가 고치는 표현의 종류.
#:
#: - ``assistant_tone``: '확인되는 범위는 다음과 같습니다'처럼 AI가 사람에게 답변할 때 쓰는 말투
#: - ``hedge``: '정확하지 않을 수 있습니다'처럼 책임을 피하려고 붙인 군더더기
#: - ``report_tone``: '본 글에서는 살펴보겠습니다'처럼 블로그가 아니라 보고서로 읽히는 문구
#: - ``repetition``: 같은 종결어미·접속어가 이어지거나 같은 말을 다시 하는 문장
#: - ``fake_experience``: 사용자가 주지 않은 경험을 겪은 것처럼 적은 문장
#: - ``awkward``: 뜻이 바로 잡히지 않거나 앞 문단과 이어지지 않는 문장
POLISH_EDIT_KINDS = (
    "assistant_tone",
    "hedge",
    "report_tone",
    "repetition",
    "fake_experience",
    "awkward",
)


class PolishEdit(CamelModel):
    """문장 다듬기가 제안한 교정 하나와, 그것이 실제로 반영됐는지.

    거절된 교정도 버리지 않고 결과에 남긴다. 이 단계는 사실을 건드리면 안 되는 자리라
    **무엇을 막았는지가 무엇을 고쳤는지만큼 중요하다** — 남기지 않으면 규칙이 지나치게
    빡빡한지 느슨한지를 나중에 판단할 근거가 사라진다.
    """

    kind: str
    reason: str
    #: 원고에 그대로 있는 문장(교정 전).
    before: str
    #: 그 자리를 대신할 문장(교정 후).
    after: str
    #: 실제로 원고에 반영됐는가.
    applied: bool = False
    #: 반영하지 않았다면 어느 규칙에 걸렸는가(코드가 붙인다). 반영했으면 None.
    rejected_rule: str | None = None


class PolishResult(CamelModel):
    """문장 다듬기(M4 5단계) 한 번의 결과. 무엇을 고쳤고 무엇을 막았는지 함께 남긴다."""

    polished_at: str
    provider: str
    model: str
    #: 실제로 원고에 반영한 교정 수.
    applied: int = 0
    #: 규칙에 걸려 반영하지 않은 교정 수.
    rejected: int = 0
    #: 제안된 교정 전부(반영·거절 모두). 변경 전후를 그대로 들고 있어 디버깅에 쓴다.
    edits: list[PolishEdit] = Field(default_factory=list)


class DraftGenerationResult(CamelModel):
    prompt_version: str
    provider: str
    model: str
    generated_at: str
    final_post: FinalPost
    # 이 원고를 쓰기 전에 만든 콘텐츠 설계. 어떤 구조·시각자료 계획으로 쓴 글인지
    # 추적할 수 있게 결과에 함께 저장한다. 설계 없이 쓴 글(폴백)은 None.
    content_plan: ContentPlan | None = None
    # 모델이 반환한 코드 렌더링 시각자료의 구조화 데이터. 렌더링된 PNG는 final_post의
    # images에 들어가고, 어떤 수치·구조로 그렸는지는 여기 남는다(편집·검증용).
    visuals: list[PlannedVisual] | None = None
    # 원고 완성 후 만든 시각자료 카드 계획(디자인 시스템 + 카드 브리프). 어떤 근거로
    # 몇 장을 만들었는지 추적할 수 있게 결과에 함께 저장한다. 계획 없이 만든 글은 None.
    card_plan: VisualCardPlan | None = None
    # 이 글의 편집·시각 스타일. **결과에 저장하는 것이 핵심이다** — 새로고침해도 같은
    # 디자인이 나오고, '다시 생성하기'에서만 다른 변형이 선택된다. 옛 문서에는 없다(None).
    editorial_style_plan: EditorialStylePlan | None = None
    # 참고자료를 근거 정보로 정리한 것. 원고·이미지·검증이 같은 사실을 본다.
    reference_evidence_profile: ReferenceEvidenceProfile | None = None
    # 대표 썸네일의 피사체·문구 배치. 옛 문서에는 없으므로 None이고, 그때는 중앙 배치를
    # 폴백으로 쓴다(예전과 같은 그림).
    thumbnail_layout_plan: ThumbnailLayoutPlan | None = None
    # 최종 검수(M4 4단계)가 무엇을 보고 무엇을 고쳤는지. 검수를 돌지 않았거나 실패한
    # 글, 그리고 이 단계가 생기기 전의 옛 문서는 None이다.
    final_review: FinalReviewResult | None = None
    # 문장 다듬기(M4 5단계)가 어떤 표현을 어떻게 고쳤는지. 고칠 것이 없었으면 edits가 빈
    # 목록이고, 단계를 돌지 않았거나 실패한 글과 옛 문서는 None이다.
    polish: PolishResult | None = None
