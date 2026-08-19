"""각 provider의 응답 껍데기에서 쓸 부분만 꺼낸다.
provider마다 출력을 다르게 감싸므로, 그 차이를 여기서 정규화한다.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.shared import SearchSource
from app.shared.reference_url import is_public_reference_url

from .parsing import (
    LiveAdapterError,
    ProviderEmptyResponseError,
    dedupe_sources,
    string_value,
)


@dataclass(frozen=True)
class GeminiUrlContextResult:
    """Interactions API가 돌려준 URL Context 조회 한 건.

    ``url_context_result``는 모델 답변 문구와 별개라, 이 상태를 읽어야 사용자가 준 URL을
    실제로 가져왔는지 확인할 수 있다. 공식 스키마는 status/url만 보장하지만 현재 응답은
    title/snippet도 돌려주므로 둘 다 방어적으로 받는다.
    """

    url: str
    status: str
    requested_url: str = ""
    title: str = ""
    snippet: str = ""


def _as_dict(payload: Any) -> dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _url_identity(value: str) -> tuple[str, str, str, str] | None:
    """출처 중복 판정용 URL. fragment와 끝 slash 차이는 같은 문서로 본다."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        parsed.query,
    )


def _citation_text(text: str, annotation: dict[str, Any]) -> str:
    """citation의 명시 본문 또는 공식 byte offset이 가리키는 UTF-8 구간."""

    explicit = string_value(
        annotation.get("cited_text") or annotation.get("citedText")
    ).strip()
    if explicit:
        return explicit
    start = annotation.get("start_index", annotation.get("startIndex"))
    end = annotation.get("end_index", annotation.get("endIndex"))
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
    ):
        return ""
    encoded = text.encode("utf-8")
    if start < 0 or end <= start or end > len(encoded):
        return ""
    try:
        return encoded[start:end].decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def extract_openai_text(payload: Any) -> str:
    response = _as_dict(payload)
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    chunks: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content_item in item.get("content") or []:
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text") or content_item.get("output_text")
            if isinstance(text, str):
                chunks.append(text)

    if chunks:
        return "\n".join(chunks)
    raise LiveAdapterError("OpenAI response did not contain text output")


def extract_openai_image_base64(payload: Any) -> str:
    response = _as_dict(payload)
    data = response.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and isinstance(first.get("b64_json"), str):
            return first["b64_json"]
    raise LiveAdapterError("OpenAI image response did not contain b64_json")


def extract_anthropic_text(payload: Any) -> str:
    response = _as_dict(payload)
    chunks = [
        item["text"]
        for item in response.get("content") or []
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    if chunks:
        return "\n".join(chunks)
    raise LiveAdapterError("Anthropic response did not contain text output")


def extract_anthropic_tool_input(payload: Any, tool_name: str) -> dict[str, Any] | None:
    response = _as_dict(payload)
    for item in response.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use" and item.get("name") == tool_name:
            tool_input = item.get("input")
            if isinstance(tool_input, dict):
                return tool_input
    return None


def extract_gemini_interaction_text(payload: Any) -> str:
    response = _as_dict(payload)
    for key in ("output_text", "outputText"):
        if isinstance(response.get(key), str):
            return response[key]

    chunks: list[str] = []
    for step in response.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for item in step.get("content") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])

    if chunks:
        return "\n".join(chunks)
    # 설정 오류가 아니라 **그 한 번의 응답**이 비어 있는 것이다 — 다른 모델로 같은 일을
    # 다시 시켜 볼 수 있게 타입으로 구분한다(2026-08-12 사용자 신고).
    raise ProviderEmptyResponseError(
        "Gemini interaction response did not contain text output"
    )


def extract_gemini_interaction_sources(payload: Any) -> list[SearchSource]:
    response = _as_dict(payload)
    found: list[SearchSource] = []
    context_keys: set[tuple[str, str, str, str]] = set()
    context_positions: dict[tuple[str, str, str, str], int] = {}
    attempted_context_keys: set[tuple[str, str, str, str]] = set()

    # 결과 step이 통째로 빠진 실패도 있다. call에 들어간 URL을 먼저 기록해 두면 같은 URL이
    # 일반 검색 citation으로 다시 나타나도 직접 조회 성공으로 잘못 승격되지 않는다.
    for step in response.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "url_context_call":
            continue
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        for url in arguments.get("urls") or []:
            key = _url_identity(url) if isinstance(url, str) else None
            if key is not None:
                attempted_context_keys.add(key)

    # 사용자가 명시한 URL을 실제로 읽은 결과를 일반 검색 인용보다 앞에 둔다. 모델이 최종
    # 문장에 inline citation을 빠뜨려도 url_context_result가 성공했다면 그 URL은 사라지지
    # 않아야 한다. 실패·paywall·unsafe URL은 근거로 올리지 않는다.
    for result in extract_gemini_url_context_results(response):
        for candidate in (result.requested_url, result.url):
            key = _url_identity(candidate)
            if key is not None:
                attempted_context_keys.add(key)
        if result.status != "success":
            continue
        source_url = result.requested_url or result.url
        if not is_public_reference_url(source_url):
            continue
        host = urlsplit(source_url).hostname or ""
        source_position = len(found)
        found.append(
            SearchSource(
                title=result.title or host or "사용자 참고 URL",
                url=source_url,
                snippet=result.snippet,
            )
        )
        for candidate in (result.requested_url, result.url):
            key = _url_identity(candidate)
            if key is not None:
                context_keys.add(key)
                context_positions[key] = source_position

    for step in response.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for item in step.get("content") or []:
            if not isinstance(item, dict):
                continue
            response_text = string_value(item.get("text"))
            for annotation in item.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                if annotation.get("type") != "url_citation":
                    continue
                url = string_value(annotation.get("url")).strip()
                if not is_public_reference_url(url):
                    continue
                url_key = _url_identity(url)
                if url_key in attempted_context_keys and url_key not in context_keys:
                    continue
                if url_key in context_keys:
                    # URL Context 결과 자체에 snippet이 없는 응답도 있다. 같은 문서의 citation이
                    # 준 cited_text를 합쳐, 직접 읽은 페이지 내용이 다음 M3/M4 단계로 전달되게
                    # 한다. 출처 행은 하나만 유지한다.
                    position = context_positions[url_key]
                    base = found[position]
                    cited_text = _citation_text(response_text, annotation)
                    cited_title = string_value(annotation.get("title")).strip()
                    found[position] = base.model_copy(
                        update={
                            "title": cited_title or base.title,
                            "snippet": cited_text or base.snippet,
                        }
                    )
                    continue
                found.append(
                    SearchSource(
                        title=string_value(annotation.get("title"), "Gemini grounded source"),
                        url=url,
                        snippet=_citation_text(response_text, annotation),
                    )
                )

    return dedupe_sources(found)[:10]


