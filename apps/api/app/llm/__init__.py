"""LLM provider adapters.

큐도 워커 프로세스도 아니다. API가 같은 프로세스 안에서 import해 쓴다.
"""

from .contracts import (
    DraftGenerator,
    ExcludedAngle,
    PostImageGenerationInput,
    PhotoSearch,
    PostImageGenerator,
    TitleEvaluationInput,
    TitleJudgment,
    TopicEvaluator,
    TopicGenerationInput,
    TopicGenerator,
    TopicRecommendationResult,
    FeatureBrief,
    SiteReadInput,
    SiteReader,
    TrendFetchInput,
    TrendFetchResult,
    TrendProvider,
    WebSearchAnalyzer,
)
from .factory import LlmProviders, RoleStatus, create_llm_providers, describe_llm_status
from .parsing import (
    LiveAdapterError,
    ProviderContextExceededError,
    ProviderOverloadedError,
    ProviderEmptyResponseError,
    ProviderRefusedError,
    ProviderTruncatedError,
)
from .schemas import TOPIC_CANDIDATE_COUNT
from .provider_config import (
    LlmConfig,
    LlmConfigError,
    LlmProvider,
    LlmRole,
    RoleConfig,
    read_api_key,
    resolve_llm_config,
)

__all__ = [
    "TOPIC_CANDIDATE_COUNT",
    "DraftGenerator",
    "ExcludedAngle",
    "LiveAdapterError",
    "ProviderContextExceededError",
    "ProviderOverloadedError",
    "ProviderEmptyResponseError",
    "ProviderRefusedError",
    "ProviderTruncatedError",
    "LlmConfig",
    "LlmConfigError",
    "LlmProvider",
    "LlmProviders",
    "LlmRole",
    "PostImageGenerationInput",
    "PhotoSearch",
    "PostImageGenerator",
    "RoleConfig",
    "RoleStatus",
    "TitleEvaluationInput",
    "TitleJudgment",
    "TopicEvaluator",
    "TopicGenerationInput",
    "TopicGenerator",
    "TopicRecommendationResult",
    "FeatureBrief",
    "SiteReadInput",
    "SiteReader",
    "TrendFetchInput",
    "TrendFetchResult",
    "TrendProvider",
    "WebSearchAnalyzer",
    "create_llm_providers",
    "describe_llm_status",
    "read_api_key",
    "resolve_llm_config",
]
