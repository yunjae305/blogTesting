"""예약 시작 요청 검증(validate_start_batch_request·normalize_topics).

화면과 서버가 같은 규칙으로 소재를 세지 않으면 '총 N개 소재'와 실제로 만들어지는 작업
수가 어긋난다. 그래서 정리(공백·빈 줄·중복)와 한도(개수·길이·간격)를 여기서 못 박는다.

LLM·Mongo·셀레니움은 하나도 부르지 않는다 — 순수 함수 검증이다.
"""

import pytest

from app.errors import BlogTaskError
from app.modules.blog_task.validation import MAX_TOPIC_CHARS as 새글_소재_한도
from app.modules.scheduled_posting.models import SchedulePlatform, ScheduleTopicMode
from app.modules.scheduled_posting.validation import (
    MAX_INTERVAL_SECONDS,
    MAX_SCHEDULED_TOPICS,
    MIN_INTERVAL_SECONDS,
    MAX_TOPIC_CHARS,
    SCHEDULED_DEFAULT_PURPOSE,
    normalize_topics,
    validate_start_batch_request,
)

VALID_BODY = {
    "topics": ["첫 소재", "둘째 소재", "셋째 소재"],
    "intervalSeconds": 1800,
}


def body(**overrides):
    """기본 본문에서 몇 개만 바꿔 끼운다."""
    return {**VALID_BODY, **overrides}


# ------------------------------------------------------------------ 소재 정리


def test_소재는_앞뒤_공백과_줄바꿈이_지워지고_빈_줄은_버려진다():
    topics, dropped = normalize_topics(
        ["  첫 소재  ", "", "   ", "\n둘째 소재\n", "\t셋째 소재\t"]
    )

    assert topics == ["첫 소재", "둘째 소재", "셋째 소재"]
    assert dropped == []


def test_중복된_소재는_첫_번째만_남고_입력_순서가_유지된다():
    """뒤에 온 중복을 남기면 사용자가 적어 넣은 우선순위가 뒤집힌다."""
    topics, dropped = normalize_topics(
        ["가을 등산", "겨울 캠핑", "  가을 등산\n", "봄 소풍", "겨울 캠핑"]
    )

    assert topics == ["가을 등산", "겨울 캠핑", "봄 소풍"]
    # 버린 것은 화면에 그대로 알려 주므로 정리된 형태로 담긴다.
    assert dropped == ["가을 등산", "겨울 캠핑"]


def test_정리된_중복은_요청_결과의_dropped_duplicates에_담긴다():
    result = validate_start_batch_request(
        body(topics=["첫 소재", "  첫 소재  ", "둘째 소재"])
    )

    assert result.topics == ["첫 소재", "둘째 소재"]
    assert result.dropped_duplicates == ["첫 소재"]
    assert result.target_count == 2


def test_소재가_배열이_아니거나_문자열이_아니면_거부한다():
    with pytest.raises(BlogTaskError) as caught:
        normalize_topics("첫 소재")
    assert caught.value.code == "VALIDATION_FAILED"

    with pytest.raises(BlogTaskError) as caught:
        normalize_topics(["첫 소재", 3])
    assert caught.value.code == "VALIDATION_FAILED"


# ------------------------------------------------------------------ 소재 개수


def test_소재가_하나도_없으면_거부한다():
    for 없는_소재 in ([], ["", "   ", "\n"]):
        with pytest.raises(BlogTaskError) as caught:
            validate_start_batch_request(body(topics=없는_소재))
        assert caught.value.code == "VALIDATION_FAILED"

    # topics 키 자체가 없는 본문도 마찬가지다.
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request({"intervalSeconds": 1800})
    assert caught.value.code == "VALIDATION_FAILED"


def test_소재가_20개면_통과하고_21개면_거부한다():
    스무개 = [f"소재 {n}" for n in range(MAX_SCHEDULED_TOPICS)]
    result = validate_start_batch_request(body(topics=스무개))
    assert len(result.topics) == MAX_SCHEDULED_TOPICS == 20

    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(body(topics=[*스무개, "소재 20"]))
    assert caught.value.code == "VALIDATION_FAILED"


def test_중복을_지운_뒤의_개수로_상한을_센다():
    """21개를 넣어도 중복 하나를 지우면 20개다 — 화면이 세는 수와 같아야 한다."""
    스무개 = [f"소재 {n}" for n in range(MAX_SCHEDULED_TOPICS)]

    result = validate_start_batch_request(body(topics=[*스무개, "소재 0"]))

    assert len(result.topics) == MAX_SCHEDULED_TOPICS
    assert result.dropped_duplicates == ["소재 0"]


# ------------------------------------------------------------------ 소재 길이


def test_소재_길이_한도는_새_글_작성과_같은_300자다():
    """예약만 더 긴 소재를 받아 두면 create_blog_task가 뒤늦게 거부해, 이미 만들어진
    배치 안에서 실패한다. 그래서 같은 상수를 쓴다."""
    assert MAX_TOPIC_CHARS == 새글_소재_한도 == 300

    딱_맞는_소재 = "가" * MAX_TOPIC_CHARS
    result = validate_start_batch_request(body(topics=[딱_맞는_소재]))
    assert result.topics == [딱_맞는_소재]

    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(body(topics=["가" * (MAX_TOPIC_CHARS + 1)]))
    assert caught.value.code == "VALIDATION_FAILED"


