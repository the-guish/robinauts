# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The to-do list, kept in one JSON file.

Every operation reads the file, changes the list and writes it back whole, under one lock:
two calls at once cannot lose each other's change. A write goes to a file beside the list
first and then replaces it, so a crash leaves the old list or the new one, never half of one.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_TEXT = 500
"""The longest text a to-do may have, in characters."""

MAX_ITEMS = 1000
"""The most to-dos the list holds."""


class StoreError(ValueError):
    """A change the list refuses, with a sentence that says why."""


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    text: str
    done: bool


def default_path() -> Path:
    """Where the list lives when nothing names a file: the user's data directory."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "robinauts-to-do-list" / "todos.json"


class TodoStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def check(self) -> None:
        """Read the file once, so that a file that is not a list fails at start-up."""
        with self._lock:
            self._read()

    def items(self) -> list[Item]:
        with self._lock:
            return self._read()[1]

    def add(self, text: str) -> list[Item]:
        text = " ".join(str(text).split())
        if not text:
            raise StoreError("a to-do needs some text")
        if len(text) > MAX_TEXT:
            raise StoreError(f"a to-do is at most {MAX_TEXT} characters")
        with self._lock:
            next_id, items = self._read()
            if len(items) >= MAX_ITEMS:
                raise StoreError(f"the list is full: it holds at most {MAX_ITEMS} to-dos")
            items.append(Item(str(next_id), text, False))
            self._write(next_id + 1, items)
            return items

    def toggle(self, item_id: str) -> list[Item]:
        with self._lock:
            next_id, items = self._read()
            index = self._find(items, item_id)
            item = items[index]
            items[index] = Item(item.id, item.text, not item.done)
            self._write(next_id, items)
            return items

    def remove(self, item_id: str) -> list[Item]:
        with self._lock:
            next_id, items = self._read()
            del items[self._find(items, item_id)]
            self._write(next_id, items)
            return items

    @staticmethod
    def _find(items: list[Item], item_id: str) -> int:
        for index, item in enumerate(items):
            if item.id == str(item_id):
                return index
        raise StoreError(f"there is no to-do with id {item_id!r}")

    def _read(self) -> tuple[int, list[Item]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 1, []
        try:
            raw = json.loads(text)
            items = [
                Item(str(entry["id"]), str(entry["text"]), bool(entry["done"]))
                for entry in raw["items"]
            ]
            next_id = int(raw["next_id"])
        except (TypeError, KeyError, ValueError) as error:
            raise StoreError(f"{self.path} is not a to-do list: {error!r}") from None
        return next_id, items

    def _write(self, next_id: int, items: list[Item]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"next_id": next_id, "items": [asdict(item) for item in items]}
        handle, name = tempfile.mkstemp(dir=self.path.parent, prefix=".todos-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(name, self.path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
