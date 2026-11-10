import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const css = readFileSync(fileURLToPath(new URL("./globals.css", import.meta.url)), "utf8");

function themeTokens(selector: ":root" | ".dark"): Map<string, string> {
  const start = css.indexOf(`\n${selector} {`);
  expect(start).toBeGreaterThanOrEqual(0);
  const body = css.slice(start, css.indexOf("\n}", start));
  return new Map(
    Array.from(body.matchAll(/--([\w-]+):\s*oklch\(([^)]*)\)/g), (match) => [
      match[1] ?? "",
      match[2] ?? "",
    ]),
  );
}

// OKLCH → OKLab → linear sRGB (Björn Ottosson). 반투명 토큰은 배경 합성이 필요해 대상에서 뺀다.
function relativeLuminance(oklch: string): number {
  const [lightness = 0, chroma = 0, hue = 0] = oklch.split("/")[0]!.trim().split(/\s+/).map(Number);
  const a = chroma * Math.cos((hue * Math.PI) / 180);
  const b = chroma * Math.sin((hue * Math.PI) / 180);
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const clamp = (value: number) => Math.min(1, Math.max(0, value));
  const red = clamp(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s);
  const green = clamp(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s);
  const blue = clamp(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s);
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrast(tokens: Map<string, string>, foreground: string, background: string): number {
  const first = tokens.get(foreground);
  const second = tokens.get(background);
  expect(first, foreground).toBeDefined();
  expect(second, background).toBeDefined();
  const [lighter = 0, darker = 0] = [relativeLuminance(first!), relativeLuminance(second!)].sort(
    (left, right) => right - left,
  );
  return (lighter + 0.05) / (darker + 0.05);
}

const TEXT_PAIRS = [
  ["foreground", "background"],
  ["card-foreground", "card"],
  ["muted-foreground", "background"],
  ["muted-foreground", "muted"],
  ["muted-foreground", "card"],
  ["primary-foreground", "primary"],
  ["secondary-foreground", "secondary"],
  ["accent-foreground", "accent"],
  ["destructive", "background"],
  ["destructive", "card"],
  ["sidebar-foreground", "sidebar"],
  ["sidebar-accent-foreground", "sidebar-accent"],
  ["sidebar-primary-foreground", "sidebar-primary"],
] as const;

// 키보드 포커스 실선(globals.css 의 :focus-visible)이 놓이는 표면들이다.
const FOCUS_SURFACES = ["background", "card", "muted", "sidebar", "sidebar-accent"] as const;

// 결과 상태 아이콘 색과 그 아이콘이 놓이는 카드·요약 상자 표면이다. 아이콘은 비텍스트 대비 3:1 이상이다.
const STATUS_ICON_PAIRS = ["success", "warning", "info", "destructive"].flatMap((color) =>
  ["card", "muted"].map((surface) => [color, surface] as const),
);

describe.each([":root", ".dark"] as const)("theme %s", (selector) => {
  const tokens = themeTokens(selector);

  it.each(TEXT_PAIRS)("keeps %s on %s at WCAG AA text contrast", (foreground, background) => {
    expect(contrast(tokens, foreground, background)).toBeGreaterThanOrEqual(4.5);
  });

  it.each(FOCUS_SURFACES)("keeps the focus ring visible on %s", (surface) => {
    expect(contrast(tokens, "ring", surface)).toBeGreaterThanOrEqual(3);
  });

  it.each(STATUS_ICON_PAIRS)("keeps the %s status icon visible on %s", (color, surface) => {
    expect(contrast(tokens, color, surface)).toBeGreaterThanOrEqual(3);
  });
});
