"""fixture 모드가 돌려주는 가짜 provider 응답.

**이 응답의 숫자는 기준선이 아니다.** fixture 모드의 목적은 두 가지뿐이다:
  1) 실제 API 비용 없이 harness 배선(프롬프트 조립 → 파싱 → 지표 계산)이 도는지 확인
  2) 지표가 실제로 문제를 잡아내는지 확인 — 그래서 본문에 결함을 일부러 심어 두었다
     (도입부 상투구, '또한' 반복, 문단 길이 균일, 결론이 소제목 되풀이).

기준선 수치는 `--mode live-titles` / `--mode live-full`로만 만든다. PDF 2단계가 요구한
"임의의 결과를 만들지 말고 미검증 항목을 구분해 보고한다"를 지키기 위한 구분이다.
"""

from __future__ import annotations

# 일부러 결함을 심은 본문. 각 결함이 어떤 지표를 울리는지 주석으로 남긴다.
FIXTURE_MARKDOWN = """# 제습기 물통 냄새를 잡는 관리 순서

오늘은 제습기 관리에 대해 알아보겠습니다. 장마철이 되면 물통에서 냄새가 올라옵니다.
많은 분들이 물통만 비우고 끝냅니다. 그래서 냄새가 다시 돌아옵니다.

## 물통은 어떻게 관리하나

먼저 물통을 분리해 미온수로 헹굽니다. 또한 바닥에 남은 물기를 마른 천으로 닦아 냅니다.
또한 주 1회는 구연산을 풀어 30분 담가 둡니다. 그러면 냄새의 원인이 줄어듭니다.

## 필터는 어떻게 관리하나

필터는 2주에 한 번 흐르는 물로 씻습니다. 또한 그늘에서 완전히 말려야 합니다.
젖은 채로 끼우면 곰팡이가 생깁니다. 마른 뒤에 끼우면 문제가 없습니다.

## 습도는 어떻게 관리하나

습도는 55~60%가 적당합니다. 그보다 낮추면 전기요금만 올라갑니다.
빨래를 말릴 때만 잠시 45%로 내립니다. 평소에는 60%로 둡니다.

## 정리하면

물통은 어떻게 관리하나, 필터는 어떻게 관리하나, 습도는 어떻게 관리하나.
결론적으로 오늘은 물통부터 헹구면 됩니다. 도움이 되셨기를 바랍니다.
"""

FIXTURE_TITLES = [
    {
        "title": "제습기 물통 냄새를 잡는 관리 순서 정리",
        "titleType": "정보형",
        "hookType": "NONE",
        "hookStrength": "LOW",
    },
    {
        "title": "제습기 물통 냄새, 비우기만 하면 안 되는 이유",
        "titleType": "정보형",
        "hookType": "NONE",
        "hookStrength": "LOW",
    },
    {
        "title": "제습기 관리 이것만 놓치면 냄새가 돌아옵니다",
        "titleType": "정보형",
        "hookType": "CURIOSITY",
        "hookStrength": "LOW",
    },
    {
        "title": "장마철 제습기, 물통과 필터 중 무엇을 먼저 씻나",
        "titleType": "비교형",
        "hookType": "COMPARISON",
        "hookStrength": "MEDIUM",
    },
    {
        "title": "제습기 냄새를 그냥 두면 생기는 손해",
        "titleType": "경고형",
        "hookType": "LOSS_AVERSION",
        "hookStrength": "MEDIUM",
    },
]


