// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The tree from `./parse.ts`, drawn as React elements.
 *
 * Only elements and text: every string from the source reaches the page as
 * a React text child or an attribute value, which React escapes. There is
 * no HTML string anywhere and no `dangerouslySetInnerHTML`.
 *
 * The streaming cursor is threaded down the last branch of the tree, so
 * that it lands after the last text that was drawn -- inside the last
 * paragraph, the last list item, the last table cell or the last code
 * block -- wherever that is, rather than after the block that holds it.
 */
import { Fragment, type ReactNode } from "react";

import { CheckIcon, CopyIcon } from "../../icons";
import { IconButton } from "../IconButton";
import { useCopied } from "../useCopied";
import {
  fenceLanguage,
  safeHref,
  type Block,
  type Inline,
  type ListItem,
} from "./parse";

/** The "working" dot the chat shows while an answer is arriving. */
function Cursor(): ReactNode {
  return <span className="md-cursor" aria-hidden="true" />;
}

/** Draws blocks, with the cursor after the last one's text when asked. */
export function renderBlocks(blocks: Block[], cursor: boolean): ReactNode[] {
  const out = blocks.map((block, at) =>
    renderBlock(block, at, cursor && at === blocks.length - 1),
  );
  // Nothing to put it in: nothing yet, or a thematic break last.
  const last = blocks[blocks.length - 1];
  if (cursor && (last === undefined || last.type === "thematicBreak")) {
    out.push(<Cursor key="cursor" />);
  }
  return out;
}

function renderBlock(block: Block, key: number, cursor: boolean): ReactNode {
  switch (block.type) {
    case "paragraph":
      return (
        <p key={key} className="md-p">
          {renderInlines(block.children)}
          {cursor && <Cursor />}
        </p>
      );
    case "heading": {
      const Tag = `h${block.depth}` as const;
      return (
        <Tag key={key} className={`md-h${block.depth}`}>
          {renderInlines(block.children)}
          {cursor && <Cursor />}
        </Tag>
      );
    }
    case "code":
      return (
        <CodeBlock
          key={key}
          lang={block.lang}
          value={block.value}
          cursor={cursor}
        />
      );
    case "blockquote":
      return (
        <blockquote key={key} className="md-blockquote">
          {renderBlocks(block.children, cursor)}
        </blockquote>
      );
    case "list": {
      const items = block.items.map((item, at) =>
        renderItem(
          item,
          at,
          block.tight,
          cursor && at === block.items.length - 1,
        ),
      );
      return block.ordered ? (
        <ol
          key={key}
          className="md-ol"
          start={block.start === 1 ? undefined : block.start}
        >
          {items}
        </ol>
      ) : (
        <ul key={key} className="md-ul">
          {items}
        </ul>
      );
    }
    case "thematicBreak":
      return <hr key={key} className="md-hr" />;
    case "table":
      return renderTable(block, key, cursor);
  }
}

/**
 * A list item. In a tight list its paragraphs are drawn without `<p>`, as
 * CommonMark's HTML has it; a task item's checkbox goes in front of the
 * first paragraph's text, so that it sits on the line it belongs to.
 */
function renderItem(
  item: ListItem,
  key: number,
  tight: boolean,
  cursor: boolean,
): ReactNode {
  const box =
    item.checked === null ? null : (
      <input
        type="checkbox"
        className="md-task-box"
        checked={item.checked}
        disabled
        readOnly
      />
    );
  const children = item.children;
  const body = children.map((block, at) => {
    const last = cursor && at === children.length - 1;
    const prefix = at === 0 ? box : null;
    if (block.type === "paragraph" && (tight || prefix !== null)) {
      const content = (
        <>
          {prefix}
          {renderInlines(block.children)}
          {last && <Cursor />}
        </>
      );
      return tight ? (
        <Fragment key={at}>{content}</Fragment>
      ) : (
        <p key={at} className="md-p">
          {content}
        </p>
      );
    }
    return (
      <Fragment key={at}>
        {prefix}
        {renderBlock(block, at, last)}
      </Fragment>
    );
  });
  return (
    <li key={key} className={box === null ? "md-li" : "md-li md-task"}>
      {children.length === 0 && box}
      {body}
      {cursor && children.length === 0 && <Cursor />}
    </li>
  );
}

