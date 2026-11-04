// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { json, type Call } from "../../test/api";
import { conversation, id, message, opened } from "../../test/conversations";
import { event, streamHeaders, writable } from "../../test/stream";
import { Chat, type ChatProps } from "../index";
import type { ChatMessage } from "../state";
import { COPIED_MS } from "../useCopied";
import { AssistantMessage } from "./AssistantMessage";
import { DISCLOSURE_MS } from "./Disclosure";
import { useStickToBottom } from "./scroll";
import { useAutosize } from "./textarea";

const RUN = "11111111-2222-4333-8444-555555555555";
const CONVERSATION = id(1);
const TREE = [
  message("m1", "user", "Why do robins sing before dawn?"),
  message("m2", "assistant", "Because it is quiet then."),
];

function stub(answer: (call: Call) => Response | undefined) {
  const fetch = vi.fn<typeof globalThis.fetch>((input, init) => {
    const raw = init?.body;
    const call: Call = {
      url: String(input),
      method: (init?.method ?? "GET").toUpperCase(),
      body: typeof raw === "string" ? JSON.parse(raw) : undefined,
    };
    const given = answer(call);
    if (given === undefined) {
      throw new Error(`nothing answers ${call.method} ${call.url}`);
    }
    return Promise.resolve(given);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

/** The posts of turns into the conversation, by their bodies. */
function turnsIn(fetch: ReturnType<typeof stub>): unknown[] {
  return fetch.mock.calls
    .filter(
      ([url, init]) =>
        String(url) === `/api/conversations/${CONVERSATION}/turns` &&
        init?.method?.toUpperCase() === "POST",
    )
    .map(([, init]) => JSON.parse(String(init?.body)));
}

function draw(props: Partial<ChatProps> = {}) {
  return render(
    <Chat
      conversationId={CONVERSATION}
      agentId="helper"
      modelId="sonnet"
      onConversationStarted={vi.fn()}
      {...props}
    />,
  );
}

const settle = () =>
  act(async () => new Promise((done) => setTimeout(done, 0)));

/** A conversation of `TREE`, whose next turn streams into `stream`. */
async function conversationWithStream(messages = TREE): Promise<{
  fetch: ReturnType<typeof stub>;
  write: (chunk: string) => void;
  close: () => void;
}> {
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  const fetch = stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), messages));
    }
    if (call.url === `/api/conversations/${CONVERSATION}/turns`) {
      return response;
    }
    if (call.url.endsWith("/cancel")) return json({ id: RUN });
    return undefined;
  });
  draw();
  await waitFor(() => {
    expect(screen.getAllByRole("paragraph").length).toBeGreaterThan(0);
  });
  return { fetch, write, close };
}

afterEach(() => {
  vi.useRealTimers();
});

// ------------------------------------------------------------------ the box

test("Enter sends, and Shift+Enter is a new line", async () => {
  const { fetch, write, close } = await conversationWithStream();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "And at night?" } });
  fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
  await settle();
  expect(turnsIn(fetch)).toEqual([]);
  // Nothing was sent, so the line break is the browser's to make.
  expect(box).toHaveValue("And at night?");

  fireEvent.keyDown(box, { key: "Enter" });
  await settle();
  expect(turnsIn(fetch)).toEqual([
    { text: "And at night?", parent_id: "m2", model_id: "sonnet" },
  ]);
  // The box handed the message over.
  expect(box).toHaveValue("");
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
});

test("an Enter that ends the composition of a character is not a send", async () => {
  const { fetch } = await conversationWithStream();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "日本" } });
  fireEvent.keyDown(box, { key: "Enter", isComposing: true });
  fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
  await settle();
  expect(turnsIn(fetch)).toEqual([]);
});

test("Send is not offered for an empty box, or with nobody to send to", async () => {
  stub(() => json({ items: [] }));
  const drawn = draw({ conversationId: null });
  await settle();
  const send = screen.getByRole("button", { name: "Send message" });
  expect(send).toBeDisabled();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "   " } });
  expect(send).toBeDisabled();
  fireEvent.change(box, { target: { value: "Hello" } });
  expect(send).toBeEnabled();
  drawn.unmount();

  // No agent: the box takes text, and nothing is sent.
  draw({ conversationId: null, agentId: null });
  await settle();
  fireEvent.change(screen.getByRole("textbox", { name: "Message input" }), {
    target: { value: "Hello" },
  });
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
});

