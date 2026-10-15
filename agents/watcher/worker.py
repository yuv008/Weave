"""One worker tick: change events, then handoffs, then sends.

The order matters: a notice's handoff runs before the notice is sent, so the
first DM already carries the draft pointer. Each step claims its own work
with ``SKIP LOCKED``, so several workers may tick the same tenant at once.
"""

from collections import Counter
from dataclasses import dataclass, field

from agents.runtime import Runtime
from agents.watcher.handoff import run_handoffs
from agents.watcher.outbox import Outbox
from agents.watcher.sender import Transport, run_sends
from agents.watcher.watcher import AGENT, ImpactWatcher, WatchOutcome

STEPS = ("events", "handoffs", "sends")


@dataclass
class TickResult:
    tenant: str
    events: list[WatchOutcome] = field(default_factory=list)
    handoffs: list[dict] = field(default_factory=list)
    sends: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        """Counts only: no names, ids or message text."""

        parts = [
            f"tenant={self.tenant}",
            f"events={len(self.events)}",
            f"notified={sum(len(o.notified) for o in self.events)}",
            f"filtered={sum(o.filtered for o in self.events)}",
            f"written={sum(len(o.written) for o in self.events)}",
            f"existing={sum(len(o.existing) for o in self.events)}",
            f"errors={sum(o.error for o in self.events)}",
        ]
        for name, outcomes in (("handoffs", self.handoffs), ("sends", self.sends)):
            states = Counter(o["state"] for o in outcomes)
            parts.append(f"{name}=" + (",".join(f"{s}:{n}" for s, n in sorted(states.items())) or "0"))
        return " ".join(parts)


def parse_steps(value: str) -> tuple[str, ...]:
    steps = tuple(dict.fromkeys(s.strip() for s in value.split(",") if s.strip()))
    unknown = [s for s in steps if s not in STEPS]
    if unknown or not steps:
        raise ValueError(f"steps must be a comma-separated subset of {','.join(STEPS)}")
    return tuple(s for s in STEPS if s in steps)


def tick(runtime: Runtime, tenant: str, *, outbox: Outbox, agent=None, transport: Transport | None = None,
         steps: tuple[str, ...] = STEPS, batch: int | None = None, worker: str = AGENT) -> TickResult:
    """Run the chosen steps once, each up to ``batch`` items. Sends are a dry run unless ``transport`` says otherwise."""

    batch = batch or runtime.cfg.watcher.batch
    result = TickResult(tenant)
    if "events" in steps:
        result.events = ImpactWatcher(runtime, outbox, worker).drain(tenant, batch)
    if "handoffs" in steps:
        if agent is None:
            from agents.answer.agent import build_agent

            agent = build_agent(runtime)
        result.handoffs = run_handoffs(runtime, agent, tenant, worker, batch)
    if "sends" in steps:
        result.sends = run_sends(runtime, tenant, worker, transport, batch)
    return result
