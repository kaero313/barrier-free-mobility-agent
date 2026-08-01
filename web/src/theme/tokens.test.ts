import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { designTokens } from "./tokens";

function relativeLuminance(hexColor: string): number {
  const channels = [1, 3, 5].map((start) => Number.parseInt(hexColor.slice(start, start + 2), 16) / 255);
  const [red = 0, green = 0, blue = 0] = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );

  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrast(first: string, second: string): number {
  const values = [relativeLuminance(first), relativeLuminance(second)].sort((a, b) => b - a);
  return ((values[0] ?? 0) + 0.05) / ((values[1] ?? 0) + 0.05);
}

describe("design token baseline", () => {
  it("keeps the required size and motion accessibility values", () => {
    expect(designTokens.meta.accessibilityTarget).toBe("WCAG 2.2 AA");
    expect(designTokens.meta.baseFontSizePx).toBe(16);
    expect(designTokens.sizePx.controlMinHeight).toBe(44);
    expect(designTokens.sizePx.targetMinWidth).toBe(44);
    expect(designTokens.sizePx.targetMinHeight).toBe(44);
    expect(designTokens.motionMs.reduced).toBe(0);
  });

  it("keeps the Assistant layout contract aligned with its task states", () => {
    expect(designTokens.layout.desktopColumns).toBe(12);
    expect(designTokens.layout.assistant).toEqual({
      emptyMaxWidthPx: 880,
      clarificationMaxWidthPx: 960,
      resultMainColumns: 8,
      resultEvidenceColumns: 4,
    });
    expect(
      designTokens.layout.assistant.resultMainColumns +
        designTokens.layout.assistant.resultEvidenceColumns,
    ).toBe(designTokens.layout.desktopColumns);
  });

  it("keeps the build-local token mirror equal to the documented source", () => {
    const documentedPath = fileURLToPath(
      new URL("../../../docs/design-tokens.json", import.meta.url),
    );
    const documentedTokens = JSON.parse(readFileSync(documentedPath, "utf8"));

    expect(designTokens).toEqual(documentedTokens);
  });

  it("keeps body, control and focus contrast above their minimums", () => {
    expect(contrast(designTokens.color.text.primary, designTokens.color.surface.default)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(designTokens.color.text.secondary, designTokens.color.surface.default)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(designTokens.color.border.control, designTokens.color.surface.default)).toBeGreaterThanOrEqual(3);
    expect(contrast(designTokens.color.focus.ring, designTokens.color.surface.default)).toBeGreaterThanOrEqual(3);
    expect(contrast(designTokens.color.focus.ring, designTokens.color.surface.inverse)).toBeGreaterThanOrEqual(3);
  });

  it("publishes the documented CSS variable contract from a build-local file", () => {
    const cssPath = fileURLToPath(new URL("./tokens.css", import.meta.url));
    const css = readFileSync(cssPath, "utf8");

    for (const variable of [
      "--color-surface-canvas",
      "--color-surface-default",
      "--color-text-primary",
      "--color-text-secondary",
      "--color-brand",
      "--color-border-control",
      "--focus-ring",
      "--control-min-height",
      "--radius-medium",
    ]) {
      expect(css).toContain(variable);
    }
  });
});
