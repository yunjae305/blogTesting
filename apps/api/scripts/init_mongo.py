"""API가 기대하는 컬렉션, JSON 스키마 검증기, 인덱스를 생성한다.

    python apps/api/scripts/init_mongo.py

다시 실행해도 안전하다: 이미 있는 컬렉션은 검증기를 제자리에서 갱신한다.

유니크 인덱스는 선택 사항이 아니다: postId 중복과 이메일 중복 거부는 애플리케이션
코드가 아니라 인덱스가 보장한다.

users 컬렉션에 평문 email만 있는 옛 문서가 남아 있으면 먼저
``scripts/migrate_email_encryption.py --apply``를 돌려야 한다 — 여기서 만드는
emailHash 유니크 인덱스가 그 문서들을 전부 null로 보고 충돌시킨다. main()이 시작할 때
확인하고 안내한다.
"""

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure

sys.path.insert(0, str(Path(__file__).parent))
from _env import mongodb_uri  # noqa: E402

URI = mongodb_uri()

VALIDATION_OPTIONS = {"validationLevel": "moderate", "validationAction": "error"}

BLOG_TASK_STATUSES = [
    "INPUT",
    "REFERENCE_PROCESSING",
    "SEARCH_ANALYZING",
    "INTENT_SELECTED",
    "GENERATING",
    "READY_TO_PUBLISH",
    "POSTING",
    "POSTED",
    "POSTING_NEEDS_HUMAN",
    "FAILED",
    "CONTENT_POLICY_VIOLATION",
]

