// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The parser, tree by tree.
 *
 * Every case here is written for this project; none is taken from the
 * CommonMark or GFM specifications, whose examples are under a licence
 * this project does not take in. What is drawn from the trees is tested in
 * `./Markdown.test.tsx`.
 */
import { describe, expect, it } from "vitest";

import {
  MAX_BLOCK_DEPTH,
  parse,
  safeHref,
  type Block,
  type Inline,
} from "./parse";

/** The inlines of a one-paragraph document. */
function inline(source: string): Inline[] {
  const blocks = parse(source);
  expect(blocks).toHaveLength(1);
  const [block] = blocks;
  if (block?.type !== "paragraph") {
    throw new Error(`expected a paragraph, got ${block?.type}`);
  }
  return block.children;
}

const text = (value: string): Inline => ({ type: "text", value });
const link = (
  href: string | null,
  children: Inline[],
  title: string | null = null,
): Inline => ({ type: "link", href, title, image: false, children });

/** Every block, depth first, for checking the shape of a big tree. */
function walk(blocks: Block[], visit: (block: Block) => void): void {
  const stack = [...blocks];
  while (stack.length > 0) {
    const block = stack.pop()!;
    visit(block);
    if (block.type === "blockquote") stack.push(...block.children);
    if (block.type === "list") {
      for (const item of block.items) stack.push(...item.children);
    }
  }
}

describe("paragraphs and line breaks", () => {
  it("splits paragraphs on blank lines and keeps soft breaks as newlines", () => {
    expect(parse("Robins sing\nat dawn.\n\nOwls do not.")).toEqual([
      { type: "paragraph", children: [text("Robins sing\nat dawn.")] },
      { type: "paragraph", children: [text("Owls do not.")] },
    ]);
  });

  it("strips the indentation of continuation lines and trailing space", () => {
    expect(inline("first line   \n   second line   ")).toEqual([
      text("first line"),
      { type: "break" },
      text("second line"),
    ]);
  });

  it("makes a hard break of two spaces or a backslash, not of one space", () => {
    expect(inline("one  \ntwo\\\nthree \nfour")).toEqual([
      text("one"),
      { type: "break" },
      text("two"),
      { type: "break" },
      text("three\nfour"),
    ]);
  });

  it("parses the empty document and a blank one to nothing", () => {
    expect(parse("")).toEqual([]);
    expect(parse("\n \n\t\n")).toEqual([]);
  });

  it("treats CR LF and lone CR as line endings", () => {
    expect(parse("a\r\nb\rc\r\n\r\nd")).toEqual([
      { type: "paragraph", children: [text("a\nb\nc")] },
      { type: "paragraph", children: [text("d")] },
    ]);
  });

  it("replaces NUL with the replacement character", () => {
    expect(inline("a\0b")).toEqual([text("a�b")]);
  });
});

describe("headings", () => {
  it("reads all six ATX levels", () => {
    const source = [1, 2, 3, 4, 5, 6]
      .map((n) => `${"#".repeat(n)} Level ${n}`)
      .join("\n");
    expect(parse(source)).toEqual(
      [1, 2, 3, 4, 5, 6].map((depth) => ({
        type: "heading",
        depth,
        children: [text(`Level ${depth}`)],
      })),
    );
  });

  it("refuses seven hashes and a hash with no space after it", () => {
    expect(parse("####### seven")[0]?.type).toBe("paragraph");
    expect(parse("#hashtag")[0]?.type).toBe("paragraph");
  });

  it("drops a closing sequence but keeps a hash that is part of the text", () => {
    expect(parse("## Nests ##   ")).toEqual([
      { type: "heading", depth: 2, children: [text("Nests")] },
    ]);
    expect(parse("# Issue #42")).toEqual([
      { type: "heading", depth: 1, children: [text("Issue #42")] },
    ]);
    expect(parse("# C#")).toEqual([
      { type: "heading", depth: 1, children: [text("C#")] },
    ]);
  });

  it("allows an empty heading", () => {
    expect(parse("#")).toEqual([{ type: "heading", depth: 1, children: [] }]);
  });

  it("parses emphasis inside a heading", () => {
    expect(parse("### *Why* robins")).toEqual([
      {
        type: "heading",
        depth: 3,
        children: [
          { type: "emphasis", children: [text("Why")] },
          text(" robins"),
        ],
      },
    ]);
  });

  it("reads setext headings underlined with = and -", () => {
    expect(parse("Dawn chorus\n===\n\nEvening song\n---\n")).toEqual([
      { type: "heading", depth: 1, children: [text("Dawn chorus")] },
      { type: "heading", depth: 2, children: [text("Evening song")] },
    ]);
  });

  it("interrupts a paragraph", () => {
    expect(parse("text\n# heading").map((b) => b.type)).toEqual([
      "paragraph",
      "heading",
    ]);
  });
});

