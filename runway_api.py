#!/usr/bin/env python3
"""
Runway Detection API
FastAPI server providing runway configuration information
"""

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import logging
import json

from runway_parser import RunwayParser, RunwayConfiguration

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database configuration
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'database': os.getenv('DB_NAME', 'runway-detection'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', 'postgres'),
    'port': os.getenv('DB_PORT', '5432'),
}

# Add SSL mode if specified (required for cloud databases like Azure)
if os.getenv('DB_SSLMODE'):
    DB_CONFIG['sslmode'] = os.getenv('DB_SSLMODE')

# Initialize FastAPI app
app = FastAPI(
    title="Runways in Use API",
    description="Real-time airport runway configuration information",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize runway parser
parser = RunwayParser()

# Response models
class RunwayResponse(BaseModel):
    airport: str
    timestamp: str
    information_letter: Optional[str]
    arriving_runways: List[str]
    departing_runways: List[str]
    traffic_flow: str
    configuration_name: Optional[str]
    confidence: float
    last_updated: str

class RunwayHistoryItem(BaseModel):
    timestamp: str
    information_letter: Optional[str]
    arriving_runways: List[str]
    departing_runways: List[str]
    traffic_flow: str
    configuration_name: Optional[str]
    duration_minutes: Optional[int]

class AtisReport(BaseModel):
    timestamp: str
    information_letter: Optional[str]
    datis_text: str
    arriving_runways: List[str]
    departing_runways: List[str]
    traffic_flow: str
    confidence: float

class AirportSummary(BaseModel):
    airport: str
    name: str
    city: Optional[str] = None
    state: Optional[str] = None
    current_config: Optional[RunwayResponse]
    status: str  # "active", "no_data", "stale"

class SystemStatus(BaseModel):
    status: str
    airports_monitored: int
    airports_active: int
    last_collection: Optional[str]
    database_status: str

class DashboardStats(BaseModel):
    current_time: str
    total_airports: int
    active_airports: int
    stale_airports: List[Dict]  # Airports with no updates in 3+ hours
    parsing_stats: Dict  # Success/failure rates
    confidence_stats: Dict  # Average confidence by airport
    activity_stats: Dict  # Updates by time period (hour, day, week, month)
    recent_changes: List[Dict]  # Recent runway config changes

class AirportStatus(BaseModel):
    airport_code: str
    arriving: List[str]
    departing: List[str]
    flow: str
    last_change: str
    recent_changes: List[Any]  # 4 most recent changes

class ReviewItem(BaseModel):
    """Legacy model for old review endpoints - will be removed when review page is updated"""
    id: int
    atis_id: int
    airport_code: str
    atis_text: str
    original_arriving: List[str]
    original_departing: List[str]
    confidence: float
    collected_at: str
    issue_type: str
    merged_from_pair: bool = False
    component_confidence: Optional[Dict[str, float]] = None
    has_reciprocal_runways: bool = False
    is_incomplete_pair: bool = False
    warnings: List[str] = []
    reviewed: bool = False
    corrected_arriving: Optional[List[str]] = None
    corrected_departing: Optional[List[str]] = None
    reviewer_notes: Optional[str] = None

class ErrorReport(BaseModel):
    id: int
    airport_code: str
    reported_at: str
    current_atis: Dict[str, Any]  # Full ATIS data
    paired_atis: Optional[Dict[str, Any]]  # Paired DEP/ARR report if applicable
    parsed_arriving: List[str]
    parsed_departing: List[str]
    confidence: float
    reviewed: bool
    reviewed_at: Optional[str]
    corrected_arriving: Optional[List[str]]
    corrected_departing: Optional[List[str]]
    reviewer_notes: Optional[str]

class ReviewSubmission(BaseModel):
    corrected_arriving: List[str]
    corrected_departing: List[str]
    notes: Optional[str] = None
    reviewed_by: Optional[str] = 'human_reviewer'

class ReviewStats(BaseModel):
    total_reports: int
    unreviewed_count: int
    reviewed_count: int
    by_source: Dict[str, int]  # Breakdown by reported_by field


# Database connection helper
def get_db_connection():
    """Create database connection"""
    try:
        return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        raise HTTPException(status_code=500, detail="Database connection failed")

# Helper functions for review queue
def detect_reciprocal_runways(runways: List[str]) -> bool:
    """
    Detect if list contains reciprocal runways (opposite ends of same runway)
    Reciprocals differ by 18 (180 degrees)
    Examples: 09/27, 18/36, 16/34
    """
    if not runways or len(runways) < 2:
        return False

    # Extract runway numbers (without L/C/R suffix)
    runway_numbers = []
    for rwy in runways:
        import re
        match = re.match(r'([0-9]{1,2})', rwy)
        if match:
            runway_numbers.append(int(match.group(1)))

    # Check all pairs for reciprocals
    for i in range(len(runway_numbers)):
        for j in range(i + 1, len(runway_numbers)):
            diff = abs(runway_numbers[i] - runway_numbers[j])
            if diff == 18:  # Reciprocal runways
                return True

    return False

def get_latest_configs_per_airport(conn):
    """
    Get the most current runway config for each airport with real-time split ATIS pairing.

    For split ATIS airports (DEP INFO / ARR INFO):
      - Find latest ARR INFO config
      - Find latest DEP INFO config
      - If both within 15 minutes: merge them
      - If only one: return it with incomplete pair warning

    For normal ATIS airports:
      - Return latest config

    Returns: List of dicts with config data + warnings
    """
    cursor = conn.cursor()

    # Get all unreviewed configs from last 6 hours, grouped by airport
    cursor.execute("""
        WITH unreviewed_configs AS (
            SELECT
                rc.id,
                rc.airport_code,
                rc.atis_id,
                rc.arriving_runways,
                rc.departing_runways,
                rc.confidence_score,
                rc.merged_from_pair,
                rc.component_confidence,
                rc.created_at,
                ad.datis_text,
                ad.collected_at,
                ad.datis_text LIKE '%DEP INFO%' as is_dep_info,
                ad.datis_text LIKE '%ARR INFO%' as is_arr_info
            FROM runway_configs rc
            JOIN atis_data ad ON rc.atis_id = ad.id
            LEFT JOIN human_reviews hr ON rc.id = hr.runway_config_id
            WHERE hr.id IS NULL
              AND (rc.confidence_score < 1.0
                   OR rc.arriving_runways::text = '[]'
                   OR rc.departing_runways::text = '[]')
              AND ad.collected_at > NOW() - INTERVAL '6 hours'
        )
        SELECT * FROM unreviewed_configs
        ORDER BY airport_code, created_at DESC
    """)

    all_configs = cursor.fetchall()

    # Group by airport
    airports = {}
    for config in all_configs:
        airport = config['airport_code']
        if airport not in airports:
            airports[airport] = []
        airports[airport].append(config)

    # Process each airport
    result_configs = []

    for airport_code, configs in airports.items():
        # Check if this is a split ATIS airport
        has_dep_info = any(c['is_dep_info'] for c in configs)
        has_arr_info = any(c['is_arr_info'] for c in configs)
        is_split_atis = has_dep_info or has_arr_info

        if is_split_atis:
            # Find latest ARR and DEP configs
            arr_configs = [c for c in configs if c['is_arr_info']]
            dep_configs = [c for c in configs if c['is_dep_info']]

            latest_arr = arr_configs[0] if arr_configs else None
            latest_dep = dep_configs[0] if dep_configs else None

            # Try to pair them if both exist and within 15 minutes
            if latest_arr and latest_dep:
                time_diff = abs((latest_arr['collected_at'] - latest_dep['collected_at']).total_seconds() / 60)

                if time_diff <= 15:
                    # Merge them - use latest as base
                    if latest_arr['created_at'] >= latest_dep['created_at']:
                        merged = dict(latest_arr)
                        merged['departing_runways'] = latest_dep['departing_runways']
                        merged['atis_text'] = f"ARR: {latest_arr['datis_text'][:100]}... | DEP: {latest_dep['datis_text'][:100]}..."
                        merged['merged_from_pair'] = True
                        merged['is_incomplete_pair'] = False
                    else:
                        merged = dict(latest_dep)
                        merged['arriving_runways'] = latest_arr['arriving_runways']
                        merged['atis_text'] = f"ARR: {latest_arr['datis_text'][:100]}... | DEP: {latest_dep['datis_text'][:100]}..."
                        merged['merged_from_pair'] = True
                        merged['is_incomplete_pair'] = False

                    result_configs.append(merged)
                else:
                    # Too far apart - show the latest one with warning
                    latest = latest_arr if latest_arr['created_at'] >= latest_dep['created_at'] else latest_dep
                    latest = dict(latest)
                    latest['is_incomplete_pair'] = True
                    result_configs.append(latest)
            elif latest_arr:
                # Only ARR INFO available
                latest_arr = dict(latest_arr)
                latest_arr['is_incomplete_pair'] = True
                result_configs.append(latest_arr)
            elif latest_dep:
                # Only DEP INFO available
                latest_dep = dict(latest_dep)
                latest_dep['is_incomplete_pair'] = True
                result_configs.append(latest_dep)
        else:
            # Normal ATIS - just get latest
            latest = dict(configs[0])
            latest['is_incomplete_pair'] = False
            result_configs.append(latest)

    return result_configs

# API Endpoints
@app.get("/")
async def root():
    """Redirect to the dashboard"""
    return RedirectResponse(url="/dashboard")

@app.get("/api/v1", response_model=Dict)
async def api_info():
    """API root endpoint with basic information"""
    return {
        "name": "Runways in Use API",
        "version": "1.0.0",
        "endpoints": {
            "/dashboard": "Real-time monitoring dashboard",
            "/review": "Human review dashboard for corrections",
            "/api/v1/runway/{airport_code}": "Get current runway configuration",
            "/api/v1/runways/all": "Get all airports' runway configurations",
            "/api/v1/runway/{airport_code}/history": "Get runway configuration history",
            "/api/v1/airports": "List all monitored airports",
            "/api/v1/status": "System status",
            "/docs": "Interactive API documentation"
        }
    }

@app.get("/api/v1/runway/{airport_code}", response_model=RunwayResponse)
async def get_runway_status(airport_code: str):
    """Get current runway configuration for an airport"""

    airport_code = airport_code.upper()
    if not airport_code.startswith('K'):
        airport_code = 'K' + airport_code

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get latest ATIS data
        cursor.execute("""
            SELECT airport_code, collected_at, information_letter, datis_text
            FROM atis_data
            WHERE airport_code = %s
            ORDER BY collected_at DESC
            LIMIT 1
        """, (airport_code,))

        result = cursor.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail=f"No data available for {airport_code}")

        # Check if data is stale (>30 minutes old)
        age_minutes = (datetime.utcnow() - result['collected_at']).total_seconds() / 60
        if age_minutes > 30:
            logger.warning(f"Data for {airport_code} is {age_minutes:.1f} minutes old")

        # Parse runway configuration
        config = parser.parse(
            airport_code,
            result['datis_text'],
            result['information_letter']
        )

        # Store parsed configuration
        cursor.execute("""
            INSERT INTO runway_configs
            (airport_code, atis_id, arriving_runways, departing_runways,
             traffic_flow, configuration_name, confidence_score)
            SELECT %s,
                   (SELECT id FROM atis_data WHERE airport_code = %s ORDER BY collected_at DESC LIMIT 1),
                   %s, %s, %s, %s, %s
            ON CONFLICT (airport_code, atis_id) DO NOTHING
        """, (
            airport_code,
            airport_code,
            json.dumps(config.arriving_runways),
            json.dumps(config.departing_runways),
            config.traffic_flow,
            config.configuration_name,
            config.confidence_score
        ))
        conn.commit()

        return RunwayResponse(
            airport=config.airport_code,
            timestamp=config.timestamp.isoformat(),
            information_letter=config.information_letter,
            arriving_runways=config.arriving_runways,
            departing_runways=config.departing_runways,
            traffic_flow=config.traffic_flow,
            configuration_name=config.configuration_name,
            confidence=config.confidence_score,
            last_updated=result['collected_at'].isoformat()
        )

    finally:
        conn.close()

