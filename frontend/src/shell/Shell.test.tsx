// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";

import type { Session } from "../session/session";
import { callsTo, json, refusal, stubFetch, type Call } from "../test/api";
import { conversation, id, message, opened, page } from "../test/conversations";
import { event, streamHeaders, streamed } from "../test/stream";
import { NOT_OFFERED, unoffered } from "./ModelPicker";
import { PANEL_KEY, Shell } from "./Shell";

const RUN = "11111111-2222-4333-8444-555555555555";

const SESSION: Session = {
  sign_in: true,
  local_development: false,
  providers: [{ id: "google", title: "Google" }],
  user: { id: "u-1", provider: "google", name: "Ada", email: "ada@x.test" },
};

/**
 * One agent, two models and no conversations. A test that is about the history or a
 * conversation answers those calls itself and falls through to these for
 * the rest.
 */
function answering(
  answer: (call: Call) => Response | undefined = () => undefined,
) {
  return stubFetch((call) => answer(call) ?? otherwise(call));
}

function otherwise(call: Call): Response {
  if (call.url.startsWith("/api/agents")) {
    return json({
      items: [
        { id: "helper", title: "Helper", engine: "langgraph", model: "sonnet" },
      ],
    });
  }
  if (call.url.startsWith("/api/models")) {
    return json({
      items: [
        { id: "sonnet", title: "Sonnet" },
        { id: "opus", title: "Opus" },
      ],
    });
  }
  if (call.url.startsWith("/api/apps")) {
    return json({ apps: [] });
  }
  return json(page([]));
}

/** Render, and let the agents and the history arrive. */
async function shell(
  signOut = () => Promise.resolve(),
  answer?: (call: Call) => Response | undefined,
  session: Session = SESSION,
) {
  const fetch = answering(answer);
  const drawn = render(<Shell session={session} signOut={signOut} />);
  await waitFor(() => {
    expect(screen.queryByText("Loading the conversations…")).toBeNull();
  });
  await act(async () => {
    await Promise.resolve();
  });
  return { ...drawn, fetch };
}

const panel = () => screen.getByRole("complementary", { name: "Panel" });

/**
 * Ask for the phone layout, where the panel is a drawer.
 *
 * jsdom's window is 1024 wide and its `matchMedia` answers accordingly, so
 * without this every test is the wide one and the drawer is never the drawer.
 * `src/test/setup.ts` takes the stub back after each test.
 */
