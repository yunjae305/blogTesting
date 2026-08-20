"""브랜드 자료 — 글마다 반복해서 들어가는 회사·서비스 정보.

트렌드 키워드에 브랜드를 엮어 글을 쓰려면, 모델이 그 브랜드를 알아야 한다. 매번
사용자가 같은 설명을 입력하게 하는 대신 한 번 적어 두고 계속 쓴다.

**저장소는 DB다.** 파일에 두면 자료를 고칠 때마다 코드를 커밋해야 하고, 사용자별로
나눌 수도 없다. 이미지는 이 저장소가 이미 쓰는 data URL 방식을 그대로 쓴다(M5 원고
이미지와 같은 형식이라 발행 경로가 그대로 받는다).
"""

from typing import Literal

from .base import CamelModel


#: 소재와 브랜드의 **결합 가능성** 등급(2026-08-19). 판정은 ``modules/brand/fit.py``가
#: 하지만, 값 자체는 여기 둔다 — 글 입력 검증(``modules/blog_task/validation.py``)이 같은
#: 글자를 봐야 하는데, 브랜드 모듈에 두면 그쪽이 이미 검증 모듈을 import하고 있어
#: 순환이 된다(``BRAND_MATERIAL_ORIGIN``과 같은 사정이다).
#:
#: ``A`` 소재가 브랜드 기준표의 상황과 곧바로 닿는다 — 적극적으로 쓴다.
#: ``B`` 소재를 다루다 보면 그 상황이 생긴다 — 상황을 먼저 만들면 쓸 수 있다.
#: ``C`` 닿는 곳이 없다 — 억지로 이으면 광고 문장만 남는다.
BRAND_FIT_DIRECT = "A"
BRAND_FIT_SITUATIONAL = "B"
BRAND_FIT_FORCED = "C"
BRAND_FIT_GRADES: tuple[str, ...] = (
    BRAND_FIT_DIRECT,
    BRAND_FIT_SITUATIONAL,
    BRAND_FIT_FORCED,
)


#: 기타 유형. 이 값을 고른 경우에만 직접 입력을 받는다.
AUDIENCE_OTHER = "기타"

#: 주요 고객 선택지. **대분류 → 유형** 2단계다.
#:
#: 여기 없는 것: 연령대·글 목적·이번 글의 키워드. 그 셋은 글을 쓸 때마다 달라지므로
#: 작성 화면에서 받는다 — 브랜드 자료에 박아 두면 모든 글이 같은 대상을 향하게 된다.
#:
#: 자유 입력을 없앤 이유는 두 가지다. 사람마다 "중소기업"·"중기"·"SMB"로 달리 적어
#: 프롬프트가 들쭉날쭉해지고, 무엇을 적어야 할지 몰라 비워 두는 칸이 됐다.
AUDIENCE_CATALOG: dict[str, tuple[str, ...]] = {
    "기업·사업자": (
        "대기업",
        "중견기업",
        "중소기업",
        "스타트업",
        "소상공인·자영업자",
        "1인 사업자",
        AUDIENCE_OTHER,
    ),
    "공공·비영리기관": (
        "중앙부처·공공기관",
        "지방자치단체",
        "협회·단체",
        "재단·NGO",
        AUDIENCE_OTHER,
    ),
    "교육기관": (
        "초·중·고등학교",
        "대학·대학원",
        "학원·교습소",
        "평생교육·직업훈련",
        AUDIENCE_OTHER,
    ),
    "개인 고객": (
        "직장인",
        "자영업자",
        "학생",
        "주부",
        "시니어",
        AUDIENCE_OTHER,
    ),
}


class BrandAudience(CamelModel):
    """고른 고객 한 갈래. 대분류 하나와 그 아래 유형들이다."""

    category: str
    types: list[str] = []
    #: ``기타``를 골랐을 때만 채운다.
    other: str | None = None


