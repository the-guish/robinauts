// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Markdown, drawn as React elements.
 *
 * STUB. This is the agreed interface, kept here so that the chat can be built
 * against it while the renderer is written. The real implementation replaces
 * this file (and adds a parser beside it) without changing the interface.
 *
 * The contract the real one honours:
 *
 * - **No HTML string is ever produced**, and nothing goes through
 *   `dangerouslySetInnerHTML`: the output is a tree of React elements with
 *   text nodes, so React escapes everything. Raw HTML in the source is text.
 * - **Links carry only `http:`, `https:` and `mailto:`**; any other scheme is
 *   drawn as text. Links open in a new tab with `rel="noopener noreferrer"`.
 *   Images are drawn as links with their alt text, not as `<img>`: the
 *   Content-Security-Policy allows no remote image anyway.
 * - **Streaming input is normal input.** While `streaming` is true the text is
 *   still arriving; an unclosed fence, emphasis or table is drawn as far as it
 *   goes and never throws. Parsing is linear in the size of the text.
 * - What it draws: paragraphs, headings, emphasis and strong, inline code,
 *   fenced and indented code (with a language label and a copy button),
 *   ordered, unordered and task lists (nested), links and autolinks,
 *   blockquotes, thematic breaks, hard breaks, tables and strikethrough.
 * - Every element carries a class prefixed `md-` and is styled by
 *   `./markdown.css`, over the tokens of `src/tokens.css`. No colour or radius
 *   is written as a literal.
 */
import type { ReactNode } from "react";

export interface MarkdownProps {
  /** The Markdown source. */
  text: string;
  /** True while the text is still arriving (a run in flight). */
  streaming?: boolean;
}

export function Markdown({ text }: Readonly<MarkdownProps>): ReactNode {
  return (
    <div className="md">
      {text.split(/\n{2,}/).map((block, at) => (
        <p key={at} className="md-p">
          {block}
        </p>
      ))}
    </div>
  );
}
