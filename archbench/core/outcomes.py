"""SubmitOutcome — the typed outcome returned to the agent after every submit.

Historical incident (commits fe938ef2, 48313c1f in the legacy repo):

- A 2h sim timeout was classified as "transient — retry". The agent
  submitted the same pathological code three times, burning 6 wall-hours.
- Compile failures were charged against the agent's submission budget,
  even though no simulation actually ran.

Both bugs were rooted in ambiguous string-based outcome classification.
The structural fix: enumerate the four legitimate outcomes, give each a
fixed agent-facing message AND a fixed budget rule. There is no other
way the connector can answer a submit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SubmitOutcome(Enum):
    """One of exactly four outcomes the connector reports per submit.

    The `consumes_budget` and `retry_recommended` rules are part of the
    enum value definition, not a separate policy — to make it impossible
    to "drift" the rules without changing the enum.
    """

    BUILD_FAIL = "build_fail"          # source compiled wrong
    VALIDATION_REJECT = "validation_reject"  # challenge pre-simulation validation failed
    SIM_TIMEOUT = "sim_timeout"        # build OK, simulator exceeded wall budget
    SIM_OK = "sim_ok"                  # full simulation completed, metrics parsed

    @property
    def consumes_budget(self) -> bool:
        """True iff this outcome counts against the agent's submission limit.

        Only SIM_OK counts. Build/validation/timeout failures are free, so an
        agent isn't penalized for trying things and getting honest feedback.
        """
        return self == SubmitOutcome.SIM_OK

    @property
    def retry_recommended(self) -> bool:
        """True iff the agent should be told 'try again with a fix'.

        SIM_OK obviously doesn't need retry. SIM_TIMEOUT is the dangerous
        case — the legacy bug was telling the agent to retry, which it
        did, identically, three times. We say: do NOT retry without
        changing approach.
        """
        return self in {SubmitOutcome.BUILD_FAIL, SubmitOutcome.VALIDATION_REJECT}

    @property
    def agent_message_prefix(self) -> str:
        """Fixed prefix the connector prepends to the message returned to the agent.

        Agents key off these literal strings to know what just happened.
        DO NOT translate, paraphrase, or vary them.
        """
        return {
            SubmitOutcome.BUILD_FAIL: "SUBMIT BUILD_FAIL",
            SubmitOutcome.VALIDATION_REJECT: "SUBMIT VALIDATION_REJECT",
            SubmitOutcome.SIM_TIMEOUT: "SUBMIT SIM_TIMEOUT",
            SubmitOutcome.SIM_OK: "SUBMIT SIM_OK",
        }[self]


@dataclass
class OutcomeReport:
    """Full structured outcome of one submit call.

    Always returned by the connector — never a None, never a raised
    exception escaping to the agent. (Past bug: container TimeoutExpired
    from `docker exec` would propagate as an unhandled exception and
    crash the agent loop, leaving an orphan container.)
    """

    outcome: SubmitOutcome
    metrics: Optional[dict] = None       # populated iff outcome == SIM_OK
    detail: str = ""                     # human-readable, shown to agent
    raw_log_tail: str = ""               # last ~50 lines of sim output
    submit_index: int = 0                # 1-based, only counted for SIM_OK
    metadata: dict = field(default_factory=dict)

    def to_agent_message(self) -> str:
        """Format the outcome into the literal string returned to the agent.

        Format is stable across all simulators and runtimes; agents parse
        it with line-anchored regex.
        """
        lines = [self.outcome.agent_message_prefix]
        if self.outcome == SubmitOutcome.SIM_OK and self.metrics:
            lines.append(f"  submit_index={self.submit_index}")
            # Sort keys so identical metrics always render identically
            for k in sorted(self.metrics.keys()):
                if k.startswith("_"):
                    continue
                lines.append(f"  {k}={self.metrics[k]}")
        if self.detail:
            lines.append(f"  detail: {self.detail}")
        if self.raw_log_tail:
            lines.append("  ---- last log lines ----")
            for line in self.raw_log_tail.strip().split("\n")[-20:]:
                lines.append(f"  | {line}")
        return "\n".join(lines)
