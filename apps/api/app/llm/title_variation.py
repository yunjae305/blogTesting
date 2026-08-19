"""제목 후보의 역할 배정과 재생성 방향 회전.

**난수를 쓰지 않는다.** 예전에는 다양성을 temperature 1.0에 맡겼는데(모델 샘플링), 그것은
재현되지 않고 무엇이 달라졌는지 설명할 수도 없다. Opus 5는 temperature를 아예 받지 않으므로
그 방식은 남길 수도 없다.

대신 코드가 정한다:
- 후보 5개는 **서로 다른 역할**을 맡는다. 프롬프트에 "다양하게 쓰라"고 적는 것으로는 각도가
  갈리지 않는다(실측: 31사례 중 24건에서 다섯 후보 중 최소 한 쌍이 같은 말로 시작했다).
- '제목 추천 다시'는 **방향을 옮긴다**. 같은 소재·같은 회차면 항상 같은 방향이 나오고, 회차가
  바뀌면 다음 방향으로 돌아간다. 프롬프트에는 seed 숫자가 아니라 방향의 이름과 의미만 준다 —
  숫자는 모델에게 아무 뜻이 없다.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateRole:
    label: str
    direction: str
    # 이 역할을 쓰려면 목적이 허용해야 하는 후킹. 비어 있으면 어느 목적에서나 쓸 수 있다.
    requires_hooks: frozenset[str] = frozenset()


# 후보 다섯이 서로 다른 일을 하게 만드는 역할. 순서가 곧 배정 우선순위다.
CANDIDATE_ROLES: tuple[CandidateRole, ...] = (
    CandidateRole(
        "독자 상황",
        "독자가 지금 겪고 있는 상황이나 조건을 제목에서 그대로 짚는다.",
    ),
    CandidateRole(
        "구체적 효익",
        "이 글을 읽으면 무엇을 얻는지, 얻는 것을 구체적인 말로 적는다.",
    ),
    CandidateRole(
        "핵심 차이",
        "내용을 읽어야 알 수 있는 핵심 차이나 조건을 앞세운다. 답을 제목에서 다 주지 않는다.",
    ),
    CandidateRole(
        "인식 차이",
        "흔히 알려진 것과 실제가 다른 지점을 짚는다. 실제로 다른 근거가 있을 때만 쓴다.",
        requires_hooks=frozenset({"REVERSAL", "CURIOSITY"}),
    ),
    CandidateRole(
        "비교 기준",
        "두 대상이나 선택지를 무엇을 기준으로 나누는지 제목에서 밝힌다.",
        requires_hooks=frozenset({"COMPARISON"}),
    ),
)


@dataclass(frozen=True)
class RegenerationDirection:
    name: str
    meaning: str


# 재생성이 옮겨 갈 축. 문장만 바꾸는 것이 아니라 **관점을 옮기는** 것이 재생성이다.
REGENERATION_DIRECTIONS: tuple[RegenerationDirection, ...] = (
    RegenerationDirection(
        "대상 독자 축",
        "누가 읽어야 하는 글인지를 앞세운다. 독자를 가르는 조건이 제목에 드러나게 한다.",
    ),
    RegenerationDirection(
        "사용 시점 축",
        "언제 필요한 이야기인지를 앞세운다. 시기·주기·순서 중 하나가 제목에 드러나게 한다.",
    ),
    RegenerationDirection(
        "문제 원인 축",
        "증상이 아니라 원인을 앞세운다. 무엇 때문에 그렇게 되는지가 제목에 드러나게 한다.",
    ),
    RegenerationDirection(
        "비교 기준 축",
        "무엇과 무엇을 어떤 기준으로 나누는지를 앞세운다.",
    ),
    RegenerationDirection(
        "실행 결과 축",
        "그대로 했을 때 무엇이 달라지는지를 앞세운다. 결과를 먼저 보여준다.",
    ),
    RegenerationDirection(
        "오해 교정 축",
        "많이들 이렇게 알고 있지만 실제는 다르다는 지점을 앞세운다. 근거가 있을 때만.",
    ),
    RegenerationDirection(
        "선택 기준 축",
        "독자가 무엇을 기준으로 골라야 하는지를 앞세운다. 같은 효익을 다른 말로 바꾸지 않는다.",
    ),
)


# 결말 방식. 아키타입 구조의 마지막 줄이 결말을 정하지만, 같은 아키타입이면 결말도 늘 같아서
# 같은 소재로 여러 번 쓰면 마지막 문단이 똑같이 읽혔다. 도입(RHYTHM_OPENINGS)에는 이미 축이
# 있는데 결말에는 없었다.
#
# 도입을 회전 대상에 넣지 않는 이유: 도입 방식은 모델이 articleRhythm으로 고르고(목적·자료를
# 보고 하는 판단이다) 코드가 정리·폴백한다. 그건 임의 선택이 아니라 근거 있는 선택이므로
# 그대로 둔다. 결말은 그런 판단 축이 없었기 때문에 코드가 돌린다.
CLOSING_MODES: tuple[RegenerationDirection, ...] = (
    RegenerationDirection("확인 기준", "독자가 바로 확인할 기준 하나로 닫는다."),
    RegenerationDirection("다음 행동", "지금 할 수 있는 가장 작은 다음 행동 하나로 닫는다."),
    RegenerationDirection("조건부 판단", "어떤 조건에서 어떻게 다른지로 닫는다."),
    RegenerationDirection("작성자 관점", "글을 쓴 사람의 최종 관점 한 줄로 닫는다."),
    RegenerationDirection("놓치기 쉬운 것", "고를 때 놓치기 쉬운 한 가지로 닫는다."),
    RegenerationDirection("남은 확인", "앞으로 확인이 필요한 부분으로 닫는다."),
)

# 목적별로 어울리지 않는 결말은 후보에서 뺀다. 비어 있으면 전부 쓸 수 있다.
_CLOSING_EXCLUDED_BY_PURPOSE: dict[str, frozenset[str]] = {
    # 일상 기록에 '확인 기준'·'다음 행동'을 닫는 말로 쓰면 정보글로 바뀐다.
    "일상·경험 공유": frozenset({"확인 기준", "다음 행동"}),
    # 비교글의 결말은 판단이어야 한다 — '남은 확인'으로 닫으면 비교를 안 한 글이 된다.
    "비교·추천": frozenset({"남은 확인"}),
}


def closing_mode(
    *, post_id: str, revision: int, purpose: str
) -> RegenerationDirection:
    """이 글의 결말 방식. 같은 (글, 회차, 목적)이면 항상 같고, 다시 생성하면 다음 축으로 간다."""
    excluded = _CLOSING_EXCLUDED_BY_PURPOSE.get(purpose, frozenset())
    usable = [mode for mode in CLOSING_MODES if mode.name not in excluded] or list(CLOSING_MODES)
    base = zlib.crc32(f"{post_id}:{purpose}".encode("utf-8"))
    return usable[(base + max(revision, 0)) % len(usable)]


def roles_for_purpose(
    allowed_hooks: Sequence[str], *, count: int
) -> tuple[CandidateRole, ...]:
    """목적이 허용한 후킹 안에서 쓸 수 있는 역할만, 최대 count개.

    쓸 수 없는 역할은 억지로 적용하지 않는다 — 비교 대상이 없는 글에 '비교 기준' 역할을
    맡기면 모델은 없는 비교를 만든다.
    """
    permitted = set(allowed_hooks)
    usable = [
        role
        for role in CANDIDATE_ROLES
        if not role.requires_hooks or (role.requires_hooks & permitted)
    ]
    return tuple(usable[:count])


def regeneration_direction(
    *, seed_key: str, regeneration_count: int
) -> RegenerationDirection | None:
    """이번 재생성이 옮겨 갈 방향. 첫 생성(0회)에는 없다.

    같은 `seed_key`(키워드 또는 글 id)와 같은 회차면 항상 같은 방향이 나오고, 회차가 오르면
    다음 방향으로 넘어간다. 난수가 아니라 해시라서 재현되고, 왜 그 방향이 나왔는지 설명할 수
    있다.
    """
    if regeneration_count <= 0:
        return None
    base = zlib.crc32((seed_key or "").encode("utf-8"))
    index = (base + regeneration_count) % len(REGENERATION_DIRECTIONS)
    return REGENERATION_DIRECTIONS[index]