def tool_payload(name: str, value: dict) -> dict:
    """Anthropic tool_use 응답 형태. 어댑터의 extract_anthropic_tool_input이 읽는 모양 그대로."""
    return {
        "content": [{"type": "tool_use", "name": name, "input": value}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def responses(case_title: str = "제습기 물통 냄새를 잡는 관리 순서 정리") -> dict[str, dict]:
    """도구 이름 → 응답 본문."""
    return {
        "return_title_candidates": {"topicCandidates": FIXTURE_TITLES},
        "return_title_scores": {
            "titles": [
                {
                    "title": item["title"],
                    "relevance": 82,
                    "trendReflection": 70,
                    "purposeMatch": 78,
                    "audienceInterest": 74,
                    "reason": "소재를 정확히 담았다",
                }
                for item in FIXTURE_TITLES
            ]
        },
        "return_keyword_relevance": {"keywords": []},
        "return_title_plan": {
            "titlePlan": {
                "primaryTitle": case_title,
                "alternativeTitles": ["제습기 물통 냄새를 없애는 순서"],
                "h1": case_title,
                "primaryKeyword": "제습기 관리",
                "titleStrategy": "HOW_TO",
            }
        },
        "return_reference_evidence": {
            "referenceEvidenceProfile": {
                "primaryEntity": "제습기",
                "brand": None,
                "productCategory": "생활가전",
                "confirmedAttributes": [],
                "confirmedUseScenes": [],
                "referenceImageRoles": [],
                "sourceFacts": [],
                "forbiddenClaims": ["직접 사용해 본 후기"],
            }
        },
        "return_editorial_style_plan": {
            "editorialStylePlan": {
                "contentCategory": "LIFE_HOME",
                "editorialArchetype": "STEP_BY_STEP_TUTORIAL",
                "voiceMode": "FRIENDLY_GUIDE",
                "visualDensity": "LOW",
                "emojiLevel": "NONE",
                "decorationLevel": "LOW",
                "articleRhythm": "PROBLEM_FIRST",
                "bodyHighlightStyle": "BOLD_ONLY",
                "thumbnailLayout": "CENTER_TITLE_BOX",
                "thumbnailCopyMode": "SHORT",
                "visualBudget": {
                    "bodyPhotosMax": 2,
                    "renderedVisualsMax": 1,
                    "referenceImagesMax": 0,
                },
                # 편집 지시 11항목. 일부러 두 칸(rhythmProfile·detailFocus)에 형용사만 넣어,
                # 실행할 수 없는 값이 걸러지는지 fixture 모드에서 확인한다.
                "writingDirection": {
                    "voiceDistance": "설명은 3인칭으로 하고, 판단이 필요한 대목에서만 견해를 드러낸다.",
                    "readerRelationship": "매 문단 질문하지 않고 세척 순서를 고를 때만 말을 건다.",
                    "sentenceDensity": "핵심 판단은 한두 문장으로 먼저 쓰고 조건은 뒤 문단에서 설명한다.",
                    "openingMode": "물통을 비워도 냄새가 남는 상황에서 바로 시작한다.",
                    "rhythmProfile": "자연스럽게",
                    "transitionStyle": "부품이 바뀌는 지점에서 무엇이 달라지는지로 넘어간다.",
                    "detailFocus": "읽기 좋게",
                    "firstPersonPolicy": "화자의 시선으로 쓰되, 확인된 사실과 개인적인 판단을 구분해 적는다.",
                    "certaintyPolicy": "제조사가 밝힌 주기는 단정하고, 체감 냄새는 조건과 함께 쓴다.",
                    "closingMode": "다음 세척에서 무엇부터 할지 하나만 남긴다.",
                    "avoidPatterns": ["모든 섹션을 세척 절차로 끝내기", "부품마다 같은 분량으로 쓰기"],
                },
                "rationale": "생활 관리 절차라 단계형 구조가 맞다",
            }
        },
        "return_content_plan": {
            "contentPlan": {
                "targetReader": "습한 집에 사는 1인 가구",
                "readerProblem": "물통을 비워도 냄새가 남는다",
                "readerQuestion": "무엇부터 씻어야 하나",
                "articlePromise": "관리 순서를 한 번에 정리한다",
                "contentAngle": "순서와 주기를 함께 준다",
                "articleType": "HOW_TO",
                "tone": "담백한 해요체",
                "sections": [
                    {
                        "sectionId": "section-1",
                        "heading": "물통은 어떻게 관리하나",
                        "question": "물통을 어떻게 씻나",
                        "purpose": "해결 방법",
                        "keyPoints": ["미온수 헹굼", "구연산 담금"],
                        "evidenceIds": [],
                        "visualType": "NONE",
                        "visualReason": "절차는 문장으로 충분하다",
                        "interpretation": "구연산과 세제 중 무엇을 쓸지는 냄새의 원인에 따라 갈린다",
                        "omitBackground": "제습 원리는 도입에서 이미 말했다",
                        "connection": "냄새의 출발점이 물통이라 여기서 시작한다",
                        "lengthShare": "30~40%",
                        "personaDetail": "물때가 남는 자리를 짚어 준다",
                        "forbiddenClaims": ["구연산이 세균을 몇 % 없앤다"],
                    },
                    {
                        "sectionId": "section-2",
                        "heading": "필터는 어떻게 관리하나",
                        "question": "필터 주기는 어떻게 되나",
                        "purpose": "해결 방법",
                        "keyPoints": ["2주 1회", "완전 건조"],
                        "evidenceIds": [],
                        "visualType": "NONE",
                        "visualReason": "절차는 문장으로 충분하다",
                        "interpretation": "주기는 사용 시간에 따라 달라진다",
                        "omitBackground": "필터 종류별 구조 설명",
                        "connection": "물통을 씻어도 냄새가 남으면 다음은 필터다",
                        "lengthShare": "25~30%",
                        "personaDetail": "덜 마른 필터에서 나는 냄새를 언급한다",
                        "forbiddenClaims": ["직접 2주마다 세척해 본 결과"],
                    },
                    {
                        "sectionId": "section-3",
                        "heading": "습도는 어떻게 관리하나",
                        "question": "몇 %가 적당한가",
                        "purpose": "판단 기준",
                        "keyPoints": ["55~60%", "빨래 시 45%"],
                        "evidenceIds": [],
                        "visualType": "NONE",
                        "visualReason": "수치가 두 개뿐이다",
                        "interpretation": "권장 습도와 실제 체감이 다른 이유를 설명해야 한다",
                        "omitBackground": "습도계 구매 기준",
                        "connection": "부품을 다 씻었다면 남는 것은 설정값이다",
                        "lengthShare": "20~25%",
                        "personaDetail": "장마철 빨래를 널 때의 설정 차이를 짚는다",
                        "forbiddenClaims": ["습도 50%가 모든 집에 맞다"],
                    },
                ],
            }
        },
        "return_seo_keyword_plan": {
            "seoKeywordPlan": {
                "primary": "제습기 관리",
                "secondary": ["물통 냄새", "필터 세척", "적정 습도"],
                "avoid": ["최저가"],
            }
        },
        "return_blog_draft": {
            "finalPost": {
                "title": case_title,
                "markdownContent": FIXTURE_MARKDOWN,
                "hashtags": ["제습기", "제습기관리", "물통냄새", "필터세척", "장마", "습도", "생활팁"],
                "thumbnailCopy": ["제습기 관리", "순서 정리"],
            }
        },
        "return_card_plan": {
            "cards": [
                {
                    "cardId": "card-1",
                    "cardType": "THUMBNAIL",
                    "sectionId": None,
                    "sectionHeading": None,
                    "articleClaim": "장마철이 되면 물통에서 냄새가 올라옵니다.",
                    "visualPurpose": "관리 장면을 보여준다",
                    "scene": {
                        "mainSubject": "a dehumidifier water tank on a bathroom floor",
                        "action": "hands rinsing the tank with water",
                        "setting": "a small bathroom with daylight from a window",
                        "mustInclude": ["water tank"],
                        "supportingProps": ["towel"],
                        "cameraAngle": "eye level",
                        "cameraDistance": "medium",
                        "lighting": "soft daylight",
                        "mustAvoid": ["text", "logo"],
                    },
                    "altText": "제습기 물통을 물로 헹구는 모습",
                    "necessityScore": 88,
                    "usesReferenceImage": False,
                    "referenceId": None,
                    "photoRole": "WORK_PROCESS",
                    "subjectIdentity": "제습기 물통",
                    "generatedOrReused": "GENERATED",
                }
            ]
        },
    }
