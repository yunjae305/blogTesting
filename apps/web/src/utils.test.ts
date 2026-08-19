import { afterEach, describe, expect, it, vi } from "vitest";

import {
  articleHtmlForClipboard,
  articleMarkdownForClipboard,
  collectReferenceMaterials,
  downloadPostImages,
  referenceUrlProblem,
  markdownFromHtml,
  MAX_REFERENCE_FILE_BYTES,
  inlineSegments,
  previewBlocks,
} from "./utils";
import type { FinalPost } from "./api/types";

describe("reference materials", () => {
  it("sends a PDF as a data URL with its filename", async () => {
    const file = new File(["pdf body"], "report.pdf", { type: "application/pdf" });

    const materials = await collectReferenceMaterials({ text: "", url: "", files: [file] });

    expect(materials).toHaveLength(1);
    expect(materials[0]).toMatchObject({ type: "PDF", name: "report.pdf" });
    expect(materials[0].value).toMatch(/^data:application\/pdf;base64,/);
  });

  it("rejects an oversized file before reading it", async () => {
    const file = new File([new Uint8Array(MAX_REFERENCE_FILE_BYTES + 1)], "large.pdf", {
      type: "application/pdf",
    });

    await expect(
      collectReferenceMaterials({ text: "", url: "", files: [file] }),
    // 숫자는 상수에서 읽는다 — 상한을 올릴 때(2026-08-11 5→20MB) 테스트가 옛말을
    // 못박고 있으면 그때마다 함께 고쳐야 한다. 파일당·합계 상한이 같은 값이라
    // 어느 쪽 문구가 먼저 걸리는지는 정하지 않는다 — 중요한 것은 **읽기 전에 막는
    // 것**이고, 그것이 이 시험의 이름이다.
    ).rejects.toThrow(`${MAX_REFERENCE_FILE_BYTES / 1024 / 1024}MB`);
  });

  it("keeps separately added memos as separate reference materials", async () => {
    const materials = await collectReferenceMaterials({
      text: ["첫 번째 메모", "  두 번째 메모  ", ""],
      url: "",
      files: [],
    });

    expect(materials).toEqual([
      { type: "TEXT", value: "첫 번째 메모" },
      { type: "TEXT", value: "두 번째 메모" },
    ]);
  });
});