function narrow() {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

test("the panel holds the brand, a new chat, the history and the profile", async () => {
  await shell();
  expect(screen.getByText("Robinauts")).toBeVisible();
  expect(screen.getByRole("button", { name: "New chat" })).toBeVisible();
  expect(screen.getByRole("navigation", { name: "History" })).toBeVisible();
  expect(
    screen.getByRole("list", { name: "Conversations" }),
  ).toBeEmptyDOMElement();
  expect(screen.getByText("Ada")).toBeVisible();
  expect(screen.getByText("ada@x.test")).toBeVisible();
  // Projects are out of the POC; nothing pretends otherwise.
  expect(screen.queryByRole("navigation", { name: "Projects" })).toBeNull();
});

test("collapsing the panel is remembered by this browser", async () => {
  await shell();
  fireEvent.click(screen.getByRole("button", { name: "Collapse the panel" }));
  expect(panel()).toHaveAttribute("data-collapsed", "true");
  expect(localStorage.getItem(`robinauts.${PANEL_KEY}`)).toBe("rail");
  fireEvent.click(screen.getByRole("button", { name: "Expand the panel" }));
  expect(panel()).toHaveAttribute("data-collapsed", "false");
  expect(localStorage.getItem(`robinauts.${PANEL_KEY}`)).toBe("open");
});

test("a browser that left it as a rail opens it as one", async () => {
  localStorage.setItem(`robinauts.${PANEL_KEY}`, "rail");
  await shell();
  expect(panel()).toHaveAttribute("data-collapsed", "true");
  expect(
    screen.getByRole("button", { name: "Expand the panel" }),
  ).toBeVisible();
});

test("the drawer opens from the header and closes with Escape", async () => {
  narrow();
  const { container } = await shell();
  const open = screen.getByRole("button", { name: "Open the panel" });
  // Held on to: open, the panel stops being a complementary landmark and
  // becomes the dialog it then is.
  const aside = panel();
  expect(aside).toHaveAttribute("data-drawer", "closed");
  fireEvent.click(open);
  expect(aside).toHaveAttribute("data-drawer", "open");
  // The focus goes into the drawer rather than staying behind it.
  expect(screen.getByRole("button", { name: "Close the panel" })).toHaveFocus();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(aside).toHaveAttribute("data-drawer", "closed");
  expect(container.querySelector("[data-backdrop]")).toBeNull();
  // Escape is a close like any other: the focus goes back to the button
  // that opened it, rather than being dropped on <body>.
  expect(open).toHaveFocus();
});

test("the drawer closes on the backdrop, and the focus comes back", async () => {
  narrow();
  const { container } = await shell();
  const open = screen.getByRole("button", { name: "Open the panel" });
  const aside = panel();
  fireEvent.click(open);
  const backdrop = container.querySelector("[data-backdrop]");
  expect(backdrop).not.toBeNull();
  fireEvent.click(backdrop as Element);
  expect(aside).toHaveAttribute("data-drawer", "closed");
  expect(open).toHaveFocus();
});

test("the closed drawer is out of a keyboard's way; the open one is a dialog", async () => {
  narrow();
  await shell();
  // jsdom answers `matchMedia` with "no match", so this is the narrow
  // layout: the panel is the drawer, and closed it is off the screen.
  const aside = panel();
  expect(aside).toHaveAttribute("inert");
  expect(aside).not.toHaveAttribute("aria-modal");

  fireEvent.click(screen.getByRole("button", { name: "Open the panel" }));
  expect(aside).not.toHaveAttribute("inert");
  expect(aside).toHaveAttribute("role", "dialog");
  expect(aside).toHaveAttribute("aria-modal", "true");
  // The page behind it does not scroll under it.
  expect(document.body.style.overflow).toBe("hidden");

  fireEvent.keyDown(document, { key: "Escape" });
  expect(aside).toHaveAttribute("inert");
  expect(document.body.style.overflow).toBe("");
});

/**
 * Say that this environment draws everything but `hidden`.
 *
 * jsdom has no layout engine and reports no boxes for anything, so without
 * this the trap cannot tell what is on the screen -- and the panel holds a
 * control that belongs to the other layout: "Collapse the panel" is hidden
 * by `./panel.css` below the breakpoint, `display: none`. A stop the
 * browser will not focus is a stop the trap must skip, or Tab deadlocks on
 * it. `vi.restoreAllMocks` in src/test/setup.ts puts this back.
 */
function drawsEverythingBut(hidden: Element) {
  const box = [new DOMRect(0, 0, 100, 20)] as unknown as DOMRectList;
  const none = [] as unknown as DOMRectList;
  vi.spyOn(Element.prototype, "getClientRects").mockImplementation(function (
    this: Element,
  ) {
    return this === hidden ? none : box;
  });
}

test("Tab goes round inside the open drawer, and never behind it", async () => {
  narrow();
  await shell();
  const outside = screen.getByRole("button", { name: "Open the panel" });
  fireEvent.click(outside);

  const collapse = screen.getByRole("button", { name: "Collapse the panel" });
  drawsEverythingBut(collapse);
  // The first stop is therefore the close button, not the collapse button
  // in front of it, which is the other layout's and is not drawn here.
  const first = screen.getByRole("button", { name: "Close the panel" });
  const last = screen.getByRole("button", { name: "Sign out" });

  last.focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(first).toHaveFocus();
  expect(collapse).not.toHaveFocus();

  fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
  expect(last).toHaveFocus();

  // The focus escaped some other way: the next Tab brings it back in.
  outside.focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(first).toHaveFocus();
});

test("a stop taken out of the tab order is not one", async () => {
  narrow();
  await shell();
  fireEvent.click(screen.getByRole("button", { name: "Open the panel" }));
  const collapse = screen.getByRole("button", { name: "Collapse the panel" });
  drawsEverythingBut(collapse);
  // A tabindex of -1 takes an element out of the tab order whatever it is,
  // so the first stop moves on to "New chat".
  screen
    .getByRole("button", { name: "Close the panel" })
    .setAttribute("tabindex", "-1");

  screen.getByRole("button", { name: "Sign out" }).focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(screen.getByRole("button", { name: "New chat" })).toHaveFocus();
});

test("a stop the page has made invisible is not one", async () => {
  narrow();
  await shell();
  fireEvent.click(screen.getByRole("button", { name: "Open the panel" }));
  const collapse = screen.getByRole("button", { name: "Collapse the panel" });
  drawsEverythingBut(collapse);
  // `visibility: hidden` still has a box, so `getClientRects` says nothing
  // about it and only the computed style does. Written inline, because
  // vitest loads no stylesheet and jsdom computes this much for real.
  screen.getByRole("button", { name: "Close the panel" }).style.visibility =
    "hidden";

  screen.getByRole("button", { name: "Sign out" }).focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(screen.getByRole("button", { name: "New chat" })).toHaveFocus();
});

test("with no layout to read, every stop counts", async () => {
  // jsdom as it is: no boxes for anything. Dropping every stop would be a
  // trap that traps nothing, so the DOM is what is left to go on.
  narrow();
  await shell();
  fireEvent.click(screen.getByRole("button", { name: "Open the panel" }));
  const last = screen.getByRole("button", { name: "Sign out" });
  last.focus();
  fireEvent.keyDown(document, { key: "Tab" });
  expect(
    screen.getByRole("button", { name: "Collapse the panel" }),
  ).toHaveFocus();
});

test("the agents are asked for once, however many new chats are started", async () => {
  const { fetch } = await shell();
  expect(
    fetch.mock.calls.filter(([url]) => String(url) === "/api/agents"),
  ).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "New chat" }));
  fireEvent.click(screen.getByRole("button", { name: "New chat" }));
  await act(async () => {
    await Promise.resolve();
  });
  expect(
    fetch.mock.calls.filter(([url]) => String(url) === "/api/agents"),
  ).toHaveLength(1);
});

test("signing out goes through the session, and a refusal is shown", async () => {
  const signOut = vi
    .fn<() => Promise<void>>()
    .mockRejectedValueOnce(new Error("the server said no"))
    .mockResolvedValueOnce(undefined);
  await shell(signOut);
  const button = screen.getByRole("button", { name: "Sign out" });
  await act(async () => {
    fireEvent.click(button);
  });
  expect(signOut).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("alert")).toHaveTextContent("the server said no");
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  });
  expect(signOut).toHaveBeenCalledTimes(2);
});

test("the sign-out button is not left disabled once it has finished", async () => {
  // The shell is usually on its way out by now, so this only shows when it
  // is not -- and then a button stuck disabled is one nobody can press.
  await shell(() => Promise.resolve());
  const button = screen.getByRole("button", { name: "Sign out" });
  await act(async () => {
    fireEvent.click(button);
  });
  expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
});

