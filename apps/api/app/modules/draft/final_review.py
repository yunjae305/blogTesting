"""최종 검수가 찾은 문제를 완성 원고에 반영한다(M4 4단계).

이 모듈은 **모델을 부르지 않는다**. 검수 모델이 돌려준 지적(quote → replacement, 그리고
빼야 할 이미지 번호)을 실제 원고에 적용하는 순수 함수만 있다. 그래야 "무엇을 어떻게 고쳤나"를
모델 없이 테스트할 수 있다.

핵심은 **원고를 다시 쓰지 않는다**는 것이다. 지적된 문장 자리만 바꾼다. 통째로 다시 쓰면
이미 만든 이미지·구성·SEO 배치를 전부 잃고, 고쳐야 할 곳이 아닌 데까지 달라진다.

FinalPost는 같은 글을 세 벌로 들고 있다 — ``body``(평문), ``html_content``(발행본),
``markdown_content``(편집본). 셋 중 하나만 고치면 화면과 발행물이 다른 말을 하게 되므로,
교정은 반드시 셋 모두에 같이 적용하거나 아예 적용하지 않는다.
"""

import logging
import re

from app.shared import (
    FinalPost,
    FinalReviewIssue,
    FinalReviewReport,
    FinalReviewTarget,
    GeneratedPostImage,
)

logger = logging.getLogger(__name__)


def as_review_report(value: object) -> FinalReviewReport:
    """검수 응답을 보고서 형태로 맞춘다.

    지적 목록만 돌려주는 어댑터(평가 harness·테스트 스텁, 항목별 판정이 생기기 전의 구현)도
    그대로 받는다. 그때는 항목별 판정이 없으므로 비워 두고 — '검사하지 않았다'와 '통과했다'를
    섞지 않는다.
    """
    if isinstance(value, FinalReviewReport):
        return value
    if isinstance(value, list):
        issues = [item for item in value if isinstance(item, FinalReviewIssue)]
        return FinalReviewReport(
            overall_status="revise" if issues else "pass",
            overall_score=70 if issues else 100,
            issues=issues,
        )
    return FinalReviewReport()

# 태그 사이에 끼어드는 것을 건너뛰며 문장을 찾기 위한 조각. html_content에는 문장 한가운데에
# <strong>·<mark>가 들어 있을 수 있어, 평문으로 받은 quote가 그대로는 매칭되지 않는다.
_TAG = r"(?:<[^>]*>)*"


def _html_pattern(quote: str) -> re.Pattern[str]:
    """평문 문장을 HTML 안에서 찾는 정규식.

    글자와 글자 사이에 태그가 몇 개 있어도 통과시킨다. 그래서 ``오늘 <strong>중요한</strong>
    발표``에서도 ``오늘 중요한 발표``를 찾아낸다. 찾은 자리를 평문으로 바꾸므로 그 구간의
    인라인 강조는 사라진다 — 사실을 고치는 일이 강조를 지키는 일보다 앞선다.
    """
    return re.compile(_TAG.join(re.escape(char) for char in quote))


def _apply_to_html(html: str, quote: str, replacement: str) -> tuple[str, bool]:
    """HTML에서 quote를 찾아 바꾼다. 첫 한 곳만 바꾼다 — 같은 문장이 두 번 나오면
    지적된 것이 어느 쪽인지 알 수 없고, 둘 다 바꾸면 지적하지 않은 곳까지 건드린다."""
    if quote in html:
        return html.replace(quote, replacement, 1), True
    pattern = _html_pattern(quote)
    replaced, count = pattern.subn(lambda _: replacement, html, count=1)
    return replaced, count > 0


def apply_text_issue(post: FinalPost, issue: FinalReviewIssue) -> FinalPost | None:
    """본문 지적 하나를 세 벌 모두에 반영한 원고. 한 곳이라도 못 찾으면 None."""
    return apply_sentence_replacement(post, issue.quote, issue.replacement)


