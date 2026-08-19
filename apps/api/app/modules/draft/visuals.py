"""코드로 렌더링하는 시각자료 — 차트·과정도·인포그래픽·비교표.

정확한 한글이 필요한 자료는 이미지 모델에 텍스트를 맡기지 않는다(모델은 한글을 반드시
깨뜨린다 — imaging.py의 썸네일 문구와 같은 이유). 모델은 구조화된 데이터(PlannedVisual)만
반환하고, 그리는 것은 여기서 PIL로 한다. 데이터가 함께 저장되므로 어떤 수치로 그렸는지
추적할 수 있다.

디자인 방향은 '전문 디자이너가 블로그 내용을 쉽게 설명하려고 직접 정리한 미니멀한
에디토리얼 인포그래픽'이다 — 기업 보고서·PPT·엑셀 캡처가 아니다. 그래서:
  - 진한 색으로 화면을 덮는 헤더, 굵은 외곽선, 강한 드롭 섀도, 노란 오프셋 그림자를 쓰지 않는다.
  - 제목만 Bold, 머리글은 옅은 틴트, 본문은 Regular로 위계를 나눈다.
  - 높이는 내용에 맞춰 동적으로 계산해 위아래로 빈 공간을 남기지 않는다.
  - 셀 텍스트는 두 줄로 줄바꿈→글자 축소하고, 그래도 넘치면 열을 나눠 세로로 배치한다.
  - 서비스명·제작 도구 워터마크를 넣지 않는다. 하단 표기는 '외부 출처가 있을 때'만 붙는다.

그래프(BAR/LINE/PIE)는 실측 수치와 출처가 있을 때만 그린다. 검증에 실패한 자료는
렌더링하지 않고 본문의 마커만 걷어낸다 — 근거 없는 그래프보다 그래프가 없는 편이 낫다.
"""

import base64
import io
import logging
import os
import re
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from app.llm.imaging import FontUnavailable, font_path
from app.shared import GeneratedPostImage, PlannedVisual, VisualTableRow
from app.shared.format import now_iso as _now

logger = logging.getLogger(__name__)

VISUAL_TAG_PATTERN = re.compile(r"\[\[VISUAL:\s*([^\]]+?)\s*]]")

# 네이버 본문 표시 폭(~966px)과 거의 같은 크기로 그린다 — 글자가 선명하게 보이는 규격이다.
# 높이는 고정하지 않는다. 내용이 적으면 줄이고 많으면 늘려, 위아래 빈 공간을 남기지 않는다.
_W = 960
# 외부 여백. 스펙 권장(40~56px) 안에서 통일한다.
_MARGIN = 44

# 실제로는 2배 크기로 그린 뒤 마지막에 절반으로 줄인다(수퍼샘플링).
#
# 표의 칸 중앙 같은 좌표는 나눗셈에서 나오므로 604.67처럼 소수가 된다. 그 자리에 글자를
# 그리면 획이 픽셀 두 칸에 반씩 걸쳐 흐려지고, 한글 세로획처럼 얇은 선은 획마다 농도가
# 달라져 '깨진' 것처럼 보인다. 2배로 그려 정수 좌표에 앉히고 LANCZOS로 줄이면 이 문제가
# 사라진다. 도형·글자 좌표는 아래 _Draw가 알아서 확대하므로, 그리는 쪽 코드는 예전처럼
# 960 기준으로 쓴다.
_SCALE = 2

_CHART_TYPES = ("BAR_CHART", "LINE_CHART", "PIE_CHART")


# ── 테마 ────────────────────────────────────────────────────────────────────
#
# 모든 도표가 똑같은 파란 템플릿으로 나오지 않게 하되, 매번 무작위 색을 뽑지도 않는다.
# 글의 카테고리가 검증된 테마 목록을 정하고(editorial_style.CATEGORY_PROFILES), 그 안에서
# variation_seed가 하나를 고른다. 색은 여기서만 정의된다 — 렌더러는 이름으로 찾아 쓴다.
#
#   page        도화지 바닥
#   card        카드·표 바탕
#   ink         본문 글자(가장 진한 값이지만 순검정은 피한다)
#   muted       보조 글자·라벨·출처
#   grid        아주 연한 눈금·구분선(옛 이름 faint)
#   line        카드 테두리(grid보다 살짝 진함, 옛 이름 hairline)
#   accent      한 개의 강조색(포인트)
#   accent_soft 강조색의 옅은 틴트(머리글 배경 등)
#   result      '결과·최종'만 짚는 색(과정도 마지막 단계)
#   result_soft 결과색 옅은 틴트
#   positive    좋은 값·승자
#   negative    나쁜 값·주의
#   bar_base    그래프 기본 막대·면(강조가 아닌 것)
#   radius      모서리 반경(테마의 성격을 가장 크게 바꾸는 값)
#   title/body/label_size  글자 위계
#   header_mode UNDERLINE(짧은 강조 밑줄) · RULE(가로 구분선) · TINT_BAR(왼쪽 틴트 바) · PLAIN
#   source_mode CAPTION(하단 한 줄) · NONE
#   dark        어두운 바탕 테마인가(막대·눈금 대비 계산이 달라진다)
def _theme(
    *,
    page,
    card,
    ink,
    muted,
    grid,
    line,
    accent,
    accent_soft,
    result,
    result_soft,
    positive,
    negative,
    bar_base,
    radius=10,
    title_size=29,
    body_size=17,
    label_size=15,
    header_mode="UNDERLINE",
    source_mode="CAPTION",
    chart_variant="VERTICAL_BAR",
    table_variant="STANDARD_GRID",
    process_variant="HORIZONTAL_STEPS",
    infographic_variant="HUB_AND_SPOKE",
    dark=False,
) -> dict:
    return {
        "page": page,
        "card": card,
        "ink": ink,
        "muted": muted,
        "grid": grid,
        "line": line,
        # 옛 이름. 기존 렌더러 코드가 그대로 동작하도록 함께 채운다.
        "faint": grid,
        "hairline": line,
        "accent": accent,
        "accent_soft": accent_soft,
        "result": result,
        "result_soft": result_soft,
        "positive": positive,
        "negative": negative,
        "bar_base": bar_base,
        "radius": radius,
        "title_size": title_size,
        "body_size": body_size,
        "label_size": label_size,
        "header_mode": header_mode,
        "source_mode": source_mode,
        "chart_variant": chart_variant,
        "table_variant": table_variant,
        "process_variant": process_variant,
        "infographic_variant": infographic_variant,
        "dark": dark,
    }


_THEMES: dict[str, dict] = {
    "EDITORIAL_NEUTRAL": _theme(
        page=(250, 250, 249), card=(255, 255, 255), ink=(33, 37, 44), muted=(124, 129, 138),
        grid=(234, 236, 240), line=(223, 226, 231), accent=(39, 87, 158),
        accent_soft=(233, 240, 249), result=(22, 122, 87), result_soft=(232, 245, 239),
        positive=(22, 122, 87), negative=(190, 74, 66), bar_base=(185, 199, 218),
    ),
    "BEAUTY_EDITORIAL": _theme(
        page=(253, 250, 251), card=(255, 255, 255), ink=(58, 45, 52), muted=(154, 134, 143),
        grid=(245, 236, 240), line=(236, 224, 230), accent=(186, 110, 127),
        accent_soft=(250, 238, 242), result=(150, 118, 162), result_soft=(246, 238, 249),
        positive=(126, 152, 126), negative=(198, 108, 108), bar_base=(230, 209, 217),
        radius=16, title_size=28, header_mode="TINT_BAR",
        table_variant="PROS_CONS_CARDS", infographic_variant="BEFORE_AFTER",
    ),
    "LIFESTYLE_JOURNAL": _theme(
        page=(251, 249, 244), card=(255, 255, 255), ink=(52, 46, 40), muted=(142, 131, 119),
        grid=(238, 233, 225), line=(228, 221, 210), accent=(176, 122, 84),
        accent_soft=(244, 236, 227), result=(122, 131, 78), result_soft=(238, 240, 226),
        positive=(122, 131, 78), negative=(186, 104, 86), bar_base=(216, 203, 187),
        radius=14, header_mode="TINT_BAR", table_variant="COMPACT_MOBILE",
        process_variant="VERTICAL_TIMELINE", infographic_variant="STACKED_SECTIONS",
    ),
    "FITNESS_PERFORMANCE": _theme(
        page=(250, 250, 250), card=(255, 255, 255), ink=(26, 30, 34), muted=(116, 124, 132),
        grid=(232, 234, 236), line=(218, 222, 226), accent=(16, 138, 116),
        accent_soft=(226, 244, 239), result=(214, 92, 60), result_soft=(252, 236, 229),
        positive=(16, 138, 116), negative=(214, 92, 60), bar_base=(178, 188, 194),
        radius=8, header_mode="RULE", table_variant="WINNER_HIGHLIGHT",
        process_variant="CHECKPOINT_FLOW", infographic_variant="BEFORE_AFTER",
    ),
    "TECH_BENCHMARK_LIGHT": _theme(
        page=(247, 248, 250), card=(255, 255, 255), ink=(30, 35, 42), muted=(118, 126, 138),
        grid=(231, 234, 239), line=(219, 224, 231), accent=(26, 122, 142),
        accent_soft=(226, 242, 245), result=(33, 120, 122), result_soft=(227, 243, 242),
        positive=(33, 130, 110), negative=(196, 88, 74), bar_base=(189, 206, 214),
        radius=8, header_mode="RULE", table_variant="SPEC_SHEET",
        process_variant="INPUT_OUTPUT_FLOW", infographic_variant="CAUSE_AND_EFFECT",
    ),
    "TECH_BENCHMARK_DARK": _theme(
        page=(22, 26, 32), card=(31, 37, 45), ink=(232, 236, 242), muted=(148, 159, 174),
        grid=(45, 52, 63), line=(58, 66, 79), accent=(70, 190, 210),
        accent_soft=(35, 58, 68), result=(120, 208, 168), result_soft=(31, 56, 48),
        positive=(120, 208, 168), negative=(228, 120, 104), bar_base=(78, 90, 106),
        radius=8, header_mode="RULE", table_variant="WINNER_HIGHLIGHT",
        process_variant="INPUT_OUTPUT_FLOW", infographic_variant="CAUSE_AND_EFFECT",
        dark=True,
    ),
    "GAMING_ESPORTS": _theme(
        page=(25, 22, 34), card=(35, 31, 48), ink=(236, 232, 245), muted=(160, 152, 180),
        grid=(50, 45, 68), line=(64, 58, 84), accent=(190, 108, 224),
        accent_soft=(58, 44, 78), result=(110, 198, 232), result_soft=(34, 52, 66),
        positive=(110, 198, 232), negative=(232, 108, 132), bar_base=(84, 76, 108),
        radius=12, header_mode="TINT_BAR", table_variant="WINNER_HIGHLIGHT",
        infographic_variant="KEYWORD_CLUSTER", dark=True,
    ),
    "FINANCE_REPORT": _theme(
        page=(255, 255, 255), card=(255, 255, 255), ink=(34, 38, 45), muted=(120, 126, 135),
        grid=(232, 234, 238), line=(215, 219, 226), accent=(37, 99, 175),
        accent_soft=(231, 239, 250), result=(37, 99, 175), result_soft=(231, 239, 250),
        positive=(24, 118, 88), negative=(190, 70, 62), bar_base=(151, 157, 166),
        radius=4, header_mode="RULE", table_variant="SPEC_SHEET",
        process_variant="INPUT_OUTPUT_FLOW", infographic_variant="STACKED_SECTIONS",
    ),
    "FOOD_TRAVEL": _theme(
        page=(252, 249, 244), card=(255, 255, 255), ink=(52, 42, 34), muted=(146, 130, 114),
        grid=(240, 233, 223), line=(230, 221, 208), accent=(192, 96, 58),
        accent_soft=(250, 238, 229), result=(122, 132, 74), result_soft=(238, 241, 226),
        positive=(122, 132, 74), negative=(198, 92, 74), bar_base=(220, 202, 182),
        radius=14, header_mode="TINT_BAR", table_variant="COMPACT_MOBILE",
        process_variant="VERTICAL_TIMELINE", infographic_variant="STACKED_SECTIONS",
    ),
    "EDUCATION_GUIDE": _theme(
        page=(249, 251, 250), card=(255, 255, 255), ink=(32, 42, 40), muted=(118, 132, 128),
        grid=(232, 240, 237), line=(220, 230, 226), accent=(40, 122, 104),
        accent_soft=(228, 243, 238), result=(46, 106, 150), result_soft=(230, 240, 249),
        positive=(40, 122, 104), negative=(196, 96, 80), bar_base=(186, 208, 200),
        radius=12, header_mode="TINT_BAR", table_variant="STANDARD_GRID",
        process_variant="CHECKPOINT_FLOW", infographic_variant="STACKED_SECTIONS",
    ),
    "BRAND_MINIMAL": _theme(
        page=(255, 255, 255), card=(250, 250, 249), ink=(26, 26, 27), muted=(132, 130, 126),
        grid=(238, 237, 234), line=(225, 224, 220), accent=(142, 116, 74),
        accent_soft=(246, 241, 232), result=(60, 60, 58), result_soft=(240, 240, 238),
        positive=(74, 106, 84), negative=(168, 88, 72), bar_base=(206, 202, 194),
        radius=2, title_size=30, header_mode="PLAIN", table_variant="SPEC_SHEET",
        process_variant="HORIZONTAL_STEPS", infographic_variant="TWO_COLUMN_EDITORIAL",
    ),
    "TREND_MAGAZINE": _theme(
        page=(250, 250, 250), card=(255, 255, 255), ink=(24, 24, 26), muted=(122, 122, 126),
        grid=(234, 234, 236), line=(220, 220, 223), accent=(202, 58, 58),
        accent_soft=(252, 234, 234), result=(38, 62, 128), result_soft=(232, 236, 249),
        positive=(38, 108, 92), negative=(202, 58, 58), bar_base=(198, 198, 202),
        radius=2, title_size=31, header_mode="RULE", table_variant="FEATURE_MATRIX",
        process_variant="VERTICAL_TIMELINE", infographic_variant="CAUSE_AND_EFFECT",
    ),
}

