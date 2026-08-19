import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * 프런트가 부르는 API 경로가 Vite 프록시 목록에 다 있는가.
 *
 * 빠지면 **조용히** 깨진다: Vite가 index.html을 돌려주고, 프런트는 그것을 JSON으로 읽다
 * 실패한다. API는 호출된 적조차 없다. 실제로 두 번 났다 — `/naver`(과거), 그리고
 * `/posting`(2026-08-04, 2단계 인증 코드 입력창이 영영 안 떴다).
 *
 * 그래서 목록을 눈으로 지키지 않고 여기서 대조한다.
 */
function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    return /\.(ts|tsx)$/.test(entry) && !/\.test\.tsx?$/.test(entry) ? [path] : [];
  });
}

function proxiedRoutes(): string[] {
  const config = readFileSync(join(process.cwd(), "vite.config.ts"), "utf-8");
  const list = /const API_ROUTES = \[([\s\S]*?)\]/.exec(config);
  if (!list) throw new Error("vite.config.ts에서 API_ROUTES를 찾지 못했습니다.");
  return [...list[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
}

/** request("/posting/verification") 같은 호출에서 최상위 경로만 뽑는다. */
function calledTopLevelPaths(): Set<string> {
  const calls = new Set<string>();
  for (const file of sourceFiles(join(process.cwd(), "src"))) {
    const source = readFileSync(file, "utf-8");
    for (const match of source.matchAll(/request(?:Public|WithToken)?<[^>]*>\(\s*[`"'](\/[\w-]+)/g)) {
      calls.add(match[1]);
    }
    // 제네릭 없이 부르는 호출(request("/posting/verification", …))도 잡는다.
    for (const match of source.matchAll(/request(?:Public|WithToken)?\(\s*[`"'](\/[\w-]+)/g)) {
      calls.add(match[1]);
    }
  }
  return calls;
}

describe("Vite 프록시 목록", () => {
  it("프런트가 부르는 모든 API 경로를 덮는다", () => {
    const proxied = proxiedRoutes();
    const missing = [...calledTopLevelPaths()].filter((path) => !proxied.includes(path));

    expect(missing, `vite.config.ts의 API_ROUTES에 없는 경로: ${missing.join(", ")}`).toEqual([]);
  });

  it("이번에 문제가 된 /posting을 실제로 담고 있다", () => {
    expect(proxiedRoutes()).toContain("/posting");
  });
});
