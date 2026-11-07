# Files

## What it is

An agent keeps files for a person: notes it takes across conversations, a
list it maintains, a draft the person comes back to. The files live on a file
system the operator provides, in a **folder that is that person's with that
agent** and nobody else's, and the model reaches them through a small set of
tools whose reach is that folder alone.

There are two sets of tools:

- **Basic edit**, the tool `basic_edit`: eight functions, each on one file
  or one folder at a time, named by its path. Always bound to the folder.
  This is what ships.
- **Full edit**, the tool `full_edit`: what coding work needs. Planned, not
  specified; it will run only where the operator says so, with an
  environment variable on the worker's process
  ([Full edit](#full-edit-planned)).

Who owns the files, where they are kept and that nothing outside the folder
can be named are decided once, outside the tools: the worker binds the folder
to the turn's owner and agent before a tool sees it
([architecture/rules.md](../architecture/rules.md)), and a tool asks the
folder for a path and nothing else.

## Words

- **Files root**: where every folder is kept: a directory on the server
  today, a bucket or a drive when an adapter for one exists.
- **Folder**: the one place an owner with an agent has: `<root>/<owner
  id>/<agent id>/`.
- **Path**: where something is inside a folder, written with `/` between
  names, relative to the folder: `notes/today.md`. Never absolute, never
  with `.` or `..` in it.

## The folder

- A folder is `<root>/<owner id>/<agent id>/`. The owner's id is the
  platform's own for that person, the `users` table's, which names nobody
  to someone reading the disk. The agent's id is its configuration id.
- Two agents of one person have two folders; one agent with two people has
  two folders. Neither sees the other's, and none sees the root.
- A folder is made the first time something is written in it. Until then it
  lists as empty.
- **Nothing outside the folder can be named.** A path the model gives is
  checked by one function before any storage sees it
  (`filesystems.contract.domain.relative_path`): it is relative, `/`
  separates its names, no name is empty, `.` or `..`, and no name holds a
  `/`, a backslash or a character that is not printable. What passes is
  handed to the storage as names, never as text to interpret. The one thing
  a path cannot say and a disk can, a link inside the folder that leads out,
  the local adapter refuses on every operation, on sources and targets
  alike. The tools cannot make a link.
- The files are **outside the database**. A backup of the deployment is the
  database and the files root. Deleting a person's conversations does not
  touch their folder; purging a person
  ([privacy.md](privacy.md)) will have to, and does not yet.
- The owner's id is stable on PostgreSQL. In the local development mode
  without a database the user is made anew at every start, and so is the
  folder; the demo keeps its database and so keeps the folders. A database
  recreated before the first release gives people new ids
  ([CONTRIBUTING.md](../../CONTRIBUTING.md)): the folders under the old
  ids stay on disk for the operator to move or remove.
- A write replaces a file whole and atomically: a reader sees the file
  before or after, never half written. Two turns writing the same file at
  once leave the later one.

## Basic edit

Eight functions. Each takes one path, or two, and nothing that could name
more than one thing: no pattern, no search, no recursion past the depth
asked. Every path is relative to the folder. Files are UTF-8 text; a file
that is not is refused rather than read wrong, and a line ends at `\n`, so
the line numbers these tools say are the ones a person's editor says.

| function | what it does |
|---|---|
| `read_file` | the whole file, the lines `from_line` to `to_line` (1-based, both included), or the last `tail` lines; says how many lines the file has and which came back, and `content` is exactly the text of those lines |
| `list_folder` | what a folder holds, by name, each with its path, kind, size and last change; `depth` 1 is that folder alone, up to 5 goes into its folders, each folder's entries right after it |
| `write_file` | create a file, or replace one whole, with `content`; the folders on the way are made |
| `replace_in_file` | every occurrence of `old`, the characters themselves and not a pattern, becomes `new`; refused when `old` is nowhere in the file |
| `replace_lines` | the lines `from_line` to `to_line` become `content`, any number of lines or none; `to_line` = `from_line` − 1 inserts before `from_line`, and `from_line` one past the last line appends |
| `move_file` | one file to a path with nothing at it; the folders on the way are made |
| `copy_file` | one file to a path with nothing at it |
| `delete_file` | one file, or one folder with nothing left in it |

The bounds, constants rather than settings:

| bound | value |
|---|---|
| a file these tools read, write or edit | 1 MiB |
| one read | 1000 lines or 50,000 characters, cut at a whole line; the answer says `truncated` and `next_line` |
| one listing | 500 entries, 5 levels deep |
| a path | 1024 characters; a name 255 bytes of UTF-8 |

A result that would pass the file bound is refused before it is built: a
replacement's size is known from the counts, and a text the model sends is
bounded on its own, so no call can ask the worker for more memory than a
few files' worth.

Refusals are failed tool results in words the model can act on: the path
that was wrong and why, the file that is not there, the folder that is not
empty, the text that is nowhere in the file. None names the root, the owner
or anything outside the folder.

The engines may call a tool again on a resumed turn
([agent-engines.md](agent-engines.md), "Resumed"). `write_file`,
`move_file`, `copy_file` and `delete_file` repeated do the same thing or
refuse; `replace_in_file` repeated finds its text gone and refuses;
`replace_lines` repeated edits the lines it already edited, which is why its
description tells the model to read the lines first.

The functions are offered to the model under those names, beside the tools
of the agent's MCP servers. A server that offers a tool of the same name is
the operator's to `exclude` ([operations.md](operations.md)).

## Configuration

```toml
# Optional, here with its defaults.
[files]
kind = "local"
path = "~/robinauts"
```

- `kind` is the storage: `local` is a directory on the server, the only
  kind today. `path` is its root; `~` is the home of the user the server
  runs as. A deployment names a directory of its own, such as
  `/var/lib/robinauts/files`.
- An agent keeps files by naming the tool: `tools = ["basic_edit"]`.
  Nothing is built, and nothing touched on disk, unless an agent does.
- Each worker makes the root ready when it starts, mode `0700` when it
  makes it, as it is when the operator made it. A root that cannot be made,
  is not a folder or cannot be written stops the worker, and with it the
  start, saying so.
- An agent that keeps files needs an id that can name a folder, which is
  the rule a path's names follow. One that cannot is refused at start-up.
- The files root is a path and not a secret, so it is in the file; a
  bucket's or a drive's credential, when there is one, will be the name of
  an environment variable, as every secret is.

## Backends

The file system is a port with two sides
(`robinauts/filesystems/contract/`):

- `FileSystem`: `setup`, which makes the root ready and is safe to repeat,
  and `folder(owner, agent)`, which binds the folder of two plain names and
  makes nothing.
- `Folder`: `stat`, `list`, `read` within a size, `write` whole, `move`,
  `copy`, `delete` of one file or one empty folder. Every one takes a path
  as its names, already checked, and raises the contract's refusals.

An adapter is a package beside `local/`, named by `cli.composition` alone
from the `kind` in `[files]`, and the tools and the worker never see it. It
keeps the contract by passing the contract suite
(`backend/tests/util/contracts/folder.py`), which is what the local adapter
and the in-memory folder of the tests pass. What an adapter for a bucket
would do: the folder is a key prefix, `<owner>/<agent>/`, a path is the
rest of the key, folders are what the prefixes of the keys say, and
deleting an empty folder removes nothing. What one for a drive would do:
the folder is a directory of the drive's, found or made by name under a
root the operator names, and paths are resolved name by name. Neither needs
anything of the tools: the editing logic sits above the port and reads and
writes whole files.

## Full edit (planned)

The second set is for coding: a working tree, search, patterns, several
files at once, whatever the usual coding tools need. None of it is
specified yet. What is settled:

- It is a tool of its own, `full_edit`, named by an agent like any other,
  and never the same tool as `basic_edit` grown wider.
- It is built only where the worker's process has
  `ROBINAUTS_ALLOW_FULL_EDIT` set. Where it is not set, the tool is not in
  the build, and an agent that names it is refused at start-up as naming a
  tool the build does not have. The gate is the operator's, per machine,
  and not a line in a file that travels with the configuration.
- Its reach, and whether it is a folder of the files root or something
  else, is decided when it is specified.

## Open

- Quotas: how much a folder may hold, and what happens when it is full.
- A person's view of their folders in the interface: read, download,
  upload, delete.
- Purging a person removes their folders with everything else.
- Folders shared by a team, with projects ([privacy.md](privacy.md)).
- Files that are not text: images and documents the model reads or writes.
- Naming a folder by the person's identity at the provider rather than by
  their id, so that it survives a database made anew.
