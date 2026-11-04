// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The component: what reaches the page, what never does, the cursor, the
 * memoisation and the copy button. The parser's trees are tested in
 * `./parse.test.ts`; here the same constructs are checked as DOM, because
 * the DOM is what the chat, its tests and the end-to-end suite read.
 *
 * Every case is written for this project, none taken from a specification.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { COPIED_MS } from "../useCopied";
import { Markdown } from "./Markdown";
import { parse } from "./parse";

// The real parser, watched, so that the memoisation can be counted.
vi.mock("./parse", async (importOriginal) => {
  const real = await importOriginal<typeof import("./parse")>();
  return { ...real, parse: vi.fn(real.parse) };
});

/** The rendered root, `div.md`. */
function draw(text: string, streaming = false): HTMLElement {
  const { container } = render(<Markdown text={text} streaming={streaming} />);
  const root = container.firstElementChild;
  if (!(root instanceof HTMLElement)) throw new Error("nothing rendered");
  return root;
}

/** The markup inside the root, for small exact comparisons. */
function markup(text: string, streaming = false): string {
  return draw(text, streaming).innerHTML;
}

describe("blocks", () => {
  it("draws the root and paragraphs", () => {
    const root = draw("First.\n\nSecond\nline.");
    expect(root.tagName).toBe("DIV");
    expect(root.className).toBe("md");
    expect(root.innerHTML).toBe(
      '<p class="md-p">First.</p><p class="md-p">Second\nline.</p>',
    );
    expect(screen.getAllByRole("paragraph")).toHaveLength(2);
  });

  it("draws headings at their level", () => {
    draw("# One\n## Two\n### Three\n#### Four\n##### Five\n###### Six");
    for (const [level, name] of [
      [1, "One"],
      [2, "Two"],
      [3, "Three"],
      [4, "Four"],
      [5, "Five"],
      [6, "Six"],
    ] as const) {
      const heading = screen.getByRole("heading", { level, name });
      expect(heading).toHaveClass(`md-h${level}`);
    }
  });

  it("draws block quotes, nested", () => {
    expect(markup("> outer\n> > inner")).toBe(
      '<blockquote class="md-blockquote"><p class="md-p">outer</p>' +
        '<blockquote class="md-blockquote"><p class="md-p">inner</p>' +
        "</blockquote></blockquote>",
    );
  });

  it("draws a thematic break", () => {
    expect(markup("above\n\n---\n\nbelow")).toBe(
      '<p class="md-p">above</p><hr class="md-hr"><p class="md-p">below</p>',
    );
  });

  it("draws raw HTML blocks as text", () => {
    const root = draw('<div onclick="alert(1)">\n<b>hi</b>\n</div>');
    expect(root.querySelector("div div, b")).toBeNull();
    expect(root.textContent).toBe(
      '<div onclick="alert(1)">\n<b>hi</b>\n</div>',
    );
  });
});

describe("lists", () => {
  it("draws a tight list without paragraphs", () => {
    expect(markup("- one\n- two")).toBe(
      '<ul class="md-ul"><li class="md-li">one</li><li class="md-li">two</li></ul>',
    );
  });

  it("draws a loose list with paragraphs", () => {
    expect(markup("1. one\n\n2. two")).toBe(
      '<ol class="md-ol"><li class="md-li"><p class="md-p">one</p></li>' +
        '<li class="md-li"><p class="md-p">two</p></li></ol>',
    );
  });

  it("carries the start number of an ordered list, and only when it is not 1", () => {
    const root = draw("7. seven\n8. eight");
    expect(root.querySelector("ol")).toHaveAttribute("start", "7");
    expect(draw("1. one").querySelector("ol")).not.toHaveAttribute("start");
  });

  it("nests lists inside items", () => {
    const root = draw("- fruit\n  1. apple\n  2. pear\n- bread");
    const items = root.querySelectorAll(":scope > ul > li");
    expect(items).toHaveLength(2);
    expect(items[0]!.querySelectorAll("ol > li")).toHaveLength(2);
    expect(items[0]!.firstChild?.textContent).toBe("fruit");
  });

  it("draws task items as disabled checkboxes", () => {
    draw("- [ ] feed the robin\n- [x] fill the bath");
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes).toHaveLength(2);
    expect(boxes[0]).not.toBeChecked();
    expect(boxes[1]).toBeChecked();
    for (const box of boxes) {
      expect(box).toBeDisabled();
      expect(box).toHaveClass("md-task-box");
      expect(box.closest("li")).toHaveClass("md-li", "md-task");
    }
    expect(boxes[0]!.closest("li")).toHaveTextContent("feed the robin");
  });

  it("puts the box inside the first paragraph of a loose task item", () => {
    const root = draw("- [x] done\n\n- [ ] not yet");
    const paragraph = root.querySelector("li > p");
    expect(paragraph?.firstChild).toBeInstanceOf(HTMLInputElement);
  });

  it("draws an empty task item as just its box", () => {
    const item = draw("- [ ]").querySelector("li");
    expect(item?.childNodes).toHaveLength(1);
    expect(item?.firstElementChild).toBe(screen.getByRole("checkbox"));
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });
});

