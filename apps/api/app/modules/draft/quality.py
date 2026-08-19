"""생성된 원고가 요구사항을 지켰는지 검사한다.

프롬프트는 "본문 1800자 이상", "이미지 태그 정확히 2개", "해시태그 정확히 N개"를 시키지만
모델은 그것을 어길 수 있고, 지금까지는 아무도 확인하지 않았다. 900자짜리 원고가 그대로
사용자 화면에 올라갔다는 뜻이다.

두 가지를 구분한다.

- **치명적**: 원고를 못 쓰게 만드는 것. 본문이 없거나 너무 짧거나, 낚시성 표현이 박혀
  있는 것. 여기 걸리면 한 번 더 생성하고(draft.service), 두 번 다 걸리면 실패라고 말한다.
- **경고**: 코드가 이미 감당하는 것. 이미지 태그를 빠뜨려도 _with_inserted_images가
  자리를 잡아 주므로, 이걸로 멀쩡한 원고를 버리면 그게 더 나쁘다. 로그만 남긴다.
"""

import re
from dataclasses import dataclass, field

from app.llm.imaging import BODY_IMAGE_COUNT
from app.llm.prompts import ASSISTANT_TONE_PHRASES, CLICHE_PHRASES, HYPE_PHRASES
from app.shared import FinalPost

# 설정이 없거나 테스트에서 별도 문턱을 넘기지 않았을 때 쓰는 기본 하한.
MIN_BODY_CHARS = 1_200

# 상투구(요즘 많은 분들이·오늘은 알아보겠습니다 등)가 이보다 많이 나오면 경고한다. 한두 번은
# 자연스러운 연결이라 오탐이 되므로, '과다 사용'만 잡는다. 낚시(BANNED_PHRASES)와 달리
# 원고를 반려하지는 않는다.
MAX_CLICHE_HITS = 2

# 아래 셋은 프롬프트가 시키는 것(소제목 섹션, 짧은 문단, 반복 금지)을 모델이 지켰는지 본다.
# 소제목 부족·긴 문단은 경고에 그친다 — 모델의 HTML이 소제목을 <strong>으로 낼 수도 있어
# 멀쩡한 원고를 놓치기 쉽다. 같은 문장이 여러 번 반복되는 것만은 명백한 결함이라 치명이다.
MIN_HEADINGS = 2
# 2026-08-03 실측: 문단 평균 113자·최대 211자로 화면이 빽빽했다. 프롬프트는 1~2문장·
# 120자를 요구하므로, 그 배를 넘는 문단만 경고한다(경고이지 반려가 아니다 — 뜻이 통하려면
# 길어야 하는 대목이 있다).
MAX_PARAGRAPH_CHARS = 240
# 이보다 많은 문장이 통째로 중복되면 다시 생성한다. 1~2개는 마무리 문장이 겹친 정도라 경고.
MAX_DUPLICATE_SENTENCES = 2
# 짧은 상투구("그렇다.")까지 중복으로 세면 오탐이 많다. 문장다운 길이만 본다.
MIN_DEDUP_SENTENCE_CHARS = 16
# 같은 문장은 아니지만 같은 세 단어 묶음이 계속 반복되는 원고를 잡는다.
NGRAM_SIZE = 3
MIN_NGRAMS_FOR_RATE = 24
WARN_DUPLICATE_NGRAM_RATE = 0.10
MAX_DUPLICATE_NGRAM_RATE = 0.18

IMAGE_TAG = re.compile(r"\[\[IMAGE:", re.IGNORECASE)
IMAGE_TAG_BLOCK = re.compile(r"\[\[IMAGE:[^\]]+\]\]", re.IGNORECASE)
CONTENT_MARKER_BLOCK = re.compile(r"\[\[(?:IMAGE|VISUAL|STICKER):[^\]]+\]\]", re.IGNORECASE)
HEADING_TAG = re.compile(r"<h2[\s>]", re.IGNORECASE)
SENTENCE_SPLIT = re.compile(r"[.!?。\n]+")
TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
EMPHASIS_TAG = re.compile(r"<(strong|mark)[\s>]", re.IGNORECASE)

