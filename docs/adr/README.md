# Architecture decision records

An ADR is written for a decision that needed a discussion: it keeps the
options that were on the table and why it went the way it did. It is
history, not a description of the system — that is what
[specs/](../specs/core.md) is for. Most of the system was specified without
needing one.

One file per decision, numbered, never renumbered. A decision that changes
gets a new ADR that supersedes the old one.

| # | Decision |
|---|---|
| [0001](0001-chat-ui-assistant-ui-with-tailwind.md) | Chat UI: assistant-ui styled components on Tailwind, behind an explicit seam |
| [0002](0002-conversation-persistence.md) | The platform owns the conversation record; agent frameworks are stateless per turn |
| [0003](0003-single-visible-thread.md) | One visible thread: the tree stays in storage, users see the newest path |
| [0004](0004-context-management-in-the-adapter.md) | Context management belongs to the agent adapter; supersedes that part of 0002 |
