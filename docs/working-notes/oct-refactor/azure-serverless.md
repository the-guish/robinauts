# Robinauts on Azure, serverless, paid by consumption

Read `aws-serverless.md` first. It is the reference: the rules the store port follows,
the logical model, the turn dispatcher, leases, cancel through the store, the
documents, and why. This note lists only what differs on Azure. Anything not
mentioned here applies unchanged.

As with AWS, **nothing in `open-shipyards/robinauts` depends on Azure**. No Azure
library is in this repository, and none will be. Everything Azure-specific named below
belongs to an integration in a repository of its own, built on the extension points
the reference describes. The one exception is not a library: the owner's id on
session operations is a change to this repository's port, and it is vendor-neutral.

## The products

| the AWS reference | on Azure |
|---|---|
| DynamoDB, one table | **Cosmos DB for NoSQL, serverless**, one container with hierarchical partition keys |
| S3 for large documents | Blob Storage |
| CloudFront + S3 for the UI | **none**: web serves the UI, as the wheel already does |
| Lambda + Web Adapter for web | **Container Apps**, consumption plan, `robinauts start` as it is |
| Lambda async invoke + worker | **none**: the turn stays in web's replica, which drains before stopping |
| EventBridge Scheduler | a Container Apps job on a cron schedule |
| SSM Parameter Store | Key Vault, referenced by the app's secrets |
| ECR | GitHub Container Registry (see fixed costs) |

There is no PostgreSQL billed by consumption on Azure. Flexible Server has no
serverless tier; even its smallest burstable size is billed by the hour. Azure SQL
serverless is not PostgreSQL. Cosmos DB is the fit, as DynamoDB is on AWS.

## Cosmos DB

One container. The partition key is hierarchical: `/owner_id`, then `/session_id`.
Users go in a second container partitioned by `/id`.

| record | partition key | `id` |
|---|---|---|
| user | `<provider>|<subject>`, escaped. The user is stored in its identity item | same |
| session | `[owner, session]` | `session` |
| message | `[owner, session]` | `msg:<id>` |
| turn | `[owner, session]` | `turn:<id>` |
| turn event | `[owner, session]` | `evt:<turn>:<position, zero-padded>` |

- **The owner is the first level of the key, which changes the port.** A point read
  needs the full partition key, so the session operations take the owner's id as well
  as the session's: `get_session(owner_id, session_id)` and so on. The controller
  always has the user, since every operation is asked for one. A session that isn't
  the user's is then simply not found, which is what `SessionNotFoundError` already
  promises. DynamoDB and PostgreSQL take the extra argument and ignore it, or use it
  as a check. Decide this in block 5, because it is the one change to the reference
  port that Azure forces.
- **Listing is a query on the owner prefix.** `WHERE owner_id = @o AND kind = 'session'
  AND NOT IS_DEFINED(deleted_at) ORDER BY updated_at DESC, id DESC` reaches only that
  owner's partitions. It needs a composite index on `(updated_at desc, id desc)`. The
  cursor is the logical key, as on AWS, not Cosmos's continuation token. Cosmos has no
  sparse index, so the filter on `deleted_at` does that job.
- **`start_turn` is a transactional batch.** A batch is atomic only within one full
  partition key, and a session's records all share `[owner, session]`. The batch
  creates an item with the fixed id `active`, which fails with 409 if it exists,
  with the question and the turn. `end_turn` deletes it in the batch that ends the
  turn.
  `ensure_user` is one `create`, with a 409 meaning "read it", as on Firestore.
- **A logical partition holds at most 20 GB**, which applies to each
  `[owner, session]`. Hierarchical keys let an owner exceed it in total. A batch or a
  request is at most 2 MB.
- **An item is at most 2 MB.** That is the most generous of the three stores, so the
  threshold for moving a document to Blob Storage is about 1.5 MB. It is the same
  mechanism as on AWS.
- **Exclude the document from indexing.** Cosmos indexes every path by default, and
  each indexed property adds request units (RUs) to every write. Set the indexing
  policy to exclude `/document/?`; it is never queried.
- **TTL is per item.** Turn events carry `ttl` in seconds, and Cosmos deletes them in
  the background. The other items carry none.
