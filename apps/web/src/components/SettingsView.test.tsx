import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PersonaCatalogEntry, UserSettings } from "../api/types";

/**
 * 설정 화면은 디자인만 다시 그렸다. 네 카드가 요청한 자리(왼쪽 01·02 / 오른쪽 03·04)에
 * 놓이는지, 저장 payload가 예전 그대로인지, '연결 관리' 행이 사라졌는지를 고정한다.
 */
const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  store: {
    session: { user: { userId: "user_1" } },
    settings: null as UserSettings | null,
    personas: [] as PersonaCatalogEntry[],
    personaCatalogLoading: false,
    reloadPersonaCatalog: vi.fn(),
    setSettings: vi.fn(),
    setRoute: vi.fn(),
    showToast: vi.fn(),
    reportError: vi.fn(),
    markOnboardingSeen: vi.fn(),
  },
}));

vi.mock("../api/client", () => ({ request: mocks.request }));
vi.mock("../store", () => ({ useStore: () => mocks.store }));

import { SettingsView } from "./SettingsView";

const PERSONAS: PersonaCatalogEntry[] = [
  {
    personaId: "p_daily",
    kind: "preset",
    name: "일상 기록 블로거",
    description: "일상의 사소한 생각을 편안하게 풀어냅니다.",
    prompt: "daily prompt",
  },
  {
    personaId: "p_review",
    kind: "preset",
    name: "체험 후기 리뷰어",
    description: "사용하거나 방문한 경험을 구체적으로 정리합니다.",
    prompt: "review prompt",
  },
  {
    personaId: "custom",
    kind: "custom",
    name: "커스텀 페르소나",
    description: "직접 설정합니다.",
    prompt: null,
  },
];

const SETTINGS: UserSettings = {
  userId: "user_1",
  hashtagCount: 5,
  articleLength: "medium",
  blendMode: "trend",
  defaultPersona: "p_daily",
  autoPostingEnabled: true,
  updatedAt: "2026-07-29T00:00:00.000Z",
} as UserSettings;

