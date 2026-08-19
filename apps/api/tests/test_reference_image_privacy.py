"""사용자가 올린 사진이 **글로 들어갈 때** 개인정보가 덮이는가.

`test_image_privacy.py`는 '칠하기'를 잰다. 여기서 재는 것은 그 칠하기가 실제 경로에
연결돼 있는가다 — 모델 판정(privateRegions)이 파싱을 지나 `enrich`를 거쳐
`_reference_images`까지 살아 오는지. 이 사슬은 한 칸만 끊겨도 조용히 아무것도 안 덮고,
그러면 번호판이 그대로 발행된다(2026-08-07 신고).
"""

import base64
import io

from PIL import Image

from app.llm.parsing import reference_evidence_profile_from_json
from app.modules.draft.reference_evidence import build_profile, enrich
from app.modules.draft.service import _reference_images
from app.shared import (
    BlogTask,
    BlogTaskInput,
    BlogTaskStatus,
    PrivateRegion,
    ReferenceEvidenceProfile,
    ReferenceImageEvidence,
    ReferenceMaterial,
    ReferenceMaterialType,
)
from app.shared.format import now_iso
from app.shared.image_bytes import to_data_url


def _photo_data_url(width: int = 200, height: int = 200) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (220, 30, 30)).save(buffer, "PNG")
    return to_data_url(buffer.getvalue(), "image/png")


def _task_with_photo(data_url: str) -> BlogTask:
    now = now_iso()
    return BlogTask(
        post_id="post_1",
        user_id="user_1",
        status=BlogTaskStatus.GENERATING,
        version=1,
        created_at=now,
        updated_at=now,
        status_history=[],
        posting_logs=[],
        input=BlogTaskInput(
            topic="야간 드라이브",
            keywords=["야간 드라이브"],
            reference_materials=[
                ReferenceMaterial(
                    type=ReferenceMaterialType.IMAGE, name="주차장 사진", value=data_url
                )
            ],
        ),
    )


def _pixel(data_url: str, xy: tuple[int, int]):
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGB").getpixel(xy)


def _is_black(pixel, tolerance: int = 12) -> bool:
    return all(channel <= tolerance for channel in pixel)


def _evidence(regions: list[PrivateRegion]) -> ReferenceEvidenceProfile:
    return ReferenceEvidenceProfile(
        has_references=True,
        reference_image_roles=[
            ReferenceImageEvidence(
                reference_id="reference-image-1",
                role="CONTEXT_ONLY",
                subject="주차된 자동차",
                private_regions=regions,
                privacy_scanned=True,
            )
        ],
    )


class TestThePlateNeverReachesTheArticle:
    def test_번호판_자리가_덮인_사본이_글에_실린다(self):
        data_url = _photo_data_url()
        task = _task_with_photo(data_url)
        evidence = _evidence(
            [PrivateRegion(x=0.3, y=0.6, width=0.4, height=0.2, kind="번호판")]
        )

        images = _reference_images(task, "밤 도로", evidence)

        assert len(images) == 1
        assert _is_black(_pixel(images[0].data_url, (100, 140))), "번호판 자리가 안 덮였다"

    def test_저장된_원본은_손대지_않는다(self):
        """덮은 것은 글에 싣는 사본이다. 사용자가 올린 자료는 그대로 남아야 한다."""
        data_url = _photo_data_url()
        task = _task_with_photo(data_url)
        evidence = _evidence([PrivateRegion(x=0.3, y=0.6, width=0.4, height=0.2)])

        _reference_images(task, "밤 도로", evidence)

        assert task.input.reference_materials[0].value == data_url

    def test_덮으면_mime도_JPEG으로_바뀐다(self):
        """PNG라고 적힌 채 나가면 받는 쪽이 PNG인 줄 알고 연다."""
        task = _task_with_photo(_photo_data_url())
        evidence = _evidence([PrivateRegion(x=0.3, y=0.6, width=0.4, height=0.2)])

        images = _reference_images(task, "밤 도로", evidence)

        assert images[0].mime_type == "image/jpeg"
        assert images[0].data_url.startswith("data:image/jpeg;base64,")

    def test_개인정보가_없어도_메타데이터_없는_사본만_실린다(self):
        """EXIF·GPS가 원고나 외부 provider로 따라가지 않게 픽셀만 새 파일에 담는다."""
        data_url = _photo_data_url()
        task = _task_with_photo(data_url)

        images = _reference_images(task, "밤 도로", _evidence([]))

        assert images[0].data_url != data_url
        assert _pixel(images[0].data_url, (10, 10)) == _pixel(data_url, (10, 10))

    def test_근거_프로필이_없으면_사진만_생략하고_글은_계속된다(self):
        """검사 실패를 '민감정보 없음'으로 오인해 원본 사진을 싣지 않는다."""
        task = _task_with_photo(_photo_data_url())

        images = _reference_images(task, "밤 도로", None)

        assert images == []

    def test_마스킹_실패는_원본_게시가_아니라_사진_제외다(self, monkeypatch):
        task = _task_with_photo(_photo_data_url())
        evidence = _evidence(
            [PrivateRegion(x=0.3, y=0.6, width=0.4, height=0.2, kind="번호판")]
        )
        monkeypatch.setattr(
            "app.modules.draft.service.mask_data_url", lambda _url, _regions: None
        )

        assert _reference_images(task, "밤 도로", evidence) == []


