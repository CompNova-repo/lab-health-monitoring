# Enhancement E2E Test — `f9381d42-4018-4e5b-8392-e7e76b188123`

- Status: **FAILED**
- Started: `2026-07-16T05:38:00.180580+00:00`
- Database: `e2e_enhancement_test`
- Evidence directory: `/home/vagrant/.hermes/skills/new-metric-addition/enhancement_tests/test-results/run-20260716T053800Z-f9381d42`

## Steps

| # | Enhancement | Step | Status | Duration |
|---:|---|---|---|---:|
| 1 | preflight | Connected to an isolated test database | **PASSED** | 4 ms |
| 2 | preflight | Generated machine, metric, and app test fixtures | **PASSED** | 1 ms |
| 3 | preflight | App fixture target machine alias matches machine fixture alias | **PASSED** | 1 ms |
| 4 | preflight | Cleanup test-only E2E artifacts before run | **PASSED** | 23 ms |
| 5 | machine | Machine alias is absent before onboarding | **PASSED** | 8 ms |
| 6 | machine | Run machine onboarding skill | **FAILED** | 5147 ms |
| 7 | machine | Machine exists in machines table with expected values | **FAILED** | 4 ms |
| 8 | machine | Machine SSH port is reachable | **FAILED** | 10022 ms |
| 9 | machine | Machine accepts non-interactive SSH | **SKIPPED** | 7 ms |
| 10 | machine | Run p1_fixed.py after machine onboarding | **PASSED** | 52819 ms |
| 11 | machine | p1 inserted metric_samples for new machine | **SKIPPED** | 2 ms |
| 12 | metric | Metric key and metric_samples column are absent before onboarding | **PASSED** | 6 ms |
| 13 | metric | Run metric onboarding skill | **FAILED** | 50513 ms |
| 14 | metric | Metric exists in metric_registry | **FAILED** | 3 ms |
| 15 | metric | Metric command executes successfully on test machine | **SKIPPED** | 1 ms |
| 16 | metric | Run p1_fixed.py after metric onboarding | **PASSED** | 53089 ms |
| 17 | metric | p1 created metric_samples column and inserted a value | **SKIPPED** | 1 ms |
| 18 | app | Application name is absent before onboarding | **PASSED** | 2 ms |
| 19 | app | Run application onboarding skill | **PASSED** | 1162 ms |
| 20 | app | Application command is stored in monitored apps JSON | **PASSED** | 1 ms |
| 21 | app | Stored application command executes on test machine | **SKIPPED** | 4 ms |
| 22 | app | Run p1_fixed.py after application onboarding | **PASSED** | 52996 ms |
| 23 | app | p1 inserted app_metric_samples for new application | **SKIPPED** | 3 ms |

## Failures

- **#6 Run machine onboarding skill** — Assertion returned false
- **#7 Machine exists in machines table with expected values** — Assertion returned false
- **#8 Machine SSH port is reachable** — timed out
- **#13 Run metric onboarding skill** — Assertion returned false
- **#14 Metric exists in metric_registry** — Assertion returned false

Full expected/actual values, SQL evidence, command output paths, and tracebacks are in `report.json`.