test("Escape stops the run that is going, and Enter is not a send meanwhile", async () => {
  const { fetch, write, close } = await conversationWithStream();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "And at night?" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await settle();
  expect(
    screen.getByRole("button", { name: "Stop generating" }),
  ).toBeInTheDocument();

  fireEvent.change(box, { target: { value: "Impatient." } });
  fireEvent.keyDown(box, { key: "Enter" });
  await settle();
  expect(turnsIn(fetch)).toHaveLength(1);
  expect(box).toHaveValue("Impatient.");

  fireEvent.keyDown(box, { key: "Escape" });
  await settle();
  expect(
    fetch.mock.calls.some(([url]) => String(url).endsWith("/cancel")),
  ).toBe(true);
  write(
    event(
      "RUN_FINISHED",
      { threadId: CONVERSATION, runId: RUN, outcome: { type: "cancelled" } },
      2,
    ),
  );
  close();
});

test("the box grows with what is written, and shrinks back", async () => {
  // jsdom lays nothing out, so a line is what this says it is.
  Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
    configurable: true,
    get(this: HTMLTextAreaElement) {
      return this.value.split("\n").length * 24;
    },
  });
  await conversationWithStream();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "one\ntwo\nthree" } });
  expect(box.style.height).toBe("72px");
  fireEvent.change(box, { target: { value: "" } });
  expect(box.style.height).toBe("24px");
  // Back to the element's own, from `Element.prototype`.
  Reflect.deleteProperty(HTMLTextAreaElement.prototype, "scrollHeight");
});

test("the box measures itself again when its width changes", () => {
  const watched: Element[] = [];
  let changed: () => void = () => undefined;
  const disconnect = vi.fn();
  class Watcher {
    constructor(callback: () => void) {
      changed = callback;
    }
    observe(element: Element) {
      watched.push(element);
    }
    unobserve() {}
    disconnect = disconnect;
  }
  vi.stubGlobal("ResizeObserver", Watcher);
  let width = 300;
  let lines = 1;
  Object.defineProperty(HTMLTextAreaElement.prototype, "clientWidth", {
    configurable: true,
    get: () => width,
  });
  Object.defineProperty(HTMLTextAreaElement.prototype, "scrollHeight", {
    configurable: true,
    get: () => lines * 24,
  });
  function Box() {
    const box = useAutosize("a long line that wraps when the box is narrow");
    return <textarea ref={box} aria-label="Box" readOnly />;
  }
  try {
    const drawn = render(<Box />);
    const box = screen.getByRole("textbox", { name: "Box" });
    expect(watched).toEqual([box]);
    expect(box.style.height).toBe("24px");
    // The same text over more lines in a narrower box.
    width = 150;
    lines = 2;
    act(() => {
      changed();
    });
    expect(box.style.height).toBe("48px");
    // A change of height alone is the box's own doing, and not measured.
    lines = 3;
    act(() => {
      changed();
    });
    expect(box.style.height).toBe("48px");
    drawn.unmount();
    expect(disconnect).toHaveBeenCalled();
  } finally {
    Reflect.deleteProperty(HTMLTextAreaElement.prototype, "clientWidth");
    Reflect.deleteProperty(HTMLTextAreaElement.prototype, "scrollHeight");
  }
});

test("the box keeps the keyboard when Send is clicked", async () => {
  const { write, close } = await conversationWithStream();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "And at night?" } });
  const send = screen.getByRole("button", { name: "Send message" });
  // A click on a button focuses it before it is a click.
  send.focus();
  await act(async () => {
    fireEvent.click(send);
    await settle();
  });
  expect(
    screen.getByRole("button", { name: "Stop generating" }),
  ).toBeInTheDocument();
  expect(box).toHaveFocus();
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
});

test("the box keeps the keyboard when Send is pressed from the keyboard", async () => {
  const { write, close } = await conversationWithStream();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "And at night?" } });
  // Tab from the box is Send; Enter on a button is a click on it, which is
  // what jsdom, which has no default actions, is given here.
  const send = screen.getByRole("button", { name: "Send message" });
  send.focus();
  expect(send).toHaveFocus();
  await act(async () => {
    fireEvent.keyDown(send, { key: "Enter" });
    fireEvent.click(send);
    await settle();
  });
  expect(
    screen.getByRole("button", { name: "Stop generating" }),
  ).toBeInTheDocument();
  expect(box).toHaveFocus();
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
});

