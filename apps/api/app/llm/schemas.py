"""provider가 출력을 제약하는 데 쓰는 JSON 스키마. 이 스키마는 통신에 실려 나가고
(OpenAI json_schema / Anthropic tool input_schema / Gemini responseSchema — 마지막 것은
to_gemini_schema가 방언을 맞춰 준다), draft 프롬프트에 문자열로도 박히므로
키 이름은 camelCase를 유지한다.
"""

from app.shared import (
    ARTICLE_RHYTHMS,
    ARTICLE_TYPES,
    BLOG_CATEGORIES,
    BODY_HIGHLIGHT_STYLES,
    CARD_TYPES,
    CONTENT_CATEGORIES,
    CONTENT_ENTITY_TYPES,
    DECORATION_LEVELS,
    EDITORIAL_ARCHETYPES,
    EMOJI_LEVELS,
    FINAL_REVIEW_CHECK_KEYS,
    FINAL_REVIEW_CHECK_STATUSES,
    FINAL_REVIEW_ISSUE_KINDS,
    FINAL_REVIEW_OVERALL_STATUSES,
    IMAGE_SOURCES,
    INFOGRAPHIC_VARIANTS,
    PHOTO_FRAMINGS,
    PHOTO_ROLES,
    PHOTO_SOURCE_MODES,
    POLISH_EDIT_KINDS,
    PROCESS_VARIANTS,
    REAL_IMAGE_TYPES,
    REFERENCE_IMAGE_ROLES,
    SECTION_PURPOSES,
    SOURCE_TYPES,
    TABLE_VARIANTS,
    THUMBNAIL_COPY_MODES,
    THUMBNAIL_LAYOUTS,
    TITLE_STRATEGIES,
    VISUAL_DENSITY_LEVELS,
    VISUAL_SUBJECT_KINDS,
    VISUAL_THEMES,
    VISUAL_TYPES,
    VOICE_MODES,
)

TITLE_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["titlePlan"],
    "properties": {
        "titlePlan": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "primaryTitle",
                "alternativeTitles",
                "h1",
                "primaryKeyword",
                "titleStrategy",
            ],
            "properties": {
                "primaryTitle": {
                    "type": "string",
                    "description": (
                        "이 글의 확정 제목. 20~35자(25자 안팎 권장)를 목표로 하고 45자를"
                        " 넘지 않는다. primaryKeyword를 자연스럽게 포함해야 한다."
                    ),
                },
                "alternativeTitles": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 4,
                    "items": {"type": "string"},
                    "description": (
                        "채택하지 않은 제목 후보. primaryTitle과 서로 다른 각도여야 한다."
                    ),
                },
                "h1": {
                    "type": "string",
                    "description": "본문 H1. primaryTitle과 정확히 같은 문자열을 쓴다.",
                },
                "primaryKeyword": {
                    "type": "string",
                    "description": (
                        "이 글이 노리는 핵심 검색 구문. 검색자가 실제로 입력할 법한 표현이어야"
                        " 하고, primaryTitle 안에 그대로(띄어쓰기 차이는 허용) 들어 있어야 한다."
                    ),
                },
                "titleStrategy": {"type": "string", "enum": list(TITLE_STRATEGIES)},
            },
        }
    },
}

# 원고를 쓰기 전에 만드는 SEO 키워드 계획. primary는 제목이 노리는 핵심 검색 구문과
# 맞아야 하고(코드가 title_plan.primary_keyword로 고정), secondary·avoid는 모델이 넓힌다.
SEO_KEYWORD_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["seoKeywordPlan"],
    "properties": {
        "seoKeywordPlan": {
            "type": "object",
            "additionalProperties": False,
            "required": ["primary", "secondary", "avoid"],
            "properties": {
                "primary": {
                    "type": "string",
                    "description": (
                        "글에서 가장 중심이 되는 SEO 키워드 하나. 최종 주제·제목과 직접"
                        " 관련되고, 제목과 첫 문단에 자연스럽게 쓸 수 있는 표현이어야 한다."
                        " 검색량만 높고 주제와 무관한 키워드나 지나치게 넓은 일반 명사는 고르지"
                        " 않는다."
                    ),
                },
                "secondary": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 8,
                    "items": {"type": "string"},
                    "description": (
                        "primary를 보완하는 관련 검색어 3~8개. 검색 의도·세부 정보·비교 기준"
                        " 등으로 확장하되, primary와 의미가 완전히 겹치거나 조사·띄어쓰기만"
                        " 다른 것은 넣지 않는다."
                    ),
                },
                "avoid": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 8,
                    "items": {"type": "string"},
                    "description": (
                        "본문에서 쓰지 않아야 하는 표현. 다른 카테고리의 키워드, 동음이의어로"
                        " 잘못 연결될 수 있는 표현, 참고자료 문맥과 맞지 않는 표현, 과장·확인"
                        " 불가 표현을 담는다. 없으면 빈 배열."
                    ),
                },
            },
        }
    },
}

# 참고자료를 근거 정보로 바꾼 프로필. 원고·이미지·검증이 같은 사실을 보게 하는 것이 목적이라,
# '보이는 것'과 '단정하면 안 되는 것'을 반드시 나눠 받는다.
# 소재가 실제로 무엇인지 확정하는 블록. 참고자료 근거와 같은 호출에서 함께 받는다 —
# 두 판단 모두 '이 글이 붙잡아야 할 대상은 무엇인가'이고, 호출을 하나 더 늘리면 원고
# 생성이 그만큼 늦어진다.
CONTENT_ENTITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "entityType",
        "primaryCategory",
        "secondaryCategory",
        "writingMode",
        "canonicalName",
        "brand",
        "platform",
        "officialChannel",
        "relatedPeople",
        "coreFormat",
        "primaryActivities",
        "secondaryActivities",
        "backgroundScenes",
        "officialVideoQueries",
        "naturalPhrases",
        "forbiddenPhrases",
        "requiresFreshResearch",
        "requiresRealImages",
        "realImageType",
        "confidence",
    ],
    "properties": {
        "entityType": {
            "type": "string",
            "enum": list(CONTENT_ENTITY_TYPES),
            "description": (
                "소재가 무엇인가. 일반 명사와 작품명이 같은 소재는 검색 결과와 사용자가"
                " 고른 키워드를 함께 보고 확정한다 — 사람 이름이 함께 검색된 일반 명사는"
                " 그 일반 명사의 사전적 의미가 아니라 작품·프로그램일 가능성이 높다."
                " 가능한 한 구체적인 값을 고른다(제품이면 PRODUCT_OR_SERVICE보다"
                " BRAND_MENU_ITEM·TECH_PRODUCT·CAR_MODEL처럼 실제 종류를)."
                " 확신할 수 없으면 GENERAL_TOPIC."
            ),
        },
        "primaryCategory": {
            "type": "string",
            "enum": list(BLOG_CATEGORIES),
            "description": (
                "이 글의 메인 카테고리. 글의 **전체 구조**가 여기서 결정된다. 소재가 아니라"
                " '독자가 무엇을 알고 싶어 하는가'로 고른다 — 같은 프랜차이즈 신메뉴라도"
                " 제품 정보가 중심이면 상품리뷰, 특정 지점 방문이 중심이면 맛집이다."
            ),
        },
        "secondaryCategory": {
            "type": "string",
            "enum": ["", *BLOG_CATEGORIES],
            "description": (
                "보조 카테고리. 문체·정보·이미지 지침을 보완하는 용도로만 쓰인다."
                " 글의 중심이 하나뿐이면 빈 문자열. 메인과 같은 값을 넣지 않는다."
            ),
        },
        "writingMode": {
            "type": "string",
            "description": (
                "이 글을 어떤 형태로 쓰는가(예: 신제품 정보형, 시청 포인트형, 방문 전"
                " 참고형, 구매 전 확인형, 개념 설명형). 사용자가 실제 경험 자료를 주지"
                " 않았으면 반드시 정보형·소개형 계열이어야 한다 — 후기형은 쓸 수 없다."
            ),
        },
        "canonicalName": {
            "type": "string",
            "description": (
                "검색으로 확인된 정식 명칭(프로그램명·작품명·제품명). 사용자가 입력한"
                " 검색어 조합을 그대로 옮기지 않는다. 확인되지 않으면 빈 문자열."
            ),
        },
        "brand": {
            "type": "string",
            "description": (
                "확인된 브랜드·제조사·출판사·개발사·제작 주체. 상품·메뉴·차량·게임처럼"
                " '어느 브랜드의 무엇'인지가 갈리는 소재에서는 반드시 채운다."
                " 확인되지 않으면 빈 문자열."
            ),
        },
        "platform": {
            "type": "string",
            "description": "공개 플랫폼·매체(YouTube, 넷플릭스, 지상파 등). 없으면 빈 문자열.",
        },
        "officialChannel": {
            "type": "string",
            "description": "공식 채널명 또는 제작 주체. 확인되지 않으면 빈 문자열.",
        },
        "relatedPeople": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "relation"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "확인된 사람의 공식 이름. 검색어에 축약명이 왔더라도 근거가"
                            " 있으면 공식 이름으로 적는다."
                        ),
                    },
                    "relation": {
                        "type": "string",
                        "description": "이 콘텐츠와의 관계(출연자·진행자·제작자 등).",
                    },
                },
            },
        },
        "coreFormat": {
            "type": "string",
            "description": (
                "매 회차 반복되는 핵심 포맷 한 줄. 회차마다 달라지는 소재가 아니라"
                " 프로그램의 정체성이다. 확인되지 않으면 빈 문자열 — 지어내지 않는다."
            ),
        },
        "primaryActivities": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string"},
            "description": "회차마다 반복되는 핵심 활동. 글의 중심이 될 것들.",
        },
        "secondaryActivities": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string"},
            "description": (
                "회차에 따라 등장하는 보조 활동. 설명은 할 수 있지만 프로그램의 핵심"
                " 포맷처럼 다루면 안 되는 것들."
            ),
        },
        "backgroundScenes": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string"},
            "description": "중심으로 다루면 콘텐츠를 잘못 설명하게 되는 부수 장면.",
        },
        "officialVideoQueries": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string"},
            "description": (
                "이 대상의 **실제 사진·썸네일**을 찾을 검색어. 정밀한 것부터:"
                " 브랜드+정식명, 정식명+주요 인물, 정식명+공식 채널. 실존 대상이"
                " 아니면 빈 배열. (필드 이름은 옛 저장 호환으로 남아 있다 —"
                " 영상만이 아니라 상품·인물·장소에도 쓴다.)"
            ),
        },
        "naturalPhrases": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string"},
            "description": (
                "글의 문장·제목에 그대로 쓸 수 있는 자연스러운 표현. 검색어 조합이 아니라"
                " 관계를 풀어 쓴 한국어 명사구여야 한다"
                "(예: 'A가 출연하는 B', '유튜브 웹예능 B', 'B의 멤버 A')."
            ),
        },
        "forbiddenPhrases": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string"},
            "description": (
                "이 소재에서 쓰면 안 되는 표현. 사용자의 검색어 조합을 하나의 명사처럼"
                " 쓴 형태를 반드시 포함한다."
            ),
        },
        "requiresFreshResearch": {
            "type": "boolean",
            "description": (
                "날짜·가격·사양·일정·기록처럼 시점에 따라 달라지는 사실이 이 글의 중심인가."
                " true인데 확인된 출처가 없으면 본문은 그 수치를 단정하지 않고 확인이"
                " 필요하다고 밝혀야 한다."
            ),
        },
        "requiresRealImages": {
            "type": "boolean",
            "description": (
                "생성 이미지로 대체하면 안 되는 실존 대상인가. 실제 상품·프로그램·인물·"
                "장소·작품이면 true. 추상적 개념·일반적인 생활 상황이면 false."
            ),
        },
        "realImageType": {
            "type": "string",
            "enum": list(REAL_IMAGE_TYPES),
            "description": (
                "구해야 하는 실물 이미지의 종류. requiresRealImages가 false면 NONE."
                " 유튜브·방송 프로그램은 OFFICIAL_VIDEO_THUMBNAIL, 영화·드라마·공연은"
                " OFFICIAL_POSTER_OR_STILL, 상품·메뉴는 OFFICIAL_PRODUCT_IMAGE,"
                " 인물·그룹은 OFFICIAL_PERSON_PHOTO, 장소·매장은 OFFICIAL_PLACE_PHOTO,"
                " 책·앨범은 OFFICIAL_COVER_ART, 게임·소프트웨어는 OFFICIAL_SCREENSHOT."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "이 판정의 근거 세기(0~1). 검색 출처가 없으면 낮게 둔다.",
        },
    },
}

