// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useEffect, type ReactNode } from "react";
import { expect, expectTypeOf, test, vi } from "vitest";

import { json, refusal, type Call } from "../../test/api";
import type { Conversation } from "../../conversation/conversation";
import {
  calling,
  conversation,
  id,
  message,
  opened,
  results,
} from "../../test/conversations";
import { event, streamed, streamHeaders, writable } from "../../test/stream";
import { TOOL_FAILED } from "./ToolCall";
import {
  Chat,
  type AgentId,
  type ChatProps,
  type ConversationId,
  type ModelId,
} from "../index";

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

function draw(props: Partial<ChatProps> = {}) {
  const started = vi.fn();
  const drawn = render(
    <Chat
      conversationId={null}
      agentId="helper"
      modelId="sonnet"
      onConversationStarted={started}
      {...props}
    />,
  );
  return { ...drawn, started };
}

const settle = () =>
  act(async () => new Promise((done) => setTimeout(done, 0)));

/** Write into the box and press the send button the vendored composer draws. */
async function send(text: string) {
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: text } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settle();
  });
}

test("a tool call is drawn as data: the name, the arguments and the result as text", async () => {
  // The name is the model's, the arguments are the model's and the result
  // is the tool's: attacker-influenced text, every one of them
  // (`docs/specs/wire.md`). None is markup and none becomes a link.
  const markup = "<b>no</b> such [repository](https://example.test/)";
  stub(() =>
    json(
      opened(conversation(1), [
        message("m1", "user", "Any robins on GitHub?"),
        calling("m2", "Let me look.", [
          {
            call_id: "toolu_01",
            name: "github__search_<i>x</i>",
            arguments: { q: "<script>alert(1)</script>" },
          },
        ]),
        results("t1", [{ call_id: "toolu_01", text: markup, is_error: true }]),
        message("m3", "assistant", "None."),
      ]),
    ),
  );
  draw({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(screen.getByText("None.")).toBeInTheDocument();
  });
  // The vendored Thread folds the calls of an answer behind one trigger, and
  // each call behind its own, which names the tool as text.
  fireEvent.click(screen.getByRole("button", { name: "1 tool call" }));
  const trigger = await screen.findByRole("button", {
    name: /github__search_<i>x<\/i>/,
  });
  expect(trigger.querySelector("i")).toBeNull();
  fireEvent.click(trigger);
  await waitFor(() => {
    expect(screen.getByText(markup)).toBeInTheDocument();
  });
  expect(
    screen.getByText('{"q":"<script>alert(1)</script>"}'),
  ).toBeInTheDocument();
  // The tool said it failed, and the chat says so rather than drawing a
  // check mark over the answer.
  expect(screen.getByText(TOOL_FAILED)).toBeInTheDocument();
  // The one <b> is the trigger's own, around the name; the result's is text.
  expect(
    [...document.querySelectorAll("b")].map((bold) => bold.textContent),
  ).toEqual(["github__search_<i>x</i>"]);
  expect(document.querySelector("script")).toBeNull();
  expect(document.querySelector('a[href="https://example.test/"]')).toBeNull();
});

test("a conversation that is loaded is drawn as a thread", async () => {
  stub(() => json(opened(conversation(1), TREE)));
  draw({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(
      screen.getByText("Why do robins sing before dawn?"),
    ).toBeInTheDocument();
  });
  expect(screen.getByText("Because it is quiet then.")).toBeInTheDocument();
  // The box is there, which is the whole of what the stand-in could not do.
  expect(screen.getByRole("textbox", { name: "Message input" })).toBeVisible();
});

test("the empty chat shows what it is given above the box", async () => {
  stub(() => json({ items: [] }));
  draw({ welcome: <p>Pick an agent</p> });
  await settle();
  expect(screen.getByText("Pick an agent")).toBeVisible();
});

test("a conversation with nothing in it is not the empty chat", async () => {
  stub((call) =>
    call.url === `/api/conversations/${CONVERSATION}`
      ? json(opened(conversation(1), []))
      : undefined,
  );
  draw({ conversationId: CONVERSATION, welcome: <p>Pick an agent</p> });
  await settle();
  expect(screen.getByRole("textbox", { name: "Message input" })).toBeVisible();
  expect(screen.queryByText("Pick an agent")).toBeNull();
});

test("sending a first message posts a turn and reports the conversation", async () => {
  const fetch = stub((call) => {
    if (call.url === "/api/turns") {
      return streamed(
        [
          event(
            "TEXT_MESSAGE_START",
            { messageId: "m2", role: "assistant" },
            2,
          ),
          event(
            "TEXT_MESSAGE_CONTENT",
            { messageId: "m2", delta: "Because it is quiet then." },
            3,
          ),
          event("TEXT_MESSAGE_END", { messageId: "m2" }, 4),
          event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 5),
        ],
        { headers: streamHeaders(RUN, CONVERSATION) },
      );
    }
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  const { started } = draw();
  await settle();
  await send("Why do robins sing before dawn?");

  expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
    agent_id: "helper",
    model_id: "sonnet",
    text: "Why do robins sing before dawn?",
  });
  expect(started).toHaveBeenCalledWith(CONVERSATION);
  await waitFor(() => {
    expect(screen.getByText("Because it is quiet then.")).toBeInTheDocument();
  });
});

