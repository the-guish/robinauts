// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * A button that is an icon: Copy, Refresh, Resume, Edit, scroll to bottom.
 *
 * Its name is `label`, given twice: as `aria-label`, which is what is read and
 * what a test or the end-to-end suite finds it by, and as `title`, which is
 * the tip a pointer shows. The icon inside is decorative (`src/icons`).
 */
import type { ReactNode } from "react";

export function IconButton({
  label,
  onClick,
  disabled = false,
  className,
  children,
}: Readonly<{
  label: string;
  onClick: () => void;
  disabled?: boolean;
  className?: string | undefined;
  children: ReactNode;
}>) {
  return (
    <button
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
    >
      {children}
    </button>
  );
}
