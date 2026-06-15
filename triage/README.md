# linear-triage

Linear webhook → PM agent (warm Claude tmux session) → Linear comment.

## Layout

```
/root/work/triage/
  triage_service.py     # FastAPI app
  claude_session.py     # Persistent claude tmux wrapper (pm-claude session)
  linear_client.py      # Tiny GraphQL client
  routing_matrix.yaml   # Source of truth for harness recommendations
  pm_prompt.md          # System prompt for the PM agent
  requirements.txt
  systemd/triage.service
/etc/triage/env         # LINEAR_API_KEY, LINEAR_WEBHOOK_SECRET
```

## Endpoints

| Method | Path           | Notes                                          |
|--------|----------------|------------------------------------------------|
| GET    | /healthz       | liveness                                       |
| POST   | /webhook       | Linear event sink (HMAC-verified)              |
| POST   | /triage-now    | manual: `{"id": "ENG-123"}` → triage that issue |

## Operations

```
systemctl status triage cloudflared-triage pcci-proxy
tail -f /var/log/triage.log
tmux ls                           # pm-claude should be present
tmux attach -t pm-claude          # peek at the warm Claude session
```

Manual triage test:
```
curl -s http://127.0.0.1:8088/triage-now -H 'Content-Type: application/json' \
  -d '{"id":"ENG-123"}' | jq
```

## Editing routing logic

Edit `/root/work/triage/routing_matrix.yaml` then restart the service:
```
systemctl restart triage
```

The PM agent re-reads the matrix on every issue (it's part of the system prompt).
