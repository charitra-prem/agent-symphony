"""Add a case S.DEV_ASSIGNED to next_action so the sweep can advance / block."""
from pathlib import Path

p = Path("/root/work/orchestrator/orchestrator.py")
s = p.read_text()

old = '''                case S.TRIAGED:
                    if transition(linear_id, S.TRIAGED, S.DEV_ASSIGNED, "driver"):
                        await invoke_dev_agent(issue)'''

new = '''                case S.TRIAGED:
                    if transition(linear_id, S.TRIAGED, S.DEV_ASSIGNED, "driver"):
                        await invoke_dev_agent(issue)
                case S.DEV_ASSIGNED:
                    # Re-enter: idempotent. Blocks if repo missing, otherwise
                    # picks up where dev work left off (or no-ops if in flight).
                    await invoke_dev_agent(issue)'''

if old not in s:
    print("WARN: TRIAGED case not found verbatim")
    raise SystemExit(2)
s = s.replace(old, new, 1)
p.write_text(s)
print("added case S.DEV_ASSIGNED to next_action")