test("an answer arriving is on the screen before the run has ended", async () => {
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  stub((call) => {
    if (call.url === "/api/turns") return response;
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  draw();
  await settle();
  await send("Why?");

  write(event("TEXT_MESSAGE_START", { messageId: "m2", role: "assistant" }, 2));
  write(event("TEXT_MESSAGE_CONTENT", { messageId: "m2", delta: "Half" }, 3));
  await settle();
  // The copied Markdown component eases text in a few characters at a time,
  // so what is waited for is that it arrives rather than that it is instant.
  await waitFor(() => {
    expect(screen.getByText("Half")).toBeInTheDocument();
  });
  // A run in flight: the box offers stopping rather than sending.
  expect(
    screen.getByRole("button", { name: "Stop generating" }),
  ).toBeInTheDocument();

  write(
    event("TEXT_MESSAGE_CONTENT", { messageId: "m2", delta: " an answer" }, 4),
  );
  await settle();
  await waitFor(() => {
    expect(screen.getByText("Half an answer")).toBeInTheDocument();
  });
  write(event("TEXT_MESSAGE_END", { messageId: "m2" }, 5));
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 6));
  close();
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Send message" })).toBeVisible();
  });
});

test("thinking is shown while it arrives, and it is collapsed", async () => {
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  stub((call) => {
    if (call.url === "/api/turns") return response;
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  draw();
  await settle();
  await send("Why?");
  write(event("TEXT_MESSAGE_START", { messageId: "m2", role: "assistant" }, 2));
  write(event("REASONING_MESSAGE_START", { messageId: "m2:reasoning:3" }, 3));
  write(
    event(
      "REASONING_MESSAGE_CONTENT",
      { messageId: "m2:reasoning:3", delta: "Robins are quiet birds." },
      4,
    ),
  );
  write(event("REASONING_MESSAGE_END", { messageId: "m2:reasoning:3" }));
  write(event("TEXT_MESSAGE_CONTENT", { messageId: "m2", delta: "Quiet." }, 5));
  await settle();

  // The disclosure the copied reasoning components draw, shut. **Thinking is
  // kept apart from the answer** (`docs/specs/conversations.md`,
  // "Reasoning"): every watcher sees it arrive and no message holds any of
  // it, so it is something to open rather than something to read.
  const trigger = await screen.findByRole("button", { name: /Reasoning/ });
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  await act(async () => {
    fireEvent.click(trigger);
    await settle();
  });
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  await waitFor(() => {
    expect(screen.getByText("Robins are quiet birds.")).toBeInTheDocument();
  });

  // **Reasoning is shown and not stored** (`docs/specs/conversations.md`), so
  // the reread that ends the turn leaves nothing of it behind.
  write(event("TEXT_MESSAGE_END", { messageId: "m2" }, 6));
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 7));
  close();
  await waitFor(() => {
    expect(screen.queryByRole("button", { name: /Reasoning/ })).toBeNull();
  });
});

test("the box does not offer stopping before there is a run to stop", async () => {
  // assistant-ui's own stop takes a trailing question out of the thread and
  // puts its text back into the box. Between the send and the response's
  // headers there is no run, so the composer must still be a composer.
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  let arrived: (given: Response) => void = () => undefined;
  const fetch = stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  draw({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(screen.getByText("Because it is quiet then.")).toBeInTheDocument();
  });
  fetch.mockImplementationOnce(
    () => new Promise<Response>((done) => (arrived = done)),
  );
  await send("And at night?");

  expect(screen.getByText("And at night?")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Stop generating" })).toBeNull();
  expect(screen.getByRole("button", { name: "Send message" })).toBeVisible();
  expect(screen.getByRole("textbox", { name: "Message input" })).toHaveValue(
    "",
  );

  // The headers arrive, and only then is there something to stop.
  await act(async () => {
    arrived(response);
    await settle();
  });
  expect(
    screen.getByRole("button", { name: "Stop generating" }),
  ).toBeInTheDocument();
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Send message" })).toBeVisible();
  });
});

test("the action bar of a finished answer: copy, regenerate and edit", async () => {
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  draw({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(screen.getByText("Because it is quiet then.")).toBeInTheDocument();
  });
  expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  // A question's own action bar is the edit button, and it shows on the
  // message rather than beside every one of them.
  const asked = document.querySelector('[data-role="user"]');
  if (asked === null) throw new Error("no question on the screen");
  await act(async () => {
    fireEvent.mouseEnter(asked);
    await settle();
  });
  expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
});

