# Block/state latency canary comparison

Cases: 40

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Expanded Block exact | 72.50% | 77.50% | 5.00% |
| Completion tokens | 134108 | 35363 | 73.63% reduction |
| Response P90 seconds | 101.83645309999974 | 32.822704200000004 | 67.77% reduction |
| Received cases | 40 | 40 | - |
| API errors | 1 | 0 | - |

## Criteria

- block_accuracy_no_material_drop: PASS
- completion_tokens_materially_reduced: PASS
- p90_latency_materially_reduced: PASS
- provider_accepted_parameters: PASS
- provider_execution_evidence: PASS

Overall: PASS