class BrandLimits:
    MAX_NAME_LENGTH = 80
    # 서술 칸(소개·핵심 기능·주요 고객) 하나의 상한. 셋을 꽉 채우면 12,000자다.
    # 이 글자는 **모든 글의 프롬프트에 그대로 실린다** — 길수록 매 편이 비싸진다.
    MAX_SECTION_LENGTH = 4000
    MAX_ITEM_LENGTH = 200
    MAX_LINKS = 20
    #: 기준표("이런 상황이면 이 기능")의 줄 수. 서술 칸과 달리 **프롬프트에 골라서**
    #: 실리지만(소재에 맞는 줄만), 판정은 전부를 훑으므로 무한정 둘 수는 없다.
    MAX_USE_CASES = 30
    #: 기준표 한 줄이 갖는 검색어 수.
    MAX_USE_CASE_KEYWORDS = 12
    #: 고정 해시태그 후보 수. 실제로 글에 붙는 것은 앞의 두 개다(BRAND_HASHTAG_COUNT) —
    #: 나머지는 순서를 바꿔 쓰기 위한 자리다.
    MAX_HASHTAGS = 8
    #: '기타'에 직접 적는 글자 수.
    MAX_AUDIENCE_OTHER_LENGTH = 100
    MAX_DOCUMENTS = 5
    #: 텍스트 파일 하나의 글자 수. PDF와 달리 값이 글자 그대로라 프롬프트에 바로 실린다.
    MAX_TEXT_DOCUMENT_LENGTH = 20000
    #: PDF 한 개(원본 바이트 기준).
    MAX_PDF_BYTES = 4 * 1024 * 1024
    #: **첨부 전체의 합**(이미지 + PDF, 원본 바이트 기준).
    #:
    #: 낱개 상한만 두면 합계가 요청 본문 상한(16MB)을 넘는다. 이미지 10장(10MB)에 PDF
    #: 4MB짜리 하나만 더해도 base64로 18MB가 되어 413으로 잘린다 — 화면은 다 받아 놓고
    #: 저장 버튼에서 통째로 실패하는 셈이다. 그래서 **합계를 따로 검사한다.**
    #:
    #:     10MB × 4/3(base64) ≈ 13.3MB < 16MB
    MAX_ATTACHMENT_TOTAL_BYTES = 10 * 1024 * 1024
    MAX_IMAGES = 10
    # 이미지는 data URL(base64)로 **한 요청에 통째로** 실려 온다. 그래서 이 값은 요청 본문
    # 상한(routes.MAX_JSON_BODY_BYTES = 16MB)과 함께 정해야 한다.
    #
    #   10장 × 1MB × 4/3(base64) ≈ 13MB < 16MB
    #
    # 처음에는 4MB로 뒀는데, 그러면 큰 사진 3장에서 요청이 413으로 잘렸다 — 화면은 10장까지
    # 된다고 해 놓고 4장째에서 막히는 셈이었다. 블로그에 넣는 이미지가 1MB를 넘는 일은
    # 드물어 장수를 지키는 쪽을 택했다.
    MAX_IMAGE_BYTES = 1024 * 1024


class BrandLink(CamelModel):
    """공식 홈페이지·서비스 소개처럼 글에서 인용할 수 있는 주소."""

    label: str
    url: str


class BrandImage(CamelModel):
    """로고·서비스 화면처럼 글에 넣을 수 있는 이미지.

    ``data_url``은 ``data:image/...;base64,...`` 형식이다. 원고 이미지와 같은 형식이라
    발행 경로(네이버 앵커 치환)가 그대로 받는다.
    """

    label: str
    data_url: str
    caption: str | None = None


#: 파일을 붙일 수 있는 서술 칸과 그 이름. 문서는 **어느 칸에 붙였는지**를 함께 들고
#: 다닌다 — 모델에게 넘길 때 "브랜드 소개 자료"인지 "핵심 기능·서비스 자료"인지 알려야
#: 파일 이름만 보고 짐작하지 않는다.
BRAND_DOCUMENT_SECTIONS: dict[str, str] = {
    "description": "브랜드 소개",
    "features": "핵심 기능·서비스",
}


class BrandDocument(CamelModel):
    """올려 둔 문서 하나. 회사 소개서·서비스 자료처럼 글마다 참고할 것.

    서술 칸마다 붙는다(``section``). 칸에 글을 쓰든 파일을 붙이든, 또는 둘 다 하든 된다.

    ``kind``가 ``TEXT``면 ``value``는 **글자 그대로**이고, ``PDF``면 data URL이다. 둘 다
    글 작성 입력의 참고자료(``referenceMaterials``)와 같은 형식이라, 이 저장소의
    파이프라인이 손댈 것 없이 받는다 — PDF는 프롬프트를 만들 때 텍스트를 뽑아 쓴다
    (``llm/prompts.py``).
    """

    #: 어느 서술 칸의 자료인지. ``BRAND_DOCUMENT_SECTIONS``의 키다.
    section: str
    name: str
    kind: str  # "TEXT" | "PDF"
    value: str