describe("code", () => {
  it("reads a backtick fence with its language", () => {
    expect(parse("```python\nprint('hi')\n\n  indented\n```")).toEqual([
      {
        type: "code",
        lang: "python",
        value: "print('hi')\n\n  indented",
      },
    ]);
  });

  it("reads a tilde fence, and keeps only the first word of the info", () => {
    expect(parse("~~~ts linenums=on\nlet a = 1;\n~~~")).toEqual([
      { type: "code", lang: "ts", value: "let a = 1;" },
    ]);
  });

  it("needs a closing fence at least as long as the opening one", () => {
    expect(parse("````\n```\nstill code\n````\nafter")).toEqual([
      { type: "code", lang: "", value: "```\nstill code" },
      { type: "paragraph", children: [text("after")] },
    ]);
  });

  it("does not close a backtick fence with tildes", () => {
    expect(parse("```\n~~~\n```")).toEqual([
      { type: "code", lang: "", value: "~~~" },
    ]);
  });

  it("keeps the language's own punctuation, as in c++ and c#", () => {
    expect(parse("```c++\nint main();\n```")[0]).toMatchObject({
      lang: "c++",
    });
    expect(parse("```c#\n```")[0]).toMatchObject({ lang: "c#" });
  });

  it("ends the language at the first character a class name cannot hold", () => {
    // A form feed would start a second class, written or as a reference.
    expect(parse("```js&#12;chat-visually-hidden\n```")[0]).toMatchObject({
      lang: "js",
    });
    expect(parse("```js\fchat-visually-hidden\n```")[0]).toMatchObject({
      lang: "js",
    });
    expect(parse("```{.python}\n```")[0]).toMatchObject({ lang: "" });
  });

  it("refuses a backtick in a backtick fence's info string", () => {
    expect(parse("``` a`b\n")[0]?.type).toBe("paragraph");
  });

  it("removes the fence's own indentation from its lines", () => {
    expect(parse("  ```\n  two\n    four\n one\n  ```")).toEqual([
      { type: "code", lang: "", value: "two\n  four\none" },
    ]);
  });

  it("keeps tabs inside a fence, which a Makefile needs", () => {
    expect(parse("```make\nall:\n\tcc main.c\n```")).toEqual([
      { type: "code", lang: "make", value: "all:\n\tcc main.c" },
    ]);
  });

  it("runs an unclosed fence to the end of the document", () => {
    expect(parse("```js\nconst a = 1;\nconst b")).toEqual([
      { type: "code", lang: "js", value: "const a = 1;\nconst b" },
    ]);
  });

  it("keeps markup inside code as it is", () => {
    expect(parse("```\n**not bold** <b>&amp;</b>\n```")).toEqual([
      { type: "code", lang: "", value: "**not bold** <b>&amp;</b>" },
    ]);
  });

  it("ends the language at an escape or a reference, undecoded", () => {
    // Read before decoding, so that nothing a reference spells can reach the
    // class name; `c++` is written as itself.
    expect(parse("``` c&#43;&#43;\nx\n```")[0]).toEqual({
      type: "code",
      lang: "c",
      value: "x",
    });
    expect(parse("``` c\\+\nx\n```")[0]).toMatchObject({ lang: "c" });
  });

  it("reads indented code, without trailing blank lines", () => {
    expect(parse("    line one\n\n      line two\n\n\nafter")).toEqual([
      { type: "code", lang: "", value: "line one\n\n  line two" },
      { type: "paragraph", children: [text("after")] },
    ]);
  });

  it("reads a tab as indentation for indented code", () => {
    expect(parse("\tcode")).toEqual([
      { type: "code", lang: "", value: "code" },
    ]);
  });

  it("does not let indented code interrupt a paragraph", () => {
    expect(parse("prose\n    more prose")).toEqual([
      { type: "paragraph", children: [text("prose\nmore prose")] },
    ]);
  });
});

describe("inline code", () => {
  it("reads runs of any length", () => {
    expect(inline("`a` and ``b`c`` and ```d``e```")).toEqual([
      { type: "code", value: "a" },
      text(" and "),
      { type: "code", value: "b`c" },
      text(" and "),
      { type: "code", value: "d``e" },
    ]);
  });

  it("strips one space from both ends, but not from all-space content", () => {
    expect(inline("`` `tick` `` and `  `")).toEqual([
      { type: "code", value: "`tick`" },
      text(" and "),
      { type: "code", value: "  " },
    ]);
  });

  it("turns line breaks inside into spaces", () => {
    expect(inline("`one\ntwo`")).toEqual([{ type: "code", value: "one two" }]);
  });

  it("leaves an unmatched run as text", () => {
    expect(inline("a `b ``c")).toEqual([text("a `b ``c")]);
  });

  it("does not process escapes, emphasis or references inside", () => {
    expect(inline("`*a* \\_ &amp;`")).toEqual([
      { type: "code", value: "*a* \\_ &amp;" },
    ]);
  });

  it("binds tighter than emphasis and links", () => {
    expect(inline("*a `b*` [c `d](e)`")).toEqual([
      text("*a "),
      { type: "code", value: "b*" },
      text(" [c "),
      { type: "code", value: "d](e)" },
    ]);
  });
});

