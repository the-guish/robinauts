// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * What a Copy button does: write to the clipboard, and say that it did for a
 * moment.
 *
 * One helper for every Copy of the chat -- an answer's and a code block's --
 * so that they behave alike. Where there is no clipboard (an insecure origin,
 * an old browser) or the browser refuses the write, nothing was copied and
 * nothing says that it was: there is nothing the reader could do about it,
 * and selecting the text still works. A second copy while the first still
 * shows starts the moment again, rather than ending on the first one's time.
 */
import { useCallback, useEffect, useRef, useState } from "react";

/** How long a Copy shows that it copied. */
export const COPIED_MS = 2000;

export function useCopied(): {
  copied: boolean;
  copy: (text: string) => void;
} {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // A tick left running would set the state of a button that has gone.
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );
  const copy = useCallback((text: string) => {
    const clipboard =
      typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (typeof clipboard?.writeText !== "function") return;
    clipboard.writeText(text).then(
      () => {
        if (timer.current !== null) clearTimeout(timer.current);
        setCopied(true);
        timer.current = setTimeout(() => {
          timer.current = null;
          setCopied(false);
        }, COPIED_MS);
      },
      () => undefined,
    );
  }, []);
  return { copied, copy };
}
