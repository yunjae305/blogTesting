"""캔버스 작업 — 이미지 모델이 못 하는 두 가지를 여기서 처리한다.

첫째, 크기. gpt-image-2에는 썸네일 1024², 본문 1200×688을 용도별로 요청하고 우리
발행 규격(썸네일 720×720, 본문 900×506)으로 한 번만 내려앉힌다. 구형 API가 사용자
지정 크기를 거부하면 어댑터가 표준 1536×1024로 되돌아간다.

둘째, 글자. 이미지 모델은 한글을 반드시 깨뜨린다 — 프롬프트로 막을 수 있는
문제가 아니다. 그래서 썸네일 문구는 모델에게 그리라고 시키지 않는다. 모델에는
주제를 분명히 보여 주는 자연 사진만 요청하고, 실제 검정 제목 박스·한글·핵심어 색은
생성이 끝난 뒤 여기서 진짜 텍스트 레이어로 얹는다.
"""

import base64
import io
import logging
import os
import re
from collections.abc import Mapping
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from app.shared import ThumbnailLayoutPlan
from app.shared.image_bytes import load_safe_image, normalize_image_bytes

logger = logging.getLogger(__name__)

# 대표 썸네일 캔버스. 네이버 권장 규격의 1:1(720×720)이다(2026-08-03 사용자 결정).
# 예전 1536×864(16:9)는 피드·검색의 정사각 크롭에서 좌우 336px씩 잘려 나가 안전 영역
# 계산이 따로 필요했는데, 처음부터 정사각으로 만들면 어디서든 그대로 보인다 — "핵심
# 내용을 이미지 중앙에"라는 네이버 팁도 자동으로 지켜진다.
CANVAS_WIDTH = 720
CANVAS_HEIGHT = 720

# 본문 이미지 규격. 저장 경로가 900px보다 큰 사진을 다시 JPEG q80으로 줄이므로 처음부터
# 최종 폭으로 렌더한다. provider 원본 → 900×506 JPEG 한 번으로 끝나 세대 손실과 CPU를
# 없앤다(900/506은 16:9에 가장 가까운 정수 규격).
BODY_WIDTH = 900
BODY_HEIGHT = 506

# 대표 썸네일은 글마다 항상 정확히 1장이다. 계획이 없을 때 장식용 본문 사진을 임의로
# 채우지 않는다. 단, 저장된 구형 원고의 [[IMAGE:]] 태그는 최대 2개까지 계속 지원한다.
BODY_IMAGE_COUNT = 0
LEGACY_BODY_IMAGE_LIMIT = 2

# 캔버스가 1:1이 되면서 '피드 정사각 크롭에서 살아남는 가운데 영역'이 캔버스 전체와
# 같아졌다(LEFT=0). 수식을 남겨 두는 이유: 배치 좌표 전부가 이 안전 영역 기준이라,
# 캔버스 비율을 되돌리면 이 값들이 다시 잘림 영역을 계산한다.
SAFE_AREA_LEFT = (CANVAS_WIDTH - CANVAS_HEIGHT) // 2  # 1:1에서는 0
SAFE_AREA_RIGHT = SAFE_AREA_LEFT + CANVAS_HEIGHT
SAFE_AREA_WIDTH = SAFE_AREA_RIGHT - SAFE_AREA_LEFT

MAX_COPY_LINES = 2
MAX_COPY_CHARS_PER_LINE = 12
# 실제로 크게 읽히는 길이. 상한(12자)은 규격이고 이건 목표라, 프롬프트가 이 범위를 권한다.
PREFERRED_COPY_CHARS = (5, 9)

# 아래 픽셀 값들은 안전 영역 폭에 비례해 잡는다. 예전 864px 기준 값에 720/864(≈0.83)를
# 곱해 옮겼다 — 캔버스가 작아졌는데 글자만 그대로면 문구가 화면을 다 덮는다.
_SCRIM_PADDING_X = 42
_SCRIM_PADDING_Y = 27
# 제목 박스가 프레임 위·아래 끝에서 최소한 이만큼은 떨어진다.
_SCRIM_EDGE_MARGIN = 13
_SCRIM_RADIUS = 13
_MAX_FONT_SIZE = 120
_MIN_FONT_SIZE = 44
_LINE_SPACING = 1.3

# 중앙 반투명 박스 위에서 의미가 바로 읽히는 강조색. 어두운 원색을 그대로 쓰면 검정
# 스크림 위에서도 죽으므로, 브랜드/소재의 색조는 유지하되 썸네일용으로 밝힌 색만 둔다.
_ACCENT_FAMILY_COLORS: dict[str, tuple[int, int, int]] = {
    "CYAN_NAVY": (103, 190, 255),
    "TERRACOTTA_SAND": (255, 164, 112),
    "AMBER_CHARCOAL": (255, 211, 78),
    "ROSE_CREAM": (255, 139, 181),
    "VIOLET_SLATE": (194, 166, 255),
    "CORAL_INK": (255, 119, 112),
    "FOREST_STONE": (111, 211, 137),
}
_DEFAULT_ACCENT_COLOR = (255, 211, 78)