test("the box takes the keyboard back from Stop when the run ends", async () => {
  const { write, close } = await conversationWithStream();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "And at night?" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await settle();
  const stop = screen.getByRole("button", { name: "Stop generating" });
  stop.focus();
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
  await waitFor(() => {
    expect(
      screen.queryByRole("button", { name: "Stop generating" }),
    ).toBeNull();
  });
  expect(box).toHaveFocus();
});

test("nothing is sent while the conversation is read, and the box keeps its text", async () => {
  let answerRead: (response: Response) => void = () => undefined;
  const reading = new Promise<Response>((done) => {
    answerRead = done;
  });
  const fetch = vi.fn<typeof globalThis.fetch>((input) =>
    String(input) === `/api/conversations/${CONVERSATION}`
      ? reading
      : Promise.reject(new Error(`nothing answers ${String(input)}`)),
  );
  vi.stubGlobal("fetch", fetch);
  draw();
  await settle();
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "Typed early" } });
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  fireEvent.keyDown(box, { key: "Enter" });
  await settle();
  expect(box).toHaveValue("Typed early");
  expect(turnsIn(fetch as ReturnType<typeof stub>)).toEqual([]);

  // Read, and the same text can be sent.
  await act(async () => {
    answerRead(json(opened(conversation(1), TREE)));
    await settle();
  });
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
  });
  expect(box).toHaveValue("Typed early");
});

// ------------------------------------------------------------ a question

test("Edit opens the question's own box, and Escape closes it again", async () => {
  await conversationWithStream();
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  const edit = screen.getByRole("textbox", { name: "Edit message" });
  expect(edit).toHaveValue("Why do robins sing before dawn?");
  expect(edit).toHaveFocus();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Update" })).toBeEnabled();
  // An empty edit is not one to send.
  fireEvent.change(edit, { target: { value: " " } });
  expect(screen.getByRole("button", { name: "Update" })).toBeDisabled();

  fireEvent.keyDown(edit, { key: "Escape" });
  expect(screen.queryByRole("textbox", { name: "Edit message" })).toBeNull();
  // The keyboard goes back to the Edit that opened it.
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Edit" })).toHaveFocus();
  });
});

test("Update sends the edit of that question, and Cancel sends nothing", async () => {
  const { fetch, write, close } = await conversationWithStream();
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await settle();
  expect(turnsIn(fetch)).toEqual([]);
  expect(
    screen.getByText("Why do robins sing before dawn?"),
  ).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Edit message" }), {
    target: { value: "Why do robins sing at night?" },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    await settle();
  });
  expect(turnsIn(fetch)).toEqual([
    { text: "Why do robins sing at night?", edit: "m1", model_id: "sonnet" },
  ]);
  // The thread is cut at the edit, and goes on from the new question.
  expect(screen.queryByText("Because it is quiet then.")).toBeNull();
  expect(screen.getByText("Why do robins sing at night?")).toBeInTheDocument();
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
});

test("Enter in the edit box sends it", async () => {
  const { fetch, write, close } = await conversationWithStream();
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  const edit = screen.getByRole("textbox", { name: "Edit message" });
  fireEvent.change(edit, { target: { value: "Really?" } });
  await act(async () => {
    fireEvent.keyDown(edit, { key: "Enter" });
    await settle();
  });
  expect(turnsIn(fetch)).toEqual([
    { text: "Really?", edit: "m1", model_id: "sonnet" },
  ]);
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
});

// ---------------------------------------------------------------- an answer

