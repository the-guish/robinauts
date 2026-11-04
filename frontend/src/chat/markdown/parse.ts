// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Markdown source to a tree: CommonMark's blocks and inlines, plus the GFM
 * tables, task items, strikethrough and bare-URL autolinks.
 *
 * Written here rather than taken from a package (ADR 0001, "Alternative B"):
 * what a chat answer needs is a subset, and the subset has to be safe and
 * fast on text that a language model is still writing. Three properties are
 * designed in rather than hoped for.
 *
 * **Linear time.** Every scanner is hand-written and moves forward. Where
 * CommonMark's algorithm would look ahead again and again from many starting
 * points -- the tail of a link, a reference label -- the look-ahead is paid
 * for out of one budget for the whole document, a few times its length, and
 * once that is spent the parser stops trying and keeps the text as text.
 * Ordinary answers never come near it; an adversarial one degrades to plain
 * text instead of freezing the tab. Code spans find their closing run in a
 * precomputed index rather than by searching. Nesting is capped
 * (`MAX_BLOCK_DEPTH`, `MAX_INLINE_DEPTH`): past the cap a `>` or a list
 * marker is text and emphasis is flattened, which keeps both the per-line
 * work and the depth of the React tree bounded.
 *
 * **Nothing here is HTML.** The output is a plain data tree that
 * `./render.tsx` turns into React elements. Raw HTML in the source is never
 * recognised, so it stays text. A link's destination is kept only when
 * `safeHref` allows it; otherwise the link has `href: null` and is drawn as
 * its text.
 *
 * **Unfinished input is ordinary input.** Nothing throws on a prefix of a
 * document: an unclosed fence runs to the end, an unclosed `*` or `[` is
 * text. With `streaming` set, three things that would flicker while the
 * last line is still being typed are held back: a partial closing fence is
 * not shown as a line of code, a lone `-` or `=` is not yet a heading
 * underline, and a partial table delimiter row already makes the table.
 *
 * Where this differs from CommonMark 0.31 / GFM 0.29, on purpose:
 *
 * - No raw HTML (blocks or inline): it is text. No setext heading from the
 *   last line while streaming (see above).
 * - Emphasis follows the spec's delimiter algorithm, including the "rule of
 *   three", but past `MAX_INLINE_DEPTH` levels nested emphasis is flattened
 *   to its text. Quotes and list items nest at most `MAX_BLOCK_DEPTH` deep.
 * - Strikethrough needs exactly two tildes; a single `~` is text (models
 *   write it for "approximately").
 * - Bare-URL autolinks are found after inline parsing, in text outside links
 *   and code, for `http://`, `https://` and `www.` only (no bare e-mail
 *   addresses). Emphasis markers inside a bare URL are still emphasis.
 * - A task item may be just `- [ ]` with nothing after it, so a list being
 *   typed does not flicker between text and a checkbox.
 * - Named character references are a common subset (`NAMED` below), not the
 *   full HTML5 table; numeric ones are complete.
 * - Link destinations are not percent-encoded; the browser does that.
 * - Unicode case folding of reference labels is `toLowerCase().toUpperCase()`.
 * - A table needs a `|` in its header or delimiter row, and at most
 *   `MAX_TABLE_COLUMNS` cells in its header; a wider one is not a table, and
 *   its lines stay text. Every row has the header's width, padded or cut, so
 *   a table's cells are at most that many per line of input: linear, where a
 *   wide header over many short rows would otherwise be quadratic.
 * - A fence's language is the longest prefix of its info string made of
 *   letters, digits and `_+#.-`, read before escapes and character
 *   references are decoded, and none when that prefix is empty. It becomes a
 *   class name, which no other character could be trusted in: a form feed,
 *   written or as `&#12;`, would start a second class.
 */

/** A piece of inline content. */
export type Inline =
  | { type: "text"; value: string }
  | { type: "code"; value: string }
  | { type: "emphasis"; children: Inline[] }
  | { type: "strong"; children: Inline[] }
  | { type: "delete"; children: Inline[] }
  | {
      type: "link";
      /** `null` when the destination is not one `safeHref` allows. */
      href: string | null;
      title: string | null;
      /** An image, drawn as a link whose text is its alt text. */
      image: boolean;
      children: Inline[];
    }
  | { type: "break" };

/** A table column's alignment, from its delimiter row. */
export type Align = "left" | "center" | "right" | null;

export interface ListItem {
  /** `null` for an ordinary item; a boolean for a GFM task item. */
  checked: boolean | null;
  children: Block[];
}

/** A block of the document. */
export type Block =
  | { type: "paragraph"; children: Inline[] }
  | { type: "heading"; depth: 1 | 2 | 3 | 4 | 5 | 6; children: Inline[] }
  | { type: "code"; lang: string; value: string }
  | { type: "blockquote"; children: Block[] }
  | {
      type: "list";
      ordered: boolean;
      start: number;
      tight: boolean;
      items: ListItem[];
    }
  | { type: "thematicBreak" }
  | { type: "table"; align: Align[]; head: Inline[][]; rows: Inline[][][] };

interface ParseOptions {
  /** The text is still arriving: see the module comment. */
  streaming?: boolean;
}

/**
 * How many block containers (quotes and list items) may nest. Real answers
 * stay under six; the cap is what keeps a line of a hundred thousand `>`
 * from costing a hundred thousand steps on every line after it.
 */
export const MAX_BLOCK_DEPTH = 32;

/** How many inline elements (emphasis, links) may nest before flattening. */
const MAX_INLINE_DEPTH = 24;

/**
 * How many columns a table may have. Wider than any table a person reads in
 * a chat, and what bounds the cells a table can make of its input.
 */
const MAX_TABLE_COLUMNS = 128;