describe("inlines", () => {
  it("draws emphasis, strong, strikethrough and inline code", () => {
    expect(markup("*a* **b** ~~c~~ `d`")).toBe(
      '<p class="md-p"><em class="md-em">a</em> <strong class="md-strong">b</strong> ' +
        '<del class="md-del">c</del> <code class="md-inline-code">d</code></p>',
    );
  });

  it("draws hard breaks", () => {
    expect(markup("one  \ntwo\\\nthree")).toBe(
      '<p class="md-p">one<br>two<br>three</p>',
    );
  });

  it("decodes character references and backslash escapes", () => {
    expect(draw("&lt;b&gt; &amp; &#39;q&#x27; \\*").textContent).toBe(
      "<b> & 'q' *",
    );
  });

  it("draws links that open in a new tab", () => {
    draw('[the docs](https://example.test/docs "Read them")');
    const anchor = screen.getByRole("link", { name: "the docs" });
    expect(anchor).toHaveAttribute("href", "https://example.test/docs");
    expect(anchor).toHaveAttribute("title", "Read them");
    expect(anchor).toHaveAttribute("target", "_blank");
    expect(anchor).toHaveAttribute("rel", "noopener noreferrer");
    expect(anchor).toHaveClass("md-link");
  });

  it("draws autolinks, bare URLs and mailto links", () => {
    draw("<https://a.test> www.b.test <robin@nest.test> mailto-free text");
    expect(
      screen.getByRole("link", { name: "https://a.test" }),
    ).toHaveAttribute("href", "https://a.test");
    expect(screen.getByRole("link", { name: "www.b.test" })).toHaveAttribute(
      "href",
      "http://www.b.test",
    );
    expect(
      screen.getByRole("link", { name: "robin@nest.test" }),
    ).toHaveAttribute("href", "mailto:robin@nest.test");
  });

  it("draws reference links", () => {
    draw("Read [the guide][g].\n\n[g]: https://guide.test");
    expect(screen.getByRole("link", { name: "the guide" })).toHaveAttribute(
      "href",
      "https://guide.test",
    );
  });

  it("draws an image as a link to it, named by its alt text, never as <img>", () => {
    const root = draw("![a robin on a branch](https://img.test/robin.jpg)");
    expect(root.querySelector("img")).toBeNull();
    const anchor = screen.getByRole("link", { name: "a robin on a branch" });
    expect(anchor).toHaveAttribute("href", "https://img.test/robin.jpg");
    expect(anchor).toHaveClass("md-link", "md-image");
  });

  it("names an image with no alt text", () => {
    draw("![](https://img.test/x.png)");
    expect(screen.getByRole("link", { name: "image" })).toBeInTheDocument();
  });
});

