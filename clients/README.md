# Clients

Programs that reach a Robinauts deployment through its HTTP API, each apart
from the backend: its own project, its own dependencies, its own container or
store listing. None imports the backend, and the backend imports none of them.

| directory | what it is | acts for |
|---|---|---|
| [connectors/](connectors/) | chat platforms, Slack and Telegram first (Python) | many people at once, with a credential of its own |
| `android/` | the mobile application, planned | one person, signed in |

The web interface is not here: it ships inside the backend's wheel
(`frontend/`).

Why and how: `docs/clients/connectors/working-notes/warm-up/`.