/** What a fence's language may be made of, from the start of its info. */
const LANGUAGE = /^[A-Za-z0-9_+#.-]*/;

/**
 * A fence's language: the longest prefix of the info string that `LANGUAGE`
 * allows, or `""` for none (see the module comment).
 */
export function fenceLanguage(info: string): string {
  return LANGUAGE.exec(info)?.[0] ?? "";
}

/** Look-ahead allowed for free, before the per-character allowance. */
const BUDGET_BASE = 100_000;

/** Parses Markdown source into blocks. Never throws on any string. */
export function parse(source: string, options: ParseOptions = {}): Block[] {
  const parser = new BlockParser(options.streaming === true);
  const doc = parser.run(source);
  const ctx: Context = {
    refs: parser.refs,
    budget: { left: BUDGET_BASE + source.length * 4 },
  };
  return convertBlocks(doc.children, ctx);
}

/**
 * The destination a link may carry, or `null`.
 *
 * Only `http:`, `https:` and `mailto:`. The check is made on what a browser
 * would see: tabs and line breaks anywhere are dropped and control
 * characters and spaces at either end are trimmed (the URL standard does
 * both), so `java\tscript:` is read as `javascript:` and refused. A
 * destination with no scheme -- a relative path, `//host` -- is refused
 * too: an answer has no base URL that would make it mean anything.
 */
export function safeHref(raw: string): string | null {
  let url = "";
  for (let i = 0; i < raw.length; i++) {
    const c = raw.charCodeAt(i);
    if (c !== 9 && c !== 10 && c !== 13) url += raw[i];
  }
  let start = 0;
  let end = url.length;
  while (start < end && url.charCodeAt(start) <= 32) start++;
  while (end > start && url.charCodeAt(end - 1) <= 32) end--;
  url = url.slice(start, end);
  const colon = url.indexOf(":");
  if (colon < 1) return null;
  const scheme = url.slice(0, colon).toLowerCase();
  if (scheme !== "http" && scheme !== "https" && scheme !== "mailto") {
    return null;
  }
  return url;
}

// ---------------------------------------------------------------------------
// Characters

const ASCII_PUNCTUATION = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";

function isAsciiPunctuation(c: string | undefined): boolean {
  return c !== undefined && c.length === 1 && ASCII_PUNCTUATION.includes(c);
}

function isSpaceOrTab(c: string | undefined): boolean {
  return c === " " || c === "\t";
}

// Single-character tests: no quantifier, so nothing to backtrack over.
const WHITESPACE = /\s/;
const PUNCTUATION = /[\p{P}\p{S}]/u;

function isDigit(c: string | undefined): boolean {
  return c !== undefined && c >= "0" && c <= "9";
}

function isHexDigit(c: string | undefined): boolean {
  return (
    c !== undefined &&
    ((c >= "0" && c <= "9") || (c >= "a" && c <= "f") || (c >= "A" && c <= "F"))
  );
}

function isAlphanumeric(c: string | undefined): boolean {
  return (
    c !== undefined &&
    ((c >= "0" && c <= "9") || (c >= "a" && c <= "z") || (c >= "A" && c <= "Z"))
  );
}

/** Whether `s` holds only spaces and tabs from `i` on. */
function blankFrom(s: string, i: number): boolean {
  for (let j = i; j < s.length; j++) if (!isSpaceOrTab(s[j])) return false;
  return true;
}

/** `s` without trailing spaces, tabs and line breaks. */
function trimEnd(s: string): string {
  let end = s.length;
  while (end > 0) {
    const c = s.charCodeAt(end - 1);
    if (c !== 32 && c !== 9 && c !== 10) break;
    end--;
  }
  return s.slice(0, end);
}

/** `s` without leading and trailing spaces and tabs. */
function trimSpaces(s: string): string {
  let start = 0;
  let end = s.length;
  while (start < end && isSpaceOrTab(s[start])) start++;
  while (end > start && isSpaceOrTab(s[end - 1])) end--;
  return s.slice(start, end);
}

/**
 * The named character references decoded: the common ones a model writes.
 * An unknown name stays as written, which is also what a browser shows for
 * a name it does not know.
 */
const NAMED = new Map<string, string>([
  ["amp", "&"],
  ["lt", "<"],
  ["gt", ">"],
  ["quot", '"'],
  ["apos", "'"],
  ["nbsp", " "],
  ["ensp", " "],
  ["emsp", " "],
  ["thinsp", " "],
  ["shy", "­"],
  ["copy", "©"],
  ["reg", "®"],
  ["trade", "™"],
  ["hellip", "…"],
  ["mdash", "—"],
  ["ndash", "–"],
  ["lsquo", "‘"],
  ["rsquo", "’"],
  ["sbquo", "‚"],
  ["ldquo", "“"],
  ["rdquo", "”"],
  ["bdquo", "„"],
  ["laquo", "«"],
  ["raquo", "»"],
  ["lsaquo", "‹"],
  ["rsaquo", "›"],
  ["bull", "•"],
  ["middot", "·"],
  ["dagger", "†"],
  ["Dagger", "‡"],
  ["prime", "′"],
  ["Prime", "″"],
  ["times", "×"],
  ["divide", "÷"],
  ["minus", "−"],
  ["plusmn", "±"],
  ["deg", "°"],
  ["micro", "µ"],
  ["para", "¶"],
  ["sect", "§"],
  ["euro", "€"],
  ["pound", "£"],
  ["yen", "¥"],
  ["cent", "¢"],
  ["larr", "←"],
  ["uarr", "↑"],
  ["rarr", "→"],
  ["darr", "↓"],
  ["harr", "↔"],
  ["lArr", "⇐"],
  ["rArr", "⇒"],
  ["hArr", "⇔"],
  ["le", "≤"],
  ["ge", "≥"],
  ["ne", "≠"],
  ["asymp", "≈"],
  ["equiv", "≡"],
  ["infin", "∞"],
  ["sum", "∑"],
  ["radic", "√"],
  ["frac12", "½"],
  ["frac14", "¼"],
  ["frac34", "¾"],
  ["sup1", "¹"],
  ["sup2", "²"],
  ["sup3", "³"],
  ["iexcl", "¡"],
  ["iquest", "¿"],
  ["check", "✓"],
  ["alpha", "α"],
  ["beta", "β"],
  ["gamma", "γ"],
  ["delta", "δ"],
  ["epsilon", "ε"],
  ["theta", "θ"],
  ["lambda", "λ"],
  ["mu", "μ"],
  ["pi", "π"],
  ["sigma", "σ"],
  ["tau", "τ"],
  ["phi", "φ"],
  ["omega", "ω"],
  ["Delta", "Δ"],
  ["Sigma", "Σ"],
  ["Omega", "Ω"],
  ["aacute", "á"],
  ["agrave", "à"],
  ["auml", "ä"],
  ["ccedil", "ç"],
  ["eacute", "é"],
  ["egrave", "è"],
  ["iacute", "í"],
  ["ntilde", "ñ"],
  ["oacute", "ó"],
  ["ouml", "ö"],
  ["szlig", "ß"],
  ["uacute", "ú"],
  ["uuml", "ü"],
]);

/** The character reference starting at `s[i]` (an `&`), if it is one. */
function entityAt(s: string, i: number): { value: string; end: number } | null {
  let j = i + 1;
  if (s[j] === "#") {
    j++;
    const hex = s[j] === "x" || s[j] === "X";
    if (hex) j++;
    const start = j;
    const most = hex ? 6 : 7;
    while (j - start < most && (hex ? isHexDigit(s[j]) : isDigit(s[j]))) j++;
    if (j === start || s[j] !== ";") return null;
    let code = Number.parseInt(s.slice(start, j), hex ? 16 : 10);
    // NUL, surrogates and what is past Unicode are the replacement
    // character, as the HTML standard has it.
    if (code === 0 || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) {
      code = 0xfffd;
    }
    return { value: String.fromCodePoint(code), end: j + 1 };
  }
  const start = j;
  while (j - start < 32 && isAlphanumeric(s[j])) j++;
  if (j === start || s[j] !== ";") return null;
  const value = NAMED.get(s.slice(start, j));
  return value === undefined ? null : { value, end: j + 1 };
}

/** Backslash escapes and character references resolved, as in a URL or title. */
function unescape(s: string): string {
  if (!s.includes("\\") && !s.includes("&")) return s;
  let out = "";
  let i = 0;
  while (i < s.length) {
    const c = s[i]!;
    if (c === "\\" && isAsciiPunctuation(s[i + 1])) {
      out += s[i + 1]!;
      i += 2;
    } else if (c === "&") {
      const entity = entityAt(s, i);
      if (entity === null) {
        out += c;
        i++;
      } else {
        out += entity.value;
        i = entity.end;
      }
    } else {
      out += c;
      i++;
    }
  }
  return out;
}

/** A reference label as it is looked up: case and whitespace do not count. */
function normalizeLabel(label: string): string {
  return label.trim().replace(/\s+/g, " ").toLowerCase().toUpperCase();
}

// ---------------------------------------------------------------------------
// Link pieces, shared by inline links and reference definitions

interface Scan {
  ok: boolean;
  value: string;
  /** Where the scan stopped, whether or not it succeeded. */
  end: number;
}

function scanDestination(s: string, i: number, allowEmpty: boolean): Scan {
  if (s[i] === "<") {
    let j = i + 1;
    while (j < s.length) {
      const c = s[j];
      if (c === "\\" && isAsciiPunctuation(s[j + 1])) {
        j += 2;
        continue;
      }
      if (c === ">") {
        return { ok: true, value: unescape(s.slice(i + 1, j)), end: j + 1 };
      }
      if (c === "\n" || c === "<") break;
      j++;
    }
    return { ok: false, value: "", end: j };
  }
  let j = i;
  let depth = 0;
  while (j < s.length) {
    const code = s.charCodeAt(j);
    if (code === 92 && isAsciiPunctuation(s[j + 1])) {
      j += 2;
      continue;
    }
    if (code <= 32 || code === 127) break;
    if (code === 40) {
      depth++;
      // The spec's own limit on nested parentheses in a destination.
      if (depth > 32) return { ok: false, value: "", end: j };
    } else if (code === 41) {
      if (depth === 0) break;
      depth--;
    }
    j++;
  }
  if (depth !== 0 || (j === i && !allowEmpty)) {
    return { ok: false, value: "", end: j };
  }
  return { ok: true, value: unescape(s.slice(i, j)), end: j };
}

function scanTitle(s: string, i: number): Scan {
  const open = s[i];
  const close = open === "(" ? ")" : open;
  let j = i + 1;
  while (j < s.length) {
    const c = s[j];
    if (c === "\\" && isAsciiPunctuation(s[j + 1])) {
      j += 2;
      continue;
    }
    if (c === close) {
      return { ok: true, value: unescape(s.slice(i + 1, j)), end: j + 1 };
    }
    if (open === "(" && c === "(") break;
    j++;
  }
  return { ok: false, value: "", end: j };
}

/** A link label `[...]` starting at `s[i]`; the end is past the `]`. */
function scanLabel(s: string, i: number): Scan {
  let j = i + 1;
  while (j < s.length && j - i <= 1000) {
    const c = s[j];
    if (c === "\\" && isAsciiPunctuation(s[j + 1])) {
      j += 2;
      continue;
    }
    if (c === "[") break;
    if (c === "]") {
      return { ok: true, value: s.slice(i + 1, j), end: j + 1 };
    }
    j++;
  }
  return { ok: false, value: "", end: j };
}

function skipWhitespace(s: string, i: number): number {
  while (i < s.length && (s[i] === " " || s[i] === "\t" || s[i] === "\n")) i++;
  return i;
}

/** Skips spaces and tabs and at most one line break. */
function skipWhitespaceOneLine(s: string, i: number): number {
  while (isSpaceOrTab(s[i])) i++;
  if (s[i] === "\n") {
    i++;
    while (isSpaceOrTab(s[i])) i++;
  }
  return i;
}

/** If only spaces and tabs remain on this line, where the next line starts. */
function endOfBlankRest(s: string, i: number): number {
  while (isSpaceOrTab(s[i])) i++;
  if (i >= s.length) return s.length;
  return s[i] === "\n" ? i + 1 : -1;
}

interface Target {
  href: string;
  title: string | null;
}

// ---------------------------------------------------------------------------
// Blocks
//
// The strategy is the one the CommonMark specification describes in its
// appendix: each line first continues as many open containers as it can,
// then may open new ones, and what is left is text for the innermost block.

type BlockType =
  | "document"
  | "quote"
  | "list"
  | "item"
  | "paragraph"
  | "heading"
  | "fence"
  | "indented"
  | "hr"
  | "table";

interface ListData {
  ordered: boolean;
  /** The bullet character, or the delimiter after the number. */
  marker: string;
  start: number;
  markerOffset: number;
  padding: number;
}

interface BlockNode {
  type: BlockType;
  parent: BlockNode | null;
  children: BlockNode[];
  open: boolean;
  lastLineBlank: boolean;
  lastLineChecked: boolean;
  startLine: number;
  /** Quotes and list items from the document down to here, inclusive. */
  depth: number;
  /** Paragraph, heading and code content, one entry per line. */
  lines: string[];
  level: number;
  fenceChar: string;
  fenceLength: number;
  fenceOffset: number;
  /** A fence's language, which is all that is kept of its info string. */
  info: string;
  list: ListData | null;
  tight: boolean;
  align: Align[];
  head: string[];
  rows: string[][];
}

function blockNode(
  type: BlockType,
  parent: BlockNode | null,
  line: number,
): BlockNode {
  const container = type === "quote" || type === "item";
  return {
    type,
    parent,
    children: [],
    open: true,
    lastLineBlank: false,
    lastLineChecked: false,
    startLine: line,
    depth: (parent?.depth ?? 0) + (container ? 1 : 0),
    lines: [],
    level: 0,
    fenceChar: "",
    fenceLength: 0,
    fenceOffset: 0,
    info: "",
    list: null,
    tight: true,
    align: [],
    head: [],
    rows: [],
  };
}

function canContain(parent: BlockType, child: BlockType): boolean {
  if (parent === "document" || parent === "quote" || parent === "item") {
    return child !== "item";
  }
  return parent === "list" && child === "item";
}

function acceptsLines(type: BlockType): boolean {
  return (
    type === "paragraph" ||
    type === "fence" ||
    type === "indented" ||
    type === "table"
  );
}

/** Whether a list or item ends in a blank line, looking into its last child. */
function endsWithBlankLine(start: BlockNode): boolean {
  let block: BlockNode | undefined = start;
  while (block !== undefined) {
    if (block.lastLineBlank) return true;
    const descend =
      !block.lastLineChecked &&
      (block.type === "list" || block.type === "item");
    // Each block is looked into once, which keeps closing a long list
    // linear: without the mark, every item would walk its last child again.
    block.lastLineChecked = true;
    if (!descend) break;
    block = block.children[block.children.length - 1];
  }
  return false;
}

/** The cells of a table row; `\|` is a pipe inside a cell, as in GFM. */
function splitRow(line: string): string[] {
  let s = trimSpaces(line);
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells: string[] = [];
  let cell = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i]!;
    if (c === "\\" && s[i + 1] === "|") {
      cell += "|";
      i++;
    } else if (c === "|") {
      cells.push(trimSpaces(cell));
      cell = "";
    } else {
      cell += c;
    }
  }
  cells.push(trimSpaces(cell));
  return cells;
}

