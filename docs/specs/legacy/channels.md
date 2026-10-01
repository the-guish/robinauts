# Delivery channels

The same agents, the same conversations and the same rules are reachable
from more than one place. The web UI is the first channel, not the only
one.

## One API

- There is **one API**. Every channel is a client of it; none has an API of
  its own, and the backend holds no channel-specific logic.
- The web UI uses nothing that another client could not use. Whatever it
  can do — start a conversation with an agent, send a message, watch or
  fetch the answer, browse history, share, work in projects — a mobile
  application or a bridge can do through the same routes.
- A conversation does not belong to a channel. One started in the web UI
  can be continued from a mobile application, and the reverse. The
  conversation records the channel each message came from.
- Privacy, roles, limits and audit apply identically whatever the channel
  ([privacy.md](../privacy.md), [operations.md](../operations.md)).

## Channels

| channel | status |
|---|---|
| Web UI | first version |
| Mobile application | planned |
| Slack, through a bridge | planned |

- **A bridge** is a separate component that translates between a
  messaging platform's API and the Robinauts API: a Slack bridge built on
  Slack's Bolt framework receives Slack events, calls the same API as any
  client, and posts the answers back to Slack. It is a client of the
  platform, not a part of the backend, and it is swappable like any other
  component.
- A person reached through a bridge is the same user as in the web UI. How
  a bridge establishes who is speaking is specified with the bridge.

## Clients that cannot stream

- The web UI watches a run as a live stream ([wire.md](../wire.md)). Some
  clients cannot hold a stream, or have no use for one — a Slack message
  is posted whole.
- Because a run is persisted and independent of the request that started
  it ([runs.md](runs.md)), a client does not have to stream: it can start
  a run and obtain the result when the run has finished.
- The non-streaming way to consume a run is **planned**, and specified
  with the first channel that needs it.

## What this asks of the API now

- Nothing in the API assumes a browser: no route depends on the UI
  library, on cookies being the only credential, or on the client holding
  a stream.
- Channels other than the browser need a credential that is not a session
  cookie. That arrives with API tokens, which are planned
  ([sign-in.md](../sign-in.md)).
- Message content is the platform's own format
  ([conversations.md](conversations.md)), not markup for one renderer: each
  channel renders it its own way.