test("the local mode banner is there only in the local mode", async () => {
  answering();
  const { rerender } = render(<Shell session={SESSION} />);
  await waitFor(() => {
    expect(screen.queryByText("Loading the conversations…")).toBeNull();
  });
  expect(screen.queryByRole("note")).toBeNull();
  rerender(<Shell session={{ ...SESSION, local_development: true }} />);
  expect(screen.getByRole("note")).toHaveTextContent(/Local development mode/);
});

test("with no sign-in there is nothing to sign out of", async () => {
  answering();
  render(
    <Shell session={{ ...SESSION, sign_in: false, local_development: true }} />,
  );
  await waitFor(() => {
    expect(screen.queryByText("Loading the conversations…")).toBeNull();
  });
  expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  expect(screen.getByText("Sign-in is off")).toBeVisible();
});

/** Let the event loop run once, which is what a queued `hashchange` waits for. */
const settled = () => new Promise((done) => setTimeout(done, 0));

/** The routes a conversation being open needs answered. */
function withConversation(call: Call): Response | undefined {
  if (call.url === `/api/conversations/${id(1)}`) {
    return json(opened(conversation(1, "Robins"), []));
  }
  if (call.url.startsWith("/api/conversations?")) {
    return json(page([conversation(1, "Robins")]));
  }
  return undefined;
}

test("the hash decides which of the two the main area is", async () => {
  location.hash = `#/c/${id(1)}`;
  await shell(undefined, withConversation);

  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Robins");
  expect(screen.queryByText("New chat", { selector: "h1" })).toBeNull();
  // The panel's current item follows the route.
  expect(screen.getByRole("link", { name: "Robins" })).toHaveAttribute(
    "aria-current",
    "page",
  );
});

test("an open conversation says which agent it is with", async () => {
  location.hash = `#/c/${id(1)}`;
  await shell(undefined, withConversation);

  // The fixture's conversation is with "helper", which the agents call Helper.
  expect(screen.getByText("with Helper")).toBeInTheDocument();
});

test("a conversation the panel has not listed still has its title and agent", async () => {
  location.hash = `#/c/${id(1)}`;
  await shell(undefined, (call) => {
    if (call.url === `/api/conversations/${id(1)}`) {
      return json(opened(conversation(1, "Robins"), []));
    }
    return undefined;
  });

  await waitFor(() => {
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Robins",
    );
  });
  expect(screen.getByText("with Helper")).toBeInTheDocument();
  expect(screen.queryByLabelText("Agent")).toBeNull();
});

test("an agent the deployment no longer offers is named by its id", async () => {
  location.hash = `#/c/${id(1)}`;
  await shell(undefined, (call) => {
    if (call.url.startsWith("/api/agents")) {
      return json({
        items: [{ id: "other", title: "Other", engine: "langgraph" }],
      });
    }
    return withConversation(call);
  });

  expect(screen.getByText("with helper")).toBeInTheDocument();
});

test("the empty chat has the picker and no line about an agent", async () => {
  location.hash = "#/";
  await shell();

  expect(document.querySelector("[data-agent]")).toBeNull();
});

test("the chat stands where the placeholder stood, on both routes", async () => {
  // The empty chat: the box, and the agent picker above it. What used to be
  // here was a sentence saying the box arrives with the chat.
  const { unmount } = await shell();
  expect(screen.getByRole("textbox", { name: "Message input" })).toBeVisible();
  expect(screen.queryByText(/The message box arrives/)).toBeNull();
  unmount();

  location.hash = `#/c/${id(1)}`;
  await shell(undefined, (call) => {
    if (call.url === `/api/conversations/${id(1)}`) {
      return json(
        opened(conversation(1, "Robins"), [
          message("m1", "user", "Why do robins sing?"),
          message("m2", "assistant", "Because it is quiet."),
        ]),
      );
    }
    return withConversation(call);
  });
  await waitFor(() => {
    expect(screen.getByText("Because it is quiet.")).toBeInTheDocument();
  });
  expect(screen.getByRole("textbox", { name: "Message input" })).toBeVisible();
});

test("a first message routes to the conversation it created", async () => {
  const created = id(9);
  await shell(undefined, (call) => {
    if (call.url === "/api/turns") {
      return streamed(
        [event("RUN_FINISHED", { threadId: created, runId: RUN }, 2)],
        { headers: streamHeaders(RUN, created) },
      );
    }
    if (call.url === `/api/conversations/${created}`) {
      return json(opened(conversation(9, "Robins"), []));
    }
    if (call.url.startsWith("/api/conversations?")) {
      return json(page([conversation(9, "Robins")]));
    }
    return undefined;
  });

  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "Why do robins sing?" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settled();
  });
  expect(location.hash).toBe(`#/c/${created}`);
  // Who it is with is known before the panel has listed it.
  expect(screen.getByText("with Helper")).toBeInTheDocument();
  // The chat is not remounted by the route following it: the answer that is
  // arriving would be thrown away.
  await waitFor(() => {
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Robins",
    );
  });
});

test("an unknown hash is the empty chat, and the hash is left as it is", async () => {
  // The sign-in page reads this one for itself; nothing here may rewrite it.
  location.hash = "#/sign-in?error=not_allowed";
  await shell();
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
    "New chat",
  );
  expect(location.hash).toBe("#/sign-in?error=not_allowed");
});

test("New chat goes to the empty chat's own hash", async () => {
  location.hash = `#/c/${id(1)}`;
  await shell(undefined, withConversation);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    await settled();
  });
  expect(location.hash).toBe("#/");
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
    "New chat",
  );
});

test("opening a conversation from the panel is a navigation", async () => {
  await shell(undefined, withConversation);
  await act(async () => {
    fireEvent.click(screen.getByRole("link", { name: "Robins" }));
    await settled();
  });
  expect(location.hash).toBe(`#/c/${id(1)}`);
  await waitFor(() => {
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Robins",
    );
  });
});

