"""LLM·이미지 provider 호출이 공유하는 httpx 클라이언트.

예전에는 _post_json이 호출(재시도 포함)마다 AsyncClient를 새로 만들어 TCP·TLS
핸드셰이크를 반복했다. 한 번의 글 생성이 provider를 십수 번 부르므로(설계·원고·
카드 계획·이미지 3~6장) keep-alive 풀 하나로 연결을 재사용한다.

클라이언트는 이벤트 루프에 묶인다 — 루프가 바뀌면(pytest는 테스트마다 새 루프)
이전 루프의 연결을 재사용할 수 없으므로 새로 만든다. 이전 클라이언트는 닫지 않고
버린다: 닫으려면 죽은 루프가 필요하고, 테스트에서는 respx가 가로채 실제 소켓이
열리지 않는다. 운영에서는 루프가 하나라 이 경로를 타지 않는다.
"""

from __future__ import annotations

import asyncio

import httpx

# read가 지배적이다: 이미지 생성·긴 원고는 응답까지 수십 초가 걸린다.
#
# **180초인 이유**(2026-08-13에 300초에서 내렸다). 사용자 신고: "계속 연결오류로 재시도
# 시작하니까 속도가 느려". 멈춘 호출 하나가 300초를 태우고 그제야 재시도하니, 한 번
# 걸릴 때마다 5분이 사라졌다(최악 4시도 = 20분).
#
# 값은 실측에서 왔다. 같은 로그의 provider 호출 소요는 편집 문체 26·29초, 콘텐츠 설계
# 57·57초, 본문 50·51초, 이미지 계획 33·41초, 이중 비평 38·39초, 이미지 생성 39·41초로
# **가장 느린 것이 57초**다. 180초는 그 3배이고, 여기 걸리는 호출은 정상 지연이 아니라
# 멈춘 것으로 본다. 재시도는 1.5초 뒤에 시작하므로 대개 그쪽이 먼저 끝난다.
#
# httpx의 read는 **청크 사이 대기**다. 이미지 응답이 수 MB base64라도 흐르는 동안에는
# 갱신되므로, 느린 회선에서 다운로드가 오래 걸린다고 걸리지 않는다.
#
# connect·write는 이 배포의 실제 회선에 맞춘다(2026-08-07 사용자 신고: "provider 연결
# 오류 (1/4) - 재시도"가 반복). 실측한 회선이 0.09MB/s 수준이라 — GPT 비평은 원고
# 이미지 전부(1~2MB), 이미지 생성은 참고 사진을 함께 올린다 — write 30초가 업로드
# 도중에 끊고, 느린 TLS 연결이 connect 10초에 걸렸다. 짧게 끊고 재시도하면 같은
# 업로드를 처음부터 다시 하므로 손해다.
REQUEST_TIMEOUT = httpx.Timeout(connect=20.0, read=180.0, write=120.0, pool=10.0)
# provider 호스트는 3~4개뿐이다. 이미지 병렬 생성(최대 6장) + 동시 사용자 요청을
# 감당할 만큼만 열고, keep-alive로 유지한다.
LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def shared_client() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, limits=LIMITS)
        _client_loop = loop
    return _client


async def close_shared_client() -> None:
    """서버 종료 시 연결 정리. 종료 외에는 부르지 않는다."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