/**
 * The alignments of a delimiter row, or `null` if this is not one. With
 * `lenient`, a last cell still being typed (`:` or nothing yet) is dropped
 * rather than failing the row.
 */
function delimiterRow(line: string, lenient: boolean): Align[] | null {
  if (!line.includes("-")) return null;
  const cells = splitRow(line);
  const align: Align[] = [];
  for (let k = 0; k < cells.length; k++) {
    const cell = cells[k]!;
    const left = cell.startsWith(":");
    const right = cell.length > 1 && cell.endsWith(":");
    const dashes = cell.slice(left ? 1 : 0, right ? -1 : undefined);
    let valid = dashes.length > 0;
    for (let i = 0; valid && i < dashes.length; i++) valid = dashes[i] === "-";
    if (!valid) {
      if (lenient && k === cells.length - 1 && (cell === "" || cell === ":")) {
        break;
      }
      return null;
    }
    align.push(
      left && right ? "center" : right ? "right" : left ? "left" : null,
    );
  }
  return align.length === 0 ? null : align;
}

class BlockParser {
  readonly refs = new Map<string, Target>();
  private readonly doc = blockNode("document", null, 0);
  private tip: BlockNode = this.doc;
  private oldTip: BlockNode = this.doc;
  private lastMatched: BlockNode = this.doc;
  private allClosed = true;

  private line = "";
  private lineNumber = 0;
  /** This is the last line and no line break has arrived after it yet. */
  private unfinishedLine = false;
  private offset = 0;
  private column = 0;
  private nextNonspace = 0;
  private nextNonspaceColumn = 0;
  private indent = 0;
  private indented = false;
  private blank = false;
  private partiallyConsumedTab = false;

  constructor(private readonly streaming: boolean) {}

  run(source: string): BlockNode {
    const text = source.replace(/\r\n?/g, "\n").replaceAll("\0", "�");
    const lines = text.split("\n");
    if (lines[lines.length - 1] === "") lines.pop();
    const unfinished = !text.endsWith("\n");
    for (let i = 0; i < lines.length; i++) {
      this.unfinishedLine = unfinished && i === lines.length - 1;
      this.incorporate(lines[i]!);
    }
    let block: BlockNode | null = this.tip;
    while (block !== null) {
      const parent: BlockNode | null = block.parent;
      this.finalize(block);
      block = parent;
    }
    return this.doc;
  }

  // Position within the line, in characters and in columns: a tab is a
  // stop every four columns, and a marker can consume part of one.

  private findNextNonspace(): void {
    const line = this.line;
    let i = this.offset;
    let cols = this.column;
    while (i < line.length) {
      const c = line.charCodeAt(i);
      if (c === 32) {
        i++;
        cols++;
      } else if (c === 9) {
        i++;
        cols += 4 - (cols % 4);
      } else {
        break;
      }
    }
    this.blank = i >= line.length;
    this.nextNonspace = i;
    this.nextNonspaceColumn = cols;
    this.indent = cols - this.column;
    this.indented = this.indent >= 4;
  }

  private advanceNextNonspace(): void {
    this.offset = this.nextNonspace;
    this.column = this.nextNonspaceColumn;
    this.partiallyConsumedTab = false;
  }

  private advanceOffset(count: number, columns: boolean): void {
    const line = this.line;
    while (count > 0 && this.offset < line.length) {
      if (line.charCodeAt(this.offset) === 9) {
        const toTab = 4 - (this.column % 4);
        if (columns) {
          this.partiallyConsumedTab = toTab > count;
          const advance = toTab > count ? count : toTab;
          this.column += advance;
          this.offset += this.partiallyConsumedTab ? 0 : 1;
          count -= advance;
        } else {
          this.partiallyConsumedTab = false;
          this.column += toTab;
          this.offset += 1;
          count -= 1;
        }
      } else {
        this.partiallyConsumedTab = false;
        this.offset += 1;
        this.column += 1;
        count -= 1;
      }
    }
  }

  private advanceToEnd(): void {
    this.offset = this.line.length;
    this.partiallyConsumedTab = false;
  }

  // The tree.

  private addChild(type: BlockType): BlockNode {
    while (!canContain(this.tip.type, type)) this.finalize(this.tip);
    const child = blockNode(type, this.tip, this.lineNumber);
    this.tip.children.push(child);
    this.tip = child;
    return child;
  }