# -------------------------------------------------------------------- 목표량


def test_목표량이_소재_수를_넘으면_거부한다():
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(body(targetCount=4))

    assert caught.value.code == "VALIDATION_FAILED"


def test_목표량은_정리된_소재_수를_기준으로_센다():
    """중복 하나를 지우면 소재는 2개다 — 3을 넣으면 거부다."""
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(
            body(topics=["첫 소재", "첫 소재", "둘째 소재"], targetCount=3)
        )

    assert caught.value.code == "VALIDATION_FAILED"


def test_목표량이_0_이하면_거부한다():
    for 목표량 in (0, -1, -20):
        with pytest.raises(BlogTaskError) as caught:
            validate_start_batch_request(body(targetCount=목표량))
        assert caught.value.code == "VALIDATION_FAILED"


def test_목표량이_bool이면_1로_통과하지_않는다():
    """bool은 int의 하위형이라 True를 1로 읽으면 검사를 그대로 지나간다."""
    for 목표량 in (True, False):
        with pytest.raises(BlogTaskError) as caught:
            validate_start_batch_request(body(targetCount=목표량))
        assert caught.value.code == "VALIDATION_FAILED"


def test_목표량이_소재_수보다_적으면_앞에서부터_그만큼만_예약한다():
    """입력 순서가 곧 사용자가 정한 우선순위다."""
    다섯개 = ["첫 소재", "둘째 소재", "셋째 소재", "넷째 소재", "다섯째 소재"]

    result = validate_start_batch_request(body(topics=다섯개, targetCount=2))

    assert result.topics == ["첫 소재", "둘째 소재"]
    assert result.target_count == 2


def test_목표량을_생략하면_소재_수만큼이다():
    result = validate_start_batch_request(body())

    assert result.target_count == 3
    assert result.topics == VALID_BODY["topics"]


# ---------------------------------------------------------------- 발행 간격


def test_발행_간격은_15초_이상_60분_이하만_받는다():
    for 간격 in (MIN_INTERVAL_SECONDS, 30, 600, MAX_INTERVAL_SECONDS):
        result = validate_start_batch_request(body(intervalSeconds=간격))
        assert result.interval_seconds == 간격

    # 15초 미만은 네이버가 연속 게시로 볼 위험이 크다.
    for 간격 in (0, -1, MIN_INTERVAL_SECONDS - 1, MAX_INTERVAL_SECONDS + 1):
        with pytest.raises(BlogTaskError) as caught:
            validate_start_batch_request(body(intervalSeconds=간격))
        assert caught.value.code == "VALIDATION_FAILED"


def test_발행_간격이_bool이나_문자열이면_거부한다():
    for 간격 in (True, False, "30", "", None, 30.0):
        with pytest.raises(BlogTaskError) as caught:
            validate_start_batch_request(body(intervalSeconds=간격))
        assert caught.value.code == "VALIDATION_FAILED"


def test_발행_간격_키가_아예_없으면_거부한다():
    """기본값을 조용히 끼워 넣지 않는다 — 간격은 사용자가 정하는 값이다."""
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request({"topics": ["첫 소재"]})

    assert caught.value.code == "VALIDATION_FAILED"


# ------------------------------------------------------------------ 플랫폼


def test_플랫폼을_생략하면_네이버다():
    result = validate_start_batch_request(body())

    assert result.platform == SchedulePlatform.NAVER


def test_platform에_다른_값을_넣으면_거부한다():
    """platform은 배치의 이름표다 — 발행할 곳은 publishNaver·publishThreads가 정한다.

    두 곳에서 채널을 정하려는 요청이므로 조용히 고치지 않고 막는다.
    """
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(body(platform="threads"))

    assert caught.value.code == "VALIDATION_FAILED"
    assert "publishThreads" in caught.value.message


def test_publishThreads가_요청에서_결과로_실린다():
    result = validate_start_batch_request(body(publishThreads=True))

    assert result.publish_threads is True
    # 기본값은 예전 동작(네이버만)이다.
    assert validate_start_batch_request(body()).publish_threads is False


def test_publishThreads는_불리언만_받는다():
    with pytest.raises(BlogTaskError):
        validate_start_batch_request(body(publishThreads="yes"))


def test_publishNaver를_생략하면_True다():
    """옛 클라이언트는 이 값을 보내지 않는다 — 그때는 네이버가 언제나 발행 대상이었다."""
    assert validate_start_batch_request(body()).publish_naver is True


def test_쓰레드에만_올리는_예약을_받는다():
    """2026-08-06 사용자 요청 — 쓰레드로만 쓰고 싶은 사람이 있다."""
    result = validate_start_batch_request(
        body(publishNaver=False, publishThreads=True)
    )

    assert result.publish_naver is False
    assert result.publish_threads is True
    # 배치의 이름표는 그대로 naver다(저장·조회 경로가 그 값을 쓴다).
    assert result.platform == SchedulePlatform.NAVER


