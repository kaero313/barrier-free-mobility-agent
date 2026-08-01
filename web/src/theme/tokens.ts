import tokens from "./design-tokens.json";

/**
 * Build-local mirror of docs/design-tokens.json.
 *
 * The Web application must never import files from outside `web/` at build time.
 * Update this mirror and tokens.css together when the product token source changes.
 */
export const designTokens = tokens;

export type StatusTone = keyof typeof designTokens.color.status;
