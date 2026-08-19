"""원본 검색 키워드와 '글에 쓸 표현'의 분리(2026-08-03).

사용자가 트렌드 패널에서 고르는 것은 검색어 조합이다("창섭 전과자"). 검색창에는 그렇게
넣지만 문장에서는 그렇게 쓰지 않는다 — 두 고유명사를 띄어쓰기로만 이어 붙인 문자열에
조사를 달면 한국어가 아니다.

여기서 확인하는 것은 세 가지다.

- 조합인 키워드는 그대로 복사하라고 시키지 않는다(제목 규칙).
- 조합을 명사처럼 쓴 자리를 찾아낸다.
- **그러나 정상적인 고유명사는 예전 그대로다** — 이번 변경이 모든 키워드를 쪼개면
  멀쩡한 소재까지 어색해진다.
"""

from app.llm.keyword_naturalization import (
    entity_aliases,
    is_entity_juxtaposition,
    is_single_token_keyword,
    keyword_meaning_covered,
    keyword_tokens,
    primary_raw_keyword,
    raw_keyword_misuse,
)
from app.llm.prompts import blend_rules, keyword_naturalization_rules


class TestSingleTokenKeyword:
    """C. 정상적인 단일 키워드는 예전처럼 그대로 쓴다."""

    def test_a_single_proper_noun_is_not_split(self):
        assert is_single_token_keyword("아이폰17") is True
        assert keyword_tokens("아이폰17") == ["아이폰17"]

    def test_a_search_combination_is_split(self):
        assert is_single_token_keyword("창섭 전과자") is False
        assert keyword_tokens("창섭 전과자") == ["창섭", "전과자"]

    def test_title_rules_keep_a_single_keyword_verbatim(self):
        rules = "\n".join(keyword_naturalization_rules("아이폰17"))
        assert "그대로 넣고" in rules
        # 쪼개라는 지시가 붙지 않는다.
        assert "검색어 조합" not in rules

    def test_blend_rules_no_longer_demand_a_verbatim_combination(self):
        rules = "\n".join(blend_rules("trend", "창섭 전과자", "전과자"))
        assert "반드시 그대로 들어간다" not in rules
        assert "검색어 조합" in rules
        assert "'창섭 전과자는'" in rules

    def test_blend_rules_still_forbid_the_topic_prefix(self):
        """기존 규칙(소재를 앞머리에 콜론으로 나열 금지)은 그대로 남는다."""
        rules = "\n".join(blend_rules("trend", "아이폰17", "아이폰"))
        assert "'아이폰: ...'" in rules


class TestEntityJuxtaposition:
    """조합인지 아닌지는 **확인된 이름들과의 관계**로 판정한다(하드코딩 없음)."""

    NAMES = ["전과자", "이창섭"]

    def test_person_plus_program_is_a_juxtaposition(self):
        assert is_entity_juxtaposition("창섭 전과자", self.NAMES) is True

    def test_a_natural_noun_phrase_is_not(self):
        """'전과자 학과 체험'은 그 자체로 자연스러운 명사구다 — 금지 대상이 아니다."""
        assert is_entity_juxtaposition("전과자 학과 체험", self.NAMES) is False

    def test_a_single_token_is_never_a_juxtaposition(self):
        assert is_entity_juxtaposition("아이폰17", ["아이폰17"]) is False

    def test_without_confirmed_names_there_is_nothing_to_judge(self):
        assert is_entity_juxtaposition("창섭 전과자", []) is False


