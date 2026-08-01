import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import styles from "./assistant-workspace.module.css";

const components: Components = {
  h1: ({ children }) => <h3 className={styles.canonicalHeading}>{children}</h3>,
  h2: ({ children }) => <h3 className={styles.canonicalHeading}>{children}</h3>,
  h3: ({ children }) => <h3 className={styles.canonicalHeading}>{children}</h3>,
  h4: ({ children }) => <h4 className={styles.canonicalSubheading}>{children}</h4>,
  table: ({ children }) => (
    <div className={styles.tableScroller} tabIndex={0} role="region" aria-label="조회 응답 표">
      <table>{children}</table>
    </div>
  ),
  blockquote: ({ children }) => <blockquote className={styles.canonicalQuote}>{children}</blockquote>,
  a: ({ children, href }) => (
    <a href={href} rel="noreferrer noopener">
      {children}
    </a>
  ),
};

interface CanonicalMarkdownProps {
  compact?: boolean;
  message: string;
}

export function CanonicalMarkdown({ compact = false, message }: CanonicalMarkdownProps) {
  return (
    <div className={`${styles.canonicalMarkdown} ${compact ? styles.canonicalMarkdownCompact : ""}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {message}
      </ReactMarkdown>
    </div>
  );
}
