# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.397 | +16.5% | 0.270 | +10.1% | 0.520 | +4.5% | 31% | 392 (383) |
| arav_kyc_1 | 0.418 | +16.7% | 0.245 | +14.0% | 0.598 | -0.2% | 4% | 438 (349) |
| arav_renewal_0 | 0.461 | +14.2% | 0.290 | +10.1% | 0.563 | +2.8% | 15% | 479 (458) |
| kest_cancel_2 | 0.394 | +16.5% | 0.269 | +10.3% | 0.517 | +3.9% | 32% | 438 (388) |
| kest_kyc_1 | 0.400 | +12.7% | 0.239 | +11.9% | 0.591 | -1.1% | 2% | 376 (309) |
| kest_renewal_0 | 0.454 | +13.2% | 0.289 | +10.1% | 0.558 | +2.1% | 16% | 490 (430) |
| nort_cancel_2 | 0.401 | +18.1% | 0.268 | +10.1% | 0.527 | +6.1% | 38% | 435 (389) |
| nort_kyc_1 | 0.437 | +23.5% | 0.257 | +20.3% | 0.595 | -0.2% | 3% | 411 (346) |
| nort_renewal_0 | 0.468 | +16.7% | 0.290 | +9.8% | 0.571 | +4.8% | 25% | 510 (431) |
| tide_cancel_2 | 0.404 | +19.0% | 0.267 | +9.2% | 0.535 | +7.3% | 47% | 425 (406) |
| tide_kyc_1 | 0.411 | +15.8% | 0.243 | +13.9% | 0.594 | -0.6% | 4% | 399 (339) |
| tide_renewal_0 | 0.463 | +15.3% | 0.288 | +9.8% | 0.569 | +4.2% | 24% | 450 (422) |
| ALL | 0.426 | +16.4% | 0.268 | +11.4% | 0.561 | +2.9% | 19% | 5243 (4650) |

## Changes applied by the controller

81 applied — neutral: 36, beneficial: 45

- nort_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- nort_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- kest_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- kest_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- arav_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- arav_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- tide_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.010 → **neutral**
- tide_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- arav_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.019 → **neutral**
- nort_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.054 → **beneficial**
- nort_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- kest_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.054 → **beneficial**
- kest_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- arav_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.034 → **beneficial**
- arav_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- tide_renewal_0 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.054 → **beneficial**
- tide_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.024 → **beneficial**
- nort_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- kest_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- arav_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- tide_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- tide_cancel_2 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- kest_renewal_0 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nort_renewal_0 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- tide_renewal_0 v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- arav_cancel_2 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- kest_cancel_2 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nort_cancel_2 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- arav_renewal_0 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nort_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.009 didΔ=+0.064 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.009 didΔ=+0.050 → **beneficial**
- kest_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.009 didΔ=+0.050 → **beneficial**
- kest_cancel_2 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.040 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.057 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.071 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.029 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.009 didΔ=+0.063 → **beneficial**
- nort_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.266 → **beneficial**
- kest_cancel_2 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.039 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- tide_cancel_2 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.027 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.036 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- tide_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.175 → **beneficial**
- arav_kyc_1 v3 (controller_approved) dims=['welcome_message', 'caller_id'] engageΔ=+0.008 didΔ=+0.194 → **beneficial**
- kest_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.269 → **beneficial**
- tide_renewal_0 v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nort_renewal_0 v7 (controller_approved) dims=['voice'] engageΔ=-0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.029 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.010 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.033 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v9 (controller_auto) dims=['welcome_message'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- tide_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.274 → **beneficial**
- arav_renewal_0 v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.141 → **beneficial**
- nort_cancel_2 v10 (controller_auto) dims=['welcome_message'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nort_cancel_2 v11 (controller_auto) dims=['welcome_message'] engageΔ=-0.005 didΔ=+0.000 → **neutral**
- nort_renewal_0 v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v12 (controller_auto) dims=['welcome_message'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- arav_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.283 → **beneficial**
- nort_cancel_2 v13 (controller_auto) dims=['welcome_message'] engageΔ=-0.003 didΔ=+0.000 → **neutral**
- nort_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.129 → **beneficial**
- tide_kyc_1 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v9 (controller_approved) dims=['voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- kest_cancel_2 v9 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- kest_renewal_0 v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.021 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_renewal_0 v9 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v9 (controller_approved) dims=['welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- nort_cancel_2 v14 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- nort_kyc_1 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 63,
  "decisions": {
    "approved": 55,
    "rejected": 7,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 7
  },
  "applied_via_approval": 55,
  "auto_applied": 26,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nort_renewal_0": 2,
    "nort_kyc_1": 2,
    "nort_cancel_2": 1,
    "kest_renewal_0": 1,
    "kest_kyc_1": 2,
    "kest_cancel_2": 1,
    "arav_renewal_0": 2,
    "arav_kyc_1": 2,
    "arav_cancel_2": 1,
    "tide_renewal_0": 3,
    "tide_kyc_1": 2,
    "tide_cancel_2": 1
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
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.719,
    "4": 0.713
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.454,
    "4": 0.442
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.545,
    "4": 0.465
  },
  "tide_renewal_0:welcome_message=W3": {
    "3": 0.662,
    "4": 0.607
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0