describe("emphasis, strong and strikethrough", () => {
  it("reads * and _ for emphasis and doubled for strong", () => {
    expect(inline("*a* _b_ **c** __d__")).toEqual([
      { type: "emphasis", children: [text("a")] },
      text(" "),
      { type: "emphasis", children: [text("b")] },
      text(" "),
      { type: "strong", children: [text("c")] },
      text(" "),
      { type: "strong", children: [text("d")] },
    ]);
  });

  it("nests strong in emphasis for a triple run", () => {
    expect(inline("***loud***")).toEqual([
      {
        type: "emphasis",
        children: [{ type: "strong", children: [text("loud")] }],
      },
    ]);
  });

  it("nests different kinds inside each other", () => {
    expect(inline("**bold with *italic* inside**")).toEqual([
      {
        type: "strong",
        children: [
          text("bold with "),
          { type: "emphasis", children: [text("italic")] },
          text(" inside"),
        ],
      },
    ]);
  });

  it("allows intraword * but not intraword _", () => {
    expect(inline("un*frigging*believable snake_case_name")).toEqual([
      text("un"),
      { type: "emphasis", children: [text("frigging")] },
      text("believable snake_case_name"),
    ]);
  });

  it("does not open emphasis before a space", () => {
    expect(inline("2 * 3 * 4")).toEqual([text("2 * 3 * 4")]);
  });

  it("applies the rule of three", () => {
    expect(inline("*one**two*")).toEqual([
      { type: "emphasis", children: [text("one**two")] },
    ]);
  });

  it("leaves what does not match as text", () => {
    expect(inline("**unclosed and *also")).toEqual([
      text("**unclosed and *also"),
    ]);
    expect(inline("closed** before")).toEqual([text("closed** before")]);
  });

  it("matches the nearest opener and leaves the rest", () => {
    expect(inline("**a *b**")).toEqual([
      text("*"),
      {
        type: "emphasis",
        children: [text("a "), { type: "emphasis", children: [text("b")] }],
      },
    ]);
    expect(inline("*a **b*")).toEqual([
      text("*a *"),
      { type: "emphasis", children: [text("b")] },
    ]);
  });

  it("reads ~~ as strikethrough and a single ~ as text", () => {
    expect(inline("~~gone~~ but ~5 remain and ~~~three~~~")).toEqual([
      { type: "delete", children: [text("gone")] },
      text(" but ~5 remain and ~~~three~~~"),
    ]);
  });

  it("puts emphasis inside strikethrough", () => {
    expect(inline("~~*old* news~~")).toEqual([
      {
        type: "delete",
        children: [
          { type: "emphasis", children: [text("old")] },
          text(" news"),
        ],
      },
    ]);
  });

  it("does not let emphasis cross a link's brackets", () => {
    expect(inline("*a [b* c](https://x.test)")).toEqual([
      text("*a "),
      link("https://x.test", [text("b* c")]),
    ]);
  });

  it("treats punctuation around the run as the specification does", () => {
    expect(inline('"*quoted*"')).toEqual([
      text('"'),
      { type: "emphasis", children: [text("quoted")] },
      text('"'),
    ]);
  });
});

describe("escapes and character references", () => {
  it("escapes ASCII punctuation and nothing else", () => {
    expect(inline("\\*not em\\* \\a \\\\")).toEqual([text("*not em* \\a \\")]);
  });

  it("keeps a trailing backslash", () => {
    expect(inline("ends with \\")).toEqual([text("ends with \\")]);
  });

  it("decodes named, decimal and hex references", () => {
    expect(
      inline("&amp; &lt; &gt; &quot; &#39; &#x27; &copy; &hellip;"),
    ).toEqual([text("& < > \" ' ' © …")]);
  });

  it("keeps unknown names and malformed references as written", () => {
    expect(inline("&bogus; &amp &#; &#x; &#12345678;")).toEqual([
      text("&bogus; &amp &#; &#x; &#12345678;"),
    ]);
  });

  it("replaces NUL and out-of-range code points", () => {
    expect(inline("&#0; &#xD800; &#x110000;")).toEqual([text("� � �")]);
  });

  it("does not make markup out of a decoded reference", () => {
    expect(inline("&#42;not emphasis&#42;")).toEqual([text("*not emphasis*")]);
  });
});