test("deleting the conversation that is open leaves the page on the empty chat", async () => {
  location.hash = `#/c/${id(1)}`;
  let gone = false;
  await shell(undefined, (call) => {
    if (call.method === "DELETE") {
      gone = true;
      return json(null, 204);
    }
    if (gone) return json(page([]));
    return withConversation(call);
  });

  fireEvent.click(screen.getByRole("button", { name: "Actions for Robins" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: /^Yes, delete/ }));
    await settled();
  });

  expect(location.hash).toBe("#/");
  await waitFor(() => {
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "New chat",
    );
  });
  expect(screen.getByText("No conversations yet.")).toBeVisible();
});

test("the empty chat has a model picker beside the agent's, on its default", async () => {
  location.hash = "#/";
  await shell();
  expect(screen.getByLabelText("Agent")).toHaveValue("helper");
  expect(screen.getByLabelText("Model")).toHaveValue("sonnet");
});

test("picking a model on the empty chat leaves the focus on the picker", async () => {
  // A keyboard arrows through a closed <select> one change at a time; a
  // picker remounted by each of them would drop the focus to <body>.
  location.hash = "#/";
  await shell();
  const picker = screen.getByLabelText("Model");
  picker.focus();
  await act(async () => {
    fireEvent.change(picker, { target: { value: "opus" } });
    await settled();
  });
  expect(screen.getByLabelText("Model")).toBe(picker);
  expect(picker).toHaveFocus();
  expect(picker).toHaveValue("opus");
});

test("the model picked on the empty chat is the one the first message names", async () => {
  const created = id(9);
  const { fetch } = await shell(undefined, (call) => {
    if (call.url === "/api/turns") {
      return streamed(
        [event("RUN_FINISHED", { threadId: created, runId: RUN }, 2)],
        { headers: streamHeaders(RUN, created) },
      );
    }
    if (call.url === `/api/conversations/${created}`) {
      return json(opened({ ...conversation(9, "Robins"), model: "opus" }, []));
    }
    return undefined;
  });

  fireEvent.change(screen.getByLabelText("Model"), {
    target: { value: "opus" },
  });
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "Why do robins sing?" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settled();
  });
  expect(callsTo(fetch, "/api/turns", "POST")[0]?.body).toEqual({
    agent_id: "helper",
    model_id: "opus",
    text: "Why do robins sing?",
  });
  // The conversation it made shows the same model on its line.
  expect(screen.getByLabelText("Model")).toHaveValue("opus");
});

test("an open conversation's line has its model, and a change is a PUT", async () => {
  location.hash = `#/c/${id(1)}`;
  let model = "sonnet";
  const { fetch } = await shell(undefined, (call) => {
    if (
      call.method === "PUT" &&
      call.url === `/api/conversations/${id(1)}/model`
    ) {
      model = (call.body as { model_id: string }).model_id;
      return json({ ...conversation(1, "Robins"), model });
    }
    if (call.url === `/api/conversations/${id(1)}`) {
      return json(opened({ ...conversation(1, "Robins"), model }, []));
    }
    if (call.url.startsWith("/api/conversations?")) {
      return json(page([{ ...conversation(1, "Robins"), model }]));
    }
    return undefined;
  });

  const picker = await screen.findByLabelText("Model");
  expect(picker).toHaveValue("sonnet");
  const listings = callsTo(fetch, "/api/conversations").length;
  await act(async () => {
    fireEvent.change(picker, { target: { value: "opus" } });
    await settled();
  });
  expect(
    callsTo(fetch, `/api/conversations/${id(1)}/model`, "PUT").map(
      (call) => call.body,
    ),
  ).toEqual([{ model_id: "opus" }]);
  expect(screen.getByLabelText("Model")).toHaveValue("opus");
  // A change dates the conversation, so the panel's list is asked for again.
  await waitFor(() => {
    expect(callsTo(fetch, "/api/conversations")).toHaveLength(listings + 1);
  });
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a change that is refused is said, and the picker goes back", async () => {
  location.hash = `#/c/${id(1)}`;
  let answer = () =>
    refusal(422, "UnknownModelError", "body.model_id: is not a model");
  await shell(undefined, (call) => {
    if (call.method === "PUT") return answer();
    return withConversation(call);
  });

  const picker = await screen.findByLabelText("Model");
  await act(async () => {
    fireEvent.change(picker, { target: { value: "opus" } });
    await settled();
  });
  expect(screen.getByRole("alert")).toHaveTextContent(NOT_OFFERED);
  expect(screen.getByLabelText("Model")).toHaveValue("sonnet");

  // Anything else is the backend's own sentence, as a rename's is.
  answer = () =>
    refusal(404, "NotFoundError", "there is nothing here of that id");
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "opus" },
    });
    await settled();
  });
  expect(screen.getByRole("alert")).toHaveTextContent(
    "there is nothing here of that id",
  );
});