describe("tables", () => {
  it("draws a header, a body and the column alignments", () => {
    const root = draw(
      "| Bird | Song | Count |\n|:--|:-:|--:|\n| Robin | loud | 3 |",
    );
    expect(
      root.querySelector(".md-table-wrap > table.md-table"),
    ).not.toBeNull();
    const headers = screen.getAllByRole("columnheader");
    expect(headers.map((h) => h.textContent)).toEqual([
      "Bird",
      "Song",
      "Count",
    ]);
    expect(headers.map((h) => h.className)).toEqual([
      "md-th md-align-left",
      "md-th md-align-center",
      "md-th md-align-right",
    ]);
    const cells = screen.getAllByRole("cell");
    expect(cells.map((c) => c.textContent)).toEqual(["Robin", "loud", "3"]);
    expect(cells[2]).toHaveClass("md-td", "md-align-right");
    expect(root.querySelector("table")).not.toHaveAttribute("style");
  });

  it("draws no body for a table with only a header", () => {
    expect(draw("|a|\n|-|").querySelector("tbody")).toBeNull();
  });
});

describe("code blocks", () => {
  it("draws the language, the copy button and the code", () => {
    const root = draw("```python\nprint('hi')\n```");
    const block = root.querySelector(".md-code-block");
    expect(block).not.toBeNull();
    const header = block!.querySelector(".md-code-header");
    expect(header?.querySelector(".md-code-lang")?.textContent).toBe("python");
    const button = screen.getByRole("button", { name: "Copy code" });
    expect(header).toContainElement(button);
    expect(button).toHaveClass("md-code-copy");
    expect(button).toHaveAttribute("type", "button");
    const pre = block!.querySelector("pre.md-code");
    expect(pre?.previousElementSibling).toBe(header);
    const code = pre?.querySelector("code");
    expect(code).toHaveClass("md-code-text", "language-python");
    expect(code?.textContent).toBe("print('hi')");
  });

  it("has no language label when there is no language", () => {
    const root = draw("    indented code");
    expect(root.querySelector(".md-code-lang")).toBeNull();
    expect(root.querySelector("code")?.className).toBe("md-code-text");
    expect(
      screen.getByRole("button", { name: "Copy code" }),
    ).toBeInTheDocument();
  });

  it("draws only the language's safe prefix, as the class and the label", () => {
    const root = draw("```js&#12;chat-visually-hidden\nlet a;\n```");
    expect([...root.querySelector("code")!.classList]).toEqual([
      "md-code-text",
      "language-js",
    ]);
    expect(root.querySelector(".md-code-lang")?.textContent).toBe("js");
  });

  it("keeps a language's own punctuation", () => {
    const root = draw("```c++\nint main();\n```");
    expect(root.querySelector("code")).toHaveClass("language-c++");
    expect(root.querySelector(".md-code-lang")?.textContent).toBe("c++");
  });

  it("keeps markup in code as text", () => {
    const root = draw("```html\n<script>alert(1)</script>\n```");
    expect(root.querySelector("script")).toBeNull();
    expect(root.querySelector("code")?.textContent).toBe(
      "<script>alert(1)</script>",
    );
  });
});