  private closeUnmatchedBlocks(): void {
    if (this.allClosed) return;
    while (this.oldTip !== this.lastMatched) {
      const parent = this.oldTip.parent!;
      this.finalize(this.oldTip);
      this.oldTip = parent;
    }
    this.allClosed = true;
  }

  private addLine(): void {
    let text: string;
    if (this.partiallyConsumedTab) {
      this.offset += 1;
      const toTab = 4 - (this.column % 4);
      text = " ".repeat(toTab) + this.line.slice(this.offset);
    } else {
      text = this.line.slice(this.offset);
    }
    if (this.tip.type === "table") this.tip.rows.push(splitRow(text));
    else this.tip.lines.push(text);
  }

  private finalize(block: BlockNode): void {
    block.open = false;
    switch (block.type) {
      case "paragraph": {
        if (block.lines[0]?.startsWith("[") === true) {
          const rest = this.extractDefinitions(block.lines.join("\n"));
          block.lines = rest === "" ? [] : rest.split("\n");
        }
        if (block.lines.every((line) => blankFrom(line, 0))) {
          const siblings = block.parent!.children;
          const at = siblings.lastIndexOf(block);
          if (at >= 0) siblings.splice(at, 1);
        }
        break;
      }
      case "indented":
        while (
          block.lines.length > 0 &&
          blankFrom(block.lines[block.lines.length - 1]!, 0)
        ) {
          block.lines.pop();
        }
        break;
      case "list": {
        // Loose when an item, or a block in an item, is followed by a
        // blank line before its next sibling.
        const items = block.children;
        for (let k = 0; k < items.length && block.tight; k++) {
          const item = items[k]!;
          if (k < items.length - 1 && endsWithBlankLine(item)) {
            block.tight = false;
            break;
          }
          const children = item.children;
          for (let m = 0; m < children.length - 1; m++) {
            if (endsWithBlankLine(children[m]!)) {
              block.tight = false;
              break;
            }
          }
        }
        break;
      }
      default:
        break;
    }
    this.tip = block.parent ?? this.doc;
  }

  // One line.

  private incorporate(line: string): void {
    this.line = line;
    this.lineNumber++;
    this.offset = 0;
    this.column = 0;
    this.blank = false;
    this.partiallyConsumedTab = false;
    this.oldTip = this.tip;

    // Which open containers does this line continue?
    let container = this.doc;
    for (;;) {
      const last = container.children[container.children.length - 1];
      if (last === undefined || !last.open) break;
      container = last;
      this.findNextNonspace();
      const result = this.continues(container);
      if (result === 0) continue;
      if (result === 1) {
        container = container.parent!;
        break;
      }
      return; // A closing fence: the line is used up.
    }
    this.allClosed = container === this.oldTip;
    this.lastMatched = container;

    // Does it open new blocks?
    let matchedLeaf =
      container.type === "fence" || container.type === "indented";
    while (!matchedLeaf) {
      this.findNextNonspace();
      const result = this.starts(container);
      if (result === 0) {
        this.advanceNextNonspace();
        break;
      }
      container = this.tip;
      if (result === 2) matchedLeaf = true;
    }

    // What is left is text, for the innermost block or for a new paragraph.
    if (!this.allClosed && !this.blank && this.tip.type === "paragraph") {
      // A lazy continuation line of a paragraph inside a container.
      this.addLine();
      return;
    }
    this.closeUnmatchedBlocks();
    const type = container.type;
    if (this.blank && container.children.length > 0) {
      container.children[container.children.length - 1]!.lastLineBlank = true;
    }
    const lastLineBlank =
      this.blank &&
      !(
        type === "quote" ||
        type === "fence" ||
        (type === "item" &&
          container.children.length === 0 &&
          container.startLine === this.lineNumber)
      );
    for (let b: BlockNode | null = container; b !== null; b = b.parent) {
      b.lastLineBlank = lastLineBlank;
    }
    if (acceptsLines(type)) {
      // The line that opened a fence or a table is not one of its lines.
      // (A table made from a paragraph keeps the paragraph's first line, so
      // for a table it is enough that nothing is left of this one.)
      const opened =
        this.offset >= line.length &&
        (type === "table" ||
          (type === "fence" && container.startLine === this.lineNumber));
      if (!opened) this.addLine();
    } else if (this.offset < line.length && !this.blank) {
      this.addChild("paragraph");
      this.advanceNextNonspace();
      this.addLine();
    }
  }

  /** 0: the line continues `block`; 1: it does not; 2: it closed a fence. */
  private continues(block: BlockNode): 0 | 1 | 2 {
    const line = this.line;
    switch (block.type) {
      case "quote":
        if (!this.indented && line[this.nextNonspace] === ">") {
          this.advanceNextNonspace();
          this.advanceOffset(1, false);
          if (isSpaceOrTab(line[this.offset])) this.advanceOffset(1, true);
          return 0;
        }
        return 1;
      case "item": {
        const list = block.list!;
        if (this.blank) {
          if (block.children.length === 0) return 1;
          this.advanceNextNonspace();
          return 0;
        }
        if (this.indent >= list.markerOffset + list.padding) {
          this.advanceOffset(list.markerOffset + list.padding, true);
          return 0;
        }
        return 1;
      }
      case "list":
        return 0;
      case "fence": {
        if (!this.indented && line[this.nextNonspace] === block.fenceChar) {
          let end = this.nextNonspace;
          while (line[end] === block.fenceChar) end++;
          if (blankFrom(line, end)) {
            if (end - this.nextNonspace >= block.fenceLength) {
              this.finalize(block);
              return 2;
            }
            // While streaming, the start of the closing fence is not a
            // line of code: it is about to become the fence.
            if (this.streaming && this.unfinishedLine) return 2;
          }
        }
        for (let i = block.fenceOffset; i > 0; i--) {
          if (!isSpaceOrTab(line[this.offset])) break;
          this.advanceOffset(1, true);
        }
        return 0;
      }
      case "indented":
        if (this.indent >= 4) {
          this.advanceOffset(4, true);
        } else if (this.blank) {
          this.advanceNextNonspace();
        } else {
          return 1;
        }
        return 0;
      case "paragraph":
      case "table":
        return this.blank ? 1 : 0;
      default:
        return 1;
    }
  }

  /** 0: nothing starts here; 1: a container started; 2: a leaf started. */
  private starts(container: BlockNode): 0 | 1 | 2 {
    const line = this.line;
    const c = line[this.nextNonspace];
    const atCap = container.depth >= MAX_BLOCK_DEPTH;

    if (!this.indented) {
      if (c === ">" && !atCap) {
        this.advanceNextNonspace();
        this.advanceOffset(1, false);
        if (isSpaceOrTab(line[this.offset])) this.advanceOffset(1, true);
        this.closeUnmatchedBlocks();
        this.addChild("quote");
        return 1;
      }
      if (c === "#" && this.atxHeading()) return 2;
      if ((c === "`" || c === "~") && this.fence()) return 2;
      if (container.type === "paragraph" && this.table(container)) return 2;
      if (
        container.type === "paragraph" &&
        (c === "=" || c === "-") &&
        this.setextHeading(container)
      ) {
        return 2;
      }
      if ((c === "*" || c === "-" || c === "_") && this.thematicBreak()) {
        return 2;
      }
    }
    if ((!this.indented || container.type === "list") && !atCap) {
      if (this.listItem(container)) return 1;
    }
    if (this.indented && this.tip.type !== "paragraph" && !this.blank) {
      this.advanceOffset(4, true);
      this.closeUnmatchedBlocks();
      this.addChild("indented");
      return 2;
    }
    return 0;
  }

  private atxHeading(): boolean {
    const line = this.line;
    let end = this.nextNonspace;
    while (line[end] === "#" && end - this.nextNonspace < 7) end++;
    const level = end - this.nextNonspace;
    if (level > 6 || (end < line.length && !isSpaceOrTab(line[end]))) {
      return false;
    }
    this.closeUnmatchedBlocks();
    const heading = this.addChild("heading");
    heading.level = level;
    let content = trimSpaces(line.slice(end));
    // An optional closing sequence of `#`, which needs a space before it.
    let cut = content.length;
    while (cut > 0 && content[cut - 1] === "#") cut--;
    if (cut === 0) content = "";
    else if (cut < content.length && isSpaceOrTab(content[cut - 1])) {
      content = trimSpaces(content.slice(0, cut));
    }
    heading.lines = [content];
    this.advanceToEnd();
    return true;
  }