# 고유색이 확실한 브랜드·소재는 해당 색으로, 그 외 주제어는 글의 accent_family 색으로
# 칠한다. 한 글자짜리 키는 오탐이 너무 많으므로 넣지 않는다.
_BRAND_TERM_COLORS: dict[str, tuple[int, int, int]] = {
    "토스뱅크": (77, 156, 255),
    "토스": (77, 156, 255),
    "카카오뱅크": (254, 229, 0),
    "카카오": (254, 229, 0),
    "네이버": (44, 213, 111),
    "삼성": (101, 151, 255),
    "갤럭시": (101, 151, 255),
    "유튜브": (255, 104, 104),
    "넷플릭스": (255, 92, 105),
    "당근": (255, 145, 69),
}
_SEMANTIC_TERM_COLORS: dict[str, tuple[int, int, int]] = {
    "파킹통장": (255, 209, 102),
    "통장": (255, 209, 102),
    "금리": (255, 209, 102),
    "예금": (255, 209, 102),
    "적금": (255, 209, 102),
    "금융": (255, 209, 102),
    "주식": (94, 213, 154),
    "투자": (94, 213, 154),
    "커피": (230, 169, 107),
    "초콜릿": (230, 154, 105),
    "딸기": (255, 105, 112),
    "토마토": (255, 105, 112),
    "레몬": (255, 224, 92),
    "오렌지": (255, 159, 67),
    "말차": (117, 211, 127),
    "녹차": (117, 211, 127),
    "식물": (94, 208, 122),
    "바다": (89, 191, 255),
    "하늘": (116, 199, 255),
    "여행": (86, 205, 205),
    "원목": (230, 174, 116),
    "나무": (230, 174, 116),
    "가죽": (222, 158, 105),
    "데님": (111, 165, 236),
    "골드": (255, 211, 78),
    "실버": (211, 221, 232),
    "AI": (103, 190, 255),
    "인공지능": (103, 190, 255),
    "스마트폰": (103, 190, 255),
    "노트북": (103, 190, 255),
    "게임": (194, 166, 255),
    "운동": (132, 224, 118),
    "러닝": (132, 224, 118),
}
_MATERIAL_TERMS = {
    "커피",
    "초콜릿",
    "딸기",
    "토마토",
    "레몬",
    "오렌지",
    "말차",
    "녹차",
    "식물",
    "바다",
    "하늘",
    "원목",
    "나무",
    "가죽",
    "데님",
    "골드",
    "실버",
}
_GENERIC_COPY_WORDS = {
    "가이드",
    "정리",
    "총정리",
    "방법",
    "이유",
    "비교",
    "추천",
    "후기",
    "핵심",
    "정보",
    "공개",
    "체크",
    "포인트",
    "알아보기",
    "완벽",
    "진짜",
    "요즘",
}
_KOREAN_PARTICLES = (
    "으로",
    "에서",
    "부터",
    "까지",
    "처럼",
    "보다",
    "에게",
    "이란",
    "라는",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "와",
    "과",
    "로",
    "도",
    "만",
    "란",
)
_TERM_TOKEN = re.compile(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣.+-]*")
_HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")

# ── 썸네일 배치 영역 ────────────────────────────────────────────────────────
#
# 좌표는 안전 정사각(1:1 캔버스에서는 720×720 전체) 안의 비율이다. 어떤 레이아웃을
# 골라도 문구는 이 정사각 안에서만 움직인다.
#
# 서로 겹치지 않게 좌/우, 상/하를 나눠 두었다 — 인물 얼굴이나 제품 위에 글자가 올라가던
# 문제는 '문구를 중앙에 둔다'는 규칙 자체에서 나왔다.
_ZONE_RECTS: dict[str, tuple[float, float, float, float]] = {
    "LEFT_CENTER": (0.02, 0.16, 0.48, 0.84),
    "RIGHT_CENTER": (0.52, 0.16, 0.98, 0.84),
    # 위·아래 띠는 제목 박스(문구 + 상하 여백 _SCRIM_PADDING_Y)가 통째로 들어갈 만큼
    # 높아야 한다. 예전 값(0.02~0.34 / 0.66~0.98)은 두 줄짜리 제목의 박스보다 낮아서
    # 박스가 화면 끝까지 흘러넘쳐 잘린 띠처럼 보였다 — 아래 여백이 4px이었다.
    "TOP_CENTER": (0.02, 0.05, 0.98, 0.42),
    "BOTTOM_CENTER": (0.02, 0.58, 0.98, 0.95),
    "CENTER": (0.10, 0.30, 0.90, 0.70),
    "TOP_LEFT": (0.02, 0.04, 0.46, 0.24),
    "BOTTOM_LEFT": (0.02, 0.76, 0.46, 0.96),
}
_DEFAULT_ZONE = "CENTER"

# 영역별 글자 크기 범위. 좁은 영역에 최대 글자를 넣으려 하면 한 줄에 세 글자만 들어간다.
_ZONE_FONT_RANGE: dict[str, tuple[int, int]] = {
    "LEFT_CENTER": (86, 38),
    "RIGHT_CENTER": (86, 38),
    "TOP_CENTER": (106, 42),
    "BOTTOM_CENTER": (106, 42),
    "CENTER": (_MAX_FONT_SIZE, _MIN_FONT_SIZE),
    "TOP_LEFT": (50, 30),
    "BOTTOM_LEFT": (50, 30),
}

# 문구 영역이 피사체 영역을 이 비율 넘게 덮으면 겹친 것으로 본다. 모서리 몇 픽셀이
# 스치는 것까지 '겹침'으로 보면 쓸 수 있는 조합이 거의 남지 않는다.
_ZONE_OVERLAP_TOLERANCE = 0.25

# 반대편으로 문구를 옮길 때의 짝. 겹침이 확인되면 여기로 되돌린다.
_OPPOSITE_ZONE = {
    "LEFT_CENTER": "RIGHT_CENTER",
    "RIGHT_CENTER": "LEFT_CENTER",
    "TOP_CENTER": "BOTTOM_CENTER",
    "BOTTOM_CENTER": "TOP_CENTER",
    "TOP_LEFT": "BOTTOM_LEFT",
    "BOTTOM_LEFT": "TOP_LEFT",
    "CENTER": "BOTTOM_CENTER",
}

_ALIGNMENT_ANCHORS = {"LEFT": "lm", "CENTER": "mm", "RIGHT": "rm"}

_JPEG_QUALITY = 88

FONT_PATH_ENV = "THUMBNAIL_FONT_PATH"

# 팀은 Windows에서 돌리고 배포는 리눅스일 수 있다. 순서대로 먼저 찾히는 것을 쓰고,
# 여기 없는 폰트를 쓰려면 THUMBNAIL_FONT_PATH로 지정한다. 굵은 자족을 먼저 두는 것은
# 썸네일 문구가 작은 화면에서 읽혀야 하기 때문이다.
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/AppleGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
)


class FontUnavailable(Exception):
    """한글 폰트를 못 찾았다. 문구 없는 썸네일은 규격 미달이므로 조용히 넘기지 않는다."""


@lru_cache(maxsize=1)
def font_path() -> str | None:
    override = os.environ.get(FONT_PATH_ENV, "").strip()
    if override:
        if os.path.isfile(override):
            return override
        logger.warning("%s=%s does not exist; falling back to the system fonts", FONT_PATH_ENV, override)

    return next((path for path in _FONT_CANDIDATES if os.path.isfile(path)), None)


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont:
    path = font_path()
    if path is None:
        raise FontUnavailable(
            "no Korean font was found on this machine — set "
            f"{FONT_PATH_ENV} to a .ttf/.ttc that can draw Hangul"
        )
    try:
        return ImageFont.truetype(path, size)
    except OSError as error:
        raise FontUnavailable(f"{path} could not be loaded as a font: {error}") from error


def _clean(line: str) -> str:
    return " ".join(line.replace("\n", " ").split()).strip()


