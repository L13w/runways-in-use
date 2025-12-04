# System Architecture Documentation

## Overview

The Runway Direction Detection System is a real-time data collection and parsing system that monitors D-ATIS (Digital ATIS) broadcasts from US airports to determine active runway configurations. This document provides a technical deep-dive into the system architecture, data flow, and design decisions.

---

## System Components

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     External Data Source                     │
│                 https://datis.clowd.io/api/all              │
└────────────────────────────┬────────────────────────────────┘
                             │ HTTP GET (every 5 min)
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    ATIS Collector Service                    │
│  ┌──────────────────┐      ┌──────────────────────────────┐ │
│  │  Cron Scheduler  │──────▶  Collection & Parsing Logic  │ │
│  │  (*/5 * * * *)   │      │  - Fetch ATIS data          │ │
│  │                  │      │  - Hash comparison          │ │
│  │                  │      │  - Parse runways            │ │
│  │                  │      │  - Calculate confidence     │ │
│  └──────────────────┘      └──────────────┬───────────────┘ │
└───────────────────────────────────────────┼─────────────────┘
                                            │ psycopg2
                                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    PostgreSQL Database                       │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │  atis_data  │  │runway_configs│  │ runway_changes   │   │
│  │             │  │              │  │ (trigger-based)  │   │
│  └─────────────┘  └──────────────┘  └──────────────────┘   │
│  ┌──────────────┐  ┌──────────────┐                        │
│  │error_reports │  │parsing_corr. │                        │
│  └──────────────┘  └──────────────┘                        │
└────────────────────────────┬────────────────────────────────┘
                             │ psycopg2
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                      FastAPI Server                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  REST API    │  │  Dashboard   │  │  Review System   │  │
│  │  Endpoints   │  │  (HTML/JS)   │  │  (HTML/JS)       │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
└────────────────────────────┬────────────────────────────────┘
                             │ HTTP
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                        End Users                             │
│  - API clients requesting runway data                        │
│  - Dashboard viewers monitoring real-time stats             │
│  - Human reviewers correcting parsing errors                │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Flow

### 1. Collection Flow (Every 5 Minutes)

```
┌──────────┐
│ Cron Job │ Triggers every 5 minutes
└────┬─────┘
     │
     ▼
┌────────────────────┐
│ Fetch ATIS Data    │ GET https://datis.clowd.io/api/all
│ from clowd.io API  │ Returns JSON array of all US airports
└────┬───────────────┘
     │
     ▼
┌────────────────────┐
│ For Each Airport   │ Loop through ~200+ airports
└────┬───────────────┘
     │
     ├──▶ Calculate MD5 hash of ATIS text
     │
     ├──▶ Compare with last stored hash
     │
     ├──▶ IF CHANGED:
     │    │
     │    ├──▶ Store new ATIS record (is_changed=TRUE)
     │    │
     │    ├──▶ Parse runway configuration
     │    │    │
     │    │    ├──▶ Extract arriving runways
     │    │    ├──▶ Extract departing runways
     │    │    ├──▶ Determine traffic flow
     │    │    ├──▶ Calculate confidence score
     │    │    └──▶ Generate config name
     │    │
     │    └──▶ Store runway_config record
     │         │
     │         └──▶ TRIGGER: Insert into runway_changes (if different from previous)
     │
     └──▶ ELSE: Store ATIS record (is_changed=FALSE), skip parsing

After all airports processed:
     │
     ▼
┌────────────────────────────────┐
│ Merge Split ATIS Pairs         │ For airports with ARR INFO + DEP INFO
│ (KCLE, KDEN, KDTW, etc.)      │
│                                │
│  - Combine arrivals + departures
│  - Calculate merged confidence │
│  - Store merged config         │
│  - Validate merged result      │
│  - Create error report if low  │
│    confidence (< 90%)          │
└────────────────────────────────┘
```

### 1a. Split-ATIS Handling

Some airports publish separate arrival and departure ATIS broadcasts:
- **ARR INFO**: Contains only arrival runway assignments
- **DEP INFO**: Contains only departure runway assignments

