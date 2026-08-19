"""Facebook Graph API를 통한 인스타그램 해시태그.

Graph는 비즈니스 계정이 직접 지정한 해시태그만 검색하게 해주므로, 사용자 자신의
소재를 조회하고 그에 대한 인기 게시물에 함께 등장하는 해시태그를 읽는다 — 그 동시
등장이 트렌드 신호다.

FACEBOOK_USER_ACCESS_TOKEN과 INSTAGRAM_BUSINESS_ACCOUNT_ID가 필요하다. 둘 다 없으면
팩토리가 이 수집기를 빼고, 다른 소스들이 패널을 떠받친다.
"""

import asyncio

import httpx

from app.llm.contracts import TrendFetchInput
from app.shared import TrendSource

from .base import REQUEST_TIMEOUT, CollectedKeyword, seed_queries
from .text import count_keywords, hashtags, to_collected

GRAPH_URL = "https://graph.facebook.com/{version}"
# 시드마다 Graph 호출이 두 번 들고, 인스타그램은 주당 해시태그 조회 수를 제한한다.
MAX_SEEDS = 2
MEDIA_FIELDS = "caption"


class InstagramTrendCollector:
    source = TrendSource.INSTAGRAM

    def __init__(self, access_token: str, ig_user_id: str, api_version: str):
        self._access_token = access_token
        self._ig_user_id = ig_user_id
        self._base = GRAPH_URL.format(version=api_version)

    async def collect(
        self,
        trend_input: TrendFetchInput,
        limit: int | None,
        known: frozenset[str] = frozenset(),
    ) -> list[CollectedKeyword]:
        seeds = seed_queries(trend_input, limit=MAX_SEEDS)
        if not seeds:
            return []

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            results = await asyncio.gather(
                *(self._captions_for(client, seed) for seed in seeds),
                return_exceptions=True,
            )

        captions: list[str] = []
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            captions.extend(result)

        # 모든 시드가 실패한 것은 소재가 인기 없는 게 아니라 토큰이나 계정 id가 잘못된
        # 것이다 — 조용히 []를 반환하는 대신 드러낸다.
        if failures == len(seeds):
            raise RuntimeError("every Instagram hashtag lookup failed")
        if not captions:
            return []

        counts = count_keywords(captions, exclude=seeds, extractor=hashtags)
        return to_collected(counts, limit, known)

    async def _captions_for(self, client: httpx.AsyncClient, seed: str) -> list[str]:
        hashtag_id = await self._hashtag_id(client, seed)
        if not hashtag_id:
            return []

        payload = await self._get(
            client,
            f"{self._base}/{hashtag_id}/top_media",
            {"user_id": self._ig_user_id, "fields": MEDIA_FIELDS},
        )

        data = payload.get("data")
        if not isinstance(data, list):
            return []

        return [
            item["caption"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("caption"), str)
        ]

    async def _hashtag_id(self, client: httpx.AsyncClient, seed: str) -> str | None:
        # Graph는 구절이 아니라 해시태그를 매칭한다: "여름 휴가"는 "여름휴가"가 돼야 한다.
        query = "".join(seed.split())
        if not query:
            return None

        payload = await self._get(
            client,
            f"{self._base}/ig_hashtag_search",
            {"user_id": self._ig_user_id, "q": query},
        )

        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None

        first = data[0]
        hashtag_id = first.get("id") if isinstance(first, dict) else None
        return hashtag_id if isinstance(hashtag_id, str) else None

    async def _get(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str]
    ) -> dict:
        response = await client.get(
            url, params={**params, "access_token": self._access_token}
        )
        if response.is_error:
            raise RuntimeError(
                f"Instagram Graph request failed with {response.status_code}: {response.text}"
            )

        payload = response.json() if response.text else None
        return payload if isinstance(payload, dict) else {}
