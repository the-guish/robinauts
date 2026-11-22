// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The history as the panel draws it.
 *
 * Rendered inside the real `Panel`, and driven by the real `useHistory`:
 * what these prove is the thing that is on the screen, not a component in
 * isolation with a hand-made list passed to it.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { Panel } from "../shell/Panel";
import { callsTo, json, refusal, stubFetch, type Call } from "../test/api";
import { conversation, id, page } from "../test/conversations";
import { hashOf, InShell } from "../test/shell";
import { useHistory } from "./history";
import { STILL_ANSWERING, TOO_LONG } from "./HistoryList";

/** The panel, with the history hook wired to it as the shell wires it. */
function WithHistory({ current = null }: { current?: string | null }) {
  return (
    <InShell>
      <Panelled current={current} />
    </InShell>
  );
}

function Panelled({ current }: { current: string | null }) {
  const history = useHistory();
  return (
    <Panel
      history={history}
      current={current}
      collapsed={false}
      onCollapse={() => undefined}
      onNewChat={() => undefined}
      onSearch={() => undefined}
    />
  );
}

/** Render it, and let the first page arrive. */
async function panel(props: Parameters<typeof WithHistory>[0] = {}) {
  const drawn = render(<WithHistory {...props} />);
  await waitFor(() => {
    expect(screen.queryByText("Loading the conversations…")).toBeNull();
  });
  return drawn;
}

const rows = () => screen.getAllByRole("listitem");