describe("preview blocks", () => {
  function post(overrides: Partial<FinalPost>): FinalPost {
    return {
      title: "제목",
      body: "본문",
      hashtags: [],
      htmlContent: "<article><h1>제목</h1></article>",
      ...overrides,
    };
  }

  it("굵게를 지우지 않고 미리보기까지 넘긴다", () => {
    // 2026-08-03 실측: plainText가 **를 지워 미리보기에 굵게가 도달할 방법이 없었다.
    const markdown = "# 제목\n\n## 소제목\n\n선택이 갈리는 건 **부위와 구성**입니다.";

    const blocks = previewBlocks(post({ markdownContent: markdown }));
    const paragraph = blocks.find((block) => block.kind === "paragraph");

    if (paragraph?.kind !== "paragraph") throw new Error("문단 블록이 없다");
    expect(paragraph.text).toContain("**부위와 구성**");
    expect(inlineSegments(paragraph.text)).toEqual([
      { text: "선택이 갈리는 건 ", bold: false },
      { text: "부위와 구성", bold: true },
      { text: "입니다.", bold: false },
    ]);
  });

  it("HTML 본문에서도 굵게가 살아남는다", () => {
    const html = "<article><p>핵심은 <strong>부위</strong>입니다.</p></article>";

    const blocks = previewBlocks(post({ markdownContent: "", htmlContent: html }));
    const paragraph = blocks.find((block) => block.kind === "paragraph");

    if (paragraph?.kind !== "paragraph") throw new Error("문단 블록이 없다");
    expect(paragraph.text).toBe("핵심은 **부위**입니다.");
  });

  it("소제목 단계를 버리지 않는다", () => {
    const markdown = "# 제목\n\n## 큰 소제목\n\n본문.\n\n### 작은 소제목\n\n본문.";

    const headings = previewBlocks(post({ markdownContent: markdown })).filter(
      (block) => block.kind === "heading",
    );

    expect(headings.map((h) => (h.kind === "heading" ? h.level : 0))).toEqual([2, 3]);
  });

  it("기울임·형광펜 표시는 화면에 글자로 새지 않는다", () => {
    const markdown = "# 제목\n\n*기울임*과 ==형광펜==은 표시만 걷어낸다.";

    const blocks = previewBlocks(post({ markdownContent: markdown }));
    const paragraph = blocks.find((block) => block.kind === "paragraph");

    if (paragraph?.kind !== "paragraph") throw new Error("문단 블록이 없다");
    expect(paragraph.text).toBe("기울임과 형광펜은 표시만 걷어낸다.");
  });

  it("reads a markdown table as a table, not as pipe characters", () => {
    const markdown = [
      "# 제목",
      "## 좌석 비교",
      "| 구분 | 스탠딩석 | 지정석 |\n|---|---|---|\n| 자리 결정 | 입장 번호 순 | 예매 시 좌석 확정 |",
    ].join("\n\n");

    const blocks = previewBlocks(post({ markdownContent: markdown }));
    const table = blocks.find((block) => block.kind === "table");

    expect(table).toBeDefined();
    if (table?.kind !== "table") throw new Error("표 블록이 없다");
    expect(table.header).toEqual(["구분", "스탠딩석", "지정석"]);
    expect(table.rows).toEqual([["자리 결정", "입장 번호 순", "예매 시 좌석 확정"]]);
    // 파이프가 글자 그대로 새는 문단이 남아 있으면 안 된다.
    expect(blocks.some((block) => block.kind === "paragraph" && block.text.includes("|"))).toBe(
      false,
    );
  });

  it("rejoins a table whose rows arrived separated by blank lines", () => {
    // 이미 저장된 원고가 이 모양이다 — 행마다 빈 줄이 들어가 문단으로 흩어져 있었다.
    const markdown = [
      "# 제목",
      "| 구분 | 스탠딩석 |",
      "|---|---|",
      "| 체력 부담 | 오래 서 있음 |",
      "이어지는 문단입니다.",
    ].join("\n\n");

    const blocks = previewBlocks(post({ markdownContent: markdown }));
    const table = blocks.find((block) => block.kind === "table");

    expect(table).toBeDefined();
    if (table?.kind !== "table") throw new Error("표 블록이 없다");
    expect(table.rows).toEqual([["체력 부담", "오래 서 있음"]]);
    expect(blocks.at(-1)).toEqual({ kind: "paragraph", text: "이어지는 문단입니다." });
  });

  it("does not turn a lone piped sentence into a table", () => {
    const blocks = previewBlocks(post({ markdownContent: "# 제목\n\n| 한 줄짜리 문단 |" }));

    expect(blocks.some((block) => block.kind === "table")).toBe(false);
  });

  it("keeps an html table as a table when there is no markdown", () => {
    const html =
      "<article><h1>제목</h1><table><thead><tr><th>구분</th><th>값</th></tr></thead>" +
      "<tbody><tr><td>요금</td><td>무료</td></tr></tbody></table></article>";

    const blocks = previewBlocks(post({ htmlContent: html }));
    const table = blocks.find((block) => block.kind === "table");

    expect(table).toBeDefined();
    if (table?.kind !== "table") throw new Error("표 블록이 없다");
    expect(table.header).toEqual(["구분", "값"]);
    expect(table.rows).toEqual([["요금", "무료"]]);
  });

  it("스티커 마커는 이름 칩으로 바뀌고 원문은 새지 않는다", () => {
    // 2026-08-10 사용자 결정: 화면에는 스티커 이름만, 실제 스티커는 네이버 발행 때.
    const markdown = "# 제목\n\n첫 문단.\n\n[[STICKER: 뿌듯]]\n\n둘째 문단.";

    const blocks = previewBlocks(post({ markdownContent: markdown }));
    const chip = blocks.find((block) => block.kind === "caption");

    if (chip?.kind !== "caption") throw new Error("스티커 칩이 없다");
    expect(chip.text).toContain("스티커: 뿌듯");
    expect(
      blocks.some((block) => block.kind === "paragraph" && block.text.includes("STICKER")),
    ).toBe(false);
  });

  it("HTML 경로에서도 스티커 마커는 칩이 된다", () => {
    const html =
      "<article><h1>제목</h1><p>첫 문단.</p><p>[[STICKER: 응원]]</p><p>둘째 문단.</p></article>";

    const blocks = previewBlocks(post({ markdownContent: "", htmlContent: html }));
    const chip = blocks.find((block) => block.kind === "caption");

    if (chip?.kind !== "caption") throw new Error("스티커 칩이 없다");
    expect(chip.text).toContain("스티커: 응원");
    expect(
      blocks.some((block) => block.kind === "paragraph" && block.text.includes("STICKER")),
    ).toBe(false);
  });
});