test("a run that has said nothing is a dot, and no message offers anything", async () => {
  const { write, close } = await conversationWithStream();
  fireEvent.change(screen.getByRole("textbox", { name: "Message input" }), {
    target: { value: "And at night?" },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settle();
  });
  expect(
    screen.getByRole("img", { name: "Assistant is working" }),
  ).toBeInTheDocument();
  // One turn at a time: nothing is offered while one is going.
  for (const name of ["Copy", "Refresh", "Edit", "Resume"]) {
    expect(screen.queryByRole("button", { name })).toBeNull();
  }

  write(event("TEXT_MESSAGE_START", { messageId: "m4", role: "assistant" }, 2));
  write(event("TEXT_MESSAGE_CONTENT", { messageId: "m4", delta: "Owls." }, 3));
  await waitFor(() => {
    expect(screen.getByText("Owls.")).toBeInTheDocument();
  });
  // The answer is saying something, so the dot has gone.
  expect(
    screen.queryByRole("img", { name: "Assistant is working" }),
  ).toBeNull();
  // And it stands where the answer is: one answer, not two.
  expect(document.querySelectorAll('[data-role="assistant"]')).toHaveLength(2);
  write(event("TEXT_MESSAGE_END", { messageId: "m4" }, 4));
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 5));
  close();
  // Over, and what can be done with the answers is offered again.
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Send message" })).toBeVisible();
  });
  expect(
    screen.getAllByRole("button", { name: "Copy" }).length,
  ).toBeGreaterThan(0);
});

test("a failed answer says so quietly, and the last one can be resumed", async () => {
  const failed = {
    ...message("m2", "assistant", "Half an answer"),
    failed: true,
  };
  const { fetch, write, close } = await conversationWithStream([
    TREE[0]!,
    failed,
  ]);
  const said = screen.getByText(/This answer did not finish/);
  // In the answer, and an alert, so that it is read out: the box arrives
  // already holding its words.
  expect(said.closest('[data-role="assistant"]')).not.toBeNull();
  expect(said.closest('[role="alert"]')).toHaveClass("chat-error");
  // Said once: by the answer, not again under the thread.
  expect(screen.getAllByText(/This answer did not finish/)).toHaveLength(1);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    await settle();
  });
  expect(turnsIn(fetch)).toEqual([{ resume: "m2", model_id: "sonnet" }]);
  // Like a retry, it takes the failed answer off the screen.
  expect(screen.queryByText(/This answer did not finish/)).toBeNull();
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
});

test("Refresh on a failed answer is a retry, and Resume is the last answer's alone", async () => {
  const failed = {
    ...message("m2", "assistant", "Half an answer"),
    failed: true,
  };
  const { fetch, write, close } = await conversationWithStream([
    TREE[0]!,
    failed,
    message("m3", "user", "And?"),
    message("m4", "assistant", "Then this."),
  ]);
  // The failed answer is not the last, so it has no Resume.
  expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
  const answer = screen
    .getByText("Half an answer")
    .closest('[data-role="assistant"]');
  if (!(answer instanceof HTMLElement)) throw new Error("no failed answer");
  expect(answer).toHaveAttribute("data-message-id", "m2");
  expect(answer).toHaveAttribute("data-last", "false");
  const refresh = [...answer.querySelectorAll("button")].find(
    (button) => button.getAttribute("aria-label") === "Refresh",
  );
  await act(async () => {
    refresh?.click();
    await settle();
  });
  expect(turnsIn(fetch)).toEqual([{ retry: "m2", model_id: "sonnet" }]);
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
});

