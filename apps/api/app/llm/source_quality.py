"""자료로 쓰면 안 되는 출처를 걸러낸다(2026-08-11 사용자 지시).

지시: "나무위키나 디씨인사이드 같은 것은 안 돼."

왜 코드로 거르나. 수집 프롬프트에도 쓰지 말라고 적었지만, 프롬프트는 **부탁**이고
grounding이 무엇을 물어 올지는 우리가 정하지 못한다. 실제로 그 전 프롬프트는 정반대로
"community forums and wikis and Q&A threads"를 권장하고 있었고, 그렇게 들어온 자료가
원고의 사실 근거가 됐다. 그래서 마지막에 한 번 더 코드로 막는다.

무엇을 막나. **익명 커뮤니티**와 **사용자가 자유롭게 고쳐 쓰는 팬덤 위키**다. 둘의 공통점은
글쓴이가 누구인지·언제 무엇이 바뀌었는지 확인할 수 없다는 것이다 — 블로그 후기도 익명이지만
그것은 '한 사람의 경험'이라는 사실 자체가 근거이고, 이쪽은 '사실'이라고 적힌 것을 아무도
책임지지 않는다.

무엇을 막지 않나.

- **위키백과(wikipedia.org)** — 사용자 편집이지만 문장마다 출처를 요구하고 이력이 공개다.
  나무위키와 같은 것으로 묶지 않았다. 원치 않으면 아래 목록에 한 줄 더하면 된다.
- **네이버·티스토리 블로그, 커뮤니티형 후기** — 실사용 경험은 그 자체가 근거이고, 이미
  '실사용 글'이라는 표시와 함께 실린다(naver_blog).

목록은 도메인으로만 판정한다. 제목·본문으로 추측해 지우면 멀쩡한 자료가 사라진다.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: 자료로 쓰지 않는 도메인. 하위 도메인도 함께 막힌다(m.dcinside.com 등).
BLOCKED_SOURCE_DOMAINS: frozenset[str] = frozenset(
    {
        # 사용자 편집 팬덤 위키
        "namu.wiki",
        "namuwiki.com",
        "librewiki.net",
        "riguwiki.com",
        # 익명 커뮤니티
        "dcinside.com",
        "ilbe.com",
        "fmkorea.com",
        "theqoo.net",
        "instiz.net",
        "pann.nate.com",
        "todayhumor.co.kr",
        "ppomppu.co.kr",
        "ruliweb.com",
        "clien.net",
        "bobaedream.co.kr",
        "mlbpark.donga.com",
        "82cook.com",
        "gall.dcinside.com",
        # 해외 익명 커뮤니티
        "reddit.com",
        "4chan.org",
        "quora.com",
    }
)


def _host_of(url: str) -> str:
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_blocked_source(url: str) -> bool:
    """이 주소가 자료로 쓰면 안 되는 곳인가. 주소가 아니면 막지 않는다."""
    host = _host_of(url)
    if not host:
        return False
    return any(
        host == blocked or host.endswith("." + blocked)
        for blocked in BLOCKED_SOURCE_DOMAINS
    )


def drop_blocked_sources(sources: list, *, where: str = "") -> list:
    """막힌 출처를 걸러낸 목록. 몇 건을 왜 뺐는지 로그로 남긴다.

    ``sources``의 항목은 ``url`` 속성을 가진 것이면 무엇이든 된다(SearchSource 등).
    걸러낸 사실을 조용히 숨기지 않는 이유: 자료가 적게 잡힐 때 원인이 검색인지 이 필터인지
    구분할 수 있어야 한다.
    """
    kept = [source for source in sources if not is_blocked_source(getattr(source, "url", ""))]
    dropped = len(sources) - len(kept)
    if dropped:
        removed = [
            _host_of(getattr(source, "url", ""))
            for source in sources
            if is_blocked_source(getattr(source, "url", ""))
        ]
        logger.info(
            "자료 출처 제외 | %s%d건 (%s) - 익명 커뮤니티·사용자 편집 위키는 근거로 쓰지 않는다",
            f"{where} " if where else "",
            dropped,
            ", ".join(sorted(set(removed))),
        )
    return kept
