# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
"""The store: the list in its JSON file."""

import json
import threading

import pytest

from to_do_list.store import MAX_ITEMS, MAX_TEXT, Item, StoreError, TodoStore, default_path


@pytest.fixture
def store(tmp_path):
    return TodoStore(tmp_path / "data" / "todos.json")


def test_a_list_that_was_never_written_is_empty(store):
    assert store.items() == []
    assert not store.path.exists()


def test_added_items_get_ids_in_order_and_start_not_done(store):
    store.add("milk")
    assert store.add("bread") == [Item("1", "milk", False), Item("2", "bread", False)]


def test_the_list_is_kept_in_the_file(store):
    store.add("milk")
    assert TodoStore(store.path).items() == [Item("1", "milk", False)]
    assert json.loads(store.path.read_text(encoding="utf-8")) == {
        "next_id": 2,
        "items": [{"id": "1", "text": "milk", "done": False}],
    }


def test_toggle_flips_done_and_back(store):
    store.add("milk")
    assert store.toggle("1") == [Item("1", "milk", True)]
    assert store.toggle("1") == [Item("1", "milk", False)]


def test_remove_takes_the_item_out_and_its_id_is_not_reused(store):
    store.add("milk")
    store.add("bread")
    assert store.remove("1") == [Item("2", "bread", False)]
    assert store.add("eggs")[-1] == Item("3", "eggs", False)


@pytest.mark.parametrize("operation", ["toggle", "remove"])
def test_an_unknown_id_is_refused_and_nothing_changes(store, operation):
    store.add("milk")
    before = store.path.read_bytes()
    with pytest.raises(StoreError, match="no to-do with id '9'"):
        getattr(store, operation)("9")
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    ("text", "kept"),
    [("  buy   milk \n", "buy milk"), ("x" * MAX_TEXT, "x" * MAX_TEXT), ("ñandú 🦤", "ñandú 🦤")],
)
def test_text_is_kept_with_its_spaces_squeezed(store, text, kept):
    assert store.add(text)[0].text == kept


@pytest.mark.parametrize(("text", "why"), [("", "needs some text"), ("  \t\n", "needs some text")])
def test_empty_text_is_refused(store, text, why):
    with pytest.raises(StoreError, match=why):
        store.add(text)


def test_text_too_long_is_refused(store):
    with pytest.raises(StoreError, match=f"at most {MAX_TEXT} characters"):
        store.add("x" * (MAX_TEXT + 1))


def test_a_full_list_refuses_one_more(store):
    store.path.parent.mkdir(parents=True)
    items = [{"id": str(n), "text": "t", "done": False} for n in range(MAX_ITEMS)]
    store.path.write_text(json.dumps({"next_id": MAX_ITEMS, "items": items}), encoding="utf-8")
    with pytest.raises(StoreError, match="the list is full"):
        store.add("one more")


@pytest.mark.parametrize("content", ["not json at all", "[]", '{"items": []}', '{"next_id": 1}'])
def test_a_file_that_is_not_a_list_is_refused(store, content):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")
    with pytest.raises(StoreError):
        store.check()


def test_writes_at_once_lose_nothing(store):
    threads = [threading.Thread(target=store.add, args=(f"item {n}",)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(int(item.id) for item in store.items()) == list(range(1, 21))


def test_no_temporary_file_is_left_beside_the_list(store):
    store.add("milk")
    store.toggle("1")
    assert [path.name for path in store.path.parent.iterdir()] == ["todos.json"]


@pytest.mark.parametrize(
    ("xdg", "expected"),
    [("/srv/data", "/srv/data/robinauts-to-do-list/todos.json"), ("", None)],
)
def test_the_default_file_is_in_the_data_directory(monkeypatch, tmp_path, xdg, expected):
    monkeypatch.setenv("XDG_DATA_HOME", xdg)
    monkeypatch.setenv("HOME", str(tmp_path))
    fallback = tmp_path / ".local/share/robinauts-to-do-list/todos.json"
    assert str(default_path()) == (expected or str(fallback))
