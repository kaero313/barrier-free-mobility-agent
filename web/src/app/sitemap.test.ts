import { describe, expect, it } from "vitest";

import sitemap from "./sitemap";

describe("public sitemap", () => {
  it("contains only approved public product routes", () => {
    const paths = sitemap().map((entry) => new URL(entry.url).pathname);

    expect(paths).toEqual(["/", "/assistant", "/how-it-works", "/status", "/accessibility"]);
    expect(paths.some((path) => path.startsWith("/ops"))).toBe(false);
    expect(paths.some((path) => path.startsWith("/api"))).toBe(false);
  });
});
