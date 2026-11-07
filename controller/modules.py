"""Module wiring. Order matters: awareness of supervisor edits first, then safety (incidents), then
optimisation planners, and governance LAST so that everything drafted in a cycle is batched into one
change per workflow."""
from .governance import Governance
from .incidents import Hallucination, SupervisorWatch


def build_modules(ctl):
    return [SupervisorWatch(), Hallucination(), Governance(ctl)]