test("each conversation is a link to its own hash, and the open one is marked", async () => {
  stubFetch(() =>
    json(page([conversation(1, "First"), conversation(2, "Second")])),
  );
  await panel({ current: id(2) });

  const first = screen.getByRole("link", { name: "First" });
  expect(first).toHaveAttribute("href", hashOf(`/c/${id(1)}`));
  expect(first).not.toHaveAttribute("aria-current");
  expect(screen.getByRole("link", { name: "Second" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(rows()).toHaveLength(2);
});

test("a conversation with no title still has something to aim at", async () => {
  // A title is the beginning of the first message, and a message with no
  // text in it gives none (docs/specs/conversations.md, "Titles").
  stubFetch(() => json(page([conversation(1, "   ")])));
  await panel();
  expect(screen.getByRole("link", { name: "Untitled" })).toBeVisible();
});

test("an empty history says so, and a refused one says why", async () => {
  stubFetch(() => json(page([])));
  const { unmount } = await panel();
  expect(screen.getByText("No conversations yet.")).toBeVisible();
  unmount();

  stubFetch(() =>
    refusal(500, "InternalError", "the request could not be served"),
  );
  await panel();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "The conversations could not be loaded: the request could not be served",
  );
  expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
});

test("a page with a cursor offers the next one", async () => {
  const answers = [
    json(page([conversation(1)], "cursor-1")),
    json(page([conversation(2)])),
  ];
  const fetch = stubFetch(() => answers.shift() ?? json(page([])));
  await panel();

  const more = screen.getByRole("button", { name: "Load more" });
  await act(async () => {
    fireEvent.click(more);
  });
  await waitFor(() => {
    expect(rows()).toHaveLength(2);
  });
  expect(callsTo(fetch, "/robinauts/api/conversations")[1]?.url).toContain(
    "cursor=cursor-1",
  );
  // The last page hands back no cursor, so the button goes.
  expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
});

/** Open a row's menu, and answer with the pages `answers` gives. */
async function withMenu(answer: (call: Call) => Response) {
  const fetch = stubFetch(answer);
  await panel({ current: id(1) });
  fireEvent.click(screen.getByRole("button", { name: /^Actions for First/ }));
  return fetch;
}

const listing = () => json(page([conversation(1, "First")]));

test("renaming is inline: Enter saves it and the list is asked for again", async () => {
  const asked: Call[] = [];
  const fetch = await withMenu((call) => {
    asked.push(call);
    return call.method === "PATCH"
      ? json(conversation(1, "Renamed"))
      : listing();
  });

  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  const box = screen.getByRole("textbox", { name: "Title" });
  // The focus follows the box, so a keyboard types into it without hunting.
  expect(box).toHaveFocus();

  fireEvent.change(box, { target: { value: "Renamed" } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });

  expect(asked.filter((call) => call.method === "PATCH")).toEqual([
    {
      url: `/robinauts/api/conversations/${id(1)}`,
      method: "PATCH",
      body: { title: "Renamed" },
    },
  ]);
  await waitFor(() => {
    expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(2);
  });
  // The box has gone, and the focus is back where it was opened from.
  expect(screen.queryByRole("textbox", { name: "Title" })).toBeNull();
  expect(
    screen.getByRole("button", { name: /^Actions for First/ }),
  ).toHaveFocus();
});

test("Escape leaves the title as it was, and sends nothing", async () => {
  const fetch = await withMenu(() => listing());
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  const box = screen.getByRole("textbox", { name: "Title" });
  fireEvent.change(box, { target: { value: "Not this" } });
  fireEvent.keyDown(box, { key: "Escape" });

  expect(screen.queryByRole("textbox", { name: "Title" })).toBeNull();
  expect(screen.getByRole("link", { name: "First" })).toBeVisible();
  expect(
    fetch.mock.calls.filter(([, init]) => init?.method === "PATCH"),
  ).toHaveLength(0);
  expect(
    screen.getByRole("button", { name: /^Actions for First/ }),
  ).toHaveFocus();
});

test("Escape inside a row does not reach the drawer around it", async () => {
  // The innermost thing that is open is what Escape shuts; the shell's own
  // listener is on the document, and this must not get that far.
  const closed = vi.fn();
  stubFetch(() => listing());
  const drawn = render(<WithHistory current={id(1)} />);
  await waitFor(() => {
    expect(screen.queryByText("Loading the conversations…")).toBeNull();
  });
  document.addEventListener("keydown", closed);
  try {
    fireEvent.click(screen.getByRole("button", { name: /^Actions for First/ }));
    fireEvent.click(screen.getByRole("button", { name: "Rename" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Title" }), {
      key: "Escape",
    });
    expect(closed).not.toHaveBeenCalled();
  } finally {
    document.removeEventListener("keydown", closed);
    drawn.unmount();
  }
});

test("a rename that is refused is said in the row, and the box stays open", async () => {
  await withMenu((call) =>
    call.method === "PATCH"
      ? refusal(422, "InvalidValueError", "a title is one line")
      : listing(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  const box = screen.getByRole("textbox", { name: "Title" });
  fireEvent.change(box, { target: { value: "Something" } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });

  expect(screen.getByRole("alert")).toHaveTextContent("a title is one line");
  expect(screen.getByRole("textbox", { name: "Title" })).toBeVisible();
});

test("an empty title is refused here rather than at the server", async () => {
  const fetch = await withMenu(() => listing());
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  const box = screen.getByRole("textbox", { name: "Title" });
  fireEvent.change(box, { target: { value: "   " } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "A title needs something",
  );
  expect(
    fetch.mock.calls.filter(([, init]) => init?.method === "PATCH"),
  ).toHaveLength(0);
});

test("deleting asks first, and No leaves everything alone", async () => {
  const fetch = await withMenu(() => listing());
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect(screen.getByText("Delete this conversation?")).toBeVisible();
  // The safe answer is what the focus lands on.
  const no = screen.getByRole("button", { name: /^No, keep it/ });
  expect(no).toHaveFocus();

  fireEvent.click(no);
  expect(screen.queryByText("Delete this conversation?")).toBeNull();
  expect(
    fetch.mock.calls.filter(([, init]) => init?.method === "DELETE"),
  ).toHaveLength(0);
  expect(screen.getByRole("link", { name: "First" })).toBeVisible();
});

test("Yes deletes it, the list is asked for again, and the page leaves it", async () => {
  let deleted = false;
  const fetch = await withMenu((call) => {
    if (call.method === "DELETE") {
      deleted = true;
      return json(null, 204);
    }
    return deleted ? json(page([])) : listing();
  });
  location.hash = hashOf(`/c/${id(1)}`);

  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /^Yes, delete/ }));
  });

  expect(
    fetch.mock.calls.filter(
      ([url, init]) =>
        init?.method === "DELETE" &&
        String(url) === `/robinauts/api/conversations/${id(1)}`,
    ),
  ).toHaveLength(1);
  await waitFor(() => {
    expect(screen.getByText("No conversations yet.")).toBeVisible();
  });
  // It was the conversation the page was on, and there is no trash.
  expect(location.hash).toBe(hashOf(""));
});

test("a conversation that is still answering is not deleted, and says so", async () => {
  const fetch = await withMenu((call) =>
    call.method === "DELETE"
      ? refusal(
          409,
          "RunAlreadyActiveError",
          "run 7 is running in conversation 1",
        )
      : listing(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /^Yes, delete/ }));
  });

  // The API's own detail names a run and a state, for an operator's log.
  expect(screen.getByRole("alert")).toHaveTextContent(STILL_ANSWERING);
  expect(screen.getByRole("link", { name: "First" })).toBeVisible();
  expect(callsTo(fetch, "/robinauts/api/conversations")).toHaveLength(1);
});