REFERENCE_EVIDENCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["referenceEvidenceProfile"],
    "properties": {
        "referenceEvidenceProfile": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "primaryEntity",
                "brand",
                "productCategory",
                "confirmedAttributes",
                "confirmedUseScenes",
                "referenceImageRoles",
                "sourceFacts",
                "forbiddenClaims",
                "contentEntity",
            ],
            "properties": {
                "contentEntity": CONTENT_ENTITY_SCHEMA,
                "primaryEntity": {
                    "type": ["string", "null"],
                    "description": (
                        "참고자료가 확인해 주는 이 글의 실제 대상(제품명·모델명·장소명)."
                        " 자료에서 확인되지 않으면 null. 소재 입력값을 그대로 옮기지 않는다."
                    ),
                },
                "brand": {
                    "type": ["string", "null"],
                    "description": "확인된 브랜드명. 자료에 없으면 null.",
                },
                "productCategory": {
                    "type": ["string", "null"],
                    "description": "확인된 제품·서비스 분류(운동화, 립 제품, 노트북 등). 없으면 null.",
                },
                "confirmedAttributes": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string"},
                    "description": (
                        "이미지에 실제로 보이거나 URL·출처에 실제로 적힌 특징만."
                        " 색상·형태·소재·패키지처럼 눈으로 확인되는 것. 추정은 넣지 않는다."
                    ),
                },
                "confirmedUseScenes": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {"type": "string"},
                    "description": "자료에서 확인되는 장면(제품 단독 사진, 착용 사진 등).",
                },
                "referenceImageRoles": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "referenceId",
                            "role",
                            "subject",
                            "allowedUses",
                            "forbiddenInferences",
                            "privateRegions",
                        ],
                        "properties": {
                            "referenceId": {
                                "type": "string",
                                "description": (
                                    "reference-image-1 형식. 첨부된 순서와 정확히 같아야 한다."
                                ),
                            },
                            "role": {"type": "string", "enum": list(REFERENCE_IMAGE_ROLES)},
                            "subject": {
                                "type": "string",
                                "description": "그 이미지에서 실제로 보이는 대상 한 줄(한국어).",
                            },
                            "allowedUses": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "원본 재사용·썸네일 배경 확장·제품 중심 크롭 등.",
                            },
                            "forbiddenInferences": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "이 이미지에서 끌어내면 안 되는 추정(가격·사용 기간·착용감 등)."
                                ),
                            },
                            # 필수로 둔다(비어 있어도 배열은 보낸다). 선택 항목으로 두면
                            # 모델이 대개 생략하고, 그러면 '찾아봤는데 없었다'와 '아예 안
                            # 봤다'를 구분할 수 없다. 개인정보는 그 차이가 중요하다.
                            "privateRegions": {
                                "type": "array",
                                "maxItems": 8,
                                "description": (
                                    "이 이미지에서 **개인을 특정할 수 있는 글자**가 보이는 자리."
                                    " 차량 번호판, 전화번호, 생년월일, 주민등록번호, 카드번호,"
                                    " 계좌번호, 집 주소, 이름표·명찰, 택배 송장이 해당한다."
                                    " 하나도 없으면 빈 배열. 얼굴·상표·건물은 개인정보가"
                                    " 아니므로 넣지 않는다."
                                ),
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["kind", "x", "y", "width", "height"],
                                    "properties": {
                                        "kind": {
                                            "type": "string",
                                            "description": (
                                                "무엇인지 한 단어(번호판·전화번호·생년월일 등)."
                                            ),
                                        },
                                        "x": {
                                            "type": "number",
                                            "description": (
                                                "왼쪽 끝. 이미지 **가로 대비 0~1 비율**이다"
                                                "(픽셀이 아니다)."
                                            ),
                                        },
                                        "y": {
                                            "type": "number",
                                            "description": "위쪽 끝. 이미지 세로 대비 0~1 비율.",
                                        },
                                        "width": {
                                            "type": "number",
                                            "description": (
                                                "가로 길이(0~1 비율)."
                                                " 글자가 잘리면 안 되므로 **넉넉히** 잡는다."
                                            ),
                                        },
                                        "height": {
                                            "type": "number",
                                            "description": "세로 길이(0~1 비율). 넉넉히 잡는다.",
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
                "sourceFacts": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string"},
                    "description": "참고 URL·출처에서 확인된 사실 한 줄씩. 없으면 빈 배열.",
                },
                "forbiddenClaims": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string"},
                    "description": (
                        "이 자료로는 뒷받침되지 않아 본문에서 쓰면 안 되는 표현."
                        " 사용자가 실제 경험을 적지 않았으면 구매·사용 기간·측정 표현을 반드시 넣는다."
                    ),
                },
            },
        }
    },
}


# 원고를 쓸 때 그대로 실행하는 편집 지시. 편집 계획이 '친근하게'·'전문적으로'만 남기면
# 원고 단계에서는 아무것도 달라지지 않는다 — 그래서 모든 항목의 설명에 '실행할 수 있는
# 문장'을 요구하고, 형용사만 있는 답의 예를 함께 적는다.
#
# 도입과 결말은 '형식'과 '내용'을 나눠 갖는다. 형식(어떤 방식으로 열고 닫는가)은 코드가
# 회전으로 정하고(article_rhythm, title_variation.closing_mode), 여기서는 그 자리에 들어갈
# 내용만 정한다. 형식까지 모델이 고르면 아키타입이 같은 글은 매번 같은 도입·결말이 된다.
_EXECUTABLE = "형용사가 아니라 원고에서 그대로 실행할 수 있는 한국어 문장 1~2개."
WRITING_DIRECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "voiceDistance",
        "readerRelationship",
        "sentenceDensity",
        "openingMode",
        "rhythmProfile",
        "transitionStyle",
        "detailFocus",
        "firstPersonPolicy",
        "certaintyPolicy",
        "closingMode",
        "avoidPatterns",
    ],
    "properties": {
        "voiceDistance": {
            "type": "string",
            "description": (
                "화자가 독자와 두는 거리. " + _EXECUTABLE + " 예: '설명은 3인칭으로 하고,"
                " 판단이 필요한 대목에서만 화자의 견해를 드러낸다.'"
            ),
        },
        "readerRelationship": {
            "type": "string",
            "description": (
                "독자에게 말을 거는 방식과 그 빈도. " + _EXECUTABLE + " 예: '매 문단 질문하지"
                " 않고, 선택이 필요한 구간에서만 직접 말을 건다.'"
            ),
        },
        "sentenceDensity": {
            "type": "string",
            "description": (
                "한 문장에 담을 정보량. " + _EXECUTABLE + " 예: '핵심 판단은 한두 문장 안에"
                " 먼저 제시하고, 조건과 예외는 뒤 문단에서 설명한다.'"
            ),
        },
        "openingMode": {
            "type": "string",
            "description": (
                "도입 첫 문단이 무엇으로 시작하는지. 위에서 고른 articleRhythm을 이 글의"
                " 소재로 풀어 쓴다(다른 흐름으로 바꾸지 않는다). " + _EXECUTABLE
            ),
        },
        "rhythmProfile": {
            "type": "string",
            "description": (
                "문장 길이가 의미에 따라 어떻게 달라지는지. " + _EXECUTABLE + " '짧은 문장과"
                " 긴 문장을 번갈아 쓴다' 같은 기계적 교대는 답이 아니다."
            ),
        },
        "transitionStyle": {
            "type": "string",
            "description": (
                "섹션과 문단을 넘어가는 방식. " + _EXECUTABLE + " '먼저 → 다음으로 →"
                " 마지막으로' 같은 목차형 연결어를 지정하지 않는다."
            ),
        },
        "detailFocus": {
            "type": "string",
            "description": (
                "이 글에서 구체적으로 쓸 것과 넘어갈 것. " + _EXECUTABLE + " 자료에 있는"
                " 범위 안에서만 정한다."
            ),
        },
        "firstPersonPolicy": {
            "type": "string",
            "description": (
                "1인칭·경험 표현을 어디까지 허용할지. 참고자료에 사용자의 실제 사용 경험이"
                " 없으면 '1인칭 체험 표현을 사용하지 않는다'로 정한다. " + _EXECUTABLE
            ),
        },
        "certaintyPolicy": {
            "type": "string",
            "description": (
                "무엇을 단정하고 무엇을 조건과 함께 쓸지. " + _EXECUTABLE + " 예: '출처가"
                " 있는 수치는 단정하고, 사용 환경에 따라 달라지는 것은 조건을 함께 쓴다.'"
            ),
        },
        "closingMode": {
            "type": "string",
            "description": (
                "결말에서 독자가 무엇을 들고 나가야 하는지. 결말의 **형식**은 원고 단계가"
                " 따로 지정하므로 여기서는 형식을 정하지 않고 내용만 정한다. 본문 요약은"
                " 답이 아니다. " + _EXECUTABLE
            ),
        },
        "avoidPatterns": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string"},
            "description": (
                "이 글에서 특히 나오기 쉬운 기계적 패턴 3~6개. 일반론이 아니라 이 소재·목적에서"
                " 실제로 나올 만한 것을 적는다. 예: '제습기 글이라 모든 섹션을 청소 절차로 끝내기'."
            ),
        },
    },
}


