# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.367 | +50.5% | 0.247 | +35.2% | 0.507 | +7.5% | 43% | 111 (310) |
| nw_kyc | 0.456 | +18.7% | 0.291 | +17.6% | 0.590 | -1.6% | -5% | 32 (38) |
| nw_renewal | 0.462 | +7.5% | 0.287 | +8.9% | 0.537 | -2.0% | 1% | 57 (58) |
| ALL | 0.418 | +24.4% | 0.269 | +20.7% | 0.535 | +1.0% | 23% | 200 (406) |

## Changes applied by the controller

40 applied — neutral: 26, beneficial: 11, harmful: 3

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.018 → **neutral**
- ks_cancel v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.009 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=-0.004 didΔ=+0.050 → **beneficial**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.025 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.011 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.011 didΔ=+0.000 → **beneficial**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.057 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- ks_cancel v7 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- ks_cancel v8 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- ks_cancel v9 (controller_auto) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- ks_cancel v11 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.169 → **beneficial**
- ks_cancel v12 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.099 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['cohorts', 'caller_id'] engageΔ=+0.000 didΔ=+0.272 → **beneficial**
- nw_renewal v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.139 didΔ=+0.000 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v14 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- ks_cancel v15 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.010 didΔ=+0.000 → **harmful**
- nw_kyc v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v6 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v16 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v17 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.006 → **neutral**
- nw_renewal v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.044 didΔ=+0.000 → **beneficial**
- nw_renewal v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v18 (controller_approved) dims=['welcome_message'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_approved) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v7 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=-0.000 → **neutral**
- ks_cancel v19 (controller_approved) dims=['voice'] engageΔ=+0.010 didΔ=+0.000 → **neutral**
- nw_renewal v10 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=-0.015 didΔ=+0.000 → **harmful**
- nw_kyc v8 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v11 (controller_approved) dims=['voice'] engageΔ=-0.022 didΔ=+0.000 → **harmful**
- nw_renewal v12 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.080 didΔ=+0.000 → **beneficial**
- ks_cancel v20 (controller_auto) dims=['welcome_message'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- ks_cancel v21 (controller_auto) dims=['welcome_message'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- ks_cancel v22 (controller_auto) dims=['welcome_message'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- ks_cancel v23 (controller_auto) dims=['welcome_message'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- ks_cancel v24 (controller_auto) dims=['welcome_message'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v9 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 29,
  "decisions": {
    "approved": 25,
    "rejected": 4,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 4
  },
  "applied_via_approval": 23,
  "auto_applied": 17,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 3,
    "nw_kyc": 2,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.128,
      "3": 0.0,
      "4": 0.0,
      "5": 0.007,
      "6": 0.01,
      "7": 0.01,
      "8": 0.009,
      "9": 0.008
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 300,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.909,
    "4": 0.894,
    "5": 0.762,
    "6": 0.5,
    "7": 0.443,
    "8": 0.164,
    "9": 0.025
  }
}
```

Credits used: 3 · validation runs: 1 · LLM fix calls: 0