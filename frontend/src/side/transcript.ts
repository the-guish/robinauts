// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The thread on the screen, held where the side panel's tabs can read it,
 * and what they count in it.
 *
 * **A store, not state of the shell's.** The chat says what its thread is at
 * every token of a stream (`onTranscript`, `../chat/index.ts`). Held in the
 * shell's state, each token would draw the whole shell again, chat included;
 * held here, only the tabs that read it are drawn again, and only while they
 * are open.
 *
 * **Counted, not measured.** Usage recording is planned and not built
 * (`docs/specs/core.md`, goal 7), so nothing in the browser knows how many
 * tokens a turn took. What is counted is what the transcript says, and the
 * token count is an estimate: a character count divided by four, which is
 * the usual rule of thumb for English text and is labelled as one wherever
 * it is shown.
 */
import { useSyncExternalStore } from "react";

import type { Transcript } from "../chat";

export interface TranscriptStore {
  /** Hand it the thread; stable, so it can be given to the chat as it is. */
  readonly publish: (transcript: Transcript) => void;
  readonly subscribe: (changed: () => void) => () => void;
  readonly current: () => Transcript;
}

export function transcriptStore(): TranscriptStore {
  let held: Transcript = [];
  const listeners = new Set<() => void>();
  return {
    publish: (transcript) => {
      if (transcript === held) return;
      held = transcript;
      for (const listener of listeners) listener();
    },
    subscribe: (changed) => {
      listeners.add(changed);
      return () => {
        listeners.delete(changed);
      };
    },
    current: () => held,
  };
}

export function useTranscript(store: TranscriptStore): Transcript {
  return useSyncExternalStore(store.subscribe, store.current, store.current);
}

/** Characters to a token, roughly, for the estimate. */
export const CHARACTERS_PER_TOKEN = 4;

export interface Usage {
  /** Messages by who wrote them. */
  readonly questions: number;
  readonly answers: number;
  readonly toolCalls: number;
  /** Characters of what was said, thought, and sent to and from tools. */
  readonly text: number;
  readonly reasoning: number;
  readonly tools: number;
  readonly characters: number;
  /** `characters` over `CHARACTERS_PER_TOKEN`, rounded up. An estimate. */
  readonly estimatedTokens: number;
}

/** What the transcript tells about how much the conversation holds. */
export function usageOf(transcript: Transcript): Usage {
  let questions = 0;
  let answers = 0;
  let toolCalls = 0;
  let text = 0;
  let reasoning = 0;
  let tools = 0;
  for (const message of transcript) {
    if (message.role === "user") questions += 1;
    else answers += 1;
    for (const part of message.parts) {
      if (part.kind !== "tool-call") {
        if (part.kind === "text") text += part.text.length;
        else reasoning += part.text.length;
      } else {
        toolCalls += 1;
        tools +=
          part.name.length + part.argsText.length + (part.result?.length ?? 0);
      }
    }
  }
  const characters = text + reasoning + tools;
  return {
    questions,
    answers,
    toolCalls,
    text,
    reasoning,
    tools,
    characters,
    estimatedTokens: Math.ceil(characters / CHARACTERS_PER_TOKEN),
  };
}

const NUMBER = new Intl.NumberFormat("en-US");

/** A count as the tabs write it: grouped, in the interface's English. */
export function counted(value: number): string {
  return NUMBER.format(value);
}
