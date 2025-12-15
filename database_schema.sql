-- Runway Detection System Database Schema
-- PostgreSQL database setup

-- Create database (run as superuser)
-- CREATE DATABASE runway_detection;

-- Connect to runway_detection database
-- \c runway_detection;

-- Create main ATIS data table
CREATE TABLE IF NOT EXISTS atis_data (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    collected_at TIMESTAMP NOT NULL,
    information_letter CHAR(1),
    datis_text TEXT NOT NULL,
    content_hash VARCHAR(32) NOT NULL,
    is_changed BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create runway configurations table
CREATE TABLE IF NOT EXISTS runway_configs (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    atis_id INTEGER REFERENCES atis_data(id) ON DELETE CASCADE,
    arriving_runways JSONB,
    departing_runways JSONB,
    traffic_flow VARCHAR(20),
    configuration_name VARCHAR(50),
    confidence_score FLOAT,
    merged_from_pair BOOLEAN DEFAULT FALSE,  -- TRUE if arrivals/departures came from separate ARR/DEP INFO broadcasts
    component_confidence JSONB,  -- {"arrivals": 1.0, "departures": 1.0} - confidence for each component
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(airport_code, atis_id)
);

-- Create table for tracking configuration changes
CREATE TABLE IF NOT EXISTS runway_changes (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    change_time TIMESTAMP NOT NULL,
    from_config JSONB,
    to_config JSONB,
    duration_minutes INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create table for airport metadata
CREATE TABLE IF NOT EXISTS airports (
    airport_code VARCHAR(4) PRIMARY KEY,
    airport_name VARCHAR(100),
    city VARCHAR(100),
    state VARCHAR(2),
    timezone VARCHAR(30),
    runways JSONB,  -- List of all runways at airport
    active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert known airports
INSERT INTO airports (airport_code, airport_name, city, state, timezone, runways) VALUES
    ('KSEA', 'Seattle-Tacoma International', 'Seattle', 'WA', 'America/Los_Angeles', 
     '["16L", "16C", "16R", "34L", "34C", "34R"]'::jsonb),
    ('KSFO', 'San Francisco International', 'San Francisco', 'CA', 'America/Los_Angeles',
     '["01L", "01R", "19L", "19R", "10L", "10R", "28L", "28R"]'::jsonb),
    ('KLAX', 'Los Angeles International', 'Los Angeles', 'CA', 'America/Los_Angeles',
     '["06L", "06R", "07L", "07R", "24L", "24R", "25L", "25R"]'::jsonb),
    ('KORD', 'Chicago O''Hare International', 'Chicago', 'IL', 'America/Chicago',
     '["04L", "04R", "09L", "09R", "10L", "10C", "10R", "22L", "22R", "27L", "27R", "28L", "28C", "28R"]'::jsonb),
    ('KATL', 'Hartsfield-Jackson Atlanta International', 'Atlanta', 'GA', 'America/New_York',
     '["08L", "08R", "09L", "09R", "10", "26L", "26R", "27L", "27R", "28"]'::jsonb)
ON CONFLICT (airport_code) DO NOTHING;

-- Create error_reports table for user-reported parsing errors
CREATE TABLE IF NOT EXISTS error_reports (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    reported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reported_by VARCHAR(20) DEFAULT 'user',
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

COMMENT ON TABLE error_reports IS 'User-reported parsing errors for human review';

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

COMMENT ON TABLE parsing_corrections IS 'Learned correction patterns from human reviews';

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_atis_airport_time ON atis_data(airport_code, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_atis_hash ON atis_data(content_hash);
CREATE INDEX IF NOT EXISTS idx_atis_changed ON atis_data(is_changed, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_runway_airport ON runway_configs(airport_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_changes_airport ON runway_changes(airport_code, change_time DESC);

-- Indexes for error_reports
CREATE INDEX IF NOT EXISTS idx_error_reports_airport ON error_reports(airport_code);
CREATE INDEX IF NOT EXISTS idx_error_reports_reviewed ON error_reports(reviewed);
CREATE INDEX IF NOT EXISTS idx_error_reports_reported_at ON error_reports(reported_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_error_reports_airport_atis ON error_reports(airport_code, current_atis_id);

-- Indexes for parsing_corrections
CREATE INDEX IF NOT EXISTS idx_corrections_airport ON parsing_corrections(airport_code);
CREATE INDEX IF NOT EXISTS idx_corrections_success ON parsing_corrections(success_rate DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_corrections_airport_pattern ON parsing_corrections(airport_code, atis_pattern);

-- Create views for common queries
CREATE OR REPLACE VIEW current_runway_configs AS
SELECT DISTINCT ON (rc.airport_code)
    rc.airport_code,
    a.airport_name,
    rc.arriving_runways,
    rc.departing_runways,
    rc.traffic_flow,
    rc.configuration_name,
    rc.confidence_score,
    ad.information_letter,
    ad.collected_at as last_updated
FROM runway_configs rc
JOIN atis_data ad ON rc.atis_id = ad.id
LEFT JOIN airports a ON rc.airport_code = a.airport_code
ORDER BY rc.airport_code, rc.created_at DESC;

-- View for runway change frequency analysis
CREATE OR REPLACE VIEW runway_change_stats AS
SELECT 
    airport_code,
    DATE(change_time) as date,
    COUNT(*) as changes_count,
    AVG(duration_minutes) as avg_duration_minutes
FROM runway_changes
GROUP BY airport_code, DATE(change_time)
ORDER BY airport_code, date DESC;

-- Function to detect runway configuration changes
CREATE OR REPLACE FUNCTION detect_runway_change() 
RETURNS TRIGGER AS $$
DECLARE
    prev_config RECORD;
    duration_mins INTEGER;
BEGIN
    -- Get the previous configuration
    SELECT arriving_runways, departing_runways, traffic_flow, created_at
    INTO prev_config
    FROM runway_configs
    WHERE airport_code = NEW.airport_code
      AND id < NEW.id
    ORDER BY id DESC
    LIMIT 1;
    
    -- If there was a previous config and it's different
    IF FOUND AND (
        prev_config.arriving_runways::text != NEW.arriving_runways::text OR
        prev_config.departing_runways::text != NEW.departing_runways::text OR
        prev_config.traffic_flow != NEW.traffic_flow
    ) THEN
        -- Calculate duration of previous configuration
        duration_mins := EXTRACT(EPOCH FROM (NEW.created_at - prev_config.created_at)) / 60;
        
        -- Insert change record
        INSERT INTO runway_changes (
            airport_code,
            change_time,
            from_config,
            to_config,
            duration_minutes
        ) VALUES (
            NEW.airport_code,
            NEW.created_at,
            jsonb_build_object(
                'arriving', prev_config.arriving_runways,
                'departing', prev_config.departing_runways,
                'flow', prev_config.traffic_flow
            ),
            jsonb_build_object(
                'arriving', NEW.arriving_runways,
                'departing', NEW.departing_runways,
                'flow', NEW.traffic_flow
            ),
            duration_mins
        );
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger for change detection
DROP TRIGGER IF EXISTS runway_change_trigger ON runway_configs;
CREATE TRIGGER runway_change_trigger
    AFTER INSERT ON runway_configs
    FOR EACH ROW
    EXECUTE FUNCTION detect_runway_change();

-- Utility functions
CREATE OR REPLACE FUNCTION get_runway_usage_stats(
    p_airport_code VARCHAR(4),
    p_days INTEGER DEFAULT 7
)
RETURNS TABLE (
    runway VARCHAR,
    usage_count INTEGER,
    as_arrival INTEGER,
    as_departure INTEGER,
    percentage FLOAT
) AS $$
BEGIN
    RETURN QUERY
    WITH runway_usage AS (
        SELECT 
            jsonb_array_elements_text(arriving_runways) as runway,
            'arrival' as usage_type
        FROM runway_configs
        WHERE airport_code = p_airport_code
          AND created_at > CURRENT_TIMESTAMP - (p_days || ' days')::INTERVAL
        UNION ALL
        SELECT 
            jsonb_array_elements_text(departing_runways) as runway,
            'departure' as usage_type
        FROM runway_configs
        WHERE airport_code = p_airport_code
          AND created_at > CURRENT_TIMESTAMP - (p_days || ' days')::INTERVAL
    ),
    counts AS (
        SELECT 
            runway,
            COUNT(*) as total_usage,
            COUNT(CASE WHEN usage_type = 'arrival' THEN 1 END) as arrival_count,
            COUNT(CASE WHEN usage_type = 'departure' THEN 1 END) as departure_count
        FROM runway_usage
        GROUP BY runway
    )
    SELECT 
        c.runway::VARCHAR,
        c.total_usage::INTEGER,
        c.arrival_count::INTEGER,
        c.departure_count::INTEGER,
        (c.total_usage::FLOAT / SUM(c.total_usage) OVER () * 100)::FLOAT as percentage
    FROM counts c
    ORDER BY c.total_usage DESC;
END;
$$ LANGUAGE plpgsql;

-- Grant permissions (adjust as needed)
-- GRANT ALL ON ALL TABLES IN SCHEMA public TO runway_api_user;
-- GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO runway_api_user;
-- GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO runway_api_user;
