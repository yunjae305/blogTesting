import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_BRAND_ID } from "../../constants";
import type { BrandProfile } from "./types";

/**
 * 저장해 둔 브랜드 자료를 지우는 것(2026-08-20 사용자 요청).
 *
 * 되돌릴 수 없는 동작이라 여기서 보는 것이 셋이다.
 *
 * 1. **묻고 지운다** — 취소하면 아무 일도 일어나지 않는다.
 * 2. **지울 수 없는 것에는 버튼이 없다** — 기본 브랜드(AIONA)는 서버가 거부하므로,
 *    눌러 봐야 오류만 나는 버튼을 보여 주지 않는다.
 * 3. **지운 뒤 화면이 정리된다** — 고른 브랜드를 지웠으면 선택이 풀려야 한다.
 *    그러지 않으면 없는 브랜드가 골라진 채로 남아 저장할 때 서버가 404를 낸다.
 */
const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  store: { showToast: vi.fn(), reportError: vi.fn() },
}));

vi.mock("../../api/client", () => ({ request: mocks.request }));
vi.mock("../../store", () => ({ useStore: () => mocks.store }));

import { BrandEditor } from "./BrandEditor";

function brand(overrides: Partial<BrandProfile> = {}): BrandProfile {
  return {
    brandId: "brand_1",
    userId: "user_1",
    name: "우리 회사",
    description: "소개",
    features: null,
    useCases: [],
    audiences: [],
    links: [],
    documents: [],
    images: [],
    createdAt: "2026-08-20T00:00:00.000Z",
    updatedAt: "2026-08-20T00:00:00.000Z",
    ...overrides,
  };
}

describe("브랜드 자료 삭제", () => {
  let container: HTMLDivElement;
  let root: Root;
  let deleted: string[];

  beforeEach(() => {
    vi.clearAllMocks();
    // 편집기는 열릴 때 고객 유형 목록을 받아 온다(AudiencePicker). 그 응답이 없으면
    // 화면이 통째로 죽어 삭제 버튼까지 사라진다 — 이 테스트가 보려는 것과 무관한 실패다.
    mocks.request.mockImplementation(async (path: string) =>
      path.startsWith("/brands/audience-options")
        ? { otherLabel: "기타", categories: [{ category: "개인 고객", types: ["직장인"] }] }
        : undefined,
    );
    deleted = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function render(profile: BrandProfile | null, withHandler = true) {
    await act(async () =>
      root.render(
        <BrandEditor
          brand={profile}
          attachments={{ images: profile?.images ?? [], documents: profile?.documents ?? [] }}
          onSaved={() => {}}
          onCancel={() => {}}
          onDeleted={withHandler ? (id) => deleted.push(id) : undefined}
        />,
      ),
    );
  }

  function deleteButton(): HTMLButtonElement | null {
    return container.querySelector<HTMLButtonElement>(".brand-delete-button");
  }

  function answerConfirm(yes: boolean) {
    const asked: string[] = [];
    vi.stubGlobal("confirm", (message: string) => {
      asked.push(message);
      return yes;
    });
    return asked;
  }

  it("저장된 자료에는 삭제 버튼이 있다", async () => {
    await render(brand());

    expect(deleteButton()?.textContent).toContain("자료 삭제");
  });

  it("새로 만드는 중에는 삭제 버튼이 없다", async () => {
    // 아직 저장하지 않았으니 지울 것이 없다.
    await render(null);

    expect(deleteButton()).toBeNull();
  });

  it("기본 브랜드에는 삭제 버튼이 없다", async () => {
    // 서버가 거부한다(지워도 다음 조회에서 되살아나므로). 눌러 봐야 오류만 나는
    // 버튼을 보여 줄 이유가 없다.
    await render(brand({ brandId: DEFAULT_BRAND_ID, name: "AIONA" }));

    expect(deleteButton()).toBeNull();
  });

  it("묻고 지운다 — 취소하면 아무 일도 일어나지 않는다", async () => {
    const asked = answerConfirm(false);
    await render(brand());

    await act(async () => deleteButton()!.click());

    expect(asked[0]).toContain("되돌릴 수 없습니다");
    expect(mocks.request).not.toHaveBeenCalledWith(
      expect.stringContaining("/brands/brand_1"),
      expect.anything(),
    );
    expect(deleted).toEqual([]);
  });

  it("확인하면 서버에 지우고 부모에게 알린다", async () => {
    answerConfirm(true);
    await render(brand());

    await act(async () => deleteButton()!.click());

    expect(mocks.request).toHaveBeenCalledWith("/brands/brand_1", { method: "DELETE" });
    expect(deleted).toEqual(["brand_1"]);
    expect(mocks.store.showToast).toHaveBeenCalledWith(
      expect.stringContaining("삭제했습니다"),
    );
  });

  it("함께 사라지는 첨부의 개수를 미리 말한다", async () => {
    // 이미지·문서까지 없어진다는 것은 목록만 보고는 알 수 없다.
    const asked = answerConfirm(false);
    await render(
      brand({
        images: [{ label: "로고", dataUrl: "data:image/png;base64,AAAA" }],
        documents: [
          { section: "description", name: "소개.txt", kind: "TEXT", value: "본문" },
        ],
      }),
    );

    await act(async () => deleteButton()!.click());

    expect(asked[0]).toContain("이미지 1장");
    expect(asked[0]).toContain("문서 1개");
  });

  it("지우다 실패하면 부모에게 알리지 않는다", async () => {
    // 실패했는데 목록에서 빼면, 화면에는 없고 서버에는 있는 자료가 생긴다.
    answerConfirm(true);
    mocks.request.mockImplementation(async (path: string) => {
      if (path.startsWith("/brands/audience-options")) {
        return { otherLabel: "기타", categories: [{ category: "개인 고객", types: ["직장인"] }] };
      }
      throw new Error("서버 오류");
    });
    await render(brand());

    await act(async () => deleteButton()!.click());

    expect(deleted).toEqual([]);
    expect(mocks.store.reportError).toHaveBeenCalled();
  });
});