# 글 하나의 편집·시각 스타일. 카테고리·아키타입은 의미 판단이라 모델이 정하고, 테마·팔레트·
# 레이아웃 변형은 코드가 결정적으로 고른다(modules/draft/editorial_style.py). 그래서 여기에는
# '무엇에 대한 어떤 형태의 글인가'와 상한, 그리고 원고가 실행할 편집 지시를 받는다.
EDITORIAL_STYLE_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["editorialStylePlan"],
    "properties": {
        "editorialStylePlan": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "contentCategory",
                "editorialArchetype",
                "voiceMode",
                "visualDensity",
                "emojiLevel",
                "decorationLevel",
                "articleRhythm",
                "bodyHighlightStyle",
                "thumbnailLayout",
                "thumbnailCopyMode",
                "visualBudget",
                "writingDirection",
                "rationale",
            ],
            "properties": {
                "contentCategory": {
                    "type": "string",
                    "enum": list(CONTENT_CATEGORIES),
                    "description": (
                        "목적·소재·참고자료로 판단한다. 페르소나만으로 정하지 않는다 —"
                        " 같은 '체험 후기 리뷰어'라도 화장품은 BEAUTY, 러닝화는 FITNESS_SPORTS,"
                        " 노트북은 TECH_IT, 카페는 FOOD 또는 LOCAL_LIFE다."
                    ),
                },
                "editorialArchetype": {
                    "type": "string",
                    "enum": list(EDITORIAL_ARCHETYPES),
                    "description": (
                        "글의 형태. 소재와 목적에 맞는 것을 고른다."
                    ),
                },
                "voiceMode": {"type": "string", "enum": list(VOICE_MODES)},
                "visualDensity": {"type": "string", "enum": list(VISUAL_DENSITY_LEVELS)},
                "emojiLevel": {"type": "string", "enum": list(EMOJI_LEVELS)},
                "decorationLevel": {"type": "string", "enum": list(DECORATION_LEVELS)},
                "articleRhythm": {
                    "type": "string",
                    "enum": list(ARTICLE_RHYTHMS),
                    "description": (
                        "글이 흐르는 방식. 모든 글을 '현재 상황 → 불편 → 질문 → 소재 소개'로"
                        " 시작하지 않는다."
                    ),
                },
                "bodyHighlightStyle": {"type": "string", "enum": list(BODY_HIGHLIGHT_STYLES)},
                "thumbnailLayout": {
                    "type": "string",
                    "enum": list(THUMBNAIL_LAYOUTS),
                    "description": (
                        "피사체와 문구가 겹치지 않는 배치. 일상·뷰티·음식 글은 문구 없는"
                        " NO_COPY_EDITORIAL_PHOTO도 좋은 선택이다."
                    ),
                },
                "thumbnailCopyMode": {"type": "string", "enum": list(THUMBNAIL_COPY_MODES)},
                "visualBudget": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["bodyPhotosMax", "renderedVisualsMax", "referenceImagesMax"],
                    "properties": {
                        "bodyPhotosMax": {"type": "integer", "minimum": 0, "maximum": 4},
                        "renderedVisualsMax": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 3,
                            "description": (
                                "표·그래프·과정도·인포그래픽의 상한. **최소가 아니다** —"
                                " 0이 정상인 글이 많다. 목적 정책보다 크게 잡아도 코드가 자른다."
                            ),
                        },
                        "referenceImagesMax": {"type": "integer", "minimum": 0, "maximum": 2},
                    },
                },
                "writingDirection": WRITING_DIRECTION_SCHEMA,
                "rationale": {
                    "type": "string",
                    "description": "이 카테고리·아키타입을 고른 이유 한 문장(한국어).",
                },
            },
        }
    },
}


#: M3이 보여 주는 글 방향 후보 수(2026-08-11 3 → 4).
#:
#: 한 번에 만들 수 있는 원고가 최대 3편이므로 후보는 그보다 하나 많다. 3편을 만들 때도
#: **하나는 버리는 선택**이 되어야 고르는 일이 형식적이지 않다. 후보를 더 늘리지 않은
#: 이유는 한 소재에서 진짜로 다른 각도가 무한히 나오지 않기 때문이다 — 숫자를 키우면
#: 말만 바꾼 중복 후보가 섞인다.
INTENT_CANDIDATE_COUNT = 4

INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intentCandidates"],
    "properties": {
        "intentCandidates": {
            "type": "array",
            # 개수는 프롬프트가 아니라 **여기가 정한다**(2026-08-12). 프롬프트에만 4개라고
            # 적고 이 값을 3으로 두었더니 모델이 3개만 돌려주었다 — 실제로 그랬다.
            # prompts.INTENT_CANDIDATE_COUNT와 같은 값이어야 하고, 그것은 아래 테스트가 지킨다.
            "minItems": INTENT_CANDIDATE_COUNT,
            "maxItems": INTENT_CANDIDATE_COUNT,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "targetReader", "rationale", "keywords", "sources"],
                "properties": {
                    # 제목이 아니라 '글의 방향'이다. 제목은 M2에서 사용자가 고르거나 M4가
                    # 확정한다 — 이 칸을 제목처럼 채우면 검증 화면에서 사용자가 자기가 고른
                    # 제목과 혼동한다. 스키마 설명으로도 한 번 더 못박는다.
                    "title": {
                        "type": "string",
                        "description": (
                            "이 글을 어떤 각도로 풀지 가리키는 짧은 명사구(8~20자). "
                            "제목이나 완성된 문장이 아니다. 예: '재개 일정과 관람 정보'."
                        ),
                    },
                    "targetReader": {"type": "string"},
                    "rationale": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "sources": {
                        "type": "array",
                        # 자료는 5개까지. 상한만 두고 하한은 두지 않는다 — 실제로 쓸 만한
                        # 출처가 5개보다 적을 때 숫자를 맞추려고 URL을 지어내는 것보다,
                        # 있는 만큼만 받는 편이 낫다. 모델이 하나도 고르지 않았을 때만
                        # 수집한 실제 자료로 대신한다(live_adapters._sources_for_candidate).
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            # **모든 키를 required에 넣는다.** 2026-08-07에 M3 정리가
                            # Gemini로 옮겨 가면서 이 스키마를 강제하는 쪽도 Gemini의
                            # responseSchema가 됐지만, 규칙은 그대로 지킨다: 선택 항목을
                            # 빼는 대신 빈 배열/null로 받는 편이 파서를 단순하게 하고,
                            # 이 스키마가 OpenAI strict로 다시 갈 때도 그대로 통과한다
                            # (strict는 properties의 모든 키가 required여야 하고, 하나라도
                            # 빠지면 요청 자체가 400이다).
                            # 제목·URL·스니펫은 모델이 다시 베끼지 않는다 — 이미 프롬프트의
                            # 'Gemini sources' 목록에 있는 것을 출력으로 복사하는 일은 순수한
                            # 출력 토큰 낭비였다(스니펫이 길어 요약 시간의 큰 몫). 대신 목록
                            # 번호(sourceIndex)로 가리키고, 코드가 원본 출처와 합친다.
                            # 판단이 필요한 값(sourceType·relevanceScore·dataPoints)과, 이
                            # 출처가 무슨 내용인지 한 줄 요약(summary)만 남긴다. 요약은 제목·URL
                            # 재복사와 달리 짧아 토큰 비용이 작으면서, 검증 화면에서 자료마다
                            # "무슨 내용인지"를 보여 준다(수집 grounding의 cited_text가 비어도
                            # 요약은 항상 채워진다).
                            "required": [
                                "sourceIndex",
                                "summary",
                                "sourceType",
                                "relevanceScore",
                                "dataPoints",
                            ],
                            "properties": {
                                "sourceIndex": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "'Gemini sources' 목록의 번호(1부터). 목록에 없는"
                                        " 번호를 만들지 않는다."
                                    ),
                                },
                                "summary": {
                                    "type": "string",
                                    "description": (
                                        "이 출처가 무슨 내용인지 한 줄 요약(한국어, 60자 이내)."
                                        " 아래 제공된 Snippet에 근거해 쓰고, 없는 내용을 지어내지"
                                        " 않는다. 검증 화면에서 자료 옆에 표시된다."
                                    ),
                                },
                                "sourceType": {
                                    "type": "string",
                                    "enum": list(SOURCE_TYPES),
                                    "description": (
                                        "자료 성격: OFFICIAL(공식자료)·NEWS(뉴스)·"
                                        "BLOG(블로그·후기)·REPORT(통계·보고서)·CASE(활용 사례)"
                                    ),
                                },
                                "relevanceScore": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 100,
                                    "description": "소재·트렌드·대상 독자와의 관련도(0-100).",
                                },
                                # 자료에 실린 실측 수치. 원고의 통계 문장·그래프는 여기
                                # 담긴 숫자만 쓸 수 있다 — 없는 수치를 지어내지 못하게
                                # 원천에서 붙잡아 둔다. 수치가 없는 자료는 빈 배열.
                                "dataPoints": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        # unit도 required에 넣되, 단위가 없는 수치를 위해
                                        # null을 허용한다(strict에서 '선택 항목'을 표현하는
                                        # 방법은 제외가 아니라 nullable이다).
                                        "required": ["label", "value", "unit"],
                                        "properties": {
                                            "label": {
                                                "type": "string",
                                                "description": "수치의 이름(예: 2025년 이용률).",
                                            },
                                            "value": {"type": "number"},
                                            "unit": {
                                                "type": ["string", "null"],
                                                "description": "단위(%, 명, 억 원 등). 없으면 null.",
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}

# 원고를 쓰기 전에 만드는 콘텐츠 설계. 곧바로 본문을 쓰면 제목이 약속한 내용이 빠지거나
# 같은 장점이 반복되므로, 독자·문제·약속·섹션 구조·시각자료 계획을 먼저 확정한다.
CONTENT_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contentPlan"],
    "properties": {
        "contentPlan": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "targetReader",
                "readerProblem",
                "readerQuestion",
                "articlePromise",
                "contentAngle",
                "articleType",
                "tone",
                "sections",
            ],
            "properties": {
                "targetReader": {"type": "string", "description": "이 글의 핵심 대상 독자."},
                "readerProblem": {
                    "type": "string",
                    "description": "대상 독자가 겪고 있는 구체적인 문제.",
                },
                "readerQuestion": {
                    "type": "string",
                    "description": "이 글을 검색한 사람이 가장 궁금해하는 질문.",
                },
                "articlePromise": {"type": "string", "description": "글을 읽은 후 얻는 결과."},
                "contentAngle": {
                    "type": "string",
                    "description": "소재와 트렌드를 연결하는 핵심 관점.",
                },
                "articleType": {"type": "string", "enum": list(ARTICLE_TYPES)},
                "tone": {"type": "string", "description": "선택된 페르소나에 맞는 문체."},
                "sections": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "sectionId",
                            "heading",
                            "question",
                            "purpose",
                            "keyPoints",
                            "evidenceIds",
                            "visualType",
                            "visualReason",
                            "interpretation",
                            "omitBackground",
                            "connection",
                            "lengthShare",
                            "personaDetail",
                            "forbiddenClaims",
                            "targetReaderNeed",
                            "toneDirection",
                        ],
                        "properties": {
                            "sectionId": {
                                "type": "string",
                                "description": "section-1부터 순번.",
                            },
                            "heading": {
                                "type": "string",
                                "description": (
                                    "독자의 질문이나 핵심 메시지 형태의 소제목. 'OO 소개'"
                                    " 같은 명사 나열 금지."
                                ),
                            },
                            "question": {
                                "type": "string",
                                "description": "이 섹션에서 해결할 질문. 섹션끼리 겹치면 안 된다.",
                            },
                            "purpose": {"type": "string", "enum": list(SECTION_PURPOSES)},
                            "keyPoints": {"type": "array", "items": {"type": "string"}},
                            "evidenceIds": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "이 섹션이 인용할 출처 id(source-1 형식). 없으면 빈 배열.",
                            },
                            # 자연 사진은 완성 원고 뒤의 사진 계획이 정한다. 콘텐츠 설계는
                            # 정확한 한글·수치가 필요한 코드 렌더 자료만 선택한다.
                            "visualType": {
                                "type": "string",
                                "enum": [value for value in VISUAL_TYPES if value != "PHOTO"],
                            },
                            "visualReason": {
                                "type": "string",
                                "description": (
                                    "이 시각자료가 설명에 왜 필요한지. NONE이면 빈 문자열."
                                ),
                            },
                            # 아래 여섯이 '소제목 목록'을 '무엇을 어디까지 쓸지'로 바꾼다.
                            # 이것이 없으면 원고 단계에서 섹션마다 같은 분량·같은 구성이 나온다.
                            "interpretation": {
                                "type": "string",
                                "description": (
                                    "이 섹션에서 작성자의 판단이 필요한 지점. 자료를 옮겨 적는"
                                    " 것만으로 채울 수 없는 부분을 한 문장으로."
                                ),
                            },
                            "omitBackground": {
                                "type": "string",
                                "description": (
                                    "이 섹션에서는 설명하지 않고 넘어갈 배경. 다른 섹션이 이미"
                                    " 다뤘거나 독자가 이미 아는 것. 없으면 빈 문자열."
                                ),
                            },
                            "connection": {
                                "type": "string",
                                "description": (
                                    "앞 섹션과 이어지는 이유 한 문장. 첫 섹션은 제목의 약속과"
                                    " 어떻게 이어지는지 적는다."
                                ),
                            },
                            "lengthShare": {
                                "type": "string",
                                "description": (
                                    "전체 본문 대비 권장 분량 비중. '25~35%' 형태로 적고,"
                                    " 모든 섹션에 같은 값을 주지 않는다. 합계는 100% 안팎."
                                ),
                            },
                            "personaDetail": {
                                "type": "string",
                                "description": (
                                    "이 섹션에서 화자가 드러낼 수 있는 관찰이나 디테일."
                                    " 자료에 없는 개인 경험은 적지 않는다. 없으면 빈 문자열."
                                ),
                            },
                            "forbiddenClaims": {
                                "type": "array",
                                "maxItems": 4,
                                "items": {"type": "string"},
                                "description": (
                                    "이 섹션에서 하면 안 되는 주장. 자료 밖 수치·효과 단정 등."
                                    " 없으면 빈 배열."
                                ),
                            },
                            # 아래 둘은 '이 섹션이 누구를 위한 것인가'를 설계에 못박는다.
                            # 연령대·독자 정보가 프롬프트에는 들어가는데 설계 결과에는 남지
                            # 않아, 원고 단계에서 그 섹션이 누구의 무엇을 풀기로 했는지 알
                            # 길이 없었다(2026-08-05 미팅 2-2).
                            "targetReaderNeed": {
                                "type": "string",
                                "description": (
                                    "이 섹션이 대상 독자의 어떤 필요를 푸는지 한 구절."
                                    " 위 '독자 가이드'의 관심축 중 하나를 골라 쓴다."
                                    " '독자에게 도움이 된다' 같은 일반론은 쓰지 않는다."
                                ),
                            },
                            "toneDirection": {
                                "type": "string",
                                "description": (
                                    "이 섹션의 어조 한 구절(예: '절차를 담담하게 순서대로',"
                                    " '판단 근거를 먼저 제시'). 같은 글 안에서도 섹션마다"
                                    " 달라야 한다."
                                ),
                            },
                        },
                    },
                },
            },
        }
    },
}

DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["finalPost"],
    "properties": {
        "finalPost": {
            "type": "object",
            "additionalProperties": False,
            # 본문은 markdownContent 한 벌만 받는다. 예전에는 body·htmlContent까지 세 벌을
            # 요구해 모델이 같은 원고를 세 번 출력했다 — 출력 토큰이 곧 생성 시간이라
            # 원고 단계가 그만큼 느렸고, 세 벌이 서로 어긋나는 사고도 가능했다.
            # HTML과 순수 텍스트(body)는 코드가 마크다운에서 유도한다(markdown_html.py).
            "required": [
                "title",
                "markdownContent",
                "hashtags",
                "thumbnailCopy",
            ],
            "properties": {
                "title": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "markdownContent": {
                    "type": "string",
                    "description": (
                        "원고 전문(마크다운). `# 제목`으로 시작, `## ` 소제목, 빈 줄로"
                        " 문단 구분, **굵게**, ==형광펜==, `- ` 글머리 목록,"
                        " `1. ` 번호 목록, 마크다운 표, [[VISUAL: id]] 마커를 쓴다."
                    ),
                },
                # 대표 썸네일에 얹을 문구. 이미지 모델이 그리는 것이 아니라 생성된
                # 사진 위에 텍스트 레이어로 합성되므로, 여기서는 글자만 받는다.
                # 문구 없는 썸네일(NO_COPY_EDITORIAL_PHOTO)도 정상 결과라 minItems가 0이다.
                # 예전에는 1개 이상을 강제해, 문구가 필요 없는 글에도 억지 문구가 붙었다.
                "thumbnailCopy": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 2,
                    "items": {"type": "string", "maxLength": 12},
                },
            },
        },
        # 코드로 렌더링할 시각자료(그래프·과정도·인포그래픽)의 구조화 데이터. 본문의
        # [[VISUAL: visual-N]] 마커 자리에 PIL로 그린 PNG가 들어간다. 정확한 한글이
        # 필요한 자료라 이미지 모델에 텍스트를 맡기지 않는다. 그래프(BAR/LINE/PIE)는
        # 제공된 출처 dataPoints의 실제 수치가 있을 때만 만들고, 없으면 이 배열을 비운다.
        "visuals": {
            "type": "array",
            # 한 편에 들어갈 시각자료의 절대 상한. 표·그래프 장수는 글 길이 규격과
            # 무관하고(2026-08-03 사용자 결정), 근거가 있을 때만 실린다. 상한이지
            # 최소가 아니며, 목적별 게이트가 여기서 다시 자른다(modules/draft/visual_policy.py).
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "visualId",
                    "type",
                    "title",
                    "caption",
                    "altText",
                    "sectionId",
                    # style·visualReason·necessityScore는 '왜·어떤 모양으로 만드는가'다.
                    # 선택 항목으로 두었더니 대부분 기본 스타일로 렌더링됐고, 이유를 적을 수
                    # 없는 자료도 그대로 통과했다.
                    "style",
                    "visualReason",
                    "necessityScore",
                ],
                "properties": {
                    "visualId": {"type": "string", "description": "visual-1부터 순번."},
                    "type": {
                        "type": "string",
                        "enum": [
                            "BAR_CHART",
                            "LINE_CHART",
                            "PIE_CHART",
                            "PROCESS_DIAGRAM",
                            "INFOGRAPHIC",
                            "TABLE",
                        ],
                    },
                    "style": {
                        "type": "string",
                        "enum": list(VISUAL_THEMES),
                        "description": (
                            "이 글의 편집 스타일 계획이 지정한 테마를 그대로 쓴다."
                            " 한 글의 모든 시각자료는 같은 테마여야 한다."
                        ),
                    },
                    "layoutVariant": {
                        "type": "string",
                        "enum": sorted(
                            {
                                *TABLE_VARIANTS,
                                *PROCESS_VARIANTS,
                                *INFOGRAPHIC_VARIANTS,
                                "VERTICAL_BAR",
                                "HORIZONTAL_BAR",
                            }
                        ),
                        "description": (
                            "이 자료의 배치. 유형에 맞는 값만 쓴다(TABLE은 표 변형,"
                            " PROCESS_DIAGRAM은 과정 변형, INFOGRAPHIC은 인포그래픽 변형,"
                            " BAR_CHART는 막대 방향). 비우면 데이터 모양을 보고 코드가 고른다."
                        ),
                    },
                    "highlightLabels": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {"type": "string"},
                        "description": (
                            "conclusion과 직접 연결되는 항목·셀 값. 최댓값을 무조건 강조하지"
                            " 않고 여기 적힌 것만 포인트로 짚는다. 강조할 것이 없으면 빈 배열."
                        ),
                    },
                    "visualReason": {
                        "type": "string",
                        "description": (
                            "이 자료가 없으면 독자가 무엇을 놓치는지 한 문장."
                            " '한눈에 보여주기 위해', '이해를 돕기 위해' 같은 문장은 이유가"
                            " 아니며 그런 자료는 제외된다."
                        ),
                    },
                    "necessityScore": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "근거 충분성 30 + 글 이해에 주는 추가 정보 25 + 글 목적 적합성 20 +"
                            " 본문·다른 이미지와의 비중복성 15 + 모바일 가독성 10."
                            " 85점 미만이면 만들지 않는다."
                        ),
                    },
                    "title": {"type": "string"},
                    "caption": {
                        "type": "string",
                        "description": "자료 아래 붙는 캡션. 외부 자료면 출처·기준시점 포함.",
                    },
                    "altText": {"type": "string"},
                    "sectionId": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": r"^section-[1-9][0-9]*$",
                    },
                    "data": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "value"],
                            "properties": {
                                "label": {"type": "string"},
                                "value": {"type": "number"},
                            },
                        },
                        "description": (
                            "그래프 수치. 반드시 제공된 출처 dataPoints의 실제 값만 쓴다."
                        ),
                    },
                    "unit": {"type": "string"},
                    "xAxisLabel": {"type": "string", "description": "그래프 가로축 이름."},
                    "yAxisLabel": {
                        "type": "string",
                        "description": "그래프 세로축 이름(단위 포함, 예: 사용량(kWh)).",
                    },
                    "conclusion": {
                        "type": "string",
                        "description": (
                            "그래프에서 독자가 가져가야 할 결론 한 줄(35자 이내)."
                            " 수치 나열이 아니라 해석. 예: 8월 사용량이 6월의 2.3배."
                        ),
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 3,
                        "maxItems": 6,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label"],
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "단계 이름(12자 이내). 예: kW로 변환.",
                                },
                                "detail": {
                                    "type": "string",
                                    "description": (
                                        "그 단계의 실제 값이나 계산식(20자 이내, 단위 포함)."
                                        " 예: 1,500 ÷ 1,000 = 1.5kW. 계산 과정이 아니면 생략."
                                    ),
                                },
                            },
                        },
                        "description": "PROCESS_DIAGRAM의 단계(3~6개).",
                    },
                    "columns": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 8,
                            "pattern": r"\S",
                        },
                        "description": (
                            "TABLE의 비교 기준(2~4개, 각 8자 이내). 예: 월 전기료, 장점, 주의점."
                        ),
                    },
                    "rows": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "cells"],
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 20,
                                    "pattern": r"\S",
                                    "description": "비교 대상 이름(20자 이내).",
                                },
                                "cells": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 4,
                                    "items": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 20,
                                        "pattern": r"\S",
                                    },
                                    "description": (
                                        "columns와 같은 순서·같은 개수. 한 칸은 하나의 사실,"
                                        " 가능하면 2~12자이며 최대 20자."
                                    ),
                                },
                            },
                        },
                        "description": "TABLE의 비교 대상(2~5개).",
                    },
                    "centerTopic": {"type": "string"},
                    "groups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "items"],
                            "properties": {
                                "name": {"type": "string"},
                                "items": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "description": "INFOGRAPHIC의 그룹(2~4개, 그룹당 항목 2~4개).",
                    },
                    "source": {
                        "type": "string",
                        "description": "그래프 수치의 출처(기관·자료명). 그래프에는 필수.",
                    },
                    "publishedAt": {"type": "string", "description": "자료 기준 시점."},
                },
            },
        },
    },
}

# 원고 완성 후 만드는 사진 계획. 저장 호환 때문에 card/cardType 이름은 유지하지만 신규
# 출력에는 카드뉴스 문구·아이콘·디자인 시스템이 없다.
_CARD_SCENE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "mainSubject",
        "action",
        "setting",
        "mustInclude",
        "supportingProps",
        "cameraAngle",
        "cameraDistance",
        "lighting",
        "mustAvoid",
    ],
    "properties": {
        "mainSubject": {
            "type": "string",
            "description": "실제로 촬영할 수 있는 가장 중요한 피사체(영어).",
        },
        "action": {
            "type": ["string", "null"],
            "description": "눈으로 확인할 수 있는 행동(영어). 행동이 없으면 null.",
        },
        "setting": {"type": "string", "description": "실제의 구체적인 장소(영어)."},
        "mustInclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": "장면 이해에 꼭 필요한 물체만(영어).",
        },
        "supportingProps": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string"},
            "description": "실제 장소에 자연스럽게 존재하는 보조 물체, 최대 3개(영어).",
        },
        "cameraAngle": {"type": ["string", "null"]},
        "cameraDistance": {"type": ["string", "null"]},
        "lighting": {"type": ["string", "null"]},
        "mustAvoid": {"type": "array", "items": {"type": "string"}},
    },
}