- **Waiting is a poll, as on AWS.** The change feed is a pull model, read by a
  processor or a Functions trigger; it doesn't push to one waiting request. A query
  scoped to one partition for the events after `after` costs a few RUs.
- **The purge** queries the partition `[owner, session]` and deletes what it finds.
  Cosmos can delete a whole logical partition by key, but that was in preview when
  this was written; check whether it is available for serverless containers.
- **The driver** would be `azure-cosmos` (MIT), which has an async client, in the
  external package. Its tests would use the Cosmos DB emulator and run the store's
  contract suite imported from this repository.
- **Engine memory.** The Pydantic AI engine's message list would be stored as items in
  its own container. For LangGraph, community Cosmos savers exist; check the licence
  and the maintenance, or write one against `BaseCheckpointSaver`. Both are handed to
  the engines through the supplied-storage kind of stage three.

## Compute

- **One Container App serves the UI and the API**, as on Google Cloud: the wheel's
  `robinauts start` in a container, with no adapter. It uses the consumption plan with
  minimum replicas at 0. It is billed per vCPU-second and GiB-second while replicas
  run, plus per request, after a monthly free allowance. Custom domains with managed
  certificates are free.
- **The turn stays in the web replica, which drains.** Azure has no equivalent to
  Lambda's async invoke that is both prompt and billed by consumption:
  - Container Apps jobs triggered from a queue start a container per turn, after a
    KEDA polling interval, which means seconds before the first token.
  - Functions Flex Consumption has reported failures of queue triggers to scale from
    zero.
  - Functions HTTP responses are cut at 230 s.

  Container Apps, on the other hand, does not throttle CPU outside requests, and gives
  a replica up to `terminationGracePeriodSeconds` (600 s at most) after `SIGTERM`. So:
  - the turn dispatcher is the in-process one, an asyncio task, as today;
  - the scale rule's cooldown is longer than the turn bound (120 s), so a replica that
    just started a turn is not scaled in under it;
  - the grace period is longer than the turn bound, and `controller.close` **waits for
    its turns to end, up to the grace period, instead of cancelling them**. That is
    the one change to the controller's lifecycle Azure asks for. The reference port
    still holds: a replica that dies anyway leaves a lease that runs out, and the turn
    is ended as `interrupted`.
- **Cancel still goes through the store.** With several replicas, the cancel request
  may reach one replica while the turn runs on another.
- **Streams are cut at 240 s by default**, the ingress request timeout. That is longer
  than one turn, so a stream rarely meets it, and re-attaching covers it when it does.
  The idle timeout is 4 minutes, and the keep-alives stay well inside it. Raising the
  request timeout is an environment ingress setting; check whether a consumption-only
  environment allows it before relying on it.
- **Housekeeping** is a Container Apps job on a cron schedule, billed for the seconds
  it runs.
- **No VNet.** A consumption environment without VNet integration reaches the internet
  directly. A NAT gateway is billed by the hour, so avoid it unless the MCP servers
  are private.

## Fixed costs that remain

- **Container registry.** Azure Container Registry has no tier billed by consumption;
  Basic is a fixed monthly fee. Pulling the image from GitHub Container Registry,
  where the project is already hosted, avoids it.
- **Key Vault** is billed per operation, with no monthly charge per secret, so it is
  already billed by consumption. Container Apps resolves the references when a replica
  starts. A customer-managed key would add a fixed fee, as on AWS.
- **Log Analytics** is billed per GB ingested, which is consumption, but its default
  retention and any commitment tier are worth checking.

## Sources checked

- [Container Apps ingress and its timeouts](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview)
- [Functions hosting options and the HTTP limit](https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale),
  [Flex Consumption](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan),
  and [a queue trigger that doesn't scale from zero](https://learn.microsoft.com/en-us/answers/questions/5768499/queue-triggered-function-on-flex-consumption-wont)
- [Cosmos DB limits](https://learn.microsoft.com/en-us/azure/cosmos-db/concepts-limits)
  and [hierarchical partition keys](https://learn.microsoft.com/en-us/azure/cosmos-db/hierarchical-partition-keys)
