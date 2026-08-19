"""이미 발행한 내 글과 새 원고가 얼마나 겹치는가.

자동 생성의 가장 큰 위험은 **한 편의 품질**이 아니라 **쌓였을 때의 닮음**이다. 같은
브랜드로 계속 쓰면 제목·소제목·도입부가 조금씩만 달라진 글이 누적되는데, 한 편씩
보면 어느 것도 이상하지 않아 기존 검사는 아무것도 잡지 못한다.

기존 장치와 겹치지 않는다:

- ``quality.check_draft``와 ``content_validation.validate_template_repetition``은
  **한 원고 안**의 반복을 본다.
- 예약의 ``exclude_titles``는 **같은 배치**의 형제 글만 본다(제목만, 생성 전).

여기는 **이미 발행된 내 글 전체**와 **생성된 뒤**를 본다.

전부 순수 함수다. 외부 호출도, LLM도, DB도 없다 — 무엇을 비교할지는 부르는 쪽이 준다.
그래서 임계값을 바꿔 가며 시험할 수 있고, 검사 하나가 원고 생성을 막을 일도 없다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 비교용 정규화. 조사·띄어쓰기·문장부호 차이만 다른 표현은 같은 것으로 본다
# (keyword_naturalization._NORM과 같은 규칙).
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")

#: 제목·소제목·도입부에 각각 주는 가중치. 도입부를 가장 무겁게 두는 이유는, 자동 생성
#: 글이 가장 먼저 닮는 곳이 첫 문단이기 때문이다("안녕하세요, 오늘은 ○○에 대해").
_WEIGHTS = {"title": 0.3, "headings": 0.3, "opening": 0.4}

#: 운영 기준(검색엔진의 공식 수치가 아니다 — 우리 내부 관리 기준이다).
#: 0.85 이상이면 사실상 같은 글이고, 0.70~0.85면 도입부·소제목을 다시 쓰게 한다.
NEAR_DUPLICATE = 0.85
SIMILAR = 0.70

#: 몇 편까지 거슬러 비교할지. 전부 비교하면 글이 쌓일수록 느려지는데, 닮음은 대개
#: 최근 글에서 먼저 드러난다.
COMPARE_LIMIT = 30


def tokens(text: str) -> set[str]:
    """비교 단위. 한 글자 토큰은 버린다 — '이', '그'까지 세면 아무 글이나 닮아 보인다."""
    return {token for token in _TOKEN.findall((text or "").lower()) if len(token) > 1}


def similarity(left: str, right: str) -> float:
    """두 글의 겹침(자카드). 둘 중 하나라도 비면 0이다."""
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class PostDigest:
    """비교에 필요한 것만. 본문 전체를 들고 다니지 않는다(글 하나가 수십 KB다)."""

    post_id: str
    title: str = ""
    headings: list[str] = field(default_factory=list)
    opening: str = ""


@dataclass(frozen=True)
class DuplicationVerdict:
    score: float
    title_score: float
    headings_score: float
    opening_score: float
    #: 가장 닮은 글. 없으면 None.
    closest: PostDigest | None

    @property
    def near_duplicate(self) -> bool:
        return self.score >= NEAR_DUPLICATE

    @property
    def similar(self) -> bool:
        return self.score >= SIMILAR


def extract_headings(markdown: str) -> list[str]:
    """마크다운 소제목(## 이하). 원고는 H1을 쓰지 않으므로 ##부터 본다."""
    found = []
    for line in (markdown or "").splitlines():
        match = re.match(r"^#{2,4}\s+(.+?)\s*$", line.strip())
        if match:
            found.append(match.group(1))
    return found


def first_paragraph(markdown: str) -> str:
    """첫 실질 문단. 소제목·이미지·인용은 건너뛴다."""
    for block in re.split(r"\n\s*\n", markdown or ""):
        text = block.strip()
        if not text or text.startswith("#") or text.startswith(">") or text.startswith("!["):
            continue
        return text
    return ""


def compare(candidate: PostDigest, published: list[PostDigest]) -> DuplicationVerdict:
    """새 원고를 이미 발행한 글들과 견준다. 가장 닮은 한 편만 돌려준다.

    셋을 따로 재고 가중 평균한다 — 제목만 같고 내용이 다른 글과, 도입부가 통째로 같은
    글은 손봐야 할 곳이 다르다. 그래서 합계 하나만 주지 않고 축별 점수도 함께 준다.
    """
    best: DuplicationVerdict | None = None
    for other in published[:COMPARE_LIMIT]:
        if other.post_id == candidate.post_id:
            continue
        title_score = similarity(candidate.title, other.title)
        headings_score = similarity(" ".join(candidate.headings), " ".join(other.headings))
        opening_score = similarity(candidate.opening, other.opening)
        score = (
            title_score * _WEIGHTS["title"]
            + headings_score * _WEIGHTS["headings"]
            + opening_score * _WEIGHTS["opening"]
        )
        if best is None or score > best.score:
            best = DuplicationVerdict(
                score=score,
                title_score=title_score,
                headings_score=headings_score,
                opening_score=opening_score,
                closest=other,
            )
    return best or DuplicationVerdict(0.0, 0.0, 0.0, 0.0, None)
