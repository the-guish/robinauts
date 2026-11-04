// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Markdown, drawn as React elements.
 *
 * The parser is `./parse.ts`, the drawing `./render.tsx`, the look
 * `./markdown.css`. This file is the interface the chat uses, and it has
 * not changed since the stub that stood in for it.
 *
 * The contract it honours:
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
 *   goes and never throws. Parsing is linear in the size of the text. While
 *   streaming, a `<span class="md-cursor" aria-hidden="true">` follows the
 *   last text drawn: the pulsing dot that says the answer is not finished.
 * - What it draws: paragraphs, headings, emphasis and strong, inline code,
 *   fenced and indented code (with a language label and a copy button),
 *   ordered, unordered and task lists (nested), links and autolinks,
 *   blockquotes, thematic breaks, hard breaks, tables and strikethrough.
 * - Every element carries a class prefixed `md-` and is styled by
 *   `./markdown.css`, over the tokens of `src/tokens.css`. No colour or radius
 *   is written as a literal.
 *
 * **Memoised.** A streaming answer is drawn again on every few words, and
 * every message above it is drawn again with it. The component is `memo`,
 * so a message whose props did not change is not drawn at all, and the
 * parse is kept per `text` and `streaming`, so drawing again for any other
 * reason does not parse again.
 */
import "./markdown.css";

import { memo, useMemo, type ReactNode } from "react";

import { parse } from "./parse";
import { renderBlocks } from "./render";

export interface MarkdownProps {
  /** The Markdown source. */
  text: string;
  /** True while the text is still arriving (a run in flight). */
  streaming?: boolean;
}

function MarkdownImpl({
  text,
  streaming = false,
}: Readonly<MarkdownProps>): ReactNode {
  const blocks = useMemo(() => parse(text, { streaming }), [text, streaming]);
  const body = useMemo(
    () => renderBlocks(blocks, streaming),
    [blocks, streaming],
  );
  return <div className="md">{body}</div>;
}

export const Markdown = memo(MarkdownImpl);
Markdown.displayName = "Markdown";
