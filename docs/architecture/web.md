# Web

The operations the web shell offers the frontend.

## Sign-in

- `sign_in`: start the sign-in with a provider, and finish it when the provider answers.
- `sign_out`
- `current_user_session`: who is signed in, if anyone.
- `mint_api_token`, `list_api_tokens`, `revoke_api_token`: the API tokens of the person signed in, sent as a bearer. Minting and revoking need the browser's session, minting one younger than ten minutes.

## Devices

How a phone is linked and ended (`docs/architecture/device-pairing.md`). Minting and removing need the browser's session; a bearer is answered 403.

- `mint_pairing`: a code for a phone, answered as its QR picture alone; it needs a session younger than ten minutes.
- `pairing_state`: what the browser polls: waiting, claimed with the device's description, or approved.
- `claim_pairing`: the phone presents the code, with no credential, and gets a claim secret and the number to show.
- `approve_pairing`: the number the phone shows, typed into the browser; links the device.
- `refuse_pairing`: No, or a code cancelled.
- `list_devices`: the person's linked devices, oldest first.
- `remove_device`: end one of them.
- `current_user_session` also answers a bearer: the device or the token it is, or the pairing a claim waits on; and `sign_out` with a device's bearer ends that device.

## Catalogue

- `list_agents`
- `list_models`

## Sessions

- `list_sessions`: most recently updated first, a page at a time.
- `open_session`: one moment of a session, its messages and its active turn if any.
- `rename_session`
- `delete_session`
- `fork_session`: a new session from a message of another, independent from then on.

## Turns

- `start_session`: the first message, with the agent and the model; mints the session.
- `send_message`: a message under a chosen parent, with the model. An edit is this under an earlier parent.
- `regenerate_answer`: the answer to a question again, under the same question.
- `resume_answer`: the turn of the session's latest answer, when it failed, continued from what its engine saved.
- `cancel_turn`: stop the session's active turn.
- `watch_turn`: the events of the session's active turn, from a position, as they happen.

## Worker

What a worker in a process of its own reaches the core through: the operations of
`TurnBroker` (`docs/architecture/controller.md`, "Worker"), one route each under
`/internal/worker/`, served over the controller's store. They are not the frontend's and are
not in the OpenAPI document.

- The server makes a secret at its start and writes it to a pipe the worker's process reads
  at its own; it is in no environment variable and no file. Every route wants it as
  `Authorization: Bearer`: any other credential is 401. With no worker process running, the
  routes answer 404.
- The worker reaches the server where it listens, on a loopback address when it listens on
  every address.
- A turn lost is 409 `turn_lost`, a session not found 404 `session_not_found`: the worker
  raises the core's own errors for them.
- `listen_for_cancels` is a long poll past a cursor. A cancel the worker misses is named by
  its next renewal of the leases.
- A server stops its worker before it stops listening, since the worker ends its turns
  through these routes; the waits it holds end at once when the worker has stopped.

