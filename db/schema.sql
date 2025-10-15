-- PostgreSQL schema for cron job scheduling tables.
-- Run this script once against your database to create the tables required by the app.

CREATE TABLE IF NOT EXISTS cron_jobs (
    id SERIAL PRIMARY KEY,
    job_key VARCHAR(50) NOT NULL UNIQUE,
    schedule_type VARCHAR(20) NOT NULL,
    minute VARCHAR(16) NOT NULL DEFAULT '0',
    hour VARCHAR(16) NOT NULL DEFAULT '*',
    day_of_month VARCHAR(16) NOT NULL DEFAULT '*',
    month VARCHAR(16) NOT NULL DEFAULT '*',
    day_of_week VARCHAR(16) NOT NULL DEFAULT '*',
    daily_time VARCHAR(16),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    next_run TIMESTAMPTZ,
    next_run_display VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_display VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS cron_job_status (
    id SERIAL PRIMARY KEY,
    is_running BOOLEAN NOT NULL DEFAULT FALSE,
    current_trigger VARCHAR(32),
    last_run_at TIMESTAMPTZ,
    last_run_display VARCHAR(64),
    last_success BOOLEAN,
    last_message TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    last_details TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_display VARCHAR(64)
);
