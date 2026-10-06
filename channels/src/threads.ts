// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * Which platform thread is which Robinauts conversation, kept in one JSON file.
 *
 * The deployment names a new conversation in the `X-Robinauts-Conversation-Id` header of the
 * stream that started it; the bridge sends that id as the AG-UI thread id of every later run
 * in the same platform thread. Losing the file loses nothing but the link: the next message of
 * a thread starts a new conversation, and the old one is still in the deployment.
 */

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

export interface Threads {
  get(key: string): string | undefined;
  set(key: string, conversation: string): void;
  delete(key: string): void;
}

export class ThreadFile implements Threads {
  private readonly path: string;
  private readonly links: Map<string, string>;

  constructor(path: string) {
    this.path = path;
    this.links = new Map(Object.entries(read(path)));
  }

  get(key: string): string | undefined {
    return this.links.get(key);
  }

  set(key: string, conversation: string): void {
    if (this.links.get(key) === conversation) return;
    this.links.set(key, conversation);
    this.save();
  }

  delete(key: string): void {
    if (this.links.delete(key)) this.save();
  }

  private save(): void {
    // Whole, then renamed over the old one: a crash half way leaves the last good file.
    mkdirSync(dirname(this.path), { recursive: true });
    const next = `${this.path}.next`;
    writeFileSync(
      next,
      JSON.stringify(Object.fromEntries(this.links), null, 2) + "\n",
    );
    renameSync(next, this.path);
  }
}

function read(path: string): Record<string, string> {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
  const parsed: unknown = JSON.parse(text);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(
      `${path} is not a JSON object of thread keys to conversation ids`,
    );
  }
  return Object.fromEntries(
    Object.entries(parsed).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
}