test("Copy copies the answer's words, and says so for two seconds", async () => {
  vi.useFakeTimers();
  const writeText = vi.fn(() => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const answer: ChatMessage = {
    id: "m2",
    role: "assistant",
    parts: [
      { kind: "text", text: "One." },
      { kind: "tool-call", id: "c", name: "echo", argsText: "{}" },
      { kind: "text", text: "Two." },
    ],
    state: "stored",
  };
  const actions = { regenerate: vi.fn(), resume: vi.fn() };
  render(
    <AssistantMessage
      message={answer}
      last
      running={false}
      actions={actions}
    />,
  );
  const copy = screen.getByRole("button", { name: "Copy" });
  expect(copy).toHaveAttribute("title", "Copy");
  expect(copy.querySelector('[data-icon="copy"]')).not.toBeNull();
  await act(async () => {
    fireEvent.click(copy);
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(writeText).toHaveBeenCalledWith("One.\n\nTwo.");
  expect(copy.querySelector('[data-icon="check"]')).not.toBeNull();
  // Still named Copy: what it does has not changed.
  expect(copy).toHaveAccessibleName("Copy");
  act(() => {
    vi.advanceTimersByTime(COPIED_MS);
  });
  expect(copy.querySelector('[data-icon="copy"]')).not.toBeNull();
  Reflect.deleteProperty(navigator, "clipboard");
});

/** An answer of two texts and a call, drawn alone with its actions. */
function drawAnswer() {
  const answer: ChatMessage = {
    id: "m2",
    role: "assistant",
    parts: [
      { kind: "text", text: "# One" },
      { kind: "tool-call", id: "c", name: "echo", argsText: "{}" },
      { kind: "text", text: "Two." },
    ],
    state: "stored",
  };
  const actions = { regenerate: vi.fn(), resume: vi.fn() };
  return render(
    <>
      <AssistantMessage
        message={answer}
        last
        running={false}
        actions={actions}
      />
      <button type="button">Elsewhere</button>
    </>,
  );
}

test("More opens a menu from the keyboard, and Escape gives the keyboard back", () => {
  drawAnswer();
  const more = screen.getByRole("button", { name: "More" });
  expect(more).toHaveAttribute("title", "More");
  expect(more).toHaveAttribute("aria-haspopup", "menu");
  expect(more).toHaveAttribute("aria-expanded", "false");
  expect(more.querySelector('[data-icon="ellipsis"]')).not.toBeNull();
  // After Refresh, as the bar always had it.
  const names = [
    ...more.closest(".chat-answer-actions")!.querySelectorAll("button"),
  ].map((button) => button.getAttribute("aria-label"));
  expect(names).toEqual(["Copy", "Refresh", "More"]);
  expect(screen.queryByRole("menu")).toBeNull();

  more.focus();
  fireEvent.click(more);
  const menu = screen.getByRole("menu");
  expect(more).toHaveAttribute("aria-expanded", "true");
  expect(more).toHaveAttribute("aria-controls", menu.id);
  const item = screen.getByRole("menuitem", { name: "Export as Markdown" });
  expect(menu).toContainElement(item);
  expect(item).toHaveFocus();

  fireEvent.keyDown(item, { key: "Escape" });
  expect(screen.queryByRole("menu")).toBeNull();
  expect(more).toHaveAttribute("aria-expanded", "false");
  expect(more).toHaveFocus();
});

test("More's menu shuts on a press outside it, and on a Tab out of it", () => {
  drawAnswer();
  const more = screen.getByRole("button", { name: "More" });
  fireEvent.click(more);
  expect(screen.getByRole("menu")).toBeInTheDocument();
  // A press on the menu itself leaves it open.
  fireEvent.pointerDown(screen.getByRole("menu"));
  expect(screen.getByRole("menu")).toBeInTheDocument();
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("menu")).toBeNull();

  fireEvent.click(more);
  const item = screen.getByRole("menuitem", { name: "Export as Markdown" });
  const elsewhere = screen.getByRole("button", { name: "Elsewhere" });
  fireEvent.blur(item, { relatedTarget: elsewhere });
  expect(screen.queryByRole("menu")).toBeNull();

  // And More's own press shuts what it opened.
  fireEvent.click(more);
  fireEvent.pointerDown(more);
  fireEvent.click(more);
  expect(screen.queryByRole("menu")).toBeNull();
});

test("Export as Markdown downloads the answer's words as a .md file", async () => {
  vi.useFakeTimers();
  const made: Blob[] = [];
  const createObjectURL = vi.fn((file: Blob) => {
    made.push(file);
    return "blob:answer";
  });
  const revokeObjectURL = vi.fn();
  const had = {
    create: Object.getOwnPropertyDescriptor(URL, "createObjectURL"),
    revoke: Object.getOwnPropertyDescriptor(URL, "revokeObjectURL"),
  };
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: createObjectURL,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: revokeObjectURL,
  });
  const clicked: HTMLAnchorElement[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push(this);
  });
  try {
    drawAnswer();
    const more = screen.getByRole("button", { name: "More" });
    fireEvent.click(more);
    fireEvent.click(
      screen.getByRole("menuitem", { name: "Export as Markdown" }),
    );

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const file = made[0]!;
    expect(file).toBeInstanceOf(Blob);
    expect(file.type).toBe("text/markdown");
    expect(clicked).toHaveLength(1);
    const link = clicked[0]!;
    expect(link.download).toMatch(/^message-\d+\.md$/);
    expect(link.rel).toBe("noopener");
    expect(link.getAttribute("href")).toBe("blob:answer");
    // Clicked and gone, and the URL let go of once the click had it.
    expect(link.isConnected).toBe(false);
    act(() => {
      vi.runAllTimers();
    });
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:answer");
    // The menu shuts, and the keyboard is back on More.
    expect(screen.queryByRole("menu")).toBeNull();
    expect(more).toHaveFocus();
    vi.useRealTimers();
    expect(await file.text()).toBe("# One\n\nTwo.");
  } finally {
    for (const [name, was] of [
      ["createObjectURL", had.create],
      ["revokeObjectURL", had.revoke],
    ] as const) {
      if (was === undefined) Reflect.deleteProperty(URL, name);
      else Object.defineProperty(URL, name, was);
    }
  }
});

