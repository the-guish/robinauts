// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The column widths: when they are decided, when they move, and what they
 * are decided from. Tables are written as Markdown and parsed, so the cells
 * are the parser's.
 */
import { describe, expect, it } from "vitest";

import { RESIZE_EVERY, tableColumns } from "./columns";
import { parse, type Block } from "./parse";

type Table = Extract<Block, { type: "table" }>;

/** The table of `source`, which is one. */
function table(source: string): Table {
  const [block] = parse(source);
  if (block?.type !== "table") throw new Error("not a table");
  return block;
}

/** A table under `| Name | Notes |` with these rows. */
function rows(...cells: [string, string][]): Table {
  return table(
    "| Name | Notes |\n|---|---|\n" +
      cells.map(([a, b]) => `| ${a} | ${b} |`).join("\n"),
  );
}

/** Weights in characters, as the percentages they come out as. */
function pct(weights: number[]): number[] {
  const total = weights.reduce((sum, w) => sum + w, 0);
  return weights.map((w) => Math.round((w * 1000) / total) / 10);
}

/** The widths after the first `seen` rows, the table not finished. */
function streamed(block: Table, seen: number): number[] {
  return tableColumns(block.head, block.rows.slice(0, seen), false);
}

/** The widths of the finished table. */
function finished(block: Table): number[] {
  return tableColumns(block.head, block.rows, true);
}

const SHORT: [string, string] = ["ab", "cd"];
const LONG: [string, string] = ["ab", "a much longer note than the others"];
const MANY = new Array<[string, string]>(9).fill(LONG);

describe("column widths", () => {
  it.each([
    ["a header", "| Name | Notes |\n|---|---|", [50, 50]],
    ["three columns", "|a|bb|c|\n|-|-|-|", [33.3, 33.3, 33.3]],
  ])("are equal before any row is finished, under %s", (_n, src, widths) => {
    expect(tableColumns(table(src).head, [], false)).toEqual(widths);
  });

  it("are sized by the header and the first row once it is finished", () => {
    // "Name" against "ab", and the note: its length, held to 32; and padding.
    expect(streamed(rows(LONG), 1)).toEqual(pct([7, 35]));
  });

  it.each([
    ["between two looks", 2],
    ["at the last row before a look", RESIZE_EVERY],
  ])("hold still %s", (_name, seen) => {
    const block = rows(SHORT, ...MANY);
    expect(streamed(block, seen)).toEqual(streamed(block, 1));
  });

  it("move at the next look when a row needs it", () => {
    const block = rows(SHORT, ...MANY);
    expect(streamed(block, 1)).toEqual(pct([7, 8]));
    expect(streamed(block, RESIZE_EVERY + 1)).toEqual(pct([7, 35]));
  });

  it("do not move for a small difference", () => {
    const block = rows(["abcdefghij", "abcdefghij"], ["abcdefghijk", "x"]);
    expect(finished(block)).toEqual(streamed(block, 1));
  });

  it("take a last look when the table is finished", () => {
    const block = rows(SHORT, LONG);
    expect(streamed(block, 2)).toEqual(pct([7, 8]));
    expect(finished(block)).toEqual(pct([7, 35]));
  });

  it("size a finished table with no rows by its header", () => {
    const block = table("| a | a much longer heading |\n|---|---|");
    expect(tableColumns(block.head, [], true)).toEqual(pct([6, 24]));
  });

  it("count the text of emphasis, links and code, not the markup", () => {
    expect(
      finished(rows(["ab", "**some** [words](https://a.test) `x`"])),
    ).toEqual(finished(rows(["ab", "some words x"])));
  });

  it("are none for a table with no columns", () => {
    expect(tableColumns([], [], true)).toEqual([]);
  });
});