test("a model no longer offered is shown, and a turn refused for it says why", async () => {
  location.hash = `#/c/${id(1)}`;
  const retired = { ...conversation(1, "Robins"), model: "retired" };
  await shell(undefined, (call) => {
    if (call.url === `/api/conversations/${id(1)}/turns`) {
      return refusal(409, "ModelNotOfferedError", "the model is gone");
    }
    if (call.url === `/api/conversations/${id(1)}`) {
      return json(
        opened(retired, [
          message("m1", "user", "Why do robins sing?"),
          message("m2", "assistant", "Because it is quiet."),
        ]),
      );
    }
    if (call.url.startsWith("/api/conversations?")) {
      return json(page([retired]));
    }
    return undefined;
  });

  const picker = await screen.findByLabelText("Model");
  expect(picker).toHaveValue("retired");
  expect(
    screen.getByRole("option", { name: unoffered("retired") }),
  ).toBeDisabled();

  await waitFor(() => {
    expect(screen.getByText("Because it is quiet.")).toBeInTheDocument();
  });
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "And at night?" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settled();
  });
  // The chat's own sentence for it (`MODEL_GONE_NEW_CHAT` in
  // `src/chat/runtime.tsx`), and what was written is back in the box to send
  // once another is picked.
  expect(screen.getByRole("status")).toHaveTextContent(
    /model is no longer offered/,
  );
  expect(screen.getByRole("textbox", { name: "Message input" })).toHaveValue(
    "And at night?",
  );
});

test("a read answered before a model change cannot put the old model back", async () => {
  location.hash = `#/c/${id(1)}`;
  let model = "sonnet";
  const before = { ...conversation(1, "Robins"), model: "sonnet" };
  // The chat's read of the conversation is held until the change is saved,
  // and then answered with what was true before it: the network's order,
  // not the server's.
  let answerRead: (response: Response) => void = () => undefined;
  const read = new Promise<Response>((done) => {
    answerRead = done;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof globalThis.fetch>((input, init) => {
      const url = String(input);
      if (url === `/api/conversations/${id(1)}`) return read;
      if (init?.method === "PUT") {
        model = (JSON.parse(String(init.body)) as { model_id: string })
          .model_id;
        return Promise.resolve(
          json({ ...before, model, updated_at: "2026-09-03T10:00:00Z" }),
        );
      }
      if (url.startsWith("/api/conversations?")) {
        return Promise.resolve(json(page([{ ...before, model }])));
      }
      return Promise.resolve(
        otherwise({ url, method: "GET", body: undefined }),
      );
    }),
  );
  render(<Shell session={SESSION} />);

  const picker = await screen.findByLabelText("Model");
  expect(picker).toHaveValue("sonnet");
  await act(async () => {
    fireEvent.change(picker, { target: { value: "opus" } });
    await settled();
  });
  expect(screen.getByLabelText("Model")).toHaveValue("opus");

  await act(async () => {
    answerRead(json(opened(before, [])));
    await settled();
  });
  expect(screen.getByLabelText("Model")).toHaveValue("opus");
});

test("a model moved from another tab is the one shown, and picking the old one is sent", async () => {
  // The chat read the conversation on "sonnet"; the panel's page, written
  // later, has it on "opus". The later one is what the picker shows, so
  // picking "sonnet" again is a change and is sent.
  location.hash = `#/c/${id(1)}`;
  const read = { ...conversation(1, "Robins"), model: "sonnet" };
  const moved = {
    ...read,
    model: "opus",
    updated_at: "2026-09-03T10:00:00Z",
  };
  const { fetch } = await shell(undefined, (call) => {
    if (call.method === "PUT") {
      return json({ ...read, updated_at: "2026-09-04T10:00:00Z" });
    }
    if (call.url === `/api/conversations/${id(1)}`) {
      return json(opened(read, []));
    }
    if (call.url.startsWith("/api/conversations?")) {
      return json(page([moved]));
    }
    return undefined;
  });

  await waitFor(() => {
    expect(screen.getByLabelText("Model")).toHaveValue("opus");
  });
  await act(async () => {
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "sonnet" },
    });
    await settled();
  });
  expect(
    callsTo(fetch, `/api/conversations/${id(1)}/model`, "PUT").map(
      (call) => call.body,
    ),
  ).toEqual([{ model_id: "sonnet" }]);
  expect(screen.getByLabelText("Model")).toHaveValue("sonnet");
});

test("answers written in the same millisecond are ordered by their microseconds", async () => {
  // The chat's read and the panel's row fall in one millisecond; the row,
  // on "opus", is the later by 800 microseconds, and is what the picker shows.
  location.hash = `#/c/${id(1)}`;
  const read = {
    ...conversation(1, "Robins"),
    model: "sonnet",
    updated_at: "2026-09-03T10:00:00.000100Z",
  };
  const moved = {
    ...read,
    model: "opus",
    updated_at: "2026-09-03T10:00:00.000900Z",
  };
  await shell(undefined, (call) => {
    if (call.url === `/api/conversations/${id(1)}`) {
      return json(opened(read, []));
    }
    if (call.url.startsWith("/api/conversations?")) {
      return json(page([moved]));
    }
    return undefined;
  });

  await waitFor(() => {
    expect(screen.getByLabelText("Model")).toHaveValue("opus");
  });
});

test("without the list of models, the line still says which model it is on", async () => {
  location.hash = `#/c/${id(1)}`;
  await shell(undefined, (call) => {
    if (call.url.startsWith("/api/models")) {
      return refusal(500, "InternalError", "something went wrong");
    }
    return withConversation(call);
  });

  await waitFor(() => {
    expect(screen.getByText("sonnet")).toBeInTheDocument();
  });
  expect(screen.queryByLabelText("Model")).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent(
    /models could not be loaded/,
  );
});