def _wrap(text: str) -> list[str]:
    """제목을 썸네일 문구로 쓸 때의 줄바꿈. 최대 2줄까지만 채운다.

    12자를 넘는 단어는 잘라 버리지 않고 초과분을 다음 줄로 넘긴다 — 예전에는 13자째
    부터 조용히 소실되고 잘린 조각이 다음 단어와 엉키는 연쇄가 있었다.
    """
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate) <= MAX_COPY_CHARS_PER_LINE:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        if len(lines) == MAX_COPY_LINES:
            return lines
        while len(word) > MAX_COPY_CHARS_PER_LINE:
            lines.append(word[:MAX_COPY_CHARS_PER_LINE])
            if len(lines) == MAX_COPY_LINES:
                return lines
            word = word[MAX_COPY_CHARS_PER_LINE:]
        current = word
    if current and len(lines) < MAX_COPY_LINES:
        lines.append(current)
    return lines


def thumbnail_lines(copy: list[str] | None, fallback: str) -> list[str]:
    """규격(최대 2줄, 한 줄 12자)에 맞춘 썸네일 문구.

    모델이 규격을 넘겨 보내면 예전에는 12자째에서 중간 절단했는데("성능이 3배
    빨라졌"), 문구를 훼손하느니 내용을 보존한 채 다시 줄바꿈하는 편이 낫다.
    아무것도 안 보내면 제목에서 만든다 — 썸네일에 문구가 없는 편이 더 나쁘다.
    """
    lines = [_clean(line) for line in (copy or []) if _clean(line)]
    if lines and all(len(line) <= MAX_COPY_CHARS_PER_LINE for line in lines):
        return lines[:MAX_COPY_LINES]
    if lines:
        return _wrap(" ".join(lines))  # 규격 위반 → 재줄바꿈, 내용 보존
    return _wrap(_clean(fallback))


def _parse_color(
    value: str | tuple[int, int, int] | None,
    fallback: tuple[int, int, int],
) -> tuple[int, int, int]:
    if isinstance(value, tuple) and len(value) == 3:
        return tuple(  # type: ignore[return-value]
            max(0, min(255, int(channel))) for channel in value
        )
    match = _HEX_COLOR.match((value or "").strip()) if isinstance(value, str) else None
    if match is None:
        return fallback
    raw = match.group(1)
    return tuple(  # type: ignore[return-value]
        int(raw[index : index + 2], 16) for index in (0, 2, 4)
    )


