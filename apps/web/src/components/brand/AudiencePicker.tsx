import { useEffect, useState } from "react";

import { request } from "../../api/client";
import type { BrandAudience } from "./types";

type Options = {
  otherLabel: string;
  categories: { category: string; types: string[] }[];
};

type Props = {
  value: BrandAudience[];
  onChange: (next: BrandAudience[]) => void;
};

/**
 * 주요 고객 — 대분류를 고르면 그 아래 유형이 펼쳐지는 2단계 선택.
 *
 * 자유 입력(textarea)을 걷어낸 이유는 둘이다. 사람마다 "중소기업"·"중기"·"SMB"로 달리
 * 적어 프롬프트가 들쭉날쭉해졌고, 무엇을 적어야 할지 몰라 비워 두는 칸이 됐다.
 *
 * 선택지는 **서버에서 받는다**(`/brands/audience-options`). 화면이 목록을 따로 들고 있으면
 * 서버 검증과 어긋나, 사용자가 고를 수 있는데 저장은 거부되는 값이 생긴다.
 *
 * 여기 없는 것: 연령대·글 목적·이번 글의 키워드. 그 셋은 글마다 달라서 작성 화면에서
 * 받는다 — 브랜드 자료에 박아 두면 모든 글이 같은 대상을 향하게 된다.
 */
export function AudiencePicker({ value, onChange }: Props) {
  const [options, setOptions] = useState<Options | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setOptions(await request<Options>("/brands/audience-options"));
      } catch {
        // 선택지를 못 받으면 아래에서 안내만 한다. 토스트까지 띄울 일은 아니다.
        setOptions({ otherLabel: "기타", categories: [] });
      }
    })();
  }, []);

  const picked = (category: string) => value.find((item) => item.category === category);

  function toggleCategory(category: string) {
    if (picked(category)) {
      onChange(value.filter((item) => item.category !== category));
      return;
    }
    onChange([...value, { category, types: [] }]);
  }

  function toggleType(category: string, type: string) {
    onChange(
      value.map((item) => {
        if (item.category !== category) return item;
        const types = item.types.includes(type)
          ? item.types.filter((one) => one !== type)
          : [...item.types, type];
        // '기타'를 끄면 거기 적은 글자도 함께 버린다. 남겨 두면 화면에 보이지 않는 값이
        // 저장돼, 사용자가 모르는 문장이 프롬프트에 실린다.
        const other = types.includes(options?.otherLabel ?? "기타") ? item.other : undefined;
        return { ...item, types, other };
      }),
    );
  }

  function setOther(category: string, other: string) {
    onChange(value.map((item) => (item.category === category ? { ...item, other } : item)));
  }

  if (options === null) {
    return <p className="brand-field-hint">고객 유형을 불러오는 중입니다.</p>;
  }
  // `categories`가 없는 응답(옛 서버·프록시가 돌려준 엉뚱한 JSON)도 여기로 온다. 배열을
  // 가정하고 `.length`를 읽으면 자료 편집 화면이 통째로 흰 화면이 된다 — 고객 유형 하나
  // 때문에 브랜드 자료를 못 고치게 만들 이유가 없다.
  if (!options.categories?.length) {
    return <p className="brand-field-hint">고객 유형을 불러오지 못했습니다. 새로고침해 주세요.</p>;
  }

  return (
    <div className="audience-picker">
      <div className="audience-categories" role="group" aria-label="고객 대분류">
        {options.categories.map(({ category }) => {
          const on = Boolean(picked(category));
          return (
            <button
              key={category}
              type="button"
              aria-pressed={on}
              className={`audience-chip ${on ? "selected" : ""}`.trim()}
              onClick={() => toggleCategory(category)}
            >
              {/* 고른 표시(✓)는 CSS가 그린다. 글자로 넣으면 버튼의 텍스트가 '✓기업·사업자'가
                  되어, 이름으로 버튼을 찾는 쪽(테스트·보조기술)이 이름을 못 찾는다.
                  상태 자체는 aria-pressed가 말하므로 체크는 장식이다. */}
              {category}
            </button>
          );
        })}
      </div>

      {/* 고른 대분류만, 고른 순서대로 아래에 펼친다. 전부 펼쳐 두면 화면이 길어지고
          무엇을 고른 상태인지 알아보기 어렵다. */}
      {options.categories
        .filter(({ category }) => picked(category))
        .map(({ category, types }) => {
          const current = picked(category)!;
          const otherOn = current.types.includes(options.otherLabel);
          return (
            <fieldset className="audience-types" key={category}>
              <legend>{category}</legend>
              <div className="audience-type-chips">
                {types.map((type) => {
                  const on = current.types.includes(type);
                  return (
                    <button
                      key={type}
                      type="button"
                      aria-pressed={on}
                      className={`audience-chip small ${on ? "selected" : ""}`.trim()}
                      onClick={() => toggleType(category, type)}
                    >
                      {type}
                    </button>
                  );
                })}
              </div>
              {otherOn && (
                <input
                  aria-label={`${category} 기타 입력`}
                  className="audience-other"
                  placeholder="어떤 고객인지 직접 적어 주세요"
                  value={current.other ?? ""}
                  onChange={(event) => setOther(category, event.target.value)}
                />
              )}
              {!current.types.length && (
                <p className="brand-field-hint">
                  유형을 하나도 고르지 않으면 이 대분류는 저장되지 않습니다.
                </p>
              )}
            </fieldset>
          );
        })}
    </div>
  );
}
