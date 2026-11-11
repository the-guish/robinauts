// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors

/**
 * The icons: inline SVG, one component each, drawn from path data copied
 * from lucide (`./lucide.ts`, ISC, recorded in docs/legal/ip-clearance.md).
 *
 * The one icon set the interface uses (`docs/specs/frontend.md`), carried as
 * data rather than as a package. An icon is decorative unless given a
 * `title`: without one it is hidden from assistive technology and whatever
 * names the control it sits in -- its `aria-label` or its text -- is what is
 * read; with one it is an image with that name.
 *
 * Sized in CSS by the class the caller gives it, or by `size`. The defaults
 * are lucide's: a 24-unit viewBox, no fill, a 2-unit stroke in the current
 * colour with round caps and joins.
 */
import type { JSX, ReactNode, SVGProps } from "react";

import { LUCIDE, type IconNode } from "./lucide";

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, "children"> {
  /** Width and height, in pixels or any CSS length. Default 16. */
  size?: number | string;
  /** An accessible name, when the icon carries meaning of its own. */
  title?: string;
}

function icon(name: string): (props: Readonly<IconProps>) => ReactNode {
  const node: IconNode = LUCIDE[name] ?? [];
  function Icon({ size = 16, title, className, ...rest }: Readonly<IconProps>) {
    const classes = className === undefined ? "icon" : `icon ${className}`;
    return (
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 24 24"
        width={size}
        height={size}
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={classes}
        data-icon={name}
        focusable="false"
        {...(title === undefined
          ? { "aria-hidden": true }
          : { role: "img", "aria-label": title })}
        {...rest}
      >
        {title !== undefined && <title>{title}</title>}
        {node.map(([tag, attrs], at) => {
          const Tag = tag as keyof JSX.IntrinsicElements;
          return <Tag key={at} {...attrs} />;
        })}
      </svg>
    );
  }
  Icon.displayName = `Icon(${name})`;
  return Icon;
}

export const ArchiveIcon = icon("archive");
export const ArrowDownIcon = icon("arrow-down");
export const ArrowUpIcon = icon("arrow-up");
export const BrainIcon = icon("brain");
export const CheckIcon = icon("check");
export const ChevronDownIcon = icon("chevron-down");
export const CircleXIcon = icon("circle-x");
export const CopyIcon = icon("copy");
export const EllipsisIcon = icon("ellipsis");
export const LoaderIcon = icon("loader");
export const LogOutIcon = icon("log-out");
export const MenuIcon = icon("menu");
export const MonitorIcon = icon("monitor");
export const MoonIcon = icon("moon");
export const PanelLeftIcon = icon("panel-left");
export const PanelRightIcon = icon("panel-right");
export const PencilIcon = icon("pencil");
export const PlayIcon = icon("play");
export const PlusIcon = icon("plus");
export const RefreshCwIcon = icon("refresh-cw");
export const SquareIcon = icon("square");
export const SunIcon = icon("sun");
export const TrashIcon = icon("trash");
export const WorkflowIcon = icon("workflow");
export const XIcon = icon("x");
