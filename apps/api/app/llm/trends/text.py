"""자유 형식 한국어 텍스트에서 후보 키워드를 캐낸다.

네이버, 유튜브, 인스타그램에는 "트렌드 키워드를 달라"는 엔드포인트가 없다 — 사람들이
현재 게시하는 것만 돌려줄 수 있다. 그래서 최근 제목, 태그, 캡션을 읽고 무엇이 계속
등장하는지 센다.
"""

import html
import re
from collections import Counter
from functools import lru_cache
from typing import Iterable, Sequence

from kiwipiepy import Kiwi

from .base import CollectedKeyword
from .normalizer import normalize_keyword

_TOKEN = re.compile(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣]+")
_MARKUP = re.compile(r"<[^>]+>")
_HASHTAG = re.compile(r"#([0-9A-Za-z가-힣_]{2,30})")
_PHRASE_BOUNDARY = re.compile(r"[,.;:!?()\[\]{}<>/|·ㆍ،，。]+|\s+-\s+")

# 회차·편 표시 — "7화", "3회", "2편", "12월". 유튜브 제목에 흔하고, 결코 트렌드가 아니다.
_COUNTER = re.compile(r"^\d+[가-힣]{1,2}$")

MIN_LENGTH = 2
MAX_LENGTH = 20

# 한국어는 어미와 조사를 어간에 붙이므로, 정규식으로 텍스트를 자르면 "다양한",
# "되는", "있는" 같은 것이 돌아온다 — 단어처럼 읽히지만 아무것도 가리키지 않는
# 활용된 동사·형용사다. 막을 유한한 목록은 없다: 동사마다 수십 가지 형태가 있다.
# 그래서 후보는 그것이 무엇으로 이루어졌는지로 대신 검사한다.
#
# Kiwi에는 텍스트를 쪼개는 데가 아니라 단어 전체를 묻는다. 그 분석이 지켜야 할 바로
# 그 단어들을 부수기 때문이다 — 민생지원금은 민생+지원금이 되고 안유진은 안/유/진이
# 된다. 단어 전체로 보면: 민생지원금은 NNG+NNG, 안유진은 하나의 NNP인 반면, 다양한은
# NNG+XSA+ETM, 되는은 VV+ETM이다. 끝까지 명사이거나, 아니면 탈락.
_NOUN_TAGS = frozenset(
    {
        "NNG",  # 일반명사
        "NNP",  # 고유명사
        "SL",  # 로마자 — 브랜드·아티스트 이름
        "SH",  # 한자
        "SN",  # 숫자, 복합어의 일부로("코로나19")
        "XSN",  # 명사 파생 접미사 — 사용+기, 가능+성
    }
)

# 홀로 있는 단어는 주위에 뜻을 가려줄 문장이 없어, Kiwi의 단일 최선 추정이 때로
# 명사를 동사로 읽는다: 안내는 안(MAG) + 내(VV) + 어(EF)로 돌아온다. 그래서 차순위
# 해석들도 함께 고려하고, 모두 명사인 해석이 하나만 있으면 충분하다. 활용형에는
# 명사 해석이 아예 없으므로, 이는 되는이나 다양한을 통과시키지 않으면서 진짜 명사에
# 대한 문을 넓힌다.
_READINGS = 3

# 명사 해석이 최선 해석보다 얼마나 아래에 있어도 믿을 수 있는지. 측정값: 진짜
# 중의성은 1-2 근처(안내 1.1, 대한 1.5)에 오고, Kiwi의 미등록어 추정은 훨씬
# 아래(뜨는 8.5, 지난 6.9)에 있다.
_READING_MARGIN = 3.0

_kiwi = Kiwi()

