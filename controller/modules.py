"""Module wiring. Order matters: awareness of supervisor edits first, then safety (incidents, caller ID,
guardrail verification), then the shared cohort plan and the optimisers that use it, and governance LAST so
everything drafted in a cycle is batched into one change per workflow."""
from .callerid import CallerID
from .cohorts import CohortPlanner
from .governance import Governance
from .incidents import Hallucination, SupervisorWatch
from .schedule import Schedule
from .verify import Verifier


def build_modules(ctl):
    schedule = Schedule()
    return [SupervisorWatch(), Hallucination(), CallerID(), Verifier(),
            CohortPlanner([schedule]), schedule, Governance(ctl)]