describe("the copy button", () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
  });

  afterEach(() => {
    Reflect.deleteProperty(navigator, "clipboard");
    vi.useRealTimers();
  });

  it("copies the code, shows a tick, then the copy icon again", async () => {
    vi.useFakeTimers();
    draw("```sh\nmake all\nmake test\n```");
    const button = screen.getByRole("button", { name: "Copy code" });
    expect(button.querySelector('[data-icon="copy"]')).not.toBeNull();

    await act(async () => {
      fireEvent.click(button);
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledWith("make all\nmake test");
    expect(button.querySelector('[data-icon="check"]')).not.toBeNull();
    expect(button.querySelector('[data-icon="copy"]')).toBeNull();

    act(() => {
      vi.advanceTimersByTime(COPIED_MS - 1);
    });
    expect(button.querySelector('[data-icon="check"]')).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(button.querySelector('[data-icon="copy"]')).not.toBeNull();
  });

  it("starts the tick again on a second copy, and names the button twice", async () => {
    vi.useFakeTimers();
    draw("```\nx\n```");
    const button = screen.getByRole("button", { name: "Copy code" });
    expect(button).toHaveAttribute("title", "Copy code");
    const press = () =>
      act(async () => {
        fireEvent.click(button);
        await Promise.resolve();
      });
    await press();
    act(() => {
      vi.advanceTimersByTime(COPIED_MS - 500);
    });
    await press();
    act(() => {
      vi.advanceTimersByTime(COPIED_MS - 1);
    });
    expect(button.querySelector('[data-icon="check"]')).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(button.querySelector('[data-icon="copy"]')).not.toBeNull();
  });

  it("does nothing when there is no clipboard", () => {
    Reflect.deleteProperty(navigator, "clipboard");
    draw("```\nx\n```");
    const button = screen.getByRole("button", { name: "Copy code" });
    expect(() => fireEvent.click(button)).not.toThrow();
    expect(button.querySelector('[data-icon="copy"]')).not.toBeNull();
  });

  it("does nothing when the clipboard refuses", async () => {
    writeText.mockImplementation(() => Promise.reject(new Error("denied")));
    draw("```\nx\n```");
    const button = screen.getByRole("button", { name: "Copy code" });
    await act(async () => {
      fireEvent.click(button);
      await Promise.resolve();
    });
    expect(button.querySelector('[data-icon="copy"]')).not.toBeNull();
  });
});

describe("safety", () => {
  it.each([
    "[x](javascript:alert(1))",
    "[x](JAVASCRIPT:alert(1))",
    "[x](&#106;avascript:alert(1))",
    "[x](java&#x0A;script:alert(1))",
    "[x](<java\\\nscript:alert(1)>)",
    "[x](data:text/html,<script>alert(1)</script>)",
    "[x](vbscript:msgbox)",
    "[x](file:///etc/passwd)",
    "[x](//evil.test)",
    "[x](/relative)",
    "[x](unknown:thing)",
    "![x](javascript:alert(1))",
    "<javascript:alert(1)>",
    "[x][r]\n\n[r]: javascript:alert(1)",
  ])("draws %j as text, without an anchor", (source) => {
    const root = draw(source);
    expect(root.querySelector("a")).toBeNull();
    expect(root.textContent).not.toBe("");
  });

  it("draws the text of a refused link", () => {
    expect(draw("[click me](javascript:alert(1))").textContent).toBe(
      "click me",
    );
  });

  it.each([
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    '<a href="javascript:alert(1)">x</a>',
    '<iframe src="https://evil.test"></iframe>',
    "<svg onload=alert(1)>",
    "<style>body{display:none}</style>",
    '<div style="position:fixed">x</div>',
  ])("draws %j as text", (source) => {
    const root = draw(`before ${source} after`);
    expect(root.textContent).toBe(`before ${source} after`);
    expect(
      root.querySelectorAll("script, img, a, iframe, svg, style, div"),
    ).toHaveLength(0);
    expect(root.querySelector("[onerror], [onload], [style]")).toBeNull();
  });

  it("never puts an event handler or a style attribute on anything", () => {
    const root = draw(
      [
        "# h",
        "*e* **s** ~~d~~ `c` [l](https://l.test) ![i](https://i.test)",
        "> q",
        "- [x] t",
        "|a|",
        "|-|",
        "|b|",
        "```js",
        "x",
        "```",
        "---",
      ].join("\n"),
    );
    for (const element of root.querySelectorAll("*")) {
      for (const attribute of element.getAttributeNames()) {
        expect(attribute.startsWith("on")).toBe(false);
        expect(attribute).not.toBe("style");
      }
    }
  });

  it("never sets inner HTML, whatever it draws", () => {
    // `dangerouslySetInnerHTML` is carried out by setting `innerHTML`, so a
    // renderer that never sets it never used it.
    const setter = vi.spyOn(Element.prototype, "innerHTML", "set");
    draw(
      [
        "# <b>h</b>",
        "<script>x</script> *e* [l](https://l.test) ![i](https://i.test)",
        "> <i>q</i>",
        "- [x] <u>t</u>",
        "|<a>|",
        "|-|",
        "```html",
        "<p>x</p>",
        "```",
      ].join("\n"),
      true,
    );
    expect(setter).not.toHaveBeenCalled();
    // And the spy does see it, so the line above means something.
    render(<div dangerouslySetInnerHTML={{ __html: "<b>x</b>" }} />);
    expect(setter).toHaveBeenCalled();
  });
});