class TestTheChainFromModelToPixels:
    """모델 응답 → 파싱 → enrich → 그림. 한 칸이라도 끊기면 아무것도 안 덮인다."""

    def test_모델_응답의_좌표가_파싱을_지난다(self):
        profile = reference_evidence_profile_from_json(
            {
                "referenceEvidenceProfile": {
                    "referenceImageRoles": [
                        {
                            "referenceId": "reference-image-1",
                            "role": "CONTEXT_ONLY",
                            "subject": "자동차",
                            "allowedUses": [],
                            "forbiddenInferences": [],
                            "privateRegions": [
                                {
                                    "kind": "번호판",
                                    "x": 0.3,
                                    "y": 0.6,
                                    "width": 0.4,
                                    "height": 0.15,
                                }
                            ],
                        }
                    ]
                }
            }
        )

        regions = profile.reference_image_roles[0].private_regions
        assert len(regions) == 1
        assert regions[0].kind == "번호판"
        assert regions[0].x == 0.3

    def test_읽을_수_없는_상자는_버리되_나머지는_살린다(self):
        """좌표 하나가 이상하다고 원고 생성을 실패시키지 않는다."""
        profile = reference_evidence_profile_from_json(
            {
                "referenceEvidenceProfile": {
                    "referenceImageRoles": [
                        {
                            "referenceId": "reference-image-1",
                            "role": "CONTEXT_ONLY",
                            "subject": "자동차",
                            "allowedUses": [],
                            "forbiddenInferences": [],
                            "privateRegions": [
                                {"kind": "깨짐", "x": "왼쪽", "y": 0.6, "width": 0.4,
                                 "height": 0.15},
                                {"kind": "번호판", "x": 0.3, "y": 0.6, "width": 0.4,
                                 "height": 0.15},
                            ],
                        }
                    ]
                }
            }
        )

        regions = profile.reference_image_roles[0].private_regions
        assert [region.kind for region in regions] == ["번호판"]

    def test_privateRegions가_없는_옛_응답도_그대로_읽힌다(self):
        """저장된 옛 문서·옛 응답이 계속 읽혀야 한다."""
        profile = reference_evidence_profile_from_json(
            {
                "referenceEvidenceProfile": {
                    "referenceImageRoles": [
                        {
                            "referenceId": "reference-image-1",
                            "role": "CONTEXT_ONLY",
                            "subject": "자동차",
                            "allowedUses": [],
                            "forbiddenInferences": [],
                        }
                    ]
                }
            }
        )

        assert profile.reference_image_roles[0].private_regions == []

    def test_enrich가_좌표를_버리지_않는다(self):
        """코드가 만든 뼈대에는 좌표가 없다. 여기서 안 실으면 판정이 통째로 사라진다."""
        materials = [
            ReferenceMaterial(
                type=ReferenceMaterialType.IMAGE, name="주차장", value=_photo_data_url()
            )
        ]
        base = build_profile(materials, [])
        assert base.reference_image_roles[0].private_regions == []

        merged = enrich(base, _evidence([PrivateRegion(x=0.3, y=0.6, width=0.4, height=0.2)]))

        assert len(merged.reference_image_roles[0].private_regions) == 1