# 토픽을 가리키지 않으면서 모든 한국어 피드 상위에 오르는 고빈도 불용어. 일부러
# 뺀 것: 그 자체로 글의 소재가 될 수 있는 것은 넣지 않았다.
#
# 두 번째 묶음은 게시물 상투어 — 제목이 무엇에 관한 것이어서가 아니라 영상이거나
# 기사여서 달고 다니는 단어들이다. "예고편"과 "선공개"는 유튜브에서 매일 순위에
# 오르지만, 둘 다 트렌드가 아니다.
STOPWORDS = {
    "가장",
    "결국",
    "공개",
    "공식",
    "관련",
    "구독",
    "그리고",
    "기사",
    "너무",
    "네이버",
    "다시",
    "대한",
    "댓글",
    "때문",
    "모두",
    "무료",
    "방법",
    "블로그",
    "사진",
    "소식",
    "쇼츠",
    "시작",
    "공연",
    "축제",
    "행사",
    "페스티벌",
    "여기",
    "영상",
    "오늘",
    "우리",
    "위해",
    "이번",
    "이유",
    "이제",
    "올해",
    "요즘",
    "작년",
    "내년",
    "인스타",
    "있는",
    "정도",
    "정리",
    "정말",
    "제공",
    "종합",
    "좋아요",
    "지금",
    "지난",
    "진짜",
    "채널",
    "최고",
    "최근",
    "최대",
    "추천",
    "통해",
    "포스팅",
    "하는",
    "하지만",
    "한다",
    "합니다",
    "해서",
    "했다",
    "함께",
    "화제",
    "확인",
    # 게시물 상투어.
    "결말",
    "다시보기",
    "메이킹",
    "본편",
    "비하인드",
    "선공개",
    "예고",
    "예고편",
    "원작",
    "직캠",
    "클립",
    "티저",
    "풀버전",
    "하이라이트",
    "회차",
    "clip",
    "ep",
    "feat",
    "full",
    "highlight",
    "instagram",
    "live",
    "mv",
    "official",
    "shorts",
    "subscribe",
    "teaser",
    "trailer",
    "video",
    "vs",
    "youtube",
    # 영어 불용어, 주로 제목에서 나온다.
    "and",
    "are",
    "best",
    "for",
    "from",
    "how",
    "new",
    "not",
    "the",
    "top",
    "this",
    "that",
    "was",
    "what",
    "why",
    "with",
    "you",
    "your",
    # 분류 명사. 명사 검사를 통과한다 — 흠잡을 데 없는 명사다 — 하지만 장르 전체를
    # 가리키는 것은 트렌드를 가리키는 게 아니다. "게임"은 유튜브에서 매일 1위에 올라도
    # 글쓴이에게 아무것도 알려주지 않는다. 활용형과 달리 이것들은 유한한 목록이다.
    "게임",
    "노래",
    "뉴스",
    "드라마",
    "먹방",
    "무대",
    "뮤비",
    "브이로그",
    "사람",
    "생각",
    "속보",
    "신작",
    "여자",
    "연예",
    "예능",
    "영화",
    "음악",
    "이야기",
    "일상",
    "남자",
    # 트렌드가 아니라 분야 전체를 가리키는, 넓고 상시적인 명사들 (§9). 네이버가
    # 사용자 자신의 소재 텍스트에서 이것들을 캐내기에 패널을 뒤덮었다 — AI 개발에
    # 관한 글은 매번 개발, 모델, 데이터, 코딩을 끌어내는데, 어느 것도 누가 새로
    # 검색하는 대상이 아니다.
    "가족",
    "강의",
    "개발",
    "경제",
    "교육",
    "기업",
    "국내",
    "기술",
    "데이터",
    "미래",
    "모델",
    "브랜드",
    "산업",
    "서비스",
    "사회",
    "서울",
    "솔루션",
    "시스템",
    "시장",
    "세계",
    "업체",
    "운영",
    "일정",
    "회사",
    "시험",
    "정보",
    "정보기술",
    "정보화",
    "정부",
    "문화",
    "문제",
    "분야",
    "성장",
    "사업",
    "시대",
    "한국",
    "가상",
    "상속",
    "코딩",
    "플랫폼",
    "ibm",
    "boy",
    "cover",
    "dance",
    "day",
    "girl",
    "life",
    "love",
    "mix",
    "music",
    "news",
    "song",
    "world",
}