  private fence(): boolean {
    const line = this.line;
    const char = line[this.nextNonspace]!;
    let end = this.nextNonspace;
    while (line[end] === char) end++;
    const length = end - this.nextNonspace;
    if (length < 3) return false;
    const info = trimSpaces(line.slice(end));
    if (char === "`" && info.includes("`")) return false;
    this.closeUnmatchedBlocks();
    const fence = this.addChild("fence");
    fence.fenceChar = char;
    fence.fenceLength = length;
    fence.fenceOffset = this.indent;
    // Only the language is kept of the info string, as the code block uses
    // nothing else of it.
    fence.info = fenceLanguage(info);
    this.advanceToEnd();
    return true;
  }

  private table(paragraph: BlockNode): boolean {
    const header = paragraph.lines[paragraph.lines.length - 1];
    if (header === undefined) return false;
    const row = this.line.slice(this.nextNonspace);
    const lenient = this.streaming && this.unfinishedLine;
    const align = delimiterRow(row, lenient);
    if (align === null || (!row.includes("|") && !header.includes("|"))) {
      return false;
    }
    const head = splitRow(header);
    // Too wide to be a table at all: what would bound its cells is gone.
    if (head.length > MAX_TABLE_COLUMNS) return false;
    if (align.length !== head.length) {
      // A delimiter row still being typed has fewer cells than the header.
      if (!lenient || align.length > head.length) return false;
      while (align.length < head.length) align.push(null);
    }
    paragraph.lines.pop();
    let table: BlockNode;
    if (paragraph.lines.length === 0) {
      paragraph.type = "table";
      table = paragraph;
    } else {
      this.finalize(paragraph);
      table = this.addChild("table");
    }
    table.align = align;
    table.head = head;
    this.advanceToEnd();
    return true;
  }

  private setextHeading(paragraph: BlockNode): boolean {
    const line = this.line;
    const char = line[this.nextNonspace];
    let end = this.nextNonspace;
    while (line[end] === char) end++;
    if (!blankFrom(line, end)) return false;
    // A lone `-` being typed is more likely the start of a list item.
    if (this.streaming && this.unfinishedLine) return false;
    if (paragraph.lines[0]?.startsWith("[") === true) {
      const rest = this.extractDefinitions(paragraph.lines.join("\n"));
      paragraph.lines = rest === "" ? [] : rest.split("\n");
    }
    if (paragraph.lines.every((l) => blankFrom(l, 0))) return false;
    paragraph.type = "heading";
    paragraph.level = char === "=" ? 1 : 2;
    this.advanceToEnd();
    return true;
  }

  private thematicBreak(): boolean {
    const line = this.line;
    const char = line[this.nextNonspace];
    let count = 0;
    for (let i = this.nextNonspace; i < line.length; i++) {
      const c = line[i];
      if (c === char) count++;
      else if (!isSpaceOrTab(c)) return false;
    }
    if (count < 3) return false;
    this.closeUnmatchedBlocks();
    this.addChild("hr");
    this.advanceToEnd();
    return true;
  }

  private listItem(container: BlockNode): boolean {
    if (this.indent >= 4) return false;
    const line = this.line;
    const at = this.nextNonspace;
    const c = line[at];
    let data: ListData;
    let markerLength: number;
    if (c === "*" || c === "+" || c === "-") {
      data = {
        ordered: false,
        marker: c,
        start: 1,
        markerOffset: 0,
        padding: 0,
      };
      markerLength = 1;
    } else {
      let end = at;
      while (isDigit(line[end]) && end - at < 9) end++;
      const delimiter = line[end];
      if (end === at || (delimiter !== "." && delimiter !== ")")) return false;
      data = {
        ordered: true,
        marker: delimiter,
        start: Number.parseInt(line.slice(at, end), 10),
        markerOffset: 0,
        padding: 0,
      };
      markerLength = end - at + 1;
    }
    const next = line[at + markerLength];
    if (next !== undefined && !isSpaceOrTab(next)) return false;
    // An item interrupts a paragraph only if it has content, and, when it
    // is numbered, only if it starts at one: "1983. A good year" is prose.
    if (
      container.type === "paragraph" &&
      (blankFrom(line, at + markerLength) || (data.ordered && data.start !== 1))
    ) {
      return false;
    }
    data.markerOffset = this.indent;
    this.advanceNextNonspace();
    this.advanceOffset(markerLength, true);
    const spacesStartColumn = this.column;
    const spacesStartOffset = this.offset;
    do {
      this.advanceOffset(1, true);
    } while (
      this.column - spacesStartColumn < 5 &&
      isSpaceOrTab(line[this.offset])
    );
    const blankItem = this.offset >= line.length;
    const spacesAfterMarker = this.column - spacesStartColumn;
    if (spacesAfterMarker >= 5 || spacesAfterMarker < 1 || blankItem) {
      // Code indented after the marker, or no content yet: the content
      // starts one column after the marker.
      data.padding = markerLength + 1;
      this.column = spacesStartColumn;
      this.offset = spacesStartOffset;
      this.partiallyConsumedTab = false;
      if (isSpaceOrTab(line[this.offset])) this.advanceOffset(1, true);
    } else {
      data.padding = markerLength + spacesAfterMarker;
    }
    this.closeUnmatchedBlocks();
    const list = this.tip.list;
    if (
      this.tip.type !== "list" ||
      list === null ||
      list.ordered !== data.ordered ||
      list.marker !== data.marker
    ) {
      this.addChild("list").list = data;
    }
    this.addChild("item").list = data;
    return true;
  }

  /**
   * Takes the link reference definitions off the front of a paragraph,
   * records them, and returns what is left. The first definition of a
   * label wins.
   */
  private extractDefinitions(text: string): string {
    let pos = 0;
    for (;;) {
      const end = this.definition(text, pos);
      if (end < 0) return text.slice(pos);
      pos = end;
    }
  }

  private definition(s: string, start: number): number {
    if (s[start] !== "[") return -1;
    const label = scanLabel(s, start);
    if (!label.ok || s[label.end] !== ":") return -1;
    const key = normalizeLabel(label.value);
    if (key === "") return -1;
    const destination = scanDestination(
      s,
      skipWhitespaceOneLine(s, label.end + 1),
      false,
    );
    if (!destination.ok) return -1;
    const titleAt = skipWhitespaceOneLine(s, destination.end);
    let title: string | null = null;
    let end = -1;
    const quote = s[titleAt];
    if (
      titleAt > destination.end &&
      (quote === '"' || quote === "'" || quote === "(")
    ) {
      const scanned = scanTitle(s, titleAt);
      if (scanned.ok) {
        end = endOfBlankRest(s, scanned.end);
        if (end >= 0) title = scanned.value;
      }
    }
    if (end < 0) {
      end = endOfBlankRest(s, destination.end);
      if (end < 0) return -1;
    }
    if (!this.refs.has(key)) {
      this.refs.set(key, { href: destination.value, title });
    }
    return end;
  }
}

// ---------------------------------------------------------------------------
// Blocks to the public tree

interface Context {
  refs: Map<string, Target>;
  /** Look-ahead left for the whole document; see the module comment. */
  budget: { left: number };
}

function convertBlocks(nodes: BlockNode[], ctx: Context): Block[] {
  const out: Block[] = [];
  for (const node of nodes) {
    switch (node.type) {
      case "paragraph": {
        const text = trimEnd(node.lines.join("\n"));
        if (text !== "") {
          out.push({ type: "paragraph", children: parseInline(text, ctx) });
        }
        break;
      }
      case "heading":
        out.push({
          type: "heading",
          depth: node.level as 1 | 2 | 3 | 4 | 5 | 6,
          children: parseInline(trimEnd(node.lines.join("\n")), ctx),
        });
        break;
      case "fence":
        out.push({
          type: "code",
          lang: node.info,
          value: node.lines.join("\n"),
        });
        break;
      case "indented":
        out.push({ type: "code", lang: "", value: node.lines.join("\n") });
        break;
      case "hr":
        out.push({ type: "thematicBreak" });
        break;
      case "quote":
        out.push({
          type: "blockquote",
          children: convertBlocks(node.children, ctx),
        });
        break;
      case "list":
        out.push({
          type: "list",
          ordered: node.list!.ordered,
          start: node.list!.start,
          tight: node.tight,
          items: node.children.map((item) => convertItem(item, ctx)),
        });
        break;
      case "table": {
        // Short rows are padded and long ones cut to the header, as GFM has
        // it; the header is at most `MAX_TABLE_COLUMNS` wide (`table`).
        const width = node.head.length;
        out.push({
          type: "table",
          align: node.align,
          head: node.head.map((cell) => parseInline(cell, ctx)),
          rows: node.rows.map((row) => {
            const cells: Inline[][] = [];
            for (let k = 0; k < width; k++) {
              cells.push(parseInline(row[k] ?? "", ctx));
            }
            return cells;
          }),
        });
        break;
      }
      default:
        break;
    }
  }
  return out;
}

