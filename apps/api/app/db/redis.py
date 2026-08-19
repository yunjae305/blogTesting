"""Redis 연결 한 곳.

트렌드 풀 캐시(app/llm/trends/cache.py)는 자기 연결을 따로 만든다 — LLM 프로바이더가
Mongo·Redis보다 먼저 조립되기 때문이다. 여기서 만드는 연결은 그 뒤에 붙는 것들(노출 이력,
멱등성 키, 작업 잠금)이 함께 쓴다. 같은 Redis에 연결을 여러 개 두지 않기 위해서다.

연결은 만들 때 열리지 않는다(첫 명령이 연결한다). 그래서 시작 로그가 "Redis 사용"이라고
해 놓고 실제로는 Redis가 죽어 있는 일이 생긴다 — 사실이 아닌 로그는 로그가 없는 것보다
나쁘므로, 여기서 한 번 ping을 보내 확인한 결과를 돌려준다.
"""

import logging

logger = logging.getLogger(__name__)

# 연결·명령 타임아웃(초). Redis가 응답하지 않을 때 요청이 그만큼만 기다리고 폴백으로
# 넘어가게 한다 — 무한정 기다리면 트렌드 패널이 통째로 멈춘다.
CONNECT_TIMEOUT_SECONDS = 2.0
COMMAND_TIMEOUT_SECONDS = 3.0
# 끊긴 연결 재시도 횟수. redis-py가 타임아웃·연결 오류에 대해 자체적으로 다시 시도한다.
MAX_RETRIES = 2


def create_redis(url: str | None):
    """설정돼 있으면 Redis 클라이언트, 아니면 None.

    None은 오류가 아니라 '이 배포에는 Redis가 없다'는 뜻이다. 부르는 쪽이 메모리 폴백을
    고른다 — 로컬 개발에서 Redis를 띄우지 않고도 서버가 뜨는 이유다.
    """
    if not url:
        return None

    try:
        from redis.asyncio import Redis
        from redis.asyncio.retry import Retry
        from redis.backoff import ExponentialBackoff
    except ImportError:
        logger.warning(
            "REDIS_URL이 설정됐지만 redis 패키지가 없습니다. 메모리 저장소로 계속합니다."
        )
        return None

    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=COMMAND_TIMEOUT_SECONDS,
        retry=Retry(ExponentialBackoff(), MAX_RETRIES),
        health_check_interval=30,
    )


async def ping(client) -> bool:
    """연결이 실제로 되는지. 실패해도 예외를 올리지 않는다 — Redis 없이도 서버는 뜬다."""
    if client is None:
        return False
    try:
        await client.ping()
        return True
    except Exception as error:
        # 접속 정보(비밀번호가 들어 있는 URL)는 남기지 않는다.
        logger.warning("Redis 연결 확인 실패: %s", error)
        return False


async def close_redis(client) -> None:
    """종료 시 연결을 정리한다. 이미 끊겼더라도 종료를 막지 않는다."""
    if client is None:
        return
    try:
        await client.aclose()
    except Exception as error:
        logger.warning("Redis 연결 종료 실패: %s", error)