describe("사진 출처 캡션", () => {
  const SOURCE = "사진 출처: imgnews.naver.net (http://imgnews.naver.net/image/0001121334_001.jpg)";

  function post(overrides: Partial<FinalPost>): FinalPost {
    return {
      title: "제목",
      body: "본문",
      hashtags: [],
      htmlContent: "<article><h1>제목</h1></article>",
      ...overrides,
    };
  }

  it("사진 바로 아래 이탤릭 한 줄은 본문이 아니라 그 사진의 설명이다", () => {
    const markdown = [
      "![호날두 썸네일](data:image/jpeg;base64,AAA)",
      `*${SOURCE}*`,
      "",
      "# 제목",
      "",
      "도입 문단입니다.",
    ].join("\n");

    const blocks = previewBlocks(post({ markdownContent: markdown }));

    expect(blocks[0].kind).toBe("image");
    expect(blocks[1]).toEqual({ kind: "caption", text: SOURCE });
    // 출처가 본문 문단으로도 남으면 같은 줄이 두 번 보인다.
    expect(blocks.filter((block) => block.kind === "paragraph")).toEqual([
      { kind: "paragraph", text: "도입 문단입니다." },
    ]);
  });

  it("서버 HTML의 visual-caption 문단도 같은 캡션으로 읽는다", () => {
    const html =
      '<article><figure><img src="data:image/jpeg;base64,AAA" alt="사진" /></figure>' +
      `<p class="visual-caption"><em>${SOURCE}</em></p><p>도입 문단입니다.</p></article>`;

    const blocks = previewBlocks(post({ htmlContent: html }));

    expect(blocks[1]).toEqual({ kind: "caption", text: SOURCE });
  });

  it("이미지와 상관없는 이탤릭 문단은 그대로 본문이다", () => {
    const markdown = ["# 제목", "", "*강조한 한 줄입니다.*", "", "다음 문단."].join("\n");

    const blocks = previewBlocks(post({ markdownContent: markdown }));

    expect(blocks.some((block) => block.kind === "caption")).toBe(false);
    expect(blocks).toContainEqual({ kind: "paragraph", text: "강조한 한 줄입니다." });
  });

  it("표지 사진 뒤에 오는 제목도 본문에서 뺀다", () => {
    // 실사례(2026-08-03, 'MBC 놀면 뭐하니…'): 마크다운이 표지 사진으로 시작하고 제목은
    // 그다음 줄이라, '첫 블록만' 보던 검사에 걸리지 않아 표지 아래에 제목이 한 번 더
    // 찍혔다. 미리보기는 제목을 헤더로 따로 그리므로 본문에 남으면 두 번 나온다.
    const markdown = [
      "![표지](data:image/jpeg;base64,AAA)",
      "",
      "# 제목",
      "",
      "도입 문단입니다.",
      "",
      "## 진짜 소제목",
    ].join("\n");

    const blocks = previewBlocks(post({ markdownContent: markdown }));

    expect(blocks.filter((block) => block.kind === "heading")).toEqual([
      { kind: "heading", text: "진짜 소제목", level: 2 },
    ]);
    expect(blocks[0]).toEqual({ kind: "image", src: "data:image/jpeg;base64,AAA", alt: "표지" });
  });

  it("HTML 경로에서도 제목이 본문에 남지 않는다", () => {
    const html =
      '<figure><img src="data:image/jpeg;base64,AAA" alt="표지" /></figure>' +
      "<article><h1>제목</h1><p>도입 문단입니다.</p><h2>진짜 소제목</h2></article>";

    const blocks = previewBlocks(post({ markdownContent: "", htmlContent: html }));

    expect(blocks.filter((block) => block.kind === "heading").map((b) => b.text)).toEqual([
      "진짜 소제목",
    ]);
  });

  it("캡션이 없는 사진은 다음 문단을 캡션으로 삼키지 않는다", () => {
    const markdown = ["![사진](data:image/jpeg;base64,AAA)", "", "본문 첫 문단입니다."].join("\n");

    const blocks = previewBlocks(post({ markdownContent: markdown }));

    expect(blocks).toEqual([
      { kind: "image", src: "data:image/jpeg;base64,AAA", alt: "사진" },
      { kind: "paragraph", text: "본문 첫 문단입니다." },
    ]);
  });
});