# 형태소 검사는 통과하지만 여전히 현재 한국 트렌드가 아니라 넓은 개념을 가리키는
# 구절 전체. 긍정적 추천의 출처가 아니라 작은 품질 관문으로 둔다.
GENERIC_KEYWORDS = {
    "가상",
    "가상현실",
    "경제",
    "교육",
    "기업",
    "기술",
    "문화",
    "미래",
    "문제",
    "분야",
    "사업",
    "사회",
    "서울",
    "성장",
    "세계",
    "상속",
    "시장",
    "시대",
    "업체",
    "운영",
    "일정",
    "정부",
    "회사",
    "한국",
    "정보",
    "정보기술",
}

# 구절을 검색 카드가 될 만큼 구체적으로 만드는 토큰들. 최근 API 텍스트에 나타나지
# 않으면 내보내지 않는다; 이 목록은 네이버 스니펫이 "서울"이나 "일정" 같은 조각으로
# 무너지는 것을 막을 뿐이다.
CONCRETE_ANCHORS = {
    "ai",
    "k리그",
    "월드컵",
    "워터밤",
    "흠뻑쇼",
    "한강",
    "야외수영장",
    "수영장",
    "장마",
    "폭염",
    "여름휴가",
    "계곡",
    "올리브영",
    "세일",
    "메이크업",
    "프로야구",
    "올스타전",
    "스파이더맨",
    "마블",
    "개봉",
    "팝업스토어",
    "팝업",
}

LOCATION_MODIFIERS = {"서울", "한강", "부산", "제주", "강릉", "대구", "인천"}
TRAILING_PHRASE_NOISE = {"일정", "운영", "시작", "소식", "공개", "개장", "여름"}
WEAK_ANCHORS = {"세일", "개봉", "수영장", "야외수영장", "메이크업"}

PLATFORM_KEYWORDS = {
    "google",
    "googletrends",
    "naver",
    "youtube",
    "구글",
    "구글트렌드",
    "네이버",
    "유튜브",
}

# 검색 결과는 때때로 페이지 소유자나 공공 기관을 트렌드 키워드인 것처럼 드러낸다.
# 단순한 기관명은 쓸 만한 행사/토픽 카드가 아니다.
INSTITUTION_SUFFIXES = (
    "도청",
    "시청",
    "군청",
    "구청",
    "교육청",
    "경찰청",
    "소방서",
    "보건소",
    "대학교",
    "공단",
    "공사",
    "위원회",
)


def clean_text(value: str) -> str:
    """네이버는 검색 결과를 <b> 태그로 감싸고 엔티티를 이스케이프한다."""
    return html.unescape(_MARKUP.sub(" ", value or ""))


def _all_nouns(morphemes) -> bool:
    return bool(morphemes) and all(morpheme.tag in _NOUN_TAGS for morpheme in morphemes)


@lru_cache(maxsize=8192)
def is_noun(token: str) -> bool:
    """단어가 오직 명사로만 읽힐 수 있으면 True.

    트렌드 키워드는 사람들이 이야기하는 대상을 가리킨다. "되는"(VV + ETM)은 무언가를
    말하는 방식이지 대상이 아니다 — 어느 해석도 명사가 아니므로 탈락한다.
    """
    readings = _kiwi.analyze(token, top_n=_READINGS)
    if not readings:
        return False

    best, best_score = readings[0]
    if _all_nouns(best):
        return True

    # Kiwi가 모르는 단어는 "토큰 전체가 하나의 명사"로 폴백하는데, 이는 분석이 아니라
    # 추정이다 — 그래서 차순위 해석을 그 자체로 믿을 수 없다. 분야의는 최선 해석에서
    # 분야 + 의(JKG)이고 폴백에서 분야의(NNG)다. 최선 해석이 조사로 끝나므로, 이는
    # 조사가 붙은 단어다: 키워드가 아니라 조각이다. 폴백이 뭐라 하든 탈락.
    if best[-1].tag.startswith("J"):
        return False

    # 남은 것은 Kiwi가 주위 문장이 없어 맨 명사를 동사로 읽는 경우다 — 안내를 안(MAG)
    # + 내(VV) + 어(EF)로, 안내(NNG)를 근소한 2위로.
    #
    # 그것을 폴백 추정과 구분하는 것이 점수다. 진짜로 중의적인 단어는 명사 해석이
    # 동사 해석만큼이나 유력하고, Kiwi가 모르는 단어는 그것이 멀리 떨어진 최후의
    # 수단이다:
    #
    #   안내  -16.4 → -17.5   차이 1.1   진짜 중의성, 유지
    #   뜨는  -19.3 → -27.8   차이 8.5   동사이고 "명사"는 추정. 탈락.
    return any(
        _all_nouns(morphemes) and best_score - score <= _READING_MARGIN
        for morphemes, score in readings
    )


