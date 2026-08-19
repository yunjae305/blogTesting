/**
 * 이미지 한 장의 출처 표시(2026-08-11).
 *
 * 예전에는 서버가 만든 캡션 문자열 한 줄이 전부였다 — `출처: imgnews.pstatic.net`. 그
 * 한 줄로는 원문 페이지로 갈 수 없고, 이용 조건을 확인할 수 있는지도 알 수 없다. 여기서는
 * 구조화된 출처(ImageSourceInfo)를 받아 **이름은 이름대로, 링크는 링크대로** 그린다.
 *
 * 잘림에 대하여. 출처명이 길어도 카드·이미지 영역 밖으로 밀려 나가면 안 되고, 그렇다고
 * 글자 크기를 줄여 억지로 맞추지도 않는다. 이름 칸만 말줄임(...)으로 접고, 접힌 이름은
 * hover(title)로도, 눌러서 펼치기로도 전부 볼 수 있게 한다 — 마우스가 없는 화면에서
 * hover만 두면 확인할 방법이 사라진다.
 *
 * 단정하지 않는 것. 라이선스를 확인하지 못한 이미지에 '사용 가능'·'저작권 문제 없음'을
 * 적지 않는다. 그때 쓰는 문구는 '이용 조건 미확인'이다.
 */
import { useState } from "react";

import type { ImageSourceInfo } from "../../api/types";

/** 이용 가능 여부를 사람이 읽는 말로. 확인하지 못한 것을 사용 가능이라고 하지 않는다. */
const USAGE_LABELS: Record<ImageSourceInfo["usageStatus"], string> = {
  allowed: "이용 조건 확인됨",
  restricted: "이용 조건 제한",
  unknown: "이용 조건 미확인",
};

export function ImageSourceNote({ source }: { source: ImageSourceInfo | null | undefined }) {
  // 접힌 이름을 눌러서 펼친 상태. hover가 없는 화면(모바일)에서도 전체를 볼 수 있어야 한다.
  const [expanded, setExpanded] = useState(false);

  // 출처 정보가 없는 이미지(옛 문서·코드로 그린 도표·사용자 업로드)는 이 줄을 그리지 않는다.
  // 없는 출처를 '출처 정보 없음'으로 채워 넣으면 화면만 늘어난다 — 캡션이 이미 그 자리를 쓴다.
  if (!source) return null;

  // AI가 그린 이미지에는 외부 웹사이트 출처가 없다. 구분만 남기고 링크·이용 조건은 내지 않는다.
  if (source.sourceType === "generated") {
    return (
      <p className="image-source-note is-generated">
        <span className="image-source-kind">AI 생성 이미지</span>
      </p>
    );
  }

  const name = (source.sourceName ?? "").trim();
  const pageUrl = (source.sourcePageUrl ?? "").trim();
  const licenseUrl = (source.licenseUrl ?? "").trim();
  const license = (source.license ?? "").trim();

  return (
    <p className="image-source-note">
      <span className="image-source-label">출처</span>
      {name ? (
        <button
          type="button"
          className={`image-source-name${expanded ? " is-expanded" : ""}`}
          // 전체 이름은 hover로도 읽힌다. 접힌 이름을 확인할 방법을 하나만 두지 않는다.
          title={name}
          aria-expanded={expanded}
          onClick={() => setExpanded((open) => !open)}
        >
          {name}
        </button>
      ) : (
        // 사이트 이름조차 확인하지 못한 경우. 지어내지 않고 모른다고 적는다.
        <span className="image-source-name is-unknown">출처 정보 없음</span>
      )}
      {pageUrl && (
        // 전체 URL을 글자로 늘어놓지 않는다 — 링크 뒤에 숨긴다.
        <a
          className="image-source-link"
          href={pageUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          원문 보기 ↗
        </a>
      )}
      {licenseUrl ? (
        <a
          className="image-source-link"
          href={licenseUrl}
          target="_blank"
          rel="noopener noreferrer"
          title={license || undefined}
        >
          이용 조건 확인 ↗
        </a>
      ) : (
        <span className={`image-source-usage is-${source.usageStatus}`}>
          {USAGE_LABELS[source.usageStatus] ?? USAGE_LABELS.unknown}
        </span>
      )}
    </p>
  );
}