COLLECTIONS: dict[str, dict] = {
    "users": {
        # 이메일은 평문으로 저장하지 않는다. 조회·유니크는 emailHash(HMAC 블라인드 인덱스),
        # 표시·발송은 emailEnc(AES-GCM 암호문)를 복호화해서 한다(auth/repository.py).
        #
        # 그래서 required에 email이 있으면 안 된다 — 새로 만드는 문서에는 그 필드가 아예
        # 없어서, 검증기가 회원가입 insert를 통째로 거부한다. 같은 이유로 유니크 인덱스도
        # email이 아니라 emailHash에 걸어야 한다(email이 없는 문서끼리는 전부 null이라
        # 서로 충돌한다). migrate_email_encryption.py가 만드는 최종 상태와 같은 정의다.
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": [
                    "userId",
                    "passwordHash",
                    "createdAt",
                    "updatedAt",
                    "emailHash",
                    "emailEnc",
                ],
                "additionalProperties": True,
                "properties": {
                    "userId": {"bsonType": "string", "minLength": 1},
                    "emailHash": {"bsonType": "string"},
                    "emailEnc": {"bsonType": "string"},
                    # 마이그레이션 전 옛 평문 문서에만 남아 있다. 새로 쓰지 않는다.
                    "email": {"bsonType": "string"},
                    # 회원가입 표시 이름. 옛 문서에는 없으므로 required가 아니다.
                    "nickname": {"bsonType": "string", "maxLength": 30},
                    "passwordHash": {"bsonType": "string"},
                    "createdAt": {"bsonType": "string"},
                    "updatedAt": {"bsonType": "string"},
                    # 사용자 설정. 예전에는 별도 userSettings 컬렉션이었다 — userId 유니크
                    # 인덱스가 걸린 완전한 1:1이라 나눌 이유가 없었고, 설정을 읽을 때마다
                    # 쿼리가 한 번 더 나갔다. 설정이 없는 사용자도 있으므로 required가
                    # 아니고, 서브도큐먼트에 userId를 다시 두지 않는다(담고 있는 문서가
                    # 이미 그 답이다).
                    "settings": {
                        "bsonType": "object",
                        "required": [
                            "hashtagCount",
                            "defaultPersona",
                            "autoPostingEnabled",
                            "createdAt",
                            "updatedAt",
                        ],
                        "additionalProperties": True,
                        "properties": {
                            "hashtagCount": {
                                "bsonType": "number",
                                "minimum": 1,
                                "maximum": 10,
                            },
                            # 옛 문서에는 없으므로 required가 아니다. 값이 있으면 셋 중 하나다.
                            "articleLength": {"enum": ["short", "medium", "long"]},
                            # 소재/트렌드 결합 방향. 옛 문서에는 없다.
                            "blendMode": {"enum": ["subject", "balanced", "trend"]},
                            "defaultPersona": {"bsonType": "string", "maxLength": 1200},
                            "customPersonaName": {"bsonType": "string", "maxLength": 80},
                            "customPersonaDescription": {
                                "bsonType": "string",
                                "maxLength": 200,
                            },
                            "customPersona": {"bsonType": "string", "maxLength": 1200},
                            "autoPostingEnabled": {"bsonType": "bool"},
                            "createdAt": {"bsonType": "string"},
                            "updatedAt": {"bsonType": "string"},
                        },
                    },
                },
            }
        },
        "indexes": [
            ([("userId", ASCENDING)], {"unique": True, "name": "uniq_userId"}),
            ([("emailHash", ASCENDING)], {"unique": True, "name": "uniq_emailHash"}),
        ],
    },
    "persona": {
        # 기본 페르소나(프리셋). _id = 페르소나 id. 사용자 설정의 defaultPersona가 이 id를
        # 가리킨다. 런타임 시드(services.py)가 없어도 되지만, 스키마 검증을 위해 등록한다.
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["_id", "name", "prompt"],
                "additionalProperties": True,
                "properties": {
                    "_id": {"bsonType": "string", "minLength": 1},
                    "name": {"bsonType": "string"},
                    "description": {"bsonType": "string"},
                    "prompt": {"bsonType": "string"},
                },
            }
        },
        "indexes": [],
    },
    "blogTask": {
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": [
                    "postId",
                    "userId",
                    "status",
                    "version",
                    "createdAt",
                    "updatedAt",
                    "statusHistory",
                    "input",
                    "postingLogs",
                ],
                "additionalProperties": True,
                "properties": {
                    "postId": {"bsonType": "string"},
                    "userId": {"bsonType": "string", "minLength": 1},
                    "status": {"enum": BLOG_TASK_STATUSES},
                    "version": {"bsonType": "number"},
                    "createdAt": {"bsonType": "string"},
                    "updatedAt": {"bsonType": "string"},
                    "statusHistory": {
                        "bsonType": "array",
                        "items": {
                            "bsonType": "object",
                            "required": ["from", "to", "at", "by"],
                            "properties": {
                                "from": {"bsonType": "string"},
                                "to": {"bsonType": "string"},
                                "at": {"bsonType": "string"},
                                "by": {"bsonType": "string"},
                            },
                        },
                    },
                    "postingLogs": {"bsonType": "array"},
                    "input": {
                        "bsonType": "object",
                        "required": ["topic", "keywords", "referenceMaterials"],
                        "properties": {
                            "topic": {"bsonType": "string"},
                            "purpose": {"bsonType": "array", "items": {"bsonType": "string"}},
                            "keywords": {"bsonType": "array", "items": {"bsonType": "string"}},
                            "tone": {"bsonType": "string"},
                            "targetReader": {"bsonType": "string"},
                            "readerAgeRange": {"bsonType": "string"},
                            "readerKnowledgeLevel": {"bsonType": "string"},
                            "referenceMaterials": {
                                "bsonType": "array",
                                "items": {
                                    "bsonType": "object",
                                    "required": ["type", "value"],
                                    "properties": {
                                        "type": {"enum": ["IMAGE", "PDF", "TEXT", "URL"]},
                                        "value": {"bsonType": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "trendSelection": {
                        "bsonType": "object",
                        "required": [
                            "finalTopic",
                            "selectedTrendKeywordIds",
                            "skipped",
                            "selectedAt",
                        ],
                        "properties": {
                            "topicCandidateId": {"bsonType": "string"},
                            "finalTopic": {"bsonType": "string"},
                            "selectedTrendKeywordIds": {
                                "bsonType": "array",
                                "items": {"bsonType": "string"},
                            },
                            "skipped": {"bsonType": "bool"},
                            "selectedAt": {"bsonType": "string"},
                        },
                    },
                    "selectedIntent": {
                        "bsonType": "object",
                        "required": ["intentId", "title", "targetReader", "rationale"],
                        "properties": {
                            "intentId": {"bsonType": "string"},
                            "title": {"bsonType": "string"},
                            "targetReader": {"bsonType": "string"},
                            "rationale": {"bsonType": "string"},
                        },
                    },
                    "draftGenerationResult": {
                        "bsonType": "object",
                        # finalPost는 여기 없다. 문서 맨 위의 finalPost와 **항상 같은 값**
                        # 인데 안에 base64 이미지가 들어 있어, 두 벌을 쓰면 문서가 정확히
                        # 두 배가 된다(실측: 1.11MB 중 0.54MB씩 두 벌). 한 벌만 저장하고
                        # 읽을 때 repository._with_restored_final_post()가 되돌린다.
                        #
                        # required에 남겨 두면 중복을 지우는 순간 검증기가 거부한다 —
                        # 실제로 이관 스크립트가 여기서 막혔다.
                        "required": [
                            "promptVersion",
                            "provider",
                            "model",
                            "generatedAt",
                        ],
                        "properties": {
                            "promptVersion": {"bsonType": "string"},
                            "provider": {"bsonType": "string"},
                            "model": {"bsonType": "string"},
                            "generatedAt": {"bsonType": "string"},
                        },
                    },
                    "finalPost": {
                        "bsonType": "object",
                        "required": ["title", "body", "hashtags", "htmlContent"],
                        "properties": {
                            "title": {"bsonType": "string"},
                            "body": {"bsonType": "string"},
                            "hashtags": {"bsonType": "array", "items": {"bsonType": "string"}},
                            "htmlContent": {"bsonType": "string"},
                            "markdownContent": {"bsonType": "string"},
                        },
                    },
                },
                # 원고의 자리가 최상위 `finalPost` 하나뿐이 된 뒤로, 그 자리가 비면
                # 생성 기록이 아무것도 설명하지 못한다. 실제로 `BlogTask`의 복구 검증기는
                # 그때 `draftGenerationResult`를 통째로 버리므로(shared/blog_task.py),
                # 최종 검수·문장 다듬기·카드 계획 기록이 조용히 사라진다.
                #
                # 그래서 "생성 기록이 있으면 원고도 있다"를 스키마가 지킨다. 소재만 넣은
                # 글(둘 다 없음)과 입력을 고쳐 생성 기록만 지우는 경로(replace_input —
                # finalPost는 남긴다)는 그대로 통과한다.
                "dependencies": {"draftGenerationResult": ["finalPost"]},
            }
        },
        # uniq_user_postId(userId, postId)를 걷어냈다. postId가 이미 전역 유니크라
        # (userId, postId) 조합에 유니크를 한 번 더 거는 것은 아무것도 더 막지 못하고,
        # find_one({userId, postId}) 같은 질의도 uniq_postId가 문서 하나로 좁힌 뒤
        # userId를 비교하면 끝난다. 쓰기마다 갱신할 인덱스만 하나 늘어나 있었다.
        "drop_indexes": ["uniq_user_postId"],
        "indexes": [
            ([("postId", ASCENDING)], {"unique": True, "name": "uniq_postId"}),
            # 내 글 목록: find({userId}).sort(createdAt, -1) 과 정확히 같은 모양.
            (
                [("userId", ASCENDING), ("createdAt", DESCENDING)],
                {"name": "by_user_createdAt"},
            ),
            # 중단된 작업 복구 스위퍼의 상태 조회: find({status: {$in: [...]}}).
            (
                [("status", ASCENDING), ("updatedAt", ASCENDING)],
                {"name": "by_status_updatedAt"},
            ),
        ],
    },
    # 공용 트렌드 키워드 풀(최신순). 키워드 1개 = 문서 1개. 예전에는 런타임(cache.py)에만
    # 맡겨 스키마 등록·인덱스가 없었다 — 여기 등록해 형식이 문서로 남고, 소스별 조회와
    # 다음 순번 발급이 인덱스를 탄다.
    "trend_keywords": {
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["keyword", "source", "at", "score", "seq"],
                "additionalProperties": True,
                "properties": {
                    # 문서 id는 "key<seq>" 순번 문자열. 문자열 _id는 사전순이라
                    # "key9" > "key10"이 되므로, 다음 번호 발급용 숫자 seq를 함께 둔다.
                    "_id": {"bsonType": "string"},
                    "keyword": {"bsonType": "string", "minLength": 1},
                    "source": {"bsonType": "string", "minLength": 1},
                    # 수집 시각(epoch 초). 소스당 200개 상한을 넘으면 오래된 것부터 지운다.
                    "at": {"bsonType": ["double", "int"]},
                    # 소스 내 상대 인기(40~100 정규화). 원시 검색량이 아니다.
                    "score": {"bsonType": ["double", "int"]},
                    "seq": {"bsonType": "int"},
                },
            }
        },
        "indexes": [
            # 소스별 풀 읽기·상한 정리(오래된 것부터)가 모두 이 경로다.
            ([("source", ASCENDING), ("at", ASCENDING)], {"name": "by_source_at"}),
            # 다음 순번 발급: seq 최댓값 하나만 읽는다.
            ([("seq", DESCENDING)], {"name": "by_seq_desc"}),
        ],
    },
    # 소재별 관련 키워드 풀. 최신순이 쓰는 공용 trend_keywords와 일부러 분리했다 —
    # "배틀그라운드 감도 설정"은 그 소재의 글에만 의미가 있고, 공용 풀에 섞이면 아무 관계
    # 없는 사용자의 최신순 패널에 노출된다. 키는 소재(materialKey)이지 사용자가 아니라서,
    # 같은 소재로 쓰는 모든 글이 한 번 검증한 풀을 재사용한다. (app/llm/trends/material_store.py)
    "material_related_keywords": {
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["materialKey", "keyword", "normalizedKeyword", "source"],
                "properties": {
                    # 소재명을 공백·대소문자·기호만 지워 정규화한 값. userId·postId는 넣지
                    # 않는다 — 넣으면 사용자마다 풀이 쪼개져 재사용이라는 목적이 사라진다.
                    "materialKey": {"bsonType": "string"},
                    "keyword": {"bsonType": "string"},
                    "normalizedKeyword": {"bsonType": "string"},
                    "categoryKey": {"bsonType": ["string", "null"]},
                    # 소재와 맺는 관계의 종류. 소재 관련순의 노출 게이트라 점수와 함께 저장한다.
                    "relationType": {
                        "enum": [
                            "DIRECT",
                            "ADJACENT",
                            "CONTEXTUAL",
                            "FORCED",
                            "NONE",
                            "AMBIGUOUS",
                            None,
                        ]
                    },
                    "subjectRelevance": {"bsonType": ["double", "int", "null"]},
                    "purposeRelevance": {"bsonType": ["double", "int", "null"]},
                    "personaRelevance": {"bsonType": ["double", "int", "null"]},
                    "relevance": {"bsonType": ["double", "int", "null"]},
                    "demandScore": {"bsonType": ["double", "int"]},
                    "category": {"bsonType": ["string", "null"]},
                    # 채점 기준 버전. 기준이 바뀌면 옛 점수를 '미채점'으로 보고 다시 채점한다.
                    "promptVersion": {"bsonType": ["int", "null"]},
                    "source": {"bsonType": "string"},
                    "sources": {"bsonType": "array", "items": {"bsonType": "string"}},
                    "collectedAt": {"bsonType": ["double", "int"]},
                    "verifiedAt": {"bsonType": ["double", "int", "null"]},
                    "lastAccessedAt": {"bsonType": ["double", "int"]},
                },
            }
        },
        # 인덱스를 하나로 줄였다. 걷어낸 셋과 그 이유:
        #
        # - by_category_relevance(categoryKey, …): categoryKey는 코드 어디에서도 쓰지
        #   않는다 — 쓰지도 읽지도 않는 필드에 걸린 인덱스였다.
        # - by_material_relevance(materialKey, subjectRelevance, demandScore): load()는
        #   {materialKey}로 찾기만 하고 정렬은 파이썬(_ranked)이 한다. 남은 접두사
        #   materialKey는 아래 유니크 인덱스가 그대로 커버한다. _trim()의 정렬에는 쓰일
        #   수 있지만 소재당 풀이 120건 상한이라 인메모리 정렬로 충분하다.
        # - by_lastAccessedAt: 값을 쓰기만 하고 조회하는 코드가 없다(오래된 풀 정리 작업이
        #   아직 없다). 그 작업이 생기면 그때 다시 만든다.
        #
        # 남긴 하나는 성능이 아니라 정합성 때문이다: 같은 소재에 같은 키워드가 두 번
        # 저장되는 것을 인덱스가 막는다(save()의 upsert 필터와 같은 모양).
        "drop_indexes": [
            "uniq_context_keyword",
            "by_context_relevance",
            "by_material_mode",
            "by_material_relevance",
            "by_category_relevance",
            "by_lastAccessedAt",
        ],
        "indexes": [
            (
                [("materialKey", ASCENDING), ("normalizedKeyword", ASCENDING)],
                {"unique": True, "name": "uniq_material_keyword"},
            ),
        ],
    },
    # 참고자료로 판별한 소재 문맥 프로필(§15). (materialKey, referenceFingerprint)당 하나.
    # 참고자료 원문·개인 메모는 저장하지 않는다 — 판별 결과(개체·카테고리·허용/제외 주제)와
    # 근거의 짧은 요약만.
    "post_images": {
        # 원고 이미지의 실제 바이트(base64 data URL). 글 문서에는 번호와 설명만 남는다.
        #
        # 왜 나눴나: 이미지가 글 문서 안에 있을 때 한 건이 1.6MB까지 커졌고, **그 글을
        # 여는 것이 20초 타임아웃으로 실패했다**(2026-08-06 실측: 가벼운 필드만 읽으면
        # 20ms, 이미지까지 읽으면 실패). 목록은 프로젝션으로 피할 수 있지만 상세·발행은
        # 피할 수 없다.
        #
        # index는 본문 이미지의 순서이고, **-1은 대표 이미지**다(본문 번호와 겹치지 않게).
        #
        # 바이트는 `bytes`(이진) + `mimeType`으로 담는다. base64 글자는 원본보다 33% 크고,
        # 글 하나를 여는 데 오가는 것의 85%가 이미지였다(2026-08-06 실측). 이관 전에
        # 저장된 행은 `dataUrl` 글자를 들고 있어 **둘 다 받는다** — 읽는 쪽
        # (`repository._data_url_of`)이 어느 쪽이든 같은 data URL로 되돌린다.
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["postId", "index"],
                "additionalProperties": True,
                "properties": {
                    "postId": {"bsonType": "string", "minLength": 1},
                    "index": {"bsonType": "int"},
                    "dataUrl": {"bsonType": "string", "minLength": 1},
                    "bytes": {"bsonType": "binData"},
                    "mimeType": {"bsonType": "string", "minLength": 1},
                },
                # 둘 중 하나는 반드시 있어야 한다. 없으면 '이미지가 없는 행'이 되어,
                # 읽을 때 "이미지를 찾지 못했습니다"로 멈추는 글이 조용히 생긴다.
                "anyOf": [
                    {"required": ["dataUrl"]},
                    {"required": ["bytes", "mimeType"]},
                ],
            }
        },
        "indexes": [
            # 글 하나의 이미지를 한 번에 읽는다. 같은 자리에 두 장이 들어가지 않게 유니크다.
            (
                [("postId", ASCENDING), ("index", ASCENDING)],
                {"unique": True, "name": "uniq_post_index"},
            ),
        ],
    },
    "brand_profiles": {
        # 브랜드 자료. 글마다 반복해서 들어가는 회사·서비스 정보를 한 번 적어 두고 쓴다.
        # 이미지는 data URL 문자열로 담는다(원고 이미지와 같은 형식이라 발행 경로가
        # 그대로 받는다). additionalProperties가 True라 항목을 더해도 검증기는 통과한다.
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": [
                    "_id",
                    "brandId",
                    "userId",
                    "name",
                    "createdAt",
                    "updatedAt",
                ],
                "additionalProperties": True,
                "properties": {
                    "_id": {"bsonType": "string", "minLength": 1},
                    "brandId": {"bsonType": "string", "minLength": 1},
                    "userId": {"bsonType": "string", "minLength": 1},
                    "name": {"bsonType": "string", "minLength": 1},
                    "description": {"bsonType": ["string", "null"]},
                    "features": {"bsonType": ["string", "null"]},
                    # 주요 고객은 고른 값이다(대분류 → 유형). 항목 모양은 애플리케이션
                    # 검증(modules/brand/validation.py)이 카탈로그와 대조해 지킨다.
                    "audiences": {"bsonType": "array"},
                    "links": {"bsonType": "array"},
                    # 서술 칸(소개·핵심 기능)에 붙인 텍스트·PDF 문서.
                    # 항목 모양은 애플리케이션 검증이 지킨다.
                    "documents": {"bsonType": "array"},
                    "images": {"bsonType": "array"},
                    # "이런 상황이면 이 기능" 기준표(2026-08-19). 트렌드 소재에 브랜드를
                    # 활용 도구로 얹는 글이 이 표에서 기능 이름을 가져오고, 결합 가능성
                    # 판정(A·B·C)도 이 표로 잰다. 줄 모양은 애플리케이션 검증이 지킨다.
                    "useCases": {"bsonType": "array"},
                    # 글 맨 끝에 붙는 마무리(사실 한 줄 + 링크). 모양은 애플리케이션
                    # 검증(modules/brand/validation.py)이 지킨다.
                    "closing": {"bsonType": ["object", "null"]},
                    # 지운 시각(2026-08-20). 기본 브랜드에만 쓰인다 — 문서를 없애면
                    # 다시 만들어 주는 자리가 되살리므로, 지웠다는 사실만 남긴다.
                    "deletedAt": {"bsonType": ["string", "null"]},
                    "createdAt": {"bsonType": "string"},
                    "updatedAt": {"bsonType": "string"},
                },
            }
        },
        "indexes": [
            # 목록은 항상 "내 자료를 최근 수정순으로"다. 그 한 가지 조회를 위한 인덱스다.
            (
                [("userId", ASCENDING), ("updatedAt", DESCENDING)],
                {"name": "userId_updatedAt"},
            ),
        ],
    },
    "scheduled_batches": {
        # 예약 포스팅 배치. '예약 시작'을 누른 한 번이 문서 하나다.
        # 네이버 아이디·비밀번호·쿠키는 여기 담지 않는다 — DB 밖 로컬 파일에만 있다.
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": [
                    "_id",
                    "batchId",
                    "userId",
                    "platform",
                    "status",
                    "targetCount",
                    "intervalSeconds",
                    "totalCount",
                    "createdAt",
                    "updatedAt",
                ],
                "additionalProperties": True,
                "properties": {
                    "_id": {"bsonType": "string", "minLength": 1},
                    "batchId": {"bsonType": "string", "minLength": 1},
                    "userId": {"bsonType": "string", "minLength": 1},
                    "platform": {"enum": ["naver"]},
                    "topicMode": {"enum": ["multi", "single"]},
                    # 이 배치의 글에 활용할 브랜드(2026-08-19). 옛 배치에는 없다.
                    "brandId": {"bsonType": ["string", "null"]},
                    "status": {
                        "enum": [
                            "READY",
                            "RUNNING",
                            "PAUSE_REQUESTED",
                            "PAUSED",
                            "NEEDS_HUMAN",
                            "STOP_REQUESTED",
                            "STOPPED",
                            "COMPLETED",
                            "FAILED",
                        ]
                    },
                    "targetCount": {"bsonType": "int", "minimum": 1},
                    "intervalSeconds": {"bsonType": "int", "minimum": 15, "maximum": 3600},
                    "totalCount": {"bsonType": "int", "minimum": 0},
                    "createdAt": {"bsonType": "string"},
                    "updatedAt": {"bsonType": "string"},
                },
            }
        },
        "indexes": [
            # 활성 배치 조회(사용자당 하나)와 재시작 복구 조회가 쓰는 인덱스.
            (
                [("userId", ASCENDING), ("status", ASCENDING)],
                {"name": "by_user_status"},
            ),
            ([("status", ASCENDING)], {"name": "by_status"}),
            # 같은 클릭이 두 번 도착해도 배치가 하나만 생기게 한다. clientRequestId가
            # 없는 문서는 인덱스에서 빠지므로(partial) 옛 문서·직접 호출도 막지 않는다.
            (
                [("userId", ASCENDING), ("clientRequestId", ASCENDING)],
                {
                    "unique": True,
                    "name": "uniq_user_client_request",
                    "partialFilterExpression": {"clientRequestId": {"$type": "string"}},
                },
            ),
        ],
    },
    "scheduled_jobs": {
        # 배치 안의 소재 하나. 만들어진 글은 postId로만 가리키고, 원고·이미지·발행 기록은
        # blogTask에 그대로 둔다(복사하면 두 벌이 어긋난다).
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": [
                    "_id",
                    "jobId",
                    "batchId",
                    "userId",
                    "platform",
                    "sequence",
                    "topic",
                    "status",
                    "stage",
                    "createdAt",
                    "updatedAt",
                ],
                "additionalProperties": True,
                "properties": {
                    "_id": {"bsonType": "string", "minLength": 1},
                    "jobId": {"bsonType": "string", "minLength": 1},
                    "batchId": {"bsonType": "string", "minLength": 1},
                    "userId": {"bsonType": "string", "minLength": 1},
                    "platform": {"enum": ["naver"]},
                    "sequence": {"bsonType": "int", "minimum": 0},
                    "variantIndex": {"bsonType": "int", "minimum": 0},
                    "topic": {"bsonType": "string", "minLength": 1},
                    # 이 글에 활용할 브랜드(2026-08-19). 배치에서 물려받지만 값은 작업이
                    # 들고 있는다 — 글을 만드는 것이 작업이다. 옛 작업에는 없다.
                    "brandId": {"bsonType": ["string", "null"]},
                    "status": {
                        "enum": [
                            "WAITING",
                            "RUNNING",
                            "READY_TO_PUBLISH",
                            "PUBLISHING",
                            "COMPLETED",
                            "FAILED",
                            "NEEDS_HUMAN",
                            "CANCELED",
                        ]
                    },
                    "stage": {
                        "enum": [
                            "CREATE_POST",
                            "TREND_RECOMMENDATION",
                            "TITLE_GENERATION",
                            "SEARCH_ANALYSIS",
                            "INTENT_SELECTION",
                            "DRAFT_GENERATION",
                            "NAVER_PUBLISH",
                            "THREADS_PUBLISH",
                            "DONE",
                        ]
                    },
                    "createdAt": {"bsonType": "string"},
                    "updatedAt": {"bsonType": "string"},
                },
            }
        },
        "indexes": [
            # 배치의 작업을 순서대로 읽는다 — 화면의 표와 워커의 실행 순서가 이 정렬이다.
            (
                [("batchId", ASCENDING), ("sequence", ASCENDING)],
                {"name": "by_batch_sequence"},
            ),
            # 작업 단건 재시도는 소유권을 쿼리에 넣어 조회한다.
            ([("userId", ASCENDING)], {"name": "by_user"}),
            # 예약 목록: 내 예약을 발행 시각 순으로 읽는다(list_user_jobs).
            (
                [("userId", ASCENDING), ("publishAt", ASCENDING)],
                {"name": "by_user_publish_at"},
            ),
        ],
    },
}