test("there is nothing to copy in an answer with no words, or one still arriving", () => {
  const actions = { regenerate: vi.fn(), resume: vi.fn() };
  const calls: ChatMessage = {
    id: "m2",
    role: "assistant",
    parts: [{ kind: "tool-call", id: "c", name: "echo", argsText: "{}" }],
    state: "stored",
  };
  const drawn = render(
    <AssistantMessage message={calls} last running={false} actions={actions} />,
  );
  expect(screen.getByRole("button", { name: "Copy" })).toBeDisabled();
  drawn.rerender(
    <AssistantMessage
      message={{ ...calls, parts: [{ kind: "text", text: "half" }] }}
      last
      running={false}
      actions={actions}
    />,
  );
  expect(screen.getByRole("button", { name: "Copy" })).toBeEnabled();
  drawn.rerender(
    <AssistantMessage
      message={{
        ...calls,
        parts: [{ kind: "text", text: "half" }],
        state: "running",
      }}
      last
      running={false}
      actions={actions}
    />,
  );
  expect(screen.getByRole("button", { name: "Copy" })).toBeDisabled();
});

// ------------------------------------------------------------ disclosures

test("a disclosure is a button that says what it opens, and the panel goes when shut", async () => {
  vi.useFakeTimers();
  const answer: ChatMessage = {
    id: "m2",
    role: "assistant",
    parts: [
      {
        kind: "tool-call",
        id: "c1",
        name: "echo",
        argsText: '{"a":1}',
        result: "1",
      },
      {
        kind: "tool-call",
        id: "c2",
        name: "shout",
        argsText: "{}",
        result: "!",
      },
    ],
    state: "stored",
  };
  const actions = { regenerate: vi.fn(), resume: vi.fn() };
  render(
    <AssistantMessage
      message={answer}
      last
      running={false}
      actions={actions}
    />,
  );
  const group = screen.getByRole("button", { name: "2 tool calls" });
  expect(group).toHaveAttribute("aria-expanded", "false");
  expect(group).not.toHaveAttribute("aria-controls");
  expect(screen.queryByRole("button", { name: /echo/ })).toBeNull();

  fireEvent.click(group);
  expect(group).toHaveAttribute("aria-expanded", "true");
  const panel = document.getElementById(group.getAttribute("aria-controls")!);
  expect(panel).not.toBeNull();
  expect(panel).toHaveAttribute("data-state", "open");
  expect(panel).toContainElement(
    screen.getByRole("button", { name: "Used tool: echo" }),
  );
  expect(screen.getByRole("button", { name: "Used tool: shout" })).toBe(
    panel?.querySelectorAll("button")[1],
  );

  // Shut: closing, and inert while it does, and then not in the page.
  fireEvent.click(group);
  expect(group).toHaveAttribute("aria-expanded", "false");
  expect(panel).toHaveAttribute("data-state", "closed");
  expect(panel).toHaveAttribute("inert");
  act(() => {
    vi.advanceTimersByTime(DISCLOSURE_MS);
  });
  expect(panel).not.toBeInTheDocument();
  expect(group).not.toHaveAttribute("aria-controls");
});

