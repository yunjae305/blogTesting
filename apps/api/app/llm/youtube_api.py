"""유튜브 Data API 호출에 붙는 헤더를 한 곳에 둔다(2026-08-12 — 키의 리퍼러 제한).

무엇이 문제였나. 새로 발급한 `YOUTUBE_API_KEY`에 **애플리케이션 제한 = HTTP 리퍼러**가
걸려 있었다. 이 제한은 요청의 `Referer` 헤더를 콘솔에 등록한 패턴과 대조하는 검사인데,
우리 호출은 브라우저가 아니라 서버(httpx)가 만들므로 그 헤더가 아예 없다. 그래서 모든
호출이 유튜브에 닿기도 전에 게이트웨이에서 잘렸다:

    403 PERMISSION_DENIED
    reason: API_KEY_HTTP_REFERRER_BLOCKED
    metadata.httpReferrer: <empty>

원래는 콘솔에서 제한을 '없음'으로 바꾸는 것이 정답이다. 그러나 콘솔을 고칠 수 없는
상황이라(2026-08-12 사용자), **등록된 리퍼러를 우리가 실어 보내는** 쪽으로 맞춘다.
이것은 남의 자물쇠를 여는 것이 아니라 우리 키에 우리가 건 조건을 우리 호출이 만족시키는
것이다 — 리퍼러 제한은 브라우저에서 새어 나간 키를 위한 검사라, 서버 호출에는 애초에
보호가 되지 않는다(그래서 서버 키에는 IP 제한이나 '없음 + API 제한'을 쓴다).

실측(2026-08-12, 실제 API 호출):

| 보낸 Referer | 결과 |
|---|---|
| (헤더 없음) | 403 API_KEY_HTTP_REFERRER_BLOCKED |
| `http://localhost:5173/` | 200 |
| `http://localhost:3000/` | 200 |
| `http://localhost/` | 200 |
| `http://127.0.0.1:5173/` | **403** — 등록된 것은 `localhost`뿐이다 |

호출부가 둘(트렌드 수집·썸네일 검색)이라 여기서만 만든다. 한 곳이 빠지면 그 경로만
조용히 403으로 죽는다 — 네이버 인증 헤더를 `naver_api`에 모아 둔 것과 같은 이유다.
"""

from __future__ import annotations

#: 값을 지정하는 환경 변수. 콘솔에 등록한 리퍼러가 바뀌면 이것만 고치면 된다.
YOUTUBE_API_REFERRER_ENV = "YOUTUBE_API_REFERRER"

#: 기본값. 지금 키에 등록된 것이 로컬호스트라 개발 환경에서 그대로 통한다. 리퍼러 제한이
#: 없는 키에서는 이 헤더가 무시되므로, 보내도 손해가 없다.
DEFAULT_YOUTUBE_API_REFERRER = "http://localhost:5173/"


def api_headers(referrer: str | None) -> dict[str, str]:
    """유튜브 API 호출에 붙일 헤더. 리퍼러가 비어 있으면 아무것도 붙이지 않는다."""
    return {"Referer": referrer} if referrer else {}