test("the box keeps the focus while a rename is in the air, and Escape stays in the row", async () => {
  // A disabled control loses the focus to `<body>`, and Escape from there
  // reaches the shell's own listener and closes the drawer instead of this.
  const held = new Promise<Response>(() => undefined);
  const fetch = vi.fn<typeof globalThis.fetch>((_, init) =>
    init?.method === "PATCH" ? held : Promise.resolve(listing()),
  );
  vi.stubGlobal("fetch", fetch);
  const drawn = render(<WithHistory current={id(1)} />);
  await waitFor(() => {
    expect(screen.queryByText("Loading the conversations…")).toBeNull();
  });
  fireEvent.click(screen.getByRole("button", { name: /^Actions for First/ }));
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  const box = screen.getByRole("textbox", { name: "Title" });
  fireEvent.change(box, { target: { value: "Renamed" } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });

  // Still there, still holding the focus, and read-only rather than disabled.
  expect(box).toHaveFocus();
  expect(box).toHaveAttribute("readonly");
  expect(box).toHaveAttribute("aria-disabled", "true");

  const escaped = vi.fn();
  document.addEventListener("keydown", escaped);
  try {
    fireEvent.keyDown(box, { key: "Escape" });
    expect(escaped).not.toHaveBeenCalled();
  } finally {
    document.removeEventListener("keydown", escaped);
    drawn.unmount();
  }
});

test("a rename whose row is not on the fresh page leaves the focus on the list", async () => {
  // The write is followed by a first page, and this conversation may not be
  // on it. The focus would otherwise be dropped on <body>.
  let renamed = false;
  await withMenu((call) => {
    if (call.method === "PATCH") {
      renamed = true;
      return json(conversation(1, "Renamed"));
    }
    return renamed ? json(page([conversation(2, "Another")])) : listing();
  });
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  const box = screen.getByRole("textbox", { name: "Title" });
  fireEvent.change(box, { target: { value: "Renamed" } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });

  await waitFor(() => {
    expect(screen.queryByRole("link", { name: "First" })).toBeNull();
  });
  expect(screen.getByRole("list", { name: "Conversations" })).toHaveFocus();
});

test("a page after the first that is refused says which ask it was", async () => {
  const answers = [
    json(page([conversation(1)], "cursor-1")),
    refusal(422, "InvalidCursorError", "cursor: not a cursor of ours"),
  ];
  stubFetch(() => answers.shift() ?? json(page([])));
  await panel();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
  });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "More conversations could not be loaded: cursor: not a cursor of ours",
  );
});

test("a title is bounded in code points, as the backend counts them", async () => {
  const asked: Call[] = [];
  await withMenu((call) => {
    asked.push(call);
    return call.method === "PATCH" ? json(conversation(1, "ok")) : listing();
  });
  fireEvent.click(screen.getByRole("button", { name: "Rename" }));
  const box = screen.getByRole("textbox", { name: "Title" });

  // 121 characters: one over, and refused here with the sentence the route
  // would have answered with.
  fireEvent.change(box, { target: { value: "a".repeat(121) } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(screen.getByRole("alert")).toHaveTextContent(TOO_LONG);
  expect(asked.filter((call) => call.method === "PATCH")).toHaveLength(0);

  // 61 emoji: 122 UTF-16 units, which an HTML `maxlength` counts, and 61
  // characters, which is what the backend counts. It goes.
  const emoji = "🐦".repeat(61);
  fireEvent.change(box, { target: { value: emoji } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter" });
  });
  expect(asked.filter((call) => call.method === "PATCH")).toEqual([
    {
      url: `/robinauts/api/conversations/${id(1)}`,
      method: "PATCH",
      body: { title: emoji },
    },
  ]);
});

test("a control that says what it does says what it does it to", async () => {
  // "Rename" and "Yes, delete" are on every row; the title goes after the
  // visible words, so speech input still reaches them by what they say.
  const untitled = {
    ...conversation(1, ""),
    id: "9a7c1d2e-0000-4000-8000-000000000001",
  };
  stubFetch(() => json(page([untitled])));
  await panel();
  const actions = screen.getByRole("button", {
    name: "Actions for Untitled 9a7c1d2e",
  });
  fireEvent.click(actions);
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  expect(
    screen.getByRole("button", { name: "Yes, delete: Untitled 9a7c1d2e" }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "No, keep it: Untitled 9a7c1d2e" }),
  ).toBeVisible();
});