test("a call shows how it went: running, cancelled, or failed with the answer", () => {
  const actions = { regenerate: vi.fn(), resume: vi.fn() };
  const call = {
    kind: "tool-call" as const,
    id: "c",
    name: "echo",
    argsText: "{}",
  };
  const at = (state: ChatMessage["state"], detail?: string) => {
    const drawn = render(
      <AssistantMessage
        message={{
          id: "m2",
          role: "assistant",
          parts: [call],
          state,
          ...(detail === undefined ? {} : { detail }),
        }}
        last
        running={false}
        actions={actions}
      />,
    );
    fireEvent.click(drawn.getByRole("button", { name: "1 tool call" }));
    const trigger = drawn.getByRole("button", { name: /echo/ });
    return { drawn, trigger };
  };

  const running = at("running");
  expect(running.trigger.querySelector('[data-icon="loader"]')).toHaveClass(
    "chat-spin",
  );
  // The group spins too while one of its calls is running.
  expect(
    running.drawn
      .getByRole("button", { name: "1 tool call" })
      .querySelector('[data-icon="loader"]'),
  ).not.toBeNull();
  running.drawn.unmount();

  const cancelled = at("cancelled");
  expect(cancelled.trigger).toHaveAccessibleName("Cancelled tool: echo");
  expect(
    cancelled.trigger.querySelector('[data-icon="circle-x"]'),
  ).not.toBeNull();
  cancelled.drawn.unmount();

  const failed = at("failed", "This answer did not finish.");
  expect(failed.trigger).toHaveAccessibleName("Used tool: echo");
  expect(failed.trigger.querySelector('[data-icon="circle-x"]')).not.toBeNull();
  fireEvent.click(failed.trigger);
  const error = failed.drawn.container.querySelector(
    '[data-slot="tool-fallback-error"]',
  );
  expect(error).toHaveTextContent("Error:This answer did not finish.");
  failed.drawn.unmount();

  const done = at("stored");
  expect(done.trigger.querySelector('[data-icon="check"]')).not.toBeNull();
});

test("thinking is open while it arrives, closes when the answer moves on, and then does as it is told", async () => {
  const { write, close } = await conversationWithStream();
  fireEvent.change(screen.getByRole("textbox", { name: "Message input" }), {
    target: { value: "Why?" },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settle();
  });
  write(event("TEXT_MESSAGE_START", { messageId: "m4", role: "assistant" }, 2));
  write(event("REASONING_MESSAGE_START", { messageId: "m4:reasoning:3" }, 3));
  write(
    event(
      "REASONING_MESSAGE_CONTENT",
      { messageId: "m4:reasoning:3", delta: "Robins are quiet birds." },
      4,
    ),
  );
  const trigger = await screen.findByRole("button", { name: "Reasoning" });
  await waitFor(() => {
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });
  expect(screen.getByText("Robins are quiet birds.")).toBeInTheDocument();
  // The person shuts it while it streams: it stays shut.
  fireEvent.click(trigger);
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  write(
    event(
      "REASONING_MESSAGE_CONTENT",
      { messageId: "m4:reasoning:3", delta: " Mostly." },
      5,
    ),
  );
  await settle();
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  write(event("TEXT_MESSAGE_END", { messageId: "m4" }, 6));
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 7));
  close();
});

// --------------------------------------------------------------- the thread

test("a conversation being read is an outline, said politely", async () => {
  let arrived: (given: Response) => void = () => undefined;
  stub(() => undefined);
  vi.mocked(fetch).mockImplementationOnce(
    () => new Promise<Response>((done) => (arrived = done)),
  );
  draw();
  await settle();
  expect(screen.getByRole("status")).toHaveTextContent("Loading conversation");
  // Not the empty chat: no welcome, and the box is there to write in.
  expect(
    screen.getByRole("textbox", { name: "Message input" }),
  ).toBeInTheDocument();
  await act(async () => {
    arrived(json(opened(conversation(1), TREE)));
    await settle();
  });
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByText("Because it is quiet then.")).toBeInTheDocument();
});

test("the thread is a region of its own, which the keyboard can scroll", async () => {
  await conversationWithStream();
  const region = screen.getByRole("region", { name: "Chat" });
  expect(region).toHaveAttribute("tabindex", "0");
  expect(region).toContainElement(
    screen.getByRole("textbox", { name: "Message input" }),
  );
});

test("a question that is not the last keeps its Edit in reach of the keyboard", async () => {
  // Shown on a pointer or on focus (`thread.css`), and always in the page:
  // a control the keyboard cannot reach is no control at all.
  await conversationWithStream([
    ...TREE,
    message("m3", "user", "And?"),
    message("m4", "assistant", "Then this."),
  ]);
  const edits = screen.getAllByRole("button", { name: "Edit" });
  expect(edits).toHaveLength(2);
  edits[0]!.focus();
  expect(edits[0]).toHaveFocus();
  expect(edits[0]!.closest('[data-role="user"]')).toHaveAttribute(
    "data-last",
    "false",
  );
});