describe("links", () => {
  it("reads an inline link with and without a title", () => {
    expect(
      inline(
        '[docs](https://example.test/a "The docs") and [b](http://b.test)',
      ),
    ).toEqual([
      link("https://example.test/a", [text("docs")], "The docs"),
      text(" and "),
      link("http://b.test", [text("b")]),
    ]);
  });

  it("reads titles in single quotes and parentheses", () => {
    expect(
      inline("[a](https://a.test 'one') [b](https://b.test (two))"),
    ).toEqual([
      link("https://a.test", [text("a")], "one"),
      text(" "),
      link("https://b.test", [text("b")], "two"),
    ]);
  });

  it("reads a destination in angle brackets, which may hold spaces", () => {
    expect(inline("[a](<https://a.test/my file>)")).toEqual([
      link("https://a.test/my file", [text("a")]),
    ]);
  });

  it("balances parentheses in a destination", () => {
    expect(inline("[w](https://w.test/Robin_(bird))")).toEqual([
      link("https://w.test/Robin_(bird)", [text("w")]),
    ]);
  });

  it("decodes escapes and references in destinations and titles", () => {
    expect(
      inline('[a](https://a.test/\\(x\\)?q=1&amp;r=2 "say &quot;hi&quot;")'),
    ).toEqual([link("https://a.test/(x)?q=1&r=2", [text("a")], 'say "hi"')]);
  });

  it("parses emphasis and code in the link text", () => {
    expect(inline("[*see* `this`](https://a.test)")).toEqual([
      link("https://a.test", [
        { type: "emphasis", children: [text("see")] },
        text(" "),
        { type: "code", value: "this" },
      ]),
    ]);
  });

  it("does not nest links: the inner one wins", () => {
    expect(inline("[outer [inner](https://in.test)](tail)")).toEqual([
      text("[outer "),
      link("https://in.test", [text("inner")]),
      text("](tail)"),
    ]);
  });

  it("leaves brackets that make no link as text", () => {
    expect(inline("[just brackets] and [half](")).toEqual([
      text("[just brackets] and [half]("),
    ]);
    expect(inline("a ] b [ c")).toEqual([text("a ] b [ c")]);
  });

  it("reads reference links in all three forms", () => {
    const source = [
      "[full][Robin] [Robin][] [robin]",
      "",
      '[robin]: https://robin.test "Robins"',
    ].join("\n");
    const target = (children: Inline[]) =>
      link("https://robin.test", children, "Robins");
    expect(parse(source)).toEqual([
      {
        type: "paragraph",
        children: [
          target([text("full")]),
          text(" "),
          target([text("Robin")]),
          text(" "),
          target([text("robin")]),
        ],
      },
    ]);
  });

  it("matches reference labels without regard to case or spacing", () => {
    expect(parse("[A  Bird]\n\n[a bird]: https://bird.test")).toEqual([
      {
        type: "paragraph",
        children: [link("https://bird.test", [text("A  Bird")])],
      },
    ]);
  });

  it("keeps the first definition of a label", () => {
    expect(
      parse("[x]: https://one.test\n[x]: https://two.test\n\n[x]"),
    ).toEqual([
      { type: "paragraph", children: [link("https://one.test", [text("x")])] },
    ]);
  });

  it("removes definitions from the text, and keeps text after them", () => {
    expect(parse("[x]: https://x.test\nThen prose [x].")).toEqual([
      {
        type: "paragraph",
        children: [
          text("Then prose "),
          link("https://x.test", [text("x")]),
          text("."),
        ],
      },
    ]);
  });

  it("reads a definition's title on the next line", () => {
    expect(parse("[x]: https://x.test\n  'Title'\n\n[x]")).toEqual([
      {
        type: "paragraph",
        children: [link("https://x.test", [text("x")], "Title")],
      },
    ]);
  });

  it("leaves an undefined reference as text", () => {
    expect(inline("[nothing][here]")).toEqual([text("[nothing][here]")]);
  });

  it("reads URI and e-mail autolinks", () => {
    expect(inline("<https://a.test/x?y=1> <robin@nest.test>")).toEqual([
      link("https://a.test/x?y=1", [text("https://a.test/x?y=1")]),
      text(" "),
      link("mailto:robin@nest.test", [text("robin@nest.test")]),
    ]);
  });

  it("reads bare URLs and www addresses", () => {
    expect(inline("See https://a.test/path and www.b.test today.")).toEqual([
      text("See "),
      link("https://a.test/path", [text("https://a.test/path")]),
      text(" and "),
      link("http://www.b.test", [text("www.b.test")]),
      text(" today."),
    ]);
  });

  it("leaves trailing punctuation and unbalanced parentheses out of a bare URL", () => {
    expect(inline("(see https://a.test/x_(y)).")).toEqual([
      text("(see "),
      link("https://a.test/x_(y)", [text("https://a.test/x_(y)")]),
      text(")."),
    ]);
  });

  it("does not link a bare URL inside a link's text or inside code", () => {
    expect(inline("[https://a.test](https://a.test) `https://b.test`")).toEqual(
      [
        link("https://a.test", [text("https://a.test")]),
        text(" "),
        { type: "code", value: "https://b.test" },
      ],
    );
  });

  it("does not link a bare host with no dot, or a URL inside a word", () => {
    expect(inline("http://localhost and xhttps://a.test")).toEqual([
      text("http://localhost and xhttps://a.test"),
    ]);
  });

  it("draws an image as a link with its alt text", () => {
    expect(inline("![a *robin*](https://img.test/robin.png)")).toEqual([
      {
        type: "link",
        href: "https://img.test/robin.png",
        title: null,
        image: true,
        children: [text("a robin")],
      },
    ]);
  });

  it("allows an image inside a link", () => {
    expect(
      inline("[![logo](https://i.test/l.png)](https://home.test)"),
    ).toEqual([link("https://home.test", [text("logo")])]);
  });
});