def apply_sentence_replacement(
    post: FinalPost, quote: str, replacement: str
) -> FinalPost | None:
    """문장 한 자리를 세 벌 모두에서 바꾼 원고. 한 곳이라도 못 찾으면 None.

    부분 적용을 하지 않는 것이 이 함수의 계약이다. 화면(body)에서는 고쳐졌는데 발행본
    (html_content)에는 옛 문장이 남아 있으면, 사용자는 고쳐진 글을 보면서 안 고쳐진 글을
    발행하게 된다 — 검수를 넣은 이유가 통째로 무너진다.

    검수(4단계)와 문장 다듬기(5단계)가 같이 쓴다. 두 단계는 **무엇을 고칠지 정하는 기준**이
    다를 뿐, 정해진 뒤 원고에 넣는 방법은 하나여야 한다 — 두 벌로 두면 한쪽만 고쳐지는
    버그가 두 배로 생긴다.
    """
    if not quote.strip():
        return None

    body = post.body or ""
    html = post.html_content or ""
    markdown = post.markdown_content or ""

    if quote not in body:
        return None

    new_html, html_ok = _apply_to_html(html, quote, replacement)
    if html and not html_ok:
        return None

    updates: dict[str, object] = {
        "body": _tidy(body.replace(quote, replacement, 1)),
        "html_content": _tidy_html(new_html) if html else html,
    }
    # markdownContent는 없을 수도 있다(옛 문서·폴백 경로). 있을 때만 맞춘다 — 여기서
    # 못 찾았다고 교정을 통째로 포기하면, 정작 발행되는 두 벌은 고칠 수 있는데 안 고친다.
    if markdown:
        if quote in markdown:
            updates["markdown_content"] = _tidy(markdown.replace(quote, replacement, 1))
        else:
            logger.info(
                "문장 교정: markdownContent 사본에서는 문장을 찾지 못해 그 사본만 건너뜁니다"
                " (발행되는 body·htmlContent는 고쳤습니다)"
            )

    return post.model_copy(update=updates)