# M2의 trend_title은 키워드가 아니라 완성된 제목이다. 본문에 제목 전체를 한 문장으로 다시
# 쓰게 하면 자연스러운 원고도 매번 누락으로 오인한다. 아래 말들은 제목의 편집 문구라 소재를
# 식별하지 못하므로 제외하고, 남은 고유 소재어가 본문에 등장하는지를 본다.
TREND_GENERIC_TOKENS = frozenset(
    {
        "ai",
        "트렌드",
        "핵심",
        "지금",
        "최신",
        "화제",
        "주목",
        "주목받는",
        "배경",
        "변화",
        "이유",
        "정리",
        "총정리",
        "소개",
        "분석",
        "가이드",
        "방법",
        "관련",
    }
)
TREND_GENERIC_PREFIXES = (
    "트렌드",
    "핵심",
    "최신",
    "화제",
    "주목",
    "배경",
    "변화",
    "이유",
    "정리",
    "소개",
    "분석",
    "관련",
)
KOREAN_PARTICLES = (
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "처럼",
    "보다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "와",
    "과",
    "의",
    "도",
    "만",
)

# 강조(<strong>/<mark>)는 전체 문장의 15%를 넘으면 강조가 아니라 소음이다(스펙 §7).
# 2026-08-03: 프롬프트가 '소제목 구간마다 한 곳'을 요구하게 되면서, 짧은 글(문장 20개
# 안팎에 소제목 4개)은 하한만 지켜도 20%가 된다. 상한을 그대로 두면 규칙끼리 부딪혀
# 지키는 쪽이 경고를 받는다 — 상한을 하한과 맞물리게 올린다.
MAX_EMPHASIS_RATE = 0.20
MIN_EMPHASIS_FOR_RATE = 4

# 낚시성·과장 표현. 프롬프트가 쓰지 말라고 한 것이고, 네이버에서 저품질 문서로 분류되기
# 쉬운 표현이기도 하다.
BANNED_PHRASES = (
    "충격적",
    "경악",
    "소름 돋",
    "역대급",
    "클릭 필수",
    "무조건 사세요",
    "100% 보장",
    "절대 후회 안",
)


# 제목 규격. 생성 프롬프트가 요구하는 상한과 같다(트렌드 제목 채점의 HARD_MAX_LEN).
MAX_TITLE_CHARS = 60

# 글의 골격을 드러내는 정형구. 상투구(CLICHE_PHRASES)가 '표현이 닳았다'면 이쪽은 '구성이
# 템플릿이다'를 가리킨다 — 소재만 달라지고 글이 같아 보이는 원인이다.
#
# 하나씩은 자연스러울 수 있어 반려하지 않는다. content_validation.template_repetition이
# 신호로 모으고, 임계값은 실제 결과를 보고 조정한다(§11: 처음부터 FAIL로 만들지 않는다).
TEMPLATE_PHRASES = (
    "이 글에서는",
    "이 글에서 다룰",
    "읽고 나면",
    "한 가지 질문이 떠오른다",
    "크게 두 가지로 나뉜다",
    "크게 세 가지로 나뉜다",
    "앞으로 세 가지를 지켜봐",
    "지켜볼 포인트",
    "정리해보면",
    "살펴보겠습니다",
    "먼저 결론부터",
)

# 정형구가 이보다 많이 나오면 경고한다. 도입·결론에 하나씩은 흔한 일이다.
MAX_TEMPLATE_HITS = 2


def normalize_for_match(text: str) -> str:
    """한국어 제목 안에서 키워드를 찾기 위한 정규화.

    'AI 블로그 자동화'와 'AI블로그 자동화'는 검색자에게 같은 말이다. 띄어쓰기·문장부호를
    걷어내고 소문자로 맞춰 비교해야, 조사·띄어쓰기 차이 때문에 멀쩡한 제목을 반려하는
    오탐이 생기지 않는다.
    """
    return re.sub(r"[^0-9a-z가-힣]", "", (text or "").lower())