describe("link safety", () => {
  it.each([
    "https://a.test",
    "http://a.test",
    "HTTPS://A.TEST",
    "mailto:robin@nest.test",
    "MailTo:robin@nest.test",
  ])("allows %s", (url) => {
    expect(safeHref(url)).toBe(url);
  });

  it.each([
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    " \u0001javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "//evil.test/x",
    "/relative/path",
    "relative",
    "ftp://files.test",
    "",
    ":nothing",
  ])("refuses %j", (url) => {
    expect(safeHref(url)).toBeNull();
  });

  it("trims spaces and controls at the ends, as a browser would", () => {
    expect(safeHref("  https://a.test \u0000")).toBe("https://a.test");
  });

  it("refuses unsafe schemes in every kind of link, after decoding", () => {
    const blocks = parse(
      [
        "[a](javascript:alert(1))",
        "[b](&#106;avascript:alert(1))",
        "[c](java&#x09;script:alert(1))",
        "<javascript:alert(1)>",
        "![d](data:image/png;base64,AAAA)",
        "[e][r]",
        "[f](//evil.test)",
        "",
        "[r]: vbscript:x",
      ].join("\n"),
    );
    const links: Inline[] = [];
    walk(blocks, (block) => {
      if (block.type === "paragraph") {
        links.push(...block.children.filter((n) => n.type === "link"));
      }
    });
    expect(links).toHaveLength(7);
    for (const node of links) {
      expect(node).toMatchObject({ type: "link", href: null });
    }
  });
});

describe("raw HTML", () => {
  it("is text, inline and as a block", () => {
    expect(parse("<div>\n<script>alert(1)</script>\n</div>")).toEqual([
      {
        type: "paragraph",
        children: [text("<div>\n<script>alert(1)</script>\n</div>")],
      },
    ]);
    expect(inline('a <img src=x onerror="alert(1)"> b')).toEqual([
      text('a <img src=x onerror="alert(1)"> b'),
    ]);
  });
});

describe("blockquotes", () => {
  it("holds blocks, with or without a space after the marker", () => {
    expect(parse("> # Title\n>text\n> - item")).toEqual([
      {
        type: "blockquote",
        children: [
          { type: "heading", depth: 1, children: [text("Title")] },
          { type: "paragraph", children: [text("text")] },
          {
            type: "list",
            ordered: false,
            start: 1,
            tight: true,
            items: [
              {
                checked: null,
                children: [{ type: "paragraph", children: [text("item")] }],
              },
            ],
          },
        ],
      },
    ]);
  });

  it("nests", () => {
    expect(parse("> outer\n>> inner")).toEqual([
      {
        type: "blockquote",
        children: [
          { type: "paragraph", children: [text("outer")] },
          {
            type: "blockquote",
            children: [{ type: "paragraph", children: [text("inner")] }],
          },
        ],
      },
    ]);
  });

  it("continues a paragraph lazily, and ends at a blank line", () => {
    expect(parse("> a quote\nthat goes on\n\nnot quoted")).toEqual([
      {
        type: "blockquote",
        children: [
          { type: "paragraph", children: [text("a quote\nthat goes on")] },
        ],
      },
      { type: "paragraph", children: [text("not quoted")] },
    ]);
  });
});

describe("thematic breaks", () => {
  it("reads ***, --- and ___ with spaces between", () => {
    expect(parse("***\n- - -\n_____\n *  *  * ")).toEqual(
      Array.from({ length: 4 }, () => ({ type: "thematicBreak" })),
    );
  });

  it("needs three of one character and nothing else", () => {
    expect(parse("**")[0]?.type).toBe("paragraph");
    expect(parse("*-*")[0]?.type).toBe("paragraph");
    expect(parse("--- a")[0]?.type).toBe("paragraph");
  });

  it("interrupts a paragraph with *** ", () => {
    expect(parse("text\n***").map((b) => b.type)).toEqual([
      "paragraph",
      "thematicBreak",
    ]);
  });
});