def extract_gemini_url_context_results(payload: Any) -> list[GeminiUrlContextResult]:
    """Interactions API의 URL별 조회 상태를 안전하게 정규화한다.

    API 문서의 정식 상태는 success/error/paywall/unsafe다. 일부 응답 예시는 상태 없이
    title/url/snippet만 싣기 때문에, 그 형식은 step 전체가 오류가 아닐 때 success로 본다.
    잘못된 URL과 알 수 없는 상태는 성공으로 승격하지 않는다.
    """

    response = _as_dict(payload)
    results: list[GeminiUrlContextResult] = []
    positions: dict[str, int] = {}
    allowed_statuses = {"success", "error", "paywall", "unsafe"}
    requested_by_call: dict[str, list[str]] = {}

    for step in response.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "url_context_call":
            continue
        call_id = string_value(step.get("id")).strip()
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        urls = arguments.get("urls") or []
        if call_id and isinstance(urls, list):
            requested_by_call[call_id] = [url for url in urls if isinstance(url, str)]

    for step in response.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "url_context_result":
            continue
        raw_results = step.get("result") or []
        if isinstance(raw_results, dict):
            raw_results = [raw_results]
        if not isinstance(raw_results, list):
            continue
        step_failed = step.get("is_error") is True or step.get("isError") is True

        call_urls = requested_by_call.get(string_value(step.get("call_id")).strip(), [])
        for index, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue
            url = string_value(
                item.get("url") or item.get("retrieved_url") or item.get("retrievedUrl")
            ).strip()
            if not is_public_reference_url(url):
                continue

            raw_status = string_value(
                item.get("status")
                or item.get("url_retrieval_status")
                or item.get("urlRetrievalStatus")
            ).strip().lower()
            # generateContent 스타일 enum이 섞여 와도 같은 helper가 안전하게 읽는다.
            status = raw_status.removeprefix("url_retrieval_status_")
            if not status:
                status = "error" if step_failed else "success"
            if status not in allowed_statuses:
                status = "error"

            requested_url = ""
            retrieved_key = _url_identity(url)
            for candidate in call_urls:
                if _url_identity(candidate) == retrieved_key:
                    requested_url = candidate.strip()
                    break
            if not requested_url and len(call_urls) == len(raw_results) and index < len(call_urls):
                # 리디렉션이면 결과 URL이 달라진다. 배열 크기가 같을 때만 같은 위치를 믿어,
                # 일부 실패 결과가 생략됐을 때 다음 URL로 잘못 밀리는 일을 막는다.
                requested_url = call_urls[index].strip()

            normalized = GeminiUrlContextResult(
                url=url,
                status=status,
                requested_url=requested_url or url,
                title=string_value(item.get("title")).strip(),
                snippet=string_value(item.get("snippet")).strip(),
            )
            position_key = normalized.requested_url or url
            existing = positions.get(position_key)
            if existing is None:
                positions[position_key] = len(results)
                results.append(normalized)
            elif results[existing].status != "success" and status == "success":
                results[existing] = normalized

    return results


def extract_gemini_text(payload: Any) -> str:
    """``generateContent`` 응답의 본문 텍스트.

    수집이 쓰는 ``interactions``와 응답 모양이 다르다(그쪽은
    ``extract_gemini_interaction_text``). 여기서는 첫 후보의 parts에 담긴 text를 잇는다.

    responseSchema를 걸어 부르면 그 text가 곧 JSON 문자열이다.
    """
    response = _as_dict(payload)
    chunks: list[str] = []
    for candidate in response.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        if chunks:
            break

    if chunks:
        return "".join(chunks)
    raise LiveAdapterError("Gemini generateContent 응답에 텍스트가 없습니다")
