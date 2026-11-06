import {
  CircleArrowRightIcon,
  CircleCheckIcon,
  CircleHelpIcon,
  CircleXIcon,
  type LucideIcon,
} from "lucide-react";
import type { Components, ExtraProps } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

type MarkdownNode = { type: string; value?: string; children?: MarkdownNode[] };

const LINE_BREAK_TAG = /^<br\s*\/?>$/i;

// GFM 표 셀은 줄바꿈을 담을 수 없어 Agent 문서가 셀 안에서 `<br>`을 쓴다. 원시 HTML은 계속
// 글자로 보여 주고, 정확히 `<br>` 태그 하나인 노드만 markdown 줄바꿈 노드로 바꾼다.
function remarkLineBreakTags() {
  const visit = (node: MarkdownNode) => {
    node.children?.forEach((child, index, siblings) => {
      if (child.type === "html" && LINE_BREAK_TAG.test(child.value?.trim() ?? "")) {
        siblings[index] = { type: "break" };
        return;
      }
      visit(child);
    });
  };
  return (tree: MarkdownNode) => visit(tree);
}

type MarkdownElement = NonNullable<ExtraProps["node"]>;

// MCP 답변 규칙은 주의 결과의 핵심 결론을 이용 제한·확인됨·확인 안 됨·권장 확인 라벨 줄로 나눈다.
// 문장은 해석하지 않고, 목록 항목이 이 굵은 라벨로 시작할 때만 상태 아이콘을 앞에 붙인다.
const LABEL_MARKERS = new Map<string, Readonly<{ icon: LucideIcon; className: string }>>([
  ["이용 제한", { icon: CircleXIcon, className: "text-destructive" }],
  ["확인됨", { icon: CircleCheckIcon, className: "text-success" }],
  ["확인 안 됨", { icon: CircleHelpIcon, className: "text-warning" }],
  ["권장 확인", { icon: CircleArrowRightIcon, className: "text-info" }],
]);

function leadingLabel(node: MarkdownElement | undefined): string | null {
  let current = node;
  // 느슨한 목록은 항목 내용을 문단(p)으로 한 번 더 감싼다.
  for (let depth = 0; current !== undefined && depth < 2; depth += 1) {
    const first = current.children.find(
      (child) => child.type !== "text" || child.value.trim() !== "",
    );
    if (first?.type !== "element") return null;
    if (first.tagName === "strong") {
      return first.children
        .map((child) => (child.type === "text" ? child.value : ""))
        .join("")
        .replace(/:\s*$/, "")
        .trim();
    }
    current = first.tagName === "p" ? first : undefined;
  }
  return null;
}

type MarkdownChild = MarkdownElement["children"][number];

// MCP 답변 규칙의 마지막 두 구역(조회 시각, 짧은 주의사항)은 답에 덧붙는 근거다. 답변이 이 소제목들로
// 끝날 때만 작은 글씨의 근거 상자 하나로 묶어, 짧은 내용이 큰 소제목 구역으로 화면 아래를 길게
// 차지하지 않게 한다. 소제목은 그대로 두어 제목으로 훑어 읽을 수 있다.
const BASIS_HEADINGS = new Set(["기준 시각", "주의사항"]);
const SECTION_HEADINGS = new Set(["h1", "h2", "h3"]);
const BASIS_BOX = "mt-2 flex flex-col gap-3 rounded-lg border bg-muted/40 px-4 py-3 text-sm leading-6";
const BASIS_GROUP = [
  "grid gap-x-4 gap-y-1 sm:grid-cols-[5rem_minmax(0,1fr)]",
  "[&>h3]:text-sm [&>h3]:text-muted-foreground",
].join(" ");
const BASIS_BODY = [
  "flex min-w-0 flex-col gap-1.5 [&_blockquote]:pl-3 [&_ul]:ml-4 [&_ul]:space-y-0.5",
  // 한 줄뿐인 목록은 글머리표 없이 문장으로 둔다.
  "[&_ul:has(>li:only-child)]:ml-0 [&_ul:has(>li:only-child)]:list-none",
].join(" ");

function plainText(node: MarkdownChild): string {
  if (node.type === "text") return node.value;
  return node.type === "element" ? node.children.map(plainText).join("") : "";
}

function hastElement(tagName: string, className: string, children: MarkdownChild[]): MarkdownElement {
  return { type: "element", tagName, properties: { className: className.split(" ") }, children };
}

function rehypeAnswerBasis() {
  return (tree: { children: MarkdownChild[] }) => {
    const headings = tree.children.flatMap((child, index) =>
      child.type === "element" && SECTION_HEADINGS.has(child.tagName) ? [index] : [],
    );
    let first = headings.length;
    while (first > 0 && BASIS_HEADINGS.has(plainText(tree.children[headings[first - 1]!]!).trim())) {
      first -= 1;
    }
    if (first === headings.length) return;
    const groups = headings.slice(first).map((start, offset) => {
      const end = headings[first + offset + 1] ?? tree.children.length;
      const [heading, ...body] = tree.children.slice(start, end);
      return hastElement("div", BASIS_GROUP, [heading!, hastElement("div", BASIS_BODY, body)]);
    });
    tree.children.splice(headings[first]!, tree.children.length, hastElement("section", BASIS_BOX, groups));
  };
}