describe("lists", () => {
  it("reads bullets and numbers, with the start number", () => {
    expect(parse("- one\n- two\n\n3. three\n4. four")).toEqual([
      {
        type: "list",
        ordered: false,
        start: 1,
        tight: true,
        items: [
          {
            checked: null,
            children: [{ type: "paragraph", children: [text("one")] }],
          },
          {
            checked: null,
            children: [{ type: "paragraph", children: [text("two")] }],
          },
        ],
      },
      {
        type: "list",
        ordered: true,
        start: 3,
        tight: true,
        items: [
          {
            checked: null,
            children: [{ type: "paragraph", children: [text("three")] }],
          },
          {
            checked: null,
            children: [{ type: "paragraph", children: [text("four")] }],
          },
        ],
      },
    ]);
  });

  it("starts a new list when the bullet or delimiter changes", () => {
    const blocks = parse("- a\n+ b\n* c\n1. d\n2) e");
    expect(blocks.map((b) => b.type)).toEqual([
      "list",
      "list",
      "list",
      "list",
      "list",
    ]);
  });

  it("nests by indentation", () => {
    const blocks = parse("- fruit\n  - apple\n    - green\n- bread");
    expect(blocks).toHaveLength(1);
    const outer = blocks[0];
    if (outer?.type !== "list") throw new Error("not a list");
    expect(outer.items).toHaveLength(2);
    const inner = outer.items[0]!.children[1];
    if (inner?.type !== "list") throw new Error("not a nested list");
    expect(inner.items[0]!.children[1]).toMatchObject({
      type: "list",
      items: [{ children: [{ type: "paragraph", children: [text("green")] }] }],
    });
  });

  it("nests under a numbered item by the item's content column", () => {
    const blocks = parse("10. ten\n    - under ten\n11. eleven");
    const list = blocks[0];
    if (list?.type !== "list") throw new Error("not a list");
    expect(list.start).toBe(10);
    expect(list.items[0]!.children.map((b) => b.type)).toEqual([
      "paragraph",
      "list",
    ]);
  });

  it("is loose when items are separated by a blank line", () => {
    expect(parse("- a\n\n- b")[0]).toMatchObject({
      type: "list",
      tight: false,
    });
  });

  it("is loose when blocks inside an item are separated by a blank line", () => {
    expect(parse("- a\n\n  b\n- c")[0]).toMatchObject({
      type: "list",
      tight: false,
    });
  });

  it("stays tight when the blank line is inside a nested list or a fence", () => {
    expect(parse("- a\n  - b\n\n  - c\n- d")[0]).toMatchObject({ tight: true });
    expect(parse("- a\n  ```\n  x\n\n  y\n  ```\n- b")[0]).toMatchObject({
      tight: true,
    });
  });

  it("stays tight with a blank line after the last item", () => {
    expect(parse("- a\n- b\n\nafter")[0]).toMatchObject({ tight: true });
  });

  it("holds code and quotes inside an item", () => {
    const blocks = parse("1. step\n\n   ```sh\n   make\n   ```\n\n   > note");
    const list = blocks[0];
    if (list?.type !== "list") throw new Error("not a list");
    expect(list.items[0]!.children.map((b) => b.type)).toEqual([
      "paragraph",
      "code",
      "blockquote",
    ]);
    expect(list.items[0]!.children[1]).toEqual({
      type: "code",
      lang: "sh",
      value: "make",
    });
  });

  it("does not let a number other than one interrupt a paragraph", () => {
    expect(parse("In\n1984. it rained")).toEqual([
      { type: "paragraph", children: [text("In\n1984. it rained")] },
    ]);
    expect(parse("Steps:\n1. first").map((b) => b.type)).toEqual([
      "paragraph",
      "list",
    ]);
  });

  it("does not let an empty item interrupt a paragraph", () => {
    expect(parse("text\n*")).toEqual([
      { type: "paragraph", children: [text("text\n*")] },
    ]);
    // A lone `-` there is a heading underline instead.
    expect(parse("text\n-")[0]?.type).toBe("heading");
  });

  it("reads an empty item", () => {
    expect(parse("-\n- b")[0]).toMatchObject({
      type: "list",
      items: [{ children: [] }, { children: [{ type: "paragraph" }] }],
    });
  });

  it("needs a space after the marker", () => {
    expect(parse("-not a list\n1.nor this")[0]?.type).toBe("paragraph");
  });

  it("reads task items, checked and not", () => {
    const blocks = parse(
      "- [ ] to do\n- [x] done\n- [X] done too\n- [y] not a task",
    );
    const list = blocks[0];
    if (list?.type !== "list") throw new Error("not a list");
    expect(list.items.map((item) => item.checked)).toEqual([
      false,
      true,
      true,
      null,
    ]);
    expect(list.items[0]!.children).toEqual([
      { type: "paragraph", children: [text("to do")] },
    ]);
    expect(list.items[3]!.children).toEqual([
      { type: "paragraph", children: [text("[y] not a task")] },
    ]);
  });

  it("reads a task item with nothing after the box yet", () => {
    const list = parse("- [ ]")[0];
    expect(list).toMatchObject({ items: [{ checked: false, children: [] }] });
  });

  it("does not read a box that is not at the start", () => {
    const list = parse("- buy [x] eggs")[0];
    expect(list).toMatchObject({ items: [{ checked: null }] });
  });
});

