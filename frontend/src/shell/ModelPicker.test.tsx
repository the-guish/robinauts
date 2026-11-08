// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { conversation, id } from "../test/conversations";
import {
  ConversationModel,
  MODEL_KEY,
  ModelPicker,
  unoffered,
  useChosenModel,
  useModels,
  type Model,
  type Models,
} from "./ModelPicker";

const MODELS: Model[] = [
  { id: "sonnet", title: "Claude Sonnet" },
  { id: "gpt", title: "GPT 5.5" },
  { id: "gemini", title: "Gemini 2.5 Pro" },
];

const ready = (items: Model[]): Models => ({ status: "ready", items });

const kept = () => localStorage.getItem(`robinauts.${MODEL_KEY}`);

/** The picker with the choice above it, as the shell holds the pair. */
function Picking({
  models,
  agentDefault,
}: {
  models: Models;
  agentDefault: string | null;
}) {
  const [chosen, choose] = useChosenModel(models, agentDefault);
  return <ModelPicker models={models} chosen={chosen} onChoose={choose} />;
}

test("the models by their titles, and no Default among them", () => {
  render(<Picking models={ready(MODELS)} agentDefault="sonnet" />);
  const options = screen
    .getAllByRole("option")
    .map((option) => option.textContent);
  expect(options).toEqual(["Claude Sonnet", "GPT 5.5", "Gemini 2.5 Pro"]);
});

test("nothing picked yet: the agent's default is what is selected", () => {
  render(<Picking models={ready(MODELS)} agentDefault="gpt" />);
  expect(screen.getByLabelText("Model")).toHaveValue("gpt");
  // Choosing the default for somebody is not them choosing it.
  expect(kept()).toBeNull();
});

test("a pick is remembered, and kept whichever agent is chosen", () => {
  const { rerender } = render(
    <Picking models={ready(MODELS)} agentDefault="sonnet" />,
  );
  const picker = screen.getByLabelText("Model");
  fireEvent.change(picker, { target: { value: "gemini" } });
  expect(picker).toHaveValue("gemini");
  expect(kept()).toBe("gemini");
  rerender(<Picking models={ready(MODELS)} agentDefault="gpt" />);
  expect(screen.getByLabelText("Model")).toHaveValue("gemini");
});

test("until something is picked, another agent brings its own default", () => {
  const { rerender } = render(
    <Picking models={ready(MODELS)} agentDefault="sonnet" />,
  );
  expect(screen.getByLabelText("Model")).toHaveValue("sonnet");
  rerender(<Picking models={ready(MODELS)} agentDefault="gpt" />);
  expect(screen.getByLabelText("Model")).toHaveValue("gpt");
});

test("what this browser picked is what it opens with", () => {
  localStorage.setItem(`robinauts.${MODEL_KEY}`, "gemini");
  render(<Picking models={ready(MODELS)} agentDefault="sonnet" />);
  expect(screen.getByLabelText("Model")).toHaveValue("gemini");
});

test("a remembered model the deployment no longer offers is not kept", () => {
  localStorage.setItem(`robinauts.${MODEL_KEY}`, "gone");
  render(<Picking models={ready(MODELS)} agentDefault="gpt" />);
  expect(screen.getByLabelText("Model")).toHaveValue("gpt");
});

test("the model a first message would run on", () => {
  const { result } = renderHook(() => useChosenModel(ready(MODELS), "gpt"));
  expect(result.current[0]).toBe("gpt");
  // Not arrived, or not coming: nobody who never picked gets a model named,
  // and the backend runs the agent's default.
  const loading = renderHook(() =>
    useChosenModel({ status: "loading" }, "gpt"),
  );
  expect(loading.result.current[0]).toBeNull();
  // Somebody who did gets what they picked, unchecked: the backend checks it.
  localStorage.setItem(`robinauts.${MODEL_KEY}`, "gemini");
  const failed = renderHook(() =>
    useChosenModel({ status: "failed", detail: "no" }, "gpt"),
  );
  expect(failed.result.current[0]).toBe("gemini");
});

test("a remembered model the backend refused is forgotten", () => {
  localStorage.setItem(`robinauts.${MODEL_KEY}`, "retired");
  const { result } = renderHook(() =>
    useChosenModel({ status: "failed", detail: "no" }, "gpt"),
  );
  expect(result.current[0]).toBe("retired");
  // Refused for some other model than the one kept: nothing to forget.
  act(() => {
    result.current[2]("gemini");
  });
  expect(kept()).toBe("retired");
  act(() => {
    result.current[2]("retired");
  });
  expect(result.current[0]).toBeNull();
  expect(kept()).toBeNull();
});