def test_두_플랫폼을_모두_끄면_거부한다():
    """아무 데도 안 올라가는 예약을 만들어 두고 나중에 실패를 보게 하지 않는다."""
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(body(publishNaver=False, publishThreads=False))

    assert caught.value.code == "VALIDATION_FAILED"
    assert "하나 이상" in caught.value.message


def test_publishNaver는_불리언만_받는다():
    for 값 in ("yes", 1, None):
        with pytest.raises(BlogTaskError):
            validate_start_batch_request(body(publishNaver=값))


def test_네이버를_명시해도_통과한다():
    result = validate_start_batch_request(body(platform="naver"))

    assert result.platform == SchedulePlatform.NAVER


# ------------------------------------------------------------- clientRequestId


def test_clientRequestId는_strip해서_담는다():
    result = validate_start_batch_request(body(clientRequestId="  req-1  "))

    assert result.client_request_id == "req-1"


def test_clientRequestId가_공백뿐이면_거부한다():
    for 값 in ("", "   ", "\n\t"):
        with pytest.raises(BlogTaskError) as caught:
            validate_start_batch_request(body(clientRequestId=값))
        assert caught.value.code == "VALIDATION_FAILED"


def test_clientRequestId를_생략하면_None이다():
    result = validate_start_batch_request(body())

    assert result.client_request_id is None


# ---------------------------------------------------------------- 본문 자체


def test_본문이_객체가_아니면_거부한다():
    for 본문 in ("문자열", ["첫 소재"], None, 3):
        with pytest.raises(BlogTaskError) as caught:
            validate_start_batch_request(본문)
        assert caught.value.code == "VALIDATION_FAILED"


# ------------------------------------------------- 소재를 글로 나누는 방식


def test_기본은_소재별_한_편이다():
    """옛 클라이언트는 topicMode를 보내지 않는다 — 그때 동작이 바뀌면 안 된다."""
    result = validate_start_batch_request(body(topics=["첫 소재", "둘째 소재"], targetCount=2))
    assert result.topic_mode is ScheduleTopicMode.MULTI


def test_소재_하나로_여러_편_모드는_글의_개수가_소재_수에_매이지_않는다():
    """소재는 하나뿐인데 글은 다섯 편 — 이 모드가 있는 이유 그 자체다."""
    result = validate_start_batch_request(
        body(topics=["한 가지 소재"], targetCount=5, topicMode="single")
    )
    assert result.topic_mode is ScheduleTopicMode.SINGLE
    assert result.topics == ["한 가지 소재"]
    assert result.target_count == 5


def test_소재_하나_모드는_여러_줄이_와도_첫_줄만_쓴다():
    """나머지를 조용히 섞으면 사용자가 고른 모드와 다른 글이 나온다."""
    result = validate_start_batch_request(
        body(topics=["첫 소재", "둘째 소재", "셋째 소재"], targetCount=3, topicMode="single")
    )
    assert result.topics == ["첫 소재"]


def test_소재_하나_모드도_글의_개수_상한은_지킨다():
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(
            body(topics=["한 가지 소재"], targetCount=MAX_SCHEDULED_TOPICS + 1, topicMode="single")
        )
    assert caught.value.code == "VALIDATION_FAILED"


def test_소재별_한_편_모드는_여전히_소재_수를_넘을_수_없다():
    with pytest.raises(BlogTaskError) as caught:
        validate_start_batch_request(
            body(topics=["첫 소재"], targetCount=3, topicMode="multi")
        )
    assert caught.value.code == "VALIDATION_FAILED"
    assert "소재 수" in caught.value.message


def test_모르는_topicMode는_거부한다():
    for 잘못된 in ("both", "", "MULTI", 1, None):
        with pytest.raises(BlogTaskError) as caught:
            validate_start_batch_request(body(topicMode=잘못된))
        assert caught.value.code == "VALIDATION_FAILED"


def test_기본_목적_문자열이_화면_상수와_같다():
    """화면은 이 문장을 걸러서 안 보여 준다 — 두 값이 어긋나면 예약 글에 다시 나타난다.

    한쪽만 고치는 사고를 막으려고 실제 파일을 읽어 맞춰 본다(2026-08-04).
    """
    from pathlib import Path

    constants = Path(__file__).resolve().parents[3] / "apps" / "web" / "src" / "constants.ts"
    assert constants.is_file(), f"화면 상수 파일을 찾지 못했습니다: {constants}"
    text = constants.read_text(encoding="utf-8")

    assert f'"{SCHEDULED_DEFAULT_PURPOSE[0]}"' in text, (
        "apps/web/src/constants.ts의 SCHEDULED_DEFAULT_PURPOSE가 서버 값과 다릅니다 — "
        "한쪽만 고치면 예약 글의 '글 목적'에 내부 기본값이 다시 보입니다."
    )