describe("tables", () => {
  it("reads a header, alignments and rows", () => {
    const source = [
      "| Bird | Song | Count |",
      "|:-----|:----:|------:|",
      "| Robin | *loud* | 3 |",
      "| Wren | soft | 12 |",
    ].join("\n");
    expect(parse(source)).toEqual([
      {
        type: "table",
        align: ["left", "center", "right"],
        head: [[text("Bird")], [text("Song")], [text("Count")]],
        rows: [
          [
            [text("Robin")],
            [{ type: "emphasis", children: [text("loud")] }],
            [text("3")],
          ],
          [[text("Wren")], [text("soft")], [text("12")]],
        ],
      },
    ]);
  });

  it("reads a table without outer pipes", () => {
    expect(parse("a | b\n--- | ---\n1 | 2")[0]).toMatchObject({
      type: "table",
      align: [null, null],
      head: [[text("a")], [text("b")]],
      rows: [[[text("1")], [text("2")]]],
    });
  });

  it("pads short rows and cuts long ones", () => {
    expect(parse("|a|b|\n|-|-|\n|1|\n|1|2|3|")[0]).toMatchObject({
      rows: [
        [[text("1")], []],
        [[text("1")], [text("2")]],
      ],
    });
  });

  it("reads an escaped pipe inside a cell, even in code", () => {
    expect(parse("|a|b|\n|-|-|\n|x \\| y|`p\\|q`|")[0]).toMatchObject({
      rows: [[[text("x | y")], [{ type: "code", value: "p|q" }]]],
    });
  });

  it("is not a table when the delimiter row does not match the header", () => {
    expect(parse("|a|b|\n|-|")[0]?.type).toBe("paragraph");
    expect(parse("|a|b|\n|-|x|")[0]?.type).toBe("paragraph");
  });

  it("ends at a blank line or at another block", () => {
    expect(parse("|a|\n|-|\n|1|\n\nafter").map((b) => b.type)).toEqual([
      "table",
      "paragraph",
    ]);
    expect(parse("|a|\n|-|\n|1|\n> quote").map((b) => b.type)).toEqual([
      "table",
      "blockquote",
    ]);
  });

  it("keeps the lines before the header as a paragraph", () => {
    expect(parse("Some birds:\n|a|b|\n|-|-|").map((b) => b.type)).toEqual([
      "paragraph",
      "table",
    ]);
  });

  it("is not a table without a pipe anywhere", () => {
    expect(parse("a\n---")[0]?.type).toBe("heading");
  });

  it("is a table up to 128 columns, and text past them", () => {
    const table = (width: number) =>
      "|a".repeat(width) + "|\n" + "|-".repeat(width) + "|\n|b|";
    const widest = parse(table(128))[0];
    expect(widest?.type).toBe("table");
    if (widest?.type !== "table") return;
    expect(widest.head).toHaveLength(128);
    // The one-cell row has the header's width.
    expect(widest.rows).toHaveLength(1);
    expect(widest.rows[0]).toHaveLength(128);
    expect(parse(table(129)).map((block) => block.type)).toEqual(["paragraph"]);
  });
});

describe("streaming", () => {
  const streaming = { streaming: true };

  it("never throws on any prefix of a rich document", () => {
    const doc = [
      "# Title with *emphasis*",
      "",
      'A paragraph with **strong**, `code`, [a link](https://a.test "t"),',
      "an image ![alt](https://i.test/x.png) and <https://auto.test>.\\",
      "",
      "> quote",
      "> > nested",
      "",
      "1. one",
      "   - [ ] task",
      "   - [x] done",
      "",
      "| a | b |",
      "|:--|--:|",
      "| 1 | 2 |",
      "",
      "```ts",
      "const x = 1;",
      "```",
      "",
      "~~gone~~ &amp; &#x27; done",
      "",
      "[r]: https://r.test",
    ].join("\n");
    for (let end = 0; end <= doc.length; end++) {
      const prefix = doc.slice(0, end);
      expect(() => parse(prefix, streaming)).not.toThrow();
      expect(() => parse(prefix)).not.toThrow();
    }
  });

  it("draws an unclosed fence as code so far", () => {
    expect(parse("```py\ndef f():\n    ret", streaming)).toEqual([
      { type: "code", lang: "py", value: "def f():\n    ret" },
    ]);
  });

  it("does not show a closing fence being typed as a line of code", () => {
    expect(parse("```\nx\n``", streaming)).toEqual([
      { type: "code", lang: "", value: "x" },
    ]);
    // Without streaming it is what it is: a line of the unclosed fence.
    expect(parse("```\nx\n``")).toEqual([
      { type: "code", lang: "", value: "x\n``" },
    ]);
  });

  it("keeps unclosed emphasis and a dangling bracket as text", () => {
    expect(parse("some **bold and [link", streaming)).toEqual([
      { type: "paragraph", children: [text("some **bold and [link")] },
    ]);
  });

  it("does not take a lone - being typed for a heading underline", () => {
    expect(parse("A line\n-", streaming)).toEqual([
      { type: "paragraph", children: [text("A line\n-")] },
    ]);
    expect(parse("A line\n-\n", streaming)[0]?.type).toBe("heading");
  });

  it("makes the table as soon as its delimiter row starts", () => {
    expect(parse("| a | b |\n| --", streaming)).toEqual([
      {
        type: "table",
        align: [null, null],
        head: [[text("a")], [text("b")]],
        rows: [],
      },
    ]);
    expect(parse("| a | b |\n| -- | :", streaming)[0]).toMatchObject({
      type: "table",
      align: [null, null],
    });
    // Once the line is finished, it has to be a whole delimiter row.
    expect(parse("| a | b |\n| --\n", streaming)[0]?.type).toBe("paragraph");
  });

  it("keeps a half link as text, with the bare URL GFM finds in it", () => {
    expect(parse("see [docs](https://a.te", streaming)).toEqual([
      {
        type: "paragraph",
        children: [
          text("see [docs]("),
          link("https://a.te", [text("https://a.te")]),
        ],
      },
    ]);
    expect(parse("see [docs](/pa", streaming)).toEqual([
      { type: "paragraph", children: [text("see [docs](/pa")] },
    ]);
  });
});