def _without_korean_particle(token: str) -> str:
    if not re.fullmatch(r"[가-힣]+", token):
        return token
    for suffix in KOREAN_PARTICLES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def _trend_anchor_tokens(trend_title: str) -> list[str]:
    """완성형 제목에서 본문의 트렌드 반영 여부를 확인할 소재어만 뽑는다."""
    tokens: list[str] = []
    for raw in TOKEN.findall((trend_title or "").lower()):
        token = _without_korean_particle(raw)
        if (
            len(token) < 2
            or raw in TREND_GENERIC_TOKENS
            or token in TREND_GENERIC_TOKENS
            or any(raw.startswith(prefix) for prefix in TREND_GENERIC_PREFIXES)
        ):
            continue
        tokens.append(token)

    # "AI 트렌드"처럼 편집어만 남는 제목도 검사를 통째로 건너뛰지는 않는다. 이때는 가장
    # 구체적인(긴) 원래 토큰 하나를 앵커로 쓴다.
    if not tokens:
        fallback = [
            _without_korean_particle(token)
            for token in TOKEN.findall((trend_title or "").lower())
            if len(_without_korean_particle(token)) >= 2
        ]
        if fallback:
            tokens = [max(fallback, key=len)]

    return list(dict.fromkeys(tokens))


def body_char_count(body: str) -> int:
    """독자에게 보이는 본문 글자 수. 내부 마커·표 구분선·레이아웃 개행은 세지 않는다."""
    plain = CONTENT_MARKER_BLOCK.sub("", body or "").replace("|", "")
    return len(re.sub(r"\s+", " ", plain).strip())


def _trend_is_reflected(body: str, trend_title: str, trend_keyword: str | None = None) -> bool:
    body_normalized = normalize_for_match(body)
    title_normalized = normalize_for_match(trend_title)
    if title_normalized and title_normalized in body_normalized:
        return True
    anchor_source = (trend_keyword or "").strip() or trend_title
    return any(
        normalize_for_match(token) in body_normalized
        for token in _trend_anchor_tokens(anchor_source)
    )


def check_title_plan(plan, fixed_title: str | None = None) -> list[str]:
    """확정 제목이 규격을 지켰는지 본다. 반환값은 반려 사유 목록(비어 있으면 통과).

    이 검사는 **원고를 쓰기 전에** 돈다. 여기서 걸리면 제목 생성만 다시 하면 되고, 본문은
    아직 쓰지도 않았으므로 버릴 것이 없다. 원고를 다 쓴 뒤에 제목을 반려하면 멀쩡한 본문
    수천 자를 함께 버리게 된다 — 그래서 제목 검사를 앞으로 당겼다.
    """
    problems: list[str] = []

    title = (plan.primary_title or "").strip()
    if not title:
        return ["확정 제목이 비어 있습니다"]

    if len(title) > MAX_TITLE_CHARS:
        problems.append(
            f"제목이 {len(title)}자로 상한 {MAX_TITLE_CHARS}자를 넘습니다: {title}"
        )

    if plan.h1.strip() != title:
        problems.append("H1이 확정 제목과 다릅니다")

    banned = [phrase for phrase in BANNED_PHRASES if phrase in title]
    if banned:
        problems.append(f"제목에 낚시성 표현: {', '.join(banned)}")

    # 사용자가 M2에서 고른 제목은 이미 확정된 값이다. 모델이 그것을 다듬었다면 규격 위반이다.
    if fixed_title and fixed_title.strip() and title != fixed_title.strip():
        problems.append(f"사용자가 고른 제목이 변형됐습니다: '{fixed_title.strip()}' → '{title}'")

    keyword = (plan.primary_keyword or "").strip()
    if not keyword:
        problems.append("핵심 검색 구문이 비어 있습니다")
    elif normalize_for_match(keyword) not in normalize_for_match(title):
        problems.append(
            f"핵심 검색 구문 '{keyword}'가 제목 '{title}'에 들어 있지 않습니다. "
            "제목에 실제로 쓰인 구문을 primaryKeyword로 고르거나, 그 구문이 들어가도록 "
            "제목을 자연스럽게 다시 쓰세요."
        )

    return problems