def _is_candidate(token: str) -> bool:
    if not MIN_LENGTH <= len(token) <= MAX_LENGTH:
        return False
    if token.isdigit() or _COUNTER.match(token):
        return False
    # 두 글자 로마자 토큰은 전치사나 이니셜이지 결코 소재가 아니다 — "of", "vs",
    # "ep". 두 음절 한국어 단어는 평범한 명사이므로, 길이 하한은 문자 체계에 따라
    # 달라야 한다.
    if token.isascii() and len(token) < 3:
        return False
    if is_low_quality_keyword(token):
        return False
    return is_noun(token)


def tokenize(value: str) -> list[str]:
    return [token for token in _TOKEN.findall(clean_text(value)) if _is_candidate(token)]


def noun_tokens(value: str) -> list[str]:
    """문장에서 명사 어간만 뽑는다(조사·어미 제거). tokenize와 달리 활용형을 버리지 않고 Kiwi로
    분해해 어간을 남긴다 — '콘텐츠에'→'콘텐츠', 'AIONA로'→'aiona'. 제목의 근접 중복(단어 순서만
    바꾼 변형) 판정에 쓴다."""
    return [
        token.form.lower()
        for token in _kiwi.tokenize(clean_text(value))
        if token.tag in _NOUN_TAGS and len(token.form) >= 2
    ]


def _trim_particle(word: str) -> str:
    # 제목은 짧은 상업 용어에 조사를 붙이곤 한다("세일과"). 명확한 자음 종결 조사만
    # 떼어낸다; "휴가"가 실제 단어이므로 이/가는 떼지 않는다.
    if len(word) > 2 and word[-1] in {"과", "와", "은", "는", "을", "를", "의"}:
        return word[:-1]
    return word


def _words(value: str) -> list[str]:
    return [_trim_particle(word) for word in _TOKEN.findall(clean_text(value))]


def _is_concrete_phrase(words: Sequence[str]) -> bool:
    if not words or len(words) > 5:
        return False
    if any(_COUNTER.match(word) for word in words):
        return False
    compact_words = [_compact(word) for word in words]
    if any(not word for word in compact_words):
        return False

    has_anchor = any(word in CONCRETE_ANCHORS for word in compact_words)
    if not has_anchor:
        return False
    if not any(word in CONCRETE_ANCHORS and word not in WEAK_ANCHORS for word in compact_words):
        return False
    if (
        any(word in LOCATION_MODIFIERS for word in compact_words[1:])
        and compact_words[0] in {"야외수영장", "수영장", "개봉", "세일"}
    ):
        return False

    if any(
        is_low_quality_keyword(word) and _compact(word) not in LOCATION_MODIFIERS
        for word in words
    ):
        return False

    useful = [
        word
        for word in words
        if not is_low_quality_keyword(word) or _compact(word) in LOCATION_MODIFIERS
    ]
    if not useful:
        return False

    # 모든 단어가 명사(또는 알려진 앵커)여야 한다. 소재가 그 자체로 앵커("ai")인 제품이면
    # 검색 결과가 "AI 도입하려", "AI 기반으로", "AI 서비스가 쏟아지"처럼 동사·조사로 끝나는
    # 조각을 쏟아내는데, 앵커만 보고 통과시키면 이 소재-메아리 조각이 트렌드인 척 올라온다.
    if not all(is_noun(word) or _compact(word) in CONCRETE_ANCHORS for word in words):
        return False

    # 한 단어 앵커는 이미 구체적인 행사·이슈 이름일 때만 허용한다. "세일" 같은 일반
    # 단어는 이웃이 필요하다.
    if len(words) == 1:
        return compact_words[0] in {
            "월드컵",
            "워터밤",
            "흠뻑쇼",
            "장마",
            "폭염",
            "여름휴가",
            "스파이더맨",
            "프로야구",
            "팝업스토어",
        }

    return True


