# Enhancement E2E Test — `e193af40-f925-425c-b5a6-0b8e44e17e93`

- Status: **FAILED**
- Started: `2026-07-16T05:28:29.419781+00:00`
- Database: `e2e_enhancement_test`
- Evidence directory: `/home/vagrant/.hermes/skills/new-metric-addition/enhancement_tests/test-results/run-20260716T052829Z-e193af40`

## Steps

| # | Enhancement | Step | Status | Duration |
|---:|---|---|---|---:|
| 1 | preflight | Connected to an isolated test database | **PASSED** | 2 ms |
| 2 | preflight | Generated machine, metric, and app test fixtures | **PASSED** | 1 ms |
| 3 | preflight | App fixture target machine alias matches machine fixture alias | **PASSED** | 1 ms |
| 4 | preflight | Cleanup test-only E2E artifacts before run | **PASSED** | 390 ms |
| 5 | machine | Machine alias is absent before onboarding | **PASSED** | 6 ms |
| 6 | machine | Run machine onboarding skill | **FAILED** | 5322 ms |
| 7 | machine | Machine exists in machines table with expected values | **FAILED** | 2 ms |
| 8 | machine | Machine SSH port is reachable | **FAILED** | 10053 ms |
| 9 | machine | Machine accepts non-interactive SSH | **SKIPPED** | 3 ms |
| 10 | machine | Run p1_fixed.py after machine onboarding | **PASSED** | 54633 ms |
| 11 | machine | p1 inserted metric_samples for new machine | **SKIPPED** | 2 ms |
| 12 | metric | Metric key and metric_samples column are absent before onboarding | **PASSED** | 3 ms |
| 13 | metric | Run metric onboarding skill | **FAILED** | 50476 ms |
| 14 | metric | Metric exists in metric_registry | **FAILED** | 2 ms |
| 15 | metric | Metric command executes successfully on test machine | **SKIPPED** | 1 ms |
| 16 | metric | Run p1_fixed.py after metric onboarding | **PASSED** | 52877 ms |
| 17 | metric | p1 created metric_samples column and inserted a value | **SKIPPED** | 1 ms |
| 18 | app | Application name is absent before onboarding | **PASSED** | 4 ms |
| 19 | app | Run application onboarding skill | **PASSED** | 1148 ms |
| 20 | app | Application command is stored in monitored apps JSON | **PASSED** | 11 ms |
| 21 | app | Stored application command executes on test machine | **SKIPPED** | 2 ms |
| 22 | app | Run p1_fixed.py after application onboarding | **PASSED** | 52882 ms |
| 23 | app | p1 inserted app_metric_samples for new application | **SKIPPED** | 5 ms |

## Failures

- **#6 Run machine onboarding skill** — Assertion returned false
- **#7 Machine exists in machines table with expected values** — Assertion returned false
- **#8 Machine SSH port is reachable** — timed out
- **#13 Run metric onboarding skill** — Assertion returned false
- **#14 Metric exists in metric_registry** — Assertion returned false

Full expected/actual values, SQL evidence, command output paths, and tracebacks are in `report.json`.
