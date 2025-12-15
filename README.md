# Runways in Use

A real-time system that parses D-ATIS data to determine active runway configurations at US airports, filling what I have always considered to be a gap in aviation data APIs.

## Problem Statement

While weather data is readily available via API, runway direction information (which runways are active for arrivals/departures) is not available through any existing API. Wind direction does not always indicate which runways are active. This system solves that by:
- Collecting D-ATIS data every 5 minutes from 76 major US airports
- Parsing runway information using pattern matching and machine learning
- Providing a REST API and web dashboard for runway configuration queries

## About This Repository

This repository contains a stable, self-contained version of the system that you can run locally or deploy yourself. It uses regex-based parsing and is intentionally frozen to keep the setup straightforward.

**If you just want runway data, use the free API at [runwaysinuse.com](https://runwaysinuse.com)**—no setup required.

The live service includes additional features not in this repo:
- **ML-enhanced parsing** (T5-small model trained on 6,837+ samples, 96.4% accuracy)
- **Human review system** for continuous accuracy improvement
- **CDN caching** and production optimizations
- **Metrics dashboard** with system performance data

Use this repo if you want to run your own instance or understand how the system works.

---

## Live Service

**Dashboard**: [runwaysinuse.com/dashboard](https://runwaysinuse.com/dashboard)
**API Documentation**: [runwaysinuse.com/docs](https://runwaysinuse.com/docs)

The service collects D-ATIS data every 5 minutes from 76 major US airports and parses runway assignments automatically.

### API Usage

#### Get All Airports
```bash
curl https://runwaysinuse.com/api/v1/airports
```

#### Get Runway Configuration
```bash
curl https://runwaysinuse.com/api/v1/runway/KSEA
```

#### Response Example
```json
{
  "airport": "KSEA",
  "timestamp": "2025-12-14T20:15:00Z",
  "information_letter": "J",
  "arriving_runways": ["16L", "16R"],
  "departing_runways": ["16L"],
  "traffic_flow": "SOUTH",
  "configuration_name": "South Flow",
  "confidence": 1.0,
  "last_updated": "2025-12-13T13:00:01Z"
}
```

#### All Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/airports` | All airports with current configs |
| `GET /api/v1/runway/{code}` | Single airport runway config |
| `GET /api/v1/runway/{code}/history` | Configuration change history |
| `GET /api/v1/runways` | All current runway configs |
| `GET /api/v1/status` | System health and statistics |
| `GET /health` | Health check |

Full API documentation with interactive examples: [runwaysinuse.com/docs](https://runwaysinuse.com/docs)

### Dashboards

**Main Dashboard** — [runwaysinuse.com/dashboard](https://runwaysinuse.com/dashboard)

Real-time view of all monitored airports showing current runway configurations, traffic flow directions, time since last configuration change, and parsing confidence levels.

**Metrics Dashboard** — [runwaysinuse.com/metrics](https://runwaysinuse.com/metrics) *(production only)*

System performance metrics including parsing accuracy, data freshness, and collection statistics. This dashboard is not included in the self-hosted version.

---

## Self-Hosting

### Quick Start (Docker Compose)

```bash
# Clone the repository
git clone <your-repo-url>
cd runway-detection

# Start all services
docker-compose up -d

# View logs
docker-compose logs -f

# Access the API
curl http://localhost:8000/api/v1/runway/KSEA
```

### Local API Endpoints

The self-hosted version uses the same `/api/v1/` endpoints as the live service.

```bash
# Get runway configuration
GET /api/v1/runway/{airport_code}
curl http://localhost:8000/api/v1/runway/KSEA

# Get all airports
GET /api/v1/runways/all

# Get runway history
GET /api/v1/runway/{airport_code}/history?hours=24

# List monitored airports
GET /api/v1/airports

# System status
GET /api/v1/status
```

#### Response Example
```json
{
  "airport": "KSEA",
  "timestamp": "2024-11-13T18:30:00Z",
  "information_letter": "C",
  "arriving_runways": ["16L", "16C", "16R"],
  "departing_runways": ["16L", "16C", "16R"],
  "traffic_flow": "SOUTH",
  "configuration_name": "South Flow",
  "confidence": 0.9,
  "last_updated": "2024-11-13T18:33:00Z"
}
```

### Environment Variables

```bash
DB_HOST=localhost
DB_NAME=runway-detection
DB_USER=postgres
DB_PASSWORD=postgres
DB_PORT=5432
```

### Local Dashboards

**Monitoring Dashboard** — `http://localhost:8000/dashboard`

- System overview: total airports, active status, parsing success rates
- Activity stats: updates tracked over last hour, day, week, month
- Runway changes: real-time feed of configuration changes
- Low confidence alerts: airports where parsing confidence is < 100%
- Stale airport detection: alerts for airports with no updates in 3+ hours
- Auto-refresh every 30 seconds

**Human Review Dashboard** — `http://localhost:8000/review`

An interactive interface for reviewing and correcting parsing errors. This is how you train the system to improve accuracy.

**Queue Prioritization:**
- Low confidence parses (< 100%)
- Results with empty runway arrays
- Failed parsing attempts

**Review Workflow:**
1. System displays ATIS text with current parse results
2. Reviewer corrects arriving/departing runway fields
3. Optional notes for context
4. Two actions: "Mark as Correct" (skip if accurate) or "Submit Correction"

**Learning System:**
When you submit a correction:
1. Original and corrected data stored in `error_reports` table
2. Patterns extracted from ATIS text and stored in `parsing_corrections` table
3. System builds knowledge base of successful corrections
4. Success rates tracked for each learned pattern

```
┌─────────────────┐
│   ATIS Data     │
│   Collected     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Parse Runways  │◄────────┐
│  (confidence)   │         │
└────────┬────────┘         │
         │                  │
         ▼                  │
┌─────────────────┐         │
│ Low Confidence? │         │
│   Empty Data?   │         │
└────────┬────────┘         │
         │ YES              │
         ▼                  │
┌─────────────────┐         │
│ Human Reviews   │         │
│  & Corrects     │         │
└────────┬────────┘         │
         │                  │
         ▼                  │
┌─────────────────┐         │
│ Store Pattern   │         │
│  in Database    │─────────┘
└─────────────────┘
   Improves Future
     Parsing
```

### Training & Improvement

**Data Requirements:**
- Minimum: 2-3 weeks (4,000+ samples)
- Robust: 1-2 months (17,000+ samples)
- Seasonal coverage: 3+ months (captures wind patterns)

**Model Evolution Path:**
1. Current (this repo): Rule-based regex patterns (85-90% accuracy)
2. With human review: Learned corrections improve accuracy
3. Next: NLP with spaCy + learned patterns (90-95% accuracy)
4. Production (runwaysinuse.com): Fine-tuned T5-small (96.4% accuracy)

**Accuracy Monitoring:**
```sql
-- Check parser accuracy
SELECT
    airport_code,
    AVG(confidence_score) as avg_confidence,
    COUNT(*) as samples
FROM runway_configs
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY airport_code
ORDER BY avg_confidence DESC;

-- View error reports and corrections
SELECT
    airport_code,
    COUNT(*) as total_reports,
    COUNT(CASE WHEN reviewed THEN 1 END) as reviewed_count,
    COUNT(CASE WHEN corrected_arriving_runways IS NOT NULL THEN 1 END) as corrections_made
FROM error_reports
GROUP BY airport_code
ORDER BY total_reports DESC;

-- Check learned patterns
SELECT
    airport_code,
    COUNT(*) as patterns_learned,
    AVG(success_rate) as avg_success_rate
FROM parsing_corrections
GROUP BY airport_code
HAVING COUNT(*) > 0
ORDER BY patterns_learned DESC;
```

### Database Maintenance

```sql
-- Clean old data (>90 days)
DELETE FROM atis_data
WHERE collected_at < NOW() - INTERVAL '90 days';

-- Analyze runway usage patterns
SELECT * FROM get_runway_usage_stats('KSEA', 30);
```

---

## Technical Details

### Data Collection

- Fetches D-ATIS from [datis.clowd.io](https://datis.clowd.io/api/all) every 5 minutes
- Covers 76 major US airports
- Detects changes using content hashing to minimize storage

### Parsing

The system uses pattern matching to identify runway assignments:

**Arrival Patterns:** "APPROACH", "LANDING", "ILS RWY", "VISUAL RWY", "APCH RWY"
**Departure Patterns:** "DEPARTURE", "TAKEOFF", "DEP RWY"
**Combined Patterns:** "RWYS IN USE", "LANDING AND DEPARTING"

The live service adds an ML fallback (T5-small) for edge cases the regex patterns miss.

### Traffic Flow Detection

Runway headings are converted to cardinal flow directions:
- **North Flow**: Runways 34, 35, 36, 01, 02 (340°-020°)
- **South Flow**: Runways 16, 17, 18, 19, 20 (160°-200°)
- **East Flow**: Runways 07, 08, 09, 10, 11 (070°-110°)
- **West Flow**: Runways 25, 26, 27, 28, 29 (250°-290°)

### Split-ATIS Airports

Some major airports publish separate arrival and departure ATIS broadcasts:
- **ARR INFO**: Contains only arrival runway assignments
- **DEP INFO**: Contains only departure runway assignments

The system automatically merges these into a single configuration. Split-ATIS airports include:
KATL, KCLE, KCLT, KCVG, KDEN, KDFW, KDTW, KMCO, KMIA, KMSP, KPHL, KPIT, KTPA

### Airport-Specific Notes

**Seattle (KSEA)**
- South Flow: 16L, 16C, 16R (most common)
- North Flow: 34L, 34C, 34R (strong south winds)

**San Francisco (KSFO)**
- West Flow: 28L, 28R (typical)
- Southeast Flow: 19L, 19R (rare)
- Often uses crossing runways (28s and 1s)

**Los Angeles (KLAX)**
- West Flow: 24L, 24R, 25L, 25R
- East Flow: 06L, 06R, 07L, 07R
- Complex quad-parallel operations

### Known Limitations

1. **Pattern Variations**: Some airports use non-standard ATIS phrasing
2. **Special Operations**: May miss "opposite direction ops" or emergency configs
3. **Closed Runways**: Currently doesn't track runway closures
4. **International**: Only supports US airports and territories (ICAO prefixes K, P, and TJ)

---

## Contributing

**Use the Human Review Dashboard:**
1. Visit `http://localhost:8000/review`
2. Review items with low confidence or missing data
3. Correct runway assignments
4. Your corrections automatically improve future parsing

**Other Contributions:**
- Collect ATIS samples with unusual patterns
- Add regex patterns for new phrases
- Test with diverse airport configurations
- Report parsing issues via GitHub

---

## License

Licensed under [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/).

## Acknowledgments

- D-ATIS data provided by [datis.clowd.io](https://datis.clowd.io)
- Inspired by the lack of runway direction APIs
- Aviation community for ATIS format documentation

## Support

For issues or questions:
- Open an issue on GitHub
- API documentation: [runwaysinuse.com/docs](https://runwaysinuse.com/docs) or `http://localhost:8000/docs`

---

**Note**: This system is for informational purposes only. Always verify runway information through official aviation sources for operational use.