function convertItem(item: BlockNode, ctx: Context): ListItem {
  let checked: boolean | null = null;
  const first = item.children[0];
  if (first?.type === "paragraph") {
    const line = first.lines[0] ?? "";
    const box = line[1];
    if (
      line[0] === "[" &&
      line[2] === "]" &&
      (box === " " || box === "x" || box === "X") &&
      (line.length === 3 || isSpaceOrTab(line[3]))
    ) {
      checked = box !== " ";
      first.lines[0] = trimSpaces(line.slice(3));
    }
  }
  return { checked, children: convertBlocks(item.children, ctx) };
}

// ---------------------------------------------------------------------------
// Inlines

type InlineKind =
  | "root"
  | "text"
  | "code"
  | "emphasis"
  | "strong"
  | "delete"
  | "link"
  | "break";

/**
 * A node of the working inline tree: a doubly linked list of siblings, so
 * that wrapping a run of them in emphasis or a link moves only that run.
 */
interface InlineNode {
  kind: InlineKind;
  text: string;
  /** A delimiter or bracket run, kept apart from its neighbours until done. */
  delimiter: boolean;
  href: string | null;
  title: string | null;
  image: boolean;
  parent: InlineNode | null;
  prev: InlineNode | null;
  next: InlineNode | null;
  first: InlineNode | null;
  last: InlineNode | null;
}

function inlineNode(kind: InlineKind, text = ""): InlineNode {
  return {
    kind,
    text,
    delimiter: false,
    href: null,
    title: null,
    image: false,
    parent: null,
    prev: null,
    next: null,
    first: null,
    last: null,
  };
}

function appendChild(parent: InlineNode, child: InlineNode): void {
  child.parent = parent;
  child.prev = parent.last;
  child.next = null;
  if (parent.last === null) parent.first = child;
  else parent.last.next = child;
  parent.last = child;
}

function unlink(node: InlineNode): void {
  if (node.prev === null) {
    if (node.parent !== null) node.parent.first = node.next;
  } else {
    node.prev.next = node.next;
  }
  if (node.next === null) {
    if (node.parent !== null) node.parent.last = node.prev;
  } else {
    node.next.prev = node.prev;
  }
  node.parent = null;
  node.prev = null;
  node.next = null;
}

function insertAfter(reference: InlineNode, node: InlineNode): void {
  node.parent = reference.parent;
  node.prev = reference;
  node.next = reference.next;
  if (reference.next === null) {
    if (reference.parent !== null) reference.parent.last = node;
  } else {
    reference.next.prev = node;
  }
  reference.next = node;
}

interface Delimiter {
  node: InlineNode;
  char: string;
  count: number;
  original: number;
  canOpen: boolean;
  canClose: boolean;
  prev: Delimiter | null;
  next: Delimiter | null;
}

interface Bracket {
  node: InlineNode;
  image: boolean;
  /** Where the link text starts in the source. */
  start: number;
  /** Order of creation, for deactivation (see `linksBefore`). */
  sequence: number;
  prevDelimiter: Delimiter | null;
  prev: Bracket | null;
}

function parseInline(text: string, ctx: Context): Inline[] {
  return convertInline(new InlineParser(text, ctx).parse());
}

function isSpecial(code: number): boolean {
  switch (code) {
    case 10: // \n
    case 92: // \
    case 96: // `
    case 42: // *
    case 95: // _
    case 126: // ~
    case 91: // [
    case 93: // ]
    case 33: // !
    case 60: // <
    case 38: // &
      return true;
    default:
      return false;
  }
}

class InlineParser {
  private pos = 0;
  private readonly root = inlineNode("root");
  private delimiters: Delimiter | null = null;
  private brackets: Bracket | null = null;
  private bracketCount = 0;
  /**
   * Every `[` opened at or before this sequence number comes before a link
   * already made, and so can no longer start one (links do not nest). One
   * number instead of a flag on each opener, so that making a link costs
   * nothing however many openers are waiting below it.
   */
  private linksBefore = -1;
  /** Backtick runs by length, for code spans: positions in order. */
  private runs: Map<number, number[]> | null = null;
  private readonly runCursor = new Map<number, number>();

  constructor(
    private readonly s: string,
    private readonly ctx: Context,
  ) {}

  parse(): InlineNode {
    const s = this.s;
    while (this.pos < s.length) {
      switch (s.charCodeAt(this.pos)) {
        case 10:
          this.newline();
          break;
        case 92:
          this.backslash();
          break;
        case 96:
          this.backticks();
          break;
        case 42:
        case 95:
        case 126:
          this.delimiterRun();
          break;
        case 91:
          this.openBracket(false, 1);
          break;
        case 33:
          if (s[this.pos + 1] === "[") this.openBracket(true, 2);
          else this.text("!", 1);
          break;
        case 93:
          this.closeBracket();
          break;
        case 60:
          if (!this.autolink()) this.text("<", 1);
          break;
        case 38: {
          const entity = entityAt(s, this.pos);
          if (entity === null) this.text("&", 1);
          else this.text(entity.value, entity.end - this.pos);
          break;
        }
        default: {
          let end = this.pos + 1;
          while (end < s.length && !isSpecial(s.charCodeAt(end))) end++;
          // Spaces before a line break are not text: they are the break.
          let textEnd = end;
          if (s.charCodeAt(end) === 10) {
            while (textEnd > this.pos && s.charCodeAt(textEnd - 1) === 32) {
              textEnd--;
            }
          }
          this.text(s.slice(this.pos, textEnd), end - this.pos);
          break;
        }
      }
    }
    this.processEmphasis(null);
    return this.root;
  }

  /** Appends text, joining it to the text before when that is plain. */
  private text(value: string, advance: number): void {
    this.pos += advance;
    const last = this.root.last;
    if (last !== null && last.kind === "text" && !last.delimiter) {
      last.text += value;
    } else {
      appendChild(this.root, inlineNode("text", value));
    }
  }

  /**
   * A line break: hard after two spaces, soft otherwise. The spaces are
   * counted in the source, and the text run before already left them out
   * (see `parse`): reading them back from the text node would make V8
   * flatten a string built by concatenation on every line, which turns a
   * long paragraph quadratic.
   */
  private newline(): void {
    let spaces = 0;
    while (this.s.charCodeAt(this.pos - 1 - spaces) === 32) spaces++;
    if (spaces >= 2) {
      appendChild(this.root, inlineNode("break"));
      this.pos++;
    } else {
      this.text("\n", 1);
    }
    this.skipLeadingSpace();
  }

  private skipLeadingSpace(): void {
    while (isSpaceOrTab(this.s[this.pos])) this.pos++;
  }

  private backslash(): void {
    const next = this.s[this.pos + 1];
    if (next === "\n") {
      appendChild(this.root, inlineNode("break"));
      this.pos += 2;
      this.skipLeadingSpace();
    } else if (isAsciiPunctuation(next)) {
      this.text(next!, 2);
    } else {
      this.text("\\", 1);
    }
  }

  private backticks(): void {
    const s = this.s;
    const start = this.pos;
    let end = start;
    while (s.charCodeAt(end) === 96) end++;
    const length = end - start;
    const closer = this.closingRun(start, length);
    if (closer < 0) {
      this.text(s.slice(start, end), length);
      return;
    }
    let content = s.slice(end, closer).replaceAll("\n", " ");
    if (
      content.length >= 2 &&
      content.startsWith(" ") &&
      content.endsWith(" ") &&
      content.trim() !== ""
    ) {
      content = content.slice(1, -1);
    }
    appendChild(this.root, inlineNode("code", content));
    this.pos = closer + length;
  }

