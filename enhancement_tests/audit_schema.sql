CREATE TABLE IF NOT EXISTS public.enhancement_test_runs (
    run_id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('running', 'passed', 'failed', 'partial')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    config_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    report_path text,
    passed_steps integer NOT NULL DEFAULT 0,
    failed_steps integer NOT NULL DEFAULT 0,
    skipped_steps integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS public.enhancement_test_steps (
    id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES public.enhancement_test_runs(run_id) ON DELETE CASCADE,
    sequence_no integer NOT NULL,
    enhancement text NOT NULL CHECK (enhancement IN ('preflight', 'machine', 'metric', 'app', 'finalize')),
    step_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'passed', 'failed', 'skipped')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    duration_ms bigint,
    expected jsonb,
    actual jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    UNIQUE (run_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS idx_enhancement_test_steps_run
    ON public.enhancement_test_steps(run_id, sequence_no);
