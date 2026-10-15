"""Flow 3 on the fixture: Provider A is replaced, and Priya hears about her Acme commitment.

Usage: python -m scripts.flow3 [--tenant acme] [--reset]
       (python scripts/flow3.py works too)

``--reset`` marks the fixture's change event pending again and deletes what
earlier runs wrote for it: the ``notice:`` and ``handoff:`` tasks, the draft
proposal each handoff produced, and that draft's episode and conversation.
Then one watcher tick runs (events, handoffs, sends) and the script prints
what Priya gets: the notice as delivered, the handoff state and the draft.
Only for the fixture tenant; delivery is always a dry run there.
"""

import argparse
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def reset(connect, tenant: str, event_id: str) -> dict:
    prefixes = [f"notice:{event_id}:", f"handoff:{event_id}:"]
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE change_event SET processed = false WHERE tenant_id = %s AND id = %s", (tenant, event_id)
            )
            events = cur.rowcount
            cur.execute(
                "SELECT id, payload->>'proposal_id', payload->>'episode_id' FROM tasks WHERE tenant_id = %s"
                " AND (left(id, length(%s)) = %s OR left(id, length(%s)) = %s)",
                (tenant, prefixes[0], prefixes[0], prefixes[1], prefixes[1]),
            )
            rows = cur.fetchall()
            tasks = [r[0] for r in rows] + [r[1] for r in rows if r[1]]
            episodes = [r[2] for r in rows if r[2]]
            cur.execute(
                "SELECT conversation_id FROM episodes WHERE tenant_id = %s AND id = ANY(%s::text[])"
                " AND conversation_id IS NOT NULL", (tenant, episodes),
            )
            conversations = [r[0] for r in cur.fetchall()]
            cur.execute("DELETE FROM tasks WHERE tenant_id = %s AND id = ANY(%s::text[])", (tenant, tasks))
            deleted_tasks = cur.rowcount
            cur.execute("DELETE FROM episodes WHERE tenant_id = %s AND id = ANY(%s::text[])", (tenant, episodes))
            cur.execute("DELETE FROM conversation_turns WHERE conversation_id = ANY(%s::text[])", (conversations,))
            cur.execute("DELETE FROM conversations WHERE tenant_id = %s AND id = ANY(%s::text[])",
                        (tenant, conversations))
        conn.commit()
    finally:
        conn.close()
    return {"events_reset": events, "tasks_deleted": deleted_tasks, "episodes_deleted": len(episodes)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    from agents.answer.agent import build_agent
    from agents.runtime import live_runtime
    from agents.watcher.outbox import PostgresOutbox
    from agents.watcher.sender import DryRunTransport
    from agents.watcher.watcher import handoff_id, notice_id
    from agents.watcher.worker import tick
    from fixtures.acme import build
    from shared.config import load_config
    from shared.env import load_env
    from shared.pg import connect_pg

    load_env()
    cfg = load_config()
    tenant = args.tenant or os.environ.get("WEAVE_TENANT", cfg.tenant)
    if tenant != cfg.tenant:
        raise SystemExit(f"refusing to run flow 3 for {tenant!r}; it resets fixture data, only {cfg.tenant!r}")
    fx = build(tenant, cfg)
    [event] = fx.change_events
    priya = fx.ids["priya"]

    if args.reset:
        counts = reset(connect_pg, tenant, event["id"])
        print(" ".join(f"{k}={v}" for k, v in counts.items()))
        if not counts["events_reset"]:
            raise SystemExit("the fixture change event is missing; run python -m scripts.seed_fixture --skip-graph")

    runtime = live_runtime()
    result = tick(runtime, tenant, outbox=PostgresOutbox(), agent=build_agent(runtime),
                  transport=DryRunTransport(), worker="flow3")
    print(result.summary())

    memory = runtime.memory
    notice = memory.get_task(notice_id(event["id"], priya))
    handoff = memory.get_task(handoff_id(event["id"], priya))
    print(f"\nChange: {event['id']}")
    if notice is None:
        print("Priya has no notice for this event (run with --reset to replay it).")
        return
    delivery = notice.payload.get("delivery") or {}
    print(f"\nNotice to Priya [{notice.state}, {delivery.get('transport', 'not delivered')}]:")
    print(delivery.get("text") or notice.payload.get("text"))
    if handoff is None:
        print("\nNo handoff was queued.")
        return
    print(f"\nHandoff [{handoff.state}]" + (f": {handoff.payload['reason']}" if handoff.payload.get("reason") else ""))
    proposal_id = handoff.payload.get("proposal_id")
    proposal = memory.get_task(proposal_id) if proposal_id else None
    if proposal is None:
        return
    print(f"\nDraft {proposal.id} ({proposal.kind}, {proposal.state}, created by Priya: {proposal.created_by == priya}):")
    print(proposal.payload.get("draft"))
    print(f"\nConfirm: python -m scripts.confirm_action {proposal.id} accept --as priya")


if __name__ == "__main__":
    main()