@app.get("/api/v1/runways/all", response_model=List[RunwayResponse])
async def get_all_runways():
    """Get runway configurations for all monitored airports"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get latest ATIS for each airport
        cursor.execute("""
            SELECT DISTINCT ON (airport_code)
                   airport_code, collected_at, information_letter, datis_text
            FROM atis_data
            WHERE collected_at > NOW() - INTERVAL '1 hour'
            ORDER BY airport_code, collected_at DESC
        """)

        results = cursor.fetchall()
        runway_configs = []

        for result in results:
            try:
                config = parser.parse(
                    result['airport_code'],
                    result['datis_text'],
                    result['information_letter']
                )

                runway_configs.append(RunwayResponse(
                    airport=config.airport_code,
                    timestamp=config.timestamp.isoformat(),
                    information_letter=config.information_letter,
                    arriving_runways=config.arriving_runways,
                    departing_runways=config.departing_runways,
                    traffic_flow=config.traffic_flow,
                    configuration_name=config.configuration_name,
                    confidence=config.confidence_score,
                    last_updated=result['collected_at'].isoformat()
                ))
            except Exception as e:
                logger.error(f"Error parsing {result['airport_code']}: {e}")
                continue

        return runway_configs

    finally:
        conn.close()

@app.get("/api/v1/runway/{airport_code}/history", response_model=List[RunwayHistoryItem])
async def get_runway_history(
    airport_code: str,
    hours: int = Query(default=24, ge=1, le=168)  # Max 1 week
):
    """Get runway configuration changes over time"""

    airport_code = airport_code.upper()
    if not airport_code.startswith('K'):
        airport_code = 'K' + airport_code

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get ATIS changes (only records where content changed)
        cursor.execute("""
            SELECT collected_at, information_letter, datis_text
            FROM atis_data
            WHERE airport_code = %s
              AND collected_at > NOW() - INTERVAL '%s hours'
              AND is_changed = true
            ORDER BY collected_at DESC
        """, (airport_code, hours))

        results = cursor.fetchall()

        if not results:
            return []

        history = []
        prev_timestamp = None

        for i, result in enumerate(results):
            config = parser.parse(
                airport_code,
                result['datis_text'],
                result['information_letter']
            )

            # Calculate duration if we have a previous timestamp
            duration = None
            if prev_timestamp:
                duration = int((prev_timestamp - result['collected_at']).total_seconds() / 60)

            history.append(RunwayHistoryItem(
                timestamp=result['collected_at'].isoformat(),
                information_letter=config.information_letter,
                arriving_runways=config.arriving_runways,
                departing_runways=config.departing_runways,
                traffic_flow=config.traffic_flow,
                configuration_name=config.configuration_name,
                duration_minutes=duration
            ))

            prev_timestamp = result['collected_at']

        return history

    finally:
        conn.close()

@app.get("/api/v1/runway/{airport_code}/reports", response_model=List[AtisReport])
async def get_atis_reports(
    airport_code: str,
    limit: int = Query(default=4, ge=1, le=20)
):
    """Get recent ATIS reports with full text for an airport"""

    airport_code = airport_code.upper()
    if not airport_code.startswith('K'):
        airport_code = 'K' + airport_code

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get recent ATIS data with runway configs (only where configs exist)
        cursor.execute("""
            SELECT
                ad.collected_at,
                ad.information_letter,
                ad.datis_text,
                rc.arriving_runways,
                rc.departing_runways,
                rc.traffic_flow,
                rc.confidence_score
            FROM atis_data ad
            INNER JOIN runway_configs rc ON ad.id = rc.atis_id
            WHERE ad.airport_code = %s
            ORDER BY ad.collected_at DESC
            LIMIT %s
        """, (airport_code, limit))

        results = cursor.fetchall()

        reports = []
        for result in results:
            reports.append(AtisReport(
                timestamp=result['collected_at'].isoformat(),
                information_letter=result['information_letter'],
                datis_text=result['datis_text'],
                arriving_runways=result['arriving_runways'] or [],
                departing_runways=result['departing_runways'] or [],
                traffic_flow=result['traffic_flow'] or 'UNKNOWN',
                confidence=result['confidence_score'] or 0.0
            ))

        return reports

    finally:
        conn.close()

@app.get("/api/v1/airports", response_model=List[AirportSummary])
async def get_airports():
    """List all monitored airports with current status"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get list of all airports and their latest data
        cursor.execute("""
            SELECT DISTINCT ON (airport_code)
                   airport_code,
                   collected_at,
                   information_letter,
                   datis_text
            FROM atis_data
            ORDER BY airport_code, collected_at DESC
        """)

        results = cursor.fetchall()
        airports = []

        # Airport metadata: name, city, state
        airport_metadata = {
            'KATL': ('Hartsfield-Jackson Atlanta', 'Atlanta', 'GA'),
            'KORD': ("Chicago O'Hare", 'Chicago', 'IL'),
            'KLAX': ('Los Angeles International', 'Los Angeles', 'CA'),
            'KDFW': ('Dallas/Fort Worth', 'Dallas', 'TX'),
            'KDEN': ('Denver International', 'Denver', 'CO'),
            'KJFK': ('John F. Kennedy', 'New York', 'NY'),
            'KSFO': ('San Francisco International', 'San Francisco', 'CA'),
            'KSEA': ('Seattle-Tacoma', 'Seattle', 'WA'),
            'KLAS': ('Las Vegas Harry Reid', 'Las Vegas', 'NV'),
            'KMCO': ('Orlando International', 'Orlando', 'FL'),
            'KPHX': ('Phoenix Sky Harbor', 'Phoenix', 'AZ'),
            'KIAH': ('Houston George Bush', 'Houston', 'TX'),
            'KMIA': ('Miami International', 'Miami', 'FL'),
            'KBOS': ('Boston Logan', 'Boston', 'MA'),
            'KMSF': ('Minneapolis-St. Paul', 'Minneapolis', 'MN'),
            'KDTW': ('Detroit Metropolitan', 'Detroit', 'MI'),
            'KPHL': ('Philadelphia International', 'Philadelphia', 'PA'),
            'KLGA': ('LaGuardia', 'New York', 'NY'),
            'KBWI': ('Baltimore/Washington', 'Baltimore', 'MD'),
            'KDCA': ('Ronald Reagan Washington', 'Washington', 'DC'),
            'KSLC': ('Salt Lake City International', 'Salt Lake City', 'UT'),
            'KSAN': ('San Diego International', 'San Diego', 'CA'),
            'KTPA': ('Tampa International', 'Tampa', 'FL'),
            'KMDW': ('Chicago Midway', 'Chicago', 'IL'),
            'KBNA': ('Nashville International', 'Nashville', 'TN'),
            'KAUS': ('Austin-Bergstrom', 'Austin', 'TX'),
            'KMSY': ('New Orleans Louis Armstrong', 'New Orleans', 'LA'),
            'KOAK': ('Oakland International', 'Oakland', 'CA'),
            'KHOU': ('Houston Hobby', 'Houston', 'TX'),
            'KRDU': ('Raleigh-Durham', 'Raleigh', 'NC'),
            'KSTL': ('St. Louis Lambert', 'St. Louis', 'MO'),
            'KCLT': ('Charlotte Douglas', 'Charlotte', 'NC'),
            'KSNA': ('John Wayne Orange County', 'Santa Ana', 'CA'),
            'KSJC': ('San Jose International', 'San Jose', 'CA'),
            'KPDX': ('Portland International', 'Portland', 'OR'),
            'KMCI': ('Kansas City International', 'Kansas City', 'MO'),
            'KCVG': ('Cincinnati/Northern Kentucky', 'Cincinnati', 'OH'),
            'KSMF': ('Sacramento International', 'Sacramento', 'CA'),
            'KSAT': ('San Antonio International', 'San Antonio', 'TX'),
            'KPIT': ('Pittsburgh International', 'Pittsburgh', 'PA'),
            'KIND': ('Indianapolis International', 'Indianapolis', 'IN'),
            'KCMH': ('Columbus John Glenn', 'Columbus', 'OH'),
            'KMKE': ('Milwaukee Mitchell', 'Milwaukee', 'WI'),
            'KBDL': ('Bradley International', 'Hartford', 'CT'),
            'KBUF': ('Buffalo Niagara', 'Buffalo', 'NY'),
            'KBUR': ('Hollywood Burbank', 'Burbank', 'CA'),
            'KBOI': ('Boise Air Terminal', 'Boise', 'ID'),
            'KABQ': ('Albuquerque International', 'Albuquerque', 'NM'),
            'KONT': ('Ontario International', 'Ontario', 'CA'),
            'KSDF': ('Louisville Muhammad Ali', 'Louisville', 'KY'),
            'KRNO': ('Reno-Tahoe International', 'Reno', 'NV'),
            'KTUS': ('Tucson International', 'Tucson', 'AZ'),
            'KTUL': ('Tulsa International', 'Tulsa', 'OK'),
            'KOKC': ('Will Rogers World', 'Oklahoma City', 'OK'),
            'KOMA': ('Eppley Airfield', 'Omaha', 'NE'),
            'KRSW': ('Southwest Florida International', 'Fort Myers', 'FL'),
            'KDSM': ('Des Moines International', 'Des Moines', 'IA'),
            'KRIC': ('Richmond International', 'Richmond', 'VA'),
            'KGEG': ('Spokane International', 'Spokane', 'WA'),
            'KGRR': ('Gerald R. Ford International', 'Grand Rapids', 'MI'),
            'KBHM': ('Birmingham-Shuttlesworth', 'Birmingham', 'AL'),
            'KMEM': ('Memphis International', 'Memphis', 'TN'),
            'KSYR': ('Syracuse Hancock', 'Syracuse', 'NY'),
            'KORF': ('Norfolk International', 'Norfolk', 'VA'),
            'KPVD': ('Rhode Island T.F. Green', 'Providence', 'RI'),
            'KALB': ('Albany International', 'Albany', 'NY'),
            'KMHT': ('Manchester-Boston Regional', 'Manchester', 'NH'),
            'KFLL': ('Fort Lauderdale-Hollywood', 'Fort Lauderdale', 'FL'),
            'KPBI': ('Palm Beach International', 'West Palm Beach', 'FL'),
            'KJAX': ('Jacksonville International', 'Jacksonville', 'FL'),
            'KELP': ('El Paso International', 'El Paso', 'TX'),
            'KADW': ('Joint Base Andrews', 'Camp Springs', 'MD'),
            'KSAV': ('Savannah/Hilton Head', 'Savannah', 'GA'),
            'KCHA': ('Chattanooga Metropolitan', 'Chattanooga', 'TN'),
            'KBIL': ('Billings Logan', 'Billings', 'MT'),
            'KFAT': ('Fresno Yosemite', 'Fresno', 'CA'),
            'KBAK': ('Columbus Air Force Base', 'Columbus', 'MS'),
            'KCOS': ('Colorado Springs Municipal', 'Colorado Springs', 'CO'),
            'KPWM': ('Portland International Jetport', 'Portland', 'ME'),
            'KLGB': ('Long Beach Airport', 'Long Beach', 'CA'),
            'KDAY': ('Dayton International', 'Dayton', 'OH'),
            'KLIT': ('Little Rock National', 'Little Rock', 'AR'),
            'KCLE': ('Cleveland Hopkins', 'Cleveland', 'OH'),
            'KCHS': ('Charleston International', 'Charleston', 'SC'),
            'KDAL': ('Dallas Love Field', 'Dallas', 'TX'),
            'KEWR': ('Newark Liberty', 'Newark', 'NJ'),
            'KGSO': ('Piedmont Triad', 'Greensboro', 'NC'),
            'KHPN': ('Westchester County', 'White Plains', 'NY'),
            'KIAD': ('Washington Dulles', 'Washington', 'DC'),
            'KMSP': ('Minneapolis-St. Paul', 'Minneapolis', 'MN'),
            'KTEB': ('Teterboro', 'Teterboro', 'NJ'),
            'KVNY': ('Van Nuys', 'Los Angeles', 'CA'),
            'PANC': ('Ted Stevens Anchorage', 'Anchorage', 'AK'),
            'PHNL': ('Daniel K. Inouye International', 'Honolulu', 'HI'),
            'TJSJ': ('Luis Munoz Marin', 'San Juan', 'PR'),
        }

        # Legacy: maintain backward compatibility
        airport_names = {code: meta[0] for code, meta in airport_metadata.items()}

        for result in results:
            airport_code = result['airport_code']
            age_minutes = (datetime.utcnow() - result['collected_at']).total_seconds() / 60

            # Determine status
            if age_minutes > 60:
                status = "stale"
            elif age_minutes > 30:
                status = "aging"
            else:
                status = "active"

            # Parse current configuration
            current_config = None
            if status in ["active", "aging"]:
                try:
                    config = parser.parse(
                        airport_code,
                        result['datis_text'],
                        result['information_letter']
                    )
                    current_config = RunwayResponse(
                        airport=config.airport_code,
                        timestamp=config.timestamp.isoformat(),
                        information_letter=config.information_letter,
                        arriving_runways=config.arriving_runways,
                        departing_runways=config.departing_runways,
                        traffic_flow=config.traffic_flow,
                        configuration_name=config.configuration_name,
                        confidence=config.confidence_score,
                        last_updated=result['collected_at'].isoformat()
                    )
                except Exception as e:
                    logger.error(f"Error parsing {airport_code}: {e}")

            # Get metadata (name, city, state) or defaults
            metadata = airport_metadata.get(airport_code, (airport_code, None, None))

            airports.append(AirportSummary(
                airport=airport_code,
                name=metadata[0],
                city=metadata[1],
                state=metadata[2],
                current_config=current_config,
                status=status
            ))

        return airports

    finally:
        conn.close()