async def upsert_collection(db: AsyncIOMotorDatabase, name: str, definition: dict) -> None:
    existing = await db.list_collection_names(filter={"name": name})

    if existing:
        # Database.command()는 명령을 하나의 문서로 받는다 — 명령 이름이 첫
        # 번째 키여야 하므로 kwargs만으로는 안 된다.
        await db.command(
            {
                "collMod": name,
                "validator": definition["validator"],
                **VALIDATION_OPTIONS,
            }
        )
        print(f"updated validator: {name}")
    else:
        await db.create_collection(name, validator=definition["validator"], **VALIDATION_OPTIONS)
        print(f"created collection: {name}")

    # 이름이 바뀌었거나 정의가 달라진 옛 인덱스를 먼저 걷어낸다(멱등 — 없으면 조용히 넘어간다).
    for stale in definition.get("drop_indexes", []):
        try:
            await db[name].drop_index(stale)
            print(f"dropped stale index: {name}.{stale}")
        except OperationFailure:
            pass  # 이미 없다.

    for keys, options in definition["indexes"]:
        await db[name].create_index(keys, **options)
        print(f"ensured index: {name}.{options['name']}")


async def fold_user_settings(db) -> None:
    """옛 `userSettings` 컬렉션을 `users.settings` 서브도큐먼트로 접어 넣는다.

    멱등이다: 옮긴 뒤 원본 문서를 지우므로, 두 번 돌리면 컬렉션이 비어 있어 아무 일도
    하지 않는다. 이미 users.settings가 있는 사용자는 덮어쓰지 않는다 — 새 코드로 저장한
    값이 최신이고, 옛 컬렉션에 남아 있는 것은 이관 전 상태다.

    주인 없는 설정 문서(users에 해당 userId가 없음)는 옮기지 않고 남긴다. 조용히 버리면
    무엇이 사라졌는지 알 수 없고, 남겨 두면 다음 실행에서 다시 보고된다.
    """
    if "userSettings" not in await db.list_collection_names():
        return

    moved = orphaned = skipped = 0
    async for document in db["userSettings"].find({}):
        user_id = document.get("userId")
        if not user_id:
            continue
        settings = {
            field: value
            for field, value in document.items()
            if field not in ("_id", "userId")
        }
        result = await db["users"].update_one(
            {"userId": user_id, "settings": {"$exists": False}},
            {"$set": {"settings": settings}},
        )
        if result.modified_count:
            moved += 1
            await db["userSettings"].delete_one({"_id": document["_id"]})
        elif await db["users"].count_documents({"userId": user_id}):
            # 이미 settings가 있다 — 새 코드가 저장한 값이므로 옛 문서를 버린다.
            skipped += 1
            await db["userSettings"].delete_one({"_id": document["_id"]})
        else:
            orphaned += 1

    if moved or skipped:
        print(f"folded userSettings into users: moved={moved} already_present={skipped}")
    if orphaned:
        print(
            f"  주의: users에 없는 userId의 설정 문서 {orphaned}개는 userSettings에 남겨 두었습니다."
        )
    elif await db["userSettings"].count_documents({}) == 0:
        await db["userSettings"].drop()
        print("dropped empty collection: userSettings")


