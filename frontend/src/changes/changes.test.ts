// SPDX-License-Identifier: Apache-2.0
// Copyright The Robinauts Authors
import { expect, test } from "vitest";

import { retryDelay } from "./changes";

test.each([
  [-1, 0],
  [0, 0],
  [1, 1_000],
  [2, 2_000],
  [5, 16_000],
  [6, 30_000],
  [50, 30_000],
])("after %i failures in a row the feed waits %i ms", (failures, wait) => {
  expect(retryDelay(failures)).toBe(wait);
});