test("the branch picker never renders: there is one thread", async () => {
  // The vendored Thread still carries a BranchPicker, hidden when a message
  // has one branch -- and every message has, because what is handed over is
  // a chain (ADR 0003). Nothing an edit put aside is here to be a branch.
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  draw({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(screen.getByText("Because it is quiet then.")).toBeInTheDocument();
  });
  expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Previous" })).toBeNull();
  expect(document.querySelector(".aui-branch-picker-state")).toBeNull();
});

test("a message that was not sent says so, where it can be read", async () => {
  // The box empties itself when it hands a message over, so a turn refused
  // for being a second one has to say what happened to it -- and stay said:
  // the run that is going starts and ends without wiping it.
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  const fetch = stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(opened(conversation(1), TREE));
    }
    return undefined;
  });
  draw({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(screen.getByText("Because it is quiet then.")).toBeInTheDocument();
  });
  // Held, so the second send lands while the first is still going out.
  let arrived: (given: Response) => void = () => undefined;
  fetch.mockImplementationOnce(
    () => new Promise<Response>((done) => (arrived = done)),
  );
  await send("And at night?");
  await send("Impatient.");

  const notice = screen.getByRole("alert");
  expect(notice).toHaveTextContent(
    /it takes one turn at a time\. That message was not sent\./,
  );
  // And the message is back where it was typed.
  expect(screen.getByRole("textbox", { name: "Message input" })).toHaveValue(
    "Impatient.",
  );

  await act(async () => {
    arrived(response);
    await settle();
  });
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 2));
  close();
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Send message" })).toBeVisible();
  });
  // Still there: a run that came and went is not what clears it.
  expect(screen.getByRole("alert")).toBe(notice);
});

test("a run that ended badly says so, and says it once", async () => {
  stub((call) => {
    if (call.url === `/api/conversations/${CONVERSATION}`) {
      return json(
        opened(conversation(1), TREE, {
          ended_badly: {
            run_id: RUN,
            state: "interrupted",
            ended_at: "2026-09-22T10:00:00Z",
          },
        }),
      );
    }
    return undefined;
  });
  const { container } = draw({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(
      screen.getByText(/interrupted when the server stopped/),
    ).toBeInTheDocument();
  });
  expect(container.querySelectorAll("[data-ended]")).toHaveLength(1);
});

test("a conversation that is not here has no thread to draw", async () => {
  stub(() => refusal(404, "NotFoundError", "there is nothing here of that id"));
  draw({ conversationId: CONVERSATION });
  await waitFor(() => {
    expect(screen.getByRole("alert")).toHaveTextContent(
      /This conversation is not here/,
    );
  });
  expect(screen.queryByRole("textbox", { name: "Message input" })).toBeNull();
});

test("the seam is a component and eight names, and nothing of a library", () => {
  // **A claim about the types, checked by the type checker.** `tsc -b` reads
  // this file, so the four assertions below fail the build rather than a
  // run: the seam is exactly these eight names, they are exactly these types,
  // and none of them comes from assistant-ui (ADR 0001, the discard test).
  expectTypeOf<ChatProps>().toEqualTypeOf<{
    conversationId: ConversationId | null;
    agentId: AgentId | null;
    modelId: ModelId | null;
    onConversationStarted: (id: ConversationId) => void;
    onConversationOpened?: (conversation: Conversation) => void;
    onTurnEnded?: () => void;
    onModelRefused?: (modelId: ModelId) => void;
    welcome?: ReactNode;
  }>();
  // A ninth name does not belong to it, whatever it is called.
  const extra = {
    conversationId: null,
    agentId: null,
    modelId: null,
    onConversationStarted: () => undefined,
    // @ts-expect-error -- nothing of a chat library crosses this seam, and
    // nothing else is added to it without being written down above.
    runtime: "assistant-ui",
  } satisfies ChatProps;
  // And `ConversationId`, `AgentId` and `ModelId` are what the rest of the
  // application already calls these, which is a string and not a library's
  // handle.
  expectTypeOf<ConversationId>().toEqualTypeOf<string>();
  expectTypeOf<AgentId>().toEqualTypeOf<string>();
  expectTypeOf<ModelId>().toEqualTypeOf<string>();
  expect(typeof Chat).toBe("function");
  expect(extra.conversationId).toBeNull();
});

test("the welcome is not remounted by what is typed beside it", async () => {
  stub(() => json({ items: [] }));
  let mounts = 0;
  function Picker() {
    useEffect(() => {
      mounts += 1;
    }, []);
    return <span>picker</span>;
  }
  draw({ welcome: <Picker /> });
  await settle();
  expect(mounts).toBe(1);
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "typing" } });
  await settle();
  // Re-rendered, perhaps; remounted, no -- a `<select>` that is remounted
  // loses the focus, and the box beside it re-renders on every keystroke.
  expect(mounts).toBe(1);
});