def _relative_luminance(color: tuple[int, int, int]) -> float:
    def linear(channel: int) -> float:
        value = channel / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(
    foreground: tuple[int, int, int], background: tuple[int, int, int]
) -> float:
    lighter = max(_relative_luminance(foreground), _relative_luminance(background))
    darker = min(_relative_luminance(foreground), _relative_luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def _readable_accent(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """중앙 스크림의 가장 밝은 합성 결과 위에서도 큰 제목 대비(3:1)를 확보한다."""
    adjusted = color
    panel_worst_case = (78, 78, 80)
    for _ in range(10):
        if _contrast_ratio(adjusted, panel_worst_case) >= 3.0:
            break
        adjusted = tuple(  # type: ignore[assignment]
            round(channel + (255 - channel) * 0.14) for channel in adjusted
        )
    return adjusted


def _strip_particle(term: str) -> str:
    for particle in _KOREAN_PARTICLES:
        if term.endswith(particle) and len(term) - len(particle) >= 2:
            return term[: -len(particle)]
    return term


def _candidate_terms(value: str | None) -> list[str]:
    """저장 문자열을 바꾸지 않고, 정확히 색칠할 수 있는 소재 후보만 뽑는다."""
    cleaned = _clean(value or "").strip(".,!?;:()[]{}'\"")
    if not cleaned:
        return []

    candidates: list[str] = []
    # '생성형 AI'처럼 짧은 소재 구절이 문구에 그대로 들어간 경우에는 구절 전체가 우선이다.
    if 2 <= len(cleaned) <= MAX_COPY_CHARS_PER_LINE:
        candidates.append(cleaned)
    for token in _TERM_TOKEN.findall(cleaned):
        for candidate in (token, _strip_particle(token)):
            if (
                2 <= len(candidate) <= MAX_COPY_CHARS_PER_LINE
                and candidate.casefold() not in _GENERIC_COPY_WORDS
                and candidate not in candidates
            ):
                candidates.append(candidate)
    return candidates


def _term_pattern(term: str) -> str:
    escaped = re.escape(term)
    # 영문 약어 AI가 DAILY 안에서 우연히 잡히는 식의 부분 일치를 막는다. 한국어 뒤에 붙는
    # 조사까지 막으면 'AI로'를 찾지 못하므로 경계는 ASCII 영숫자에만 적용한다.
    if re.fullmatch(r"[A-Za-z0-9.+-]+", term):
        return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    return escaped


def _term_occurs(line: str, term: str) -> bool:
    return re.search(_term_pattern(term), line, flags=re.IGNORECASE) is not None


def _semantic_color_for(
    term: str, fallback: tuple[int, int, int]
) -> tuple[int, int, int]:
    folded = term.casefold()
    known = {**_SEMANTIC_TERM_COLORS, **_BRAND_TERM_COLORS}
    for keyword in sorted(known, key=len, reverse=True):
        keyword_folded = keyword.casefold()
        if keyword_folded in folded or folded in keyword_folded:
            return known[keyword]
    return fallback


def thumbnail_keyword_colors(
    lines: list[str],
    *,
    topic: str | None = None,
    subject: str | None = None,
    keywords: list[str] | None = None,
    intent_keywords: list[str] | None = None,
    subject_identity: str | None = None,
    emphasis_words: list[str] | None = None,
    accent_family: str | None = None,
    accent_color: str | tuple[int, int, int] | None = None,
) -> dict[str, tuple[int, int, int]]:
    """썸네일 문구에 실제로 들어 있는 소재/핵심어와 그 색을 결정한다.

    DB의 thumbnailCopy에는 순수 문자열만 남긴다. 렌더 직전에 기존 입력값과 정확히
    일치하는 단어만 최대 두 개(한 줄당 하나) 골라 색을 입힌다. 긴 단어를 먼저 보므로
    ``토스``가 ``토스뱅크`` 안에서 따로 칠해지지 않는다.
    """
    if not lines:
        return {}

    family = _ACCENT_FAMILY_COLORS.get((accent_family or "").upper())
    fallback = _readable_accent(
        family or _parse_color(accent_color, _DEFAULT_ACCENT_COLOR)
    )
    scores: dict[str, int] = {}

    def add_source(value: str | None, score: int) -> None:
        for term in _candidate_terms(value):
            if any(_term_occurs(line, term) for line in lines):
                scores[term] = max(scores.get(term, 0), score + len(term))

    for word in emphasis_words or []:
        add_source(word, 1000)
    add_source(subject_identity, 900)
    add_source(subject, 850)
    add_source(topic, 750)
    for word in keywords or []:
        add_source(word, 650)
    for word in intent_keywords or []:
        add_source(word, 600)

    copy_text = " ".join(lines)
    for term in _BRAND_TERM_COLORS:
        if _term_occurs(copy_text, term):
            scores[term] = max(scores.get(term, 0), 1100 + len(term))
    for term in _SEMANTIC_TERM_COLORS:
        if not _term_occurs(copy_text, term):
            continue
        specificity = 800 if term in _MATERIAL_TERMS or len(term) >= 4 else 520
        scores[term] = max(scores.get(term, 0), specificity + len(term))

    selected: dict[str, tuple[int, int, int]] = {}
    used_lines: set[int] = set()
    ranked = sorted(scores, key=lambda term: (scores[term], len(term)), reverse=True)
    for term in ranked:
        available_lines = [
            index
            for index, line in enumerate(lines)
            if index not in used_lines and _term_occurs(line, term)
        ]
        if not available_lines:
            continue
        line_index = available_lines[0]
        # 같은 위치에서 '토스'보다 '토스뱅크', '통장'보다 '파킹통장'을 고른다.
        if any(
            term.casefold() in longer.casefold()
            and len(longer) > len(term)
            and scores[longer] >= scores[term]
            and _term_occurs(lines[line_index], longer)
            for longer in ranked
        ):
            continue
        color = _semantic_color_for(term, fallback)
        selected[term] = _readable_accent(color)
        used_lines.add(line_index)
        if len(selected) == min(2, len(lines)):
            break
    return selected


def _emphasis_segments(
    line: str,
    keyword_colors: Mapping[str, tuple[int, int, int]],
) -> list[tuple[str, tuple[int, int, int] | None]]:
    terms = [term for term in keyword_colors if term and _term_occurs(line, term)]
    if not terms:
        return [(line, None)]
    terms.sort(key=len, reverse=True)
    pattern = re.compile(
        "|".join(f"(?:{_term_pattern(term)})" for term in terms),
        flags=re.IGNORECASE,
    )
    colors = {term.casefold(): color for term, color in keyword_colors.items()}
    segments: list[tuple[str, tuple[int, int, int] | None]] = []
    cursor = 0
    for match in pattern.finditer(line):
        if match.start() > cursor:
            segments.append((line[cursor : match.start()], None))
        segments.append((match.group(0), colors.get(match.group(0).casefold())))
        cursor = match.end()
    if cursor < len(line):
        segments.append((line[cursor:], None))
    return segments or [(line, None)]


# 세로 크롭 위치. 0.5는 가운데(생성 이미지의 기존 동작), 작을수록 위쪽을 남긴다.
CENTER_CROP = 0.5
# 웹에서 찾아온 인물 사진의 크롭 위치. 보도 사진은 세로로 길고 얼굴이 위쪽에 있어,
# 가운데를 자르면 머리가 날아가고 몸통만 남는다. 위쪽을 남겨 얼굴을 지킨다.
FACE_CROP = 0.22

# 잘라 버려도 되는 최대 면적 비율(2026-08-05). 이보다 많이 버려야 규격 비율이 되는
# 사진은 자르지 않고 비율을 보존한다(contain).
#
# 왜 필요한가. 웹에서 찾아온 제품 사진은 세로로 긴 것이 흔한데(쇼핑몰 상세컷은 3:4나
# 2:3이 기본이다), 그것을 16:9로 자르면 세로의 절반 이상이 사라진다. 실제로 디올 가방
# 사진에서 손잡이만 남고 몸통이 프레임 밖으로 나가는 일이 있었다 — 자른 것이 잘못이
# 아니라 '얼마나 잘랐는가'가 문제였다. 대상의 전체 형태가 남지 않을 만큼 잘라야 한다면
# 자르지 않는 편이 낫다. 남는 자리는 검은 띠가 아니라 같은 사진을 흐리게 깐 배경이라
# (_blurred_backdrop) 한 장의 사진으로 읽힌다.
#
# 0.35인 이유: 16:9(1.78)에 3:2(1.5) 원본은 16% 손실로 통과하고, 4:3(1.33)은 25%로
# 통과하며, 3:4(0.75)는 58%라 걸린다. 흔한 가로 사진은 예전처럼 잘리고 세로로 긴
# 상세컷만 보존된다.
MAX_CROP_LOSS = 0.35


def crop_loss(
    source_width: int, source_height: int, target_width: int, target_height: int
) -> float:
    """이 사진을 target 비율로 가운데 잘랐을 때 버려지는 면적 비율(0~1).

    비율만 보므로 실제 해상도와 무관하다 — 3:4 사진은 크든 작든 16:9에서 같은 비율을
    잃는다. 읽을 수 없는 크기(0 이하)는 0으로 둔다(자르지 않는 쪽으로 판단하지 않는다).
    """
    if source_width <= 0 or source_height <= 0 or target_width <= 0 or target_height <= 0:
        return 0.0
    source_ratio = source_width / source_height
    target_ratio = target_width / target_height
    if source_ratio > target_ratio:
        # 가로가 남는다 — 좌우를 잘라 낸다.
        return 1.0 - target_ratio / source_ratio
    return 1.0 - source_ratio / target_ratio


# 비율을 보존해 넣을 때 남는 자리를 메우는 배경. 흐림이 약하면 배경이 또 하나의 사진처럼
# 읽혀 시선을 뺏고, 어둡히지 않으면 가운데 실제 사진과의 경계가 사라진다.
_BACKDROP_BLUR_RADIUS = 24
_BACKDROP_DIM = 0.55
# 배경조차 만들 수 없을 때(이미지가 너무 작아 흐림이 무의미할 때)의 단색.
LETTERBOX_COLOR = (17, 17, 17)


def _blurred_backdrop(image: Image.Image, width: int, height: int) -> Image.Image:
    """비율 보존으로 남는 자리를 메울 배경 — 같은 사진을 꽉 채워 흐리고 어둡힌 것.

    단색 띠는 '사진이 잘렸다'처럼 보이지만, 흐린 배경은 한 장의 사진으로 읽힌다.
    대표 썸네일이 1:1(720×720)이 된 뒤로 16:9 유튜브 썸네일에는 위아래가 크게 남는데,
    그 자리를 검게 두면 규격을 못 맞춘 이미지처럼 보인다.
    """
    source_width, source_height = image.size
    if source_width <= 0 or source_height <= 0:
        return Image.new("RGB", (width, height), LETTERBOX_COLOR)

    # 캔버스를 덮을 때까지 확대한 뒤 가운데를 쓴다(cover).
    scale = max(width / source_width, height / source_height)
    covered = image.resize(
        (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
        Image.LANCZOS,
    )
    left = (covered.width - width) // 2
    top = (covered.height - height) // 2
    backdrop = covered.crop((left, top, left + width, top + height))
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(_BACKDROP_BLUR_RADIUS))
    return Image.blend(
        Image.new("RGB", (width, height), (0, 0, 0)), backdrop.convert("RGB"), _BACKDROP_DIM
    )


def _fit_canvas(
    image: Image.Image,
    width: int = CANVAS_WIDTH,
    height: int = CANVAS_HEIGHT,
    vertical_bias: float = CENTER_CROP,
    contain: bool = False,
    max_crop_loss: float | None = None,
) -> Image.Image:
    """지정 규격의 비율로 잘라 내려앉힌다. 세로 위치는 ``vertical_bias``가 정한다.

    썸네일(1:1)은 gpt-image-2의 1024×1024 출력을 그대로 720으로 줄이고, 본문은
    1200×688 원본에서 위아래의 아주 얇은 띠만 정리해 900×506으로 줄인다. 구형 API
    폴백 1536×1024에서도 같은 최종 규격을 보장한다.

    기본값은 가운데(0.5)로, 생성 이미지의 기존 동작 그대로다. 세로로 긴 인물 사진에는
    위쪽(FACE_CROP)을 쓴다 — 가운데를 자르면 얼굴이 프레임 밖으로 나간다.

    ``contain=True``면 자르지 않고 원본 비율을 보존해 넣는다. 유튜브 썸네일처럼 원본에
    이미 문구와 얼굴이 구워져 있는 이미지가 여기 해당한다 — 잘라내면 우리가 그 정보를
    지우는 셈이다. 본문(16:9)에서는 16:9 원본이 여백 없이 딱 맞아 크롭과 결과가 같고,
    대표 썸네일(1:1)에서만 위아래가 남는다. 그 자리는 단색 띠 대신 **같은 사진을 꽉 채운
    뒤 흐리게 깐 배경**으로 메운다: 검은 띠는 잘린 것처럼 보이지만 흐린 배경은 한 장의
    사진으로 읽힌다.

    ``max_crop_loss``를 주면 그만큼보다 많이 잘라야 하는 사진은 자르지 않고 ``contain``
    으로 되돌린다 — 세로로 긴 제품 사진에서 대상의 몸통이 프레임 밖으로 나가는 것을
    막는 장치다(MAX_CROP_LOSS 주석 참고). None이면 예전 그대로 무조건 자른다.
    """
    source_width, source_height = image.size
    target = width / height
    bias = min(max(vertical_bias, 0.0), 1.0)

    if (
        not contain
        and max_crop_loss is not None
        and crop_loss(source_width, source_height, width, height) > max_crop_loss
    ):
        contain = True

    if contain:
        scale = min(width / source_width, height / source_height)
        fitted = image.resize(
            (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
            Image.LANCZOS,
        )
        if fitted.size == (width, height):
            return fitted
        canvas = _blurred_backdrop(image, width, height)
        canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        return canvas

    if source_width / source_height > target:
        crop_width = round(source_height * target)
        left = (source_width - crop_width) // 2
        box = (left, 0, left + crop_width, source_height)
    else:
        crop_height = round(source_width / target)
        top = round((source_height - crop_height) * bias)
        box = (0, top, source_width, top + crop_height)

    return image.resize((width, height), Image.LANCZOS, box=box)


def _encode(image: Image.Image, *, for_text: bool = False) -> bytes:
    """JPEG 인코딩. 글자를 얹은 썸네일은 고대비 글자 경계에 모스키토 노이즈가 생기지
    않도록 크로마 서브샘플링을 끈다(4:4:4, q95). 본문 사진은 기본 품질로 줄인다.
    PNG는 쓰지 않는다 — 1536×864 사진의 PNG는 1MB를 넘기기 쉽다."""
    buffer = io.BytesIO()
    if for_text:
        image.save(buffer, format="JPEG", quality=95, subsampling=0, optimize=True)
    else:
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def to_canvas(
    image_bytes: bytes,
    vertical_bias: float = CENTER_CROP,
    contain: bool = False,
    max_crop_loss: float | None = None,
) -> bytes:
    """본문 이미지. 네이버 본문 폭에 맞춘 900×506(약 16:9)을 쓴다. 크기만 맞추고
    글자는 얹지 않는다. ``contain=True``면 자르지 않고 비율을
    보존한다 — 본문 규격이 16:9라 유튜브 썸네일은 여백 없이 그대로 들어간다.
    ``max_crop_loss``는 '이만큼보다 많이 잘라야 하면 자르지 말라'는 상한이다."""
    source, _format_name = load_safe_image(image_bytes)
    return _encode(
        _fit_canvas(
            source.convert("RGB"),
            BODY_WIDTH,
            BODY_HEIGHT,
            vertical_bias,
            contain,
            max_crop_loss,
        )
    )


# 변환 시 긴 변 상한(px). Anthropic 권장 범위로 줄여 용량·토큰을 아낀다.
_ANTHROPIC_IMAGE_MAX_EDGE = 1568


def prepare_anthropic_image(b64_data: str) -> tuple[str, str] | None:
    """참고자료 이미지를 Anthropic Messages API가 받는 (media_type, base64)로 만든다.
    쓸 수 없으면 None(첨부 생략).

    - 선언된 mime을 믿지 않고 실제 바이트로 형식을 판별한다 — image/jpeg로 잘못 붙어 온
      WebP를 그대로 보내면 "media_type과 실제 이미지가 다르다"며 400으로 거절돼 원고 생성이
      통째로 죽기 때문이다(실측 사례).
    - 크기가 작아도 항상 EXIF orientation을 픽셀에 반영하고 새 파일로 저장한다. 이 과정이
      GPS·기기명·XMP·PNG 텍스트 같은 메타데이터를 provider 전송 전에 제거한다.
    - JPEG·PNG·WebP는 실제 형식을 유지한다. GIF는 개인정보 분석에 필요한 첫 프레임을
      메타데이터 없는 PNG로 고정한다.
    - 그 외 형식(BMP·TIFF·ICO 등 Pillow가 여는 무엇이든)은 PNG로 변환해 '모든 이미지 형식'을
      지원한다. 열 수 없는 데이터는 None — 이미지 하나를 빼는 편이 생성을 실패시키는 것보다 낫다.
    """
    try:
        raw = base64.b64decode(b64_data, validate=True)
        normalized, output_media_type = normalize_image_bytes(
            raw,
            max_edge=_ANTHROPIC_IMAGE_MAX_EDGE,
            output_format="provider",
        )
    except (ValueError, OSError) as error:
        logger.info("참고 이미지 변환 실패 - 첨부 생략: %s", error)
        return None
    return output_media_type, base64.b64encode(normalized).decode("ascii")


# 이미지 편집(image-to-image) 입력용 긴 변 상한. 편집 입력은 원본만큼 클 필요가 없다.
_EDIT_INPUT_MAX_EDGE = 1024


def to_edit_input_png(b64_data: str) -> bytes | None:
    """참고 이미지를 OpenAI 이미지 편집(image-to-image) 입력용 PNG 바이트로 만든다.

    편집 입력은 png/webp/jpg만 받으므로 형식을 PNG로 통일하고, 긴 변을 1024px로 줄여
    업로드·처리를 가볍게 한다. 열 수 없는 데이터는 None(편집을 포기하고 일반 생성으로 되돌린다).
    """
    try:
        raw = base64.b64decode(b64_data, validate=True)
        normalized, _mime = normalize_image_bytes(
            raw,
            max_edge=_EDIT_INPUT_MAX_EDGE,
            output_format="png",
        )
    except (ValueError, OSError) as error:
        logger.info("참고 이미지 편집 입력 변환 실패 - 일반 생성으로 진행: %s", error)
        return None
    return normalized


def zone_box(zone: str) -> tuple[int, int, int, int]:
    """영역 이름 → 캔버스 좌표(px). 항상 모바일 안전 정사각 안에 있다."""
    rect = _ZONE_RECTS.get((zone or "").upper(), _ZONE_RECTS[_DEFAULT_ZONE])
    x0, y0, x1, y1 = rect
    return (
        SAFE_AREA_LEFT + round(x0 * SAFE_AREA_WIDTH),
        round(y0 * CANVAS_HEIGHT),
        SAFE_AREA_LEFT + round(x1 * SAFE_AREA_WIDTH),
        round(y1 * CANVAS_HEIGHT),
    )


def zones_overlap(copy_zone: str, subject_zone: str) -> bool:
    """문구가 피사체 위에 올라가는가.

    두 영역이 실제로 얼마나 겹치는지를 문구 영역 넓이 대비로 본다 — 모서리가 조금 스치는
    것과 얼굴을 덮는 것은 다르다.
    """
    cx0, cy0, cx1, cy1 = zone_box(copy_zone)
    sx0, sy0, sx1, sy1 = zone_box(subject_zone)
    overlap_w = max(0, min(cx1, sx1) - max(cx0, sx0))
    overlap_h = max(0, min(cy1, sy1) - max(cy0, sy0))
    copy_area = max(1, (cx1 - cx0) * (cy1 - cy0))
    return (overlap_w * overlap_h) / copy_area > _ZONE_OVERLAP_TOLERANCE


def resolve_thumbnail_layout(plan: ThumbnailLayoutPlan | None) -> ThumbnailLayoutPlan:
    """실제로 그릴 배치를 확정한다.

    - 계획이 없으면(옛 저장 데이터·계획 실패) 중앙 배치 — 예전과 똑같은 그림이다.
    - 문구 영역이 피사체 영역을 덮으면 반대편으로 옮긴다. 인물 얼굴·제품 로고 위에
      글자가 올라가는 것은 레이아웃 이름이 무엇이든 허용하지 않는다.
    - CENTER끼리 겹치는 것은 예외다: 그 레이아웃(CENTER_COPY_ON_NEGATIVE_SPACE)은
      이미지 프롬프트가 가운데를 비워 달라고 요구한 경우다.
    """
    if plan is None:
        return ThumbnailLayoutPlan(show_copy=False, copy_lines=[])

    copy_zone = (plan.copy_zone or _DEFAULT_ZONE).upper()
    subject_zone = (plan.subject_zone or _DEFAULT_ZONE).upper()
    if copy_zone not in _ZONE_RECTS:
        copy_zone = _DEFAULT_ZONE
    if subject_zone not in _ZONE_RECTS:
        subject_zone = _DEFAULT_ZONE

    if copy_zone != "CENTER" and zones_overlap(copy_zone, subject_zone):
        moved = _OPPOSITE_ZONE.get(copy_zone, "BOTTOM_CENTER")
        if not zones_overlap(moved, subject_zone):
            logger.info(
                "썸네일 문구 영역 %s가 피사체 영역 %s와 겹쳐 %s로 옮깁니다",
                copy_zone,
                subject_zone,
                moved,
            )
            copy_zone = moved

    alignment = (plan.copy_alignment or "CENTER").upper()
    if alignment not in _ALIGNMENT_ANCHORS:
        alignment = "CENTER"
    scrim = (plan.scrim_style or "LOCAL_ROUNDED").upper()
    if scrim not in ("LOCAL_ROUNDED", "SOFT_GRADIENT", "TEXT_SHADOW_ONLY"):
        scrim = "LOCAL_ROUNDED"

    return plan.model_copy(
        update={
            "copy_zone": copy_zone,
            "subject_zone": subject_zone,
            "copy_alignment": alignment,
            "scrim_style": scrim,
        }
    )


def _fit_font(lines: list[str], zone: str = "CENTER") -> ImageFont.FreeTypeFont:
    """영역 안에 들어가는 가장 큰 글씨.

    12자짜리 두 줄이면 작아지고, 6자 두 줄이면 커진다 — 규격이 허용하는 한 크게 쓰는
    것이 모바일에서 읽히는 유일한 방법이다. 좁은 영역(좌우 절반·작은 라벨)은 상한부터
    낮춰야 한 줄에 두세 글자만 들어가는 일이 없다.
    """
    x0, y0, x1, y1 = zone_box(zone)
    max_width = (x1 - x0) - 2 * 20
    # 글자만이 아니라 **제목 박스가** 영역 안에 들어가야 한다. 박스는 글자 위아래로
    # _SCRIM_PADDING_Y만큼 더 크므로 그만큼을 미리 뺀다. 이걸 빼지 않아서, 영역을 넓히면
    # 폰트가 그만큼 커지고 박스는 다시 프레임 끝에 붙었다.
    max_height = (y1 - y0) - 2 * _SCRIM_PADDING_Y
    ceiling, floor = _ZONE_FONT_RANGE.get(zone.upper(), (_MAX_FONT_SIZE, _MIN_FONT_SIZE))

    for size in range(ceiling, floor - 1, -4):
        font = _font(size)
        widest = max(font.getlength(line) for line in lines)
        block_height = round(size * _LINE_SPACING) * len(lines)
        if widest <= max_width and block_height <= max_height:
            return font

    return _font(floor)


def _mean_luminance(canvas: Image.Image, box: tuple[int, int, int, int]) -> float:
    left, top, right, bottom = box
    left, top = max(0, left), max(0, top)
    right = min(canvas.width, max(right, left + 1))
    bottom = min(canvas.height, max(bottom, top + 1))
    return ImageStat.Stat(canvas.crop((left, top, right, bottom)).convert("L")).mean[0]


def _scrim_alpha(canvas: Image.Image, box: tuple[int, int, int, int]) -> int:
    """문구 뒤가 밝을수록 더 짙게 깐다.

    흰 책상 위 사진에 흰 글씨를 그대로 얹으면 아무것도 안 읽힌다. 배경을 눌러 대비를
    만들어 주는 쪽이 확실하다 — 사진이 무엇이든 흰 글씨가 살아난다.
    """
    luminance = _mean_luminance(canvas, box)
    if luminance > 150:
        return 135
    if luminance > 90:
        return 105
    return 72


def _local_scrim_alpha(canvas: Image.Image, box: tuple[int, int, int, int]) -> int:
    """레퍼런스형 중앙 제목 박스의 농도.

    투명감은 남기되 사진 밝기와 무관하게 검정 사각형의 형태가 보여야 한다. 큰 강조색도
    살아야 하므로 기존 국소 스크림보다 최저 농도를 높인다.
    """
    luminance = _mean_luminance(canvas, box)
    if luminance > 150:
        return 190
    if luminance > 90:
        return 176
    return 164


def _text_block(
    lines: list[str], font: ImageFont.FreeTypeFont, zone: str, alignment: str
) -> tuple[int, int, tuple[int, int, int, int]]:
    """(문구 기준 x, 첫 줄 상단 y, 글자 덩어리의 바운딩 박스).

    기준 x는 정렬에 따라 달라진다 — 왼쪽 정렬이면 영역의 왼쪽 안쪽, 가운데면 중앙.
    """
    x0, y0, x1, y1 = zone_box(zone)
    inset_x, inset_y = 24, 16
    line_height = round(font.size * _LINE_SPACING)
    block_height = line_height * len(lines)
    widest = round(max(font.getlength(line) for line in lines))
    top = y0 + max(inset_y, ((y1 - y0) - block_height) // 2)
    # 글자 덩어리가 영역보다 크면 아래로 흘러나간다. 영역 안으로 되돌린다 — 넘칠 때
    # 위쪽이 잘리는 편이 아래쪽이 화면 밖으로 나가는 것보다 낫다(제목 박스가 프레임
    # 가장자리에 붙어 잘린 띠처럼 보이던 원인).
    top = min(top, max(y0, y1 - block_height))

    if alignment == "LEFT":
        anchor_x = x0 + inset_x
        box = (anchor_x, top, anchor_x + widest, top + block_height)
    elif alignment == "RIGHT":
        anchor_x = x1 - inset_x
        box = (anchor_x - widest, top, anchor_x, top + block_height)
    else:
        anchor_x = (x0 + x1) // 2
        box = (anchor_x - widest // 2, top, anchor_x + widest // 2, top + block_height)
    return anchor_x, top, box


def _gradient_scrim(
    canvas: Image.Image, box: tuple[int, int, int, int], zone: str, alpha: int
) -> Image.Image:
    """문구 쪽 가장자리에서 안쪽으로 사라지는 국소 그라데이션.

    사진 절반을 덮는 검은 박스를 쓰지 않는다. 문구가 놓인 변에서만 어둠이 시작해 피사체
    쪽으로 가면서 0이 되므로, 피사체는 원래 밝기를 유지한다.
    """
    box_left, box_top, box_right, box_bottom = box
    pad = 60
    vertical = zone in ("TOP_CENTER", "BOTTOM_CENTER")
    from_start = zone in ("LEFT_CENTER", "TOP_LEFT", "BOTTOM_LEFT", "TOP_CENTER")

    # 프레임 가장자리에서 시작해 안쪽으로 사라진다. 화면 한가운데 떠 있는 띠를 만들면
    # 진행 방향과 직각인 변이 직선으로 보여, 그라데이션이 아니라 검은 사각형이 된다.
    # 가로 방향이면 위아래 전체를, 세로 방향이면 좌우 전체를 덮으므로 잘린 변이 없다.
    if vertical:
        left, right = 0, canvas.width
        top = 0 if from_start else max(0, box_top - pad)
        bottom = min(canvas.height, box_bottom + pad) if from_start else canvas.height
    else:
        top, bottom = 0, canvas.height
        left = 0 if from_start else max(0, box_left - pad)
        right = min(canvas.width, box_right + pad) if from_start else canvas.width

    width, height = max(1, right - left), max(1, bottom - top)
    span = height if vertical else width
    ramp = Image.new("L", (1, span) if vertical else (span, 1))
    pixels = ramp.load()
    for index in range(span):
        position = index / max(1, span - 1)
        strength = 1.0 - position if from_start else position
        # 가장자리에서 충분히 짙고, 안쪽으로 갈수록 제곱으로 빠르게 사라진다.
        value = int(alpha * max(0.0, min(1.0, strength)) ** 1.7)
        if vertical:
            pixels[0, index] = value
        else:
            pixels[index, 0] = value
    mask = ramp.resize((width, height)).filter(ImageFilter.GaussianBlur(8))

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    band = Image.new("RGBA", (width, height), (12, 13, 16, 255))
    band.putalpha(mask)
    overlay.paste(band, (left, top))
    return overlay


def _draw_copy(
    canvas: Image.Image,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    layout: ThumbnailLayoutPlan,
    keyword_colors: Mapping[
        str, str | tuple[int, int, int]
    ] | None = None,
) -> Image.Image:
    zone = layout.copy_zone
    alignment = layout.copy_alignment
    anchor_x, top, box = _text_block(lines, font, zone, alignment)
    line_height = round(font.size * _LINE_SPACING)
    anchor = _ALIGNMENT_ANCHORS[alignment]

    luminance = _mean_luminance(canvas, box)
    scrim_style = layout.scrim_style
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if scrim_style == "SOFT_GRADIENT":
        overlay = _gradient_scrim(canvas, box, zone, _scrim_alpha(canvas, box))
        draw = ImageDraw.Draw(overlay)
        text_fill = (255, 255, 255, 255)
        halo = (0, 0, 0, 90)
    elif scrim_style == "TEXT_SHADOW_ONLY":
        # 스크림을 깔지 않는 대신 글자 색을 배경 밝기에 맞춰 뒤집는다. 밝은 사진 위에
        # 흰 글씨 + 그림자는 결국 읽히지 않는다.
        if luminance > 165:
            text_fill, halo = (18, 18, 20, 255), (255, 255, 255, 150)
        else:
            text_fill, halo = (255, 255, 255, 255), (0, 0, 0, 165)
    else:  # LOCAL_ROUNDED
        pad_x, pad_y = _SCRIM_PADDING_X, _SCRIM_PADDING_Y
        # 위아래로도 프레임에 붙지 않게 한다. 가장자리에 닿은 박스는 '의도한 배치'가
        # 아니라 '잘린 것'으로 읽힌다 — 가로는 이미 안전 영역 안으로 막고 있었다.
        scrim = (
            max(SAFE_AREA_LEFT + 8, box[0] - pad_x),
            max(_SCRIM_EDGE_MARGIN, box[1] - pad_y),
            min(SAFE_AREA_RIGHT - 8, box[2] + pad_x),
            min(canvas.height - _SCRIM_EDGE_MARGIN, box[3] + pad_y),
        )
        draw.rounded_rectangle(
            scrim,
            radius=_SCRIM_RADIUS,
            fill=(15, 15, 18, _local_scrim_alpha(canvas, box)),
        )
        text_fill = (255, 255, 255, 255)
        halo = (0, 0, 0, 80)

    if layout.accent_style == "SMALL_TAG":
        tag = (
            max(0, box[0] - 20),
            max(0, box[1] - 14),
            min(canvas.width, box[2] + 20),
            min(canvas.height, box[3] + 14),
        )
        draw.rounded_rectangle(tag, radius=10, fill=(15, 15, 18, 128))
        text_fill, halo = (255, 255, 255, 255), (0, 0, 0, 70)
    elif layout.accent_style == "THIN_RULE":
        rule_y = max(0, box[1] - 22)
        rule_x0 = box[0] if alignment != "RIGHT" else box[2] - 96
        draw.rounded_rectangle(
            (rule_x0, rule_y, rule_x0 + 96, rule_y + 5), radius=2, fill=(255, 255, 255, 220)
        )

    normalized_colors = {
        term: _readable_accent(_parse_color(color, _DEFAULT_ACCENT_COLOR))
        for term, color in (keyword_colors or {}).items()
        if term.strip()
    }
    for index, line in enumerate(lines):
        baseline = top + index * line_height + line_height // 2
        shadow_offsets = (
            ((2, 2), (-2, 2), (2, -2), (-2, -2))
            if scrim_style == "TEXT_SHADOW_ONLY"
            else ((2, 2),)
        )
        for offset in shadow_offsets:
            draw.text(
                (anchor_x + offset[0], baseline + offset[1]),
                line,
                font=font,
                fill=halo,
                anchor=anchor,
            )
        segments = _emphasis_segments(line, normalized_colors)
        if len(segments) == 1 and segments[0][1] is None:
            draw.text((anchor_x, baseline), line, font=font, fill=text_fill, anchor=anchor)
            continue

        line_width = font.getlength(line)
        if alignment == "LEFT":
            cursor_x = float(anchor_x)
        elif alignment == "RIGHT":
            cursor_x = float(anchor_x) - line_width
        else:
            cursor_x = float(anchor_x) - line_width / 2
        for segment, color in segments:
            draw.text(
                (cursor_x, baseline),
                segment,
                font=font,
                fill=(*color, 255) if color is not None else text_fill,
                anchor="lm",
            )
            cursor_x += font.getlength(segment)

    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def render_thumbnail(
    image_bytes: bytes,
    lines: list[str],
    layout: ThumbnailLayoutPlan | None = None,
    keyword_colors: Mapping[
        str, str | tuple[int, int, int]
    ] | None = None,
    vertical_bias: float = CENTER_CROP,
    contain: bool = False,
    max_crop_loss: float | None = None,
) -> bytes:
    """대표 썸네일. 720×720(1:1)으로 잘라낸 뒤 배치 계획이 지정한 영역에만 문구를 얹는다.

    ``layout``이 없으면 예전 동작(안전 영역 중앙)을 그대로 쓴다 — 저장된 옛 원고를 다시
    렌더링해도 그림이 달라지지 않는다. ``show_copy=False``면 글자를 아예 그리지 않고,
    제목을 억지로 다시 얹지 않는다. ``keyword_colors``는 화면에 실제 존재하는 부분 문자열만
    색칠하며 thumbnailCopy 저장 문자열이나 alt 텍스트에는 어떤 마크업도 넣지 않는다.

    폰트가 없을 때 글 전체를 실패시키지는 않는다 — 문구 없는 썸네일이 아쉬운 것이지,
    다 쓴 원고를 버릴 이유는 아니다. 대신 조용히 넘어가지 않고 고치는 법과 함께 남긴다.
    """
    source, _format_name = load_safe_image(image_bytes)
    canvas = _fit_canvas(
        source.convert("RGB"),
        CANVAS_WIDTH,
        CANVAS_HEIGHT,
        vertical_bias,
        contain,
        max_crop_loss,
    )

    if layout is not None and not layout.show_copy:
        return _encode(canvas)

    resolved = (
        resolve_thumbnail_layout(layout)
        if layout is not None
        else ThumbnailLayoutPlan(
            layout="CENTER_COPY_ON_NEGATIVE_SPACE",
            subject_zone="CENTER",
            copy_zone="CENTER",
            copy_alignment="CENTER",
            scrim_style="LOCAL_ROUNDED",
            accent_style="NONE",
            show_copy=True,
        )
    )

    if not lines:
        return _encode(canvas)

    try:
        font = _fit_font(lines, resolved.copy_zone)
    except FontUnavailable as error:
        logger.error(
            "thumbnail copy %s was not drawn: %s. Install a Korean font (Windows: 맑은 고딕, "
            "Linux: fonts-nanum) or point %s at one. The font path is cached per process — "
            "restart the API after installing.",
            lines,
            error,
            FONT_PATH_ENV,
        )
        return _encode(canvas)

    return _encode(
        _draw_copy(canvas, lines, font, resolved, keyword_colors),
        for_text=True,
    )
