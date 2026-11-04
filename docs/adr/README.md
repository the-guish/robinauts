# Architecture decision records

An ADR records a decision that needed a discussion. It keeps the options on
the table, and why the decision went the way it did. An ADR is history. The
[specs](../specs/core.md) describe the system. Most of the system needed no
ADR.

Each decision has one file, numbered once and never renumbered. A changed
decision gets a new ADR, which supersedes the old one. A superseded ADR is
history only. Skip it unless you need that history.

| # | Decision |
|---|---|
| [0001](0001-chat-ui-assistant-ui-with-tailwind.md) | Chat UI: assistant-ui styled components on Tailwind, behind an explicit seam |
| [0002](0002-conversation-persistence.md) | Superseded by 0005. The platform owns the conversation record; agent frameworks are stateless per turn |
| [0003](0003-single-visible-thread.md) | One visible thread: the tree stays in storage, users see the newest path |
| [0004](0004-context-management-in-the-adapter.md) | Superseded by 0005. Context management belongs to the agent adapter |
| [0005](0005-the-framework-owns-the-loop-and-the-memory.md) | The framework owns the loop, the context and the model's memory; the platform keeps the transcript. Supersedes 0002 and 0004 |
