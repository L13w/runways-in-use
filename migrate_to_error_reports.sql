-- Migration: Switch from confidence-based review to user-reported errors
-- Date: 2025-11-16

-- Create new error_reports table
CREATE TABLE IF NOT EXISTS error_reports (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    reported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    current_atis_id INTEGER NOT NULL REFERENCES atis_data(id) ON DELETE CASCADE,
    paired_atis_id INTEGER REFERENCES atis_data(id) ON DELETE CASCADE,
    parsed_arriving_runways JSONB NOT NULL DEFAULT '[]',
    parsed_departing_runways JSONB NOT NULL DEFAULT '[]',
    confidence_score FLOAT NOT NULL DEFAULT 0.0,
    reviewed BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_at TIMESTAMP,
    corrected_arriving_runways JSONB,
    corrected_departing_runways JSONB,
    reviewer_notes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Add indexes for performance
CREATE INDEX idx_error_reports_airport ON error_reports(airport_code);
CREATE INDEX idx_error_reports_reviewed ON error_reports(reviewed);
CREATE INDEX idx_error_reports_reported_at ON error_reports(reported_at DESC);

-- Drop old human_reviews table (replaced by error_reports)
DROP TABLE IF EXISTS human_reviews CASCADE;

-- Add comment
COMMENT ON TABLE error_reports IS 'User-reported parsing errors for human review';

-- Add reported_by column to track computer vs user reports
ALTER TABLE error_reports ADD COLUMN IF NOT EXISTS reported_by VARCHAR(20) DEFAULT 'user';

-- Add unique constraint to prevent duplicate reports for same ATIS
CREATE UNIQUE INDEX IF NOT EXISTS idx_error_reports_airport_atis
ON error_reports(airport_code, current_atis_id);

-- Create parsing_corrections table for learned patterns
CREATE TABLE IF NOT EXISTS parsing_corrections (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    atis_pattern TEXT NOT NULL,
    correction_type VARCHAR(50) NOT NULL DEFAULT 'human_review',
    expected_arriving JSONB NOT NULL DEFAULT '[]',
    expected_departing JSONB NOT NULL DEFAULT '[]',
    success_rate NUMERIC(3,2) NOT NULL DEFAULT 1.0,
    times_applied INTEGER NOT NULL DEFAULT 0,
    created_from_review_id INTEGER,  -- References error_reports.id (no FK constraint)
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Add indexes for parsing_corrections
CREATE INDEX IF NOT EXISTS idx_corrections_airport ON parsing_corrections(airport_code);
CREATE INDEX IF NOT EXISTS idx_corrections_success ON parsing_corrections(success_rate DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_corrections_airport_pattern
ON parsing_corrections(airport_code, atis_pattern);

COMMENT ON TABLE parsing_corrections IS 'Learned correction patterns from human reviews';
