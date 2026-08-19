"""M2 트렌드 키워드 수집, 소스당 수집기 하나."""

from .aggregate import AggregateTrendProvider
from .base import CollectedKeyword, TrendCollector, seed_queries
from .cache import (
    POOL_TTL_SECONDS,
    InMemoryPoolCache,
    MongoPoolCache,
    PoolCache,
    RedisPoolCache,
    create_pool_cache,
)
from .google_trends import GoogleTrendsCollector
from .instagram import InstagramTrendCollector
from .material_store import (
    MATERIAL_POOL_MAX_SIZE,
    MATERIAL_TARGET_POOL_SIZE,
    RELEVANCE_PROMPT_VERSION,
    InMemoryMaterialKeywordStore,
    MaterialKeyword,
    MaterialKeywordStore,
    MongoMaterialKeywordStore,
    material_key,
)
from .naver import NaverTrendCollector
from .normalizer import normalize_keyword, repair_spacing
from .youtube import YouTubeTrendCollector

__all__ = [
    "POOL_TTL_SECONDS",
    "AggregateTrendProvider",
    "CollectedKeyword",
    "InMemoryPoolCache",
    "MongoPoolCache",
    "PoolCache",
    "RedisPoolCache",
    "create_pool_cache",
    "GoogleTrendsCollector",
    "InstagramTrendCollector",
    "NaverTrendCollector",
    "TrendCollector",
    "YouTubeTrendCollector",
    "MATERIAL_POOL_MAX_SIZE",
    "MATERIAL_TARGET_POOL_SIZE",
    "RELEVANCE_PROMPT_VERSION",
    "InMemoryMaterialKeywordStore",
    "MaterialKeyword",
    "MaterialKeywordStore",
    "MongoMaterialKeywordStore",
    "material_key",
    "normalize_keyword",
    "repair_spacing",
    "seed_queries",
]
