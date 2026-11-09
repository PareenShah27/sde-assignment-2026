# Load test — public_03_load

Machine: 8 vCPU, 14 GB; simulator world generation 1018s (not ours).

| measure | value |
|---|---|
| events ingested (history + 5 days) | 9,737,158 (5,536,955 history) |
| ingest throughput (parse + fold into cubes) | **92,268 events/s** |
| controller time per simulated day (ingest + decide, all cycles) | **22.9 s** |
| decision lag per cycle (ingest + decide), median / p95 / max | 0.36 / 1.15 / 10.41 s |
| decide-only time per cycle, median / p95 | 0.11 / 0.70 s |
| history bootstrap cycle (all pre-D0 events) | 62.3 s |
| simulator time inside /sim/advance per day (not ours) | 18.4 s |
| end-to-end wall-clock (controller process) | 274 s for 5 days = 55 s/day |
| peak memory (controller RSS) | **706 MB** |

Time by module (whole run, s): Schedule 11, Governance 5, CohortPlanner 4, Variants 4, CallerID 0

SRE target: one simulated day of public_03 in < 10 wall-clock minutes on 4 vCPU / 8 GB.

Grade report: reports/public_03_load_grade.md