const ALIGN_CLASS = {
  left: "md-align-left",
  center: "md-align-center",
  right: "md-align-right",
} as const;

function renderTable(
  block: Extract<Block, { type: "table" }>,
  key: number,
  cursor: boolean,
): ReactNode {
  const cellClass = (base: string, column: number) => {
    const align = block.align[column];
    return align === null || align === undefined
      ? base
      : `${base} ${ALIGN_CLASS[align]}`;
  };
  const lastRow = block.rows.length - 1;
  const lastColumn = block.head.length - 1;
  return (
    <div key={key} className="md-table-wrap">
      <table className="md-table">
        <thead>
          <tr>
            {block.head.map((cell, column) => (
              <th key={column} className={cellClass("md-th", column)}>
                {renderInlines(cell)}
                {cursor && lastRow < 0 && column === lastColumn && <Cursor />}
              </th>
            ))}
          </tr>
        </thead>
        {block.rows.length > 0 && (
          <tbody>
            {block.rows.map((row, at) => (
              <tr key={at}>
                {row.map((cell, column) => (
                  <td key={column} className={cellClass("md-td", column)}>
                    {renderInlines(cell)}
                    {cursor && at === lastRow && column === lastColumn && (
                      <Cursor />
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        )}
      </table>
    </div>
  );
}

function renderInlines(nodes: Inline[]): ReactNode[] {
  return nodes.map((node, key) => renderInline(node, key));
}

function renderInline(node: Inline, key: number): ReactNode {
  switch (node.type) {
    case "text":
      return node.value;
    case "code":
      return (
        <code key={key} className="md-inline-code">
          {node.value}
        </code>
      );
    case "emphasis":
      return (
        <em key={key} className="md-em">
          {renderInlines(node.children)}
        </em>
      );
    case "strong":
      return (
        <strong key={key} className="md-strong">
          {renderInlines(node.children)}
        </strong>
      );
    case "delete":
      return (
        <del key={key} className="md-del">
          {renderInlines(node.children)}
        </del>
      );
    case "break":
      return <br key={key} />;
    case "link": {
      // An image has no `<img>`: the Content-Security-Policy allows no
      // remote image, and a link to it says what it is just as well.
      const children =
        node.image && node.children.length === 0
          ? ["image"]
          : renderInlines(node.children);
      // Checked again here, cheaply, so that this file is safe on its own
      // whatever tree it is handed.
      const href = node.href === null ? null : safeHref(node.href);
      if (href === null) return <Fragment key={key}>{children}</Fragment>;
      return (
        <a
          key={key}
          className={node.image ? "md-link md-image" : "md-link"}
          href={href}
          title={node.title ?? undefined}
          target="_blank"
          rel="noopener noreferrer"
        >
          {children}
        </a>
      );
    }
  }
}

interface CodeBlockProps {
  lang: string;
  value: string;
  cursor: boolean;
}

/** A fenced or indented code block, with its language and a copy button. */
function CodeBlock({ lang: given, value, cursor }: Readonly<CodeBlockProps>) {
  // Checked again here, cheaply, as a link's destination is: the language
  // becomes a class name, and this file is safe on its own whatever tree it
  // is handed.
  const lang = fenceLanguage(given);
  return (
    <div className="md-code-block">
      <div className="md-code-header">
        {lang !== "" && <span className="md-code-lang">{lang}</span>}
        <CopyButton text={value} />
      </div>
      <pre className="md-code">
        <code
          className={
            lang === "" ? "md-code-text" : `md-code-text language-${lang}`
          }
        >
          {value}
          {cursor && <Cursor />}
        </code>
      </pre>
    </div>
  );
}

/**
 * Copies the code, then shows a tick for a moment (`../useCopied.ts`, which
 * also says what happens where there is no clipboard).
 */
function CopyButton({ text }: Readonly<{ text: string }>) {
  const { copied, copy } = useCopied();
  return (
    <IconButton
      label="Copy code"
      className="md-code-copy"
      onClick={() => {
        copy(text);
      }}
    >
      {copied ? (
        <CheckIcon className="md-code-copy-icon" size={14} />
      ) : (
        <CopyIcon className="md-code-copy-icon" size={14} />
      )}
    </IconButton>
  );
}
