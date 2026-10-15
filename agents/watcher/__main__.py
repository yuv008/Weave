"""Run the impact watcher: change events become gated notices, customer-update drafts and Slack DMs.

Usage: python -m agents.watcher [--tenant acme] [--once | --every [SECONDS]] [--steps events,handoffs,sends]

Each tick claims pending ``change_event`` rows, runs the queued handoffs
through the answer agent, then delivers ready DMs. Delivery is a dry run
unless ``watcher.send`` is ``slack``, the tenant is not the fixture tenant
and ``SLACK_BOT_TOKEN`` is set. Prints one line of counts per tick.
"""

import argparse
import logging
import os
import time

from agents.watcher.worker import STEPS, parse_steps, tick

log = logging.getLogger("weave.watcher")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="one tick, then exit (the default)")
    mode.add_argument("--every", type=int, nargs="?", const=0, metavar="SECONDS",
                      help="tick until stopped; without a value, watcher.every from the config")
    parser.add_argument("--steps", default=",".join(STEPS))
    args = parser.parse_args()
    try:
        steps = parse_steps(args.steps)
    except ValueError as exc:
        parser.error(str(exc))
    if args.every is not None and args.every < 0:
        parser.error("--every must be a positive number of seconds")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from agents.answer.agent import build_agent
    from agents.runtime import live_runtime
    from agents.watcher.outbox import PostgresOutbox
    from agents.watcher.sender import transport_for

    runtime = live_runtime()
    cfg = runtime.cfg
    tenant = args.tenant or os.environ.get("WEAVE_TENANT", cfg.tenant)
    transport = transport_for(cfg.watcher.send, tenant, os.environ.get("SLACK_BOT_TOKEN"), fixture_tenant=cfg.tenant)
    agent = build_agent(runtime) if "handoffs" in steps else None
    outbox = PostgresOutbox()
    worker = f"watcher:{os.getpid()}"
    every = None if args.every is None else (args.every or cfg.watcher.every)
    print(f"watcher tenant={tenant} steps={','.join(steps)} send={type(transport).__name__}"
          + (f" every={every}s" if every else ""), flush=True)

    try:
        while True:
            try:
                result = tick(runtime, tenant, outbox=outbox, agent=agent, transport=transport, steps=steps,
                              worker=worker)
                print(result.summary(), flush=True)
            except Exception:
                if every is None:
                    raise
                log.exception("tick failed; retrying in %ss", every)
            if every is None:
                return
            time.sleep(every)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