test("a model changed on a conversation opened from the panel stays changed", async () => {
  // On one conversation, open another from the panel, and change its model
  // before the chat's read of it has landed: what the shell holds is still
  // the conversation it came from. The panel's page, asked again, is no
  // help either -- here it was read before the change was saved.
  location.hash = `#/c/${id(2)}`;
  const first = { ...conversation(1, "Robins"), model: "sonnet" };
  const second = { ...conversation(2, "Wrens"), model: "sonnet" };
  let answerRead: (response: Response) => void = () => undefined;
  const read = new Promise<Response>((done) => {
    answerRead = done;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof globalThis.fetch>((input, init) => {
      const url = String(input);
      if (url === `/api/conversations/${id(1)}`) return read;
      if (url === `/api/conversations/${id(2)}`) {
        return Promise.resolve(json(opened(second, [])));
      }
      if (init?.method === "PUT") {
        const model = (JSON.parse(String(init.body)) as { model_id: string })
          .model_id;
        return Promise.resolve(
          json({ ...first, model, updated_at: "2026-09-03T10:00:00Z" }),
        );
      }
      if (url.startsWith("/api/conversations?")) {
        return Promise.resolve(json(page([first, second])));
      }
      return Promise.resolve(
        otherwise({ url, method: "GET", body: undefined }),
      );
    }),
  );
  render(<Shell session={SESSION} />);
  await waitFor(() => {
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Wrens",
    );
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("link", { name: "Robins" }));
    await settled();
  });
  await waitFor(() => {
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      "Robins",
    );
  });

  const picker = screen.getByLabelText("Model");
  expect(picker).toHaveValue("sonnet");
  await act(async () => {
    fireEvent.change(picker, { target: { value: "opus" } });
    await settled();
  });
  // The change's answer is what the shell now holds about this one.
  expect(screen.getByLabelText("Model")).toHaveValue("opus");

  // And the read answered before the change was saved does not undo it.
  await act(async () => {
    answerRead(json(opened(first, [])));
    await settled();
  });
  expect(screen.getByLabelText("Model")).toHaveValue("opus");
});

test("without the list, a remembered model is still the one a first message names", async () => {
  location.hash = "#/";
  localStorage.setItem("robinauts.model", "retired");
  const { fetch } = await shell(undefined, (call) => {
    if (call.url.startsWith("/api/models")) {
      return refusal(500, "InternalError", "something went wrong");
    }
    if (call.url === "/api/turns") {
      return refusal(422, "UnknownModelError", "body.model_id: is not a model");
    }
    return undefined;
  });

  const send = async () => {
    const box = screen.getByRole("textbox", { name: "Message input" });
    fireEvent.change(box, { target: { value: "Why do robins sing?" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send message" }));
      await settled();
    });
  };
  await send();
  expect(callsTo(fetch, "/api/turns", "POST")[0]?.body).toMatchObject({
    model_id: "retired",
  });
  // Refused for it: said, the message is back, and the model is forgotten,
  // so the next one goes to the agent's default.
  expect(screen.getByRole("status")).toHaveTextContent(/no longer offered/);
  expect(screen.getByRole("textbox", { name: "Message input" })).toHaveValue(
    "Why do robins sing?",
  );
  expect(localStorage.getItem("robinauts.model")).toBeNull();
  await send();
  expect(callsTo(fetch, "/api/turns", "POST")[1]?.body).toMatchObject({
    model_id: null,
  });
});

test("a first message refused for the agent's default asks for the models again", async () => {
  // The agent's default, "sonnet", has been removed since the list came:
  // the refusal asks for the list again, which no longer has it, so the
  // next message goes to a model still offered.
  location.hash = "#/";
  const created = id(9);
  let lists = 0;
  const { fetch } = await shell(undefined, (call) => {
    if (call.url.startsWith("/api/models")) {
      lists += 1;
      return lists === 1
        ? undefined
        : json({ items: [{ id: "opus", title: "Opus" }] });
    }
    if (call.url === "/api/turns") {
      const asked = (call.body as { model_id: string }).model_id;
      if (asked === "sonnet") {
        return refusal(
          422,
          "UnknownModelError",
          "body.model_id: is not a model",
        );
      }
      return streamed(
        [event("RUN_FINISHED", { threadId: created, runId: RUN }, 2)],
        { headers: streamHeaders(RUN, created) },
      );
    }
    if (call.url === `/api/conversations/${created}`) {
      return json(opened(conversation(9, "Robins"), []));
    }
    return undefined;
  });
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "Why do robins sing?" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settled();
  });
  expect(screen.getByRole("status")).toHaveTextContent(/no longer offered/);
  await waitFor(() => {
    expect(screen.getByLabelText("Model")).toHaveValue("opus");
  });

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settled();
  });
  expect(
    callsTo(fetch, "/api/turns", "POST").map(
      (call) => (call.body as { model_id: string }).model_id,
    ),
  ).toEqual(["sonnet", "opus"]);
});

test("with the list in hand, a first message refused for its model forgets the pick", async () => {
  // Picked from a list that has gone stale since: the next message goes to
  // the agent's default, and is not refused for the same model again.
  location.hash = "#/";
  const created = id(9);
  const { fetch } = await shell(undefined, (call) => {
    if (call.url === "/api/turns") {
      const asked = (call.body as { model_id: string }).model_id;
      if (asked === "opus") {
        return refusal(
          422,
          "UnknownModelError",
          "body.model_id: is not a model",
        );
      }
      return streamed(
        [event("RUN_FINISHED", { threadId: created, runId: RUN }, 2)],
        { headers: streamHeaders(RUN, created) },
      );
    }
    if (call.url === `/api/conversations/${created}`) {
      return json(opened(conversation(9, "Robins"), []));
    }
    return undefined;
  });
  fireEvent.change(screen.getByLabelText("Model"), {
    target: { value: "opus" },
  });
  const send = async () => {
    const box = screen.getByRole("textbox", { name: "Message input" });
    fireEvent.change(box, { target: { value: "Why do robins sing?" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send message" }));
      await settled();
    });
  };
  await send();
  expect(screen.getByRole("status")).toHaveTextContent(/no longer offered/);
  expect(screen.getByRole("textbox", { name: "Message input" })).toHaveValue(
    "Why do robins sing?",
  );
  expect(localStorage.getItem("robinauts.model")).toBeNull();
  expect(screen.getByLabelText("Model")).toHaveValue("sonnet");

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settled();
  });
  expect(
    callsTo(fetch, "/api/turns", "POST").map(
      (call) => (call.body as { model_id: string }).model_id,
    ),
  ).toEqual(["opus", "sonnet"]);
  expect(location.hash).toBe(`#/c/${created}`);
});

