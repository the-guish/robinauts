// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { act, render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import type { Transcript } from "../chat";
import type { Models } from "../shell/ModelPicker";
import {
  ContextWindowTab,
  contextWindowOf,
  shownModel,
  UsageTab,
} from "./BuiltInTabs";
import { transcriptStore, usageOf } from "./transcript";

// 40 + 20 characters said, 8 thought, and a call: 7 + 14 + 13.
const THREAD: Transcript = [
  { role: "user", parts: [{ kind: "text", text: "x".repeat(40) }] },
  {
    role: "assistant",
    parts: [
      { kind: "reasoning", text: "y".repeat(8) },
      {
        kind: "tool-call",
        name: "s__find",
        argsText: '{"q":"robins"}',
        result: "two robins.  ",
      },
      { kind: "text", text: "z".repeat(20) },
    ],
  },
];

const MODELS: Models = {
  status: "ready",
  items: [{ id: "sonnet", title: "Sonnet", context_window: null }],
};

function value(term: string): string | null | undefined {
  return screen.getByText(term, { selector: "dt" }).nextElementSibling
    ?.textContent;
}

test("what the transcript tells is counted, and the tokens estimated", () => {
  expect(usageOf(THREAD)).toEqual({
    questions: 1,
    answers: 1,
    toolCalls: 1,
    text: 60,
    reasoning: 8,
    tools: 7 + 14 + 13,
    characters: 60 + 8 + 34,
    estimatedTokens: Math.ceil(102 / 4),
  });
  expect(usageOf([])).toMatchObject({ characters: 0, estimatedTokens: 0 });
});

test("Usage follows the thread as it changes, and says it is an estimate", () => {
  const store = transcriptStore();
  render(<UsageTab transcript={store} />);
  expect(value("Messages")).toBe("0");
  act(() => {
    store.publish(THREAD);
  });
  expect(value("Messages")).toBe("2");
  expect(value("Questions")).toBe("1");
  expect(value("Answers")).toBe("1");
  expect(value("Tool calls")).toBe("1");
  expect(value("Characters")).toBe("102");
  expect(value("Tokens (estimate)")).toBe("about 26");
  expect(screen.getByText(/An estimate/)).toBeVisible();
  expect(screen.getByText(/planned and not built yet/)).toBeVisible();
});

test("Context window says unknown when the deployment does not say", () => {
  const store = transcriptStore();
  store.publish(THREAD);
  render(
    <ContextWindowTab
      transcript={store}
      model={shownModel(MODELS, "sonnet")}
      contextWindow={contextWindowOf(MODELS, "sonnet")}
    />,
  );
  expect(value("Model")).toBe("Sonnet");
  expect(value("Window")).toBe("unknown");
  expect(value("In the thread")).toBe("about 26 tokens");
  expect(screen.queryByRole("meter")).toBeNull();
  expect(
    screen.getByText(/does not say how large this model's window is/),
  ).toBeVisible();
});

test("with a window, the estimate is a meter against it", () => {
  const store = transcriptStore();
  store.publish(THREAD);
  render(
    <ContextWindowTab
      transcript={store}
      model={{ id: "sonnet", title: "Sonnet" }}
      contextWindow={100}
    />,
  );
  expect(value("Window")).toBe("100 tokens");
  const meter = screen.getByRole("meter", { name: "Context used" });
  expect(meter).toHaveAttribute("aria-valuenow", "26");
  expect(meter).toHaveAttribute("aria-valuemax", "100");
  expect(meter).toHaveAttribute(
    "aria-valuetext",
    "about 26 of 100 tokens, 26%",
  );
});

test("the window is read off the model list when it states one", () => {
  const listing = (window: number | null): Models => ({
    status: "ready",
    items: [{ id: "sonnet", title: "Sonnet", context_window: window }],
  });
  expect(contextWindowOf(listing(200_000), "sonnet")).toBe(200_000);
  expect(contextWindowOf(listing(2.5), "sonnet")).toBe(null);
  expect(contextWindowOf(listing(0), "sonnet")).toBe(null);
  expect(contextWindowOf(listing(null), "sonnet")).toBe(null);
  expect(contextWindowOf(listing(200_000), "opus")).toBe(null);
  expect(contextWindowOf(listing(200_000), null)).toBe(null);
  expect(contextWindowOf({ status: "loading" }, "sonnet")).toBe(null);
  // A model the list has not got is named by its id.
  expect(shownModel({ status: "loading" }, "opus")).toEqual({
    id: "opus",
    title: "opus",
  });
  expect(shownModel(MODELS, null)).toBeNull();
});