The collector handles these specially:

```
┌─────────────────────────────────────────────────────────────┐
│                    Split ATIS Flow                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ARR INFO broadcast                DEP INFO broadcast       │
│       │                                  │                  │
│       ▼                                  ▼                  │
│  ┌──────────────┐                ┌──────────────┐          │
│  │ Parse arrivals│                │Parse departures│         │
│  │ (dep=empty)  │                │ (arr=empty)   │         │
│  └──────┬───────┘                └──────┬───────┘          │
│         │                               │                   │
│         │   Collect both halves         │                   │
│         └───────────┬───────────────────┘                   │
│                     │                                       │
│                     ▼                                       │
│         ┌──────────────────────┐                           │
│         │ Merge Configurations │                           │
│         │ - ARR runways from ARR│                           │
│         │ - DEP runways from DEP│                           │
│         │ - Avg confidence      │                           │
│         │   (100% if both ≥90%) │                           │
│         └──────────┬───────────┘                           │
│                    │                                        │
│                    ▼                                        │
│         ┌──────────────────────┐                           │
│         │ Validate Merged      │                           │
│         │ Config & Create      │                           │
│         │ Error Report if      │                           │
│         │ Issues Found         │                           │
│         └──────────────────────┘                           │
│                                                             │
│  NOTE: Error reports are ONLY created for the merged       │
│  configuration, NOT for individual ARR/DEP broadcasts      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Airports with Split ATIS**: KATL, KCLE, KCLT, KCVG, KDEN, KDFW, KDTW, KMCO, KMIA, KMSP, KPHL, KPIT, KTPA

### 2. API Request Flow

```
┌─────────────┐
│ HTTP Request│ GET /api/runway/KSEA
└──────┬──────┘
       │
       ▼
┌────────────────────────┐
│ FastAPI Route Handler  │
└──────┬─────────────────┘
       │
       ▼
┌────────────────────────┐
│ Database Query         │ SELECT latest runway_config for KSEA
└──────┬─────────────────┘
       │
       ▼
┌────────────────────────┐
│ Join with ATIS Data    │ Get timestamp, info letter, etc.
└──────┬─────────────────┘
       │
       ▼
┌────────────────────────┐
│ Format Response        │ Convert to JSON response model
└──────┬─────────────────┘
       │
       ▼
┌────────────────────────┐
│ Return JSON            │ HTTP 200 with runway config data
└────────────────────────┘
```

### 3. Human Review Flow

```
┌──────────────────┐
│ User visits      │ http://localhost:8000/review
│ Review Dashboard │
└────┬─────────────┘
     │
     ▼
┌─────────────────────────────┐
│ Fetch Pending Items         │ Query error_reports WHERE:
│                             │ - reviewed = FALSE
└────┬────────────────────────┘
     │
     ▼
┌─────────────────────────────┐
│ Display ATIS + Parse Result │ Show original text + what parser found
└────┬────────────────────────┘
     │
     ├──▶ Option 1: Mark as Correct ──▶ UPDATE error_reports SET reviewed=TRUE
     │
     └──▶ Option 2: Submit Correction
          │
          ├──▶ UPDATE error_reports
          │    - corrected_arriving_runways
          │    - corrected_departing_runways
          │    - reviewer_notes, reviewed=TRUE
          │
          └──▶ Extract patterns ──▶ INSERT INTO parsing_corrections
               - atis_pattern (text to match)
               - correction_type (what was fixed)
               - success_rate (initialize to 1.0)
```

---

## Database Schema

### Entity Relationship Diagram

```
┌─────────────────┐
│   atis_data     │
│─────────────────│
│ id (PK)         │───┐
│ airport_code    │   │
│ collected_at    │   │
│ info_letter     │   │
│ datis_text      │   │
│ content_hash    │   │
│ is_changed      │   │
└─────────────────┘   │
                      │
                      │ 1:N
                      │
                      ▼