describe("streaming", () => {
  const cursor = '<span class="md-cursor" aria-hidden="true"></span>';

  it("puts the cursor after the last text of a paragraph", () => {
    expect(markup("Because it is quiet then", true)).toBe(
      `<p class="md-p">Because it is quiet then${cursor}</p>`,
    );
  });

  it("puts no cursor when the answer is complete", () => {
    expect(draw("done", false).querySelector(".md-cursor")).toBeNull();
  });

  it("puts the cursor alone in an empty answer", () => {
    expect(markup("", true)).toBe(cursor);
  });

  it("puts exactly one cursor, inside the last block", () => {
    const cases: [string, string][] = [
      ["# Head", "h1"],
      ["para\n\n- a\n- b", "ul > li:last-child"],
      ["- a\n  - b\n  - c", "ul ul > li:last-child"],
      ["- a\n\n- b", "li:last-child > p"],
      ["> q\n> > deep", "blockquote blockquote > p"],
      ["```js\nconst a", "pre > code"],
      ["|a|b|\n|-|-|\n|1|2", "tbody tr:last-child > td:last-child"],
      ["|a|b|\n|-", "thead th:last-child"],
      ["- [ ]", "li"],
    ];
    for (const [source, where] of cases) {
      const root = draw(source, true);
      const cursors = root.querySelectorAll(".md-cursor");
      expect(cursors, source).toHaveLength(1);
      expect(cursors[0]!.parentElement, source).toBe(root.querySelector(where));
      expect(cursors[0]!.nextSibling, source).toBeNull();
    }
  });

  it("puts the cursor after a thematic break, at the root", () => {
    const root = draw("text\n\n***", true);
    expect(root.lastElementChild).toHaveClass("md-cursor");
    expect(root.lastElementChild?.previousElementSibling?.tagName).toBe("HR");
  });

  it("keeps the cursor out of the text the chat reads", () => {
    const root = draw("an answer", true);
    expect(screen.getByRole("paragraph")).toHaveTextContent(/^an answer$/);
    expect(root.querySelector(".md-cursor")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });

  it("draws every prefix of an answer without throwing", () => {
    const answer =
      "## Plan\n\nFirst **check `x`** then [see](https://a.test).\n\n" +
      "| k | v |\n|---|:-:|\n| a | 1 |\n\n```py\nprint(1)\n```\n\n- [x] ok\\";
    for (let end = 0; end <= answer.length; end += 3) {
      expect(() => draw(answer.slice(0, end), true)).not.toThrow();
    }
  });

  it("removes the cursor when the stream ends", () => {
    const { container, rerender } = render(
      <Markdown text="last words" streaming />,
    );
    expect(container.querySelector(".md-cursor")).not.toBeNull();
    rerender(<Markdown text="last words" streaming={false} />);
    expect(container.querySelector(".md-cursor")).toBeNull();
  });
});

describe("memoisation", () => {
  const parsed = vi.mocked(parse);

  beforeEach(() => {
    parsed.mockClear();
  });

  it("parses once per distinct text, however often it is drawn", () => {
    const { rerender } = render(<Markdown text="one" />);
    rerender(<Markdown text="one" />);
    rerender(<Markdown text="one" streaming={false} />);
    expect(parsed).toHaveBeenCalledTimes(1);
    rerender(<Markdown text="one two" />);
    expect(parsed).toHaveBeenCalledTimes(2);
    rerender(<Markdown text="one two" />);
    expect(parsed).toHaveBeenCalledTimes(2);
  });

  it("keeps the same elements when drawn again with the same text", () => {
    const { container, rerender } = render(<Markdown text="**same**" />);
    const before = container.querySelector("strong");
    rerender(<Markdown text="**same**" />);
    expect(container.querySelector("strong")).toBe(before);
  });
});