class BrandUseCase(CamelModel):
    """"이런 상황이면 이 기능" 한 줄 — 트렌드와 브랜드를 잇는 **기준표**(2026-08-19).

    왜 자유 서술(``features``)만으로는 부족한가. 소재가 '빼빼로'인 글에서 모델이 알아야
    하는 것은 "이 브랜드가 무엇을 하는가"가 아니라 **"이 상황에서 쓸 기능이 무엇인가"**
    다. 서술 칸에는 기능이 줄글로 섞여 있어서, 모델이 매번 그중 하나를 골라 붙이거나
    (자주 같은 것만 고른다) 없는 기능명을 지어낸다.

    그래서 상황과 기능을 **짝으로** 저장한다. 이 짝이 두 곳에서 쓰인다:

    - 프롬프트: 소재에 맞는 줄만 골라 실어, 모델이 **실제로 있는 기능명**을 쓰게 한다.
    - 결합 가능성 판정(``modules/brand/fit.py``): 지금 소재가 이 브랜드와 자연스럽게
      닿는지(A·B·C)를 이 표로 잰다. 닿지 않는 소재에 억지로 브랜드를 넣으면 글이
      광고문이 되므로, 그런 조합은 아예 쓰지 않는 편이 낫다.

    ``keywords``는 선택이다. 비워 두면 ``situation``의 낱말이 그 자리를 대신한다 —
    표를 채우는 사람이 매번 검색어까지 상상하게 만들지 않는다.
    """

    #: 독자가 처한 상황. 예: "어떤 정보를 알아보고 싶을 때".
    situation: str
    #: 그때 쓰는 **실제 기능 이름**. 예: "자료 조사". 글에 이 이름 그대로 등장한다.
    feature: str
    #: 이 상황을 알아보는 소재·검색어들. 예: ["다이어트", "칼로리", "성분"].
    keywords: list[str] = []


class BrandClosing(CamelModel):
    """글 **맨 마지막에 언제나 붙는** 마무리 블록(2026-08-19 사용자 지시).

    본문은 광고가 아니어야 하지만(``brand_utility_rules``), 글의 끝에는 "여기서 보면
    된다"는 자리가 하나 있어야 한다. 그 둘은 충돌하지 않는다 — 본문에서 권유하지 않기
    때문에 마지막 한 줄이 오히려 신뢰를 얻는다. 사용자가 보여 준 실제 글들이 전부 그
    모양이었다: 본문은 담담하고, 끝에 마스코트 한 장과 사실 한 줄, 그리고 링크.

    **모델이 쓰지 않는다. 코드가 붙인다.** 이유가 둘이다.

    1. 매번 똑같아야 하는 글자다. 모델에게 맡기면 회차마다 문구·링크가 흔들리고,
       크레딧 수·조건 같은 **사실**이 슬쩍 바뀐다("웰컴 크레딧 200" 같은 것이 나온다).
    2. 붙이는 자리가 최종 검수 **뒤**다. 검수 앞에 두면 검수가 이 블록을 광고 문구로
       읽고 지적하거나 고쳐 버린다 — 본문에 권유를 금지해 두었기 때문에 더욱 그렇다.

    ``note``에 적는 것은 **확인된 사실만**이다. 이 글자는 손대지 않고 그대로 발행된다.
    """

    #: 사실 한 줄. 예: "가입은 무료, 웰컴 크레딧 100 지급, 카드 등록 없음."
    note: str
    #: 링크에 보이는 글자. 예: "aiona.kr".
    label: str
    #: 실제 주소. 예: "https://aiona.kr".
    url: str
    #: 함께 붙일 브랜드 이미지의 이름(마스코트). ``images``에 등록해 둔 것의
    #: 캡션 또는 라벨과 같아야 한다. 없으면 글자만 붙는다.
    #:
    #: 이미지 자체를 여기 담지 않는 이유: 브랜드 이미지는 이미 글의 참고자료로 펼쳐져
    #: 들어가 있다(``brand_reference_materials``). 한 벌 더 들고 있으면 같은 base64가
    #: 글 문서마다 두 번씩 저장된다.
    image_label: str | None = None


