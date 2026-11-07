"""Module wiring. Order matters: safety (incidents, caller ID) before optimisation, governance last so that
everything planned in this cycle is batched into one request per workflow."""


def build_modules(ctl):
    return []
