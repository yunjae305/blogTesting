/** 브랜드 자료 — 서버 `BrandProfile`과 같은 모양(camelCase 통신 필드). */

export interface BrandLink {
  label: string;
  url: string;
}

export interface BrandImage {
  label: string;
  dataUrl: string;
  caption?: string | null;
}

/** 고른 고객 한 갈래. 대분류 하나와 그 아래 유형들이다. */
export interface BrandAudience {
  category: string;
  types: string[];
  /** '기타'를 골랐을 때만 채운다. */
  other?: string | null;
}

/** 올려 둔 문서. TEXT면 value가 글자 그대로, PDF면 data URL이다. */
export interface BrandDocument {
  /** 어느 서술 칸의 자료인지. */
  section: "description" | "features";
  name: string;
  kind: "TEXT" | "PDF";
  value: string;
}

/**
 * "이런 상황이면 이 기능" 기준표 한 줄(2026-08-19).
 *
 * 트렌드 소재에 브랜드를 **활용 도구로** 얹는 글이 여기서 기능 이름을 가져온다. 서술
 * 칸(features)에는 기능이 줄글로 섞여 있어서, 그것만 주면 모델이 매번 같은 것을 고르거나
 * 없는 이름을 지어낸다. 결합 가능성 판정(A·B·C)도 이 표로 잰다.
 */
export interface BrandUseCase {
  /** 독자가 처한 상황. 예: "어떤 정보를 알아보고 싶을 때". */
  situation: string;
  /** 그때 쓰는 **실제 기능 이름**. 예: "자료 조사". 글에 이 이름 그대로 등장한다. */
  feature: string;
  /** 이 상황을 알아보는 소재·검색어들. 비우면 situation의 낱말이 그 자리를 대신한다. */
  keywords: string[];
}

/**
 * 글 **맨 마지막에 언제나 붙는** 마무리 블록(2026-08-19).
 *
 * 본문은 광고가 아니어야 하지만, 글의 끝에는 "여기서 보면 된다"는 자리가 하나 있어야
 * 한다. 그 둘은 충돌하지 않는다 — 본문에서 권유하지 않기 때문에 마지막 한 줄이 오히려
 * 신뢰를 얻는다.
 *
 * **모델이 쓰지 않고 서버가 붙인다.** 매번 똑같아야 하는 사실이고(가입 조건·크레딧 수),
 * 붙는 자리가 최종 검수 뒤라 검수가 광고 문구로 읽고 고칠 일이 없다.
 */
export interface BrandClosing {
  /** 사실 한 줄. 예: "가입은 무료, 웰컴 크레딧 100 지급, 카드 등록 없음." */
  note: string;
  /** 링크에 보이는 글자. 예: "aiona.kr" */
  label: string;
  /** 실제 주소. */
  url: string;
  /** 함께 붙일 브랜드 이미지의 이름(마스코트). 비우면 글자만 붙는다. */
  imageLabel?: string | null;
}

/**
 * 목록 화면이 쓰는 가벼운 브랜드(`GET /brands?view=summary`).
 *
 * 이미지·문서의 base64가 빠져 있다. 브랜드 하나가 2MB인데(실측: 이미지 9장) 고르기
 * 화면은 이름과 한 줄 소개만 그린다 — 전체를 받으면 그동안 화면이 멈춰 있다.
 * 편집기는 전체가 필요하므로 그때 `GET /brands/{id}`로 따로 받는다.
 */
export interface BrandSummary {
  brandId: string;
  userId: string;
  name: string;
  description?: string | null;
  linkCount: number;
  documentCount: number;
  imageCount: number;
  createdAt: string;
  updatedAt: string;
}

export interface BrandProfile {
  brandId: string;
  userId: string;
  name: string;
  description?: string | null;
  features?: string | null;
  /** "이런 상황이면 이 기능" 기준표. 비어 있어도 글은 써진다 — 채울수록 정확해진다. */
  useCases: BrandUseCase[];
  /** 글 맨 마지막에 언제나 붙는 마무리. 없으면 아무것도 붙지 않는다. */
  closing?: BrandClosing | null;
  /**
   * 모든 글에 **고정으로** 붙는 해시태그(2026-08-20). 앞에서부터 두 개가 쓰인다.
   *
   * 모델에게 맡기지 않는다 — 맡기면 회차마다 붙었다 안 붙었다 하고 표기도 흔들린다
   * (AIONA / 아이오나 / Aiona). '#'은 적지 않는다. 발행할 때 붙는다.
   */
  hashtags: string[];
  /** 주요 고객. 자유 입력이 아니라 고른 것이다(대분류 → 유형). */
  audiences: BrandAudience[];
  links: BrandLink[];
  documents: BrandDocument[];
  images: BrandImage[];
  createdAt: string;
  updatedAt: string;
}