test("a first message refused as not there keeps the text and the pick", async () => {
  // The agent, and not the model: nothing to forget.
  location.hash = "#/";
  await shell(undefined, (call) =>
    call.url === "/api/turns"
      ? refusal(404, "NotFoundError", "there is nothing here of that id")
      : undefined,
  );
  fireEvent.change(screen.getByLabelText("Model"), {
    target: { value: "opus" },
  });
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "Why do robins sing?" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settled();
  });
  expect(screen.getByRole("status")).toHaveTextContent(
    /agent it was for is no longer offered/,
  );
  expect(screen.getByRole("textbox", { name: "Message input" })).toHaveValue(
    "Why do robins sing?",
  );
  expect(localStorage.getItem("robinauts.model")).toBe("opus");
  expect(screen.getByLabelText("Model")).toHaveValue("opus");
});

test("the profile block links to the linked devices, beside sign-out", async () => {
  await shell();
  const profile = screen.getByRole("region", { name: "Profile" });
  expect(
    within(profile).getByRole("link", { name: "Linked devices" }),
  ).toHaveAttribute("href", "#/devices");
  expect(
    within(profile).getByRole("button", { name: "Sign out" }),
  ).toBeVisible();
});

test("in the rail the link keeps its name, as sign-out does", async () => {
  localStorage.setItem(`robinauts.${PANEL_KEY}`, "rail");
  await shell();
  const link = screen.getByRole("link", { name: "Linked devices" });
  expect(link).toHaveAttribute("title", "Linked devices");
});

test("#/devices is a page of the shell: the panel stays, the chat goes", async () => {
  location.hash = "#/devices";
  await shell();
  expect(panel()).toBeVisible();
  expect(
    screen.getByRole("heading", { level: 1, name: "Linked devices" }),
  ).toBeVisible();
  expect(screen.queryByRole("heading", { name: "New chat" })).toBeNull();
  expect(screen.getByRole("link", { name: "Linked devices" })).toHaveAttribute(
    "aria-current",
    "page",
  );
});

const sidePanel = () =>
  screen.getByRole("complementary", { name: "Side panel" });

/** The frame the flows page draws; the panel's link has the same name. */
const frameOrNull = () =>
  document.querySelector<HTMLIFrameElement>('iframe[title="Flows"]');
const frame = () => {
  const found = frameOrNull();
  if (found === null) throw new Error("no Flows frame");
  return found;
};

/** A server that mounts an orchestrator, and says where its page is. */
const WITH_FLOWS: Session = { ...SESSION, flows_url: "/neorc/ui/" };

/** The orchestrator's API, as the panel's section reads it. */
function orchestrator(call: Call): Response | undefined {
  if (call.url === "/neorc/flows") {
    return json({ flows: [{ name: "guess_number", version: "0.3.0" }] });
  }
  if (call.url.startsWith("/neorc/runs?")) {
    return json({
      runs: [{ id: RUN, flow: "guess_number", status: "active" }],
    });
  }
  if (call.url === "/neorc/events/latest") return json({ sequence: 0 });
  if (call.url.startsWith("/neorc/events?")) return json({ events: [] });
  return undefined;
}

const flowsSection = () => screen.getByRole("navigation", { name: "Flows" });

test("the orchestrator's navigation is a section of the panel, below the history", async () => {
  const { unmount } = await shell();
  expect(screen.queryByRole("navigation", { name: "Flows" })).toBeNull();
  unmount();
  await shell(undefined, orchestrator, WITH_FLOWS);
  const section = flowsSection();
  const history = screen.getByRole("navigation", { name: "History" });
  expect(
    history.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(
    within(section).getByRole("link", { name: "New run" }),
  ).toHaveAttribute("href", "#/flows/new");
  expect(within(section).getByRole("link", { name: "Runs" })).toHaveAttribute(
    "href",
    "#/flows",
  );
  // Its flows and its latest runs, read from the orchestrator.
  const flows = await within(section).findByRole("list", { name: "Workflows" });
  expect(
    within(flows).getByRole("link", { name: "guess_number" }),
  ).toHaveAttribute("href", "#/flows/flow/guess_number");
  const runs = within(section).getByRole("list", { name: "Recent runs" });
  const run = within(runs).getByRole("link");
  expect(run).toHaveAttribute("href", `#/flows/runs/${RUN}`);
  expect(run).toHaveAttribute("title", "guess_number 11111111, active");
  // In the rail, the two ways in stay, as icons with their names.
  fireEvent.click(screen.getByRole("button", { name: "Collapse the panel" }));
  expect(within(section).getByRole("link", { name: "Runs" })).toHaveAttribute(
    "title",
    "Runs",
  );
});

test("#/flows frames the orchestrator's runs, embedded, where the chat would be", async () => {
  location.hash = "#/flows";
  await shell(undefined, orchestrator, WITH_FLOWS);
  expect(panel()).toBeVisible();
  expect(frame()).toHaveAttribute("src", "/neorc/ui/?embedded#/runs");
  expect(screen.queryByRole("heading", { name: "New chat" })).toBeNull();
  expect(
    within(flowsSection()).getByRole("link", { name: "Runs" }),
  ).toHaveAttribute("aria-current", "page");
  // Not a conversation, so nothing about one.
  expect(
    screen.queryByRole("complementary", { name: "Side panel" }),
  ).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Open the side panel" }),
  ).toBeNull();
});

