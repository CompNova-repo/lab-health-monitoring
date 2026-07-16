# Enhancement E2E Test — `93a5312a-d558-42f4-b18e-9489e5c37d57`

- Status: **PASSED**
- Started: `2026-07-16T03:56:34.252466+00:00`
- Database: `e2e_enhancement_test`
- Evidence directory: `/home/vagrant/.hermes/skills/hermes-enhancement-e2e/test-results/run-20260716T035634Z-93a5312a`

## Steps

| # | Enhancement | Step | Status | Duration |
|---:|---|---|---|---:|
| 1 | preflight | Connected to an isolated test database | **PASSED** | 1 ms |
| 2 | preflight | Generated machine, metric, and app test fixtures | **PASSED** | 2 ms |
| 3 | machine | Run machine onboarding skill | **PASSED** | 2999 ms |
| 4 | machine | Machine exists in machines table with expected values | **PASSED** | 1 ms |
| 5 | machine | Machine SSH port is reachable | **PASSED** | 268 ms |
| 6 | machine | Machine accepts non-interactive SSH | **PASSED** | 7331 ms |
| 7 | machine | Run p1_fixed.py after machine onboarding | **PASSED** | 144308 ms |
| 8 | machine | p1 inserted metric_samples for new machine | **PASSED** | 1 ms |
| 9 | metric | Run metric onboarding skill | **PASSED** | 13093 ms |
| 10 | metric | Metric exists in metric_registry | **PASSED** | 2 ms |
| 11 | metric | Metric command executes successfully on test machine | **PASSED** | 2808 ms |
| 12 | metric | Run p1_fixed.py after metric onboarding | **PASSED** | 146775 ms |
| 13 | metric | p1 created metric_samples column and inserted a value | **PASSED** | 6 ms |
| 14 | app | Run application onboarding skill | **PASSED** | 1121 ms |
| 15 | app | Application command is stored in monitored apps JSON | **PASSED** | 1 ms |
| 16 | app | Stored application command executes on test machine | **PASSED** | 2977 ms |
| 17 | app | Run p1_fixed.py after application onboarding | **PASSED** | 148433 ms |
| 18 | app | p1 inserted app_metric_samples for new application | **PASSED** | 2 ms |

Full expected/actual values, SQL evidence, command output paths, and tracebacks are in `report.json`.
