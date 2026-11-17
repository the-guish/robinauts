// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The widths of a table's columns, decided from its text.
 *
 * A browser sizes an ordinary table by its content, and does it again on
 * every change, so a table that arrives a few characters at a time has
 * columns that slide back and forth under the reader. Here the widths are
 * decided from the text instead, and only at a few rows:
 *
 * - With no finished row yet the columns are of equal width.
 * - At the first finished row they are sized by the header and that row.
 * - After that they are looked at again every `RESIZE_EVERY` rows, and when
 *   the table is finished, and changed only if a column would move by more
 *   than `RESIZE_THRESHOLD` of its width.
 *
 * A column's weight is its longest cell, in characters, held between
 * `MIN_LENGTH` and `MAX_LENGTH`, plus its padding; its width is its weight's
 * share of the table, in percent. The browser keeps a column no narrower
 * than its longest word, so on a narrow screen a long word can still widen
 * one, and the table scrolls; elsewhere the widths are these.
 *
 * It is a function of the rows alone, so a table drawn from history has the
 * widths it ended its stream with. One pass over the cells: linear, like the
 * parser.
 */
import type { Inline } from "./parse";

/** How many finished rows between two looks at the widths. */
export const RESIZE_EVERY = 5;

/** How much a column has to move, as a part of its width, to be moved. */
export const RESIZE_THRESHOLD = 0.15;

/** A column is sized by its longest cell, in characters, held to these. */
const MIN_LENGTH = 3;
const MAX_LENGTH = 32;

/** A cell's padding, 0.75rem a side, in characters, near enough. */
const PADDING = 3;

/**
 * Each column's width in percent, for `rows` of a table under `head`.
 *
 * `rows` are the finished rows; `finished` says the table is complete, which
 * is the last look at the widths.
 */
export function tableColumns(
  head: Inline[][],
  rows: Inline[][][],
  finished: boolean,
): number[] {
  const count = head.length;
  const longest = new Array<number>(count).fill(0);
  const fold = (cells: Inline[][]) => {
    for (let column = 0; column < count; column++) {
      const cell = cells[column];
      if (cell !== undefined) {
        longest[column] = Math.max(longest[column]!, length(cell));
      }
    }
  };
  fold(head);

  let widths = new Array<number>(count).fill(Math.max(0, ...sized(longest)));
  const look = (always: boolean) => {
    const candidate = sized(longest);
    if (always || moves(widths, candidate)) widths = candidate;
  };
  rows.forEach((row, at) => {
    fold(row);
    const seen = at + 1;
    const checkpoint = (seen - 1) % RESIZE_EVERY === 0;
    if (checkpoint || (finished && seen === rows.length)) look(seen === 1);
  });
  if (finished && rows.length === 0) look(false);
  const total = widths.reduce((sum, w) => sum + w, 0);
  // To a tenth: enough for a width, and short in the markup.
  return widths.map((w) => Math.round((w * 1000) / total) / 10);
}

/** The widths for those longest cells. */
function sized(longest: number[]): number[] {
  return longest.map(
    (l) => Math.min(Math.max(l, MIN_LENGTH), MAX_LENGTH) + PADDING,
  );
}

/** Whether a column of `next` is more than the threshold off `now`. */
function moves(now: number[], next: number[]): boolean {
  // Compared as shares of the table, which is what the reader sees move.
  const total = (widths: number[]) => widths.reduce((sum, w) => sum + w, 0);
  const was = total(now);
  const will = total(next);
  return now.some((width, column) => {
    const before = width / was;
    return Math.abs(next[column]! / will - before) / before > RESIZE_THRESHOLD;
  });
}

/** A cell's text, in characters: what it shows, not its markup. */
function length(cell: Inline[]): number {
  let total = 0;
  for (const node of cell) {
    switch (node.type) {
      case "text":
      case "code":
        total += node.value.length;
        break;
      case "break":
        break;
      default:
        total += length(node.children);
    }
  }
  return total;
}