test("a run's route opens that run in the frame, and its link says so", async () => {
  location.hash = `#/flows/runs/${RUN}`;
  await shell(undefined, orchestrator, WITH_FLOWS);
  expect(frame()).toHaveAttribute("src", `/neorc/ui/?embedded#/runs/${RUN}`);
  const runs = await within(flowsSection()).findByRole("list", {
    name: "Recent runs",
  });
  expect(within(runs).getByRole("link")).toHaveAttribute(
    "aria-current",
    "page",
  );
});

test("#/flows on a server with no orchestrator is the empty chat", async () => {
  location.hash = "#/flows";
  await shell();
  expect(frameOrNull()).toBeNull();
  expect(
    screen.getByRole("heading", { level: 1, name: "New chat" }),
  ).toBeVisible();
});

/** The helper agent's one app, as `GET /api/apps` lists it. */
const TO_DO_APP = {
  id: "todo/show_todos",
  server: "todo",
  tool: "show_todos",
  title: "To-do list",
  description: "The list.",
};

/** The helper agent has the to-do list app; the rest as `otherwise`. */
function withApp(call: Call): Response | undefined {
  return call.url === "/api/apps?agent=helper"
    ? json({ apps: [TO_DO_APP] })
    : undefined;
}

test("the agent's apps are offered, and open as a sandboxed frame", async () => {
  location.hash = `#/c/${id(1)}`;
  await shell(undefined, (call) => {
    if (call.url === `/api/conversations/${id(1)}`) {
      return json(opened(conversation(1, "Robins"), []));
    }
    return withApp(call) ?? withConversation(call);
  });
  fireEvent.click(
    screen.getByRole("button", { name: "Expand the side panel" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Add a tab" }));
  await waitFor(() => {
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toEqual(["To-do list"]);
  });
  fireEvent.click(screen.getByRole("menuitem", { name: "To-do list" }));
  const app = within(screen.getByRole("tabpanel", { name: "To-do list" }));
  const frame = app.getByTitle("To-do list");
  expect(frame).toHaveAttribute(
    "src",
    "/api/apps/todo/show_todos/view?agent=helper",
  );
  expect(frame).toHaveAttribute("sandbox", "allow-scripts");
});

test("on a small screen the side panel is a drawer, opened from the right", async () => {
  narrow();
  const { container } = await shell();
  const aside = sidePanel();
  expect(aside).toHaveAttribute("inert");
  const open = screen.getByRole("button", { name: "Open the side panel" });
  fireEvent.click(open);
  expect(aside).toHaveAttribute("data-drawer", "open");
  expect(aside).toHaveAttribute("role", "dialog");
  // Drawn whole, whatever this browser left the rail as.
  expect(screen.getByRole("button", { name: "Add a tab" })).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Close the side panel" }),
  ).toHaveFocus();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(aside).toHaveAttribute("data-drawer", "closed");
  expect(open).toHaveFocus();

  // The two drawers are never open together. Held on to, as above: open,
  // a panel is a dialog and no longer a landmark.
  const left = panel();
  fireEvent.click(open);
  fireEvent.click(screen.getByRole("button", { name: "Open the panel" }));
  expect(aside).toHaveAttribute("data-drawer", "closed");
  expect(left).toHaveAttribute("data-drawer", "open");
  const backdrop = container.querySelector("[data-backdrop]");
  fireEvent.click(backdrop as Element);
  expect(left).toHaveAttribute("data-drawer", "closed");
});

test("Escape in the side panel's menu closes the menu, not the drawer", async () => {
  narrow();
  await shell(undefined, withApp);
  const aside = sidePanel();
  fireEvent.click(screen.getByRole("button", { name: "Open the side panel" }));
  const add = screen.getByRole("button", { name: "Add a tab" });
  await waitFor(() => {
    expect(add).toBeEnabled();
  });
  fireEvent.click(add);
  fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
  expect(screen.queryByRole("menu")).toBeNull();
  expect(aside).toHaveAttribute("data-drawer", "open");
});

test("tabs opened on the empty chat go with the conversation it starts", async () => {
  localStorage.setItem("robinauts.side", "open");
  const created = id(9);
  await shell(undefined, (call) => {
    if (call.url === "/api/turns") {
      return streamed(
        [event("RUN_FINISHED", { threadId: created, runId: RUN }, 2)],
        { headers: streamHeaders(RUN, created) },
      );
    }
    if (call.url === `/api/conversations/${created}`) {
      return json(opened(conversation(9, "Robins"), []));
    }
    return withApp(call);
  });
  const add = screen.getByRole("button", { name: "Add a tab" });
  await waitFor(() => {
    expect(add).toBeEnabled();
  });
  fireEvent.click(add);
  fireEvent.click(screen.getByRole("menuitem", { name: "To-do list" }));
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: "Why do robins sing?" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await settled();
  });
  expect(location.hash).toBe(`#/c/${created}`);
  expect(screen.getByRole("tab", { name: "To-do list" })).toBeInTheDocument();
  // And a new chat starts with nothing open.
  fireEvent.click(screen.getByRole("button", { name: "New chat" }));
  await act(async () => {
    await settled();
  });
  expect(screen.queryByRole("tab")).toBeNull();
});

test("the side panel is about a conversation, so not on a page that is not one", async () => {
  location.hash = "#/devices";
  await shell();
  expect(
    screen.queryByRole("complementary", { name: "Side panel" }),
  ).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Open the side panel" }),
  ).toBeNull();
});