/** React가 제어하는 input에 값을 넣는다(네이티브 setter를 거쳐야 onChange가 돈다). */
function setValue(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )!.set!;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("SettingsView 설정 대시보드", () => {
  let container: HTMLDivElement;
  let root: Root;

  async function render() {
    await act(async () => {
      root.render(<SettingsView />);
    });
  }

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.store.settings = SETTINGS;
    mocks.store.personas = PERSONAS;
    mocks.request.mockResolvedValue(SETTINGS);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("카드를 왼쪽 01·02 / 오른쪽 03(네이버)·04(스레드)·05 순서로 세운다", async () => {
    await render();

    const columns = container.querySelectorAll(".settings-dashboard__col");
    expect(columns).toHaveLength(2);

    const indexesIn = (column: Element) =>
      [...column.querySelectorAll(".settings-card__index")].map((node) =>
        node.textContent?.trim(),
      );
    expect(indexesIn(columns[0])).toEqual(["01", "02"]);
    expect(indexesIn(columns[1])).toEqual(["03", "04", "05"]);
    // 발행 계정 카드 둘(네이버·스레드)이 나란히 있다.
    expect(container.textContent).toContain("Naver 계정");
    expect(container.textContent).toContain("Threads 계정");

    // 섹션 제목이 왼쪽 레일로 빠지던 예전 구조는 남아 있지 않다.
    expect(container.querySelector(".settings-section")).toBeNull();
  });

  it("네이버 카드에서 '연결 관리' 행을 없애고 입력·저장·보안 안내만 남긴다", async () => {
    await render();

    expect(container.textContent).not.toContain("연결 관리");
    expect(container.querySelector("#naverId")).not.toBeNull();
    expect(container.querySelector("#naverPassword")).not.toBeNull();
    expect(container.querySelector("#saveNaverCredentials")).not.toBeNull();
    expect(container.querySelector(".naver-privacy-note")?.textContent).toContain(
      "서비스 DB에 저장되지 않고",
    );
  });

  it("계정 카드의 버튼은 저장에서 끝나지 않고 그 자리에서 로그인까지 한다", async () => {
    // 발행 도중에 2단계 인증을 만나면 기다릴 수 있는 시간이 짧아 그 발행이 통째로
    // 실패한다. 계정을 바꾸는 이 순간이 인증을 끝내기 가장 좋은 때다.
    await render();

    const naverId = container.querySelector<HTMLInputElement>("#naverId")!;
    const naverPassword = container.querySelector<HTMLInputElement>("#naverPassword")!;
    await act(async () => {
      setValue(naverId, "win-z");
      setValue(naverPassword, "secret");
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>("#saveNaverCredentials")!.click();
    });

    expect(mocks.request).toHaveBeenCalledWith(
      "/naver/login",
      expect.objectContaining({ method: "POST" }),
    );
    // 저장만 하는 옛 주소로 가면 로그인창이 뜨지 않는다.
    expect(mocks.request).not.toHaveBeenCalledWith("/naver/save", expect.anything());

    const threadsId = container.querySelector<HTMLInputElement>("#threadsId")!;
    const threadsPassword = container.querySelector<HTMLInputElement>("#threadsPassword")!;
    await act(async () => {
      setValue(threadsId, "boo_ra.a");
      setValue(threadsPassword, "secret");
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>("#saveThreadsCredentials")!.click();
    });

    expect(mocks.request).toHaveBeenCalledWith(
      "/threads/login",
      expect.objectContaining({ method: "POST" }),
    );
    expect(mocks.request).not.toHaveBeenCalledWith("/threads/save", expect.anything());
  });

  it("버튼 글자가 무엇을 하는지 그대로 말한다", async () => {
    await render();

    expect(container.querySelector("#saveNaverCredentials")?.textContent).toContain(
      "저장하고 로그인",
    );
    expect(container.querySelector("#saveThreadsCredentials")?.textContent).toContain(
      "저장하고 로그인",
    );
  });

  it("로그인에 성공하면 배지가 '그 아이디 로그인됨'으로 바뀐다", async () => {
    // '로그인됨'만 뜨면, 아이디를 바꿔 다시 눌러도 화면이 그대로라 방금 한 로그인이
    // 먹혔는지 알 수가 없다. 어느 계정으로 들어가 있는지가 보여야 한다.
    mocks.request.mockImplementation(async (path: string) => {
      if (path === "/naver/status") {
        return { configured: true, saved: true, savedUsername: "win-z", hasSession: true, sessionAccount: "win-z" };
      }
      if (path === "/threads/status") {
        return { saved: false, savedUsername: null, hasSession: false, sessionAccount: null };
      }
      if (path === "/naver/login") {
        return { configured: true, saved: true, savedUsername: "jsw985400", hasSession: true, sessionAccount: "jsw985400" };
      }
      return SETTINGS;
    });
    await render();

    const badge = () =>
      container.querySelector("#naver-connect-title")?.closest(".settings-card")
        ?.querySelector(".naver-connect-badge")?.textContent ?? "";
    expect(badge()).toContain("win-z 로그인됨");

    // 아이디를 바꿔 다시 누르면 배지도 그 계정으로 따라간다.
    await act(async () => {
      setValue(container.querySelector<HTMLInputElement>("#naverId")!, "jsw985400");
      setValue(container.querySelector<HTMLInputElement>("#naverPassword")!, "secret");
    });
    await act(async () => {
      container.querySelector<HTMLButtonElement>("#saveNaverCredentials")!.click();
    });

    expect(badge()).toContain("jsw985400 로그인됨");
  });

  it("2단계 인증을 끝내면 응답을 기다리지 않고 화면이 풀린다", async () => {
    // 2026-08-06 사용자 신고: 새 계정으로 로그인하고 휴대폰 인증까지 끝냈는데 버튼이
    // '로그인 중'에 멈춰 있었다. /naver/login은 사람이 인증을 마칠 때까지 최대 7분을
    // 기다리고, 그 뒤 글쓰기 화면을 여는 동안에도 응답이 오지 않는다. 서버는 로그인에
    // 성공하는 그 순간 계정을 프로필에 적으므로, 화면은 그것으로 성공을 안다.
    vi.useFakeTimers();
    try {
      let sessionAccount: string | null = null;
      mocks.request.mockImplementation(async (path: string) => {
        if (path === "/naver/status") {
          return {
            configured: true,
            saved: sessionAccount !== null,
            savedUsername: sessionAccount,
            hasSession: sessionAccount !== null,
            sessionAccount,
          };
        }
        // 인증이 끝나도 응답은 한참 오지 않는다 — 이 테스트에서는 영영 오지 않는다.
        if (path === "/naver/login") return new Promise(() => {});
        if (path === "/threads/status") {
          return { saved: false, savedUsername: null, hasSession: false, sessionAccount: null };
        }
        return SETTINGS;
      });
      await render();

      const button = () => container.querySelector<HTMLButtonElement>("#saveNaverCredentials")!;
      await act(async () => {
        setValue(container.querySelector<HTMLInputElement>("#naverId")!, "wuyung027");
        setValue(container.querySelector<HTMLInputElement>("#naverPassword")!, "secret");
      });
      await act(async () => {
        button().click();
      });

      // 기다리는 동안 무엇을 하면 되는지 화면이 말한다.
      expect(button().textContent).toContain("로그인 중");
      expect(container.querySelector(".naver-connect-waiting")?.textContent).toContain(
        "2단계 인증",
      );

      // 사용자가 열린 Chrome에서 인증을 끝냈다 — 서버가 세션 계정을 적는다.
      sessionAccount = "wuyung027";
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3100);
      });

      expect(button().textContent).toContain("저장하고 로그인");
      expect(container.querySelector(".naver-connect-waiting")).toBeNull();
      const badge = container
        .querySelector("#naver-connect-title")
        ?.closest(".settings-card")
        ?.querySelector(".naver-connect-badge")?.textContent;
      expect(badge).toContain("wuyung027 로그인됨");
    } finally {
      vi.useRealTimers();
    }
  });

  it("이미 그 계정으로 로그인돼 있으면 감시하지 않는다", async () => {
    // 세션 계정이 바뀌지 않는 경우다. 그때 감시하면 실패해도 '로그인됐다'고 말하게 된다.
    vi.useFakeTimers();
    try {
      mocks.request.mockImplementation(async (path: string) => {
        if (path === "/naver/status") {
          return {
            configured: true,
            saved: true,
            savedUsername: "win-z",
            hasSession: true,
            sessionAccount: "win-z",
          };
        }
        if (path === "/naver/login") return new Promise(() => {});
        if (path === "/threads/status") {
          return { saved: false, savedUsername: null, hasSession: false, sessionAccount: null };
        }
        return SETTINGS;
      });
      await render();

      await act(async () => {
        setValue(container.querySelector<HTMLInputElement>("#naverPassword")!, "secret");
      });
      await act(async () => {
        container.querySelector<HTMLButtonElement>("#saveNaverCredentials")!.click();
      });

      const before = mocks.request.mock.calls.filter(([path]) => path === "/naver/status").length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(9100);
      });
      const after = mocks.request.mock.calls.filter(([path]) => path === "/naver/status").length;

      expect(after).toBe(before);
      // 응답이 오지 않았으니 버튼은 그대로 기다린다 — 성공했다고 말하지 않는다.
      expect(
        container.querySelector("#saveNaverCredentials")?.textContent,
      ).toContain("로그인 중");
    } finally {
      vi.useRealTimers();
    }
  });

  it("계정을 모르는 세션은 예전처럼 '로그인됨'만 보여준다", async () => {
    // 기록이 없는 옛 프로필이다. 아이디를 지어내지 않는다.
    mocks.request.mockImplementation(async (path: string) => {
      if (path === "/naver/status") {
        return { configured: true, saved: true, savedUsername: "win-z", hasSession: true, sessionAccount: null };
      }
      return SETTINGS;
    });
    await render();

    const badge = container
      .querySelector("#naver-connect-title")
      ?.closest(".settings-card")
      ?.querySelector(".naver-connect-badge")?.textContent;
    expect(badge?.trim()).toBe("로그인됨");
  });

  it("페르소나 카드는 이름과 설명을 각자 칸에 온전히 싣는다", async () => {
    await render();

    const cards = container.querySelectorAll(".persona-card");
    expect(cards).toHaveLength(PERSONAS.length);

    // 이름과 설명이 한 덩어리로 눌리지 않도록 제목/설명 칸이 나뉘어 있어야 한다.
    cards.forEach((card, index) => {
      expect(card.querySelector("strong")?.textContent).toBe(PERSONAS[index].name);
      expect(card.querySelector(".persona-card-desc")?.textContent).toBe(
        PERSONAS[index].description,
      );
      expect(card.querySelector(".persona-card-glyph")).not.toBeNull();
    });
  });

  it("페르소나를 고르면 선택 표시가 붙고 저장 payload에 그 id가 실린다", async () => {
    await render();

    const review = container.querySelector<HTMLButtonElement>('[data-persona-id="p_review"]');
    await act(async () => review?.click());

    expect(review?.getAttribute("aria-pressed")).toBe("true");
    // 선택을 색만으로 알리지 않는다.
    expect(review?.querySelector(".persona-card-check")).not.toBeNull();

    await act(async () => {
      container
        .querySelector<HTMLFormElement>("#settingsForm")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(mocks.request).toHaveBeenCalledWith("/users/user_1/settings", {
      method: "PUT",
      body: {
        hashtagCount: 5,
        articleLength: "medium",
        blendMode: "trend",
        defaultPersona: "p_review",
        autoPostingEnabled: true,
      },
    });
    expect(mocks.store.setSettings).toHaveBeenCalledOnce();
  });

  it("기본값 컨트롤과 커스텀 페르소나 입력이 그대로 저장된다", async () => {
    await render();

    const hashtag = container.querySelector<HTMLInputElement>("#hashtagCount");
    expect(hashtag?.value).toBe("5");

    // '길게' 옵션은 제거됐다 — 원고 길이는 짧게(800~1,200자)·중간(1,800~2,300자) 둘뿐이다.
    const lengthOptions = [
      ...container.querySelectorAll<HTMLButtonElement>(
        '[aria-labelledby="article-length-label"] button',
      ),
    ];
    expect(lengthOptions).toHaveLength(2);
    expect(lengthOptions[0].textContent).toContain("짧게");
    expect(lengthOptions[0].textContent).toContain("800~1,200자");
    expect(lengthOptions[1].textContent).toContain("중간");
    expect(lengthOptions[1].textContent).toContain("1,800~2,300자");

    const shortOption = lengthOptions[0];
    await act(async () => shortOption?.click());
    expect(shortOption?.getAttribute("aria-pressed")).toBe("true");

    const setValue = (id: string, value: string) => {
      const node = container.querySelector<HTMLInputElement | HTMLTextAreaElement>(`#${id}`);
      const proto =
        node instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value")?.set?.call(node, value);
      node?.dispatchEvent(new Event("input", { bubbles: true }));
    };

    await act(async () => {
      setValue("customPersonaName", "IT 전문가");
      setValue("customPersona", "실무 관점으로 씁니다.");
    });

    await act(async () => {
      container
        .querySelector<HTMLFormElement>("#settingsForm")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(mocks.request).toHaveBeenCalledWith("/users/user_1/settings", {
      method: "PUT",
      body: {
        hashtagCount: 5,
        articleLength: "short",
        blendMode: "trend",
        defaultPersona: "p_daily",
        autoPostingEnabled: true,
        customPersonaName: "IT 전문가",
        customPersona: "실무 관점으로 씁니다.",
      },
    });
  });

  it("옛 설정의 '길게'는 중간으로 읽혀 다음 저장부터 medium이 된다", async () => {
    mocks.request.mockResolvedValue({ ...SETTINGS, articleLength: "long" });
    await render();

    const medium = [
      ...container.querySelectorAll<HTMLButtonElement>(
        '[aria-labelledby="article-length-label"] button',
      ),
    ].find((button) => button.textContent?.includes("중간"));
    expect(medium?.getAttribute("aria-pressed")).toBe("true");
  });
});