┌──────────────────────────┐
│   runway_configs         │
│──────────────────────────│
│ id (PK)                  │───┐
│ airport_code             │   │
│ atis_id (FK)             │◀──┘
│ arriving_runways (JSON)  │
│ departing_runways (JSON) │
│ traffic_flow             │
│ configuration_name       │
│ confidence_score         │
│ merged_from_pair (BOOL)  │  ◀── TRUE for combined ARR+DEP ATIS
│ component_confidence(JSON)│  ◀── {arrivals: X, departures: Y}
│ created_at               │
└────┬─────────────────────┘
     │
     │ 1:N (via trigger)
     │
     ▼
┌──────────────────────────┐
│   runway_changes         │
│──────────────────────────│
│ id (PK)                  │
│ airport_code             │
│ changed_at               │
│ from_config (JSON)       │
│ to_config (JSON)         │
│ atis_id_from (FK)        │
│ atis_id_to (FK)          │
└──────────────────────────┘

┌──────────────────────────┐
│   error_reports          │  (replaced human_reviews)
│──────────────────────────│
│ id (PK)                  │
│ airport_code             │
│ current_atis_id (FK)     │◀── References atis_data
│ paired_atis_id (FK)      │◀── For split ATIS pairs
│ parsed_arriving (JSON)   │
│ parsed_departing (JSON)  │
│ confidence_score         │
│ reported_by              │◀── 'user' or 'computer'
│ reported_at              │
│ reviewed                 │
│ reviewed_at              │
│ corrected_arriving(JSON) │
│ corrected_departing(JSON)│
│ reviewer_notes           │
└────┬─────────────────────┘
     │
     │ 1:N
     │
     ▼