def _redact(uri: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:****@", uri)


async def main() -> int:
    # Atlas는 끝에 DB가 없는 URI를 주는데, 이때 드라이버의 에러("No default
    # database name defined")는 어떻게 해야 하는지 알려주지 않는다.
    if not urlparse(URI).path.lstrip("/").split("?")[0]:
        print(f"URI: {_redact(URI)}", file=sys.stderr)
        print("\n실패: URI에 데이터베이스 이름이 없습니다.", file=sys.stderr)
        print("  → 끝에 /blog_it 을 붙이세요.", file=sys.stderr)
        print(
            "     예: mongodb+srv://user:pw@cluster.mongodb.net/blog_it?appName=Cluster0",
            file=sys.stderr,
        )
        return 1

    client: AsyncIOMotorClient = AsyncIOMotorClient(URI, serverSelectionTimeoutMS=8000)
    try:
        await client.admin.command("ping")
    except Exception as error:
        print(f"could not reach MongoDB at {_redact(URI)}: {error}", file=sys.stderr)
        print("  → python apps/api/scripts/check_mongo.py 로 원인을 확인하세요.", file=sys.stderr)
        client.close()
        return 1

    try:
        db = client.get_default_database()
        print(f"connected: {_redact(URI)}")
        print(f"database: {db.name}")

        # 평문 email만 있는 옛 문서가 둘 이상이면 emailHash 유니크 인덱스가 충돌한다.
        # 드라이버의 DuplicateKeyError는 무엇을 해야 하는지 알려주지 않으므로 먼저 잡는다.
        unmigrated = await db["users"].count_documents({"emailHash": {"$exists": False}})
        if unmigrated:
            print(
                f"\n실패: users에 이메일이 암호화되지 않은 문서가 {unmigrated}개 있습니다.",
                file=sys.stderr,
            )
            print(
                "  → python apps/api/scripts/migrate_email_encryption.py --apply "
                "를 먼저 실행하세요.",
                file=sys.stderr,
            )
            return 1

        # 문맥 한정 기능을 걷어냈다. 남아 있는 contextKey·contextMode·문맥 축 점수를
        # 지운다 — 읽는 코드가 없어졌으므로 남겨 두면 스키마만 헷갈리게 한다. 멱등:
        # 이미 지워진 문서는 매칭되지 않는다.
        stale_context = await db["material_related_keywords"].count_documents(
            {"contextKey": {"$exists": True}}
        )
        if stale_context:
            await db["material_related_keywords"].update_many(
                {},
                {
                    "$unset": {
                        "contextKey": "",
                        "contextMode": "",
                        "entityMatch": "",
                        "categoryMatch": "",
                        "referenceContextMatch": "",
                        "ambiguityRisk": "",
                        "matchedEntity": "",
                    }
                },
            )
            print(f"dropped context fields from {stale_context} material keyword docs")

        # 문맥 프로필 캐시는 더 이상 읽히지 않는다.
        if "material_context_profiles" in await db.list_collection_names():
            await db["material_context_profiles"].drop()
            print("dropped collection: material_context_profiles")

        # 인증 세션은 서명된 무상태 토큰으로 대체됐다(modules/auth/token.py).
        if "auth_sessions" in await db.list_collection_names():
            await db["auth_sessions"].drop()
            print("dropped collection: auth_sessions")

        await fold_user_settings(db)

        for name, definition in COLLECTIONS.items():
            await upsert_collection(db, name, definition)

        print("summary:")
        for name in COLLECTIONS:
            count = await db[name].count_documents({})
            print(f"  {name}: {count} documents")
    finally:
        client.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
