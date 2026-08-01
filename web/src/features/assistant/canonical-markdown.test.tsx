// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CanonicalMarkdown } from "./canonical-markdown";

describe("CanonicalMarkdown", () => {
  it("renders canonical GFM as semantic content without enabling raw HTML", () => {
    const { container } = render(
      <CanonicalMarkdown
        message={[
          "**엘리베이터 동선을 출발 전에 확인해 주세요.**",
          "",
          "### 역별 접근성 체크",
          "",
          "| 역 | 상태 |",
          "|---|---|",
          "| 홍대입구역 | 운행 중 |",
          "",
          "<script>unsafe()</script>",
        ].join("\n")}
      />,
    );

    expect(screen.getByText("엘리베이터 동선을 출발 전에 확인해 주세요.").tagName).toBe("STRONG");
    expect(screen.getByRole("heading", { name: "역별 접근성 체크", level: 3 })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "조회 응답 표" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("<script>unsafe()</script>")).toBeInTheDocument();
  });
});