@dataclass
class QualityReport:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def __str__(self) -> str:
        return ", ".join(self.problems or self.warnings)


def _markdown_h1(markdown: str) -> str | None:
    """마크다운 원고의 첫 H1. H1이 없으면 None(검사 대상이 아니다)."""
    match = re.search(r"^#\s+(.+)$", markdown.strip(), flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _paragraphs(body: str) -> list[str]:
    """본문을 문단 단위로 나눈다. 빈 줄이 있으면 그것을, 없으면 줄바꿈을 경계로 본다.
    이미지 태그만 있는 줄은 문단이 아니다."""
    text = body.strip()
    blocks = re.split(r"\n\s*\n", text) if "\n\n" in text else text.split("\n")
    return [
        block.strip()
        for block in blocks
        if block.strip() and not block.strip().upper().startswith("[[IMAGE")
    ]


def _repeated_sentence_count(body: str) -> int:
    """통째로 두 번 이상 나온 문장의 '초과 등장' 수. 3번 나오면 2로 센다."""
    counts: dict[str, int] = {}
    for raw in SENTENCE_SPLIT.split(body):
        sentence = re.sub(r"\s+", " ", raw).strip()
        if len(sentence) < MIN_DEDUP_SENTENCE_CHARS or sentence.upper().startswith("[[IMAGE"):
            continue
        counts[sentence] = counts.get(sentence, 0) + 1
    return sum(count - 1 for count in counts.values() if count > 1)


def _repeated_ngram_rate(body: str) -> tuple[float, int]:
    """3-token phrase repetition rate, returned as (rate, duplicate_count)."""
    plain = IMAGE_TAG_BLOCK.sub(" ", body).lower()
    tokens = TOKEN.findall(plain)
    if len(tokens) < NGRAM_SIZE:
        return 0.0, 0

    counts: dict[tuple[str, ...], int] = {}
    total = len(tokens) - NGRAM_SIZE + 1
    if total < MIN_NGRAMS_FOR_RATE:
        return 0.0, 0

    for index in range(total):
        gram = tuple(tokens[index : index + NGRAM_SIZE])
        counts[gram] = counts.get(gram, 0) + 1

    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    return duplicate_count / total, duplicate_count


def _cliche_hits(body: str) -> list[str]:
    """본문에 나타난 상투구를 (중복 제외) 모은다. 도입부 판정을 위해 위치는 보지 않는다."""
    return [phrase for phrase in CLICHE_PHRASES if phrase in body]


def _sections(body: str) -> list[tuple[str, int]]:
    """(소제목, 그 섹션의 글자 수). 소제목이 없으면 빈 목록."""
    sections: list[tuple[str, list[str]]] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        match = re.match(r"^(#{2,3})\s+(.*)$", stripped)
        if match:
            sections.append((match.group(2).strip(), []))
        elif sections:
            sections[-1][1].append(stripped)
    return [(heading, body_char_count("\n".join(lines))) for heading, lines in sections]


def _length_revision_hint(body: str, *, expand: bool) -> str:
    """어느 섹션을 늘리거나 줄일지 지목한다.

    "늘리세요"·"줄이세요"만 보내면 모델은 전체를 다시 쓰거나 아무 데나 문장을 덧붙인다.
    가장 긴 섹션과 가장 짧은 섹션을 이름으로 지목하면 고칠 자리가 정해진다. 소제목이 없는
    원고(구조 자체가 실패한 경우)에는 지목할 것이 없으므로 일반 지시만 남긴다.
    """
    sections = _sections(body)
    if expand:
        base = (
            "기존 사실과 구성은 그대로 두고, 새 주제를 추가하지 말고 이미 있는 섹션의 설명·"
            "판단 기준·조건을 더 구체적으로 쓴다. 근거 없는 경험이나 수치를 추가하지 않는다."
        )
        if sections:
            shortest = min(sections, key=lambda item: item[1])
            base += f" 가장 짧은 섹션은 '{shortest[0]}'({shortest[1]}자)다."
        return base
    base = (
        "핵심 정보·비교 결과·출처가 있는 수치·판단 기준은 유지하고, 이미 앞에서 설명한 내용의"
        " 중복부터 덜어 낸다. 새로운 사실이나 사례를 추가하지 않는다."
    )
    if sections:
        longest = max(sections, key=lambda item: item[1])
        base += f" 가장 긴 섹션은 '{longest[0]}'({longest[1]}자)다."
    return base


def check_draft(
    post: FinalPost,
    hashtag_count: int,
    min_body_chars: int = MIN_BODY_CHARS,
    max_body_chars: int | None = None,
    trend_title: str | None = None,
    trend_keyword: str | None = None,
    has_experience_material: bool = False,
    photo_count: int = BODY_IMAGE_COUNT,
    final_title: str | None = None,
) -> QualityReport:
    """photo_count는 이 글에 실제로 요청한 본문 사진 수다(콘텐츠 설계가 정한다).

    예전에는 고정값 2와 비교해서, 설계가 사진 0개를 계획해 프롬프트가 '태그를 넣지 마라'고
    시킨 글에도 '2개여야 하는데 0개'라는 경고가 남았다 — 지시와 검수가 서로 다른 기준을
    보고 있었다. 설계가 없는 글의 기본값만 예전 규격(2)이다.
    """
    report = QualityReport()

    if not post.title.strip():
        report.problems.append("제목이 없습니다")

    # 제목이 원고보다 먼저 확정된 글의 안전망. 파싱 단계가 확정 제목을 강제하고
    # (parsing.final_post_from_json의 forced_title) 마크다운·HTML의 H1도 그 제목으로 다시
    # 세우므로 여기 걸릴 일은 없다. 그래도 검사를 두는 이유는, 나중에 누군가 그 강제를
    # 우회하는 경로를 만들었을 때 조용히 통과하지 않게 하기 위해서다. 경고에 그친다 —
    # 확정 제목은 이미 아는 값이라 되물어 볼 것이 없고, 이것 때문에 본문을 다시 쓰는 것은
    # 멀쩡한 원고를 버리는 일이다.
    if final_title and final_title.strip():
        expected = final_title.strip()
        if post.title.strip() != expected:
            report.warnings.append(f"제목이 확정 제목과 다릅니다: '{post.title.strip()}'")
        h1 = _markdown_h1(post.markdown_content or "")
        if h1 is not None and h1 != expected:
            report.warnings.append(f"본문 H1이 확정 제목과 다릅니다: '{h1}'")

    body = (post.body or "").strip()
    measured_body_chars = body_char_count(body)
    if not body:
        report.problems.append("본문이 없습니다")
    elif measured_body_chars < min_body_chars:
        report.problems.append(
            f"본문이 {measured_body_chars}자로 최소 {min_body_chars}자보다 짧습니다"
            f"(목표 {min_body_chars}~{max_body_chars or min_body_chars}자,"
            f" {min_body_chars - measured_body_chars}자 이상 보강 필요). "
            + _length_revision_hint(body, expand=True)
        )
    elif max_body_chars and measured_body_chars > max_body_chars:
        # 상한 초과는 원고를 못 쓰게 하지 않는다(경고). 프롬프트가 상한을 이미 지시하고,
        # 긴 글을 반려해 버리면 멀쩡한 내용을 통째로 버리게 된다.
        report.warnings.append(
            f"본문이 {measured_body_chars}자로 권장 최대 {max_body_chars}자를 넘습니다"
            f"(목표 {min_body_chars}~{max_body_chars}자,"
            f" {measured_body_chars - max_body_chars}자 이상 줄여야 함). "
            + _length_revision_hint(body, expand=False)
        )

    if not (post.html_content or "").strip():
        report.problems.append("HTML 원고가 없습니다")

    found = [phrase for phrase in BANNED_PHRASES if phrase in f"{post.title} {body}"]
    if found:
        report.problems.append(f"낚시성 표현: {', '.join(found)}")

    # trend_title은 완성형 제목이라 본문에 그대로 반복되는 것이 오히려 부자연스럽다. 제목의
    # 편집 문구를 제외한 핵심 소재어가 본문에 있는지를 보고, 완전히 빠졌을 때만 경고한다.
    if trend_title and trend_title.strip() and not _trend_is_reflected(
        body, trend_title, trend_keyword
    ):
        report.warnings.append(f"선택한 트렌드 '{trend_title.strip()}'가 본문에 드러나지 않습니다")

    cliches = _cliche_hits(body)
    if len(cliches) > MAX_CLICHE_HITS:
        report.warnings.append(f"상투적 표현 과다: {', '.join(cliches)}")

    # 구성이 템플릿으로 굳었는지. 표현이 아니라 골격의 문제라 따로 센다.
    templates = [phrase for phrase in TEMPLATE_PHRASES if phrase in body]
    if len(templates) > MAX_TEMPLATE_HITS:
        report.warnings.append(f"정형화된 구성 문구 과다: {', '.join(templates)}")

    # 근거 없는 과장 표현(완벽한·무조건·최고의…). 근거 유무를 코드가 판단할 수 없어 경고에
    # 그친다 — 프롬프트가 절제를 지시하고, 재생성 사유 주입이 다음 시도에서 고치게 한다.
    hype = [phrase for phrase in HYPE_PHRASES if phrase in body]
    if hype:
        report.warnings.append(f"과장 표현: {', '.join(hype)} (객관적 근거 없이 사용 금지)")

    # 2026-08-03 사용자 결정으로 '경험 자료가 없는데 1인칭 체험 문구를 쓰면 반려'하던
    # 검사를 없앴다. AI 자동 생성 글이 직접 겪은 것처럼 읽히는 것이 목적이고, 감상 서술도
    # 필요하다는 판단이다. has_experience_material 인자는 호출부 계약을 위해 남겨 뒀다.

    # 강조 남발 검사: <strong>/<mark>가 전체 문장 대비 15%를 넘으면 경고(스펙 §7).
    emphasis_count = len(EMPHASIS_TAG.findall(post.html_content or ""))
    sentence_count = len(
        [s for s in SENTENCE_SPLIT.split(body) if len(s.strip()) >= MIN_DEDUP_SENTENCE_CHARS]
    )
    if (
        emphasis_count >= MIN_EMPHASIS_FOR_RATE
        and sentence_count
        and emphasis_count / sentence_count > MAX_EMPHASIS_RATE
    ):
        report.warnings.append(
            f"강조 과다: 문장 {sentence_count}개에 강조 {emphasis_count}개 "
            f"(권장 {MAX_EMPHASIS_RATE:.0%} 이하)"
        )

    # 같은 문장이 여러 번 반복되는 것은 코드가 손볼 수 없는 명백한 결함이다. 다시 생성한다.
    duplicates = _repeated_sentence_count(body)
    if duplicates > MAX_DUPLICATE_SENTENCES:
        report.problems.append(f"같은 문장이 반복됩니다 (중복 {duplicates}개)")
    elif duplicates:
        report.warnings.append(f"반복되는 문장 {duplicates}개")

    ngram_rate, ngram_duplicates = _repeated_ngram_rate(body)
    if ngram_rate > MAX_DUPLICATE_NGRAM_RATE:
        report.problems.append(f"같은 표현이 반복됩니다 (3-gram 반복률 {ngram_rate:.0%})")
    elif ngram_rate > WARN_DUPLICATE_NGRAM_RATE:
        report.warnings.append(
            f"반복되는 표현 {ngram_duplicates}개 (3-gram 반복률 {ngram_rate:.0%})"
        )

    # 아래 것들은 알려는 주되, 원고를 버리지는 않는다.
    tags = len(IMAGE_TAG.findall(post.html_content or ""))
    if tags != photo_count:
        report.warnings.append(f"본문 이미지 태그 {tags}개 (요청: {photo_count}개)")

    if len(post.hashtags) != hashtag_count:
        report.warnings.append(f"해시태그 {len(post.hashtags)}개 (요청: {hashtag_count}개)")

    # 소제목이 <strong> 등으로 나올 수 있어 경고에 그친다. 강제는 프롬프트가 한다.
    headings = 0
    if body:
        headings = len(HEADING_TAG.findall(post.html_content or ""))
        if headings == 0:
            report.warnings.append("본문에 소제목(h2)이 없습니다")
        elif headings < MIN_HEADINGS:
            report.warnings.append(f"소제목이 {headings}개입니다 (권장 {MIN_HEADINGS}개 이상)")

    # 강조 **부족** 검사(2026-08-03). 지금까지 검사는 '과다'만 봤고, 실측에서는 5섹션짜리
    # 글에 굵게가 1곳뿐이라 화면에 눈이 쉴 곳이 없었다 — 부족한 쪽도 가독성 결함이다.
    # 프롬프트는 소제목 구간마다 한 곳을 요구하므로, 그 절반에 못 미치면 알린다.
    if headings >= MIN_HEADINGS and emphasis_count * 2 < headings:
        report.warnings.append(
            f"강조 부족: 소제목 {headings}개에 강조 {emphasis_count}개 "
            "(권장 소제목 구간마다 1곳)"
        )

    long_paragraphs = [len(p) for p in _paragraphs(body) if len(p) > MAX_PARAGRAPH_CHARS]
    if long_paragraphs:
        report.warnings.append(
            f"긴 문단 {len(long_paragraphs)}개 (최장 {max(long_paragraphs)}자, 권장 {MAX_PARAGRAPH_CHARS}자 이하)"
        )

    _check_mechanical_patterns(report, post, body)
    return report


# 목차처럼 반복되면 글이 기계적으로 읽히는 연결어. 프롬프트가 금지하지만 검사가 없었다.
CONNECTIVE_PHRASES: tuple[str, ...] = (
    "먼저",
    "다음으로",
    "또한",
    "마지막으로",
    "결론적으로",
    "정리하자면",
    "그렇다면",
    "결국",
)
# 한 연결어를 이보다 많이 쓰면 목차 낭독이다.
MAX_SAME_CONNECTIVE = 3
# 문장 첫머리를 볼 때 벗겨 내는 마크다운 장식·목록 기호.
CONNECTIVE_PREFIX = re.compile(r"^[\s>#*_\-–·•]+")
# 도입부 상투구는 몇 개든 하나만 있어도 알린다 — 전체 상투구 과다(>2)와 다른 검사다.
# 위치를 보지 않는 _cliche_hits로는 잡히지 않던 것이다.
MIN_INTRO_CLICHE = 1


def connective_openings(body: str, phrase: str) -> int:
    """문장을 그 연결어로 **시작한** 횟수. 문장 중간의 같은 낱말은 세지 않는다.

    '먼저'·'결국'은 목차형 연결어이기도 하지만 뜻을 가진 낱말이기도 하다. '바닥재를 먼저
    확인한다'는 순서 나열이 아니라 그 문장이 말하는 내용이다. 본문 전체에서 낱말을 세면
    둘이 같은 수로 잡히고, 제대로 쓴 글에 '같은 연결어 반복' 경고가 붙는다 — 그 경고는
    수정 재시도에 실려 나가므로, 모델에게 옳게 쓴 낱말을 빼라고 시키는 셈이 된다.

    실측(2026-07-31, 로봇청소기 비교 원고): 본문에 '먼저'가 12회 있었지만 문장 첫머리는
    **0회**였다. 전부 '무엇을 먼저 보는가'라는 내용이었다.
    """
    count = 0
    for sentence in SENTENCE_SPLIT.split(body or ""):
        if CONNECTIVE_PREFIX.sub("", sentence).strip().startswith(phrase):
            count += 1
    return count


def _first_paragraph(body: str) -> str:
    for paragraph in _paragraphs(body):
        text = paragraph.strip()
        if not text or text.startswith("#") or text.upper().startswith("[["):
            continue
        return text
    return ""


def _check_mechanical_patterns(report: QualityReport, post: FinalPost, body: str) -> None:
    """프롬프트가 금지하지만 검사가 없던 기계적 패턴들.

    전부 경고다. 원고를 버리지 않는 이유는 하나뿐이다 — 이 신호들은 "틀렸다"가 아니라
    "기계처럼 읽힌다"이고, 멀쩡한 사실을 담은 원고를 문체 때문에 통째로 버리는 것은 사용자가
    잃는 것이 더 크다. 대신 수정 재시도가 걸릴 때 함께 전달된다.
    """
    if not body:
        return

    intro = _first_paragraph(body)
    intro_cliches = [phrase for phrase in CLICHE_PHRASES if phrase in intro]
    if len(intro_cliches) >= MIN_INTRO_CLICHE:
        report.warnings.append(f"도입부 상투 표현: {', '.join(intro_cliches)}")

    # AI 답변형 화법은 한 번만 나와도 지적한다. 상투구와 달리 '과다 사용'이 문제가 아니라
    # 글쓴이가 아니라 답변자의 자리에 서 있다는 신호라서다(2026-08-05 미팅).
    assistant_tone = [phrase for phrase in ASSISTANT_TONE_PHRASES if phrase in body]
    if assistant_tone:
        report.warnings.append(f"AI 답변형 문구: {', '.join(assistant_tone)}")

    openings = {phrase: connective_openings(body, phrase) for phrase in CONNECTIVE_PHRASES}
    overused = [
        f"{phrase} {count}회"
        for phrase, count in openings.items()
        if count > MAX_SAME_CONNECTIVE
    ]
    if overused:
        report.warnings.append(f"같은 연결어 반복: {', '.join(overused)}")

    # 제목을 첫 문장에서 그대로 되풀이하는 경우. 제목 어절이 첫 문단에 그대로 다 들어오면
    # 독자는 같은 문장을 두 번 읽는다.
    # 어절 집합을 그대로 비교하면 조사 때문에 빗나간다('순서' vs '순서를'). 정규화한 문자열
    # 안에 제목 어절이 들어 있는지로 본다 — 조사·띄어쓰기 차이를 무시하는 기존 방식과 같다.
    title_tokens = [token for token in TOKEN.findall(post.title or "") if len(token) >= 2]
    if len(title_tokens) >= 3 and intro:
        intro_normalized = normalize_for_match(intro)
        if all(normalize_for_match(token) in intro_normalized for token in title_tokens):
            report.warnings.append("제목을 첫 문단에서 그대로 반복했습니다")

    headings = [heading for heading, _ in _sections(body)]
    if len(headings) >= 3:
        endings = {heading[-2:] for heading in headings if len(heading) >= 2}
        if len(endings) == 1:
            report.warnings.append(
                f"소제목이 모두 같은 어미로 끝납니다({headings[0][-2:]})"
            )

    sizes = [size for _, size in _sections(body) if size]
    if len(sizes) >= 3 and max(sizes) - min(sizes) < max(sizes) * 0.2:
        report.warnings.append("섹션 길이가 지나치게 균일합니다")