┌──────────────────────────┐
│  parsing_corrections     │
│──────────────────────────│
│ id (PK)                  │
│ airport_code             │
│ atis_pattern (TEXT)      │
│ correction_type          │
│ expected_arriving (JSON) │
│ expected_departing(JSON) │
│ success_rate             │
│ times_applied            │
│ created_from_review_id   │
└──────────────────────────┘
```

### Key Tables Details

#### atis_data
**Purpose**: Store raw ATIS text with change detection
```sql
CREATE TABLE atis_data (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    collected_at TIMESTAMP NOT NULL DEFAULT NOW(),
    information_letter VARCHAR(1),
    datis_text TEXT NOT NULL,
    content_hash VARCHAR(32) NOT NULL,  -- MD5 hash
    is_changed BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_atis_airport_time ON atis_data(airport_code, collected_at DESC);
CREATE INDEX idx_atis_hash ON atis_data(airport_code, content_hash);
```

**Why MD5 hash?**
- Quickly detect if ATIS content changed
- Avoid parsing unchanged ATIS (saves CPU, reduces duplicate configs)
- Small footprint (32 chars vs full text comparison)

#### runway_configs
**Purpose**: Parsed runway configurations with confidence scores
```sql
CREATE TABLE runway_configs (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    atis_id INTEGER REFERENCES atis_data(id),
    arriving_runways JSONB DEFAULT '[]',  -- e.g., ["16L", "16R"]
    departing_runways JSONB DEFAULT '[]',
    traffic_flow VARCHAR(20),  -- NORTH, SOUTH, EAST, WEST, MIXED
    configuration_name VARCHAR(100),
    confidence_score DECIMAL(3,2) DEFAULT 0,  -- 0.00 to 1.00
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_runway_airport_time ON runway_configs(airport_code, created_at DESC);
CREATE INDEX idx_runway_confidence ON runway_configs(confidence_score);
```

**JSONB for runways?**
- Flexible array size (1-6+ runways possible)
- Queryable with PostgreSQL JSONB operators
- Easy to serialize to/from Python lists

#### runway_changes
**Purpose**: Track when runway configurations change
```sql
CREATE TABLE runway_changes (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    changed_at TIMESTAMP DEFAULT NOW(),
    from_config JSONB,  -- Previous config
    to_config JSONB,    -- New config
    atis_id_from INTEGER REFERENCES atis_data(id),
    atis_id_to INTEGER REFERENCES atis_data(id)
);

CREATE INDEX idx_changes_airport_time ON runway_changes(airport_code, changed_at DESC);
```

**Populated by trigger** (see Trigger Logic section below)

#### error_reports
**Purpose**: Store user-reported parsing errors for learning
```sql
CREATE TABLE error_reports (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4) NOT NULL,
    reported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reported_by VARCHAR(20) DEFAULT 'user',
    current_atis_id INTEGER NOT NULL REFERENCES atis_data(id),
    parsed_arriving_runways JSONB NOT NULL DEFAULT '[]',
    parsed_departing_runways JSONB NOT NULL DEFAULT '[]',
    confidence_score FLOAT NOT NULL DEFAULT 0.0,
    reviewed BOOLEAN NOT NULL DEFAULT FALSE,
    reviewed_at TIMESTAMP,
    corrected_arriving_runways JSONB,
    corrected_departing_runways JSONB,
    reviewer_notes TEXT
);

CREATE INDEX idx_error_reports_airport ON error_reports(airport_code);
CREATE INDEX idx_error_reports_reviewed ON error_reports(reviewed);
```

#### parsing_corrections
**Purpose**: Learned patterns from human corrections
```sql
CREATE TABLE parsing_corrections (
    id SERIAL PRIMARY KEY,
    airport_code VARCHAR(4),
    atis_pattern TEXT NOT NULL,  -- Pattern to match in ATIS text
    correction_type VARCHAR(50),  -- 'arriving', 'departing', 'both'
    expected_arriving JSONB,
    expected_departing JSONB,
    success_rate DECIMAL(3,2) DEFAULT 0,
    times_applied INTEGER DEFAULT 0,
    created_from_review_id INTEGER,  -- References error_reports.id
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_corrections_airport ON parsing_corrections(airport_code);
CREATE INDEX idx_corrections_success ON parsing_corrections(success_rate DESC);
```

---

## Trigger Logic

### runway_changes Trigger

**Purpose**: Automatically detect when runway configuration changes

```sql
CREATE OR REPLACE FUNCTION detect_runway_change()
RETURNS TRIGGER AS $$
DECLARE
    prev_config RECORD;
BEGIN
    -- Get the most recent config for this airport (excluding this new one)
    SELECT * INTO prev_config
    FROM runway_configs
    WHERE airport_code = NEW.airport_code
      AND id < NEW.id
    ORDER BY created_at DESC
    LIMIT 1;

    -- If previous config exists and is different
    IF FOUND AND (
        prev_config.arriving_runways != NEW.arriving_runways OR
        prev_config.departing_runways != NEW.departing_runways
    ) THEN
        INSERT INTO runway_changes (
            airport_code,
            changed_at,
            from_config,
            to_config,
            atis_id_from,
            atis_id_to
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
            prev_config.atis_id,
            NEW.atis_id
        );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER runway_change_trigger
    AFTER INSERT ON runway_configs
    FOR EACH ROW
    EXECUTE FUNCTION detect_runway_change();
```

**Why use a trigger?**
- Automatic change detection (no application logic needed)
- Guaranteed consistency (can't forget to log changes)
- Efficient (runs only on INSERT, not on queries)

---

## API Endpoints

### REST API Structure

#### Core Endpoints
| Method | Path | Description | Response Model |
|--------|------|-------------|----------------|
| GET | `/api/runway/{airport}` | Current runway config | RunwayConfig |
| GET | `/api/runways/all` | All airports | List[RunwayConfig] |
| GET | `/api/runway/{airport}/history` | Historical configs | List[RunwayConfig] |
| GET | `/api/airports` | List monitored airports | List[AirportStatus] |
| GET | `/api/status` | System health | SystemStatus |
| GET | `/health` | Health check | {"status": "healthy"} |

#### Dashboard Endpoints
| Method | Path | Description | Response Model |
|--------|------|-------------|----------------|
| GET | `/dashboard` | Monitoring dashboard HTML | HTML page |
| GET | `/api/dashboard/stats` | Dashboard statistics | DashboardStats |

#### Review System Endpoints
| Method | Path | Description | Response Model |
|--------|------|-------------|----------------|
| GET | `/review` | Review dashboard HTML | HTML page |
| GET | `/api/review/pending` | Items needing review | List[ReviewItem] |
| POST | `/api/review/submit` | Submit correction | {"message": "..."} |
| POST | `/api/review/skip` | Mark as correct | {"message": "..."} |
| GET | `/api/review/stats` | Review queue stats | ReviewStats |

### Response Models (Pydantic)

```python
class RunwayConfig(BaseModel):
    airport: str
    timestamp: datetime
    information_letter: Optional[str]
    arriving_runways: List[str]
    departing_runways: List[str]
    traffic_flow: Optional[str]
    configuration_name: Optional[str]
    confidence: float
    last_updated: datetime

class DashboardStats(BaseModel):
    total_airports: int
    active_airports: int
    activity_stats: ActivityStats
    parsing_stats: ParsingStats
    confidence_stats: ConfidenceStats
    recent_changes: List[RunwayChange]
    stale_airports: List[StaleAirport]

class ReviewItem(BaseModel):
    id: int
    airport_code: str
    atis_text: str
    information_letter: Optional[str]
    arriving_runways: List[str]
    departing_runways: List[str]
    confidence_score: float
    collected_at: datetime
```

---

## Docker Configuration

### Services Architecture

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:15-alpine
    container_name: runway_db
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./database_schema.sql:/docker-entrypoint-initdb.d/01-schema.sql
    # Database initializes with schema on first run

  collector:
    build:
      context: .
      dockerfile: Dockerfile.collector
    container_name: runway_collector
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      - DB_HOST=postgres  # Container hostname, not localhost!
      - DB_NAME=runway_detection
      - DB_USER=postgres
      - DB_PASSWORD=postgres
    volumes:
      - ./logs:/app/logs  # Only logs mounted, not source code
    # Runs cron in foreground, executes collector every 5 minutes

  api:
    build:
      context: .
      dockerfile: Dockerfile.api
    container_name: runway_api
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      - DB_HOST=postgres
      - DB_NAME=runway_detection
      - DB_USER=postgres
      - DB_PASSWORD=postgres
    ports:
      - "8000:8000"
    # No volume mounts - source code copied during build
    # Changes require rebuild: docker-compose up -d --build api
```

### Dockerfile.collector

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install cron + postgresql client
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    cron \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code (not mounted as volume)
COPY atis_collector.py .
COPY runway_parser.py .

# Create logs directory
RUN mkdir -p /app/logs

# Entrypoint script sets up cron with environment variables
RUN echo '#!/bin/sh\n\
# Export environment variables to a file for cron\n\
printenv | grep -E "^(DB_|PATH)" > /etc/environment\n\
# Setup cron job with environment that outputs to both log file and stdout\n\
echo "*/5 * * * * . /etc/environment; cd /app && /usr/local/bin/python /app/atis_collector.py 2>&1 | tee -a /app/logs/collector.log" | crontab -\n\
# Run once on startup\n\
python /app/atis_collector.py\n\
# Tail log file in background to show cron output in docker logs\n\
tail -F /app/logs/collector.log &\n\
# Start cron daemon in foreground\n\
cron -f' > /app/entrypoint.sh && chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
```

**Key Points**:
- Cron doesn't inherit Docker env vars, so we export them to `/etc/environment`
- Cron job sources environment before running collector
- `tee` outputs to both log file (mounted) and stdout (visible in `docker logs`)
- `tail -F` in background shows cron output in real-time

### Dockerfile.api

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code (not mounted as volume)
COPY runway_api.py .
COPY runway_parser.py .

EXPOSE 8000

# Run with --reload for development (watches for file changes)
# In production, remove --reload flag
CMD ["uvicorn", "runway_api:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

**Note**: `--reload` flag watches for file changes, but since files are copied (not mounted), changes require rebuild.

---

## Parser Implementation

### RunwayParser Class

```python
class RunwayParser:
    def __init__(self):
        self.arrival_keywords = [
            'APPROACH', 'LANDING', 'APCH RWY', 'ARRIVALS',
            'ILS.*RUNWAY', 'VISUAL.*RUNWAY', 'EXPECT.*APPROACH'
        ]
        self.departure_keywords = [
            'DEPARTURE', 'DEPARTING', 'TAKEOFF', 'DEP RWY', 'DEPG RWY'
        ]
        self.combined_keywords = [
            'RUNWAYS? IN USE', 'LANDING AND DEPARTING',
            'FOR ARRIVAL AND DEPARTURE'
        ]

    def parse(self, airport_code: str, atis_text: str, info_letter: str) -> RunwayConfig:
        # 1. Extract runway numbers (numeric and spelled-out)
        runways = self.extract_runways(atis_text)

        # 2. Classify runways (arrival, departure, or both)
        arriving = self.extract_arrivals(atis_text, runways)
        departing = self.extract_departures(atis_text, runways)

        # 3. Handle combined operations
        if not arriving and not departing:
            combined = self.extract_combined(atis_text, runways)
            arriving = combined
            departing = combined

        # 4. Calculate confidence score
        confidence = self.calculate_confidence(
            atis_text, arriving, departing
        )

        # 5. Determine traffic flow
        flow = self.determine_flow(arriving, departing)

        # 6. Generate configuration name
        config_name = self.generate_config_name(
            airport_code, arriving, departing, flow
        )

        return RunwayConfig(
            arriving_runways=arriving,
            departing_runways=departing,
            traffic_flow=flow,
            configuration_name=config_name,
            confidence_score=confidence
        )
```

### Confidence Scoring Algorithm

```python
def calculate_confidence(self, text: str, arriving: List[str], departing: List[str]) -> float:
    score = 0.0

    # Base score: Did we find any runways?
    if arriving or departing:
        score += 0.4

    # Keyword match bonus
    if any(kw in text.upper() for kw in self.arrival_keywords):
        score += 0.2
    if any(kw in text.upper() for kw in self.departure_keywords):
        score += 0.2

    # Clarity bonus: Unambiguous patterns
    if re.search(r'RUNWAY \d{2}[LCR]? APPROACH', text.upper()):
        score += 0.1  # Very clear arrival pattern
    if re.search(r'DEPARTING RUNWAY \d{2}[LCR]?', text.upper()):
        score += 0.1  # Very clear departure pattern

    # Consistency check: Logical runway numbers?
    if self.runways_are_valid(arriving, departing):
        score += 0.1

    return min(score, 1.0)  # Cap at 1.0
```

### Traffic Flow Determination

```python
def determine_flow(self, arriving: List[str], departing: List[str]) -> str:
    all_runways = arriving + departing
    if not all_runways:
        return None

    headings = [self.runway_to_heading(rwy) for rwy in all_runways]
    avg_heading = sum(headings) / len(headings)

    # Classify based on average heading
    if 340 <= avg_heading or avg_heading <= 20:
        return "NORTH"
    elif 160 <= avg_heading <= 200:
        return "SOUTH"
    elif 70 <= avg_heading <= 110:
        return "EAST"
    elif 250 <= avg_heading <= 290:
        return "WEST"
    else:
        return "MIXED"

def runway_to_heading(self, runway: str) -> int:
    # Extract numeric part (e.g., "16L" -> 16)
    match = re.match(r'(\d{2})', runway)
    if match:
        return int(match.group(1)) * 10  # 16 -> 160 degrees
    return 0
```

---

## Performance Considerations

### Database Indexing Strategy
- `airport_code` + `created_at DESC`: Fast "latest config" queries
- `confidence_score`: Filter low-confidence for review queue
- `content_hash`: Quick change detection

### Caching Opportunities (Future)
- Cache latest runway config per airport (Redis?)
- Cache dashboard stats for 30 seconds
- Cache review queue count

### Query Optimization
```sql
-- Efficient: Uses index on (airport_code, created_at DESC)
SELECT * FROM runway_configs
WHERE airport_code = 'KSEA'
ORDER BY created_at DESC
LIMIT 1;

-- Efficient: Uses index on reviewed
SELECT * FROM error_reports
WHERE reviewed = FALSE
LIMIT 20;
```

### Collection Efficiency
- Skip parsing if ATIS unchanged (hash comparison)
- Batch inserts possible, but currently one-by-one is fine for ~200 airports
- Connection pooling in FastAPI for API queries

---

## Monitoring and Observability

### Health Checks

```python
@app.get("/health")
async def health_check():
    try:
        # Check database connection
        cursor = db.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        return {"status": "healthy"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}, 503
```

### Key Metrics to Monitor
- Collection success rate (% of successful ATIS fetches)
- Parser confidence (average across all airports)
- API response time (p50, p95, p99)
- Review queue size (growing = parser issues)
- Database size (monitor growth rate)

### Log Levels
- **DEBUG**: Parser attempts, regex matches
- **INFO**: Collections, config changes, API requests
- **WARNING**: Low confidence parses, unusual patterns
- **ERROR**: Collection failures, database errors, exceptions

---

## Security Considerations

### Current State (Development)
- No authentication on API endpoints
- Database credentials in environment variables (not encrypted)
- No rate limiting
- CORS not configured (same-origin only)

### Production Recommendations
- API authentication (API keys or JWT)
- Rate limiting (per IP or per API key)
- Environment secrets management (AWS Secrets Manager, etc.)
- HTTPS/TLS for API endpoints
- Database connection encryption
- Input validation on all endpoints (Pydantic handles most)
- SQL injection protection (psycopg2 parameterized queries)

---

## Testing Strategy

### Current Coverage
- Manual testing with real ATIS data
- Human review system validates parser accuracy

### Recommended Tests

#### Unit Tests
```python
# Test parser with known ATIS samples
def test_parser_arrivals():
    parser = RunwayParser()
    config = parser.parse(
        "KSEA",
        "SEATTLE ATIS INFO C. RUNWAY 16L APPROACH.",
        "C"
    )
    assert "16L" in config.arriving_runways
    assert config.confidence_score >= 0.8

def test_parser_departures():
    # Similar test for departures
    pass

def test_parser_combined():
    # Test "RUNWAYS IN USE" pattern
    pass
```

#### Integration Tests
```python
# Test database operations
def test_collection_workflow():
    # Simulate full collection cycle
    # Verify ATIS stored, config parsed, triggers fired
    pass

def test_api_endpoint():
    client = TestClient(app)
    response = client.get("/api/runway/KSEA")
    assert response.status_code == 200
    assert "arriving_runways" in response.json()
```

#### Performance Tests
- Load test: 100+ concurrent API requests
- Collection test: Time to process all ~200 airports
- Database test: Query performance with months of data

---

## Deployment Checklist

### Initial Deployment
- [ ] PostgreSQL installed and configured
- [ ] Database schema initialized
- [ ] Docker Compose services running
- [ ] Collector running every 5 minutes (verify cron)
- [ ] API accessible on port 8000
- [ ] Dashboard loads and shows data
- [ ] Review system functional

### Configuration Verification
- [ ] Environment variables set correctly
- [ ] Database connectivity from all containers
- [ ] Logs directory mounted and writable
- [ ] Cron environment variables exported
- [ ] Timezone configured (UTC recommended)

### Ongoing Maintenance
- [ ] Monitor disk space (database + logs)
- [ ] Review error logs weekly
- [ ] Check review queue size (process corrections)
- [ ] Database backups configured
- [ ] Old data cleanup (90-day retention)

---

## Future Architecture Enhancements

### Short Term
- Add Redis caching for frequently accessed data
- Implement connection pooling for database
- Add automated testing pipeline (pytest + GitHub Actions)

### Medium Term
- Introduce message queue (RabbitMQ/Kafka) for async processing
- Separate read/write database connections
- Add Prometheus metrics export
- Implement WebSocket for real-time updates

### Long Term
- Microservices architecture (separate collector, parser, API)
- Event-driven architecture (events for ATIS changes, config updates)
- Machine learning service (separate container for ML model)
- Multi-region deployment (geographic redundancy)

---

**Last Updated**: 2025-11-14
**Version**: 1.0
**Maintainer**: Human + Claude Code collaboration