test("a model no longer offered is shown as it is, and cannot be picked again", () => {
  const choose = vi.fn();
  render(
    <ModelPicker models={ready(MODELS)} chosen="retired" onChoose={choose} />,
  );
  const picker = screen.getByLabelText("Model");
  expect(picker).toHaveValue("retired");
  const stale = screen.getByRole("option", { name: unoffered("retired") });
  expect(stale).toBeDisabled();
  fireEvent.change(picker, { target: { value: "gpt" } });
  expect(choose).toHaveBeenCalledWith("gpt");
});

test("while a change is being saved, the picker says so and still picks", () => {
  const choose = vi.fn();
  render(
    <ModelPicker models={ready(MODELS)} chosen="gpt" onChoose={choose} busy />,
  );
  const picker = screen.getByLabelText("Model");
  expect(picker).toHaveAttribute("aria-busy", "true");
  expect(picker).not.toHaveAttribute("aria-disabled");
  fireEvent.change(picker, { target: { value: "gemini" } });
  expect(choose).toHaveBeenCalledWith("gemini");
});

/**
 * A PUT of the model that answers only when the test says so, and the
 * bodies it was sent.
 */
function heldPuts() {
  const sent: string[] = [];
  const answers: (() => void)[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof globalThis.fetch>((_input, init) => {
      const model = (JSON.parse(String(init?.body)) as { model_id: string })
        .model_id;
      sent.push(model);
      return new Promise<Response>((done) => {
        answers.push(() => {
          done(
            new Response(JSON.stringify({ ...conversation(1), model }), {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
          );
        });
      });
    }),
  );
  /** Answer the oldest PUT not yet answered. */
  const answer = async () => {
    await act(async () => {
      answers.shift()?.();
      await new Promise((done) => setTimeout(done, 0));
    });
  };
  return { sent, answer };
}

test("stepping through the list saves where it stops, not every step", async () => {
  const { sent, answer } = heldPuts();
  const onMoved = vi.fn();
  render(
    <ConversationModel
      models={ready(MODELS)}
      conversationId={id(1)}
      model="sonnet"
      onMoved={onMoved}
    />,
  );
  const picker = screen.getByLabelText("Model");
  // Arrow keys on a closed select: a change at every step.
  fireEvent.change(picker, { target: { value: "gpt" } });
  fireEvent.change(picker, { target: { value: "gemini" } });
  expect(sent).toEqual(["gpt"]);
  // What is shown is where the person is, not what is being saved.
  expect(picker).toHaveValue("gemini");
  await answer();
  // The first has answered: the latest is sent, and nothing in between.
  expect(sent).toEqual(["gpt", "gemini"]);
  await answer();
  expect(onMoved.mock.calls.map(([moved]) => moved.model)).toEqual([
    "gpt",
    "gemini",
  ]);
});

test("a step back to where the conversation now is is not sent again", async () => {
  const { sent, answer } = heldPuts();
  render(
    <ConversationModel
      models={ready(MODELS)}
      conversationId={id(1)}
      model="sonnet"
      onMoved={() => undefined}
    />,
  );
  const picker = screen.getByLabelText("Model");
  fireEvent.change(picker, { target: { value: "gpt" } });
  fireEvent.change(picker, { target: { value: "gemini" } });
  fireEvent.change(picker, { target: { value: "gpt" } });
  await answer();
  // The conversation is on GPT now, which is where the person stopped.
  expect(sent).toEqual(["gpt"]);
});

test("while they are being fetched, and when they cannot be", () => {
  const { unmount } = render(
    <Picking models={{ status: "loading" }} agentDefault="sonnet" />,
  );
  expect(screen.getByText("Loading the models…")).toBeVisible();
  unmount();
  render(
    <Picking
      models={{ status: "failed", detail: "no network" }}
      agentDefault="sonnet"
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent("no network");
});

test("the list is asked for once", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
    new Response(JSON.stringify({ items: MODELS }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetch);
  const { result, rerender } = renderHook(() => useModels());
  await act(async () => {
    await Promise.resolve();
  });
  expect(result.current).toEqual({ status: "ready", items: MODELS });
  rerender();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0]?.[0]).toBe("/api/models");
});