def _tidy(text: str) -> str:
    """문장을 통째로 뺐을 때 남는 자국을 정리한다. 빈 줄이 세 줄 이상 이어지거나 문장
    사이에 공백이 두 칸 남는 것을 없앤다 — 지운 자리가 눈에 띄면 안 된다."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _tidy_html(html: str) -> str:
    """교정으로 알맹이가 빈 문단을 걷어낸다. ``<p></p>``가 남으면 발행본에 빈 줄이 생긴다."""
    return re.sub(r"<(p|li)>\s*</\1>", "", html)


def remove_images(post: FinalPost, indexes: set[int]) -> tuple[FinalPost, int]:
    """지적된 이미지를 최종본에서 뺀다. (고친 원고, 실제로 뺀 장수).

    2026-08-05 사용자 결정: 본문·자료와 맞지 않는 이미지는 다시 만들지 않고 뺀다. 다시
    만들면 장수는 지켜지지만 같은 이유로 또 어긋난 사진이 나올 수 있고, 시간과 비용은
    확실히 더 든다.

    HTML·마크다운에서도 그 이미지의 자리를 함께 지운다 — images 목록에서만 빼면 발행본에는
    사진이 그대로 남는다.
    """
    images = list(post.images or [])
    if not images or not indexes:
        return post, 0

    kept = [image for index, image in enumerate(images) if index not in indexes]
    dropped = [image for index, image in enumerate(images) if index in indexes]
    if not dropped:
        return post, 0

    html = post.html_content or ""
    markdown = post.markdown_content or ""
    for image in dropped:
        html = _without_image_html(html, image)
        markdown = _without_image_markdown(markdown, image)

    featured = post.featured_image
    if featured is not None and any(featured.data_url == image.data_url for image in dropped):
        # 대표 이미지를 뺐다. 네이버가 집어 가는 자리라 비워 둘 수 없으므로 남은 첫 장이
        # 그 자리를 잇는다. 남은 것이 없으면 대표 이미지 없는 글이 된다(허용된 상태다).
        featured = kept[0] if kept else None

    return (
        post.model_copy(
            update={
                "images": kept or None,
                "featured_image": featured,
                "html_content": _tidy_html(html),
                "markdown_content": markdown,
            }
        ),
        len(dropped),
    )


def _without_image_html(html: str, image: GeneratedPostImage) -> str:
    """그 이미지를 담은 <figure>와 바로 뒤의 캡션 문단까지 지운다.

    <img>만 지우면 빈 <figure>와 '출처: ...' 캡션이 남아, 사진 없는 캡션이 발행된다.
    """
    src = re.escape(image.data_url)
    figure = re.compile(
        rf'<figure\b[^>]*>(?:(?!</figure>).)*?<img\b[^>]*src="{src}"[^>]*>.*?</figure>'
        r'(?:\s*<p class="visual-caption">.*?</p>)?',
        re.DOTALL,
    )
    stripped = figure.sub("", html)
    if stripped != html:
        return stripped
    # <figure>로 감싸지 않은 옛 원고를 위한 폴백.
    return re.sub(rf'<img\b[^>]*src="{src}"[^>]*/?>', "", html)


def _without_image_markdown(markdown: str, image: GeneratedPostImage) -> str:
    if not markdown:
        return markdown
    src = re.escape(image.data_url)
    # ![alt](data:...) 와 바로 아래 붙는 *캡션* 줄을 함께 지운다(image_markdown이 그렇게 쓴다).
    pattern = re.compile(rf"!\[[^\]]*\]\({src}\)(?:\n\*[^\n]*\*)?")
    return _tidy(pattern.sub("", markdown))


def apply_review(
    post: FinalPost, issues: list[FinalReviewIssue]
) -> tuple[FinalPost, int, int, list[FinalReviewIssue], list[FinalReviewTarget]]:
    """critical 지적을 원고에 반영한다.

    반환: (고친 원고, 반영한 교정 수, 뺀 이미지 수, 반영하지 못한 지적, **실제로 손댄 목록**).

    마지막 값이 중요하다. 모델의 제안(issues)과 실제로 반영된 것은 다르다 — 인용한 문장을
    원고에서 못 찾으면 그 제안은 적용되지 않는다. 제안만 기록하면 고쳐지지 않은 것을
    고쳐졌다고 읽게 되므로, 코드가 손댄 것만 따로 모아 돌려준다(화면의 '일부 표현 자동
    수정'이 이 목록에서 나온다).

    minor는 건드리지 않는다 — 어감·취향 차이로 완성된 원고를 고치면 잃는 것이 더 많다.
    반영하지 못한 지적은 버리지 않고 돌려준다: 그 지적이 남아 있으면 다음 회차가 다시
    시도하고, 마지막 회차까지 남으면 기록에 남아 원인을 볼 수 있다.
    """
    critical = [issue for issue in issues if issue.severity == "critical"]
    image_issues = {
        issue.image_index: issue
        for issue in critical
        if issue.kind == "image" and issue.image_index is not None
    }
    removable = _removed_indexes(post, set(image_issues))

    updated, removed = remove_images(post, removable)

    applied = 0
    unapplied: list[FinalReviewIssue] = []
    targets: list[FinalReviewTarget] = []

    for index in sorted(removable):
        targets.append(
            FinalReviewTarget(
                kind="image",
                reference=f"image-{index}",
                action="removed",
                note=image_issues[index].reason,
            )
        )

    for issue in critical:
        if issue.kind == "image":
            # 위에서 한꺼번에 처리했다. 번호가 목록 밖이면 뺀 것이 없으므로 남겨 둔다.
            if issue.image_index not in removable:
                unapplied.append(issue)
            continue
        fixed = apply_text_issue(updated, issue)
        if fixed is None:
            logger.info(
                "최종 검수: 원고에서 문장을 찾지 못해 교정을 건너뜁니다 | %s", issue.quote[:60]
            )
            unapplied.append(issue)
            continue
        updated = fixed
        applied += 1
        targets.append(
            FinalReviewTarget(
                kind="paragraph",
                reference=issue.quote[:40],
                action="removed" if not issue.replacement.strip() else "rewritten",
                note=issue.reason,
            )
        )

    return updated, applied, removed, unapplied, targets


def _removed_indexes(post: FinalPost, requested: set[int]) -> set[int]:
    """실제로 존재해서 뺄 수 있었던 이미지 번호."""
    count = len(post.images or [])
    return {index for index in requested if 0 <= index < count}


#: 판정의 무게. 두 검수가 다른 결론을 냈을 때 **나쁜 쪽을 따른다** — 한쪽이 고칠 것이
#: 있다고 했는데 통과로 적으면, 그 지적을 왜 무시했는지 아무도 설명할 수 없다.
_STATUS_WEIGHT = {"pass": 0, "warning": 1, "revise": 2}


def _issue_key(issue: FinalReviewIssue) -> tuple:
    """같은 지적인가. 두 모델이 같은 자리를 각자 잡아 온 경우를 하나로 묶는다.

    문장 지적은 **고칠 자리(quote)** 가 같으면 같은 지적이다. 고치는 방법이 달라도
    한 자리를 두 번 바꿀 수는 없다 — 먼저 적용된 교정이 두 번째의 quote를 없애 버려,
    남겨 두면 "적용되지 않은 지적"으로 기록될 뿐이다.

    이미지 지적은 quote가 비어 있으므로 **그 이미지 번호**가 자리다.
    """
    if issue.kind == "image":
        return ("image", issue.image_index)
    return ("text", issue.quote.strip())


def merge_review_reports(
    primary: FinalReviewReport, second: FinalReviewReport
) -> FinalReviewReport:
    """두 검수 결과를 하나로 합친다(2026-08-07).

    원고를 쓴 모델이 자기 글을 보면 같은 자리를 같은 이유로 지나친다. 그래서 다른 모델이
    같은 원고를 한 번 더 보고, 두 지적을 **합친다** — 어느 한쪽만 잡은 문제도 고쳐야
    한다는 것이 이 구조의 목적이다.

    합치는 규칙:

    - **지적**: 둘을 잇되 같은 자리(_issue_key)는 하나만 남긴다. 1차가 앞이다 —
      먼저 잡은 쪽의 교정문을 쓴다.
    - **판정·점수**: 나쁜 쪽을 따른다(판정은 무게 순, 점수는 낮은 쪽). 두 모델이 갈렸을
      때 좋은 쪽을 적으면 남은 지적과 앞뒤가 맞지 않는다.
    - **항목별 판정**: 1차의 것을 그대로 둔다. 일곱 항목은 서로 다른 척도가 아니라 같은
      질문이라, 두 벌을 섞으면 어느 모델의 판단인지 알 수 없게 된다. 2차가 찾은 것은
      지적 목록에 그대로 들어 있다.
    """
    seen = {_issue_key(issue) for issue in primary.issues}
    extra = [issue for issue in second.issues if _issue_key(issue) not in seen]

    worse_status = max(
        (primary.overall_status, second.overall_status),
        key=lambda status: _STATUS_WEIGHT.get(status, 0),
    )
    if extra:
        logger.info(
            "2차 품질 검수가 %d건을 더 찾았습니다(1차 %d건)",
            len(extra),
            len(primary.issues),
        )
    return primary.model_copy(
        update={
            "overall_status": worse_status,
            "overall_score": min(primary.overall_score, second.overall_score),
            "issues": [*primary.issues, *extra],
        }
    )