describe("limits", () => {
  it("caps the nesting of quotes and keeps the rest as text", () => {
    const blocks = parse(">".repeat(MAX_BLOCK_DEPTH + 5) + " deep");
    let depth = 0;
    let block: Block | undefined = blocks[0];
    while (block?.type === "blockquote") {
      depth++;
      block = block.children[0];
    }
    expect(depth).toBe(MAX_BLOCK_DEPTH);
    expect(block).toEqual({
      type: "paragraph",
      children: [text(">>>>> deep")],
    });
  });

  it("flattens emphasis nested past the cap, keeping all the text", () => {
    const deep = "*a ".repeat(200) + "core" + " a*".repeat(200);
    const children = inline(deep);
    let depth = 0;
    let level: Inline[] = children;
    let content = "";
    const collect = (nodes: Inline[]) => {
      const stack = [...nodes].reverse();
      while (stack.length > 0) {
        const node = stack.pop()!;
        if (node.type === "text") content += node.value;
        else if ("children" in node)
          stack.push(...[...node.children].reverse());
      }
    };
    collect(children);
    while (level[0]?.type === "emphasis") {
      depth++;
      level = level[0].children;
    }
    expect(depth).toBeLessThanOrEqual(24);
    expect(content.replaceAll(" ", "")).toBe(
      `${"a".repeat(200)}core${"a".repeat(200)}`,
    );
  });
});

describe("performance", () => {
  // Each case is a few hundred kilobytes shaped to make a naive parser
  // quadratic. A linear one does each in milliseconds; the bound is loose
  // enough for a slow CI machine and far below what quadratic would cost.
  const BOUND_MS = 1000;
  const N = 200_000;
  const cases: [string, () => string][] = [
    ["a run of *", () => "a" + "*".repeat(N) + "a"],
    ["alternating * and letters", () => "*a".repeat(N / 2)],
    [
      "unclosed openers then closers",
      () => "*a ".repeat(N / 3) + " a*".repeat(N / 3),
    ],
    ["a run of _", () => "_a".repeat(N / 2)],
    ["a run of [", () => "[".repeat(N)],
    ["brackets then closers", () => "[".repeat(N / 2) + "]".repeat(N / 2)],
    ["unfinished links", () => "[a](".repeat(N / 4)],
    ["unfinished links with titles", () => '[a](b "'.repeat(N / 7)],
    ["unfinished images", () => "![a](b".repeat(N / 6)],
    ["links in links", () => "[a ".repeat(N / 6) + "[b](c)".repeat(N / 6)],
    [
      "references with definitions",
      () => "[x]".repeat(N / 3) + "\n\n[x]: https://x.test",
    ],
    ["a run of backticks", () => "`".repeat(N)],
    [
      "backtick runs of growing length",
      () => {
        let s = "";
        for (let n = 1; s.length < N; n++) s += "`".repeat(n) + "a";
        return s;
      },
    ],
    ["single backticks unmatched", () => "`a``".repeat(N / 4)],
    ["a run of >", () => ">".repeat(N)],
    [
      "many lines of deep quotes",
      () => (">".repeat(100) + " a\n").repeat(N / 102),
    ],
    [
      "a deep quote then lazy lines",
      () => ">".repeat(5000) + " a\n" + "b\n".repeat(N / 2),
    ],
    [
      "deeply nested list items",
      () => {
        let s = "";
        for (let n = 0; s.length < N; n++)
          s += " ".repeat(2 * (n % 400)) + "- x\n";
        return s;
      },
    ],
    ["a run of list markers", () => "- ".repeat(N / 2)],
    [
      "a table with thousands of cells",
      () => {
        const width = 20;
        const head = "|" + " h |".repeat(width);
        const rule = "|" + "---|".repeat(width);
        const row = "|" + " *c* `d` |".repeat(width);
        return [head, rule, ...Array.from({ length: 1000 }, () => row)].join(
          "\n",
        );
      },
    ],
    ["autolinks", () => "<a:".repeat(N / 3)],
    ["bare URLs", () => "https://a ".repeat(N / 10)],
    ["entities", () => "&#".repeat(N / 2)],
    ["escapes", () => "\\".repeat(N)],
    ["a megabyte paragraph", () => "Robins sing before dawn. ".repeat(42_000)],
    ["a megabyte of lines", () => "a line of prose\n".repeat(64_000)],
  ];

  // Wide tables, smaller than the cases above but quadratic in a parser that
  // pads every short row to the header: a header of thousands of columns
  // over thousands of one-cell rows. Past `MAX_TABLE_COLUMNS` it is no table.
  const wide: [string, () => string][] = [
    [
      "a 2000-column header over 2000 short rows",
      () =>
        "|a".repeat(2000) +
        "|\n" +
        "|-".repeat(2000) +
        "|\n" +
        "b\n".repeat(2000),
    ],
    [
      "a 5000-column header over 66666 short rows",
      () =>
        "|a".repeat(5000) +
        "|\n" +
        "|-".repeat(5000) +
        "|\n" +
        "b\n".repeat(66666),
    ],
  ];

  it.each(wide)("parses %s in well under a second", (_name, make) => {
    const source = make();
    for (const streaming of [false, true]) {
      const started = performance.now();
      parse(source, { streaming });
      expect(performance.now() - started).toBeLessThan(BOUND_MS);
    }
  });

  it.each(cases)("parses %s in well under a second", (_name, make) => {
    const source = make();
    expect(source.length).toBeGreaterThan(100_000);
    for (const streaming of [false, true]) {
      const started = performance.now();
      parse(source, { streaming });
      expect(performance.now() - started).toBeLessThan(BOUND_MS);
    }
  });
});