def _canonical_phrase_words(words: Sequence[str]) -> list[str]:
    result = list(words)
    while len(result) > 1 and _compact(result[-1]) in TRAILING_PHRASE_NOISE:
        shortened = result[:-1]
        if not _is_concrete_phrase(shortened):
            break
        result = shortened
    return result


def concrete_phrases(value: str) -> list[str]:
    """최근 API 텍스트에서 검색 의도 구절을 추출한다.

    네이버 발굴에 쓴다: 검색 API 제목은 문장이라, 이를 단일 명사로 쪼개면 쓸모없는
    카드가 생긴다. 짧은 명사형 구절을 슬라이딩하면 구체적인 검색어는 남기고 "서울",
    "일정", "운영" 같은 조각은 걸러낸다.
    """
    phrases: list[str] = []
    seen: set[str] = set()
    for segment in _PHRASE_BOUNDARY.split(clean_text(value)):
        words = _words(segment)
        for size in range(min(3, len(words)), 0, -1):
            for start in range(0, len(words) - size + 1):
                phrase_words = _canonical_phrase_words(words[start : start + size])
                if not _is_concrete_phrase(phrase_words):
                    continue
                phrase = " ".join(phrase_words)
                key = _compact(phrase)
                if key in seen:
                    continue
                seen.add(key)
                phrases.append(phrase)
    return phrases


def material_phrases(value: str) -> list[str]:
    """소재 관련순 발굴용 명사구 추출 — 앵커 어휘에 기대지 않는다.

    concrete_phrases를 소재 관련순에 쓰면 안 되는 이유가 여기 있다. 그 함수는 구절에
    CONCRETE_ANCHORS(워터밤·흠뻑쇼·장마·마블 등 22개)가 **반드시 하나 들어 있어야** 통과시킨다.
    최신순 발굴에는 맞는 설계다 — 무엇이 트렌드인지 코드가 알 방법이 없으니 알려진 이슈어를
    기준으로 삼는 것이다. 그러나 소재 관련순에서는 치명적이다: 소재가 '배틀그라운드'든
    '에어컨'이든 그 목록에 없으면 검색 결과에서 캐낸 구절이 **하나도 통과하지 못하고**,
    그래서 후보가 늘 1~2개로 끝났다.

    여기서는 앵커를 요구하지 않는다. 대신 형태(명사로만 이루어진 1~3어절 구)와 품질
    (플랫폼명·기관명·회차 표시 제외)만 코드가 보고, **소재와 관련이 있는지는 판단하지 않는다**.
    그 판단은 관련도 채점(LLM)의 몫이다. 코드가 후보를 넓게 캐고 모델이 좁히는 분업이,
    코드가 어휘 목록으로 미리 좁히는 것보다 어떤 소재에도 견딘다.
    """
    phrases: list[str] = []
    seen: set[str] = set()
    for segment in _PHRASE_BOUNDARY.split(clean_text(value)):
        words = _words(segment)
        # 긴 구절부터 본다 — "감도 설정"을 먼저 잡고, 그 부분집합인 "감도"는 아래
        # 중복 제거에 걸리지 않으므로 둘 다 후보가 되지만 순위는 긴 쪽이 앞선다.
        for size in range(min(3, len(words)), 0, -1):
            for start in range(0, len(words) - size + 1):
                phrase_words = words[start : start + size]
                if not _is_material_phrase(phrase_words):
                    continue
                phrase = " ".join(phrase_words)
                key = _compact(phrase)
                if key in seen:
                    continue
                seen.add(key)
                phrases.append(phrase)
    return phrases


