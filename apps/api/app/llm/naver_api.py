"""네이버 검색 API의 접속 규격을 한 곳에 둔다(2026-08-11 — NAVER API HUB 이관).

무엇이 바뀌었나. 개발자센터(developers.naver.com)의 검색·검색어 트렌드 API가
NAVER API HUB(네이버 클라우드 플랫폼)로 이관됐고, **호스트도 인증 헤더도 다르다.**

| | 개발자센터(옛) | NAVER API HUB(현재) |
|---|---|---|
| 호스트 | `openapi.naver.com` | `naverapihub.apigw.ntruss.com` |
| 경로 | `/v1/search/{종류}.json` | `/search/v1/{종류}` (확장자 없음) |
| 인증 | `X-Naver-Client-Id` / `X-Naver-Client-Secret` | `X-NCP-APIGW-API-KEY-ID` / `X-NCP-APIGW-API-KEY` |

키를 API HUB에서 새로 발급받았는데 코드가 옛 주소로 부르면 **모든 호출이 401
`errorCode 024`로 죽는다**. 실측(2026-08-11): 옛 주소로 뉴스·블로그·이미지·데이터랩
4종 전부 401이었고, 같은 키를 API HUB 주소·헤더로 보내니 전부 200이었다. 그 401이
서버 로그를 "네이버 보강: 키워드 측정 실패" 경고로 가득 채우고 있었다.

응답 본문 형식(`lastBuildDate`·`total`·`items[]`)은 양쪽이 같아서, 주소와 헤더만
바꾸면 파싱 코드는 그대로 돈다.

세 호출부(트렌드 발굴·이미지 검색·블로그 본문 수집)가 같은 규격을 쓰므로 여기서만
만든다 — 한 곳이 빠지면 그 경로만 조용히 401로 죽기 때문이다.
"""

from __future__ import annotations

NAVER_API_HUB_BASE = "https://naverapihub.apigw.ntruss.com"

#: 검색 API. ``kind``는 news·blog·image·kin·cafearticle 등이다.
SEARCH_URL_TEMPLATE = NAVER_API_HUB_BASE + "/search/v1/{kind}"

def search_url(kind: str) -> str:
    """검색 종류 하나의 요청 주소."""
    return SEARCH_URL_TEMPLATE.format(kind=kind)


def auth_headers(client_id: str, client_secret: str) -> dict[str, str]:
    """API HUB 인증 헤더. 이름을 틀리면 게이트웨이가 401로 답한다."""
    return {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
    }
