# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.384 | +12.4% | 0.262 | +9.3% | 0.511 | +2.3% | 10% | 61 (52) |
| nw_kyc | 0.456 | +21.2% | 0.286 | +18.2% | 0.615 | +0.5% | 16% | 23 (21) |
| nw_renewal | 0.439 | +4.4% | 0.279 | +6.6% | 0.542 | -0.9% | 7% | 54 (39) |
| ALL | 0.418 | +11.3% | 0.273 | +10.0% | 0.543 | +0.9% | 10% | 138 (112) |

## Changes applied by the controller

31 applied — neutral: 21, beneficial: 10

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.009 → **neutral**
- ks_cancel v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.009 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.021 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.015 didΔ=+0.048 → **beneficial**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.019 → **neutral**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.037 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.028 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.012 → **neutral**
- ks_cancel v7 (controller_approved) dims=['cohorts', 'voice', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.339 → **beneficial**
- nw_renewal v6 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v8 (controller_auto) dims=['voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_auto) dims=['voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- ks_cancel v11 (controller_auto) dims=['voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- ks_cancel v12 (controller_auto) dims=['voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- ks_cancel v13 (controller_auto) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- ks_cancel v14 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.010 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.034 didΔ=+0.000 → **beneficial**
- ks_cancel v15 (controller_auto) dims=['voice'] engageΔ=-0.006 didΔ=+0.000 → **neutral**
- ks_cancel v16 (controller_auto) dims=['voice'] engageΔ=-0.006 didΔ=+0.000 → **neutral**
- ks_cancel v17 (controller_auto) dims=['voice'] engageΔ=-0.000 didΔ=+0.000 → **neutral**
- ks_cancel v18 (controller_auto) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- ks_cancel v19 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- ks_cancel v20 (controller_approved) dims=['voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- nw_renewal v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- nw_kyc v6 (controller_approved) dims=['voice', 'caller_id'] engageΔ=+0.030 didΔ=+0.001 → **beneficial**

## Governance

```
{
  "approval_requests": 18,
  "decisions": {
    "approved": 15,
    "rejected": 3,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 3
  },
  "applied_via_approval": 15,
  "auto_applied": 16,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "nw_kyc": 2,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.1,
    "4": 0.09,
    "5": 0.094,
    "6": 0.065,
    "7": 0.08
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0