def _is_material_phrase(words: Sequence[str]) -> bool:
    """소재 발굴 구절의 자격 — 형태와 품질만 본다(소재 관련성은 LLM이 판단)."""
    if not words or len(words) > 3:
        return False
    if any(_COUNTER.match(word) for word in words):
        return False
    compact_words = [_compact(word) for word in words]
    if any(not word for word in compact_words):
        return False
    # 한 단어짜리 구절은 너무 넓다("설정", "가격"). 소재와 묶여야 검색 의도가 되므로
    # 두 어절 이상만 남긴다 — 단, 그 자체로 고유명사인 긴 단어(에란겔)는 살린다.
    if len(words) == 1 and len(compact_words[0]) < 4:
        return False
    if any(is_low_quality_keyword(word) for word in words):
        return False
    # 문장에서 캐낸 조각이 아니라 명사구여야 한다. 동사·조사가 섞인 조각은 키워드가
    # 아니라 잔해다(is_noun_phrase docstring과 같은 판단).
    return all(is_noun(word) and not word.isdigit() for word in words)


def is_noun_phrase(value: str) -> bool:
    """구절 전체가 오로지 명사로만 이루어졌는지.

    구글 트렌드는 단어가 아니라 검색어를 돌려준다 — "코스피 폭락"은 하나의
    키워드이며 하나로 남아야 한다. 그대로 쓰였으므로 사람들이 입력한 것이 그대로
    들어온다: "비 오는 날", "손흥민 어디". 구절은 그 안의 모든 단어가 명사일 때만
    남기고, 잘라내는 대신 통째로 버린다. 동사를 뗀 구절은 키워드가 아니라
    잔해이기 때문이다.

    여기서는 _is_candidate의 길이 하한을 적용하지 않는다. 그 하한은 문장에서 캐낸
    두 글자 쓰레기("of", "vs")를 버리려는 것이고, 큐레이션된 검색어의 "AI"는 AI를
    뜻한다.
    """
    words = _TOKEN.findall(clean_text(value))
    if not words:
        return False
    if is_low_quality_keyword(" ".join(words)):
        return False

    return all(
        is_noun(word)
        and not is_low_quality_keyword(word)
        and not word.isdigit()
        and not _COUNTER.match(word)
        for word in words
    )