CARD_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["cards"],
    "properties": {
        "cards": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "cardId",
                    "cardType",
                    "sectionId",
                    "sectionHeading",
                    "articleClaim",
                    "visualPurpose",
                    "scene",
                    "altText",
                    "necessityScore",
                    "usesReferenceImage",
                    # 아래 넷은 '이 사진이 정확히 무엇을 보여 주는가'다. 없으면 결과가
                    # '주제와 관련된 일반적인 사진'으로 수렴한다.
                    "photoRole",
                    "referenceId",
                    "subjectIdentity",
                    "generatedOrReused",
                    # 이 글의 핵심 대상이 고유 캐릭터·실제 인물인가. 없으면 사진이 그
                    # 대상 대신 주변 소품·배경으로 미끄러진다.
                    "subjectKind",
                    "mustShowSubject",
                    "identityConfidence",
                    # 이 사진을 어디서 구할지 — 웹 검색·유튜브 썸네일·AI 생성.
                    "imageSource",
                    # 이 사진이 실제로 보여 줄 대상과 그 대상을 얼마나 넓게 잡을지.
                    # 없으면 사진이 '브랜드 분위기'나 부분 확대로 미끄러진다.
                    "visualSubject",
                    "framing",
                ],
                "properties": {
                    "cardId": {
                        "type": "string",
                        "description": "thumbnail 또는 photo-1부터 순번.",
                    },
                    "cardType": {"type": "string", "enum": list(CARD_TYPES)},
                    "sectionId": {
                        "type": ["string", "null"],
                        "description": "본문 사진이 붙는 section-N. 썸네일은 null.",
                    },
                    "sectionHeading": {
                        "type": ["string", "null"],
                        "description": "실제 소제목. 썸네일은 null.",
                    },
                    "articleClaim": {
                        "type": "string",
                        "description": (
                            "이 사진이 연결되는, 원고에 실제로 있는 문장. 새 사실을 만들지 않는다."
                        ),
                    },
                    "visualPurpose": {
                        "type": "string",
                        "description": "사진이 추가하는 구체적인 이해.",
                    },
                    "scene": _CARD_SCENE_SCHEMA,
                    "altText": {"type": "string", "description": "한국어 대체 텍스트 한 줄."},
                    "necessityScore": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "필요성 점수: 원고 핵심 관련성 30 + 추가 이해 25 + 장면 구체성 20 +"
                            " 비중복성 15 + 사실 안전성 10."
                        ),
                    },
                    "usesReferenceImage": {
                        "type": "boolean",
                        "description": (
                            "첨부된 사용자 참고 이미지가 있을 때만 판단한다. 그 이미지가 보여 주는"
                            " 대상·장면과 이 카드가 같은 것을 그린다면 true(그 이미지를 시각 기준으로"
                            " 삼아 닮게 생성한다). 참고 이미지와 무관한 장면이거나 참고 이미지가"
                            " 없으면 false. 확실할 때만 true로 둔다."
                        ),
                    },
                    "referenceId": {
                        "type": ["string", "null"],
                        "description": (
                            "이 사진이 기준으로 삼는 참고 이미지(reference-image-1 형식)."
                            " 여러 장이 있으면 장면에 맞는 것을 고른다 — 첫 장만 쓰지 않는다."
                            " 참고 이미지를 쓰지 않으면 null."
                        ),
                    },
                    "photoRole": {
                        "type": "string",
                        "enum": list(PHOTO_ROLES),
                        "description": (
                            "이 사진이 맡는 정보 역할. 한 글에서 같은 역할을 두 번 쓰지 않는다"
                            " — 정면·조명만 다른 정면 같은 반복을 막는 값이다."
                            " RECEIPT_EVIDENCE·SCREENSHOT_EVIDENCE·BEFORE_AFTER_EVIDENCE는"
                            " 사용자가 실제 그 자료를 제공했을 때만 고른다."
                        ),
                    },
                    "visualSubject": {
                        "type": "string",
                        "description": (
                            "이 사진이 실제로 보여 줘야 하는 구체적인 대상 한 줄(한국어)."
                            " 소재·선택 키워드·확정 제목·이 카드의 소제목과 문단 내용을 함께"
                            " 읽고 정한다. 브랜드명이나 소재를 그대로 옮기지 않는다 —"
                            " '디올'(x) / '레이디 디올 핸드백 한 점의 전체 모습'(o),"
                            " '롯데리아'(x) / '리아 두툼새우 버거 한 개의 전체 모습'(o)."
                            " 문단이 여러 대상을 비교하면 그중 이 사진이 맡을 대상을 적는다."
                        ),
                    },
                    "framing": {
                        "type": "string",
                        "enum": list(PHOTO_FRAMINGS),
                        "description": (
                            "이 사진의 구도. FULL_SUBJECT=대상의 전체 형태가 프레임 안에"
                            " 온전히 들어온다(대표 썸네일·제품 대표컷·비교 사진은 여기다),"
                            " MEDIUM=대상과 주변 맥락이 함께 보인다,"
                            " CLOSE_UP=의도적인 부분 확대."
                            " CLOSE_UP은 그 문단이 소재·질감·마감·버튼·손잡이·로고처럼"
                            " 구체적인 디테일을 설명하고 photoRole이 PRODUCT_DETAIL일 때만"
                            " 고른다. 전체 제품·제품 비교·브랜드 특징을 설명하는 문단에"
                            " CLOSE_UP을 쓰면 대상을 알아볼 수 없는 사진이 된다."
                        ),
                    },
                    "subjectIdentity": {
                        "type": ["string", "null"],
                        "description": (
                            "이 사진이 반드시 보여 줄 대상의 정확한 이름·정체. 생성 이미지가"
                            " 다른 대상으로 바뀌지 않게 그대로 이미지 프롬프트에 실린다."
                            " 참고 이미지가 있으면 거기서 확인된 대상(제품명·색상·형태)이"
                            " 먼저다. 참고 이미지가 없어도 소재나 확정 제목이 특정 캐릭터·"
                            "실제 인물을 분명히 가리키면 그 정확한 이름을 적는다"
                            "(예: Spider-Man, Son Heung-min 손흥민)."
                            " subjectKind가 FICTIONAL_CHARACTER나 REAL_NAMED_PERSON이면"
                            " null이 될 수 없고, scene.mainSubject에 쓴 표기를 그대로 포함한다."
                            " GENERIC_PERSON_ROLE이나 확인된 대상이 없는 경우에만 null."
                            " 그룹과 멤버가 함께 나오면 글의 중심 인물을 적는다"
                            "(소재 '프로미스나인' + 키워드 '백지헌' → '백지헌')."
                        ),
                    },
                    "imageSource": {
                        "type": "string",
                        "enum": list(IMAGE_SOURCES),
                        "description": (
                            "이 사진에 가장 어울리는 소스. 어떤 값이든 실제 사진 검색이 항상"
                            " 먼저 시도되고, 못 구했을 때만 생성으로 넘어간다."
                            " WEB_PHOTO=실존 인물·캐릭터·실제 제품·장소처럼 그 대상의 실제"
                            " 사진이어야 설득력 있는 장면(네이버 이미지 검색 우선),"
                            " YOUTUBE_THUMBNAIL=영상·방송·무대·공연·게임 플레이·리뷰 영상 등"
                            " 영상 콘텐츠의 한 장면이 어울리는 카드(유튜브 썸네일 우선),"
                            " AI_GENERATED=특정 실존 대상이 없어 검색 결과가 마땅치 않을"
                            " 일상·개념·연출 장면(그래도 검색을 먼저 시도하고, 생성 시 검색"
                            " 결과를 시각 참고로 쓴다)."
                            " mustShowSubject가 true인 카드는 생성 모델이 그 대상을 그리지"
                            " 못하므로 WEB_PHOTO나 YOUTUBE_THUMBNAIL 중에서 고른다."
                        ),
                    },
                    "subjectKind": {
                        "type": "string",
                        "enum": list(VISUAL_SUBJECT_KINDS),
                        "description": (
                            "이 사진의 핵심 대상 종류."
                            " FICTIONAL_CHARACTER=이름이 명시된 허구 캐릭터(스파이더맨·배트맨),"
                            " REAL_NAMED_PERSON=이름이 명시된 실제 인물·연예인·운동선수·공인·"
                            "역사적 인물(백지헌·손흥민·아이유·백종원·세종대왕),"
                            " GENERIC_PERSON_ROLE=특정 개인이 아닌 직업·역할(헬스 트레이너·개발자),"
                            " NON_PERSON=제품·장소·음식·개념."
                            " 이름이 한 번 언급됐다는 이유가 아니라, 이 사진이 실제로 무엇을"
                            " 보여 주어야 하는가로 정한다."
                            " 소재·키워드·확정 제목·원고의 중심이 실존 인물이면 그 사람을"
                            " 보여 주는 사진은 반드시 REAL_NAMED_PERSON이다 —"
                            " '가수', '아이돌', '축구선수'로 낮춰 잡지 않는다."
                        ),
                    },
                    "mustShowSubject": {
                        "type": "boolean",
                        "description": (
                            "그 대상 본인이 반드시 화면에 보여야 하는가."
                            " subjectKind가 FICTIONAL_CHARACTER나 REAL_NAMED_PERSON이면 true다"
                            " — 주변 소품·문양·배경 도시·포스터·비슷한 인상의 모델로 대신하지"
                            " 않는다. 그 밖에는 false."
                        ),
                    },
                    "identityConfidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "이 대상이 그 사람·그 캐릭터라고 판단한 신뢰도(0~1)."
                            " 소재·키워드·제목·원고가 모두 같은 이름을 가리키면 0.9 이상,"
                            " 이름이 본문에 스치듯 한 번 나오는 정도면 0.5 미만으로 둔다."
                            " GENERIC_PERSON_ROLE·NON_PERSON은 0."
                        ),
                    },
                    "productFidelityRequirements": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {"type": "string"},
                        "description": "반드시 보존할 특징(색상·실루엣·소재·패키지·브랜드 표식).",
                    },
                    "sectionClaim": {
                        "type": ["string", "null"],
                        "description": "이 사진이 보완하는 문단이 말하는 것 한 줄.",
                    },
                    "visualContinuity": {
                        "type": ["string", "null"],
                        "description": "앞뒤 사진과 이어지는 조건(같은 공간·같은 광원 등).",
                    },
                    "generatedOrReused": {
                        "type": "string",
                        "enum": list(PHOTO_SOURCE_MODES),
                        "description": (
                            "REUSED면 사용자가 올린 원본 이미지를 그대로 쓴다(다시 그리지 않는다)."
                            " 로고·패키지 문구처럼 정확해야 하는 것이 화면에 보이면 REUSED가 안전하다."
                            " 참고 이미지가 없으면 GENERATED."
                        ),
                    },
                    "forbiddenInference": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {"type": "string"},
                        "description": "이 사진을 근거로 단정하면 안 되는 것(가격·사용 기간 등).",
                    },
                },
            },
        },
    },
}

# M2가 선택된 키워드에 내주는 제목 후보 수. 각각 후킹 각도·강도가 다르므로, 프롬프트가
# 요구하는 후킹 조합 개수의 상한이기도 하다.
TOPIC_CANDIDATE_COUNT = 5

# 제목에 얹을 수 있는 후킹 유형·강도. shared.TitleHookType / TitleHookStrength와 값이 같아야
# 한다 — 스키마가 모델을 이 집합으로 제약하고, 어댑터가 같은 문자열을 enum으로 되읽는다.
TITLE_HOOK_TYPES = (
    "NONE",
    "CURIOSITY",
    "LOSS_AVERSION",
    "FOMO",
    "AUTHORITY",
    "REVERSAL",
    "COMPARISON",
    "IDENTITY",
    "STORY",
)
TITLE_HOOK_STRENGTHS = ("LOW", "MEDIUM", "HIGH")

TOPIC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topicCandidates"],
    "properties": {
        "topicCandidates": {
            "type": "array",
            "minItems": TOPIC_CANDIDATE_COUNT,
            "maxItems": TOPIC_CANDIDATE_COUNT,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "titleType", "hookType", "hookStrength"],
                "properties": {
                    "title": {"type": "string", "description": "블로그 제목 후보."},
                    "titleType": {
                        "type": "string",
                        "description": "제목의 기본 유형(정보형·가이드형·비교형·후기형·질문형 등).",
                    },
                    # 후킹 유형·강도는 화면에 표시하지 않는다. 근거 없는 후킹을 스키마 단계에서
                    # 걸러 내고(enum 제약), 선택된 제목의 약속을 원고 단계에 넘기기 위한 내부 값이다.
                    "hookType": {
                        "type": "string",
                        "enum": list(TITLE_HOOK_TYPES),
                        "description": "이 제목이 쓴 후킹 각도. 후킹을 쓰지 않았으면 NONE.",
                    },
                    "hookStrength": {
                        "type": "string",
                        "enum": list(TITLE_HOOK_STRENGTHS),
                        "description": "후킹의 세기. 후킹이 NONE이면 LOW.",
                    },
                },
            },
        }
    },
}


