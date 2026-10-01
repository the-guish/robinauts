# Robinauts on Google Cloud, serverless, paid by consumption

Read `aws-serverless.md` first. It is the reference: the rules the store port follows,
the logical model, the turn dispatcher, leases, cancel through the store, the
documents, and why. This note lists only what differs on Google Cloud. Anything not
mentioned here applies unchanged.

As with AWS, **nothing in `open-shipyards/robinauts` depends on Google Cloud**. No
Google library is in this repository, and none will be. Everything Google-specific
named below belongs to an integration in a repository of its own, built on the
extension points the reference describes.

## The products

| the AWS reference | on Google Cloud |
|---|---|
| DynamoDB, one table | **Firestore** (native mode), collections and subcollections |
| S3 for large documents | Cloud Storage |
| CloudFront + S3 for the UI | **none**: web serves the UI, as the wheel already does |
| Lambda + Web Adapter for web | **Cloud Run** service, request-based billing, `robinauts start` as it is |
| Lambda async invoke + worker | **Cloud Tasks** → a private Cloud Run service |
| EventBridge Scheduler | Cloud Scheduler → the worker |
| SSM Parameter Store | Secret Manager, as environment variables |
| ECR | Artifact Registry |

There is no PostgreSQL billed by consumption on Google Cloud. Cloud SQL and AlloyDB
are billed by the hour. Spanner's PostgreSQL interface is billed by provisioned
capacity. Firestore is the only fit, as DynamoDB is on AWS.

## Firestore

The logical model maps onto paths instead of `PK`/`SK`:

| record | path |
|---|---|
| user | `identities/<provider>|<subject>`, escaped. The user is stored in its identity document |
| session | `sessions/<id>` |
| message | `sessions/<id>/messages/<id>` |
| turn | `sessions/<id>/turns/<id>` |
| turn event | `sessions/<id>/turns/<turn>/events/<position, zero-padded>` |

- **`ensure_user` is one document.** The user is stored in its identity document, so
  `create()` either succeeds or fails because the document exists, and then it is
  read. The port only looks users up by identity. The same simplification would work
  in DynamoDB.
- **The listing hides deleted sessions with a field that is present only while the
  session is visible.** Call it `list_owner`: set while visible, deleted along with
  setting `deleted_at`. A Firestore query on a field skips documents that don't have
  it, which plays the role of the sparse GSI. The composite index is `list_owner`
  ascending, `updated_at` descending, `__name__` descending, and the cursor is
  `start_after(updated_at, id)`.
- **`start_turn` and `end_turn` are transactions.** The server client libraries lock
  the documents they read, so "read the session's `active_turn_id`, write it and the
  turn" is safe as written.
- **Deleting a document does not delete its subcollections.** The purge is a
  `recursive_delete` of `sessions/<id>`. As with DynamoDB, "hide, then purge" is the
  only shape that works.
- **A document is at most 1 MiB**, so the threshold for moving a document to Cloud
  Storage is higher than on DynamoDB (about 900 KB). It is the same mechanism.
- **Exempt the document field from indexing.** Firestore indexes every field by
  default. The document is never queried, and an indexed string over 1,500 bytes is
  wasted storage and write cost. Exempt `document` with a single-field index exemption.
- **Write limits don't affect us.** The soft limit is one sustained write a second per
  document. A turn writes its session at start and end and its own document every
  lease tick, and each event is a document of its own.
- **TTL works on events, but it isn't free.** A TTL policy on `expires_at` deletes
  expired events within about a day, and each deletion is billed as an ordinary
  delete.
- **Firestore can push to a waiting watcher.** It is the only one of the three
  serverless stores that can: a snapshot listener on "events of this turn after
  `after`" receives new events as they are written, and each delivered document is
  billed as one read. The Python `AsyncClient` has no `on_snapshot`, though. The
  synchronous client's listener runs its callback on a thread, which would hand
  events to the loop with `call_soon_threadsafe`. Start by polling, as on AWS. The
  listener is an optimisation inside `wait_for_events`, invisible to the port.
- **The driver** would be `google-cloud-firestore` (Apache-2.0), in the external
  package. Its tests would use the Firestore emulator and run the store's contract
  suite imported from this repository.
- **Engine memory.** The Pydantic AI engine's message list would be stored as
  documents in its own collection. For LangGraph there is no first-party Firestore
  saver; community ones exist, or one can be written against `BaseCheckpointSaver`.
  Either way, check the licence and the maintenance. Both are handed to the engines
  through the supplied-storage kind.

## Compute

- **One Cloud Run service serves the UI and the API.** The wheel already carries the
  built interface and web already mounts it, so there is no CDN and the app is
  same-origin by construction. Static files cost a little request time, which is
  acceptable until it isn't.
- **Request-based billing is billed per instance, not per stream.** An instance is
  billed while it has at least one request open. With concurrency at 80 or so, many
  SSE streams share one instance's billed seconds. Waiting watchers are a per-stream
  cost on Lambda; here they are mostly shared. That narrows the case for pushing
  events over a WebSocket.
- **A request may last up to 60 minutes**, so streams are cut much less often than on
  Lambda. Re-attaching still covers it.
- **The turn can't stay in the web process.** With request-based billing, CPU is
  throttled once the response has ended, so a turn whose watcher left would stall.
  Instance-based billing avoids that but bills the idle tail of every instance, and
  Cloud Run allows only 10 s after `SIGTERM`, too little to finish a turn.
- **Cloud Tasks dispatches the turn.** Web creates a task carrying the two ids. Cloud
  Tasks POSTs it to the worker service with an OIDC token, and the turn runs for the
  length of that request. Set `max_attempts` to 1 (the same reason the AWS note sets
  retries to zero). The dispatch deadline can be up to 30 minutes, longer than the
  120 s turn bound. Tasks are billed per operation, and the first million a month are
  free.
- **The worker is the same image deployed as a second service.** It allows only
  authenticated callers, has more memory, and imports the engines. Web imports none.
- **No VPC.** Cloud Run reaches the internet directly. A Serverless VPC Access
  connector runs instances all the time, and Cloud NAT is billed by the hour. Avoid
  both unless the MCP servers are private.

## Fixed costs that remain

- **A custom domain in production.** Cloud Run's own `run.app` URL is free and ready
  for production. Its domain mappings are in preview, not supported for production,
  and limited to some regions. Firebase Hosting gives a free domain but cuts every
  request to Cloud Run at 60 s, so streams would re-attach every minute. The
  supported route is a global external Application Load Balancer, whose forwarding
  rule is billed by the hour. That is the one fixed cost a production deployment on
  GCP would carry.
- **Cloud Scheduler** is free for three jobs per billing account, then a small amount
  per job per month.
- **Secret Manager** bills a small amount per active secret version per month, after a
  free allowance. Environment variables set at deploy time avoid it, if the
  deployment pipeline is trusted with the values.

## Sources checked

- [Cloud Run custom domain mappings](https://docs.cloud.google.com/run/docs/mapping-custom-domains)
- [Firebase Hosting with Cloud Run, and its 60 s limit](https://firebase.google.com/docs/hosting/cloud-run)
- [Firestore TTL policies](https://firebase.google.com/docs/firestore/ttl)
- [Real-time listeners](https://firebase.google.com/docs/firestore/query-data/listen),
  and [no `on_snapshot` on the async client](https://github.com/googleapis/python-firestore/issues/301)