// ------------------------------------------------------------ the viewport

/** A viewport whose sizes a test sets, with its observer in hand. */
function viewportHarness(follow = true) {
  let grow: () => void = () => undefined;
  class Observer {
    constructor(callback: () => void) {
      grow = callback;
    }
    observe(): void {}
    disconnect(): void {}
  }
  vi.stubGlobal("ResizeObserver", Observer);
  const size = { scrollHeight: 400, clientHeight: 400, scrollTop: 0 };
  const scrollTo = vi.fn((options: ScrollToOptions) => {
    size.scrollTop = (options.top ?? 0) - size.clientHeight;
  });
  let toBottom: (smooth?: boolean) => void = () => undefined;
  function Harness() {
    const scroll = useStickToBottom(follow);
    toBottom = scroll.toBottom;
    return (
      <div
        ref={(element) => {
          scroll.viewport.current = element;
          if (element === null) return;
          Object.defineProperties(element, {
            scrollHeight: { configurable: true, get: () => size.scrollHeight },
            clientHeight: { configurable: true, get: () => size.clientHeight },
            scrollTop: {
              configurable: true,
              get: () => size.scrollTop,
              set: (top: number) => {
                size.scrollTop = Math.min(
                  top,
                  size.scrollHeight - size.clientHeight,
                );
              },
            },
          });
          element.scrollTo = scrollTo as typeof element.scrollTo;
        }}
        data-testid="viewport"
      >
        <div ref={scroll.content} />
        <button type="button" disabled={scroll.atBottom}>
          down
        </button>
      </div>
    );
  }
  render(<Harness />);
  return {
    size,
    scrollTo,
    viewport: screen.getByTestId("viewport"),
    button: screen.getByRole("button", { name: "down" }),
    grow: () => {
      act(() => {
        grow();
      });
    },
    toBottom: (smooth?: boolean) => {
      act(() => {
        toBottom(smooth);
      });
    },
  };
}

test("the viewport keeps to the bottom while an answer arrives", () => {
  const view = viewportHarness();
  expect(view.button).toBeDisabled();
  // The answer grows: the viewport follows it down.
  view.size.scrollHeight = 1000;
  view.grow();
  expect(view.size.scrollTop).toBe(600);
  expect(view.button).toBeDisabled();
  view.size.scrollHeight = 1400;
  view.grow();
  expect(view.size.scrollTop).toBe(1000);
});

test("scrolling up lets go, and the button or the bottom takes it back", () => {
  const view = viewportHarness();
  view.size.scrollHeight = 1000;
  view.grow();
  // The person scrolls up: it stays where they put it as the answer grows.
  view.size.scrollTop = 300;
  fireEvent.scroll(view.viewport);
  expect(view.button).toBeEnabled();
  view.size.scrollHeight = 1600;
  view.grow();
  expect(view.size.scrollTop).toBe(300);

  // The button goes down, smoothly, and the viewport is held there again.
  view.toBottom(true);
  expect(view.scrollTo).toHaveBeenCalledWith({
    top: 1600,
    behavior: "smooth",
  });
  view.size.scrollHeight = 2000;
  view.grow();
  expect(view.size.scrollTop).toBe(1600);

  // Scrolling back to the bottom by hand holds it too.
  view.size.scrollTop = 100;
  fireEvent.scroll(view.viewport);
  view.size.scrollTop = 1600;
  fireEvent.scroll(view.viewport);
  expect(view.button).toBeDisabled();
  view.size.scrollHeight = 2400;
  view.grow();
  expect(view.size.scrollTop).toBe(2000);
});

test("a thread standing still does not follow what grows in it", () => {
  // A disclosure opened with no answer arriving grows the page under the
  // pointer; pulling the page up past it would move what was just clicked.
  const view = viewportHarness(false);
  view.size.scrollHeight = 1000;
  view.grow();
  expect(view.size.scrollTop).toBe(0);
  expect(view.button).toBeEnabled();
  // Going down is still there to be asked for.
  view.toBottom();
  expect(view.scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: "auto" });
});

test("going to the bottom is instant for somebody who asked for less motion", () => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: true }) as unknown as MediaQueryList),
  );
  const view = viewportHarness();
  view.size.scrollHeight = 1000;
  view.toBottom(true);
  expect(view.scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: "auto" });
});
