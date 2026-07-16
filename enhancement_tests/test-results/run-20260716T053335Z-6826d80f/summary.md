# Enhancement E2E Test — `6826d80f-2573-4a49-bb2f-58e41290a8da`

- Status: **PARTIAL**
- Started: `2026-07-16T05:33:35.215250+00:00`
- Database: `e2e_enhancement_test`
- Evidence directory: `/home/vagrant/.hermes/skills/new-metric-addition/enhancement_tests/test-results/run-20260716T053335Z-6826d80f`

## Steps

| # | Enhancement | Step | Status | Duration |
|---:|---|---|---|---:|
| 1 | preflight | Connected to an isolated test database | **PASSED** | 2 ms |
| 2 | preflight | Generated machine, metric, and app test fixtures | **PASSED** | 2 ms |
| 3 | preflight | App fixture target machine alias matches machine fixture alias | **PASSED** | 2 ms |
| 4 | preflight | Cleanup test-only E2E artifacts before run | **PASSED** | 28 ms |
| 5 | machine | Machine alias is absent before onboarding | **PASSED** | 2 ms |
| 6 | machine | Run machine onboarding skill | **FAILED** | 5112 ms |
| 7 | machine | Machine exists in machines table with expected values | **FAILED** | 2 ms |
| 8 | machine | Machine SSH port is reachable | **FAILED** | 10033 ms |
| 9 | machine | Machine accepts non-interactive SSH | **SKIPPED** | 3 ms |
| 10 | machine | Run p1_fixed.py after machine onboarding | **PASSED** | 52846 ms |
| 11 | machine | p1 inserted metric_samples for new machine | **SKIPPED** | 2 ms |
| 12 | metric | Metric key and metric_samples column are absent before onboarding | **PASSED** | 2 ms |
| 13 | metric | Run metric onboarding skill | **FAILED** | 50499 ms |
| 14 | metric | Metric exists in metric_registry | **FAILED** | 3 ms |
| 15 | metric | Metric command executes successfully on test machine | **SKIPPED** | 2 ms |
| 16 | metric | Run p1_fixed.py after metric onboarding | **PASSED** | 52805 ms |
| 17 | metric | p1 created metric_samples column and inserted a value | **SKIPPED** | 1 ms |
| 18 | app | Application name is absent before onboarding | **PASSED** | 2 ms |
| 19 | app | Run application onboarding skill | **PASSED** | 1108 ms |
| 20 | app | Application command is stored in monitored apps JSON | **PASSED** | 4 ms |
| 21 | app | Stored application command executes on test machine | **SKIPPED** | 4 ms |
| 22 | app | Run p1_fixed.py after application onboarding | **PASSED** | 52925 ms |
| 23 | app | p1 inserted app_metric_samples for new application | **SKIPPED** | 1 ms |

## Failures

- **#6 Run machine onboarding skill** — Assertion returned false
- **#7 Machine exists in machines table with expected values** — Assertion returned false
- **#8 Machine SSH port is reachable** — timed out
- **#13 Run metric onboarding skill** — Assertion returned false
- **#14 Metric exists in metric_registry** — Assertion returned false

Full expected/actual values, SQL evidence, command output paths, and tracebacks are in `report.json`.
