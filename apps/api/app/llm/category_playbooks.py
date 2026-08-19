"""카테고리별 작성 지침.

지금까지 모든 글은 하나의 통합 문체로 쓰였다. 목적(정보 전달·후기·비교)과 아키타입
(설명형·비교형)은 갈렸지만, **독자가 무엇을 궁금해하는가**는 갈리지 않았다. 그래서 책
소개 글이 저자와 출판사를 말하지 않고, 자동차 글이 트림과 연식을 구분하지 않고, 전시
소개가 일정과 관람료 없이 분위기만 적는 결과가 나왔다.

여기 있는 것은 카테고리 하나가 요구하는 **여섯 가지**다.

- 독자의 핵심 궁금증 — 이 글을 검색한 사람이 답을 얻어야 하는 질문.
- 필수 조사 항목 — 확인하지 않으면 글이 성립하지 않는 사실.
- 권장 원고 구조 — 무엇을 어떤 순서로 말하는가.
- 이미지 우선순위 — 이 카테고리에서 무엇을 보여 줘야 하는가.
- 금지 — 이 카테고리에서 특히 자주 나오는 조작·혼동.
- 검증 — 완성 후 반드시 대조할 것.

**메인 카테고리 하나만 구조를 정한다.** 보조 카테고리는 문체·정보·이미지 지침을 보완할
뿐이고, 두 카테고리의 구조를 섞으면 어느 쪽 독자도 답을 얻지 못하는 글이 된다. 그래서
아래 블록 생성 함수도 메인의 구조만 싣고 보조는 보완 항목만 싣는다.

카테고리를 판정하지 못한 글(primary_category가 빈 문자열)에서는 이 모듈이 만드는 블록이
전부 빈 문자열이고, 프롬프트는 예전과 똑같아진다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.shared import BLOG_CATEGORIES


@dataclass(frozen=True)
class CategoryPlaybook:
    """카테고리 하나의 작성 지침."""

    category: str
    reader_questions: tuple[str, ...] = ()
    research_items: tuple[str, ...] = ()
    structure: tuple[str, ...] = ()
    image_priority: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    # 이 카테고리에서 비어 있으면 안 되는 소재 정체 필드. 검증(category_fit)이 본다.
    # 값은 ContentEntityProfile의 camelCase 필드명이다.
    required_facts: tuple[str, ...] = field(default=())


def _p(category: str, **kwargs) -> CategoryPlaybook:
    return CategoryPlaybook(category=category, **kwargs)


PLAYBOOKS: tuple[CategoryPlaybook, ...] = (
    _p(
        "문학·책",
        reader_questions=(
            "어떤 책인가",
            "무슨 내용을 다루는가",
            "누구에게 맞는가",
            "난이도와 분위기는 어떤가",
        ),
        research_items=(
            "정확한 책 제목",
            "저자",
            "출판사",
            "장르",
            "출간 정보",
            "핵심 주제",
            "공식 책 소개",
            "목차 또는 작품 구성",
            "시리즈 여부",
        ),
        structure=(
            "책과 저자 소개",
            "책이 다루는 핵심 주제",
            "주요 내용과 구성",
            "독자가 얻을 수 있는 점",
            "추천 독자",
            "읽기 전 참고할 점",
        ),
        image_priority=("실제 표지 이미지", "작가 공식 이미지", "출판사 제공 소개 이미지"),
        forbidden=(
            "결말과 반전을 경고 없이 공개",
            "존재하지 않는 문장을 책에서 인용",
            "본문을 길게 인용",
        ),
        verification=("제목·저자·출판사 일치", "스포일러 구분", "실제 표지와 다른 책 이미지 금지"),
        required_facts=("canonicalName", "relatedPeople"),
    ),
    _p(
        "영화",
        reader_questions=(
            "어떤 영화인가",
            "줄거리와 장르는 무엇인가",
            "누가 출연하는가",
            "스포일러가 있는가",
        ),
        research_items=(
            "정식 제목",
            "감독",
            "주요 배우",
            "장르",
            "개봉 또는 공개일",
            "상영 시간",
            "공식 줄거리",
            "관람 등급",
            "공개 플랫폼",
        ),
        structure=(
            "영화 기본 정보",
            "스포일러 없는 줄거리",
            "주요 인물과 관계",
            "연출·주제·장르적 특징",
            "시청 포인트",
            "추천 시청자",
        ),
        image_priority=("공식 포스터", "공식 스틸컷", "공식 예고편 썸네일"),
        forbidden=(
            "일반 배우 생성 이미지",
            "다른 영화의 스틸컷",
            "흥행·평가를 근거 없이 단정",
        ),
        verification=("배우와 배역 관계", "동명의 드라마와 구분", "스포일러 여부"),
        required_facts=("canonicalName", "relatedPeople"),
    ),
    _p(
        "미술·디자인",
        reader_questions=("어떤 스타일인가", "무엇이 특징인가", "다른 작품과 어떤 차이가 있는가"),
        research_items=(
            "작가 또는 디자이너",
            "작품명",
            "제작 시기",
            "재료 또는 매체",
            "디자인 목적",
            "주요 형태·색·구성",
            "공식 설명",
        ),
        structure=(
            "작품 또는 디자인 소개",
            "형태와 시각 요소",
            "콘셉트와 기능",
            "제작 배경",
            "활용 또는 감상 포인트",
            "비슷한 계열과 차이",
        ),
        image_priority=("실제 작품 이미지", "공식 포트폴리오", "전시 제공 이미지"),
        forbidden=(
            "추상적인 감탄만 반복",
            "작품을 보지 않고 세부 요소를 지어내는 것",
            "다른 작가의 작품 이미지 혼용",
        ),
        verification=("작가와 작품 일치", "작품명과 이미지 일치", "시각 설명이 실제 이미지와 일치"),
        required_facts=("canonicalName", "relatedPeople"),
    ),
    _p(
        "공연·전시",
        reader_questions=(
            "언제 어디서 열리는가",
            "무엇을 볼 수 있는가",
            "관람료와 시간이 어떻게 되는가",
        ),
        research_items=(
            "공식 행사명",
            "장소",
            "일정",
            "관람 시간",
            "가격",
            "출연자 또는 참여 작가",
            "전시 구성",
            "예매 방법",
            "촬영 가능 여부",
        ),
        structure=(
            "행사 개요",
            "핵심 테마와 구성",
            "주요 볼거리",
            "관람 정보",
            "추천 대상",
            "방문 전 확인사항",
        ),
        image_priority=("공식 포스터", "공식 전시 전경", "좌석·전시 구역 안내도"),
        forbidden=(
            "과거 행사 사진을 현재 행사처럼 사용",
            "일정과 가격 추정",
        ),
        verification=("개최 연도와 회차 구분", "장소·일정 최신성", "실제 해당 행사 이미지인지"),
        required_facts=("canonicalName",),
    ),
    _p(
        "음악",
        reader_questions=("어떤 곡인가", "누가 불렀는가", "어떤 분위기와 장르인가"),
        research_items=(
            "공식 곡명",
            "아티스트",
            "앨범명",
            "공개일",
            "작사·작곡 정보",
            "음악 장르",
            "공식 소개",
            "뮤직비디오 여부",
        ),
        structure=(
            "곡과 아티스트 소개",
            "곡의 장르와 분위기",
            "앨범 또는 활동 맥락",
            "메시지와 감상 포인트",
            "추천 청취 상황",
            "관련 곡 또는 활동",
        ),
        image_priority=("공식 앨범 커버", "공식 콘셉트 사진", "공식 뮤직비디오 썸네일"),
        forbidden=(
            "가사 전체 또는 긴 구절 인용",
            "확인되지 않은 의미 해석을 정답처럼 단정",
        ),
        verification=("동명의 노래·아티스트 구분", "앨범과 싱글 구분", "공식 커버 이미지 사용"),
        required_facts=("canonicalName", "relatedPeople"),
    ),
    _p(
        "드라마",
        reader_questions=(
            "어떤 이야기인가",
            "누가 출연하는가",
            "어디서 볼 수 있는가",
            "주요 인물 관계는 무엇인가",
        ),
        research_items=(
            "정식 작품명",
            "방송사 또는 플랫폼",
            "공개일",
            "회차 수",
            "감독·작가",
            "주요 배우와 배역",
            "공식 줄거리",
            "방영 상태",
        ),
        structure=(
            "드라마 기본 정보",
            "줄거리와 배경",
            "주요 인물 관계",
            "작품의 장르적 특징",
            "시청 포인트",
            "추천 대상",
        ),
        image_priority=("공식 포스터", "공식 스틸컷", "방송사·OTT 제공 이미지"),
        forbidden=(
            "배우와 배역 혼동",
            "존재하지 않는 장면을 만들어내는 것",
            "결말 스포일러 무단 공개",
        ),
        verification=("회차와 시즌 구분", "등장인물 관계 정확성", "공식 이미지 사용 여부"),
        required_facts=("canonicalName", "platform", "relatedPeople"),
    ),
    _p(
        "스타·연예인",
        reader_questions=(
            "누구인가",
            "어떤 활동을 하는가",
            "현재 어떤 소속이나 작품과 관련 있는가",
            "왜 최근 주목받는가",
        ),
        research_items=(
            "정식 활동명",
            "소속사",
            "그룹 또는 팀",
            "주요 활동",
            "대표 작품",
            "현재 활동 상태",
            "최근 공식 활동",
        ),
        structure=(
            "인물 기본 소개",
            "소속과 활동 분야",
            "주요 작품 또는 경력",
            "현재 주목받는 이유",
            "관련 인물·그룹·프로그램",
            "앞으로 확인할 활동",
        ),
        image_priority=("소속사 공식 프로필", "공식 활동 사진", "방송사·언론 보도 사진"),
        forbidden=(
            "생성형 닮은 사람 이미지",
            "사생활과 연애 추정",
            "탈퇴·불화·건강 문제 추정",
            "팬 커뮤니티 루머를 사실처럼 쓰는 것",
        ),
        verification=("현재 소속과 활동 상태", "이름과 그룹 관계", "인물 사진 일치 여부"),
        required_facts=("canonicalName",),
    ),
    _p(
        "만화·애니",
        reader_questions=(
            "어떤 작품인가",
            "세계관과 장르는 무엇인가",
            "주요 캐릭터는 누구인가",
            "입문하기 좋은가",
        ),
        research_items=(
            "작품명",
            "원작자",
            "제작사",
            "연재 또는 방영 정보",
            "주요 캐릭터",
            "세계관",
            "시즌과 시리즈 순서",
        ),
        structure=(
            "작품 소개",
            "세계관과 기본 설정",
            "주요 캐릭터",
            "작품의 재미 요소",
            "입문 순서",
            "추천 독자·시청자",
        ),
        image_priority=("공식 포스터", "공식 캐릭터 이미지", "출판사·제작사 이미지"),
        forbidden=(
            "다른 시즌 캐릭터 혼용",
            "비공식 팬아트를 공식 이미지처럼 사용",
            "스포일러 무단 포함",
        ),
        verification=("원작과 애니메이션 설정 구분", "시즌·극장판 구분", "이미지 출처와 작품 일치"),
        required_facts=("canonicalName", "relatedPeople"),
    ),
    _p(
        "방송",
        reader_questions=(
            "어떤 프로그램인가",
            "누가 출연하는가",
            "매 회차 무엇을 하는가",
            "어디서 볼 수 있는가",
        ),
        research_items=(
            "정식 프로그램명",
            "콘텐츠 종류",
            "방송사 또는 플랫폼",
            "공식 채널",
            "주요 출연자",
            "핵심 포맷",
            "반복되는 주요 활동",
            "보조 활동",
            "공개 주기 또는 회차 정보",
        ),
        structure=(
            "프로그램 기본 정보",
            "주요 출연자와 역할",
            "핵심 포맷",
            "회차별로 볼 수 있는 내용",
            "화제 포인트",
            "추천 시청자",
        ),
        image_priority=(
            "해당 프로그램 공식 영상 썸네일",
            "프로그램 공식 이미지",
            "해당 회차 공식 이미지",
        ),
        forbidden=(
            "일반 장소·일반인 생성 이미지로 프로그램을 대체하는 것",
            "대본 여부 등 확인되지 않은 제작 방식 단정",
            "부수 장면(식사·이동) 과대 강조",
        ),
        verification=(
            "프로그램명과 출연자 관계",
            "플랫폼과 공식 채널",
            "핵심 포맷 반영",
            "실제 공식 썸네일 사용 여부",
        ),
        required_facts=("canonicalName", "platform", "relatedPeople"),
    ),
    _p(
        "일상·생각",
        reader_questions=("공감할 수 있는 관점인가", "내 상황을 돌아볼 계기가 되는가"),
        research_items=("글의 중심 생각", "상황", "갈등 또는 질문", "전달할 메시지"),
        structure=(
            "일상적 상황 제시",
            "그 상황에서 생긴 생각",
            "관점의 확장",
            "독자와 연결되는 지점",
            "정리된 마무리",
        ),
        image_priority=("주제와 직접 연결되는 생활 장면", "감정을 과장하지 않은 분위기 이미지"),
        forbidden=("감정 과장", "의미 없는 감성 문장 반복"),
        verification=("하나의 중심 메시지 유지", "사실 정보와 개인 생각 구분"),
    ),
    _p(
        "육아·결혼",
        reader_questions=(
            "무엇을 준비해야 하는가",
            "비용과 절차는 어떠한가",
            "어떤 실수를 피해야 하는가",
        ),
        research_items=("대상 연령 또는 단계", "준비 항목", "비용 또는 일정", "안전 사항", "제도·지원 최신 정보"),
        structure=("상황 설명", "필요한 준비", "단계별 진행", "선택 기준", "주의사항", "체크리스트"),
        image_priority=("준비물 실제 이미지", "순서형 체크리스트", "일정표"),
        forbidden=("특정 양육법을 절대적 정답으로 단정", "의료적 조언 단정"),
        verification=("연령과 상황에 맞는 정보", "안전 관련 경고", "제도 정보 최신성"),
    ),
    _p(
        "반려동물",
        reader_questions=(
            "왜 이런 행동을 하는가",
            "어떻게 관리해야 하는가",
            "병원에 가야 하는 상황인가",
        ),
        research_items=("동물 종류", "연령", "행동 또는 건강 상황", "위험 신호", "전문가 상담 필요 여부"),
        structure=(
            "상황 또는 행동 설명",
            "가능한 일반적 원인",
            "관리 방법",
            "피해야 할 행동",
            "전문가 상담이 필요한 경우",
            "생활 팁",
        ),
        image_priority=("실제 품종·동물 사진", "관리 방법 도식", "위험 음식·식물 목록"),
        forbidden=("수의학적 진단", "특정 약물 추천"),
        verification=("동물 종류에 맞는 정보", "독성·안전 정보", "의료적 한계 표시"),
    ),
    _p(
        "좋은글·이미지",
        reader_questions=("짧고 분명한 메시지인가", "이미지와 문구가 어울리는가"),
        research_items=("핵심 메시지", "인용 출처"),
        structure=("핵심 메시지", "짧은 의미 확장", "독자와 연결되는 마무리"),
        image_priority=("문구의 분위기와 일치하는 이미지", "텍스트 가독성을 고려한 배경"),
        forbidden=("출처 불명 명언을 유명인의 말로 표기", "추상적인 문장만 반복", "이미지와 문구 불일치"),
        verification=("인용 출처", "문구 길이", "글자 대비와 가독성"),
    ),
    _p(
        "패션·미용",
        reader_questions=(
            "어떤 제품 또는 스타일인가",
            "누구에게 어울리는가",
            "어떻게 활용하는가",
            "주의점은 무엇인가",
        ),
        research_items=(
            "브랜드와 제품명",
            "색상·소재·용량",
            "제품 유형",
            "공식 특징",
            "사용 방법",
            "피부·체형·상황별 고려사항",
        ),
        structure=(
            "제품 또는 스타일 소개",
            "핵심 특징",
            "활용 방법",
            "어울리는 대상",
            "주의사항",
            "비교 또는 선택 기준",
        ),
        image_priority=("실제 상품 공식 이미지", "실제 색상과 패키지", "착용·사용 예시"),
        forbidden=(
            "다른 색상·구형 패키지 혼용",
            "피부 효능 과장",
            "체형 비하",
        ),
        verification=("브랜드·제품·색상 일치", "실제 상품 이미지", "효능 표현 근거"),
        required_facts=("canonicalName", "brand"),
    ),
    _p(
        "인테리어·DIY",
        reader_questions=(
            "어떻게 만들거나 배치하는가",
            "어떤 준비물이 필요한가",
            "비용과 난이도는 어떤가",
        ),
        research_items=("공간 크기", "작업 목적", "재료", "도구", "작업 순서", "비용 범위", "안전 요소"),
        structure=(
            "작업 목표",
            "준비물",
            "단계별 방법",
            "배치·색상·재료 팁",
            "흔한 실수",
            "안전과 유지관리",
        ),
        image_priority=("완성 예시", "단계별 작업 이미지", "평면 배치도", "전후 비교"),
        forbidden=(
            "구조상 불가능한 배치",
            "위험한 전기·공구 작업을 간단하게 묘사",
        ),
        verification=("순서와 재료 일치", "치수 단위", "안전 경고"),
    ),
    _p(
        "요리·레시피",
        reader_questions=(
            "어떤 재료가 필요한가",
            "어떤 순서로 만드는가",
            "얼마나 조리하는가",
            "실패하지 않는 방법은 무엇인가",
        ),
        research_items=("인분", "재료와 계량", "전처리", "조리 시간", "불 세기 또는 온도", "대체 재료", "보관 방법"),
        structure=(
            "요리 소개",
            "재료 목록",
            "사전 준비",
            "단계별 조리",
            "맛과 식감을 살리는 팁",
            "보관과 응용",
        ),
        image_priority=("실제 완성 음식", "재료 준비", "핵심 조리 단계", "조리 순서 다이어그램"),
        forbidden=("계량과 순서 누락", "안전하지 않은 조리법"),
        verification=("재료와 본문 단계 일치", "시간·온도·단위", "최종 이미지와 요리 일치"),
    ),
    _p(
        "상품리뷰",
        reader_questions=(
            "어떤 상품인가",
            "무엇이 달라졌는가",
            "가격과 구성은 무엇인가",
            "누구에게 적합한가",
        ),
        research_items=(
            "브랜드",
            "정식 상품명",
            "정확한 모델 또는 버전",
            "출시일",
            "가격과 구성",
            "공식 특징",
            "사양 또는 원재료",
            "기존 제품과 차이",
            "판매 채널",
        ),
        structure=(
            "상품 기본 정보",
            "출시 배경 또는 제품 위치",
            "핵심 특징",
            "기존 제품과 차이",
            "장점과 확인할 점",
            "추천 대상",
            "구매 전 체크 사항",
        ),
        image_priority=(
            "해당 상품의 공식 이미지",
            "공식 앱·SNS·보도자료 제공 이미지",
            "제품 단품 또는 구성 이미지",
        ),
        forbidden=(
            "다른 모델의 사양 혼용",
            "같은 종류의 일반 상품 이미지로 대체",
            "출시 전 루머를 확정 정보처럼 쓰는 것",
        ),
        verification=(
            "제목·본문·이미지가 모두 같은 상품인가",
            "브랜드와 상품명이 정확한가",
            "가격과 구성 최신성",
            "실제 이미지가 있는데 생성 이미지를 쓰지 않았는가",
        ),
        required_facts=("canonicalName", "brand"),
    ),
    _p(
        "원예·재배",
        reader_questions=(
            "어떤 환경에서 자라는가",
            "물과 빛은 얼마나 필요한가",
            "언제 심고 수확하는가",
            "병해충은 어떻게 관리하는가",
        ),
        research_items=("식물 종류", "생육 환경", "계절", "빛", "물주기", "토양", "온도", "병해충", "반려동물 독성 여부"),
        structure=(
            "식물 소개",
            "적합한 환경",
            "심기와 분갈이",
            "물·빛·온도 관리",
            "문제 증상과 대응",
            "계절별 관리",
        ),
        image_priority=("실제 식물 사진", "성장 단계", "잎·뿌리·병해충 비교", "관리 일정표"),
        forbidden=("모든 환경에서 동일한 성장 보장", "다른 식물 이미지 혼용", "농약을 안전 설명 없이 권장"),
        verification=("식물 종 일치", "계절과 환경 조건", "독성·안전 정보"),
        required_facts=("canonicalName",),
    ),
    _p(
        "게임",
        reader_questions=(
            "어떤 게임인가",
            "핵심 플레이는 무엇인가",
            "초보자가 어떻게 시작하는가",
            "현재 업데이트에서 무엇이 달라졌는가",
        ),
        research_items=(
            "정식 게임명",
            "개발사와 유통사",
            "플랫폼",
            "장르",
            "출시일",
            "핵심 시스템",
            "공식 업데이트",
            "게임 모드",
            "이용 등급",
        ),
        structure=(
            "게임 기본 정보",
            "핵심 플레이 구조",
            "주요 시스템",
            "초보자 입문 포인트",
            "장점과 진입 장벽",
            "최신 변화 또는 참고사항",
        ),
        image_priority=("공식 게임 스크린샷", "공식 캐릭터·키아트", "공식 트레일러 썸네일"),
        forbidden=(
            "다른 게임 이미지",
            "오래된 패치 정보를 현재 메타처럼 쓰는 것",
            "확률과 성능을 근거 없이 단정",
        ),
        verification=("동명의 게임·브랜드 구분", "플랫폼·서버·버전", "패치 정보 최신성"),
        required_facts=("canonicalName", "brand"),
    ),
    _p(
        "스포츠",
        reader_questions=("최근 경기 결과와 흐름", "선수·팀의 특징", "기록과 순위", "관전 포인트"),
        research_items=("종목", "팀 또는 선수", "경기 일정", "기록", "소속", "대회 규칙", "시즌 정보"),
        structure=(
            "대상 또는 경기 소개",
            "최근 흐름",
            "핵심 기록과 특징",
            "전술 또는 플레이 포인트",
            "관전 요소",
            "다음 일정 또는 확인사항",
        ),
        image_priority=("리그·구단 공식 사진", "공식 경기 사진", "선수 공식 프로필", "기록표"),
        forbidden=(
            "과거 기록을 현재 기록처럼 사용",
            "부상과 이적 추정",
            "팬심을 사실 판단처럼 쓰는 것",
        ),
        verification=("시즌과 날짜", "소속 팀", "기록 출처", "최신 일정"),
        required_facts=("canonicalName",),
    ),
    _p(
        "사진",
        reader_questions=("어떻게 찍어야 하는가", "어떤 설정이 필요한가", "어떻게 보정하는가"),
        research_items=("촬영 대상", "카메라 또는 스마트폰", "빛의 조건", "화각", "노출", "셔터 속도", "조리개", "보정 목적"),
        structure=(
            "촬영 목표",
            "필요한 장비 또는 설정",
            "구도와 빛 활용",
            "촬영 단계",
            "보정 방법",
            "흔한 실패와 해결",
        ),
        image_priority=("촬영 예시", "구도 가이드", "설정 비교", "보정 전후"),
        forbidden=("장비만 있으면 결과가 보장된다는 설명", "다른 렌즈의 결과를 특정 렌즈 촬영물처럼 표시"),
        verification=("설정값의 현실성", "예시 이미지와 설명 일치", "보정 전후 구분"),
    ),
    _p(
        "자동차",
        reader_questions=(
            "어떤 차량인가",
            "가격과 사양은 무엇인가",
            "기존 모델과 무엇이 다른가",
            "유지비와 활용성은 어떤가",
        ),
        research_items=(
            "제조사",
            "정확한 모델명",
            "연식",
            "트림",
            "파워트레인",
            "가격",
            "주행거리 또는 연비",
            "주요 옵션",
            "크기",
            "출시 정보",
        ),
        structure=(
            "차량 기본 정보",
            "외관과 크기",
            "성능과 주행 관련 사양",
            "실내와 편의 기능",
            "트림·가격 비교",
            "추천 사용자와 주의점",
        ),
        image_priority=("제조사 공식 차량 사진", "해당 연식·트림 이미지", "실내 이미지", "제원 비교표"),
        forbidden=(
            "다른 연식·트림 이미지 혼용",
            "공인 수치와 실주행 수치 혼용",
            "생성형 자동차를 실차처럼 사용",
        ),
        verification=("모델·연식·트림", "제원과 가격 최신성", "실제 차량 이미지"),
        required_facts=("canonicalName", "brand"),
    ),
    _p(
        "취미",
        reader_questions=("어떻게 시작하는가", "어떤 준비물이 필요한가", "비용과 난이도는 어떤가"),
        research_items=("취미 유형", "입문 비용", "필수 장비", "난이도", "활동 장소", "안전 요소", "초보자 실수"),
        structure=(
            "취미 소개",
            "매력과 특징",
            "시작 준비물",
            "입문 방법",
            "비용과 난이도",
            "꾸준히 즐기는 팁",
        ),
        image_priority=("실제 활동 모습", "준비물", "단계별 결과물", "장비 비교표"),
        forbidden=("비용과 난이도 과소평가", "위험 요소 생략"),
        verification=("초보자 관점", "준비물과 과정 일치", "안전 정보"),
    ),
    _p(
        "국내여행",
        reader_questions=(
            "어디를 가야 하는가",
            "이동 동선은 어떠한가",
            "언제 가는 것이 좋은가",
            "비용과 운영 시간은 무엇인가",
        ),
        research_items=(
            "정확한 장소명",
            "위치",
            "교통",
            "운영 시간",
            "입장료",
            "계절 특징",
            "휴무일",
            "예약 여부",
            "주변 명소",
        ),
        structure=(
            "여행지 소개",
            "주요 볼거리",
            "추천 동선",
            "교통과 방문 정보",
            "계절별 팁",
            "주변 일정 연결",
        ),
        image_priority=("실제 해당 장소", "공식 관광 이미지", "실제 지도와 동선"),
        forbidden=(
            "다른 지역 이미지",
            "오래된 운영 정보 단정",
            "과도한 '숨은 명소' 표현",
        ),
        verification=("장소와 지점 일치", "운영 정보 최신성", "실제 위치 이미지"),
        required_facts=("canonicalName",),
    ),
    _p(
        "세계여행",
        reader_questions=(
            "언제 가야 하는가",
            "어떤 준비가 필요한가",
            "비자와 입국 조건은 무엇인가",
            "이동과 비용은 어떠한가",
        ),
        research_items=(
            "국가와 도시",
            "입국 조건",
            "비자",
            "여권 요건",
            "교통",
            "화폐",
            "안전",
            "계절과 날씨",
            "문화적 주의사항",
        ),
        structure=(
            "여행지 개요",
            "핵심 관광 포인트",
            "일정과 이동",
            "입국·통신·결제 준비",
            "계절과 복장",
            "안전과 문화적 주의사항",
        ),
        image_priority=("실제 관광지", "공식 관광청 이미지", "지도와 이동 동선"),
        forbidden=(
            "유사한 해외 도시 이미지로 대체",
            "입국 규정 추정",
            "오래된 비자 정보",
        ),
        verification=("국가·도시·지점", "입국 규정 최신성", "실제 장소 이미지"),
        required_facts=("canonicalName",),
    ),
    _p(
        "맛집",
        reader_questions=(
            "어디에 있는가",
            "어떤 메뉴가 유명한가",
            "가격과 운영 시간은 무엇인가",
            "방문할 만한가",
        ),
        research_items=(
            "정확한 매장명",
            "지점명",
            "위치",
            "운영 시간",
            "대표 메뉴",
            "가격",
            "예약",
            "주차",
            "휴무일",
            "공식 메뉴 이미지",
        ),
        structure=(
            "매장 또는 메뉴 소개",
            "대표 메뉴와 특징",
            "가격과 구성",
            "방문 정보",
            "추천 대상",
            "방문 전 확인사항",
        ),
        image_priority=("실제 매장·메뉴 공식 이미지", "공식 메뉴 구성 이미지"),
        forbidden=(
            "다른 지점의 운영 정보 혼용",
            "일반 음식 생성 이미지로 실제 메뉴 대체",
        ),
        verification=("지점과 메뉴 일치", "운영 시간과 가격", "실제 메뉴 이미지"),
        required_facts=("canonicalName",),
    ),
    _p(
        "IT·컴퓨터",
        reader_questions=(
            "무엇인가",
            "어떻게 작동하는가",
            "어떻게 사용하는가",
            "기존 방식과 무엇이 다른가",
        ),
        research_items=(
            "정확한 제품·서비스명",
            "개발사",
            "버전",
            "운영체제 또는 환경",
            "주요 기능",
            "가격 정책",
            "시스템 요구사항",
            "공식 문서",
            "최근 변경사항",
        ),
        structure=(
            "기술 또는 제품 소개",
            "핵심 기능",
            "작동 원리",
            "사용 방법",
            "장점과 제한",
            "적용 대상과 주의사항",
        ),
        image_priority=("공식 UI 화면", "실제 제품 이미지", "구조도·데이터 흐름도", "기능 비교표"),
        forbidden=(
            "오래된 버전 정보를 현재처럼 쓰는 것",
            "존재하지 않는 기능",
            "추상적인 AI 이미지로 실제 제품 화면 대체",
        ),
        verification=("버전과 날짜", "공식 문서 일치", "화면 이미지와 현재 UI 일치", "기술 용어 정확성"),
        required_facts=("canonicalName", "brand"),
    ),
    _p(
        "사회·정치",
        reader_questions=(
            "어떤 일이 발생했는가",
            "배경은 무엇인가",
            "주요 쟁점은 무엇인가",
            "서로 다른 입장은 무엇인가",
        ),
        research_items=(
            "사건 날짜",
            "관련 기관과 인물",
            "공식 발표",
            "법령과 제도",
            "주요 주장",
            "사실로 확인된 부분",
            "아직 확인되지 않은 부분",
        ),
        structure=(
            "이슈 개요",
            "발생 배경",
            "주요 쟁점",
            "각 입장과 근거",
            "사회적 영향",
            "앞으로 확인할 사항",
        ),
        image_priority=("실제 사건과 관련된 보도 이미지", "공식 자료", "제도 구조도", "통계 그래프", "연표"),
        forbidden=(
            "생성 이미지로 실제 사건 장면을 재현해 사실처럼 표시",
            "편향된 표현",
            "주장과 사실 혼용",
            "출처 없는 수치",
            "악성 루머 확대",
        ),
        verification=("날짜와 맥락", "사실·주장·의견 구분", "반대 관점 누락 여부", "최신 제도"),
    ),
    _p(
        "건강·의학",
        reader_questions=(
            "어떤 증상인가",
            "일반적인 원인은 무엇인가",
            "어떻게 관리하는가",
            "언제 병원을 찾아야 하는가",
        ),
        research_items=(
            "의학적 정의",
            "일반적 증상",
            "위험 신호",
            "일반적 검사와 치료",
            "생활 관리",
            "전문 진료가 필요한 경우",
            "공신력 있는 의료 출처",
        ),
        structure=(
            "주제와 기본 개념",
            "일반적인 증상 또는 원인",
            "일상 관리",
            "피해야 할 행동",
            "의료기관 상담이 필요한 경우",
            "핵심 요약",
        ),
        image_priority=("정확한 의학 일러스트", "인체 구조도", "생활 관리 도표", "증상·위험 신호 체크표"),
        forbidden=(
            "개인 진단",
            "약 처방",
            "치료 효과 보장",
            "민간요법을 검증된 치료처럼 설명",
            "잘못된 인체 생성 이미지",
        ),
        verification=("의료적 한계 고지", "권위 있는 출처", "증상과 질병 단정 방지", "응급 위험 신호 안내"),
    ),
    _p(
        "비즈니스·경제",
        reader_questions=(
            "어떤 변화가 일어나고 있는가",
            "왜 일어나는가",
            "기업과 소비자에게 어떤 영향을 주는가",
            "수치가 무엇을 의미하는가",
        ),
        research_items=(
            "기준 날짜",
            "관련 기업 또는 산업",
            "공식 통계",
            "시장 규모",
            "정책",
            "재무 또는 가격 정보",
            "변화 원인",
            "위험 요소",
        ),
        structure=(
            "주제와 현황",
            "변화의 배경",
            "핵심 수치",
            "산업과 소비자 영향",
            "기회와 위험",
            "앞으로 확인할 지표",
        ),
        image_priority=("공식 통계 그래프", "시장 구조도", "기업 공식 이미지", "비교표", "변화 연표"),
        forbidden=(
            "투자 수익 보장",
            "특정 종목 매수·매도 단정",
            "출처 없는 시장 수치",
            "과거 가격을 현재 가격처럼 사용",
        ),
        verification=("데이터 기준일", "단위와 출처", "사실과 전망 구분", "금융 조언 한계"),
    ),
    _p(
        "어학·외국어",
        reader_questions=(
            "어떤 상황에서 쓰는가",
            "문법적으로 왜 그런가",
            "자연스러운 표현은 무엇인가",
            "어떤 실수를 자주 하는가",
        ),
        research_items=("표현의 의미", "문법", "사용 상황", "격식 수준", "지역별 차이", "유사 표현과 차이"),
        structure=(
            "학습 표현 또는 개념",
            "의미와 문법",
            "실제 사용 상황",
            "자연스러운 예문",
            "자주 하는 실수",
            "연습 방법",
        ),
        image_priority=("상황 대화 카드", "문장 구조도", "표현 비교표"),
        forbidden=("원어민이 쓰지 않는 어색한 예문", "격식·비격식 구분 누락", "단순 직역만 제공"),
        verification=("문법과 철자", "예문 자연스러움", "의미와 상황 일치"),
    ),
    _p(
        "교육·학문",
        reader_questions=(
            "무엇을 배우는가",
            "개념이 어떻게 작동하는가",
            "어떻게 공부해야 하는가",
            "어떤 진로와 연결되는가",
        ),
        research_items=(
            "정확한 개념 또는 전공명",
            "정의",
            "핵심 원리",
            "선수 지식",
            "실제 적용",
            "학습 순서",
            "진로 연결",
        ),
        structure=(
            "개념 또는 전공 소개",
            "핵심 원리",
            "쉬운 예시",
            "실제 활용",
            "학습 방법",
            "관련 분야와 진로",
        ),
        image_priority=("개념 설명 다이어그램", "학습 단계", "전공 구조도", "공식 학교·교육 자료"),
        forbidden=(
            "어려운 용어만 나열",
            "특정 학교의 교육과정을 모든 학교의 공통 과정처럼 쓰는 것",
            "진로와 취업을 보장하는 표현",
        ),
        verification=("개념 정확성", "초심자 이해 가능성", "학교별 차이 표시", "실제 교육 자료와 이미지 일치"),
    ),
)

_BY_CATEGORY = {playbook.category: playbook for playbook in PLAYBOOKS}

# 지침을 아직 쓰지 않은 카테고리가 남아 있으면 배선 실수다. 조용히 빈 지침으로 떨어지는
# 것보다 임포트 시점에 드러나는 편이 낫다.
_MISSING = tuple(name for name in BLOG_CATEGORIES if name not in _BY_CATEGORY)
if _MISSING:  # pragma: no cover - 데이터가 맞으면 실행되지 않는다
    raise RuntimeError(f"카테고리 지침 누락: {', '.join(_MISSING)}")


def playbook_for(category: str | None) -> CategoryPlaybook | None:
    """카테고리 이름으로 지침을 찾는다. 모르는 이름이면 None(지침 블록이 통째로 빠진다)."""
    return _BY_CATEGORY.get((category or "").strip())


def _bullets(title: str, items: tuple[str, ...]) -> list[str]:
    return [title, *(f"- {item}" for item in items)] if items else []


def _numbered(title: str, items: tuple[str, ...]) -> list[str]:
    if not items:
        return []
    return [title, *(f"{index}. {item}" for index, item in enumerate(items, start=1))]


def _entity_categories(entity) -> tuple[CategoryPlaybook | None, CategoryPlaybook | None]:
    if entity is None:
        return None, None
    primary = playbook_for(getattr(entity, "primary_category", ""))
    secondary = playbook_for(getattr(entity, "secondary_category", ""))
    # 같은 카테고리를 두 번 싣지 않는다.
    if secondary is not None and primary is not None and secondary.category == primary.category:
        secondary = None
    return primary, secondary


def category_writing_block(entity) -> str:
    """원고·설계 프롬프트에 싣는 카테고리 지침. 구조는 메인 하나만 싣는다.

    보조 카테고리는 '추가로 답해야 할 질문'과 '추가로 지켜야 할 금지'만 얹는다 —
    권장 구조까지 둘 다 실으면 모델이 두 구조를 이어 붙여 어느 쪽 독자도 답을 얻지
    못하는 글이 나온다.
    """
    primary, secondary = _entity_categories(entity)
    if primary is None:
        return ""

    lines = [
        f"이 글의 카테고리: {primary.category}"
        + (f" (보조: {secondary.category})" if secondary else ""),
        "카테고리마다 독자가 궁금해하는 것이 다르다. 아래는 이 카테고리에서 반드시"
        " 답해야 하는 것들이다.",
    ]
    lines += _bullets("독자의 핵심 궁금증:", primary.reader_questions)
    lines += _bullets(
        "반드시 확인하고 본문에 반영할 항목(확인되지 않은 것은 단정하지 말고 그렇게 밝힌다):",
        primary.research_items,
    )
    lines += _numbered("권장 원고 구조(소제목이 이 순서를 따를 필요는 없지만 내용은 이 흐름이다):", primary.structure)
    lines += _bullets("이 카테고리에서 하면 안 되는 것:", primary.forbidden)

    if secondary is not None:
        extra_questions = tuple(
            q for q in secondary.reader_questions if q not in primary.reader_questions
        )
        extra_forbidden = tuple(f for f in secondary.forbidden if f not in primary.forbidden)
        if extra_questions or extra_forbidden:
            lines.append(
                f"보조 카테고리 '{secondary.category}'에서 추가로 챙길 것"
                " (구조는 위 메인 카테고리를 따른다):"
            )
            lines += [f"- 추가 질문: {q}" for q in extra_questions]
            lines += [f"- 추가 금지: {f}" for f in extra_forbidden]
    return "\n".join(lines)


def category_image_block(entity) -> str:
    """사진 계획 프롬프트에 싣는 카테고리 이미지 지침."""
    primary, secondary = _entity_categories(entity)
    if primary is None:
        return ""
    lines = [f"카테고리 '{primary.category}'의 이미지 규칙:"]
    lines += [f"- 우선순위 {index}: {item}" for index, item in enumerate(primary.image_priority, 1)]
    image_forbidden = tuple(
        item for item in primary.forbidden if "이미지" in item or "생성" in item
    )
    lines += [f"- 금지: {item}" for item in image_forbidden]
    if secondary is not None and secondary.image_priority:
        lines.append(
            f"- 보조 카테고리 '{secondary.category}'도 다룬다면 함께 쓸 수 있는 것:"
            f" {', '.join(secondary.image_priority[:2])}"
        )
    return "\n".join(lines) if len(lines) > 1 else ""


def category_verification_block(entity) -> str:
    """원고 프롬프트 끝에 붙는 자체 점검 목록. 생성 후 검증(content_validation)과 같은
    것을 미리 알려 주는 것이 목적이다 — 다시 쓰게 만드는 것보다 처음부터 맞추는 것이 싸다."""
    primary, _ = _entity_categories(entity)
    if primary is None or not primary.verification:
        return ""
    return "\n".join(
        [
            f"글을 마치기 전 '{primary.category}' 카테고리 자체 점검:",
            *(f"- {item}" for item in primary.verification),
        ]
    )


def category_title_hints(entity) -> list[str]:
    """제목 계획에 싣는 카테고리별 힌트 한 줄. 독자의 첫 질문이 제목에 담겨야 한다."""
    primary, _ = _entity_categories(entity)
    if primary is None or not primary.reader_questions:
        return []
    return [
        f"이 글은 '{primary.category}' 카테고리다. 제목은 이 독자가 검색으로 알고자 하는"
        f" 것을 가리켜야 한다: {', '.join(primary.reader_questions[:3])}."
    ]


def required_facts_for(entity) -> tuple[str, ...]:
    """이 카테고리에서 비어 있으면 안 되는 소재 정체 필드. 검증이 쓴다."""
    primary, _ = _entity_categories(entity)
    return primary.required_facts if primary is not None else ()


def category_names_block() -> str:
    """분류 단계에 보여 줄 카테고리 목록. 모델이 여기서 하나를 고른다."""
    return ", ".join(BLOG_CATEGORIES)