def _compact(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", clean_text(value).lower())


def compact_for_match(value: str) -> str:
    """문서 본문을 대조용 한 덩어리로. 공백·기호·태그를 지우고 소문자로 만든다."""
    return _compact(value)


def match_terms(keyword: str) -> list[str]:
    """키워드를 문서 대조용 낱말들로 쪼갠다(빈 낱말은 버린다)."""
    return [term for term in (_compact(word) for word in _TOKEN.findall(clean_text(keyword))) if term]


def mentions_keyword(terms: Sequence[str], compact_document: str) -> bool:
    """이 문서가 그 키워드를 말했는가 — 낱말이 **모두** 등장하면 그렇다.

    구절 추출 결과(concrete_phrases 등)를 그대로 포함 판정에 쓰면 안 된다. 그 함수들은
    "이 문서에서 캐낼 만한 검색어"를 만드는 도구라, 조사가 붙은 표기를 통째로 버린다:
    '폭염 특보 발효'는 '폭염'을 내놓지만 '폭염이 계속되면서'·'폭염에 전력수요'는 아무것도
    내놓지 않는다(실측). 그래서 근거 집계가 '이번 수집 확인 블로그 1건'처럼 실제와 동떨어진
    수치가 됐다 — 문서는 분명 그 키워드를 말하고 있는데도.

    낱말을 붙여 놓고 비교하므로 조사('폭염이')·띄어쓰기('워터 밤')·어순('서울 워터밤')이
    달라도 같은 언급으로 센다. 모든 낱말을 요구하는 것이 오탐을 막는 장치다 — '워터밤 서울'은
    두 낱말이 다 있는 문서만 세므로, 서울만 나온 문서가 끼어들지 않는다.
    """
    return bool(terms) and all(term in compact_document for term in terms)


def is_low_quality_keyword(value: str) -> bool:
    """API가 보고하더라도 쓸 만한 트렌드 카드가 못 되는 플랫폼명, 넓은 명사, 기관명에
    대해 True."""
    compact = _compact(value)
    if not compact:
        return True
    if compact in PLATFORM_KEYWORDS or compact in GENERIC_KEYWORDS:
        return True
    if compact in STOPWORDS:
        return True
    if any(compact.endswith(suffix) for suffix in INSTITUTION_SUFFIXES):
        return True
    return False


def hashtags(value: str) -> list[str]:
    return [tag for tag in _HASHTAG.findall(clean_text(value)) if _is_candidate(tag)]


def _blocklist(exclude: Iterable[str]) -> set[str]:
    """사용자 자신의 시드 단어들과 그 안의 모든 토큰.

    사용자의 토픽을 "트렌드"라며 되돌려주는 것은 예전 목업이 하던 바로 그것이고,
    패널을 가짜처럼 느끼게 만드는 원인이다.
    """
    blocked: set[str] = set()
    for value in exclude:
        text = (value or "").strip()
        if not text:
            continue
        blocked.add(text.lower())
        blocked.update(token.lower() for token in _TOKEN.findall(text))
    return blocked


def count_keywords(
    texts: Sequence[str],
    *,
    weights: Sequence[float] | None = None,
    exclude: Iterable[str] = (),
    extractor=tokenize,
    min_documents: int = 1,
) -> Counter[str]:
    """원시 빈도가 아니라 문서 빈도 — 키워드는 글당 한 번만 세므로, 키워드를 잔뜩
    넣은 제목 하나가 순위를 지배할 수 없다.

    `min_documents`는 키워드가 트렌드로 세지기 전에 몇 개의 글이 그것을 써야 하는지의
    하한이다. 단어가 정확히 한 번 등장하는 것이 공유된 무언가가 아니라 한 창작자의
    어휘인, 큰 말뭉치에서는 이 값을 올린다.
    """
    blocked = _blocklist(exclude)
    weighted: Counter[str] = Counter()
    documents: Counter[str] = Counter()

    for index, text in enumerate(texts):
        weight = weights[index] if weights is not None and index < len(weights) else 1.0
        for token in sorted(set(extractor(text))):
            if token.lower() in blocked:
                continue
            weighted[token] += weight
            documents[token] += 1

    if min_documents > 1:
        # 가중치는 위치에 따라 달라지므로, 가중 점수만으로는 "상위 글 하나"와 "하위
        # 글 여러 개"를 구분할 수 없다 — 그래서 문서 수를 따로 센다.
        for token, count in documents.items():
            if count < min_documents:
                del weighted[token]

    return weighted


def to_collected(
    counts: Counter[str], limit: int | None, known: frozenset[str] = frozenset()
) -> list[CollectedKeyword]:
    """빈도 상위 limit개(None이면 전부)를 추린다. known(이미 저장된 풀의 정규화
    키워드)은 뒤로 미룬다 — 새 키워드가 앞에 오면, 저장 병합의 상한에 걸려도 실제로
    새로운 키워드가 밀려나지 않는다."""
    # Counter.most_common이 아니라 sorted: most_common은 동점 시 삽입 순서로
    # 대체되는데, 그 순서는 set에서 오므로 해시 시드에 따라 실행마다 달라진다.
    ranked = sorted(
        counts.items(),
        key=lambda item: (normalize_keyword(item[0]) in known, -item[1], item[0]),
    )[:limit]
    return [
        CollectedKeyword(keyword=keyword, score=float(score), rank=index + 1)
        for index, (keyword, score) in enumerate(ranked)
    ]