  /**
   * The next backtick run of exactly `length` after `start`, or -1.
   *
   * Every run in the text is indexed once by its length; each length keeps
   * a cursor that only moves forward, because openers are met in order.
   * Searching instead would make a text of many unmatched runs quadratic.
   */
  private closingRun(start: number, length: number): number {
    if (this.runs === null) {
      const runs = new Map<number, number[]>();
      const s = this.s;
      let i = s.indexOf("`");
      while (i >= 0) {
        let j = i;
        while (s.charCodeAt(j) === 96) j++;
        const list = runs.get(j - i);
        if (list === undefined) runs.set(j - i, [i]);
        else list.push(i);
        i = s.indexOf("`", j);
      }
      this.runs = runs;
    }
    const positions = this.runs.get(length);
    if (positions === undefined) return -1;
    let k = this.runCursor.get(length) ?? 0;
    while (k < positions.length && positions[k]! <= start) k++;
    this.runCursor.set(length, k);
    return k < positions.length ? positions[k]! : -1;
  }

  private delimiterRun(): void {
    const s = this.s;
    const char = s[this.pos]!;
    const start = this.pos;
    let end = start;
    while (s[end] === char) end++;
    const count = end - start;
    const run = s.slice(start, end);
    if (char === "~" && count !== 2) {
      this.text(run, count);
      return;
    }
    const before = start === 0 ? "\n" : s[start - 1]!;
    const after = end >= s.length ? "\n" : s[end]!;
    const beforeSpace = WHITESPACE.test(before);
    const afterSpace = WHITESPACE.test(after);
    const beforePunctuation = PUNCTUATION.test(before);
    const afterPunctuation = PUNCTUATION.test(after);
    const leftFlanking =
      !afterSpace && (!afterPunctuation || beforeSpace || beforePunctuation);
    const rightFlanking =
      !beforeSpace && (!beforePunctuation || afterSpace || afterPunctuation);
    let canOpen = leftFlanking;
    let canClose = rightFlanking;
    if (char === "_") {
      // No intraword emphasis with underscores: snake_case stays as it is.
      canOpen = leftFlanking && (!rightFlanking || beforePunctuation);
      canClose = rightFlanking && (!leftFlanking || afterPunctuation);
    }
    if (!canOpen && !canClose) {
      this.text(run, count);
      return;
    }
    this.pos = end;
    const node = inlineNode("text", run);
    node.delimiter = true;
    appendChild(this.root, node);
    const delimiter: Delimiter = {
      node,
      char,
      count,
      original: count,
      canOpen,
      canClose,
      prev: this.delimiters,
      next: null,
    };
    if (this.delimiters !== null) this.delimiters.next = delimiter;
    this.delimiters = delimiter;
  }

  private openBracket(image: boolean, length: number): void {
    const node = inlineNode("text", image ? "![" : "[");
    node.delimiter = true;
    appendChild(this.root, node);
    this.pos += length;
    this.brackets = {
      node,
      image,
      start: this.pos,
      sequence: this.bracketCount++,
      prevDelimiter: this.delimiters,
      prev: this.brackets,
    };
  }

  private closeBracket(): void {
    const s = this.s;
    const closeAt = this.pos;
    this.pos++;
    const opener = this.brackets;
    if (opener === null) {
      this.text("]", 0);
      return;
    }
    this.brackets = opener.prev;
    if (!opener.image && opener.sequence <= this.linksBefore) {
      this.text("]", 0);
      return;
    }
    const budget = this.ctx.budget;
    let target: Target | null = null;
    let after = this.pos;

    // An inline link: `[text](destination "title")`.
    if (budget.left > 0 && s[this.pos] === "(") {
      const destination = scanDestination(
        s,
        skipWhitespace(s, this.pos + 1),
        true,
      );
      let furthest = destination.end;
      if (destination.ok) {
        const titleAt = skipWhitespace(s, destination.end);
        let title: string | null = null;
        let close = titleAt;
        const quote = s[titleAt];
        if (
          titleAt > destination.end &&
          (quote === '"' || quote === "'" || quote === "(")
        ) {
          const scanned = scanTitle(s, titleAt);
          furthest = scanned.end;
          if (scanned.ok) {
            title = scanned.value;
            close = skipWhitespace(s, scanned.end);
          }
        }
        if (s[close] === ")") {
          target = { href: destination.value, title };
          after = close + 1;
        }
      }
      budget.left -= furthest - this.pos;
    }

    // A reference link: `[text][label]`, `[label][]` or `[label]`.
    if (target === null && budget.left > 0 && this.ctx.refs.size > 0) {
      let label: string | null = null;
      let end = this.pos;
      let shortcut = true;
      if (s[this.pos] === "[") {
        const scanned = scanLabel(s, this.pos);
        budget.left -= scanned.end - this.pos;
        if (scanned.ok) {
          end = scanned.end;
          if (scanned.value.trim() === "") {
            shortcut = true; // `[]`: collapsed, the text is the label.
          } else {
            shortcut = false;
            label = scanned.value;
          }
        }
      }
      if (shortcut && closeAt - opener.start <= 999) {
        label = s.slice(opener.start, closeAt);
        budget.left -= closeAt - opener.start;
      }
      if (label !== null) {
        const found = this.ctx.refs.get(normalizeLabel(label));
        if (found !== undefined) {
          target = found;
          after = end;
        }
      }
    }

    if (target === null) {
      this.text("]", 0);
      return;
    }

    const link = inlineNode("link");
    link.href = safeHref(target.href);
    link.title = target.title;
    link.image = opener.image;
    let node = opener.node.next;
    while (node !== null) {
      const next: InlineNode | null = node.next;
      unlink(node);
      appendChild(link, node);
      node = next;
    }
    appendChild(this.root, link);
    this.processEmphasis(opener.prevDelimiter);
    unlink(opener.node);
    if (!opener.image) this.linksBefore = opener.sequence;
    this.pos = after;
  }

  /** `<scheme:...>` or `<user@host>`. */
  private autolink(): boolean {
    const s = this.s;
    const start = this.pos + 1;
    // A URI: a scheme of 2 to 32 characters, a colon, no spaces or `<`.
    if (/[A-Za-z]/.test(s[start] ?? "")) {
      let i = start + 1;
      while (
        i - start < 32 &&
        (isAlphanumeric(s[i]) || s[i] === "+" || s[i] === "." || s[i] === "-")
      ) {
        i++;
      }
      if (s[i] === ":" && i - start >= 2) {
        let j = i + 1;
        while (j < s.length) {
          const code = s.charCodeAt(j);
          if (code <= 32 || code === 60 || code === 62) break;
          j++;
        }
        if (s[j] !== ">") return false;
        const uri = s.slice(start, j);
        this.addAutolink(uri, uri, j + 1);
        return true;
      }
    }
    // An e-mail address.
    let i = start;
    while (
      i < s.length &&
      (isAlphanumeric(s[i]) || ".!#$%&'*+/=?^_`{|}~-".includes(s[i]!))
    ) {
      i++;
    }
    if (i === start || s[i] !== "@") return false;
    let j = i + 1;
    while (
      j < s.length &&
      (isAlphanumeric(s[j]) || s[j] === "-" || s[j] === ".")
    ) {
      j++;
    }
    if (j === i + 1 || s[j] !== ">" || !isAlphanumeric(s[j - 1])) return false;
    const address = s.slice(start, j);
    this.addAutolink(`mailto:${address}`, address, j + 1);
    return true;
  }

  private addAutolink(href: string, text: string, end: number): void {
    const link = inlineNode("link");
    link.href = safeHref(href);
    appendChild(link, inlineNode("text", text));
    appendChild(this.root, link);
    this.pos = end;
  }

