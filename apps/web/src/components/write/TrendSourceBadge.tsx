/**
 * 출처 칩. 키워드가 어느 서비스에서 확인됐는지 보여준다.
 *
 * 이전에는 세 출처가 모두 같은 초록 칩이었다 — 유튜브에서 온 키워드와 네이버에서 온
 * 키워드가 글자를 읽기 전까지 구분되지 않았다. 각 서비스의 로고를 그대로 쓰면 카드를
 * 훑기만 해도 출처가 읽힌다.
 *
 * **화면에는 로고만 그린다**(2026-08-07 사용자 요청 — 글자와 알약 배경을 뺐다).
 * 이름은 지우지 않고 `sr-only`로 숨긴다: 색과 그림만 남기면 색을 구분하지 못하는
 * 사용자에게는 출처가 통째로 사라진다.
 *
 * 로고는 외부 요청 없이 인라인 SVG로 그린다 — 카드 하나에 칩이 여러 개 붙고, 이미지 파일을
 * 불러오면 목록이 그릴 때마다 깜빡인다.
 */

import type { ReactElement } from "react";

import { trendSourceLabel } from "./trends";

/** 브랜드가 정한 표기. 라벨 상수(TREND_SOURCE_LABELS)는 대문자 키 표기라 캡션·요약에 그대로
    쓰지만, 로고 옆에는 브랜드가 쓰는 대소문자를 쓴다(네이버 워드마크만 대문자). */
type Brand = {
  /** CSS 변형 클래스 접미사(.title-keyword-chip--google 등). */
  variant: string;
  name: string;
  logo: () => ReactElement;

};

/* 로고는 2026-08-07에 사용자가 준 SVG로 갈아 끼웠다. 파일에 있던 아트보드 배경
   사각형(구글의 #F9F9FB)은 빼고 마크만 쓴다 — 칩 안에 넣으면 회색 타일로 보인다. */

function GoogleLogo() {
  return (
    <svg viewBox="0 0 533.5 544" aria-hidden="true" focusable="false">
      <path
        fill="#4285F4"
        d="M533.5 278.4c0-18.5-1.5-37-4.7-55.2H272v104.6h147c-6.1 33.8-25.7 63.7-54.4 82.7v68h87.7c51.5-47.4 81.2-117.4 81.2-200.1z"
      />
      <path
        fill="#34A853"
        d="M272 544c73.6 0 135.5-24.3 180.4-65.5l-87.7-68c-24.3 16.3-55.5 25.8-92.6 25.8-71 0-131.2-47.9-152.8-112.3H28.7v70.1C74.8 485.7 168.9 544 272 544z"
      />
      <path
        fill="#FBBC04"
        d="M119.3 324c-11.4-33.8-11.4-70.2 0-104v-70.1H28.7c-38.6 76.9-38.6 167.5 0 244.4l90.6-70.3z"
      />
      <path
        fill="#EA4335"
        d="M272 107.7c38.9-.6 76.3 14 104.4 39.6l78-78C421.6 38.4 349.9 7.3 272 8 168.9 8 74.8 66.3 28.7 157.9l90.6 70.1C140.8 155.6 201 107.7 272 107.7z"
      />
    </svg>
  );
}

function YoutubeLogo() {
  return (
    <svg viewBox="0 0 1184 816" aria-hidden="true" focusable="false">
      <path
        fill="#FF0033"
        d="M 42 88 L 27 118 L 20 144 L 12 191 L 6 248 L 0 362 L 0 445 L 5 554 L 15 645 L 20 672 L 29 702 L 39 722 L 54 743 L 72 761 L 97 778 L 112 785 L 133 792 L 167 798 L 237 805 L 326 810 L 460 814 L 666 815 L 878 809 L 999 800 L 1035 795 L 1060 789 L 1088 777 L 1106 765 L 1129 743 L 1143 723 L 1149 712 L 1158 688 L 1166 651 L 1173 602 L 1178 547 L 1183 438 L 1182 341 L 1178 266 L 1169 181 L 1162 143 L 1152 110 L 1140 87 L 1129 72 L 1111 54 L 1091 40 L 1062 27 L 1027 19 L 955 11 L 851 5 L 668 0 L 514 0 L 308 6 L 193 14 L 155 19 L 121 27 L 94 39 L 77 50 L 53 73 Z"
      />
      <path fill="#FFFFFF" d="M 470 236 L 470 579 L 773 408 Z" />
    </svg>
  );
}

function NaverLogo() {
  return (
    <svg viewBox="0 0 299 290" aria-hidden="true" focusable="false">
      {/* 그러데이션 id는 문서 전체에서 하나다. 칩이 여러 개 그려져도 정의가 같아
          문제가 없지만, 이름은 이 로고 것임이 드러나게 둔다. */}
      <defs>
        <linearGradient id="trendChipNaverGreen" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#03EE66" />
          <stop offset="1" stopColor="#03B366" />
        </linearGradient>
      </defs>
      <rect width="299" height="290" fill="url(#trendChipNaverGreen)" />
      <path
        fill="#FFFFFF"
        d="M 215 80 L 173 80 L 172 149 L 122 81 L 84 81 L 84 207 L 125 207 L 126 139 L 174 207 L 215 207 Z"
      />
    </svg>
  );
}
function InstagramLogo() {
  return (
    <svg viewBox="0 0 28 28" aria-hidden="true" focusable="false">
      <rect x="1.6" y="1.6" width="24.8" height="24.8" rx="7.4" fill="#e1306c" />
      <circle cx="14" cy="14" r="5.6" fill="none" stroke="#fff" strokeWidth="2.4" />
      <circle cx="20.6" cy="7.6" r="1.7" fill="#fff" />
    </svg>
  );
}

const BRANDS: Record<string, Brand> = {
  GOOGLE_TRENDS: {
    variant: "google",
    name: "Google",
    logo: GoogleLogo,
  },
  YOUTUBE: { variant: "youtube", name: "YouTube", logo: YoutubeLogo },
  NAVER_DATALAB: { variant: "naver", name: "NAVER", logo: NaverLogo },
  INSTAGRAM: { variant: "instagram", name: "Instagram", logo: InstagramLogo },
};

/**
 * 출처 하나를 칩으로 그린다. 브랜드를 모르는 출처(소재 확장처럼 외부 서비스가 아닌 것)는
 * 로고 없이 기존 중립 칩으로 남는다 — 없는 로고를 만들어 붙이면 수집한 출처처럼 보인다.
 */
export function TrendSourceBadge({ source }: { source: string }) {
  const brand = BRANDS[source];
  if (!brand) {
    return <span className="title-keyword-chip source">{trendSourceLabel(source)}</span>;
  }

  const Logo = brand.logo;
  return (
    <span className={`title-keyword-source title-keyword-source--${brand.variant}`}>
      <Logo />
      {/* 화면에서는 로고만 보인다(2026-08-07 사용자 요청). 이름은 지우지 않고 숨긴다 —
          색과 그림만 남기면 색을 구분하지 못하는 사용자에게는 출처가 통째로 사라진다.
          읽어 주는 쪽에는 여전히 'YouTube'라고 들린다. */}
      <span className="sr-only">{brand.name}</span>
    </span>
  );
}