# 수집한 각 트렌드 키워드가 사용자 소재와 얼마나 관련되는지.
# 최종 4개가 한 분야에 몰리지 않도록 키워드마다 분야를 붙인다(§12). 어디에도 안
# 맞으면 "기타". 이 목록을 바꾸면 프롬프트 안내와 함께 움직인다.
TREND_CATEGORIES = [
    "스포츠·대회",
    "공연·축제·행사",
    "영화·드라마·연예",
    "뷰티·패션·쇼핑",
    "게임·IT",
    "사회·계절",
    "사회·생활",
    "음식·맛집",
    "기타",
]

def _rubric_score(description: str) -> dict:
    return {"type": "integer", "minimum": 0, "maximum": 100, "description": description}


# 생성된 제목들을 루브릭의 의미 판단 항으로 채점한다. 완성도(길이·낚시)는 코드가 규칙으로 매기므로
# 여기서는 묻지 않는다 — 모델은 관련성·트렌드 반영·목적 부합·독자 관심과 근거 한 줄만 낸다.
TITLE_EVALUATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["titles"],
    "properties": {
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title",
                    "relevance",
                    "trendReflection",
                    "purposeMatch",
                    "audienceInterest",
                    "reason",
                ],
                "properties": {
                    "title": {"type": "string", "description": "채점 대상 제목. 입력 제목과 정확히 일치."},
                    "relevance": _rubric_score("소재와의 관련성. 소재를 얼마나 정확히 담았는가."),
                    "trendReflection": _rubric_score(
                        "선택한 트렌드를 자연스럽게 반영했는가. 트렌드가 없으면 50."
                    ),
                    "purposeMatch": _rubric_score("사용자가 고른 글 목적과 얼마나 맞는가."),
                    "audienceInterest": _rubric_score("대상 독자가 클릭하고 싶을 가능성."),
                    "reason": {
                        "type": "string",
                        "description": "이 제목의 강점을 한 문장으로. 20자 내외, 과장 없이.",
                    },
                },
            },
        }
    },
}

# 키워드와 소재의 관계 유형. '얼마나 관련 있나'는 모호해서 점수가 흐르지만 '어떤 종류의
# 관계인가'는 판정할 수 있고, 유형이 subjectRelevance의 상한을 정한다(live_adapters의
# RELATION_SUBJECT_CAP에서 코드로 강제). 모델이 "어떻게든 연결"해 점수를 올리는 것을 막는
# 1차 장치다.
RELATION_TYPES = (
    "DIRECT",
    "ADJACENT",
    "CONTEXTUAL",
    "FORCED",
    "NONE",
    "AMBIGUOUS",
)

RELEVANCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["keywords"],
    "properties": {
        "keywords": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "keyword",
                    "relationType",
                    "relevance",
                    "subjectRelevance",
                    "purposeRelevance",
                    "personaRelevance",
                    "category",
                ],
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "입력받은 키워드 원문. 띄어쓰기·대소문자까지 그대로.",
                    },
                    # 점수보다 먼저 정한다. 유형이 subjectRelevance의 상한을 결정한다.
                    "relationType": {
                        "type": "string",
                        "enum": list(RELATION_TYPES),
                        "description": (
                            "DIRECT(소재 자체·하위 종류·핵심 속성·검색 의도)/"
                            "ADJACENT(함께 탐색하는 인접 주제)/"
                            "CONTEXTUAL(계절·상황·장소를 설명하면 연결)/"
                            "FORCED(억지로만 연결)/NONE(무관)/"
                            "AMBIGUOUS(뜻이 여럿이거나 고유명사·신조어라 판단 불가)"
                        ),
                    },
                    "relevance": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "종합: 0이면 소재와 무관하고, 100이면 소재와 자연스럽게 묶여"
                            " 한 편의 글이 된다."
                        ),
                    },
                    # 축별 부분 점수. subjectRelevance는 소재 관련순의 게이트·정렬 축이고
                    # 나머지는 툴팁 표기용 — 합산으로 상쇄되지 않게 반드시 따로 매긴다.
                    # (각 축의 소비처: prompts.keyword_relevance_prompt docstring)
                    "subjectRelevance": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "소재 자체와의 직접 관련성만. 목적·화자는 보지 않는다.",
                    },
                    "purposeRelevance": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "이 글 목적의 글감으로 얼마나 맞는가.",
                    },
                    "personaRelevance": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "명시된 페르소나(화자)가 이 키워드를 자연스럽게 다룰 수"
                            " 있는가. 페르소나가 없으면 100."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": TREND_CATEGORIES,
                        "description": "키워드가 속한 분야. 애매하면 '기타'.",
                    },
                },
            },
        }
    },
}


