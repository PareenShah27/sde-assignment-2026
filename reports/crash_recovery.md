# Crash-recovery test — public_02_incidents

Run A: uninterrupted (451s). Run B: controller SIGKILLed 12 times at random moments, restarted with the same state dir each time (502s, 11 restarts recorded in the audit log).

Kills (wall-clock after start → simulated time when killed):

- 19.9s → D0 00:00
- 45.12s → D0 06:00
- 30.77s → D1 00:00
- 50.04s → D2 04:30
- 51.84s → D3 11:00
- 5.7s → D3 11:30
- 1.38s → D3 11:30
- 69.28s → D5 06:00
- 21.66s → D5 18:30
- 19.6s → D6 00:30
- 82.31s → D8 00:00
- 39.03s → D8 19:00

| check | result |
|---|---|
| config history identical to uninterrupted run (all workflows: version, actor, dims, proposal_id, time) | PASS |
| approval trail identical (proposal_id, status, time) | PASS |
| no proposal applied twice | PASS |
| no approval lost (approved ⇒ applied / lapsed / stale in our store) | PASS |
| every request that reached the approver is in our outbox | PASS |
| duplicate-proposal 409s observed (recovered by lookup) | 0 |

Config versions: A=45, B=45; approvals: A=29, B=29; credits left: A=197.0, B=197.0
