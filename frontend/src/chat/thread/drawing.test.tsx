// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * A message is drawn again only when it has changed.
 *
 * A delta rebuilds the state, and a conversation is as long as it is: without
 * this, a hundred deltas into a turn of a hundred-message conversation is ten
 * thousand renderings of Markdown that has not changed since it was read. The
 * reducer builds a new object only for a message it touches
 * (`../state.ts`), and the thread draws each message by that identity. The
 * Markdown renderer is replaced here by one that counts, which is the whole
 * reason this is a file of its own.
 */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { json } from "../../test/api";
import { conversation, id, message, opened } from "../../test/conversations";
import { event, streamHeaders, writable } from "../../test/stream";
import { Chat } from "../index";

const drawn = vi.hoisted(() => new Map<string, number>());

vi.mock("../markdown/Markdown", () => ({
  Markdown: ({ text }: { text: string }) => {
    drawn.set(text, (drawn.get(text) ?? 0) + 1);
    return <p>{text}</p>;
  },
}));

const RUN = "11111111-2222-4333-8444-555555555555";
const CONVERSATION = id(1);

const settle = () =>
  act(async () => new Promise((done) => setTimeout(done, 0)));

test("each message is drawn once, however many deltas arrive", async () => {
  const { response, write, close } = writable({
    headers: streamHeaders(RUN, CONVERSATION),
  });
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) =>
      Promise.resolve(
        String(input) === `/api/conversations/${CONVERSATION}`
          ? json(
              opened(conversation(1), [
                message("m1", "user", "Why?"),
                message("m2", "assistant", "Because."),
                message("m3", "user", "And?"),
                message("m4", "assistant", "Then."),
              ]),
            )
          : response,
      ),
    ),
  );
  render(
    <Chat
      conversationId={CONVERSATION}
      agentId="helper"
      modelId="sonnet"
      onConversationStarted={vi.fn()}
    />,
  );
  await waitFor(() => {
    expect(screen.getByText("Then.")).toBeInTheDocument();
  });
  const before = new Map(drawn);

  // Typing in the box draws no message.
  const box = screen.getByRole("textbox", { name: "Message input" });
  for (const typed of ["A", "An", "And", "And then?"]) {
    fireEvent.change(box, { target: { value: typed } });
  }
  expect(drawn.get("Because.")).toBe(before.get("Because."));

  // Neither does an answer arriving: only that answer is drawn again.
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settle();
  });
  write(event("TEXT_MESSAGE_START", { messageId: "m6", role: "assistant" }, 2));
  write(event("TEXT_MESSAGE_CONTENT", { messageId: "m6", delta: "One" }, 3));
  await waitFor(() => {
    expect(screen.getByText("One")).toBeInTheDocument();
  });
  // The run has begun, which hides every action bar: that much is drawn
  // again, once. From here on, only the answer that is arriving is.
  const begun = new Map(drawn);
  for (const [at, delta] of [" two", " three", " four"].entries()) {
    write(event("TEXT_MESSAGE_CONTENT", { messageId: "m6", delta }, at + 4));
    await settle();
  }
  await waitFor(() => {
    expect(screen.getByText("One two three four")).toBeInTheDocument();
  });
  for (const text of ["Why?", "Because.", "And?", "Then."]) {
    expect(drawn.get(text) ?? 0).toBe(begun.get(text) ?? 0);
  }
  expect(drawn.get("One two three four")).toBe(1);
  write(event("RUN_FINISHED", { threadId: CONVERSATION, runId: RUN }, 9));
  close();
});