  /**
   * CommonMark's "process emphasis" over the delimiters above `bottom`.
   *
   * `openersBottom` remembers, per kind of closer, below which delimiter no
   * opener can be found, so a failed search is never repeated: that is
   * what keeps a long run of unmatched `*` linear.
   */
  private processEmphasis(bottom: Delimiter | null): void {
    const openersBottom = new Map<string, Delimiter | null>();
    let closer = this.delimiters;
    while (closer !== null && closer.prev !== bottom) closer = closer.prev;

    while (closer !== null) {
      if (!closer.canClose) {
        closer = closer.next;
        continue;
      }
      const key =
        closer.char === "~"
          ? "~"
          : `${closer.char}${closer.canOpen ? 1 : 0}${closer.original % 3}`;
      const floor = openersBottom.has(key) ? openersBottom.get(key)! : bottom;
      let opener = closer.prev;
      let found = false;
      while (opener !== null && opener !== bottom && opener !== floor) {
        if (opener.char === closer.char && opener.canOpen) {
          // The "rule of three": `*a**b*` is not `<em>a</em><em>b</em>`.
          const odd =
            closer.char !== "~" &&
            (closer.canOpen || opener.canClose) &&
            closer.original % 3 !== 0 &&
            (opener.original + closer.original) % 3 === 0;
          if (!odd) {
            found = true;
            break;
          }
        }
        opener = opener.prev;
      }
      const current: Delimiter = closer;
      if (found && opener !== null) {
        const use =
          closer.char === "~" || (closer.count >= 2 && opener.count >= 2)
            ? 2
            : 1;
        const kind: InlineKind =
          closer.char === "~" ? "delete" : use === 2 ? "strong" : "emphasis";
        opener.count -= use;
        closer.count -= use;
        opener.node.text = opener.char.repeat(opener.count);
        closer.node.text = closer.char.repeat(closer.count);
        const wrapper = inlineNode(kind);
        let node = opener.node.next;
        while (node !== null && node !== closer.node) {
          const next: InlineNode | null = node.next;
          unlink(node);
          appendChild(wrapper, node);
          node = next;
        }
        insertAfter(opener.node, wrapper);
        // Delimiters between the two can no longer match anything.
        opener.next = closer;
        closer.prev = opener;
        if (opener.count === 0) {
          unlink(opener.node);
          this.removeDelimiter(opener);
        }
        if (closer.count === 0) {
          const next = closer.next;
          unlink(closer.node);
          this.removeDelimiter(closer);
          closer = next;
        }
      } else {
        closer = closer.next;
        openersBottom.set(key, current.prev);
        if (!current.canOpen) this.removeDelimiter(current);
      }
    }
    while (this.delimiters !== null && this.delimiters !== bottom) {
      this.removeDelimiter(this.delimiters);
    }
  }

  private removeDelimiter(delimiter: Delimiter): void {
    if (delimiter.prev !== null) delimiter.prev.next = delimiter.next;
    if (delimiter.next === null) this.delimiters = delimiter.prev;
    else delimiter.next.prev = delimiter.prev;
  }
}

// ---------------------------------------------------------------------------
// The working inline tree to the public one

interface Frame {
  next: InlineNode | null;
  out: Inline[];
  depth: number;
  inLink: boolean;
  pending: string;
}

/**
 * Walks the working tree without recursion (it can be deeper than the
 * stack), joining adjacent text, finding bare URLs in text outside links,
 * unwrapping a link inside a link, and flattening past `MAX_INLINE_DEPTH`.
 */
function convertInline(root: InlineNode): Inline[] {
  const result: Inline[] = [];
  const stack: Frame[] = [
    { next: root.first, out: result, depth: 0, inLink: false, pending: "" },
  ];
  const flush = (frame: Frame) => {
    if (frame.pending === "") return;
    if (frame.inLink) pushText(frame.out, frame.pending);
    else {
      for (const piece of bareAutolinks(frame.pending)) {
        pushInline(frame.out, piece);
      }
    }
    frame.pending = "";
  };
  while (stack.length > 0) {
    const frame = stack[stack.length - 1]!;
    const node = frame.next;
    if (node === null) {
      flush(frame);
      stack.pop();
      continue;
    }
    frame.next = node.next;
    if (node.kind === "text") {
      frame.pending += node.text;
      continue;
    }
    flush(frame);
    switch (node.kind) {
      case "code":
        frame.out.push({ type: "code", value: node.text });
        break;
      case "break":
        frame.out.push({ type: "break" });
        break;
      case "link":
        if (node.image) {
          const alt = plainText(node);
          if (frame.inLink) pushText(frame.out, alt);
          else {
            frame.out.push({
              type: "link",
              href: node.href,
              title: node.title,
              image: true,
              children: alt === "" ? [] : [{ type: "text", value: alt }],
            });
          }
        } else if (frame.inLink || frame.depth >= MAX_INLINE_DEPTH) {
          stack.push({ ...frame, next: node.first, pending: "", inLink: true });
        } else {
          const children: Inline[] = [];
          frame.out.push({
            type: "link",
            href: node.href,
            title: node.title,
            image: false,
            children,
          });
          stack.push({
            next: node.first,
            out: children,
            depth: frame.depth + 1,
            inLink: true,
            pending: "",
          });
        }
        break;
      default: {
        if (frame.depth >= MAX_INLINE_DEPTH) {
          stack.push({ ...frame, next: node.first, pending: "" });
          break;
        }
        const children: Inline[] = [];
        const type = node.kind as "emphasis" | "strong" | "delete";
        frame.out.push({ type, children });
        stack.push({
          next: node.first,
          out: children,
          depth: frame.depth + 1,
          inLink: frame.inLink,
          pending: "",
        });
        break;
      }
    }
  }
  return result;
}

function pushText(out: Inline[], value: string): void {
  if (value === "") return;
  const last = out[out.length - 1];
  if (last?.type === "text") last.value += value;
  else out.push({ type: "text", value });
}

function pushInline(out: Inline[], node: Inline): void {
  if (node.type === "text") pushText(out, node.value);
  else out.push(node);
}

/** The text of a node, as an image's alt text: no markup, no recursion. */
function plainText(node: InlineNode): string {
  let text = "";
  const stack: (InlineNode | null)[] = [node.first];
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (current === null) continue;
    stack.push(current.next);
    if (current.kind === "text" || current.kind === "code") {
      text += current.text;
    } else if (current.kind === "break") {
      text += " ";
    } else {
      stack.push(current.first);
    }
  }
  return text;
}

/**
 * GFM's extended autolinks: `http://`, `https://` and `www.` in plain text,
 * at the start or after whitespace or `*`, `_`, `~`, `(`.
 */
function bareAutolinks(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (
      (c === "h" || c === "w" || c === "H" || c === "W") &&
      urlBoundary(text, i)
    ) {
      const end = bareUrlEnd(text, i);
      if (end > i) {
        if (i > last) out.push({ type: "text", value: text.slice(last, i) });
        const url = text.slice(i, end);
        const www = url.toLowerCase().startsWith("www.");
        out.push({
          type: "link",
          href: safeHref(www ? `http://${url}` : url),
          title: null,
          image: false,
          children: [{ type: "text", value: url }],
        });
        i = end;
        last = end;
        continue;
      }
    }
    i++;
  }
  if (last < text.length) out.push({ type: "text", value: text.slice(last) });
  return out;
}

function urlBoundary(text: string, i: number): boolean {
  if (i === 0) return true;
  const before = text[i - 1]!;
  return (
    before === "*" ||
    before === "_" ||
    before === "~" ||
    before === "(" ||
    WHITESPACE.test(before)
  );
}

function startsWithAt(text: string, i: number, prefix: string): boolean {
  return text.slice(i, i + prefix.length).toLowerCase() === prefix;
}

function isDomainCharacter(c: string): boolean {
  return (
    isAlphanumeric(c) || c === "-" || c === "_" || c === "." || c > "\u007f"
  );
}

/** Where a bare URL starting at `i` ends, or `i` if there is none. */
function bareUrlEnd(text: string, i: number): number {
  let domainStart: number;
  if (startsWithAt(text, i, "https://")) domainStart = i + 8;
  else if (startsWithAt(text, i, "http://")) domainStart = i + 7;
  else if (startsWithAt(text, i, "www.")) domainStart = i;
  else return i;
  let j = domainStart;
  while (j < text.length && isDomainCharacter(text[j]!)) j++;
  // A domain has a dot inside it: `https://localhost` and `www.` are not
  // links, which is GFM's rule too.
  let domainEnd = j;
  while (domainEnd > domainStart && text[domainEnd - 1] === ".") domainEnd--;
  let dot = false;
  for (let k = domainStart + (domainStart === i ? 4 : 0); k < domainEnd; k++) {
    if (text[k] === ".") dot = true;
  }
  if (!dot) return i;
  while (j < text.length && text[j] !== "<" && !WHITESPACE.test(text[j]!)) j++;
  // Trailing punctuation is the sentence's, not the URL's; a closing
  // parenthesis is the URL's only while it has an opening one to match.
  let opens = 0;
  let closes = 0;
  for (let k = i; k < j; k++) {
    if (text[k] === "(") opens++;
    else if (text[k] === ")") closes++;
  }
  while (j > domainStart) {
    const c = text[j - 1]!;
    if ("?!.,:*_~'\"".includes(c)) {
      j--;
    } else if (c === ")" && closes > opens) {
      closes--;
      j--;
    } else if (c === ";") {
      let k = j - 2;
      while (k >= domainStart && isAlphanumeric(text[k])) k--;
      if (k >= domainStart && text[k] === "&" && k < j - 2) j = k;
      else break;
    } else {
      break;
    }
  }
  return j > domainStart ? j : i;
}