class TestRawKeywordMisuse:
    def test_particles_attached_to_a_combination_are_caught(self):
        body = "창섭 전과자는 대학을 찾아가는 웹예능입니다."
        assert raw_keyword_misuse(body, "창섭 전과자") == ["창섭 전과자는"]

    def test_spacing_differences_do_not_hide_the_misuse(self):
        assert raw_keyword_misuse("창섭전과자를 봤다", "창섭 전과자") == ["창섭 전과자를"]

    def test_suffix_nouns_are_caught(self):
        found = raw_keyword_misuse("창섭 전과자 편을 처음 본 날", "창섭 전과자")
        assert "창섭 전과자 편" in found

    def test_a_natural_sentence_passes(self):
        body = "유튜브 웹예능 전과자는 이창섭이 여러 대학을 찾아가는 프로그램입니다."
        assert raw_keyword_misuse(body, "창섭 전과자") == []

    def test_single_token_keywords_are_not_checked(self):
        """'아이폰17은 ...'은 정상 문장이다. 이번 변경으로 막히면 안 된다."""
        assert raw_keyword_misuse("아이폰17은 9월에 나옵니다.", "아이폰17") == []


class TestKeywordMeaningCovered:
    """A. 연속 문자열이 아니어도 검색 의도가 담겼으면 통과한다."""

    ALIASES = entity_aliases(["전과자", "이창섭"])

    def test_a_natural_title_is_accepted(self):
        title = "이창섭이 대학 수업을 직접 듣는 유튜브 웹예능 전과자"
        assert keyword_meaning_covered(title, "창섭 전과자", self.ALIASES) is True

    def test_a_natural_first_paragraph_is_accepted(self):
        paragraph = (
            "유튜브 웹예능 전과자는 이창섭이 여러 대학의 학과를 찾아가 실제 강의와 "
            "실습에 참여하는 프로그램입니다."
        )
        assert keyword_meaning_covered(paragraph, "창섭 전과자", self.ALIASES) is True

    def test_tokens_scattered_across_different_sentences_do_not_pass(self):
        text = "전과자는 인기 있는 프로그램입니다. 이창섭은 가수로도 활동합니다."
        # 두 토큰이 서로 다른 문맥에 흩어져 있으면 그 검색어의 글이 아니다.
        assert keyword_meaning_covered(text, "창섭 전과자", self.ALIASES) is False

    def test_the_exact_string_still_passes(self):
        assert keyword_meaning_covered("창섭 전과자 정리", "창섭 전과자") is True

    def test_a_single_token_keyword_still_requires_the_word(self):
        assert keyword_meaning_covered("갤럭시 신제품 소식", "아이폰17") is False
        assert keyword_meaning_covered("아이폰17 출시일", "아이폰17") is True


class TestEntityAliases:
    def test_a_short_name_maps_to_the_official_name(self):
        table = entity_aliases(["이창섭"])
        assert "이창섭" in table["창섭"]
        assert "창섭" in table["이창섭"]

    def test_short_or_non_korean_names_are_skipped(self):
        assert entity_aliases(["BTS", "김"]) == {}


class TestGroupMemberCombination:
    """B. 사람 이름 + 그룹명 조합도 같은 규칙으로 다뤄진다."""

    NAMES = ["프로미스나인", "백지헌"]

    def test_the_combination_is_detected(self):
        assert is_entity_juxtaposition("백지헌 프로미스나인", self.NAMES) is True

    def test_using_it_as_a_noun_is_caught(self):
        body = "백지헌 프로미스나인은 최근 활동을 이어가고 있습니다."
        assert raw_keyword_misuse(body, "백지헌 프로미스나인") == ["백지헌 프로미스나인은"]

    def test_a_relationship_sentence_passes_and_still_covers_the_intent(self):
        body = "프로미스나인 멤버 백지헌은 최근 활동을 이어가고 있습니다."
        assert raw_keyword_misuse(body, "백지헌 프로미스나인") == []
        assert (
            keyword_meaning_covered(
                body, "백지헌 프로미스나인", entity_aliases(self.NAMES)
            )
            is True
        )


class TestPrimaryRawKeyword:
    def test_it_reads_only_what_the_user_selected(self):
        class _Input:
            raw_keywords = ["창섭 전과자", "전과자"]

        assert primary_raw_keyword(_Input()) == "창섭 전과자"

    def test_old_documents_without_the_field_produce_nothing(self):
        class _Input:
            pass

        assert primary_raw_keyword(_Input()) == ""
