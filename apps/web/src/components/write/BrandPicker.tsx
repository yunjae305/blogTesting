/**
 * 소재 단계의 '활용할 브랜드 · 서비스' 칸(2026-08-11, 2026-08-19 이름·잠금 변경).
 *
 * 브랜드 글쓰기는 원래 별도 메뉴였다(`#/brand`). 트렌드 키워드 하나를 고르고 시작을
 * 누르면 제목·검증·원고까지 알아서 도는 화면이었는데, 사용자가 보기에 새 글 작성과
 * 다를 것이 없었다 — 결국 둘 다 원고 한 편을 만든다. 그래서 메뉴를 없애고 **소재
 * 단계의 선택 항목 하나**로 들어왔다. 브랜드가 하는 일은 자기 자료(소개·주소·문서·
 * 이미지)를 그 글의 참고자료로 얹는 것뿐이고, 그 뒤는 평범한 글과 완전히 같은 길이다.
 *
 * 2026-08-19에 소재와의 **잠금을 없앴다.** 그전에는 소재를 적으면 이 칸이 잠겼는데,
 * 이 저장소가 실제로 만들려는 글이 바로 그 조합이다 — 트렌드가 주인공이고 브랜드는 그
 * 상황에서 쓴 도구인 글. 무엇이 주인공인지는 잠금이 아니라 부모가 그리는 역할 고르기가
 * 정한다(`children`).
 *
 * 편집기를 **모달로** 여는 이유. 별도 화면으로 나가면 지금 적던 소재·목적·참고자료가
 * 통째로 날아간다 — 아직 저장 전이라 서버에도 없다. 브랜드 자료를 고치러 갔다 왔더니
 * 처음부터 다시 적어야 하는 것은 통합의 목적과 정반대다.
 */
