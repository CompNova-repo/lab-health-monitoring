---
name: enhancement-e2e-test
summary: Run one evidence-backed end-to-end test of machine, metric, and application onboarding, including p1_fixed.py after every enhancement.
---

# Enhancement E2E Test

Use this skill only to orchestrate the deterministic test runner. Do not edit
`p1_fixed.py`, the three onboarding skills, the database schema, or the apps JSON
inside the LLM step.

## Required user input

The user's single-line prompt must provide, directly or through an already-created
config file:

- A reachable test machine: IP, SSH user, SSH key path, port, and alias.
- A harmless numeric Linux metric command.
- An installed application and a harmless monitoring command.
- The path to the test configuration JSON.

## Execution

1. Confirm that the configuration targets a disposable test database.
2. Run exactly:

```bash
python3 e2e_runner.py --config <CONFIG_PATH>
```

3. Return only:
   - Run ID
   - Overall status
   - `summary.md` path
   - `report.json` path
   - Names of failed steps, if any

The Python runner is the source of truth. Do not infer success from the skill's
stdout alone; the database and filesystem assertions in the runner determine
success.
