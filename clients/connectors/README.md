# Connectors

Chat platforms reaching a Robinauts deployment: a person writes on Slack or
Telegram, a Robinauts agent answers there. The distribution
`robinauts-connectors`, apart from the server's `robinauts`; it talks to the
deployment over HTTP and imports nothing of it.

**A stub.** The classes and their contract are written; their behaviour raises
`NotImplementedError`. The plan is
[`docs/clients/connectors/working-notes/warm-up/plan.md`](../../docs/clients/connectors/working-notes/warm-up/plan.md),
and the reasoning [`background.md`](../../docs/clients/connectors/working-notes/warm-up/background.md).

## How it is meant to run

A platform either lets the connector open the connection, or calls it back.
Hence two commands, usually in two containers:

    pip install "robinauts-connectors[slack,telegram]"
    robinauts-connectors connect slack telegram           # no public URL, one replica

    pip install "robinauts-connectors[webhook,slack]"
    robinauts-connectors serve slack --port 8080           # public HTTPS, scales out

## Where to read

| module | what it holds |
|---|---|
| `connector.py` | **the contract**: `Connector`, `Reply`, `Inbox`, the webhook types |
| `domain.py` | messages, people, threads, the answer as it arrives, allow lists, errors |
| `bridge.py` | the `Inbox`: who may ask, which conversation, one turn at a time per thread |
| `api.py` | the deployment's endpoint, as a client |
| `links.py` | which platform thread is which conversation |
| `settings.py`, `registry.py`, `cli.py` | the shared settings, the connectors installed, the command |
| `connect.py`, `serve.py` | the two runners |
| `telegram/`, `slack/` | the first two platforms, as examples of the contract |

## Development

    uv sync --all-extras
    uv run pytest           # the contract, and the import rules of pyproject.toml
    uv run ruff check . && uv run black --check .