# 옛 저장 데이터의 프리셋 이름. 예전 글을 다시 열어도 그림이 바뀌지 않게 새 테마로 옮긴다.
_LEGACY_THEME_ALIASES = {
    "LIFESTYLE_SOFT": "LIFESTYLE_JOURNAL",
    "TECH_MINIMAL": "TECH_BENCHMARK_LIGHT",
    "PROFESSIONAL_DATA": "FINANCE_REPORT",
}

_DEFAULT_STYLE = "EDITORIAL_NEUTRAL"


def _style_for(visual: PlannedVisual) -> dict:
    """자료의 테마를 고른다. 명시값이 우선, 없으면 유형 기반 기본값."""
    key = (visual.style or "").upper()
    key = _LEGACY_THEME_ALIASES.get(key, key)
    if key in _THEMES:
        return _THEMES[key]
    if visual.type in _CHART_TYPES:
        return _THEMES["FINANCE_REPORT"]
    return _THEMES[_DEFAULT_STYLE]


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """두 색을 t만큼 섞는다(0=a, 1=b). 아주 옅은 틴트를 만들 때 쓴다."""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


# 본문용 보통 굵기 글꼴. imaging.font_path()는 썸네일 문구용이라 '굵은 자족 우선'으로
# 고르는데(작은 화면에서 읽혀야 하므로), 그걸 도표에 그대로 쓰면 제목·라벨·수치·출처가
# 전부 같은 굵기로 나와 위계가 사라진다 — 정보 그래픽이 밋밋해 보이는 가장 큰 이유다.
_REGULAR_FONT_CANDIDATES = (
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/AppleGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


@lru_cache(maxsize=1)
def _regular_font_path() -> str | None:
    return next((path for path in _REGULAR_FONT_CANDIDATES if os.path.isfile(path)), None)


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """보통 굵기가 기본이고, 강조할 것(제목·머리글)만 bold로 뽑는다.

    size는 960 기준의 논리 크기다. 실제 도화지가 _SCALE배이므로 글꼴도 그만큼 키워서
    돌려준다 — 폭을 재는 _text_width가 다시 나누므로 부르는 쪽은 신경 쓸 게 없다.

    보통 굵기 글꼴을 못 찾으면 굵은 것으로 되돌아간다 — 위계는 잃지만 글자는 나온다.
    """
    path = (_regular_font_path() or font_path()) if not bold else font_path()
    if path is None:
        raise FontUnavailable("no Korean font available for chart rendering")
    return ImageFont.truetype(path, size * _SCALE)


# 글자 폭을 재기 위한 도화지. 그리지 않고 측정만 한다.
_MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _text_width(text: str, font: ImageFont.FreeTypeFont) -> float:
    """논리 좌표(960 기준)에서의 글자 폭."""
    return _MEASURE.textlength(text, font=font) / _SCALE


def _clip(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    """글자수가 아니라 실제 폭으로 자른다.

    한글·숫자·기호는 글자당 폭이 제각각이라 글자수로 자르면 어떤 문구는 상자 밖으로
    삐져나가고(잘려 보이는 원인) 어떤 문구는 자리를 남긴 채 잘린다.
    """
    text = text.strip()
    if _text_width(text, font) <= max_width:
        return text
    while text and _text_width(f"{text}…", font) > max_width:
        text = text[:-1]
    return f"{text}…" if text else ""


def _all_lines(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """폭에 맞춰 필요한 만큼 줄을 나눈다(줄 수 제한 없음). 되도록 띄어쓰기에서 끊는다."""
    remaining = text.strip()
    lines: list[str] = []
    while remaining:
        if _text_width(remaining, font) <= max_width:
            lines.append(remaining)
            break
        cut = len(remaining)
        while cut > 1 and _text_width(remaining[:cut], font) > max_width:
            cut -= 1
        space = remaining.rfind(" ", 0, cut + 1)
        if space > 0:
            cut = space
        lines.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return lines


def _wrap(
    text: str, font: ImageFont.FreeTypeFont, max_width: float, max_lines: int
) -> list[str]:
    """상자 폭에 맞춰 줄을 나눈다. 줄 수를 넘기면 마지막 줄을 말줄임으로 닫는다."""
    lines = _all_lines(text, font, max_width)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    kept[-1] = _clip(f"{kept[-1]} {''.join(lines[max_lines:])}", font, max_width)
    return kept


def _fit_table_text(
    text: str,
    max_width: float,
    sizes: tuple[int, ...],
    preferred_lines: int = 2,
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """표의 핵심 문구는 말줄임하지 않는다.

    우선 큰 글자로 두 줄 안에 맞추고, 그래도 길면 최소 글자 크기에서 필요한 만큼 줄을
    늘린다. 표는 행 높이를 내용에 맞춰 계산하므로 세 번째 줄을 허용하는 편이 마지막 단어를
    ``…``로 잃는 것보다 안전하다.
    """
    text = text.strip()
    for size in sizes:
        font = _font(size)
        lines = _all_lines(text, font, max_width)
        if len(lines) <= preferred_lines:
            return lines, font
    font = _font(sizes[-1])
    return _all_lines(text, font, max_width), font


# 붙여 쓴 가운뎃점을 " · "로 띄운다 — '줌X·줌에어·리액트'를 '줌X · 줌에어 · 리액트'로.
# 나눗셈 단위(원/kWh)까지 건드리지 않도록 가운뎃점 계열만 대상으로 한다.
_MIDDOT = re.compile(r"\s*[·・•‧]\s*")


def _space_dots(text: str) -> str:
    return _MIDDOT.sub(" · ", text).strip()


_NUMERIC_TABLE_CELL = re.compile(
    r"^(?:약\s*)?[₩$€¥]?\s*[+-]?\d[\d,.]*"
    r"(?:\s*(?:~|〜|–|—|-)\s*[+-]?\d[\d,.]*)?"
    r"(?:\s*(?:%|원|(?:천|만|억|조)\s*원|개|명|회|배|점|초|분|시간|일|개월|년|"
    r"mm|cm|m|km|g|kg|W|kW|kWh|MB|GB|TB))?"
    r"(?:\s*/\s*(?:원|개|명|회|초|분|시간|일|개월|년|mm|cm|m|km|g|kg|W|kW|"
    r"kWh|MB|GB|TB))?$",
    re.IGNORECASE,
)
_STATUS_TABLE_CELLS = {
    "가능",
    "불가",
    "포함",
    "미포함",
    "지원",
    "미지원",
    "예",
    "아니오",
    "있음",
    "없음",
    "해당 없음",
    "o",
    "x",
    "○",
    "×",
    "✓",
    "✕",
    "-",
}


def _table_cell_alignment(value: str) -> str:
    """비교표 셀의 의미에 따른 정렬: 숫자는 우측, 상태는 중앙, 설명은 좌측."""
    normalized = " ".join(value.split()).strip()
    if _NUMERIC_TABLE_CELL.fullmatch(normalized):
        return "right"
    if normalized.lower() in _STATUS_TABLE_CELLS:
        return "center"
    return "left"


def _format_value(value: float, unit: str | None) -> str:
    number = f"{value:g}"
    return f"{number}{unit}" if unit else number


def _scaled(value: float) -> int:
    """논리 좌표 → 실제 도화지 좌표. 정수로 앉혀야 획이 픽셀 경계에 걸리지 않는다."""
    return round(value * _SCALE)


class _Draw:
    """좌표를 _SCALE배로 키워 실제 도화지에 그리는 얇은 껍데기.

    렌더러들은 예전처럼 960 기준으로 좌표를 쓰고, 확대는 여기서만 일어난다. 그래야
    수퍼샘플링을 넣거나 빼는 일이 상수 하나 바꾸는 일이 된다.
    """

    def __init__(self, draw: ImageDraw.ImageDraw):
        self._draw = draw

    def _points(self, points) -> list[tuple[int, int]]:
        return [(_scaled(x), _scaled(y)) for x, y in points]

    def _box(self, box) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = box
        return _scaled(x0), _scaled(y0), _scaled(x1), _scaled(y1)

    def text(self, xy, text, **kwargs):
        self._draw.text((_scaled(xy[0]), _scaled(xy[1])), text, **kwargs)

    def line(self, points, fill=None, width=1, **kwargs):
        self._draw.line(self._points(points), fill=fill, width=width * _SCALE, **kwargs)

    def polygon(self, points, **kwargs):
        self._draw.polygon(self._points(points), **kwargs)

    def rectangle(self, box, width=1, **kwargs):
        self._draw.rectangle(self._box(box), width=width * _SCALE, **kwargs)

    def rounded_rectangle(self, box, radius=0, width=1, **kwargs):
        self._draw.rounded_rectangle(
            self._box(box), radius=radius * _SCALE, width=width * _SCALE, **kwargs
        )

    def ellipse(self, box, width=1, **kwargs):
        self._draw.ellipse(self._box(box), width=width * _SCALE, **kwargs)

    def pieslice(self, box, start, end, width=1, **kwargs):
        self._draw.pieslice(self._box(box), start, end, width=width * _SCALE, **kwargs)


def _new(height: int, style: dict) -> tuple[Image.Image, _Draw]:
    image = Image.new("RGB", (_W * _SCALE, int(height) * _SCALE), style["page"])
    return image, _Draw(ImageDraw.Draw(image))


# 제목 영역 규격. 도화지 위쪽에서 시작해 짧은 강조 밑줄로 닫고, 필요하면 보조 한 줄을 둔다.
_TITLE_TOP = 30


def _title_size(style: dict | None) -> int:
    return (style or _THEMES[_DEFAULT_STYLE])["title_size"]


def _title_indent(style: dict | None) -> int:
    """TINT_BAR 테마는 제목 왼쪽에 얇은 강조 바가 서므로 그만큼 안으로 들인다."""
    return 16 if (style or {}).get("header_mode") == "TINT_BAR" else 0


def _title_lines(title: str, style: dict | None = None) -> list[str]:
    size = _title_size(style)
    return _wrap(
        title, _font(size, bold=True), _W - 2 * _MARGIN - _title_indent(style), 2
    )


def _header_height(title: str, lead: str | None, style: dict | None = None) -> int:
    """_header가 그릴 제목 영역의 높이(본문이 시작될 y). 도화지를 만들기 전에 높이를
    미리 알아야 하므로 그리기와 같은 계산을 순수 함수로 둔다."""
    line_h = _title_size(style) + 9
    y = _TITLE_TOP + len(_title_lines(title, style)) * line_h + 5
    return y + (14 + 24 if lead else 18)


def _header(image: Image.Image, draw: _Draw, title: str, style: dict, lead: str | None) -> int:
    """왼쪽 정렬 제목 + 테마별 마감. 장식은 콘텐츠보다 튀지 않는다.

    예전의 '제목 왼쪽 굵은 파란 막대'는 제목보다 눈에 띄어 광고 카드처럼 보였다. 그렇다고
    모든 도표가 같은 짧은 밑줄로 끝나면 그것도 하나의 템플릿이다 — 테마가 네 가지 마감
    중 하나를 고른다(짧은 밑줄·가로 구분선·왼쪽 틴트 바·없음).
    """
    size = _title_size(style)
    line_h = size + 9
    indent = _title_indent(style)
    title_font = _font(size, bold=True)
    lines = _title_lines(title, style)
    for index, line in enumerate(lines):
        draw.text(
            (_MARGIN + indent, _TITLE_TOP + index * line_h), line,
            font=title_font, fill=style["ink"], anchor="la",
        )
    y = _TITLE_TOP + len(lines) * line_h + 2

    mode = style.get("header_mode", "UNDERLINE")
    if mode == "UNDERLINE":
        draw.rounded_rectangle((_MARGIN, y, _MARGIN + 40, y + 3), radius=2, fill=style["accent"])
    elif mode == "RULE":
        draw.line([(_MARGIN, y + 1), (_W - _MARGIN, y + 1)], fill=style["line"], width=1)
    elif mode == "TINT_BAR":
        bar_top = _TITLE_TOP + 4
        draw.rounded_rectangle(
            (_MARGIN, bar_top, _MARGIN + 5, bar_top + len(lines) * line_h - 12),
            radius=3, fill=style["accent"],
        )
    y += 3
    if lead:
        lead_font = _font(16)
        draw.text(
            (_MARGIN + indent, y + 14), _clip(lead, lead_font, _W - 2 * _MARGIN - indent),
            font=lead_font, fill=style["muted"], anchor="la",
        )
        return y + 14 + 24
    return y + 18


def _footer_height(visual: PlannedVisual) -> int:
    """하단 표기(출처·기준·단위)가 차지할 높이. 표기할 게 없으면 최소 여백만 남긴다 —
    출처 없는 자체 구성 자료에 빈 문구 줄을 위해 공간을 비워 두지 않는다."""
    return 38 if visual.source else 22


def _footer(draw: _Draw, visual: PlannedVisual, style: dict, height: int) -> None:
    """하단 표기는 외부 출처가 있을 때만 붙인다 — 출처·기준 시점·단위.

    자체적으로 정리한 비교표·과정도·인포그래픽에는 서비스명·제작 도구 워터마크를 붙이지
    않는다(스펙: 출처가 없으면 하단 문구 자체를 표시하지 않는다).
    """
    if not visual.source or style.get("source_mode") == "NONE":
        return
    parts = [f"출처: {visual.source}"]
    if visual.published_at:
        parts.append(f"기준: {visual.published_at}")
    if visual.unit:
        parts.append(f"단위: {visual.unit}")
    font = _font(13)
    draw.text(
        (_MARGIN, height - 26),
        _clip(" · ".join(parts), font, _W - 2 * _MARGIN),
        font=font, fill=style["muted"], anchor="lm",
    )


def _encode(image: Image.Image) -> str:
    """2배로 그린 그림을 규격 폭으로 줄여 PNG로 굽는다.

    줄이면서 생기는 평균이 곧 안티에일리어싱이라, 바로 960에 그린 것보다 획 농도가 고르다.
    폭은 항상 960으로 고정하고 높이만 내용에 따라 달라진다.
    """
    if _SCALE != 1:
        image = image.resize(
            (image.width // _SCALE, image.height // _SCALE), Image.LANCZOS
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _axis_labels(
    draw: _Draw, visual: PlannedVisual, style: dict, *,
    left: float, right: float, top: float, bottom: float,
) -> None:
    """축 이름. 세로축 이름은 회전하지 않고 축 위에 가로로 둔다 — 돌려 쓴 한글은 블로그
    크기로 줄면 읽기 어렵다."""
    if visual.y_axis_label:
        font = _font(15)
        draw.text(
            (left, top - 22), _clip(visual.y_axis_label, font, (right - left) / 2),
            font=font, fill=style["muted"], anchor="la",
        )
    if visual.x_axis_label:
        font = _font(15)
        draw.text(
            ((left + right) / 2, bottom + 40), _clip(visual.x_axis_label, font, right - left),
            font=font, fill=style["muted"], anchor="ma",
        )


def _highlight_indexes(visual: PlannedVisual, data: list) -> set[int]:
    """어느 항목을 강조할 것인가.

    최댓값을 무조건 강조하지 않는다 — 그래프가 말하려는 것은 결론(conclusion)이지 최댓값이
    아닐 수 있다. 명시된 highlightLabels가 먼저고, 없으면 결론 문장에 이름이 등장하는 항목,
    그것도 없을 때만 최댓값이다.
    """
    wanted = {label.strip() for label in (visual.highlight_labels or []) if label.strip()}
    if wanted:
        matched = {i for i, point in enumerate(data) if point.label.strip() in wanted}
        if matched:
            return matched
    conclusion = (visual.conclusion or "").strip()
    if conclusion:
        matched = {
            i
            for i, point in enumerate(data)
            if point.label.strip() and point.label.strip() in conclusion
        }
        if matched:
            return matched
    peak = max((point.value for point in data), default=0.0)
    return {i for i, point in enumerate(data) if point.value == peak}


def _bar_orientation(visual: PlannedVisual, data: list) -> str:
    """항목 이름이 길거나 개수가 많으면 가로 막대. 짧고 적으면 세로 막대.

    세로 막대에 여덟 글자짜리 라벨을 넣으면 라벨이 겹치거나 말줄임된다 — 그때 정보를 잃는
    것은 축이 아니라 항목 이름이다.
    """
    variant = (visual.layout_variant or "").upper()
    if variant in ("HORIZONTAL_BAR", "VERTICAL_BAR"):
        return variant
    longest = max((len(point.label.strip()) for point in data), default=0)
    return "HORIZONTAL_BAR" if longest > 6 or len(data) > 5 else "VERTICAL_BAR"


def _render_bar_chart(visual: PlannedVisual) -> str:
    data = visual.data or []
    if _bar_orientation(visual, data) == "HORIZONTAL_BAR":
        return _render_horizontal_bar_chart(visual)
    return _render_vertical_bar_chart(visual)


def _render_vertical_bar_chart(visual: PlannedVisual) -> str:
    style = _style_for(visual)
    data = visual.data or []
    # 결론은 아래 큰 배너 대신 제목 밑 짧은 주석으로. 그래프보다 튀지 않는다.
    content_top = _header_height(visual.title, visual.conclusion, style)
    plot_h = 288
    chart_top = content_top + 16
    chart_bottom = chart_top + plot_h
    label_row = 30
    axis_h = 24 if visual.x_axis_label else 0
    footer_h = _footer_height(visual)
    height = chart_bottom + label_row + axis_h + footer_h + 6
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, visual.conclusion)

    chart_left, chart_right = float(_MARGIN), float(_W - _MARGIN)
    peak = max((point.value for point in data), default=1.0) or 1.0
    slot = (chart_right - chart_left) / len(data)
    bar_width = min(116, int(slot * 0.5))
    highlighted = _highlight_indexes(visual, data)
    radius = max(2, min(8, style["radius"]))

    # 눈금선은 얇고 옅게. 축을 굵게 두르지 않는다.
    for offset in range(4):
        y = chart_bottom - (chart_bottom - chart_top) * offset / 3
        draw.line([(chart_left, y), (chart_right, y)], fill=style["grid"], width=1)

    # 한 계열의 막대는 모두 같은 것을 재므로 색도 같아야 한다. 무지개로 칠하면 색이 뜻을
    # 가진 것처럼 보인다. 대신 결론과 연결된 항목만 강조색으로 짚어 준다.
    for index, point in enumerate(data):
        x0 = chart_left + slot * index + (slot - bar_width) / 2
        height_px = (chart_bottom - chart_top) * (max(point.value, 0.0) / peak)
        highlight = index in highlighted
        draw.rounded_rectangle(
            (x0, chart_bottom - height_px, x0 + bar_width, chart_bottom),
            radius=radius, fill=style["accent"] if highlight else style["bar_base"],
        )
        draw.text(
            (x0 + bar_width / 2, chart_bottom - height_px - 10),
            _format_value(point.value, visual.unit),
            font=_font(16, bold=highlight),
            fill=style["ink"] if highlight else style["muted"], anchor="ms",
        )
        draw.text(
            (x0 + bar_width / 2, chart_bottom + 12),
            _clip(_space_dots(point.label), _font(16), slot - 10),
            font=_font(16), fill=style["muted"], anchor="ma",
        )

    _axis_labels(
        draw, visual, style,
        left=chart_left, right=chart_right, top=chart_top, bottom=chart_bottom,
    )
    _footer(draw, visual, style, height)
    return _encode(image)


def _render_horizontal_bar_chart(visual: PlannedVisual) -> str:
    """항목 이름이 왼쪽에 가로로 놓이는 막대그래프.

    이름을 돌리지도, 말줄임하지도 않는다 — 이름이 길어서 세로 막대를 못 쓰는 것이므로
    이름을 살리는 것이 이 변형의 존재 이유다.
    """
    style = _style_for(visual)
    data = visual.data or []
    content_top = _header_height(visual.title, visual.conclusion, style)
    label_font = _font(17)
    value_font = _font(16, bold=True)

    label_w = min(
        300.0,
        max(
            (_text_width(_space_dots(point.label), label_font) for point in data),
            default=100.0,
        )
        + 16,
    )
    track_left = _MARGIN + label_w + 14
    value_w = (
        max(
            (
                _text_width(_format_value(point.value, visual.unit), value_font)
                for point in data
            ),
            default=40.0,
        )
        + 16
    )
    track_right = _W - _MARGIN - value_w

    row_h, bar_h = 46, 24
    top = content_top + 8
    footer_h = _footer_height(visual)
    axis_h = 22 if visual.x_axis_label else 0
    height = int(top + row_h * len(data) + axis_h + footer_h + 6)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, visual.conclusion)

    peak = max((point.value for point in data), default=1.0) or 1.0
    highlighted = _highlight_indexes(visual, data)
    radius = max(2, min(6, style["radius"]))

    for index, point in enumerate(data):
        centre = top + row_h * index + row_h / 2
        highlight = index in highlighted
        draw.text(
            (_MARGIN, centre),
            _clip(_space_dots(point.label), label_font, label_w),
            font=label_font,
            fill=style["ink"] if highlight else style["muted"],
            anchor="lm",
        )
        # 옅은 트랙 위에 실제 값 막대. 트랙이 있으면 값들의 상대 크기가 한눈에 읽힌다.
        draw.rounded_rectangle(
            (track_left, centre - bar_h / 2, track_right, centre + bar_h / 2),
            radius=radius, fill=style["grid"],
        )
        length = (track_right - track_left) * (max(point.value, 0.0) / peak)
        if length > 1:
            draw.rounded_rectangle(
                (track_left, centre - bar_h / 2, track_left + length, centre + bar_h / 2),
                radius=radius, fill=style["accent"] if highlight else style["bar_base"],
            )
        draw.text(
            (_W - _MARGIN, centre),
            _format_value(point.value, visual.unit),
            font=value_font if highlight else _font(16),
            fill=style["ink"] if highlight else style["muted"],
            anchor="rm",
        )

    if visual.x_axis_label:
        draw.text(
            ((track_left + track_right) / 2, top + row_h * len(data) + 12),
            _clip(visual.x_axis_label, _font(15), track_right - track_left),
            font=_font(15), fill=style["muted"], anchor="ma",
        )
    _footer(draw, visual, style, height)
    return _encode(image)


def _render_line_chart(visual: PlannedVisual) -> str:
    style = _style_for(visual)
    data = visual.data or []
    content_top = _header_height(visual.title, visual.conclusion, style)
    plot_h = 288
    chart_top = content_top + 16
    chart_bottom = chart_top + plot_h
    label_row = 30
    axis_h = 24 if visual.x_axis_label else 0
    footer_h = _footer_height(visual)
    height = chart_bottom + label_row + axis_h + footer_h + 6
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, visual.conclusion)

    chart_left, chart_right = float(_MARGIN + 8), float(_W - _MARGIN - 8)
    values = [point.value for point in data]
    low, high = min(values), max(values)
    span = (high - low) or 1.0

    for offset in range(4):
        y = chart_bottom - (chart_bottom - chart_top) * offset / 3
        draw.line([(chart_left, y), (chart_right, y)], fill=style["grid"], width=1)

    step = (chart_right - chart_left) / (len(data) - 1)
    points = [
        (
            chart_left + step * index,
            chart_bottom - (chart_bottom - chart_top) * ((point.value - low) / span),
        )
        for index, point in enumerate(data)
    ]
    draw.line(points, fill=style["accent"], width=3, joint="curve")
    # 한 줄짜리 선그래프에서 모든 점에 값을 붙이면 숫자가 선을 가린다. 결론과 연결된
    # 변곡점만 크게 짚고 나머지는 옅게 둔다.
    highlighted = _highlight_indexes(visual, data)
    for index, ((x, y), point) in enumerate(zip(points, data)):
        highlight = index in highlighted
        size = 6 if highlight else 4
        draw.ellipse(
            (x - size, y - size, x + size, y + size),
            fill=style["accent"] if highlight else style["card"],
            outline=style["accent"], width=3 if highlight else 2,
        )
        draw.text(
            (x, y - 13), _format_value(point.value, visual.unit),
            font=_font(15, bold=highlight),
            fill=style["ink"] if highlight else style["muted"], anchor="ms",
        )
        draw.text(
            (x, chart_bottom + 12), _clip(_space_dots(point.label), _font(15), step - 6),
            font=_font(15), fill=style["muted"], anchor="ma",
        )

    _axis_labels(
        draw, visual, style,
        left=chart_left, right=chart_right, top=chart_top, bottom=chart_bottom,
    )
    _footer(draw, visual, style, height)
    return _encode(image)


def _render_pie_chart(visual: PlannedVisual) -> str:
    style = _style_for(visual)
    data = [point for point in (visual.data or []) if point.value > 0]
    content_top = _header_height(visual.title, visual.conclusion, style)
    footer_h = _footer_height(visual)
    size = 300
    height = content_top + size + footer_h + 16
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, visual.conclusion)

    total = sum(point.value for point in data)
    cx, cy = _MARGIN + size / 2, content_top + size / 2
    box = (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2)

    # 파이 조각은 한 강조색을 명도로 단계지어 쓴다 — 조각마다 다른 색을 칠하면 색이 뜻을
    # 가진 것처럼 보인다. 가장 큰 조각만 진한 강조색으로 짚는다.
    ordered = sorted(range(len(data)), key=lambda i: data[i].value, reverse=True)
    shade = {}
    for rank, i in enumerate(ordered):
        t = 0.0 if rank == 0 else min(0.62, 0.24 + rank * 0.16)
        shade[i] = _mix(style["accent"], style["card"], t)

    angle = -90.0
    for index, point in enumerate(data):
        sweep = 360.0 * point.value / total
        draw.pieslice(box, angle, angle + sweep, fill=shade[index], outline=style["page"], width=2)
        angle += sweep

    legend_x = cx + size / 2 + 44
    legend_y = content_top + 24
    legend_font = _font(18)
    legend_w = _W - _MARGIN - (legend_x + 28)
    for index, point in enumerate(data):
        share = f"  {100.0 * point.value / total:.0f}%"
        draw.rounded_rectangle(
            (legend_x, legend_y, legend_x + 16, legend_y + 16), radius=4, fill=shade[index]
        )
        label = _clip(_space_dots(point.label), legend_font, legend_w - _text_width(share, legend_font))
        draw.text(
            (legend_x + 26, legend_y + 8), f"{label}{share}",
            font=legend_font, fill=style["ink"], anchor="lm",
        )
        legend_y += 34

    _footer(draw, visual, style, height)
    return _encode(image)


def _process_columns(count: int, detailed: bool) -> int:
    """단계 수에 맞춘 열 수.

    무조건 3열로 깔면 4단계가 3+1이 되어 아래 절반이 통째로 빈다. 남는 칸이 없도록
    나누고, 그래도 남으면 마지막 줄만 덜 차게 둔다.

    계산식이 붙은 단계(detailed)는 2열까지만 쓴다 — 3열이면 상자가 좁아 '1.5 × 8시간 ×
    30일 = 360kWh' 같은 식이 잘린다. 계산 과정에서 잘리면 안 되는 건 바로 그 식이다.
    """
    if count <= 2:
        return count
    if detailed:
        return 2
    if count == 3:
        return 3
    if count == 4:
        return 2
    return 3


def _process_variant(visual: PlannedVisual, style: dict) -> str:
    """이 과정도의 배치. 데이터 모양이 테마 기본값보다 우선한다.

    - 계산식이 흐르는 과정이면 입력→변환→결과가 유일하게 맞는 그림이다.
    - 단계 설명이 길거나 다섯 단계를 넘으면 가로로 늘어놓을 수 없다.
    """
    steps = visual.steps or []
    details = [step.detail for step in steps if step.detail]
    variant = (visual.layout_variant or "").upper()
    if variant in _PROCESS_RENDERERS:
        return variant
    if details and len(details) >= max(2, len(steps) - 1):
        return "INPUT_OUTPUT_FLOW"
    if len(steps) >= 5 or any(len(step.label) > 14 for step in steps):
        return "VERTICAL_TIMELINE"
    theme_variant = style.get("process_variant", "HORIZONTAL_STEPS")
    return theme_variant if theme_variant in _PROCESS_RENDERERS else "HORIZONTAL_STEPS"


def _render_process_diagram(visual: PlannedVisual) -> str:
    style = _style_for(visual)
    return _PROCESS_RENDERERS[_process_variant(visual, style)](visual)


def _render_process_snake(visual: PlannedVisual) -> str:
    """가로로 흐르다 줄이 바뀌면 뱀처럼 되꺾이는 배치. 3~4단계의 짧은 절차에 맞다."""
    style = _style_for(visual)
    steps = visual.steps or []
    details = [step.detail for step in steps if step.detail]
    per_row = _process_columns(len(steps), bool(details))
    rows = (len(steps) + per_row - 1) // per_row

    # 화살표는 이 간격을 정확히 가로질러 앞 상자 경계에서 다음 상자 경계로 닿는다.
    col_gap, row_gap = 54, 56
    box_w = (_W - 2 * _MARGIN - (per_row - 1) * col_gap) / per_row
    # 번호는 카드 왼쪽 안 원형 배지로 나가므로, 글자는 배지 오른쪽부터 카드 끝까지 쓴다.
    badge = 28
    text_x_pad = 16 + badge + 12
    text_w = box_w - text_x_pad - 14
    label_font = _font(20, bold=True)
    detail_font = _fit_font(details, text_w) if details else _font(17)

    # 같은 계층의 상자는 같은 크기를 쓴다. 높이는 가장 긴 단계에 맞춰 한 번만 정한다 —
    # 상자마다 높이가 다르면 화살표가 중앙선에서 어긋나고 흐름이 삐뚤어 보인다.
    wrapped = [_wrap(step.label, label_font, text_w, 2) for step in steps]
    has_detail = any(step.detail for step in steps)
    label_lines = max((len(lines) for lines in wrapped), default=1)
    line_h = 26
    box_h = 22 + line_h * label_lines + (26 if has_detail else 0) + 22

    content_top = _header_height(visual.title, None, style)
    footer_h = _footer_height(visual)
    height = int(content_top + rows * box_h + (rows - 1) * row_gap + footer_h + 8)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    def slot_x(index: int) -> float:
        row, col = divmod(index, per_row)
        # 짝수 줄은 왼→오, 홀수 줄은 오→왼(뱀 배치). 줄이 바뀔 때 다음 단계가 바로 아래에
        # 오므로 아래 화살표가 실제 순서를 가리킨다 — 늘 왼쪽부터 채우면 2→4를 가리킨다.
        slot = col if row % 2 == 0 else per_row - 1 - col
        return _MARGIN + slot * (box_w + col_gap)

    def box_of(index: int) -> tuple[float, float, float, float]:
        x0 = slot_x(index)
        y0 = content_top + (index // per_row) * (box_h + row_gap)
        return x0, y0, x0 + box_w, y0 + box_h

    for index, step in enumerate(steps):
        row, col = divmod(index, per_row)
        x0, y0, x1, y1 = box_of(index)
        # 마지막 단계는 결과다. 색으로 구분해 '여기가 답'임을 한눈에 보이게 한다.
        final = index + 1 == len(steps)
        accent = style["result"] if final else style["accent"]
        draw.rounded_rectangle(
            (x0, y0, x1, y1), radius=12,
            fill=style["result_soft"] if final else style["card"],
            outline=accent if final else style["hairline"], width=1,
        )
        # 번호는 채워진 원형 배지로 — 구석의 작은 숫자는 장식처럼 보이고 순서로 안 읽힌다.
        # 배지는 언제나 상자 안, 글자와 같은 줄에 둔다.
        bx, by = x0 + 16, y0 + box_h / 2 - badge / 2
        draw.ellipse((bx, by, bx + badge, by + badge), fill=accent)
        draw.text(
            (bx + badge / 2, by + badge / 2 + 1), str(index + 1),
            font=_font(15, bold=True), fill=style["card"], anchor="mm",
        )

        lines = wrapped[index]
        detail = _clip(step.detail, detail_font, text_w) if step.detail else ""
        block_h = line_h * len(lines) + (26 if detail else 0)
        line_top = y0 + (box_h - block_h) / 2 + line_h / 2
        text_x = x0 + text_x_pad
        for line_index, line in enumerate(lines):
            draw.text(
                (text_x, line_top + line_h * line_index), line,
                font=label_font, fill=style["ink"], anchor="lm",
            )
        if detail:
            draw.text(
                (text_x, line_top + line_h * len(lines)), detail,
                font=detail_font, fill=style["result"] if final else style["muted"], anchor="lm",
            )

        if final:
            continue
        # 화살표: 앞 상자 경계에서 시작해 정확히 간격을 가로질러 다음 상자 경계에 닿는다.
        if col < per_row - 1:
            ay = (y0 + y1) / 2
            direction = 1 if row % 2 == 0 else -1
            start = (x1 if direction > 0 else x0)
            tip = start + direction * col_gap
            draw.line([(start, ay), (tip - direction * 9, ay)], fill=style["accent"], width=3)
            draw.polygon(
                [(tip - direction * 9, ay - 7), (tip - direction * 9, ay + 7), (tip, ay)],
                fill=style["accent"],
            )
        else:
            # 줄 끝 → 다음 단계는 바로 아래 칸이다(뱀 배치). 아래 상자 윗변에 닿게 내린다.
            ax = (x0 + x1) / 2
            tip = y1 + row_gap
            draw.line([(ax, y1), (ax, tip - 9)], fill=style["accent"], width=3)
            draw.polygon([(ax - 7, tip - 9), (ax + 7, tip - 9), (ax, tip)], fill=style["accent"])

    _footer(draw, visual, style, height)
    return _encode(image)


def _render_process_vertical(visual: PlannedVisual, *, checkpoints: bool = False) -> str:
    """세로 타임라인. 왼쪽에 하나의 줄기가 서고, 단계마다 배지와 카드가 붙는다.

    단계 설명이 길 때 가로 배치보다 확실히 낫다 — 가로는 상자 폭이 곧 글자 폭이라 설명이
    길면 잘리는데, 세로는 폭이 도화지 전체다. checkpoints=True면 배지가 체크 표시가 되어
    '확인하며 진행하는 절차'로 읽힌다.
    """
    style = _style_for(visual)
    steps = visual.steps or []
    content_top = _header_height(visual.title, None, style)

    rail_x = _MARGIN + 18
    card_left = rail_x + 34
    card_right = float(_W - _MARGIN)
    text_left = card_left + 20
    text_w = card_right - text_left - 20

    label_font = _font(20, bold=True)
    detail_font = _font(16)
    wrapped = [_wrap(step.label, label_font, text_w, 2) for step in steps]
    details = [
        _wrap(step.detail, detail_font, text_w, 2) if step.detail else [] for step in steps
    ]
    line_h, detail_h = 27, 23
    heights = [
        22 + line_h * len(wrapped[index]) + detail_h * len(details[index]) + 20
        for index in range(len(steps))
    ]
    gap = 16
    footer_h = _footer_height(visual)
    height = int(content_top + sum(heights) + gap * max(0, len(steps) - 1) + footer_h + 8)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    badge = 26
    y = float(content_top)
    tops: list[float] = []
    for index, step in enumerate(steps):
        card_h = heights[index]
        tops.append(y)
        final = index + 1 == len(steps)
        accent = style["result"] if final else style["accent"]
        draw.rounded_rectangle(
            (card_left, y, card_right, y + card_h),
            radius=style["radius"],
            fill=style["result_soft"] if final else style["card"],
            outline=accent if final else style["line"],
            width=1,
        )
        text_y = y + 22 + line_h / 2
        for line_index, line in enumerate(wrapped[index]):
            draw.text(
                (text_left, text_y + line_h * line_index), line,
                font=label_font, fill=style["ink"], anchor="lm",
            )
        detail_y = text_y + line_h * len(wrapped[index]) + 2
        for line_index, line in enumerate(details[index]):
            draw.text(
                (text_left, detail_y + detail_h * line_index), line,
                font=detail_font,
                fill=style["result"] if final else style["muted"], anchor="lm",
            )
        # 줄기 위의 배지. 카드 첫 줄 높이에 맞춰 앉힌다.
        by = y + 22 + line_h / 2 - badge / 2
        draw.ellipse((rail_x - badge / 2, by, rail_x + badge / 2, by + badge), fill=accent)
        if checkpoints:
            draw.line(
                [
                    (rail_x - 6, by + badge / 2),
                    (rail_x - 1, by + badge / 2 + 5),
                    (rail_x + 6, by + badge / 2 - 5),
                ],
                fill=style["card"], width=3,
            )
        else:
            draw.text(
                (rail_x, by + badge / 2 + 1), str(index + 1),
                font=_font(14, bold=True), fill=style["card"], anchor="mm",
            )
        y += card_h + gap

    # 줄기는 첫 배지에서 마지막 배지까지만 그린다. 카드 뒤로 지나가지 않도록 먼저 그린 배지
    # 위치를 이용해 구간마다 끊어 잇는다.
    for index in range(len(steps) - 1):
        start = tops[index] + 22 + line_h / 2 + badge / 2
        end = tops[index + 1] + 22 + line_h / 2 - badge / 2
        draw.line([(rail_x, start), (rail_x, end)], fill=style["grid"], width=3)

    _footer(draw, visual, style, height)
    return _encode(image)


def _render_process_checkpoints(visual: PlannedVisual) -> str:
    return _render_process_vertical(visual, checkpoints=True)


def _render_process_io(visual: PlannedVisual) -> str:
    """입력 → 변환 → 결과. 숫자가 흐르는 과정에서 계산식이 잘리지 않게 하는 배치.

    첫 단계는 입력, 마지막은 결과로 색이 갈리고, 가운데 변환 단계들은 같은 계층으로
    한 줄에 쌓인다. 번호·상자·화살표가 따로 노는 구조를 피하려고 화살표는 항상 앞 상자
    아래변에서 다음 상자 윗변으로 곧게 내린다.
    """
    style = _style_for(visual)
    steps = visual.steps or []
    content_top = _header_height(visual.title, None, style)

    box_left, box_right = float(_MARGIN), float(_W - _MARGIN)
    inner_w = box_right - box_left - 40
    label_font = _font(20, bold=True)
    detail_font = _font(17)
    wrapped = [_wrap(step.label, label_font, inner_w * 0.42, 2) for step in steps]
    details = [
        _wrap(step.detail, detail_font, inner_w * 0.54, 2) if step.detail else []
        for step in steps
    ]
    line_h = 26
    heights = [
        18 + line_h * max(len(wrapped[i]), len(details[i]) or 1) + 18 for i in range(len(steps))
    ]
    arrow_gap = 30
    footer_h = _footer_height(visual)
    height = int(
        content_top + sum(heights) + arrow_gap * max(0, len(steps) - 1) + footer_h + 8
    )
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    y = float(content_top)
    for index, step in enumerate(steps):
        box_h = heights[index]
        first, final = index == 0, index + 1 == len(steps)
        if final:
            fill, outline, ink = style["result_soft"], style["result"], style["result"]
        elif first:
            fill, outline, ink = style["accent_soft"], style["accent"], style["accent"]
        else:
            fill, outline, ink = style["card"], style["line"], style["ink"]
        draw.rounded_rectangle(
            (box_left, y, box_right, y + box_h), radius=style["radius"],
            fill=fill, outline=outline, width=1,
        )
        label_top = y + (box_h - line_h * len(wrapped[index])) / 2 + line_h / 2
        for line_index, line in enumerate(wrapped[index]):
            draw.text(
                (box_left + 20, label_top + line_h * line_index), line,
                font=label_font, fill=ink, anchor="lm",
            )
        if details[index]:
            detail_top = y + (box_h - line_h * len(details[index])) / 2 + line_h / 2
            for line_index, line in enumerate(details[index]):
                draw.text(
                    (box_right - 20, detail_top + line_h * line_index), line,
                    font=detail_font,
                    fill=style["result"] if final else style["muted"], anchor="rm",
                )
        if not final:
            centre = (box_left + box_right) / 2
            tip = y + box_h + arrow_gap
            draw.line([(centre, y + box_h), (centre, tip - 9)], fill=style["accent"], width=3)
            draw.polygon(
                [(centre - 7, tip - 9), (centre + 7, tip - 9), (centre, tip)],
                fill=style["accent"],
            )
        y += box_h + arrow_gap

    _footer(draw, visual, style, height)
    return _encode(image)


_PROCESS_RENDERERS = {
    "HORIZONTAL_STEPS": _render_process_snake,
    "SNAKE_FLOW": _render_process_snake,
    "VERTICAL_TIMELINE": _render_process_vertical,
    "CHECKPOINT_FLOW": _render_process_checkpoints,
    "INPUT_OUTPUT_FLOW": _render_process_io,
}


def _fit_font(texts: list[str], max_width: float) -> ImageFont.FreeTypeFont:
    """주어진 문구가 모두 한 줄에 들어가는 가장 큰 글자 크기. 같은 역할의 글자는 같은 크기."""
    for size in (17, 16, 15, 14):
        font = _font(size)
        if all(_text_width(text, font) <= max_width for text in texts):
            return font
    return _font(14)


def _table_name_width(
    rows: list[VisualTableRow],
    criterion_count: int,
    table_width: float,
) -> float:
    """비교 대상 열 폭. 긴 이름 하나가 나머지 기준 열을 지나치게 좁히지 않게 제한한다."""
    name_probe = _font(18, bold=True)
    longest = max((_text_width(row.name, name_probe) for row in rows), default=0.0)
    desired = min(240.0, max(178.0, longest + 34))
    return min(desired, table_width - 165.0 * criterion_count)


def _table_group_fits_two_lines(
    columns: list[str],
    rows: list[VisualTableRow],
    indexes: list[int],
    table_width: float,
) -> bool:
    """한 열 그룹이 최소 글자 크기에서 모든 핵심 정보를 두 줄 안에 보존하는지 판단한다."""
    name_width = _table_name_width(rows, len(indexes), table_width)
    cell_width = (table_width - name_width) / len(indexes)
    if any(
        len(_all_lines(row.name, _font(15), name_width - 40)) > 2
        for row in rows
    ):
        return False
    if any(
        len(_all_lines(_space_dots(columns[index]), _font(14, bold=True), cell_width - 16))
        > 2
        for index in indexes
    ):
        return False
    return all(
        len(
            _all_lines(
                _space_dots(row.cells[index]),
                _font(14),
                cell_width - 28,
            )
        )
        <= 2
        for row in rows
        for index in indexes
    )


def _table_column_groups(
    columns: list[str],
    rows: list[VisualTableRow],
    table_width: float = float(_W - 2 * _MARGIN),
) -> list[list[int]]:
    """두 줄을 넘기는 긴 표는 열을 나눠 한 PNG 안에 세로로 쌓는다.

    각 블록은 첫 열인 ``비교 대상``을 다시 표시한다. 보통 4열 표는 2+2, 3열 표는
    2+1로 갈라지며, 이미 두 줄 안에 들어가는 짧은 표는 하나의 블록을 유지한다.
    """

    def split(indexes: list[int]) -> list[list[int]]:
        if len(indexes) == 1 or _table_group_fits_two_lines(
            columns, rows, indexes, table_width
        ):
            return [indexes]
        midpoint = (len(indexes) + 1) // 2
        return split(indexes[:midpoint]) + split(indexes[midpoint:])

    return split(list(range(len(columns))))


def _table_highlights(visual: PlannedVisual) -> set[str]:
    """포인트를 줄 셀·대상 이름.

    한 행 전체를 무조건 강조하지 않는다 — 추천 결론과 관련된 값에만 색을 준다. 명시된
    highlightLabels가 먼저고, 없으면 결론 문장에 이름이 나오는 비교 대상이다.
    """
    wanted = {label.strip() for label in (visual.highlight_labels or []) if label.strip()}
    if wanted:
        return wanted
    conclusion = (visual.conclusion or "").strip()
    if not conclusion:
        return set()
    return {
        row.name.strip()
        for row in (visual.rows or [])
        if row.name.strip() and row.name.strip() in conclusion
    }


def _table_variant(visual: PlannedVisual, style: dict) -> str:
    """이 표의 배치. 데이터 모양이 테마 기본값을 이긴다.

    열이 많고 대상도 많으면 가로 격자로는 모바일에서 읽히지 않고, 기준이 장단점 두 축이면
    격자보다 카드가 맞다. 계획이 고른 변형은 그 위에서만 존중한다.
    """
    columns = visual.columns or []
    rows = visual.rows or []
    variant = (visual.layout_variant or "").upper()
    if variant in _TABLE_RENDERERS:
        return variant
    if len(columns) >= 4 and len(rows) >= 4:
        return "COMPACT_MOBILE"
    if len(columns) == 2 and _looks_like_pros_cons(columns):
        return "PROS_CONS_CARDS"
    theme_variant = style.get("table_variant", "STANDARD_GRID")
    return theme_variant if theme_variant in _TABLE_RENDERERS else "STANDARD_GRID"


_PROS_WORDS = ("장점", "좋은", "강점", "이점")
_CONS_WORDS = ("단점", "아쉬", "한계", "약점", "주의")


def _looks_like_pros_cons(columns: list[str]) -> bool:
    joined = " ".join(columns)
    return any(word in joined for word in _PROS_WORDS) and any(
        word in joined for word in _CONS_WORDS
    )


def _render_comparison_table(visual: PlannedVisual) -> str:
    """비교표. 모든 행이 같은 기준(columns)으로 채워진다 — 대상마다 다른 잣대를 쓰면
    비교가 아니라 소개가 된다.

    배치는 하나가 아니다: 표준 격자, 기준 매트릭스, 승자 강조, 사양표, 모바일 세로형,
    2대1 분할, 장단점 카드 중 데이터와 테마가 고른 것으로 그린다. 색만 바꾼 같은 표를
    다시 쓰지 않는 것이 이 분기의 목적이다.
    """
    style = _style_for(visual)
    return _TABLE_RENDERERS[_table_variant(visual, style)](visual)


def _render_table_grid(
    visual: PlannedVisual,
    *,
    header_tint: bool = True,
    zebra: bool = False,
    highlight: bool = False,
    name_label: str = "비교 대상",
) -> str:
    """가로 격자. 칸이 비어도 자리는 남겨 열이 밀리지 않게 한다.

    진한 파란 헤더·굵은 격자·큰 카드 그림자를 쓰지 않는다. 머리글은 옅은 틴트 띠 하나
    (또는 얇은 구분선), 구분선은 아주 연하게, 표는 얇은 테두리로만 배경에서 떼어 놓는다.
    """
    style = _style_for(visual)
    columns = [_space_dots(c) for c in (visual.columns or [])]
    rows = visual.rows or []
    highlights = _table_highlights(visual) if highlight else set()

    content_top = _header_height(visual.title, None, style)
    table_left, table_right = float(_MARGIN), float(_W - _MARGIN)
    table_w = table_right - table_left

    header_h = 52
    name_pad, cell_pad = 20, 14
    line_h = 24

    # 최소 글자 크기에서도 두 줄을 넘는 표는 열 그룹으로 나눈다. 각 그룹은 같은 폭의
    # 독립된 표 블록이며 첫 열을 반복하므로, 긴 셀을 말줄임하거나 지나치게 축소하지 않는다.
    column_groups = _table_column_groups(columns, rows, table_w)
    block_gap = 22
    block_plans = []
    for indexes in column_groups:
        name_w = _table_name_width(rows, len(indexes), table_w)
        cell_w = (table_w - name_w) / len(indexes)
        row_plans = []
        for row in rows:
            name_lines, name_font = _fit_table_text(
                row.name, name_w - 2 * name_pad, (18, 16, 15)
            )
            cell_layouts = []
            max_lines = len(name_lines)
            for col_index in indexes:
                raw = _space_dots(row.cells[col_index])
                lines, font = _fit_table_text(
                    raw, cell_w - 2 * cell_pad, (17, 16, 15, 14)
                )
                cell_layouts.append((lines, font, _table_cell_alignment(raw)))
                max_lines = max(max_lines, len(lines))
            row_h = max(52, 16 + max_lines * line_h + 16)
            row_plans.append((name_lines, name_font, cell_layouts, row_h))
        block_h = header_h + sum(plan[3] for plan in row_plans)
        block_plans.append((indexes, name_w, cell_w, row_plans, block_h))

    table_top = content_top + 4
    tables_h = sum(plan[4] for plan in block_plans)
    tables_h += block_gap * max(0, len(block_plans) - 1)
    table_bottom = table_top + tables_h
    footer_h = _footer_height(visual)
    height = int(table_bottom + footer_h + 6)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    radius = style["radius"]
    head_font = _font(18, bold=True)

    block_top = table_top
    for indexes, name_w, cell_w, row_plans, block_h in block_plans:
        block_bottom = block_top + block_h
        group_columns = [columns[index] for index in indexes]
        column_head_font = _fit_font(group_columns, cell_w - 16)

        # 표 바탕과 머리글. 분할된 블록마다 첫 열을 반복한다.
        draw.rounded_rectangle(
            (table_left, block_top, table_right, block_bottom),
            radius=radius,
            fill=style["card"],
        )
        if header_tint:
            draw.rounded_rectangle(
                (table_left, block_top, table_right, block_top + header_h),
                radius=radius,
                fill=style["accent_soft"],
            )
            draw.rectangle(
                (table_left, block_top + header_h - radius, table_right, block_top + header_h),
                fill=style["accent_soft"],
            )
        draw.line(
            [(table_left, block_top + header_h), (table_right, block_top + header_h)],
            fill=style["line"],
            width=1,
        )
        draw.text(
            (table_left + name_pad, block_top + header_h / 2),
            name_label,
            font=head_font,
            fill=style["accent"] if header_tint else style["muted"],
            anchor="lm",
        )
        for local_index, column in enumerate(group_columns):
            cx = table_left + name_w + cell_w * (local_index + 0.5)
            draw.text(
                (cx, block_top + header_h / 2),
                column,
                font=column_head_font,
                fill=style["accent"] if header_tint else style["muted"],
                anchor="mm",
            )

        draw.line(
            [
                (table_left + name_w, block_top + header_h),
                (table_left + name_w, block_bottom),
            ],
            fill=style["grid"],
            width=1,
        )

        y = block_top + header_h
        for row_index, (name_lines, name_font, cell_layouts, row_h) in enumerate(
            row_plans
        ):
            # 얼룩 줄무늬는 열이 많을 때 시선이 행을 놓치지 않게 한다. 테마가 켤 때만 쓴다.
            if zebra and row_index % 2 == 1:
                draw.rectangle(
                    (table_left + 1, y, table_right - 1, y + row_h), fill=style["page"]
                )
            if row_index:
                draw.line(
                    [(table_left + 14, y), (table_right - 14, y)],
                    fill=style["grid"],
                    width=1,
                )
            name_highlighted = rows[row_index].name.strip() in highlights
            name_top = y + (row_h - len(name_lines) * line_h) / 2 + line_h / 2
            for line_index, line in enumerate(name_lines):
                draw.text(
                    (table_left + name_pad, name_top + line_h * line_index),
                    line,
                    font=name_font,
                    fill=style["accent"] if name_highlighted else style["ink"],
                    anchor="lm",
                )
            for local_index, (lines, font, alignment) in enumerate(cell_layouts):
                cell_left = table_left + name_w + cell_w * local_index
                cell_right = cell_left + cell_w
                cx = table_left + name_w + cell_w * (local_index + 0.5)
                raw_cell = rows[row_index].cells[indexes[local_index]].strip()
                cell_highlighted = raw_cell in highlights
                if cell_highlighted:
                    # 행 전체가 아니라 그 셀만 짚는다. 추천 결론과 관련된 값이 어디인지
                    # 보이는 것이 목적이지, 한 대상을 통째로 칠하는 것이 아니다.
                    draw.rounded_rectangle(
                        (cell_left + 6, y + 6, cell_right - 6, y + row_h - 6),
                        radius=max(2, radius - 4),
                        fill=style["accent_soft"],
                    )
                if alignment == "right":
                    text_x, anchor = cell_right - cell_pad, "rm"
                elif alignment == "center":
                    text_x, anchor = cx, "mm"
                else:
                    text_x, anchor = cell_left + cell_pad, "lm"
                cell_top = y + (row_h - len(lines) * line_h) / 2 + line_h / 2
                for line_index, line in enumerate(lines):
                    draw.text(
                        (text_x, cell_top + line_h * line_index),
                        line,
                        font=font,
                        fill=style["accent"] if cell_highlighted else style["ink"],
                        anchor=anchor,
                    )
            y += row_h

        draw.rounded_rectangle(
            (table_left, block_top, table_right, block_bottom),
            radius=radius,
            outline=style["line"],
            width=1,
        )
        block_top = block_bottom + block_gap

    _footer(draw, visual, style, height)
    return _encode(image)


def _render_table_feature_matrix(visual: PlannedVisual) -> str:
    return _render_table_grid(visual, header_tint=False, zebra=True, name_label="기준")


def _render_table_winner(visual: PlannedVisual) -> str:
    return _render_table_grid(visual, header_tint=True, highlight=True)


def _render_table_spec_sheet(visual: PlannedVisual) -> str:
    return _render_table_grid(visual, header_tint=False, name_label="항목")


def _render_table_stacked(visual: PlannedVisual, *, per_row: int = 1) -> str:
    """대상 하나당 카드 하나. 카드 안에서 기준은 '이름 — 값' 줄로 쌓인다.

    열이 많은 표를 가로로 우겨 넣으면 모바일에서 글자가 4px가 된다. 세로로 쌓으면 폭이
    자유로워 값을 줄이지 않아도 된다. per_row=2는 두 대상을 나란히 놓는 분할 배치다.
    """
    style = _style_for(visual)
    columns = [_space_dots(column) for column in (visual.columns or [])]
    rows = visual.rows or []
    highlights = _table_highlights(visual)
    content_top = _header_height(visual.title, None, style)

    gap = 18
    card_w = (_W - 2 * _MARGIN - gap * (per_row - 1)) / per_row
    label_font = _font(15)
    value_font = _font(17)
    name_font = _font(20, bold=True)
    label_w = min(
        card_w * 0.42,
        max((_text_width(column, label_font) for column in columns), default=80.0) + 12,
    )
    value_w = card_w - 32 - label_w - 12

    entry_h = 30
    plans = []
    for row in rows:
        lines_per_cell = [
            _all_lines(_space_dots(cell), value_font, value_w) or [""]
            for cell in row.cells[: len(columns)]
        ]
        body_h = sum(max(entry_h, len(lines) * 24 + 6) for lines in lines_per_cell)
        plans.append((row, lines_per_cell, 20 + 30 + 8 + body_h + 16))

    card_h = max((plan[2] for plan in plans), default=80)
    card_rows = (len(plans) + per_row - 1) // per_row
    footer_h = _footer_height(visual)
    height = int(
        content_top + card_rows * card_h + gap * max(0, card_rows - 1) + footer_h + 8
    )
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    for index, (row, lines_per_cell, _plan_h) in enumerate(plans):
        column_index, row_index = index % per_row, index // per_row
        x0 = _MARGIN + column_index * (card_w + gap)
        y0 = content_top + row_index * (card_h + gap)
        winner = row.name.strip() in highlights
        draw.rounded_rectangle(
            (x0, y0, x0 + card_w, y0 + card_h),
            radius=style["radius"],
            fill=style["result_soft"] if winner else style["card"],
            outline=style["accent"] if winner else style["line"],
            width=1,
        )
        draw.text(
            (x0 + 16, y0 + 20 + 15),
            _clip(row.name, name_font, card_w - 32),
            font=name_font,
            fill=style["accent"] if winner else style["ink"],
            anchor="lm",
        )
        draw.line(
            [(x0 + 16, y0 + 56), (x0 + card_w - 16, y0 + 56)], fill=style["grid"], width=1
        )
        y = y0 + 64
        for column_position, lines in enumerate(lines_per_cell):
            row_height = max(entry_h, len(lines) * 24 + 6)
            draw.text(
                (x0 + 16, y + row_height / 2),
                _clip(columns[column_position], label_font, label_w),
                font=label_font, fill=style["muted"], anchor="lm",
            )
            top = y + (row_height - len(lines) * 24) / 2 + 12
            for line_index, line in enumerate(lines):
                draw.text(
                    (x0 + card_w - 16, top + line_index * 24),
                    line, font=value_font, fill=style["ink"], anchor="rm",
                )
            y += row_height

    _footer(draw, visual, style, height)
    return _encode(image)


def _render_table_split(visual: PlannedVisual) -> str:
    rows = visual.rows or []
    return _render_table_stacked(visual, per_row=2 if len(rows) == 2 else 1)


def _render_table_pros_cons(visual: PlannedVisual) -> str:
    """기준이 장단점 두 축일 때의 카드 배치. 격자보다 두 덩어리로 읽히는 편이 정확하다."""
    style = _style_for(visual)
    columns = [_space_dots(column) for column in (visual.columns or [])]
    rows = visual.rows or []
    content_top = _header_height(visual.title, None, style)

    gap = 18
    card_w = (_W - 2 * _MARGIN - gap * (len(columns) - 1)) / len(columns)
    item_font = _font(17)
    head_font = _font(19, bold=True)
    inner_w = card_w - 56

    column_items: list[list[tuple[str, list[str]]]] = []
    for column_index in range(len(columns)):
        entries = []
        for row in rows:
            if column_index >= len(row.cells):
                continue
            entries.append(
                (row.name, _wrap(_space_dots(row.cells[column_index]), item_font, inner_w, 2))
            )
        column_items.append(entries)

    line_h, entry_gap = 24, 14
    body_h = max(
        (
            sum(22 + line_h * len(lines) for _name, lines in entries)
            + entry_gap * max(0, len(entries) - 1)
            for entries in column_items
        ),
        default=40,
    )
    header_h = 48
    card_h = header_h + 16 + body_h + 20
    footer_h = _footer_height(visual)
    height = int(content_top + card_h + footer_h + 8)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    for column_index, column in enumerate(columns):
        x0 = _MARGIN + column_index * (card_w + gap)
        y0 = float(content_top)
        negative = any(word in column for word in _CONS_WORDS)
        tone = style["negative"] if negative else style["positive"]
        draw.rounded_rectangle(
            (x0, y0, x0 + card_w, y0 + card_h), radius=style["radius"],
            fill=style["card"], outline=style["line"], width=1,
        )
        draw.rounded_rectangle(
            (x0, y0, x0 + 5, y0 + card_h), radius=2, fill=tone
        )
        draw.text(
            (x0 + 20, y0 + header_h / 2), _clip(column, head_font, card_w - 40),
            font=head_font, fill=tone, anchor="lm",
        )
        draw.line(
            [(x0 + 20, y0 + header_h), (x0 + card_w - 20, y0 + header_h)],
            fill=style["grid"], width=1,
        )
        y = y0 + header_h + 16
        for name, lines in column_items[column_index]:
            draw.text(
                (x0 + 20, y + 8), _clip(name, _font(14), inner_w),
                font=_font(14), fill=style["muted"], anchor="lt",
            )
            for line_index, line in enumerate(lines):
                draw.text(
                    (x0 + 20, y + 22 + line_h * line_index + line_h / 2), line,
                    font=item_font, fill=style["ink"], anchor="lm",
                )
            y += 22 + line_h * len(lines) + entry_gap

    _footer(draw, visual, style, height)
    return _encode(image)


_TABLE_RENDERERS = {
    "STANDARD_GRID": _render_table_grid,
    "FEATURE_MATRIX": _render_table_feature_matrix,
    "WINNER_HIGHLIGHT": _render_table_winner,
    "SPEC_SHEET": _render_table_spec_sheet,
    "COMPACT_MOBILE": _render_table_stacked,
    "TWO_PRODUCT_SPLIT": _render_table_split,
    "PROS_CONS_CARDS": _render_table_pros_cons,
}


_INFOGRAPHIC_MAX_ITEMS = 4


def _infographic_variant(visual: PlannedVisual, style: dict) -> str:
    """이 인포그래픽의 배치. 모든 인포그래픽을 '중앙 알약 + 세 카드'로 만들지 않는다.

    갈래가 둘이면 분기 그림이 아니라 대비 그림이고, 다섯 이상이면 카드가 좁아 클러스터가
    맞다. 그 사이에서만 테마·계획이 고른 변형을 존중한다.
    """
    groups = visual.groups or []
    variant = (visual.layout_variant or "").upper()
    if variant in _INFOGRAPHIC_RENDERERS:
        if len(groups) == 2 and variant in ("HUB_AND_SPOKE", "KEYWORD_CLUSTER"):
            return "TWO_COLUMN_EDITORIAL"
        if len(groups) >= 3 and variant == "BEFORE_AFTER":
            return "STACKED_SECTIONS"
        return variant
    if len(groups) >= 5:
        return "KEYWORD_CLUSTER"
    theme_variant = style.get("infographic_variant", "HUB_AND_SPOKE")
    if len(groups) == 2 and theme_variant in ("HUB_AND_SPOKE", "KEYWORD_CLUSTER"):
        return "TWO_COLUMN_EDITORIAL"
    if len(groups) >= 3 and theme_variant == "BEFORE_AFTER":
        return "STACKED_SECTIONS"
    return theme_variant if theme_variant in _INFOGRAPHIC_RENDERERS else "HUB_AND_SPOKE"


def _render_infographic(visual: PlannedVisual) -> str:
    style = _style_for(visual)
    return _INFOGRAPHIC_RENDERERS[_infographic_variant(visual, style)](visual)


def _render_infographic_hub(visual: PlannedVisual) -> str:
    """중심 주제와 2~4개 갈래. 중심을 지나치게 큰 파란 박스로 만들지 않고, 카드마다 다른
    색을 칠하지 않는다 — 하나의 강조색과 옅은 틴트만 쓴다."""
    style = _style_for(visual)
    groups = visual.groups or []
    columns = len(groups)
    col_gap = 24
    col_w = (_W - 2 * _MARGIN - (columns - 1) * col_gap) / columns

    content_top = _header_height(visual.title, None, style)
    center = _space_dots(visual.center_topic or visual.title)
    pill_font = _font(19, bold=True)
    pill_w = min(_W - 2 * _MARGIN, _text_width(center, pill_font) + 44)
    pill_h = 42
    pill_top = content_top
    cards_top = pill_top + pill_h + 40

    header_h = 44
    item_font = _font(17)
    name_font = _font(19, bold=True)
    line_h, item_gap, items_top_pad, items_bottom_pad = 26, 10, 20, 16

    # 각 그룹 카드의 항목을 미리 줄바꿈해(두 줄까지) 필요한 높이를 재고, 가장 큰 카드에
    # 맞춰 모든 카드 높이를 통일한다 — 같은 계층의 카드는 같은 크기여야 한다.
    group_plans = [
        [_wrap(_space_dots(item), item_font, col_w - 52, 2) for item in group.items[:_INFOGRAPHIC_MAX_ITEMS]]
        for group in groups
    ]

    def content_height(plan: list[list[str]]) -> int:
        rows_px = sum(len(lines) * line_h for lines in plan)
        gaps = max(0, len(plan) - 1) * item_gap
        return rows_px + gaps

    body_h = max((content_height(plan) for plan in group_plans), default=0)
    card_h = header_h + items_top_pad + body_h + items_bottom_pad

    footer_h = _footer_height(visual)
    height = int(cards_top + card_h + footer_h + 6)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    cx = _W / 2
    pill_box = (cx - pill_w / 2, pill_top, cx + pill_w / 2, pill_top + pill_h)
    # 중심 주제 — 큰 진한 박스 대신 옅은 틴트 알약. 강조색 글자로 무게를 준다.
    draw.rounded_rectangle(pill_box, radius=pill_h / 2, fill=style["accent_soft"])
    draw.text(
        (cx, pill_top + pill_h / 2), _clip(center, pill_font, pill_w - 28),
        font=pill_font, fill=style["accent"], anchor="mm",
    )

    cards = [
        (
            _MARGIN + index * (col_w + col_gap), cards_top,
            _MARGIN + index * (col_w + col_gap) + col_w, cards_top + card_h,
        )
        for index in range(columns)
    ]
    # 중심에서 각 갈래로 내려오는 연결선(실제 관계가 있을 때만 선을 쓴다). 세로 줄기 →
    # 가로 레일 → 각 카드로 짧게 내려 하나의 분기 흐름으로 읽히게 한다.
    rail_y = cards_top - 20
    draw.line([(cx, pill_box[3]), (cx, rail_y)], fill=style["faint"], width=2)
    draw.line(
        [(cards[0][0] + col_w / 2, rail_y), (cards[-1][0] + col_w / 2, rail_y)],
        fill=style["faint"], width=2,
    )

    for index, group in enumerate(groups):
        x0, y0, x1, y1 = cards[index]
        gcx = x0 + col_w / 2
        draw.line([(gcx, rail_y), (gcx, y0)], fill=style["faint"], width=2)
        # 카드는 흰 바탕 + 얇은 테두리. 머리글 띠만 옅은 틴트로(카드마다 같은 색).
        draw.rounded_rectangle((x0, y0, x1, y1), radius=12, fill=style["card"])
        draw.rounded_rectangle((x0, y0, x1, y0 + header_h), radius=12, fill=style["accent_soft"])
        draw.rectangle((x0, y0 + header_h - 12, x1, y0 + header_h), fill=style["accent_soft"])
        draw.line([(x0, y0 + header_h), (x1, y0 + header_h)], fill=style["hairline"], width=1)
        draw.text(
            (gcx, y0 + header_h / 2), _clip(group.name, name_font, col_w - 24),
            font=name_font, fill=style["accent"], anchor="mm",
        )
        item_y = y0 + header_h + items_top_pad + line_h / 2
        for lines in group_plans[index]:
            draw.ellipse((x0 + 20, item_y - 4, x0 + 27, item_y + 3), fill=style["accent"])
            for line_index, line in enumerate(lines):
                draw.text(
                    (x0 + 38, item_y + line_index * line_h), line,
                    font=item_font, fill=style["ink"], anchor="lm",
                )
            item_y += len(lines) * line_h + item_gap
        # 머리글 틴트가 위 테두리를 덮으므로 카드 테두리를 마지막에 또렷하게 다시 두른다.
        draw.rounded_rectangle((x0, y0, x1, y1), radius=12, outline=style["hairline"], width=1)

    _footer(draw, visual, style, height)
    return _encode(image)


def _group_plans(
    groups: list, item_font, width: float, max_lines: int = 2
) -> list[list[list[str]]]:
    return [
        [
            _wrap(_space_dots(item), item_font, width, max_lines)
            for item in group.items[:_INFOGRAPHIC_MAX_ITEMS]
        ]
        for group in groups
    ]


def _render_infographic_stacked(visual: PlannedVisual) -> str:
    """가로로 긴 띠를 세로로 쌓는다. 갈래가 순서를 갖거나 설명이 길 때 카드 격자보다 낫다."""
    style = _style_for(visual)
    groups = visual.groups or []
    content_top = _header_height(visual.title, None, style)

    left, right = float(_MARGIN), float(_W - _MARGIN)
    name_w = 210.0
    item_font = _font(17)
    name_font = _font(19, bold=True)
    item_w = right - (left + name_w) - 48
    plans = _group_plans(groups, item_font, item_w)

    line_h, item_gap = 26, 8
    band_heights = [
        max(76, 20 + sum(len(lines) * line_h for lines in plan) + item_gap * max(0, len(plan) - 1) + 20)
        for plan in plans
    ]
    gap = 12
    footer_h = _footer_height(visual)
    height = int(
        content_top + sum(band_heights) + gap * max(0, len(groups) - 1) + footer_h + 8
    )
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    y = float(content_top)
    for index, group in enumerate(groups):
        band_h = band_heights[index]
        draw.rounded_rectangle(
            (left, y, right, y + band_h), radius=style["radius"],
            fill=style["card"], outline=style["line"], width=1,
        )
        draw.rounded_rectangle(
            (left, y, left + name_w, y + band_h), radius=style["radius"],
            fill=style["accent_soft"],
        )
        draw.rectangle((left + name_w - style["radius"], y, left + name_w, y + band_h), fill=style["accent_soft"])
        draw.text(
            (left + 20, y + band_h / 2), _clip(group.name, name_font, name_w - 36),
            font=name_font, fill=style["accent"], anchor="lm",
        )
        item_y = y + 20 + line_h / 2
        for lines in plans[index]:
            draw.ellipse(
                (left + name_w + 20, item_y - 4, left + name_w + 27, item_y + 3),
                fill=style["accent"],
            )
            for line_index, line in enumerate(lines):
                draw.text(
                    (left + name_w + 38, item_y + line_index * line_h), line,
                    font=item_font, fill=style["ink"], anchor="lm",
                )
            item_y += len(lines) * line_h + item_gap
        y += band_h + gap

    _footer(draw, visual, style, height)
    return _encode(image)


def _render_infographic_two_column(visual: PlannedVisual) -> str:
    """두 갈래를 나란히 놓는 에디토리얼 배치. 중앙 알약도 분기선도 쓰지 않는다."""
    style = _style_for(visual)
    groups = (visual.groups or [])[:2]
    content_top = _header_height(visual.title, None, style)

    gap = 22
    col_w = (_W - 2 * _MARGIN - gap) / max(1, len(groups))
    item_font = _font(17)
    name_font = _font(21, bold=True)
    plans = _group_plans(groups, item_font, col_w - 56)

    line_h, item_gap = 26, 12
    body_h = max(
        (
            sum(len(lines) * line_h for lines in plan) + item_gap * max(0, len(plan) - 1)
            for plan in plans
        ),
        default=0,
    )
    header_h = 52
    card_h = header_h + 18 + body_h + 20
    footer_h = _footer_height(visual)
    height = int(content_top + card_h + footer_h + 8)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    for index, group in enumerate(groups):
        x0 = _MARGIN + index * (col_w + gap)
        y0 = float(content_top)
        draw.rounded_rectangle(
            (x0, y0, x0 + col_w, y0 + card_h), radius=style["radius"],
            fill=style["card"], outline=style["line"], width=1,
        )
        tone = style["accent"] if index == 0 else style["result"]
        draw.rounded_rectangle((x0, y0, x0 + col_w, y0 + 4), radius=2, fill=tone)
        draw.text(
            (x0 + 22, y0 + header_h / 2 + 4), _clip(group.name, name_font, col_w - 44),
            font=name_font, fill=tone, anchor="lm",
        )
        item_y = y0 + header_h + 18 + line_h / 2
        for lines in plans[index]:
            draw.ellipse((x0 + 22, item_y - 4, x0 + 29, item_y + 3), fill=tone)
            for line_index, line in enumerate(lines):
                draw.text(
                    (x0 + 40, item_y + line_index * line_h), line,
                    font=item_font, fill=style["ink"], anchor="lm",
                )
            item_y += len(lines) * line_h + item_gap

    _footer(draw, visual, style, height)
    return _encode(image)


def _render_infographic_before_after(visual: PlannedVisual) -> str:
    """두 갈래를 '전 → 후'로 읽히게 하는 배치. 가운데 화살표가 방향을 만든다."""
    style = _style_for(visual)
    groups = (visual.groups or [])[:2]
    content_top = _header_height(visual.title, None, style)

    arrow_w = 76
    col_w = (_W - 2 * _MARGIN - arrow_w) / max(1, len(groups))
    item_font = _font(17)
    name_font = _font(21, bold=True)
    plans = _group_plans(groups, item_font, col_w - 56)

    line_h, item_gap = 26, 12
    body_h = max(
        (
            sum(len(lines) * line_h for lines in plan) + item_gap * max(0, len(plan) - 1)
            for plan in plans
        ),
        default=0,
    )
    header_h = 50
    card_h = header_h + 18 + body_h + 20
    footer_h = _footer_height(visual)
    height = int(content_top + card_h + footer_h + 8)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    for index, group in enumerate(groups):
        x0 = _MARGIN + index * (col_w + arrow_w)
        y0 = float(content_top)
        after = index == len(groups) - 1 and len(groups) > 1
        tone = style["result"] if after else style["muted"]
        draw.rounded_rectangle(
            (x0, y0, x0 + col_w, y0 + card_h), radius=style["radius"],
            fill=style["result_soft"] if after else style["card"],
            outline=tone if after else style["line"], width=1,
        )
        draw.text(
            (x0 + 22, y0 + header_h / 2 + 2), _clip(group.name, name_font, col_w - 44),
            font=name_font, fill=style["result"] if after else style["ink"], anchor="lm",
        )
        draw.line(
            [(x0 + 22, y0 + header_h), (x0 + col_w - 22, y0 + header_h)],
            fill=style["grid"], width=1,
        )
        item_y = y0 + header_h + 18 + line_h / 2
        for lines in plans[index]:
            for line_index, line in enumerate(lines):
                draw.text(
                    (x0 + 22, item_y + line_index * line_h), line,
                    font=item_font, fill=style["ink"], anchor="lm",
                )
            item_y += len(lines) * line_h + item_gap

    if len(groups) == 2:
        ax = _MARGIN + col_w + arrow_w / 2
        ay = content_top + card_h / 2
        draw.line([(ax - 20, ay), (ax + 10, ay)], fill=style["accent"], width=4)
        draw.polygon(
            [(ax + 8, ay - 10), (ax + 8, ay + 10), (ax + 24, ay)], fill=style["accent"]
        )

    _footer(draw, visual, style, height)
    return _encode(image)


def _render_infographic_cause_effect(visual: PlannedVisual) -> str:
    """왼쪽 원인들 → 오른쪽 결과. 갈래가 셋 이상일 때도 방향이 살아 있는 배치."""
    style = _style_for(visual)
    groups = visual.groups or []
    causes, effect = groups[:-1], groups[-1]
    content_top = _header_height(visual.title, None, style)

    arrow_w = 70
    left_w = (_W - 2 * _MARGIN - arrow_w) * 0.52
    right_w = (_W - 2 * _MARGIN - arrow_w) - left_w
    item_font = _font(16)
    name_font = _font(19, bold=True)

    cause_plans = _group_plans(causes, item_font, left_w - 52)
    effect_plan = _group_plans([effect], item_font, right_w - 52)[0]

    line_h, gap = 24, 12
    cause_heights = [
        max(64, 16 + 26 + sum(len(lines) * line_h for lines in plan) + 16)
        for plan in cause_plans
    ]
    left_h = sum(cause_heights) + gap * max(0, len(causes) - 1)
    right_h = max(96, 16 + 30 + sum(len(lines) * line_h for lines in effect_plan) + 18)
    block_h = max(left_h, right_h)
    footer_h = _footer_height(visual)
    height = int(content_top + block_h + footer_h + 8)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    y = float(content_top)
    for index, group in enumerate(causes):
        card_h = cause_heights[index]
        draw.rounded_rectangle(
            (_MARGIN, y, _MARGIN + left_w, y + card_h), radius=style["radius"],
            fill=style["card"], outline=style["line"], width=1,
        )
        draw.text(
            (_MARGIN + 20, y + 16 + 13), _clip(group.name, name_font, left_w - 40),
            font=name_font, fill=style["accent"], anchor="lm",
        )
        item_y = y + 16 + 30 + line_h / 2
        for lines in cause_plans[index]:
            for line_index, line in enumerate(lines):
                draw.text(
                    (_MARGIN + 20, item_y + line_index * line_h), line,
                    font=item_font, fill=style["ink"], anchor="lm",
                )
            item_y += len(lines) * line_h
        y += card_h + gap

    right_x = _MARGIN + left_w + arrow_w
    right_top = content_top + (block_h - right_h) / 2
    draw.rounded_rectangle(
        (right_x, right_top, right_x + right_w, right_top + right_h),
        radius=style["radius"], fill=style["result_soft"], outline=style["result"], width=1,
    )
    draw.text(
        (right_x + 20, right_top + 16 + 15), _clip(effect.name, name_font, right_w - 40),
        font=name_font, fill=style["result"], anchor="lm",
    )
    item_y = right_top + 16 + 34 + line_h / 2
    for lines in effect_plan:
        for line_index, line in enumerate(lines):
            draw.text(
                (right_x + 20, item_y + line_index * line_h), line,
                font=item_font, fill=style["ink"], anchor="lm",
            )
        item_y += len(lines) * line_h

    ax = _MARGIN + left_w + arrow_w / 2
    ay = content_top + block_h / 2
    draw.line([(ax - 18, ay), (ax + 8, ay)], fill=style["accent"], width=4)
    draw.polygon([(ax + 6, ay - 9), (ax + 6, ay + 9), (ax + 22, ay)], fill=style["accent"])

    _footer(draw, visual, style, height)
    return _encode(image)


def _render_infographic_cluster(visual: PlannedVisual) -> str:
    """갈래가 많을 때의 칩 묶음. 카드 격자로는 한 칸에 한 글자가 되는 구간을 맡는다."""
    style = _style_for(visual)
    groups = visual.groups or []
    content_top = _header_height(visual.title, None, style)

    left, right = float(_MARGIN), float(_W - _MARGIN)
    name_font = _font(18, bold=True)
    chip_font = _font(16)
    chip_h, chip_gap, row_gap = 34, 10, 12

    blocks: list[tuple[str, list[list[tuple[str, float]]]]] = []
    for group in groups:
        rows: list[list[tuple[str, float]]] = [[]]
        used = 0.0
        for item in group.items[:_INFOGRAPHIC_MAX_ITEMS]:
            text = _clip(_space_dots(item), chip_font, right - left - 40)
            width = _text_width(text, chip_font) + 28
            if used + width > (right - left) and rows[-1]:
                rows.append([])
                used = 0.0
            rows[-1].append((text, width))
            used += width + chip_gap
        blocks.append((group.name, rows))

    block_heights = [
        26 + len(rows) * chip_h + row_gap * max(0, len(rows) - 1) + 18 for _name, rows in blocks
    ]
    footer_h = _footer_height(visual)
    height = int(content_top + sum(block_heights) + footer_h + 8)
    image, draw = _new(height, style)
    _header(image, draw, visual.title, style, None)

    y = float(content_top)
    for index, (name, rows) in enumerate(blocks):
        draw.text(
            (left, y + 12), _clip(name, name_font, right - left),
            font=name_font, fill=style["accent"], anchor="lt",
        )
        chip_y = y + 26
        for row in rows:
            x = left
            for text, width in row:
                draw.rounded_rectangle(
                    (x, chip_y, x + width, chip_y + chip_h),
                    radius=chip_h / 2, fill=style["accent_soft"],
                )
                draw.text(
                    (x + width / 2, chip_y + chip_h / 2), text,
                    font=chip_font, fill=style["ink"], anchor="mm",
                )
                x += width + chip_gap
            chip_y += chip_h + row_gap
        y += block_heights[index]

    _footer(draw, visual, style, height)
    return _encode(image)


_INFOGRAPHIC_RENDERERS = {
    "HUB_AND_SPOKE": _render_infographic_hub,
    "STACKED_SECTIONS": _render_infographic_stacked,
    "TWO_COLUMN_EDITORIAL": _render_infographic_two_column,
    "KEYWORD_CLUSTER": _render_infographic_cluster,
    "BEFORE_AFTER": _render_infographic_before_after,
    "CAUSE_AND_EFFECT": _render_infographic_cause_effect,
}


# 그래프는 수치·출처가 필수(스펙: 실제 데이터가 없으면 그래프를 만들지 않는다).
# 과정도·인포그래픽은 구조 자료라 수치 대신 내용물(steps/groups)만 있으면 된다.


def _rejection_reason(visual: PlannedVisual) -> str | None:
    if visual.type in _CHART_TYPES:
        if not visual.data or len(visual.data) < 2:
            return "실측 수치가 2개 미만"
        if not visual.source:
            return "출처 없음"
        if visual.type == "PIE_CHART":
            positive = [point for point in visual.data if point.value > 0]
            if not positive:
                return "구성비가 전부 0"
            # 조각이 6개 이상이면 읽기 어렵다 — 만들지 않는다(스펙: 5개 초과 금지).
            if len(positive) > 5:
                return "구성 항목이 5개 초과"
        if visual.type == "LINE_CHART" and len(visual.data) < 3:
            return "시계열 수치가 3개 미만"
        return None
    if visual.type == "PROCESS_DIAGRAM":
        return None if visual.steps and 3 <= len(visual.steps) <= 6 else "단계가 3~6개가 아님"
    if visual.type == "INFOGRAPHIC":
        return None if visual.groups and len(visual.groups) >= 2 else "그룹이 2개 미만"
    if visual.type == "TABLE":
        if not visual.columns or len(visual.columns) < 2:
            return "비교 기준이 2개 미만"
        if len(visual.columns) > 4 or any(
            not column.strip() or len(column.strip()) > 8
            for column in visual.columns
        ):
            return "비교 기준이 비었거나 2~4개·8자 이내가 아님"
        if not visual.rows or len(visual.rows) < 2:
            return "비교 대상이 2개 미만"
        if len(visual.rows) > 5:
            return "비교 대상이 5개 초과"
        if any(
            not row.name.strip() or len(row.name.strip()) > 20
            for row in visual.rows
        ):
            return "비교 대상 이름이 비었거나 20자 초과"
        if any(len(row.cells) != len(visual.columns) for row in visual.rows):
            return "행의 셀 개수가 비교 기준과 다름"
        if any(
            not cell.strip() or len(cell) > 20
            for row in visual.rows
            for cell in row.cells
        ):
            return "빈 셀 또는 20자 초과 셀"
        return None
    return f"지원하지 않는 유형 {visual.type}"


_RENDERERS = {
    "BAR_CHART": _render_bar_chart,
    "LINE_CHART": _render_line_chart,
    "PIE_CHART": _render_pie_chart,
    "PROCESS_DIAGRAM": _render_process_diagram,
    "INFOGRAPHIC": _render_infographic,
    "TABLE": _render_comparison_table,
}


def visual_caption(visual: PlannedVisual) -> str:
    """자료 아래 붙는 캡션. 외부 출처가 있을 때만 출처·기준시점을 밝힌다.

    자체적으로 정리한 자료(출처 없음)에는 서비스명·제작 도구를 붙이지 않는다 — 캡션 자체를
    비운다(스펙: 하단 문구를 표시하지 않는다). caption 필드가 채워져 있으면 그것을 쓴다.
    """
    if visual.caption:
        return visual.caption
    if not visual.source:
        return ""
    parts = [visual.title, visual.source]
    if visual.published_at:
        parts.append(visual.published_at)
    return " · ".join(parts)


def renderable_visuals(visuals: list[PlannedVisual] | None) -> list[PlannedVisual]:
    """검증(수치·출처)을 통과해 실제로 그려질 시각자료만. 카드 계획이 전체 이미지 예산을
    셀 때 '이미 확정된 표·그래프 수'로 쓴다 — 검증 탈락분까지 세면 그만큼 사진 카드
    자리를 부당하게 뺏는다."""
    return [visual for visual in (visuals or []) if _rejection_reason(visual) is None]


def render_planned_visual(visual: PlannedVisual) -> GeneratedPostImage | None:
    """검증을 통과한 자료만 PNG로 그린다. 실패는 None — 마커는 호출부가 걷어낸다."""
    reason = _rejection_reason(visual)
    if reason is not None:
        logger.info("시각자료 %s(%s) 제외: %s", visual.visual_id, visual.type, reason)
        return None
    try:
        data_url = _RENDERERS[visual.type](visual)
    except FontUnavailable as error:
        logger.error("시각자료 %s 렌더링 불가(한글 폰트 없음): %s", visual.visual_id, error)
        return None
    except Exception as error:
        logger.warning("시각자료 %s 렌더링 실패: %s", visual.visual_id, error)
        return None

    return GeneratedPostImage(
        data_url=data_url,
        alt_text=visual.alt_text or visual.title,
        prompt=f"code-rendered {visual.type}: {visual.title}",
        provider="blogit-renderer",
        model=visual.type.lower(),
        generated_at=_now(),
        mime_type="image/png",
        source="rendered",
        media_kind="visual",
        caption=visual_caption(visual),
    )


def replace_visual_markers(content: str, markup_by_id: dict[str, str]) -> str:
    """본문의 [[VISUAL: id]] 마커를 렌더링 결과로 치환한다. 렌더링되지 않은(검증 탈락)
    마커는 걷어낸다 — 발행 글에 마커가 글자 그대로 찍히면 안 된다."""

    def substitute(match: re.Match) -> str:
        return markup_by_id.get(match.group(1).strip(), "")

    replaced = VISUAL_TAG_PATTERN.sub(substitute, content)
    return re.sub(r"\n{3,}", "\n\n", replaced).strip()


def visual_html(image: GeneratedPostImage) -> str:
    """도표는 사진과 다른 class로 나간다 — 데이터 그림은 테두리가 이미 그림 안에 있어
    바깥 테두리를 한 겹 더 두르면 두 줄이 겹쳐 보인다."""
    from .images import image_html

    return image_html(image)


def visual_markdown(image: GeneratedPostImage) -> str:
    caption = f"\n*{image.caption}*" if image.caption else ""
    return f"![{image.alt_text}]({image.data_url}){caption}"