@app.get("/api/v1/status", response_model=SystemStatus)
async def get_system_status():
    """Get system health and statistics"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get statistics
        cursor.execute("""
            SELECT
                COUNT(DISTINCT airport_code) as total_airports,
                COUNT(DISTINCT CASE
                    WHEN collected_at > NOW() - INTERVAL '30 minutes'
                    THEN airport_code
                END) as active_airports,
                MAX(collected_at) as last_collection
            FROM atis_data
        """)

        stats = cursor.fetchone()

        return SystemStatus(
            status="operational",
            airports_monitored=stats['total_airports'] or 0,
            airports_active=stats['active_airports'] or 0,
            last_collection=stats['last_collection'].isoformat() if stats['last_collection'] else None,
            database_status="connected"
        )

    except Exception as e:
        logger.error(f"Status check failed: {e}")
        return SystemStatus(
            status="degraded",
            airports_monitored=0,
            airports_active=0,
            last_collection=None,
            database_status="error"
        )
    finally:
        if conn:
            conn.close()

@app.post("/api/v1/report-error/{airport_code}")
async def report_error(airport_code: str):
    """Report a parsing error for an airport (user-triggered)"""

    airport_code = airport_code.upper()
    if not airport_code.startswith('K'):
        airport_code = 'K' + airport_code

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get the most recent runway config for this airport
        cursor.execute("""
            SELECT
                rc.id,
                rc.atis_id,
                rc.arriving_runways,
                rc.departing_runways,
                rc.confidence_score,
                ad.datis_text,
                ad.information_letter
            FROM runway_configs rc
            JOIN atis_data ad ON rc.atis_id = ad.id
            WHERE ad.airport_code = %s
            ORDER BY ad.collected_at DESC
            LIMIT 1
        """, (airport_code,))

        current = cursor.fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="No data found for this airport")

        # Check if this is a DEP or ARR split ATIS
        paired_atis_id = None
        if 'DEP INFO' in current['datis_text'] or 'ARR INFO' in current['datis_text']:
            # Determine if this is DEP or ARR
            is_dep = 'DEP INFO' in current['datis_text']
            search_pattern = 'ARR INFO' if is_dep else 'DEP INFO'

            # Find the matching pair within the last 30 minutes
            cursor.execute("""
                SELECT id
                FROM atis_data
                WHERE airport_code = %s
                  AND datis_text LIKE %s
                  AND collected_at > NOW() - INTERVAL '30 minutes'
                  AND id != %s
                ORDER BY collected_at DESC
                LIMIT 1
            """, (airport_code, f'%{search_pattern}%', current['atis_id']))

            pair = cursor.fetchone()
            if pair:
                paired_atis_id = pair['id']

        # Insert error report
        cursor.execute("""
            INSERT INTO error_reports (
                airport_code,
                current_atis_id,
                paired_atis_id,
                parsed_arriving_runways,
                parsed_departing_runways,
                confidence_score
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            airport_code,
            current['atis_id'],
            paired_atis_id,
            json.dumps(current['arriving_runways'] or []),
            json.dumps(current['departing_runways'] or []),
            current['confidence_score']
        ))

        report_id = cursor.fetchone()['id']
        conn.commit()

        return {
            "success": True,
            "report_id": report_id,
            "message": "Error reported successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to report error: {e}")
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/review", response_class=HTMLResponse)
async def review_dashboard():
    """Serve the human review dashboard"""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Human Review Dashboard</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #0f1419;
                color: #e8eaed;
                padding: 20px;
            }
            .header {
                background: linear-gradient(135deg, #f59e0b 0%, #ef4444 100%);
                padding: 30px;
                border-radius: 12px;
                margin-bottom: 25px;
            }
            h1 { font-size: 32px; margin-bottom: 8px; }
            .subtitle { opacity: 0.9; font-size: 14px; }
            .stats-row {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 15px;
                margin-bottom: 25px;
            }
            .stat-box {
                background: #1a1f2e;
                padding: 15px;
                border-radius: 8px;
                border: 1px solid #2d3748;
                text-align: center;
            }
            .stat-value { font-size: 28px; font-weight: bold; color: #f59e0b; }
            .stat-label { font-size: 12px; color: #a0aec0; margin-top: 5px; }
            .review-container {
                background: #1a1f2e;
                padding: 30px;
                border-radius: 12px;
                border: 1px solid #2d3748;
                margin-bottom: 20px;
            }
            .atis-text {
                background: #0f1419;
                padding: 20px;
                border-radius: 8px;
                border-left: 4px solid #f59e0b;
                margin: 20px 0;
                font-family: monospace;
                white-space: pre-wrap;
                line-height: 1.6;
            }
            .current-parse {
                background: #7f1d1d;
                border: 1px solid #991b1b;
                padding: 15px;
                border-radius: 8px;
                margin: 15px 0;
            }
            .form-group {
                margin: 20px 0;
            }
            label {
                display: block;
                margin-bottom: 8px;
                color: #a0aec0;
                font-weight: 500;
            }
            input[type="text"] {
                width: 100%;
                padding: 12px;
                background: #0f1419;
                border: 1px solid #2d3748;
                border-radius: 8px;
                color: #e8eaed;
                font-size: 14px;
            }
            input[type="text"]:focus {
                outline: none;
                border-color: #f59e0b;
            }
            textarea {
                width: 100%;
                padding: 12px;
                background: #0f1419;
                border: 1px solid #2d3748;
                border-radius: 8px;
                color: #e8eaed;
                font-size: 14px;
                min-height: 80px;
                resize: vertical;
            }
            .button-group {
                display: flex;
                gap: 15px;
                margin-top: 20px;
            }
            button {
                flex: 1;
                padding: 12px 24px;
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
            }
            .btn-submit {
                background: #10b981;
                color: white;
            }
            .btn-submit:hover {
                background: #059669;
            }
            .btn-skip {
                background: #3b82f6;
                color: white;
            }
            .btn-skip:hover {
                background: #2563eb;
            }
            .empty-state {
                text-align: center;
                padding: 60px 20px;
                color: #718096;
            }
            .empty-state h2 {
                font-size: 24px;
                margin-bottom: 10px;
                color: #10b981;
            }
            .loading {
                text-align: center;
                padding: 40px;
            }
            .spinner {
                border: 3px solid #2d3748;
                border-top: 3px solid #f59e0b;
                border-radius: 50%;
                width: 40px;
                height: 40px;
                animation: spin 1s linear infinite;
                margin: 0 auto;
            }
            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Human Review Dashboard</h1>
            <div class="subtitle">Review and correct parsing results to improve accuracy</div>
        </div>

        <div class="stats-row" id="stats">
            <div class="stat-box">
                <div class="stat-value" id="pendingCount">-</div>
                <div class="stat-label">Pending Review</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="reviewedCount">-</div>
                <div class="stat-label">Reviewed</div>
            </div>
        </div>

        <div id="reviewQueue"></div>

        <script>
            let currentItem = null;

            async function loadStats() {
                try {
                    const response = await fetch('/api/review/stats');
                    const stats = await response.json();
                    document.getElementById('pendingCount').textContent = stats.unreviewed_count;
                    document.getElementById('reviewedCount').textContent = stats.reviewed_count;
                } catch (error) {
                    console.error('Failed to load stats:', error);
                }
            }

            async function loadReviewItem() {
                const container = document.getElementById('reviewQueue');
                container.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

                try {
                    const response = await fetch('/api/review/pending?limit=1');
                    const queue = await response.json();

                    if (queue.length === 0) {
                        container.innerHTML = '<div class="empty-state"><h2>All Clear!</h2><p>No items need review at this time.</p></div>';
                        return;
                    }

                    currentItem = queue[0];
                    showCurrentItem();
                } catch (error) {
                    console.error('Failed to load review item:', error);
                    container.innerHTML = '<div class="empty-state"><p>Error loading review item</p></div>';
                }
            }

            function showCurrentItem() {
                const item = currentItem;
                const container = document.getElementById('reviewQueue');

                container.innerHTML = \`
                    <div class="review-container">
                        <h2>\${item.airport_code}</h2>

                        <div class="current-parse">
                            <strong>Original Parse (Confidence: \${(item.confidence * 100).toFixed(0)}%):</strong><br>
                            Arriving: \${item.original_arriving.join(', ') || 'None'}<br>
                            Departing: \${item.original_departing.join(', ') || 'None'}
                        </div>

                        <div class="atis-text">\${item.atis_text}</div>

                        <form id="reviewForm">
                            <div class="form-group">
                                <label>Corrected Arriving Runways</label>
                                <input type="text" id="arrivingInput"
                                       placeholder="e.g., 16L, 16C, 16R (or leave empty for none)"
                                       value="\${item.original_arriving.join(', ')}">
                            </div>

                            <div class="form-group">
                                <label>Corrected Departing Runways</label>
                                <input type="text" id="departingInput"
                                       placeholder="e.g., 34L, 34C, 34R (or leave empty for none)"
                                       value="\${item.original_departing.join(', ')}">
                            </div>

                            <div class="form-group">
                                <label>Notes (Optional)</label>
                                <textarea id="notesInput" placeholder="Add any notes about this correction..."></textarea>
                            </div>

                            <div class="button-group">
                                <button type="button" class="btn-skip" onclick="skipItem()">
                                    Mark as Correct
                                </button>
                                <button type="submit" class="btn-submit">
                                    Submit Correction
                                </button>
                            </div>
                        </form>
                    </div>
                \`;

                document.getElementById('reviewForm').addEventListener('submit', submitReview);
            }

            async function submitReview(event) {
                event.preventDefault();

                const arrivingText = document.getElementById('arrivingInput').value.trim();
                const departingText = document.getElementById('departingInput').value.trim();
                const notes = document.getElementById('notesInput').value.trim();

                const correctedArriving = arrivingText ? arrivingText.split(',').map(r => r.trim()).filter(r => r) : [];
                const correctedDeparting = departingText ? departingText.split(',').map(r => r.trim()).filter(r => r) : [];

                try {
                    const response = await fetch(\`/api/review/submit/\${currentItem.id}\`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            corrected_arriving: correctedArriving,
                            corrected_departing: correctedDeparting,
                            notes: notes || null,
                            reviewed_by: 'human_reviewer'
                        })
                    });

                    if (response.ok) {
                        loadStats();
                        loadReviewItem();
                    } else {
                        const error = await response.json();
                        alert(\`Failed to submit review: \${error.detail || 'Unknown error'}\`);
                    }
                } catch (error) {
                    console.error('Submit error:', error);
                    alert('Failed to submit review');
                }
            }

            async function skipItem() {
                try {
                    const response = await fetch(\`/api/review/skip/\${currentItem.id}\`, {
                        method: 'POST'
                    });

                    if (response.ok) {
                        loadStats();
                        loadReviewItem();
                    } else {
                        alert('Failed to skip item');
                    }
                } catch (error) {
                    console.error('Skip error:', error);
                    alert('Failed to skip item');
                }
            }

            // Initial load
            loadStats();
            loadReviewItem();

            // Refresh stats every 30 seconds
            setInterval(loadStats, 30000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the runway status dashboard"""
    try:
        with open('/app/dashboard.html', 'r') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse(content="<html><body><h1>Error: Dashboard file not found</h1></body></html>", status_code=500)


@app.get("/api/dashboard/stats", response_model=DashboardStats)
async def get_dashboard_stats():
    """Get comprehensive dashboard statistics"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get stale airports (no updates in 3+ hours)
        cursor.execute("""
            SELECT DISTINCT ON (airport_code)
                   airport_code,
                   collected_at,
                   EXTRACT(EPOCH FROM (NOW() - collected_at))/3600 as hours_since_update
            FROM atis_data
            ORDER BY airport_code, collected_at DESC
        """)
        all_airports = cursor.fetchall()

        stale_airports = []
        active_count = 0
        for apt in all_airports:
            hours_old = apt['hours_since_update']
            if hours_old >= 3:
                stale_airports.append({
                    'airport': apt['airport_code'],
                    'hours_since_update': round(hours_old, 1),
                    'last_update': apt['collected_at'].isoformat()
                })
            elif hours_old < 1:
                active_count += 1

        # Get activity stats by time period
        cursor.execute("""
            SELECT
                COUNT(CASE WHEN collected_at > NOW() - INTERVAL '1 hour' THEN 1 END) as hour,
                COUNT(CASE WHEN collected_at > NOW() - INTERVAL '1 day' THEN 1 END) as day,
                COUNT(CASE WHEN collected_at > NOW() - INTERVAL '7 days' THEN 1 END) as week,
                COUNT(CASE WHEN collected_at > NOW() - INTERVAL '30 days' THEN 1 END) as month
            FROM atis_data
        """)
        activity = cursor.fetchone()

        # Get parsing success stats
        cursor.execute("""
            WITH recent_atis AS (
                SELECT airport_code, datis_text, information_letter
                FROM atis_data
                WHERE collected_at > NOW() - INTERVAL '24 hours'
                  AND is_changed = true
            )
            SELECT COUNT(*) as total_records
            FROM recent_atis
        """)
        parsing_total = cursor.fetchone()['total_records']

        # Parse recent data to get success/failure counts
        cursor.execute("""
            SELECT airport_code, datis_text, information_letter
            FROM atis_data
            WHERE collected_at > NOW() - INTERVAL '24 hours'
              AND is_changed = true
            LIMIT 200
        """)
        recent_records = cursor.fetchall()

        success_count = 0
        failure_count = 0
        low_confidence = 0

        for record in recent_records:
            try:
                config = parser.parse(
                    record['airport_code'],
                    record['datis_text'],
                    record['information_letter']
                )
                if config.confidence_score >= 0.5:
                    success_count += 1
                    if config.confidence_score < 0.8:
                        low_confidence += 1
                else:
                    failure_count += 1
            except Exception:
                failure_count += 1

        # Get confidence stats by airport
        cursor.execute("""
            SELECT
                rc.airport_code,
                AVG(rc.confidence_score) as avg_confidence,
                COUNT(*) as config_count
            FROM runway_configs rc
            JOIN atis_data ad ON rc.atis_id = ad.id
            WHERE ad.collected_at > NOW() - INTERVAL '7 days'
            GROUP BY rc.airport_code
            HAVING COUNT(*) >= 1
            ORDER BY avg_confidence DESC
            LIMIT 20
        """)
        confidence_by_airport = cursor.fetchall()

        confidence_stats = {
            'by_airport': [
                {
                    'airport': row['airport_code'],
                    'avg_confidence': round(row['avg_confidence'], 2),
                    'sample_size': row['config_count']
                }
                for row in confidence_by_airport
            ],
            'overall_avg': round(sum(r['avg_confidence'] for r in confidence_by_airport) / len(confidence_by_airport), 2) if confidence_by_airport else 0
        }

        # Get recent runway changes
        cursor.execute("""
            SELECT
                airport_code,
                change_time,
                from_config,
                to_config,
                duration_minutes
            FROM runway_changes
            WHERE change_time > NOW() - INTERVAL '24 hours'
            ORDER BY change_time DESC
            LIMIT 50
        """)
        changes = cursor.fetchall()

        recent_changes = [
            {
                'airport': change['airport_code'],
                'time': change['change_time'].isoformat(),
                'from': change['from_config'],
                'to': change['to_config'],
                'duration_minutes': change['duration_minutes']
            }
            for change in changes
        ]

        return DashboardStats(
            current_time=datetime.utcnow().isoformat(),
            total_airports=len(all_airports),
            active_airports=active_count,
            stale_airports=stale_airports,
            parsing_stats={
                'total_parsed': success_count + failure_count,
                'successful': success_count,
                'failed': failure_count,
                'low_confidence': low_confidence,
                'success_rate': round(success_count / (success_count + failure_count) * 100, 1) if (success_count + failure_count) > 0 else 0
            },
            confidence_stats=confidence_stats,
            activity_stats={
                'last_hour': activity['hour'],
                'last_day': activity['day'],
                'last_week': activity['week'],
                'last_month': activity['month']
            },
            recent_changes=recent_changes
        )

    finally:
        conn.close()

@app.get("/api/dashboard/current-airports", response_model=List[AirportStatus])
async def get_current_airports():
    """Get current status for all airports"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get latest config for each airport
        cursor.execute("""
            WITH latest_configs AS (
                SELECT DISTINCT ON (rc.airport_code)
                    rc.airport_code,
                    rc.arriving_runways,
                    rc.departing_runways,
                    rc.traffic_flow,
                    rc.created_at
                FROM runway_configs rc
                JOIN atis_data ad ON rc.atis_id = ad.id
                WHERE ad.collected_at > NOW() - INTERVAL '6 hours'
                ORDER BY rc.airport_code, rc.created_at DESC
            )
            SELECT * FROM latest_configs
            ORDER BY airport_code
        """)

        latest_configs = cursor.fetchall()

        result = []
        for config in latest_configs:
            airport = config['airport_code']

            # Get 4 most recent changes for this airport
            cursor.execute("""
                SELECT
                    from_config,
                    to_config,
                    change_time,
                    duration_minutes
                FROM runway_changes
                WHERE airport_code = %s
                ORDER BY change_time DESC
                LIMIT 4
            """, (airport,))

            changes = cursor.fetchall()
            recent_changes = []
            for change in changes:
                from_cfg = change['from_config'] or {}
                to_cfg = change['to_config'] or {}
                recent_changes.append({
                    'time': change['change_time'].isoformat(),
                    'from': {
                        'arriving': from_cfg.get('arriving', []),
                        'departing': from_cfg.get('departing', [])
                    },
                    'to': {
                        'arriving': to_cfg.get('arriving', []),
                        'departing': to_cfg.get('departing', [])
                    },
                    'duration_minutes': change['duration_minutes']
                })

            result.append(AirportStatus(
                airport_code=airport,
                arriving=config['arriving_runways'] or [],
                departing=config['departing_runways'] or [],
                flow=config['traffic_flow'] or 'UNKNOWN',
                last_change=config['created_at'].isoformat(),
                recent_changes=recent_changes
            ))

        return result

    finally:
        conn.close()

@app.get("/api/review/pending", response_model=List[ReviewItem])
async def get_pending_reviews(
    limit: int = Query(default=100, le=100),
    include_reviewed: bool = False,
    source_filter: Optional[str] = Query(default=None, description="Filter by reported_by: 'user', 'computer', or 'all'")
):
    """Get error reports needing review with optional filtering by source"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Build filter conditions for reviewed status and source
        additional_filters = []
        if not include_reviewed:
            additional_filters.append("reviewed = FALSE")
        if source_filter and source_filter != 'all':
            additional_filters.append(f"reported_by = '{source_filter}'")

        additional_where = " AND ".join(additional_filters) if additional_filters else "TRUE"

        cursor.execute(f"""
            WITH recent_reports AS (
                SELECT DISTINCT ON (airport_code)
                    id,
                    airport_code,
                    reported_at,
                    current_atis_id,
                    paired_atis_id,
                    parsed_arriving_runways,
                    parsed_departing_runways,
                    confidence_score,
                    reviewed,
                    corrected_arriving_runways,
                    corrected_departing_runways,
                    reviewer_notes,
                    reported_by
                FROM error_reports
                WHERE reported_at > NOW() - INTERVAL '1 hour'
                ORDER BY airport_code, reported_at DESC
            )
            SELECT
                rr.id,
                rr.airport_code,
                rr.reported_at,
                rr.current_atis_id,
                rr.paired_atis_id,
                rr.parsed_arriving_runways,
                rr.parsed_departing_runways,
                rr.confidence_score,
                rr.reviewed,
                rr.corrected_arriving_runways,
                rr.corrected_departing_runways,
                rr.reviewer_notes,
                rr.reported_by,
                ad_current.datis_text as current_atis_text,
                ad_current.collected_at as current_collected_at,
                ad_paired.datis_text as paired_atis_text,
                ad_paired.collected_at as paired_collected_at
            FROM recent_reports rr
            JOIN atis_data ad_current ON rr.current_atis_id = ad_current.id
            LEFT JOIN atis_data ad_paired ON rr.paired_atis_id = ad_paired.id
            WHERE rr.confidence_score < 1.0
              AND {additional_where}
            ORDER BY rr.airport_code
            LIMIT %s
        """, (limit,))

        error_reports = cursor.fetchall()

        # Build review items
        review_items = []
        for report in error_reports:
            # Build combined ATIS text if there's a pair
            if report['paired_atis_id']:
                if 'ARR INFO' in report['current_atis_text']:
                    atis_text = f"ARR INFO:\n{report['current_atis_text']}\n\n{'='*60}\n\nDEP INFO:\n{report['paired_atis_text']}"
                else:
                    atis_text = f"ARR INFO:\n{report['paired_atis_text']}\n\n{'='*60}\n\nDEP INFO:\n{report['current_atis_text']}"
                merged_from_pair = True
            else:
                atis_text = report['current_atis_text']
                merged_from_pair = False

            # Determine issue type
            arriving = report['parsed_arriving_runways'] or []
            departing = report['parsed_departing_runways'] or []

            if report['confidence_score'] < 1.0:
                issue_type = 'low_confidence'
            elif not arriving or not departing:
                issue_type = 'has_none'
            else:
                issue_type = 'user_reported'

            # Detect reciprocal runways
            all_runways = list(arriving) + list(departing)
            has_reciprocals = detect_reciprocal_runways(all_runways)

            # Build warnings
            warnings = []
            if has_reciprocals:
                warnings.append("RECIPROCAL RUNWAYS DETECTED - Data probably wrong")

            review_items.append(ReviewItem(
                id=report['id'],
                atis_id=report['current_atis_id'],
                airport_code=report['airport_code'],
                atis_text=atis_text,
                original_arriving=arriving,
                original_departing=departing,
                confidence=report['confidence_score'],
                collected_at=report['current_collected_at'].isoformat(),
                issue_type=issue_type,
                merged_from_pair=merged_from_pair,
                component_confidence=None,
                has_reciprocal_runways=has_reciprocals,
                is_incomplete_pair=False,
                warnings=warnings,
                reviewed=report.get('reviewed', False),
                corrected_arriving=report.get('corrected_arriving_runways'),
                corrected_departing=report.get('corrected_departing_runways'),
                reviewer_notes=report.get('reviewer_notes')
            ))

        return review_items

    finally:
        conn.close()

@app.post("/api/review/submit/{review_id}")
async def submit_review(review_id: int, submission: ReviewSubmission):
    """Submit a correction for a user-reported error"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get the error report details
        cursor.execute("""
            SELECT er.id, er.airport_code, er.parsed_arriving_runways, er.parsed_departing_runways,
                   er.confidence_score, ad.datis_text
            FROM error_reports er
            JOIN atis_data ad ON er.current_atis_id = ad.id
            WHERE er.id = %s
        """, (review_id,))

        error_report = cursor.fetchone()
        if not error_report:
            raise HTTPException(status_code=404, detail="Error report not found")

        # Validate correction: Check for reciprocal runways
        all_corrected_runways = submission.corrected_arriving + submission.corrected_departing
        if detect_reciprocal_runways(all_corrected_runways):
            raise HTTPException(
                status_code=400,
                detail="Correction contains reciprocal runways (opposite ends of same runway). "
                       "Please verify the data - aircraft cannot use opposite runway ends simultaneously."
            )

        # Update the error report with corrections
        cursor.execute("""
            UPDATE error_reports
            SET reviewed = TRUE,
                reviewed_at = NOW(),
                corrected_arriving_runways = %s,
                corrected_departing_runways = %s,
                reviewer_notes = %s
            WHERE id = %s
        """, (
            json.dumps(submission.corrected_arriving),
            json.dumps(submission.corrected_departing),
            submission.notes,
            review_id
        ))

        conn.commit()

        return {
            "status": "success",
            "review_id": review_id,
            "message": "Review submitted successfully"
        }

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to submit review: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to submit review: {str(e)}")
    finally:
        conn.close()

@app.post("/api/review/skip/{config_id}")
async def skip_review(config_id: int, notes: Optional[str] = None):
    """Mark an error report as correctly parsed (skip review)"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Get the error report details
        cursor.execute("""
            SELECT id, airport_code, parsed_arriving_runways, parsed_departing_runways
            FROM error_reports
            WHERE id = %s
        """, (config_id,))

        report = cursor.fetchone()
        if not report:
            raise HTTPException(status_code=404, detail="Error report not found")

        # Mark as reviewed without corrections (means it was correct)
        cursor.execute("""
            UPDATE error_reports
            SET reviewed = TRUE,
                reviewed_at = NOW(),
                corrected_arriving_runways = parsed_arriving_runways,
                corrected_departing_runways = parsed_departing_runways,
                reviewer_notes = %s
            WHERE id = %s
        """, (
            notes or 'Marked as correct',
            config_id
        ))

        conn.commit()

        return {"status": "success", "message": "Item marked as correct"}

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.get("/api/review/stats", response_model=ReviewStats)
async def get_review_stats():
    """Get review statistics for error reports (most recent per airport)"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            WITH recent_reports AS (
                SELECT DISTINCT ON (airport_code)
                    id,
                    airport_code,
                    reviewed,
                    reported_by,
                    reported_at,
                    confidence_score
                FROM error_reports
                ORDER BY airport_code, reported_at DESC
            )
            SELECT
                COUNT(*) FILTER (WHERE confidence_score < 1.0) as total,
                COUNT(*) FILTER (WHERE confidence_score < 1.0 AND reviewed = FALSE) as unreviewed,
                COUNT(*) FILTER (WHERE confidence_score < 1.0 AND reviewed = TRUE) as reviewed
            FROM recent_reports
        """)
        stats = cursor.fetchone()

        cursor.execute("""
            WITH recent_reports AS (
                SELECT DISTINCT ON (airport_code)
                    airport_code,
                    reported_by,
                    reported_at,
                    confidence_score
                FROM error_reports
                ORDER BY airport_code, reported_at DESC
            )
            SELECT reported_by, COUNT(*) as count
            FROM recent_reports
            WHERE confidence_score < 1.0
            GROUP BY reported_by
            ORDER BY count DESC
        """)
        by_source = {row['reported_by']: row['count'] for row in cursor.fetchall()}

        return ReviewStats(
            total_reports=stats['total'],
            unreviewed_count=stats['unreviewed'],
            reviewed_count=stats['reviewed'],
            by_source=by_source
        )

    finally:
        conn.close()

# Health check endpoint
@app.get("/health")
async def health_check():
    """Simple health check endpoint"""
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