# M4 4단계(최종 검수). 완성된 원고와 이미지를 사용자 입력·조사 자료와 대조해, 사실과
# 어긋나거나 근거가 없거나 소재와 무관한 대목을 찾고 **고칠 문장까지** 함께 돌려받는다.
#
# 왜 고칠 문장을 함께 받는가: '무엇이 틀렸다'만 받으면 그걸 고치려고 원고를 통째로 다시
# 써야 한다 — 이미 만든 이미지와 구성까지 잃고, 모델 호출도 회차마다 두 번이 된다.
# quote/replacement로 받으면 그 자리만 바꾸면 되므로 회차당 호출이 한 번이다.
def _final_review_check(label: str) -> dict:
    """검수 항목 하나의 판정 스키마.

    ``issues``가 '무엇을 어떻게 고칠지'라면 이쪽은 '항목별로 어땠는지'다. 고칠 것이 없어도
    어떤 항목을 무슨 근거로 통과시켰는지가 남아야, 검수가 실제로 돌았는지 결과만 보고 알 수
    있다(2026-08-05 미팅 2-1의 검수 항목 일곱 가지).
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "reason", "affectedSections"],
        "description": label,
        "properties": {
            "status": {
                "type": "string",
                "enum": list(FINAL_REVIEW_CHECK_STATUSES),
                "description": (
                    "pass=문제 없음, warning=거슬리지만 고치지 않아도 되는 정도,"
                    " fail=고쳐야 함, skipped=검사할 것이 없었다(예: 이미지가 없는 글)."
                ),
            },
            "reason": {
                "type": "string",
                "description": "판정 근거 한 문장. pass면 비워도 된다.",
            },
            "affectedSections": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string"},
                "description": (
                    "문제가 걸린 자리. 위 '구조 설계'의 섹션 id를 쓰고,"
                    " 이미지면 'image-0'처럼 적는다. 없으면 빈 배열."
                ),
            },
        },
    }


FINAL_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overallStatus", "overallScore", "checks", "issues"],
    "properties": {
        "overallStatus": {
            "type": "string",
            "enum": list(FINAL_REVIEW_OVERALL_STATUSES),
            "description": (
                "pass=그대로 내보내도 된다, warning=거슬리는 곳이 있으나 치명적이지 않다,"
                " revise=고쳐야 할 곳이 있다. 원고를 못 쓴다는 뜻은 아니다."
            ),
        },
        "overallScore": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "글 전체 품질 점수. 판단 근거는 아래 항목별 판정이다.",
        },
        "checks": {
            "type": "object",
            "additionalProperties": False,
            "required": list(FINAL_REVIEW_CHECK_KEYS),
            "description": "일곱 항목을 하나도 빠뜨리지 않고 각각 판정한다.",
            "properties": {
                "sentenceNaturalness": _final_review_check("문장이 자연스러운지"),
                "paragraphCoherence": _final_review_check("단락 간 연결이 어색하지 않은지"),
                "topicRelevance": _final_review_check("소재와 무관한 내용이 없는지"),
                "titleBodyAlignment": _final_review_check(
                    "제목에서 제시한 관점이 본문에 반영됐는지"
                ),
                "imageRelevance": _final_review_check(
                    "이미지가 원고 내용 및 그 단락과 관련 있는지"
                ),
                "factualUncertainty": _final_review_check(
                    "사실관계가 불확실한 표현이 들어갔는지"
                ),
                "aiLikeExpression": _final_review_check(
                    "AI가 쓴 것처럼 부자연스러운 표현이 있는지"
                ),
            },
        },
        "issues": {
            "type": "array",
            "maxItems": 12,
            "description": (
                "고쳐야 할 것만 담는다. 문제가 없으면 빈 배열."
                " 표현이 마음에 들지 않는다는 이유로는 담지 않는다."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "severity", "reason", "quote", "replacement", "imageIndex"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(FINAL_REVIEW_ISSUE_KINDS),
                        "description": (
                            "fact=자료와 어긋나는 사실 진술,"
                            " unsupported=자료 어디에도 근거가 없는데 단정한 진술,"
                            " offtopic=소재와 이름만 같은 다른 대상 등 이 글에 있을 이유가"
                            " 없는 내용,"
                            " image=본문·자료와 맞지 않는 이미지,"
                            " missing=사용자 입력(목적·연령대·선택한 방향·참고자료)이 본문에"
                            " 반영되지 않음,"
                            " flow=문맥 단절·중복 표현·부자연스러운 문장,"
                            " tone=블로그 글이 아니라 AI 답변·보고서처럼 읽히는 문구."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "minor"],
                        "description": (
                            "critical만 원고를 고친다. 독자가 사실로 읽고 잘못 판단할 수 있는"
                            " 것이 critical이다. 어감·취향 차이는 minor."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "무엇이 왜 문제인지 한 문장. 자료와 어긋난다면 어느 자료의 무엇과"
                            " 어긋나는지 밝힌다."
                        ),
                    },
                    "quote": {
                        "type": "string",
                        "description": (
                            "본문에 **그대로 있는** 연속된 문장. 한 글자도 바꾸거나 줄이지"
                            " 않는다 — 이 문자열로 원고에서 자리를 찾기 때문에, 다르면 교정이"
                            " 적용되지 않는다. kind가 image면 빈 문자열."
                        ),
                    },
                    "replacement": {
                        "type": "string",
                        "description": (
                            "quote를 대신할 문장. 자료로 확인되는 범위 안에서만 쓰고, 확인되지"
                            " 않는 내용은 새로 넣지 않는다. 문장을 통째로 빼야 하면 빈 문자열."
                            " kind가 image면 빈 문자열."
                        ),
                    },
                    "imageIndex": {
                        "type": ["integer", "null"],
                        "description": (
                            "kind가 image일 때 문제가 된 이미지의 순번(0부터, 아래 이미지"
                            " 목록의 번호). 그 외에는 null."
                        ),
                    },
                },
            },
        }
    },
}


# M4 5단계(문장 다듬기). 사실 검수가 끝난 원고에서 **표현만** 고친다. 4단계와 같은
# before/after 형태인 이유는 같다 — 원고를 통째로 다시 받으면 이미 배치한 이미지·강조·
# SEO 키워드가 어디로 갔는지 확인할 수 없고, 사실이 조용히 바뀌어도 알 수 없다.
# 문장 단위로 받으면 코드가 한 건씩 검사해(modules/draft/polish.py) 걸리는 것만 버린다.
POLISH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["edits"],
    "properties": {
        "edits": {
            "type": "array",
            "maxItems": 12,
            "description": (
                "고쳐야 할 문장만 담는다. 이미 자연스러우면 빈 배열."
                " 취향 차이로 바꾸고 싶은 문장은 담지 않는다."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "reason", "before", "after"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(POLISH_EDIT_KINDS),
                        "description": (
                            "assistant_tone=AI 답변 말투,"
                            " hedge=책임 회피 군더더기,"
                            " report_tone=보고서형 문구,"
                            " repetition=같은 어미·접속어·내용의 반복,"
                            " fake_experience=겪지 않은 것을 겪은 것처럼 쓴 문장,"
                            " awkward=뜻이 바로 잡히지 않거나 앞뒤가 이어지지 않는 문장."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "이 문장이 왜 어색한지 한 문장.",
                    },
                    "before": {
                        "type": "string",
                        "description": (
                            "본문에 **그대로 있는** 연속된 문장. 한 글자도 바꾸거나 줄이지"
                            " 않는다 — 이 문자열로 원고에서 자리를 찾기 때문에, 다르면 교정이"
                            " 적용되지 않는다."
                        ),
                    },
                    "after": {
                        "type": "string",
                        "description": (
                            "그 자리를 대신할 문장. 뜻과 사실은 그대로 두고 표현만 바꾼다."
                            " before에 없던 숫자·가격·날짜·기능을 넣지 않는다."
                            " 그 대목을 빼는 것이 맞으면 빈 문자열(단, 숫자가 든 문장은 빼지"
                            " 않는다)."
                        ),
                    },
                },
            },
        }
    },
}


# --------------------------------------------------------------- Gemini 방언 변환


#: Gemini의 responseSchema가 받지 않는 키. OpenAI strict json_schema 전용이거나
#: (additionalProperties) Gemini 스키마 방언에 없는 것들이다.
#:
#: **값 제약(minimum·maximum)을 지우는 것이 손해가 아닌 이유**: 그 범위는 이미 각 항목의
#: description에 한국어로 적혀 있고, 모델이 어겨도 코드가 읽을 때 걸러 낸다. 반대로
#: 알아듣지 못하는 키 하나 때문에 요청이 400으로 거절되면 M3 검증이 통째로 실패한다.
_GEMINI_UNSUPPORTED_KEYS = frozenset({"additionalProperties", "minimum", "maximum"})


def to_gemini_schema(schema):
    """OpenAI strict json_schema → Gemini responseSchema.

    두 가지를 바꾼다.

    1. Gemini가 모르는 키를 지운다(``_GEMINI_UNSUPPORTED_KEYS``).
    2. ``"type": ["string", "null"]``(strict에서 선택 항목을 쓰는 방법)을
       ``"type": "string", "nullable": true``로 옮긴다.

    2026-08-07에 gemini-3.6-flash로 실제 호출해 확인했다 — type·properties·required·
    items·enum·minItems·maxItems·description·nullable을 모두 받고, 스키마대로 응답한다.

    원본을 고치지 않는다. 두 provider가 같은 스키마 상수를 공유하기 때문이다.
    """
    if isinstance(schema, list):
        return [to_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    converted: dict = {}
    for key, value in schema.items():
        if key in _GEMINI_UNSUPPORTED_KEYS:
            continue
        if key == "type" and isinstance(value, list):
            # ["string", "null"] 같은 조합. null을 빼고 nullable로 옮긴다.
            actual = [item for item in value if item != "null"]
            if len(actual) != len(value):
                converted["nullable"] = True
            converted["type"] = actual[0] if len(actual) == 1 else actual
            continue
        converted[key] = to_gemini_schema(value)
    return converted


#: M3 정리(방향 후보)를 Gemini에게 시킬 때 쓰는 스키마. 원본은 위 INTENT_SCHEMA이고,
#: 두 곳이 갈라지지 않도록 여기서 변환해 만든다.
GEMINI_INTENT_SCHEMA = to_gemini_schema(INTENT_SCHEMA)


# ------------------------------------------------- M4 마무리: 비평 → 통합 재작성


# 완성 원고에 대한 검토 하나(2026-08-07 사용자 결정 — Claude와 GPT가 **각자 결론**을
# 내고, 그 둘을 통합해 원고를 개선한다). 예전의 quote→replacement 교정 목록과 다르다:
# 여기서는 고칠 문장이 아니라 **의견**(좋은 점·아쉬운 점·개선점)을 받는다. 문장을 실제로
# 바꾸는 것은 통합 단계(INTEGRATION_SCHEMA)다.
#
# OpenAI strict와 Anthropic tool input 둘 다에 쓰이므로 strict 규칙을 지킨다
# (모든 object가 additionalProperties: false, properties의 모든 키가 required).
CRITIQUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["strengths", "weaknesses", "improvements", "imageFindings"],
    "properties": {
        "strengths": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string"},
            "description": "이 원고가 잘한 점. 통합 단계가 **지켜야 할 것**을 아는 데 쓴다.",
        },
        "weaknesses": {
            "type": "array",
            "maxItems": 7,
            "items": {"type": "string"},
            "description": "아쉬운 점. 어느 대목이 왜 아쉬운지 구체적으로.",
        },
        "improvements": {
            "type": "array",
            "maxItems": 7,
            "items": {"type": "string"},
            "description": (
                "고치는 방법. '~하면 좋겠다'가 아니라 어느 대목을 어떻게 바꾸라는"
                " 실행 가능한 지시로."
            ),
        },
        # 이미지를 실제로 본 검토자만 채운다(그림이 첨부되지 않았으면 빈 배열).
        # 위치는 본문의 [[IMAGE:n]] 자리표 기준이다.
        "imageFindings": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["imageIndex", "problem", "suggestion"],
                "properties": {
                    "imageIndex": {
                        "type": "integer",
                        "description": "본문 자리표 [[IMAGE:n]]의 n(1부터).",
                    },
                    "problem": {
                        "type": "string",
                        "description": "그림 자체 또는 놓인 위치의 문제.",
                    },
                    "suggestion": {
                        "type": "string",
                        "description": (
                            "어떻게 할지. 위치 문제면 어느 문단 뒤로 옮길지"
                            " 본문 문구를 인용해 말한다."
                        ),
                    },
                },
            },
        },
    },
}


# 통합 재작성. 두 검토(출처를 가린 A·B)를 받아 무엇을 반영할지 정하고, 개선된 원고
# 전체를 마크다운으로 돌려준다. [[IMAGE:n]] 자리표는 코드가 검사한다 — 하나라도
# 사라지거나 늘어나면 재작성 전체를 버리고 원본을 쓴다(critique.rebuild_post).
INTEGRATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions", "improvedMarkdown"],
    "properties": {
        "decisions": {
            "type": "array",
            "maxItems": 20,
            "description": (
                "두 검토의 지적 각각에 대한 결정. **버릴 때도 적는다** — 조용히"
                " 사라지는 지적이 없어야 나중에 통합이 제대로 됐는지 확인할 수 있다."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "point", "adopted", "reason"],
                "properties": {
                    "source": {"type": "string", "enum": ["A", "B"]},
                    "point": {"type": "string", "description": "그 검토의 지적(요약)."},
                    "adopted": {"type": "boolean"},
                    "reason": {
                        "type": "string",
                        "description": "반영/미반영의 이유 한 문장.",
                    },
                },
            },
        },
        "improvedMarkdown": {
            "type": "string",
            "description": (
                "개선된 원고 전체(마크다운). [[IMAGE:n]] 자리표를 전부, 각각 한 번씩"
                " 유지한다(옮기는 것은 된다). 제목(H1)은 바꾸지 않는다."
            ),
        },
    },
}


#: 웹 검색 사진 판정(사용 전 관문). 검색 선정(photo_search)은 픽셀 내용을 못 보고
#: 제목·구도·해상도만 재므로, '닷사이 23' 페이지의 애니 일러스트가 만점으로 통과해
#: 대표 썸네일이 된 실사례가 있다(2026-08-07). 그림을 실제로 본 모델이 사진마다
#: '기대 피사체의 실사인가'만 답한다. index는 첨부 순서(1부터)다.
WEB_PHOTO_GATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "usable", "reason"],
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "첨부 순서(1부터)",
                    },
                    "usable": {
                        "type": "boolean",
                        "description": "기대 피사체가 찍힌 실사 사진이면 true",
                    },
                    "reason": {
                        "type": "string",
                        "description": "판정 근거 한 줄(한국어)",
                    },
                },
            },
        }
    },
}
