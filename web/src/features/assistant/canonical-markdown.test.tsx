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

  it("turns only standalone br tags inside table cells into line breaks", () => {
    const { container } = render(
      <CanonicalMarkdown
        message={[
          "| 역 | 확인된 정보 |",
          "|---|---|",
          "| 홍대입구역 | 운행 중: 8번 출입구<br>운행 중: 4번 출입구<br />출구까지 연결 확인 |",
          "| 삼성역 | <b>강조</b> 운행 중 |",
        ].join("\n")}
      />,
    );

    const cell = screen.getByText("운행 중: 8번 출입구", { exact: false });
    expect(cell.tagName).toBe("TD");
    expect(cell.querySelectorAll("br")).toHaveLength(2);
    expect(cell).not.toHaveTextContent("<br");
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText("<b>", { exact: false })).toBeInTheDocument();
  });

  it("gathers the trailing checked-time and caution sections into one basis box", () => {
    const { container } = render(
      <CanonicalMarkdown
        message={[
          "**휠체어 이동 동선을 추가로 확인하세요.**",
          "",
          "### 역별 확인 결과",
          "",
          "| 역 | 상태 |",
          "|---|---|",
          "| 홍대입구역 | 운행 중 |",
          "",
          "### 기준 시각",
          "",
          "- 조회 시각: 2026년 10월 15일 14:51.",
          "",
          "### 주의사항",
          "",
          "> 엘리베이터 운행 상태는 바뀔 수 있으니 출발 직전 재확인하세요.",
          "",
          "- 공공데이터 기준이라 현장 상황과 다를 수 있습니다.",
        ].join("\n")}
      />,
    );

    const box = screen.getByRole("heading", { name: "기준 시각", level: 3 }).closest("section");
    expect(container.querySelectorAll("section")).toHaveLength(1);
    // 소제목은 제목으로 남고, 두 구역의 내용이 한 상자에 모인다.
    expect(box).toContainElement(screen.getByRole("heading", { name: "주의사항", level: 3 }));
    expect(box).toHaveTextContent("조회 시각: 2026년 10월 15일 14:51.");
    expect(box).toHaveTextContent("출발 직전 재확인하세요.");
    expect(box).not.toContainElement(screen.getByRole("heading", { name: "역별 확인 결과" }));
    expect(box).not.toContainElement(screen.getByRole("table"));
  });

  it("leaves the sections in place when they do not end the answer or the text is compact", () => {
    const message = [
      "### 기준 시각",
      "",
      "- 조회 시각: 오늘.",
      "",
      "### 다른 경로",
      "",
      "- 비교할 다른 경로가 없습니다.",
    ].join("\n");
    const { container, rerender } = render(<CanonicalMarkdown message={message} />);
    expect(container.querySelector("section")).toBeNull();

    rerender(<CanonicalMarkdown compact message={"### 주의사항\n\n- 현장과 다를 수 있습니다."} />);
    expect(container.querySelector("section")).toBeNull();
  });

  it("marks only the answer contract's labelled lines with a status icon", () => {
    render(
      <CanonicalMarkdown
        message={[
          "**휠체어 이동 동선을 추가로 확인하세요.**",
          "",
          "- **확인됨:** 엘리베이터는 현재 운행 중입니다.",
          "- **확인 안 됨:** 출구까지 연결은 나오지 않습니다.",
          "- **권장 확인:**",
          "  - 역무실에 문의하세요.",
          "- **참고:** 라벨이 아닌 굵은 글씨입니다.",
          "- 굵은 글씨 없는 항목입니다.",
        ].join("\n")}
      />,
    );

    const item = (label: string) => screen.getByText(label).closest("li") as HTMLElement;
    for (const label of ["확인됨:", "확인 안 됨:", "권장 확인:"]) {
      expect(item(label).querySelector(":scope > svg[aria-hidden='true']")).not.toBeNull();
    }
    // 여러 할 일은 라벨 아래 목록으로 남는다.
    expect(item("권장 확인:")).toHaveTextContent("역무실에 문의하세요.");
    expect(item("참고:").querySelector("svg")).toBeNull();
    expect(screen.getByText("굵은 글씨 없는 항목입니다.").querySelector("svg")).toBeNull();
  });
});