import { Suspense, lazy, useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { request } from "../../api/client";
import { matchesCampaign } from "../../campaign";
import { useStore } from "../../store";
import type { BrandProfile, BrandSummary } from "../brand/types";

/**
 * 브랜드 자료 편집기는 **모달을 열 때** 받아 온다.
 *
 * 원고 편집기(DraftEditor)와 같은 이유다. 브랜드 자료를 고치는 사람은 일부인데, 이것을
 * 같이 묶으면 '새 글 작성'을 누른 모든 사람이 27kB를 먼저 받고 기다린다(실측: 작성 화면
 * 조각이 91kB → 118kB). 브랜드 화면이 따로 있을 때는 그 화면의 조각이었던 것이라,
 * 통합하면서 작성 화면이 무거워질 이유가 없다.
 */
const BrandEditor = lazy(() =>
  import("../brand/BrandEditor").then((module) => ({ default: module.BrandEditor })),
);

/** 새 브랜드의 첨부 — 기다릴 것이 없다. 렌더마다 새 객체를 만들면 편집기의 첨부 채우기
    effect가 매번 다시 돌아 사용자가 올린 이미지를 지우므로, 모듈 상수로 고정한다. */
const EMPTY_ATTACHMENTS = { images: [], documents: [] };

type Props = {
  /** 고른 브랜드. 안 골랐으면 빈 문자열이다 — 브랜드는 **선택**이다. */
  brandId: string;
  /** 고른 브랜드의 id와 이름. 이름은 요약 칸이 '소재' 자리에 그릴 값이다. */
  onChange: (brandId: string, brandName: string) => void;
  /**
   * 칸 이름 옆 배지. 기본은 '선택'이다.
   *
   * 새 글 작성에서는 **소재와 브랜드 중 하나는 있어야** 넘어가므로 그쪽이 '둘 중 하나'로
   * 바꿔 준다(2026-08-20 사용자 지적: 둘 다 '선택'이면 아무것도 안 채워도 되는 줄 안다).
   * 자동 포스팅은 소재를 줄마다 적으므로 브랜드가 정말 선택이라 기본값 그대로다.
   */
  badge?: string;
  /**
   * 들어온 경로(`?campaign=aiona`). 이름이 같은 브랜드를 **한 번** 미리 골라 준다.
   *
   * 새 글일 때만 부모가 채워 준다. 저장된 글에서도 이 값이 오면, 브랜드를 일부러 뺀 글을
   * 다시 열었을 때 그 브랜드가 되붙는다 — 빈 값만으로는 "아직 안 골랐다"와 "빼기로
   * 했다"를 가를 수 없다.
   */
  campaign?: string;
  /**
   * 고르기 칸 **아래**에 붙는 것. 지금은 역할 고르기와 결합 가능성 판정이 들어온다
   * (2026-08-19). 이 컴포넌트가 직접 그리지 않는 이유는, 그 둘이 소재를 함께 봐야
   * 하는데 소재는 여기 없기 때문이다 — 소재를 내려보내면 이 칸이 브랜드 목록을 다루는
   * 일 말고 글의 구성까지 알게 된다.
   */
  children?: ReactNode;
};

export function BrandPicker({
  brandId,
  onChange,
  campaign = "",
  badge = "선택",
  children,
}: Props) {
  const { reportError } = useStore();

  // null은 '아직 받는 중'. 빈 배열은 '등록한 브랜드가 없음'이라는 확정된 답이다.
  const [brands, setBrands] = useState<BrandSummary[] | null>(null);
  // 편집 중인 브랜드. "new"면 새로 만드는 중, 문자열이면 그 브랜드를 고치는 중이다.
  const [editing, setEditing] = useState<string | null>(null);
  // 편집기는 전체 문서가 필요하다(목록에는 이미지·문서의 base64가 없다).
  const [editingBrand, setEditingBrand] = useState<BrandProfile | null>(null);

  const load = useCallback(async () => {
    try {
      // 목록은 **가벼운 것**만 받는다. 전체를 받으면 이미지 base64까지 딸려 와 브랜드
      // 하나에 2MB다(실측) — 이름 하나 그리려고 그걸 기다릴 이유가 없다.
      const list = await request<BrandSummary[]>("/brands?view=summary");
      // 목록이 아닌 것이 오면 '브랜드 없음'으로 둔다. 브랜드는 선택 항목이라, 여기서
      // 터지면 소재 입력 화면 전체가 함께 죽는다.
      setBrands(Array.isArray(list) ? list : []);
    } catch (error) {
      // 브랜드를 못 받았다고 소재 입력을 막지 않는다. 브랜드는 선택 항목이다.
      setBrands([]);
      reportError(error);
    }
  }, [reportError]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * AIONA 앱스튜디오에서 들어왔으면 그 브랜드를 미리 골라 둔다(2026-08-19).
   *
   * 목록이 도착한 **뒤에 한 번만** 한다. 그 전에는 고를 이름이 없고, 뒤에 다시 하면
   * 사용자가 방금 바꾼 선택을 되돌린다.
   *
   * "이미 고른 브랜드가 없을 때만" 으로는 부족하다 — 브랜드 없이 저장해 둔 글을 다시
   * 열었을 때도 빈 값이라, 사용자가 일부러 뺀 브랜드가 다시 붙는다. 그래서 **새 글일
   * 때만** 캠페인 값이 온다(부모가 그 판단을 한다).
   */
  const applied = useRef(false);
  useEffect(() => {
    if (applied.current || brands === null || brandId || !campaign) return;
    applied.current = true;
    const picked = brands.find((item) => matchesCampaign(campaign, item.name));
    if (picked) onChange(picked.brandId, picked.name);
  }, [brandId, brands, campaign, onChange]);

  useEffect(() => {
    if (!editing || editing === "new") {
      setEditingBrand(null);
      return;
    }
    let cancelled = false;
    setEditingBrand(null);
    void (async () => {
      try {
        const full = await request<BrandProfile>(`/brands/${encodeURIComponent(editing)}`);
        if (!cancelled) setEditingBrand(full);
      } catch (error) {
        if (!cancelled) {
          reportError(error);
          setEditing(null);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [editing, reportError]);

  const summary = (brands ?? []).find((item) => item.brandId === brandId) ?? null;
  // 고른 브랜드가 목록에 없다 = 다른 곳에서 지웠다. 조용히 넘어가면 사용자는 그 브랜드가
  // 반영되는 줄 알고 저장한다(서버는 404를 낸다).
  const missing = brands !== null && Boolean(brandId) && summary === null;

  return (
    <div className="field brief-brand-field">
      <div className="field-label-row">
        {/* 「브랜드 글쓰기」였던 이름을 바꿨다(2026-08-19). 그 이름은 "브랜드에 대한
            글을 쓴다"로 읽히는데, 이제 이 칸이 주로 하는 일은 그것이 아니다 — 트렌드
            글에 **활용한 도구로** 브랜드를 얹는 쪽이다. */}
        <label htmlFor="brandId">활용할 브랜드 · 서비스</label>
        <span className={`field-badge ${badge === "선택" ? "opt" : "req"}`}>{badge}</span>
      </div>

      <select
        id="brandId"
        className="brief-brand-select"
        value={brandId}
        disabled={brands === null}
        onChange={(event) => {
          const next = event.target.value;
          const picked = (brands ?? []).find((item) => item.brandId === next);
          onChange(next, picked?.name ?? "");
        }}
      >
        <option value="">
          {brands === null
            ? "브랜드 자료를 불러오는 중…"
            : brands.length
              ? "브랜드 없이 쓰기"
              : "등록한 브랜드 자료가 없습니다"}
        </option>
        {(brands ?? []).map((item) => (
          <option key={item.brandId} value={item.brandId}>
            {item.name}
          </option>
        ))}
      </select>

      <div className="brief-brand-actions">
        {/* 자료 편집은 브랜드를 고르지 않았어도 열어 둔다 — 이 버튼은 글의 방향을
            바꾸지 않고, 다음 글에 쓸 자료를 지금 다듬어 둘 수 있어야 한다. */}
        <button type="button" className="button small" onClick={() => setEditing("new")}>
          + 브랜드 추가
        </button>
        <button
          type="button"
          className="button small"
          disabled={!summary}
          onClick={() => summary && setEditing(summary.brandId)}
        >
          브랜드 관리
        </button>
      </div>

      {/* 평상시 설명 줄("선택한 브랜드의 정보와 톤앤매너가…")은 뺐다(2026-08-11 사용자
          요청). 남긴 것은 설명이 아니라 **지금 무슨 일이 일어났는지**다. */}
      {missing && (
        <p className="field-desc">고른 브랜드 자료를 찾을 수 없습니다. 다시 선택해 주세요.</p>
      )}

      {/* 역할 고르기와 결합 가능성 판정. 소재를 함께 봐야 하는 것이라 부모가 그린다. */}
      {children}

      {editing && (
        <div
          className="brand-editor-modal"
          role="dialog"
          aria-modal="true"
          aria-label={editing === "new" ? "브랜드 추가" : "브랜드 자료 편집"}
        >
          {/* 바깥을 눌러 닫지 않는다 — 편집 중인 브랜드 자료를 실수로 잃는 쪽이
              닫는 버튼을 한 번 더 누르는 것보다 나쁘다. */}
          <div className="brand-editor-modal-backdrop" aria-hidden="true" />
          <div className="brand-editor-modal-body">
            {editing !== "new" && editingBrand === null ? (
              <div className="empty" role="status" aria-live="polite">
                <p>브랜드 자료를 불러오는 중입니다.</p>
              </div>
            ) : (
              <Suspense
                fallback={
                  <div className="empty" role="status" aria-live="polite">
                    <p>편집기를 준비하는 중입니다.</p>
                  </div>
                }
              >
              <BrandEditor
                brand={editing === "new" ? null : editingBrand}
                attachments={
                  editing === "new"
                    ? EMPTY_ATTACHMENTS
                    : {
                        images: editingBrand?.images ?? [],
                        documents: editingBrand?.documents ?? [],
                      }
                }
                onCancel={() => setEditing(null)}
                onDeleted={(deletedId) => {
                  // 목록에서 빼고 편집기를 닫는다. 다시 부르지 않는 이유는 방금 무엇이
                  // 사라졌는지 이미 알기 때문이다 — 왕복 한 번이면 화면이 그만큼 늦는다.
                  setBrands((prev) => (prev ?? []).filter((item) => item.brandId !== deletedId));
                  // **고른 브랜드를 지웠으면 선택을 푼다.** 그러지 않으면 없는 브랜드가
                  // 골라진 채로 남아, 저장할 때 서버가 404를 낸다.
                  if (deletedId === brandId) onChange("", "");
                  setEditing(null);
                }}
                onSaved={(saved) => {
                  // 목록을 다시 부르지 않고 그 자리에서 갈아 끼운다 — 방금 고친 이름이
                  // 바로 보인다. 목록은 가벼운 모양이므로 무거운 필드는 개수만 옮긴다.
                  setBrands((prev) => [
                    {
                      brandId: saved.brandId,
                      userId: saved.userId,
                      name: saved.name,
                      description: saved.description,
                      linkCount: saved.links?.length ?? 0,
                      documentCount: saved.documents?.length ?? 0,
                      imageCount: saved.images?.length ?? 0,
                      createdAt: saved.createdAt,
                      updatedAt: saved.updatedAt,
                    },
                    ...(prev ?? []).filter((item) => item.brandId !== saved.brandId),
                  ]);
                  // 방금 만들거나 고친 브랜드를 그대로 고른 상태로 둔다 — 추가해 놓고
                  // 다시 목록에서 찾아 고르게 하지 않는다.
                  onChange(saved.brandId, saved.name);
                  setEditing(null);
                }}
              />
              </Suspense>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