describe("article export", () => {
  it("웹에서 가져온 사진은 복사본에 원본 주소로 실린다", () => {
    // 로컬 서버 주소(localhost)는 이 PC에서만 열린다 — 네이버가 서버에서 이미지를
    // 끌어갈 때도, 벨로그·티스토리에 붙여넣을 때도 죽은 링크였다(2026-08-10 실사례:
    // '존재하지 않는 이미지입니다'). 원본 주소가 저장돼 있으면 그것이 먼저다.
    const html = articleHtmlForClipboard(
      {
        htmlContent:
          '<article><p><img src="data:image/jpeg;base64,AAA" alt="웹 사진" />' +
          '<img src="data:image/jpeg;base64,BBB" alt="생성 사진" /></p></article>',
        images: [
          { sourceUrl: "https://imgnews.naver.net/image/1.jpg" },
          { sourceUrl: null },
        ],
      },
      "post-1",
    );

    expect(html).toContain('src="https://imgnews.naver.net/image/1.jpg"');
    // 원본이 없는 이미지는 예전처럼 로컬 엔드포인트다.
    expect(html).toContain("/posts/post-1/images/1");
  });

  it("마크다운 복사도 같은 원본 주소를 쓴다", () => {
    const markdown = articleMarkdownForClipboard(
      {
        title: "제목",
        htmlContent:
          '<article><h1>제목</h1><p><img src="data:image/jpeg;base64,AAA" alt="웹 사진" /></p></article>',
        images: [{ sourceUrl: "https://i.ytimg.com/vi/abc/maxresdefault.jpg" }],
      },
      "post-1",
    );

    expect(markdown).toContain("https://i.ytimg.com/vi/abc/maxresdefault.jpg");
    expect(markdown).not.toContain("/posts/post-1/images/0");
  });

  it("keeps semantic formatting and appends hashtags", () => {
    const html = articleHtmlForClipboard(
      { htmlContent: '<article><h1>T</h1><p><strong>핵심</strong> 설명</p></article>', hashtags: ["AI"] },
      "post-1",
    );
    expect(html).toContain("<strong>핵심</strong>");
    expect(html).toContain("#AI");

    expect(markdownFromHtml(html, "T")).toContain("**핵심** 설명");
  });

  it("제목을 h2로 낮춰 붙여넣기에서 살아남게 한다", () => {
    // 실사례(2026-08-03): 'HTML(서식) 복사'로 복사해 네이버에 붙여넣으면 제목 줄이
    // 통째로 사라졌다. 복사본에는 <h1>이 들어 있었지만 스마트에디터가 h1을 버린다.
    const html = articleHtmlForClipboard(
      { htmlContent: "<article><h1>제목입니다</h1><p>본문.</p></article>" },
      "post-1",
    );

    expect(html).not.toContain("<h1");
    expect(html).toContain("<h2>제목입니다</h2>");
  });

  it("제목이 표지 이미지보다 앞에 온다", () => {
    // 실사례(2026-08-03): 표지 <figure>가 <article> 밖 맨 앞에 있어서, 붙여넣으면
    // '표지 이미지 → 제목' 순이 되어 제목이 이미지 아래로 밀렸다.
    const html = articleHtmlForClipboard(
      {
        htmlContent:
          '<figure class="blog-media blog-media--cover"><img src="data:image/jpeg;base64,AAA" alt="표지" /></figure>' +
          "<article><h1>제목입니다</h1><h2>소제목</h2><p>본문.</p></article>",
      },
      "post-1",
    );

    expect(html.indexOf("<h2>제목입니다</h2>")).toBeLessThan(html.indexOf("<figure>"));
    // 본문 소제목은 제자리에 그대로 남는다 — 옮기는 것은 제목 하나뿐이다.
    expect(html.indexOf("<h2>소제목</h2>")).toBeGreaterThan(html.indexOf("<figure>"));
  });

  it("벨로그용 평문에도 공개 이미지 주소와 출처를 남긴다", () => {
    const post = {
      title: "사진이 있는 글",
      htmlContent:
        '<article><figure><img src="data:image/jpeg;base64,AAA" alt="본문 사진" /></figure>' +
        '<p class="visual-caption"><em>출처 : "example.com"</em></p><p>본문입니다.</p></article>',
      hashtags: ["사진"],
    };

    const markdown = articleMarkdownForClipboard(post, "post-1");

    expect(markdown).toContain("![본문 사진](");
    expect(markdown).toContain("/posts/post-1/images/0)");
    expect(markdown).not.toContain("data:image");
    expect(markdown).toContain('출처 : "example.com"');
    expect(markdown).toContain("#사진");
  });

  it("대표 이미지가 article 앞에 있어도 article 안의 본문을 모두 보존한다", () => {
    const post = {
      title: "본문 보존",
      htmlContent:
        '<figure><img src="data:image/jpeg;base64,COVER" alt="대표 이미지" /></figure>' +
        '<p class="visual-caption"><em>대표 출처</em></p>' +
        "<article><h1>본문 보존</h1><p>첫 문단입니다.</p><h2>중간 소제목</h2>" +
        '<figure><img src="data:image/jpeg;base64,BODY" alt="본문 이미지" /></figure>' +
        '<p class="visual-caption"><em>본문 출처</em></p><p>마지막 문단입니다.</p></article>',
      hashtags: [],
    };

    const markdown = articleMarkdownForClipboard(post, "post-2");

    const expectedInOrder = [
      "![대표 이미지]",
      "대표 출처",
      "첫 문단입니다.",
      "## 중간 소제목",
      "![본문 이미지]",
      "본문 출처",
      "마지막 문단입니다.",
    ];
    expectedInOrder.reduce((previousIndex, text) => {
      const currentIndex = markdown.indexOf(text);
      expect(currentIndex).toBeGreaterThan(previousIndex);
      return currentIndex;
    }, -1);
    expect(markdown.match(/!\[/g)).toHaveLength(2);
  });

  it("스티커 마커는 HTML·마크다운 복사본 어디에도 남지 않는다", () => {
    // 마커는 네이버 자동발행 전용 자리 표식이다 — 복사해 붙여넣는 경로(네이버 수동,
    // 벨로그, 티스토리)에는 스티커가 없으므로 글자로 남으면 그대로 노출 사고다.
    const post = {
      title: "제목",
      htmlContent:
        "<article><h1>제목</h1><p>첫 문단.</p><p>[[STICKER: 뿌듯]]</p>" +
        "<p>둘째 문단.</p></article>",
      hashtags: [],
    };

    const html = articleHtmlForClipboard(post, "post-1");
    const markdown = articleMarkdownForClipboard(post, "post-1");

    expect(html).not.toContain("STICKER");
    expect(html).toContain("첫 문단.");
    expect(markdown).not.toContain("STICKER");
    expect(markdown).toContain("둘째 문단.");
  });
});


describe("이미지 파일로 내려받기", () => {
  /**
   * 확장자는 실제 값인 dataUrl에서 읽는다. mimeType은 원고에 적힌 메타데이터라 저장된
   * 바이트와 어긋날 수 있다 — 서버가 이미지를 900px JPEG으로 다시 구우면서 옛 글의
   * mimeType은 image/png인 채로 남았다(2026-08-06). 그대로 두면 JPEG 파일이 .png라는
   * 이름으로 내려가 네이버 업로더에 그 이름 그대로 올라간다.
   */
  const capture = () => {
    const names: string[] = [];
    const original = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const element = original(tag) as HTMLAnchorElement;
      if (tag === "a") {
        element.click = () => names.push(element.download);
      }
      return element;
    });
    return names;
  };

  afterEach(() => vi.restoreAllMocks());

  const image = (dataUrl: string, mimeType: string) =>
    ({ dataUrl, mimeType, altText: "사진" }) as never;

  it("mimeType이 저장된 바이트와 어긋나도 dataUrl을 따른다", () => {
    const names = capture();

    downloadPostImages([image("data:image/jpeg;base64,AAAA", "image/png")], "제목");

    expect(names).toEqual(["제목-1.jpg"]);
  });

  it("dataUrl이 없으면 mimeType으로 물러난다", () => {
    const names = capture();

    downloadPostImages([image("", "image/png")], "제목");

    expect(names).toEqual(["제목-1.png"]);
  });
});


