# Runways in Use

Real-time airport runway configuration tracker for US airports, parsing D-ATIS (Digital Automatic Terminal Information Service) broadcasts to determine which runways are active for arrivals and departures.

**Created by [Llew Roberts](https://github.com/L13w)** with significant assistance from Claude Code.

![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue)
![Python](https://img.shields.io/badge/python-3.11-green)
![Docker](https://img.shields.io/badge/docker-ready-blue)

---

## The Story

### TL;DR (30 seconds)

Pilots and aviation enthusiasts often want to know which runways an airport is using, but this information isn't easily available through APIs. This project parses the free D-ATIS text broadcasts from US airports every 5 minutes and extracts the active runway configurations using regex pattern matching.

### The Medium Version (2 minutes)

Aviation weather and information services provide rich data about airports, but one critical piece of information has always been missing from public APIs: **which runways are currently in use**.

ATIS (Automatic Terminal Information Service) broadcasts contain this information, but it's buried in free-form text like:

```
"SEATTLE-TACOMA INTL ATIS INFO C 1853Z. RUNWAY 16L APPROACH IN USE.
DEPARTING RUNWAY 16L. NOTICE TO AIR MISSIONS... "
```

This project was born from a simple question: *"What runway is SEA using right now?"*

The solution involves:
1. **Data Collection**: Fetching D-ATIS data every 5 minutes from a public API
2. **Text Parsing**: Using 20+ regex patterns to extract runway information from varied ATIS phraseology
3. **Confidence Scoring**: Rating parse quality since ATIS formats vary wildly between airports
4. **Human Review**: A built-in correction system to improve accuracy over time

The result is a real-time dashboard showing runway configurations across 100+ US airports, complete with traffic flow direction (North/South/East/West) and historical data.

### The Full Story (5+ minutes)

#### The Problem

If you're a pilot, flight simulator enthusiast, or just someone who likes watching FlightRadar24, you've probably wondered: "Why is that plane landing from the north today when it usually comes from the south?"

The answer lies in runway configurations. Airports change which runways they use based on wind, traffic, noise abatement procedures, and other factors. This information is broadcast via ATIS - a continuous radio broadcast that pilots listen to before approaching an airport.

In the digital age, D-ATIS (Digital ATIS) made this information available as text through various services. But here's the catch: while you can get the raw ATIS text, no public API actually parses out the runway information. You'd have to read through paragraphs of weather data, NOTAMs, and other information to find a single line like "LANDING RUNWAY 28L."

#### The Journey

**Phase 1: Discovery**

The project started by exploring the [clowd.io D-ATIS API](https://datis.clowd.io/api/all), which provides free access to D-ATIS broadcasts for all US airports. The data was there - we just needed to extract the runway information.

**Phase 2: Pattern Recognition**

ATIS broadcasts don't follow a strict format. Different airports and different controllers phrase things differently:

- "RUNWAY 16L APPROACH IN USE"
- "LANDING AND DEPARTING RUNWAY 28L"
- "ARRIVALS RUNWAY 16C, DEPARTURES RUNWAY 16R"
- "EXPECT ILS RUNWAY 28L APPROACH"
- "RUNWAYS 16L 16C 16R IN USE"

We built a regex-based parser with 20+ patterns to handle these variations. Each pattern was crafted from real ATIS samples, and the parser assigns confidence scores based on how clearly it could identify the runways.

**Phase 3: The Split ATIS Problem**

Major airports like Denver (KDEN) and Cleveland (KCLE) use "split ATIS" - separate broadcasts for arrivals (ARR INFO) and departures (DEP INFO). This required special handling to pair the two broadcasts and merge their runway information.

**Phase 4: Human-in-the-Loop**

No regex parser is perfect. We added a human review system where:
- The system automatically flags low-confidence parses
- Users can report errors from the dashboard
- A review interface lets humans correct mistakes
- These corrections feed back to improve the system

**Phase 5: Deployment**

The system now runs continuously:
- PostgreSQL database for storing ATIS history and runway configurations
- Collector service running every 5 minutes via cron
- FastAPI server providing the REST API and dashboard
- All containerized with Docker for easy deployment

#### Technical Challenges Solved

1. **Runway Naming Conventions**: Runways are named by magnetic heading (e.g., Runway 16 faces 160 degrees). Parallel runways get L/C/R suffixes. The parser handles all variations.

2. **Traffic Flow Detection**: By analyzing runway headings, the system determines if the airport is in North Flow, South Flow, etc.

3. **Change Detection**: Using MD5 hashing to only store data when ATIS actually changes, preventing database bloat.

4. **Reciprocal Runway Detection**: The system warns when parsed data contains reciprocal runways (e.g., 16 and 34), which would be physically impossible to use simultaneously.

---

## Screenshots

<!-- TODO: Add screenshots of the dashboard -->
*Dashboard showing real-time runway configurations*

<!-- TODO: Add screenshot of ATIS detail view -->
*Detailed view with ATIS text and parsed runways*

<!-- TODO: Add screenshot of review interface -->
*Human review interface for corrections*

---

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   D-ATIS API    │────▶│    Collector     │────▶│   PostgreSQL    │
│  (clowd.io)     │     │  (every 5 min)   │     │    Database     │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                               ┌──────────────────────────┘
                               │
                               ▼
                        ┌─────────────────┐
                        │   FastAPI App   │
                        │   (runway_api)  │
                        └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
                    ▼            ▼            ▼
              ┌─────────┐  ┌─────────┐  ┌─────────┐
              │Dashboard│  │   API   │  │ Review  │
              │  (Vue)  │  │Endpoints│  │Interface│
              └─────────┘  └─────────┘  └─────────┘
```

### Components

- **Collector** (`atis_collector.py`): Fetches D-ATIS data every 5 minutes, stores in database
- **Parser** (`runway_parser.py`): Extracts runway information using regex patterns
- **API** (`runway_api.py`): FastAPI application providing REST endpoints
- **Dashboard** (`dashboard.html`): Vue.js 3 single-page application
- **Database**: PostgreSQL with change detection triggers

---

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Git

### Installation

1. Clone the repository:
```bash
git clone https://github.com/L13w/runways-in-use-public.git
cd runways-in-use-public
```

2. Copy the environment template:
```bash
cp .env.example .env
```

3. (Optional) Edit `.env` to change default passwords

4. Start the services:
```bash
docker-compose up -d
```

5. Access the dashboard at http://localhost:8000

### First Run

On first startup:
- PostgreSQL will initialize with the database schema
- The collector will fetch initial ATIS data
- The dashboard will show airports as they're populated

Data collection runs every 5 minutes automatically.

---

## API Reference

Base URL: `http://localhost:8000/api/v1`

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/airports` | GET | List all monitored airports |
| `/runway/{code}` | GET | Get current runway config for an airport |
| `/runway/{code}/history` | GET | Get runway change history |
| `/runway/{code}/reports` | GET | Get recent ATIS reports with full text |
| `/runways/all` | GET | Get all airports' current configurations |
| `/status` | GET | System health and statistics |
| `/report-error/{code}` | POST | Report a parsing error |

### Example Response

```json
{
  "airport": "KSEA",
  "timestamp": "2024-01-15T18:53:00",
  "information_letter": "C",
  "arriving_runways": ["16L", "16C"],
  "departing_runways": ["16L", "16R"],
  "traffic_flow": "SOUTH",
  "configuration_name": "South Flow",
  "confidence": 0.95,
  "last_updated": "2024-01-15T18:50:00"
}
```

---

## Parser Details

### How Runway Parsing Works

The parser uses multiple regex patterns to extract runway information:

**Arrival Patterns:**
```
RUNWAY 16L APPROACH
LANDING RUNWAY 34R
EXPECT ILS RUNWAY 28L APPROACH
ARRIVALS RUNWAY 16C
```

**Departure Patterns:**
```
DEPARTING RUNWAY 34R
DEPARTURE RUNWAY 16L
TAKEOFF RUNWAY 28R
```

**Combined Patterns:**
```
LANDING AND DEPARTING RUNWAY 16C
RUNWAYS 28L 28R IN USE
```

### Confidence Scoring

Each parse gets a confidence score (0.0 - 1.0):

- **1.0 (100%)**: Clear, unambiguous patterns matched
- **0.5 - 0.9**: Some ambiguity or partial matches
- **< 0.5**: Multiple interpretations possible
- **0.0**: No patterns matched

### Traffic Flow

The system determines traffic flow direction from runway headings:

| Flow | Runway Range | Example |
|------|--------------|---------|
| NORTH | 34, 35, 36, 01, 02 | Runway 34L = North Flow |
| SOUTH | 16, 17, 18, 19, 20 | Runway 16C = South Flow |
| EAST | 07, 08, 09, 10, 11 | Runway 09R = East Flow |
| WEST | 25, 26, 27, 28, 29 | Runway 28L = West Flow |

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `localhost` | Database hostname |
| `DB_NAME` | `runway-detection` | Database name |
| `DB_USER` | `postgres` | Database username |
| `DB_PASSWORD` | `postgres` | Database password |
| `DB_PORT` | `5432` | Database port |
| `DB_SSLMODE` | (none) | SSL mode for cloud databases |

---

## Development

### Local Development

1. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
.\venv\Scripts\activate  # Windows
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Start PostgreSQL (via Docker):
```bash
docker-compose up -d postgres
```

4. Run the API:
```bash
uvicorn runway_api:app --reload --port 8000
```

5. Run the collector manually:
```bash
python atis_collector.py
```

### Rebuilding Containers

After code changes:
```bash
docker-compose up -d --build api
docker-compose up -d --build collector
```

---

## Trivia & Fun Facts

### ATIS Information Letters

ATIS broadcasts cycle through letters A-Z (skipping letters that sound similar to others). When they reach Z, they wrap back to A. The letter changes whenever the ATIS content changes.

### Why "16L" and not "160"?

Runway numbers are magnetic headings divided by 10. Runway 16L faces approximately 160 degrees magnetic. The "L" means it's the left runway when you have parallel runways (L = Left, C = Center, R = Right).

### The Opposite Direction Mystery

Sometimes you'll see airports briefly report both 16 and 34 runways (opposite ends of the same physical runway). This usually indicates a transition period or special operations.

### Split ATIS Airports

Some busy airports have separate ATIS for arrivals and departures. This allows different controllers to update their relevant information without affecting the other. The system handles this by pairing ARR INFO and DEP INFO broadcasts.

---

## Contributing

This project uses the PolyForm Noncommercial license. You're welcome to:

- Use it for personal, educational, or research purposes
- Modify and distribute for non-commercial use
- Submit issues and pull requests

Commercial use requires a separate license agreement.

---

## License

[PolyForm Noncommercial License 1.0.0](LICENSE)

This project is free for non-commercial use including:
- Personal projects and hobby use
- Educational and research purposes
- Non-profit organizations

Commercial use requires licensing. Contact the author for details.

---

## Acknowledgments

- **[clowd.io](https://datis.clowd.io/)** for providing free D-ATIS data
- **Claude Code** by Anthropic for significant development assistance
- The aviation community for their feedback and testing

---

## Disclaimer

This tool is for informational purposes only. Always verify runway information through official channels before making any flight-related decisions. The accuracy of parsed data is not guaranteed.
