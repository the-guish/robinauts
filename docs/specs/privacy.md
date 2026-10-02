# Privacy: who sees what

Robinauts holds what people say to an assistant at work. Goal 3 keeps that
on the company's servers; this document says who, inside them, sees what.

## Visibility

- A conversation is **private to its author** by default.
- **Share by link.** The author can create a link to a conversation. It is
  read-only, works only for people who can sign in to the deployment, and
  can be revoked. It shows the **conversation's visible thread, live**: it
  follows the author when they continue, edit or regenerate, and the
  interface says so when the link is created. What an edit or a
  regeneration discarded is shown to nobody
  ([data-model.md](../architecture/data-model.md)).
- **Projects.** Any user can create a project at any time and add people.
  - The creator is the owner. Owners add and remove members and can make
    another member an owner.
  - Every member sees every conversation in the project — the visible
    thread of each — and can start new ones there.
  - Only a conversation's author continues it. Another member who wants to
    carry it on forks it into a conversation of their own
    ([controller.md](../architecture/controller.md)).

## Administrators

- An admin sees the metadata of every conversation: that it exists, who
  started it, when, which agent and models it used — and its token usage,
  once usage reporting exists.
- **An admin never sees message content** by virtue of being an admin. No
  API returns it, the purge and audit APIs included. An admin reads a
  conversation only as anyone does: as its author, as a project member, or
  through a share link.
- This is a rule about the platform's API. Whoever operates the database
  can read the database. The platform does not claim otherwise, and
  encrypting content with per-user keys is not part of this design.
- A test walks every route an admin can call and asserts that none returns
  another person's message content. The rule has to hold for every API
  added later.

## Retention and purge

- Deletion by the author is soft, with a fixed 30 days in the trash.
- **Edited-away and regenerated-away messages are retained** with their
  conversation, shown to no user, and readable by analytics alone. They
  leave with the conversation: deleting it, purging its owner and the
  retention period remove them with everything else.
- The operator may configure a retention period, after which
  conversations are deleted automatically. By default nothing expires.
- An admin can purge everything of a user. The purge shows the admin no
  content.
- Usage records hold no content. They outlive the conversation they refer
  to, and they keep their reference to the user even after a purge. A
  company subject to erasure requests has to weigh this.

## People who leave

Someone who can no longer sign in, or who was purged:

- their conversations in projects stay visible to the members, marked with
  the former author; they can be forked, not continued;
- their private conversations become unreachable, and fall under the
  retention period;
- a project left without an owner passes to its longest-standing member;
- a purge removes everything of that person, project conversations
  included.

## Audit

- An append-only log of security and administrative events: sign-ins and
  refused sign-ins, sign-outs, share links created and revoked, project
  membership changes, deletions, restores, purges, retention runs, and
  every admin view of other people's metadata.
- It never contains message content.
- Admins read it through the API, which can export it for a company's
  SIEM.

## Where it is enforced

Authorization here is mostly ownership and membership, not roles. It is
decided in the application layer and proven with in-memory fakes
([layout.md](../layout.md)); routes only declare the permission they need
([sign-in.md](sign-in.md)).

## Open

- With no retention period configured, a leaver's private conversations
  stay in the database until an admin purges them.