describe("참고 URL 유효성", () => {
  /**
   * 예전에는 제출할 때 한 번만 보고 "http 또는 https로 시작해야 합니다"라고 알렸다.
   * 다 적고 다음을 누른 뒤에야 알게 되고, 어느 칸이 문제인지도 말해 주지 않았다
   * (2026-08-07 사용자 요청).
   */
  it("쓸 수 있는 주소는 통과한다", () => {
    for (const url of [
      "https://example.com",
      "https://example.com/report?q=1#a",
      "http://blog.naver.com/winz/223",
    ]) {
      expect(referenceUrlProblem(url), url).toBeNull();
    }
  });

  it("빈 칸은 잘못 적은 것이 아니다", () => {
    // 칸을 미리 열어 두므로 빈 칸이 늘 하나 있다. 그것까지 오류로 표시하면
    // 아무것도 안 적었는데 빨간 글씨가 뜬다.
    expect(referenceUrlProblem("")).toBeNull();
    expect(referenceUrlProblem("   ")).toBeNull();
  });

  it("왜 안 되는지 말해 준다", () => {
    expect(referenceUrlProblem("example.com")).toContain("http");
    expect(referenceUrlProblem("https://exa mple.com")).toContain("공백");
    // 열어 볼 수 없는 주소는 글의 근거가 되지 못한다.
    expect(referenceUrlProblem("https://localhost:3000")).toContain("열 수 있는");
    expect(referenceUrlProblem("ftp://example.com")).toContain("http");
  });

  it("비공개 주소와 자격증명이 든 주소를 외부 AI로 보내지 않는다", () => {
    for (const url of [
      "https://user:password@example.com/report",
      "https://127.0.0.1/report",
      "https://127.1/report",
      "https://0177.0.0.1/report",
      "https://0x7f.0.0.1/report",
      "https://[::1]/report",
      "https://service.internal/report",
    ]) {
      expect(referenceUrlProblem(url), url).not.toBeNull();
    }
    for (const url of [
      "https://example.com/report?access_token=secret",
      "https://example.com/report#code=temporary-secret",
      "https://example.com/report?X-Amz-Signature=signed",
    ]) {
      expect(referenceUrlProblem(url), url).toContain("첨부");
    }
  });

  it("모으는 쪽도 같은 판정을 쓴다", async () => {
    // 두 곳이 어긋나면 화면은 통과인데 저장이 막힌다.
    await expect(
      collectReferenceMaterials({ text: "", url: "example.com", files: [] }),
    ).rejects.toThrow("참고 URL");
  });
});