// Agent 문서의 제목 단계는 화면 제목 구조와 무관하므로 모두 카드 안 소제목(h3·h4)으로 내린다.
const components: Components = {
  h1: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
  h2: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
  h3: ({ children }) => <h3 className="text-base font-semibold">{children}</h3>,
  h4: ({ children }) => <h4 className="text-sm font-semibold">{children}</h4>,
  ul: ({ children }) => <ul className="ml-5 list-disc space-y-1">{children}</ul>,
  ol: ({ children }) => <ol className="ml-5 list-decimal space-y-1">{children}</ol>,
  li: ({ children, node }) => {
    const label = leadingLabel(node);
    const marker = label === null ? undefined : LABEL_MARKERS.get(label);
    if (marker === undefined) return <li>{children}</li>;
    const Icon = marker.icon;
    // 아이콘을 따로 한 칸에 두어 줄이 넘어가도 글 첫머리가 라벨에 맞춰 정렬된다.
    return (
      <li className="flex list-none gap-2">
        <Icon aria-hidden="true" className={cn("mt-[0.3em] size-[1.1em] shrink-0", marker.className)} />
        <div className="min-w-0 flex-1">{children}</div>
      </li>
    );
  },
  // 표는 좁은 화면에서 가로로 스크롤되므로 스크롤 영역 자체가 키보드 포커스를 받는다.
  table: ({ children }) => (
    <div
      aria-label="조회 응답 표"
      className="relative w-full overflow-x-auto rounded-lg border"
      role="region"
      tabIndex={0}
    >
      {/* 좁은 화면에서 열을 글자 단위로 쪼개지 않도록 최소 너비를 두고 표 영역만 가로로 스크롤한다. */}
      <table className="w-full min-w-[36rem] caption-bottom text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-muted/50 [&_tr]:border-b">{children}</thead>,
  tbody: ({ children }) => <tbody className="[&_tr:last-child]:border-0">{children}</tbody>,
  tr: ({ children }) => <tr className="border-b">{children}</tr>,
  th: ({ children }) => (
    <th className="h-10 px-3 text-left align-middle font-medium whitespace-nowrap">{children}</th>
  ),
  // 여러 줄 셀이 섞인 표라 위쪽에 맞춰 읽게 한다. 첫 열(역·시설 이름)은 넓은 화면에서만 한 줄로
  // 두고, 표를 가로로 넘겨 보는 좁은 화면에서는 정보 열에 자리를 남긴다.
  td: ({ children }) => (
    <td className="px-3 py-2.5 align-top text-pretty first:font-medium md:first:whitespace-nowrap">
      {children}
    </td>
  ),
  // 답변 문서는 출발 전 안전 안내를 인용문으로 강조한다. 흐린 색으로 낮추지 않는다.
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-foreground/40 pl-4 font-medium">{children}</blockquote>
  ),
  code: ({ children }) => (
    <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[0.9em]">{children}</code>
  ),
  a: ({ children, href }) => (
    <a className="font-medium underline underline-offset-4" href={href} rel="noreferrer noopener">
      {children}
    </a>
  ),
};

// 문장을 해석하지 않고 마크다운 구조만으로 훑어 읽기 쉬운 위계를 만든다.
// - 첫 문단(할 일 한 줄)은 결과의 큰 제목처럼 보인다.
// - 첫 문단 바로 다음 목록(확인됨·확인 안 됨·권장 확인 같은 라벨 항목)은 요약 상자로 묶는다.
// - 소제목 구역(역별 확인 결과 등)은 구분선으로 나누고, 끝의 기준 시각·주의사항은 근거 상자로 묶는다.
const LEAD_EMPHASIS = [
  "[&>p:first-child]:text-xl [&>p:first-child]:leading-8 [&>p:first-child]:tracking-tight",
  "[&>p:first-child+ul]:ml-0 [&>p:first-child+ul]:list-none [&>p:first-child+ul]:space-y-2",
  "[&>p:first-child+ul]:rounded-lg [&>p:first-child+ul]:bg-muted/60 [&>p:first-child+ul]:px-4 [&>p:first-child+ul]:py-3",
  "[&>h3]:mt-2 [&>h3]:border-t [&>h3]:pt-5",
].join(" ");

interface CanonicalMarkdownProps {
  compact?: boolean;
  message: string;
}

export function CanonicalMarkdown({ compact = false, message }: CanonicalMarkdownProps) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col",
        compact ? "gap-2 text-sm leading-6" : ["gap-3 text-base leading-7", LEAD_EMPHASIS],
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkLineBreakTags]}
        rehypePlugins={compact ? [] : [rehypeAnswerBasis]}
        components={components}
      >
        {message}
      </ReactMarkdown>
    </div>
  );
}
