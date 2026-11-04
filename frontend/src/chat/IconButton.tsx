// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * A button that is an icon: Copy, Refresh, Resume, More, Edit, scroll to
 * bottom, and a code block's Copy code.
 *
 * Its name is `label`, given twice: as `aria-label`, which is what is read and
 * what a test or the end-to-end suite finds it by, and as `title`, which is
 * the tip a pointer shows. The icon inside is decorative (`src/icons`).
 */
import type { KeyboardEvent, ReactNode, Ref } from "react";

export function IconButton({
  label,
  onClick,
  disabled = false,
  className,
  children,
  ref,
  onKeyDown,
  popup,
}: Readonly<{
  label: string;
  onClick: () => void;
  disabled?: boolean;
  className?: string | undefined;
  children: ReactNode;
  ref?: Ref<HTMLButtonElement>;
  onKeyDown?: (event: KeyboardEvent<HTMLButtonElement>) => void;
  /** The menu it opens, for a button that opens one: its id, and whether. */
  popup?: { id: string; open: boolean };
}>) {
  return (
    <button
      ref={ref}
      type="button"
      className={
        className === undefined
          ? "chat-icon-button"
          : `chat-icon-button ${className}`
      }
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      onKeyDown={onKeyDown}
      aria-haspopup={popup === undefined ? undefined : "menu"}
      aria-expanded={popup?.open}
      aria-controls={popup?.open === true ? popup.id : undefined}
    >
      {children}
    </button>
  );
}