class BrandProfile(CamelModel):
    """한 브랜드에 대해 알아야 할 것 전부.

    필수는 ``name``뿐이다. 나머지는 비어 있어도 글은 써진다 — 채울수록 구체적인 글이
    나온다.

    말투는 여기 두지 않는다. 사용자 설정의 **페르소나**가 이미 그 일을 하고, 두 군데서
    말투를 정하면 서로 어긋난다.
    """

    brand_id: str
    user_id: str
    name: str
    #: 서술 칸은 셋이다. 주제별로 나누되 **각 칸 안은 줄글로 자유롭게** 쓴다.
    #:
    #: 처음에는 서비스·고객을 "한 줄에 하나씩" 목록으로 받았는데, 정리해 둔 글을 쪼개
    #: 넣어야 해서 불편했다(보통 쉼표로 이어 쓴다). 그렇다고 통째로 한 칸에 몰면 무엇을
    #: 적어야 할지 알 수 없다. 형식은 강요하지 않고 **주제만 나눈다.**
    #:
    #: 무엇을 하는 곳인지.
    description: str | None = None
    #: 핵심 기능·서비스.
    features: str | None = None
    #: "이런 상황이면 이 기능" 기준표. 트렌드 소재에 브랜드를 **활용 도구로** 얹는 글
    #: (``BRAND_MODE_UTILITY``)이 이 표를 읽는다. 비워 두어도 글은 써지지만, 그러면
    #: 모델이 서술 칸에서 기능을 골라야 해서 같은 기능만 반복되거나 없는 기능이 나온다.
    use_cases: list[BrandUseCase] = []
    #: 주요 고객. 자유 입력이 아니라 **고른 것**이다(대분류 → 유형).
    audiences: list[BrandAudience] = []
    links: list[BrandLink] = []
    #: 글 맨 마지막에 언제나 붙는 마무리(사실 한 줄 + 링크). 없으면 아무것도 붙지 않는다.
    closing: BrandClosing | None = None
    #: 모든 글에 **고정으로** 붙일 해시태그(2026-08-20 사용자 요청). 앞에서부터 두 개를 쓴다.
    #:
    #: 브랜드 이름은 소재마다 달라지지 않으므로 모델에게 맡기지 않는다 — 맡기면 회차마다
    #: 붙었다 안 붙었다 하고, 표기도 흔들린다(AIONA / 아이오나 / Aiona). 어느 표기를 쓸지는
    #: 브랜드가 정할 일이라 **순서**로 고른다: 앞의 두 개가 쓰인다.
    #:
    #: '#'은 적지 않는다. 발행할 때 붙는다.
    hashtags: list[str] = []
    #: 올려 둔 텍스트·PDF 문서. 줄글로 적기 어려운 긴 자료를 파일째 둔다.
    documents: list[BrandDocument] = []
    images: list[BrandImage] = []
    created_at: str
    updated_at: str
    #: 지운 시각(2026-08-20). **기본 브랜드에만 쓰인다.**
    #:
    #: 다른 브랜드는 지우면 문서가 사라진다. 기본 브랜드(AIONA)는 없으면 다시 만들어 주는
    #: 자리가 있어서(`ensure_default_brands`), 문서를 지우면 다음 조회에서 되살아난다 —
    #: 사용자는 지운 것이 왜 돌아왔는지 알 수 없다. 그래서 **지웠다는 사실만 남긴다.**
    #: 목록·조회에서는 없는 것으로 다루고, 다시 만들어 주지도 않는다.
    #:
    #: 되살리려면 `scripts/seed_aiona_brand.py --apply`를 쓴다.
    deleted_at: str | None = None
    #: 이 문서가 만들어질 때의 **기본 브랜드 정의 판번호**(2026-08-20). 기본 브랜드에만 쓴다.
    #:
    #: 기본 브랜드는 "없으면 만든다"였다. 그래서 한 번 만들어진 뒤에 정의에 새 자료가
    #: 붙으면(마스코트 그림, 고정 해시태그) **이미 쓰던 사람에게는 영영 오지 않았다** —
    #: 사용자는 등록한 적도 없는 자료가 왜 자기 것만 비어 있는지 알 수 없다.
    #: 판번호가 뒤처져 있으면 `ensure_default_brands`가 빈 칸만 채워 준다.
    defaults_revision: int = 0


class BrandListItem(CamelModel):
    """목록 화면이 실제로 쓰는 것만. **이미지·문서의 base64를 싣지 않는다.**

    브랜드 하나에 이미지가 9장이면 문서가 2MB가 된다(실측). 브랜드 고르기 화면은 이름과
    한 줄 소개만 그리는데, 그걸 보여 주려고 2MB를 받아 오고 있었다 — 화면은 그동안
    "브랜드 자료를 불러오는 중입니다"에 멈춰 있다.

    개수는 남긴다. 무엇이 들어 있는 브랜드인지 목록에서 가늠할 수 있어야 한다.
    """

    brand_id: str
    user_id: str
    name: str
    description: str | None = None
    link_count: int = 0
    document_count: int = 0
    image_count: int = 0
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, profile: "BrandProfile") -> "BrandListItem":
        return cls(
            brand_id=profile.brand_id,
            user_id=profile.user_id,
            name=profile.name,
            description=profile.description,
            link_count=len(profile.links),
            document_count=len(profile.documents),
            image_count=len(profile.images),
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )


BrandErrorCode = Literal[
    "REQUIRED", "TOO_LONG", "TOO_MANY", "INVALID_TYPE", "INVALID_URL", "UNKNOWN_VALUE"
]


class BrandValidationError(CamelModel):
    field: str
    code: BrandErrorCode
    message: str
