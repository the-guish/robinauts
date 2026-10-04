# 05 — A proxy's idle timeout cuts the stream during a slow tool call

## Summary

While a tool call runs, the turn's stream sends nothing. A reverse proxy or load
balancer closes a connection that stays quiet past its idle or read timeout
(commonly 60 s). The browser then re-attaches a few times in quick succession, and
gives up: the answer shows "connection lost", though the turn is still running and
finishes on the server. A reload shows the finished answer.

## How to reproduce

Setup as in [01](01-crash-blocks-the-conversation.md#setup-used-by-all-the-issues-in-this-folder),
behind a proxy with a 30 s read timeout. For example nginx on Linux:

```nginx
# nginx.conf
events {}
http {
  server {
    listen 8080;
    location / {
      proxy_pass http://127.0.0.1:8000;
      proxy_http_version 1.1;
      proxy_buffering off;
      proxy_read_timeout 30s;
    }
  }
}
```

```sh
docker run --rm --network host -v "$PWD/nginx.conf:/etc/nginx/nginx.conf:ro" nginx
```

1. Open `http://127.0.0.1:8080/` and send "Call slow_echo with seconds=180 and
   text=hello".
2. About 30 s after the tool call shows, nginx closes the stream. The client
   re-attaches, each attempt is closed again after 30 s of silence, and within a
   few minutes the answer is marked "connection lost".
3. Reload after 180 s: the answer is there, finished.

A milder form needs no proxy. Stop the server mid-turn and start it again more than
about 5 s later: the client has already given up.

## Why it happens

- **Nothing is sent while nothing happens.** `watch_turn` waits up to 15 s at a
  time and loops without yielding anything (`controller/application/controller.py:409-441`).
  `web/agui.py` writes only real events. The keep-alive comment the wire spec
  describes is listed as not yet served (`docs/specs/wire.md:150`).
- **A re-attach is silent too.** It waits for the turn's next event before it sends
  any response (`web/app.py:458-475`), so the proxy times it out again.
- **The client's retry budget is short.**
  - `RETRIES = 4` with backoffs of 250 ms to 2 s
    (`frontend/src/chat/assistant-ui/agui/client.ts:56-59`).
  - The budget resets only when a new numbered event arrives.
  - After it runs out, the client re-reads the conversation once, and then
    declares the stream lost (`frontend/src/chat/assistant-ui/runtime.tsx:556-577`).
- **No idle watchdog.** A half-open connection that never closes is never noticed
  (`agui/sse.ts`).
