CREATE TABLE ledger_imports (
    project_id text PRIMARY KEY,
    commit_id text NOT NULL,
    content_sha256 text NOT NULL,
    import_sha256 text NOT NULL,
    task_count integer NOT NULL,
    status_counts jsonb NOT NULL,
    authority text NOT NULL CHECK (authority = 'markdown'),
    imported_at timestamptz NOT NULL DEFAULT now()
);
