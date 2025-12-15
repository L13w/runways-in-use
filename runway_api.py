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

# Only add SSL if explicitly required
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
            LEFT JOIN error_reports er ON rc.airport_code = er.airport_code
                AND rc.atis_id = er.current_atis_id
            WHERE er.id IS NULL
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
            'KAUS': ('Austin-Bergstrom', 'Austin', 'TX'),
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
            'KBDL': ('Bradley International', 'Hartford', 'CT'),
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
            'TJSJ': ('Luis Muñoz Marín', 'San Juan', 'PR'),
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

class ErrorReportRequest(BaseModel):
    corrected_arrivals: Optional[List[str]] = None
    corrected_departures: Optional[List[str]] = None

@app.post("/api/v1/report-error/{airport_code}")
async def report_error(airport_code: str, request: ErrorReportRequest = None):
    """Report a parsing error for an airport (user-triggered)"""

    # Handle case where request body is empty or not provided
    if request is None:
        request = ErrorReportRequest()

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

        # Insert error report with optional user corrections
        cursor.execute("""
            INSERT INTO error_reports (
                airport_code,
                current_atis_id,
                paired_atis_id,
                parsed_arriving_runways,
                parsed_departing_runways,
                confidence_score,
                corrected_arriving_runways,
                corrected_departing_runways,
                reviewed,
                reviewed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            airport_code,
            current['atis_id'],
            paired_atis_id,
            json.dumps(current['arriving_runways'] or []),
            json.dumps(current['departing_runways'] or []),
            current['confidence_score'],
            json.dumps(request.corrected_arrivals) if request.corrected_arrivals else None,
            json.dumps(request.corrected_departures) if request.corrected_departures else None,
            # If user provided corrections, mark as reviewed
            True if (request.corrected_arrivals or request.corrected_departures) else False,
            datetime.utcnow() if (request.corrected_arrivals or request.corrected_departures) else None
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
            .nav-buttons {
                display: flex;
                gap: 15px;
                margin-bottom: 20px;
            }
            .btn-nav {
                flex: 1;
                padding: 10px 20px;
                border: 1px solid #2d3748;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
                background: #1a1f2e;
                color: #e8eaed;
                transition: all 0.2s;
            }
            .btn-nav:hover:not(:disabled) {
                background: #2d3748;
                border-color: #f59e0b;
            }
            .btn-nav:disabled {
                opacity: 0.4;
                cursor: not-allowed;
            }
            .badge {
                display: inline-block;
                padding: 4px 12px;
                border-radius: 12px;
                font-size: 11px;
                font-weight: 600;
                margin-left: 10px;
            }
            .badge-warning { background: #f59e0b; color: white; }
            .badge-danger { background: #ef4444; color: white; }
            .badge-info { background: #3b82f6; color: white; }
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
            .help-text {
                font-size: 12px;
                color: #718096;
                margin-top: 5px;
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
            .airport-list { margin-top: 15px; }
            .airport-item {
                display: flex;
                align-items: center;
                padding: 12px;
                background: #0f1419;
                border-radius: 8px;
                margin-bottom: 8px;
                border: 1px solid #2d3748;
                cursor: pointer;
                transition: background 0.2s;
            }
            .airport-item:hover { background: #1a1f2e; }
            .airport-item.pinned { background: #1e2a3a; border-color: #667eea; }
            .pin-icon {
                font-size: 16px;
                margin-right: 12px;
                cursor: pointer;
                user-select: none;
                opacity: 0.5;
                transition: opacity 0.2s;
            }
            .pin-icon:hover, .airport-item.pinned .pin-icon { opacity: 1; }
            .airport-code { font-weight: bold; margin-right: 12px; min-width: 50px; }
            .airport-runways { flex: 1; font-size: 14px; color: #a0aec0; }
            .airport-time { font-size: 12px; color: #718096; margin-right: 12px; }
            .chevron {
                font-size: 12px;
                transition: transform 0.3s;
                user-select: none;
            }
            .chevron.open { transform: rotate(180deg); }
            .airport-drawer {
                max-height: 0;
                overflow: hidden;
                transition: max-height 0.3s ease-out;
                background: #0a0e14;
                border-radius: 0 0 8px 8px;
                margin-top: -8px;
                margin-bottom: 8px;
            }
            .airport-drawer.open { max-height: 600px; padding: 15px; }
            .drawer-change {
                padding: 10px;
                background: #1a1f2e;
                border-radius: 6px;
                margin-bottom: 8px;
                font-size: 13px;
            }
            .drawer-change-time { color: #667eea; font-weight: bold; margin-bottom: 5px; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>👤 Human Review Dashboard</h1>
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
            <div class="stat-box">
                <div class="stat-value" id="computerCount">-</div>
                <div class="stat-label">🤖 Computer</div>
            </div>
            <div class="stat-box">
                <div class="stat-value" id="userCount">-</div>
                <div class="stat-label">👤 User</div>
            </div>
        </div>

        <div style="margin: 20px 0; padding: 15px; background: #151a21; border-radius: 8px; border: 1px solid #2d3748;">
            <div style="display: flex; gap: 20px; align-items: center; margin-bottom: 15px;">
                <label style="display: flex; align-items: center; cursor: pointer; font-size: 14px;">
                    <input type="checkbox" id="showReviewedCheckbox" onchange="handleShowReviewedChange()" style="margin-right: 10px; width: 18px; height: 18px; cursor: pointer;">
                    <span>Show already reviewed items</span>
                </label>
                <div style="display: flex; align-items: center; gap: 10px; flex: 1;">
                    <label for="sourceFilter" style="font-size: 14px; color: #a0aec0; margin: 0;">Filter by source:</label>
                    <select id="sourceFilter" onchange="handleSourceFilterChange()" style="padding: 8px 12px; background: #0f1419; border: 1px solid #2d3748; border-radius: 6px; color: #e8eaed; font-size: 14px; cursor: pointer; flex: 0 0 200px;">
                        <option value="">All Reports</option>
                        <option value="computer">🤖 Computer-detected</option>
                        <option value="user">👤 User-reported</option>
                    </select>
                </div>
            </div>
        </div>

        <div id="reviewQueue"></div>

        <script>
            let currentItem = null;
            let showReviewed = false;
            let sourceFilter = '';

            // Initialize checkbox and filter state from URL parameter or localStorage
            function initializeCheckboxState() {
                const urlParams = new URLSearchParams(window.location.search);
                const showReviewedParam = urlParams.get('show_reviewed');
                const sourceFilterParam = urlParams.get('source_filter');

                if (showReviewedParam !== null) {
                    // URL parameter takes precedence
                    showReviewed = showReviewedParam === 'true';
                } else {
                    // Fall back to localStorage
                    const stored = localStorage.getItem('showReviewed');
                    showReviewed = stored === 'true';
                }

                if (sourceFilterParam !== null) {
                    sourceFilter = sourceFilterParam;
                } else {
                    const stored = localStorage.getItem('sourceFilter');
                    sourceFilter = stored || '';
                }

                document.getElementById('showReviewedCheckbox').checked = showReviewed;
                document.getElementById('sourceFilter').value = sourceFilter;
            }

            function handleShowReviewedChange() {
                showReviewed = document.getElementById('showReviewedCheckbox').checked;
                // Save to localStorage
                localStorage.setItem('showReviewed', showReviewed);
                // Reload the current view
                loadReviewItem();
            }

            function handleSourceFilterChange() {
                sourceFilter = document.getElementById('sourceFilter').value;
                // Save to localStorage
                localStorage.setItem('sourceFilter', sourceFilter);
                // Reload the current view
                loadReviewItem();
            }

            async function loadStats() {
                try {
                    const response = await fetch('/api/review/stats');
                    const stats = await response.json();
                    document.getElementById('pendingCount').textContent = stats.unreviewed_count;
                    document.getElementById('reviewedCount').textContent = stats.reviewed_count;
                    document.getElementById('computerCount').textContent = stats.by_source.computer || 0;
                    document.getElementById('userCount').textContent = stats.by_source.user || 0;
                } catch (error) {
                    console.error('Failed to load stats:', error);
                }
            }

            function getConfigIdFromUrl() {
                const params = new URLSearchParams(window.location.search);
                return params.get('config_id');
            }

            async function loadReviewItem(configId = null) {
                const container = document.getElementById('reviewQueue');
                container.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

                try {
                    let item;
                    if (configId) {
                        // Load specific item by ID
                        const response = await fetch(`/api/review/item/${configId}`);
                        if (!response.ok) throw new Error('Item not found');
                        item = await response.json();
                    } else {
                        // Build URL with filters
                        let url = '/api/review/pending?limit=1';
                        if (showReviewed) {
                            url += '&include_reviewed=true';
                        }
                        if (sourceFilter) {
                            url += `&source_filter=${sourceFilter}`;
                        }

                        const response = await fetch(url);
                        const queue = await response.json();

                        if (queue.length === 0) {
                            const filterMsg = sourceFilter ? ` matching "${sourceFilter}" filter` : '';
                            container.innerHTML = `
                                <div class="empty-state">
                                    <h2>✅ All Clear!</h2>
                                    <p>No items need review${filterMsg} at this time.</p>
                                </div>
                            `;
                            return;
                        }

                        item = queue[0];
                        // Update URL with the first item's ID and filters
                        let newUrl = `/review?config_id=${item.id}`;
                        if (showReviewed) newUrl += '&show_reviewed=true';
                        if (sourceFilter) newUrl += `&source_filter=${sourceFilter}`;
                        window.history.replaceState({}, '', newUrl);
                    }

                    currentItem = item;
                    showCurrentItem();
                } catch (error) {
                    console.error('Failed to load review item:', error);
                    // If a specific config_id was requested but not found, redirect to main review page
                    const configId = getConfigIdFromUrl();
                    if (configId) {
                        console.log('Config not found, redirecting to main review page');
                        window.location.href = '/review';
                        return;
                    }
                    container.innerHTML = '<div class="empty-state"><p>Error loading review item</p></div>';
                }
            }

            function showCurrentItem() {
                const item = currentItem;
                const container = document.getElementById('reviewQueue');

                const issueLabel = {
                    'low_confidence': 'Low Confidence',
                    'has_none': 'Has "None"',
                    'parse_failed': 'Parse Failed',
                    'complete': 'Complete'
                }[item.issue_type] || item.issue_type;

                const badgeClass = {
                    'low_confidence': 'badge-warning',
                    'has_none': 'badge-danger',
                    'parse_failed': 'badge-danger',
                    'complete': 'badge-success'
                }[item.issue_type] || 'badge-info';

                container.innerHTML = `
                    <div class="nav-buttons">
                        <button id="prevBtn" class="btn-nav" onclick="navigateItem('prev')">← Previous</button>
                        <button id="nextBtn" class="btn-nav" onclick="navigateItem('next')">Next →</button>
                    </div>

                    <div class="review-container">
                        <h2>
                            ${item.airport_code}
                            <span class="badge ${badgeClass}">${issueLabel}</span>
                            ${item.reviewed ? '<span class="badge" style="background: #48bb78; margin-left: 8px;">✓ Reviewed</span>' : ''}
                            <span style="float: right; font-size: 14px; color: #718096;">
                                ID: ${item.id}
                            </span>
                        </h2>

                        <div class="current-parse">
                            <strong>Original Parse (Confidence: ${(item.confidence * 100).toFixed(0)}%):</strong><br>
                            ${item.merged_from_pair && item.component_confidence ? `
                                Arriving: ${item.original_arriving.join(', ') || 'None'}
                                <span style="color: #48bb78;">(${(item.component_confidence.arrivals * 100).toFixed(0)}%)</span><br>
                                Departing: ${item.original_departing.join(', ') || 'None'}
                                <span style="color: #48bb78;">(${(item.component_confidence.departures * 100).toFixed(0)}%)</span>
                            ` : `
                                Arriving: ${item.original_arriving.join(', ') || 'None'}<br>
                                Departing: ${item.original_departing.join(', ') || 'None'}
                            `}
                        </div>

                        ${item.reviewed && (item.corrected_arriving || item.corrected_departing) ? `
                            <div class="current-parse" style="background: #1a2f1a; border-left: 4px solid #48bb78; margin-top: 15px;">
                                <strong style="color: #48bb78;">✓ Corrected Values:</strong><br>
                                Arriving: ${item.corrected_arriving ? item.corrected_arriving.join(', ') : 'None'}<br>
                                Departing: ${item.corrected_departing ? item.corrected_departing.join(', ') : 'None'}
                            </div>
                        ` : ''}

                        ${item.warnings && item.warnings.length > 0 ? `
                            <div style="margin: 15px 0;">
                                ${item.warnings.map(warning => {
                                    // Red border for reciprocal runways, blue for other warnings
                                    const isReciprocal = warning.includes('RECIPROCAL');
                                    const borderColor = isReciprocal ? '#E53E3E' : '#4299E1';
                                    const bgColor = isReciprocal ? '#FFF5F5' : '#EDF2F7';
                                    const textColor = isReciprocal ? '#C53030' : '#2C5282';

                                    return `
                                        <div style="background-color: ${bgColor}; border-left: 4px solid ${borderColor}; padding: 12px; margin-bottom: 10px; border-radius: 4px;">
                                            <strong style="color: ${textColor};">${warning.split(' - ')[0]}</strong>
                                            ${warning.includes(' - ') ? `<br><span style="font-size: 14px; color: #4A5568;">${warning.split(' - ')[1]}</span>` : ''}
                                        </div>
                                    `;
                                }).join('')}
                            </div>
                        ` : ''}

                        <div class="atis-text">${item.atis_text}</div>

                        <form id="reviewForm">
                            <div class="form-group">
                                <label>Corrected Arriving Runways</label>
                                <input type="text" id="arrivingInput"
                                       placeholder="e.g., 16L, 16C, 16R (or leave empty for none)"
                                       value="${item.reviewed && item.corrected_arriving ? item.corrected_arriving.join(', ') : item.original_arriving.join(', ')}">
                                <div class="help-text">Separate multiple runways with commas</div>
                            </div>

                            <div class="form-group">
                                <label>Corrected Departing Runways</label>
                                <input type="text" id="departingInput"
                                       placeholder="e.g., 34L, 34C, 34R (or leave empty for none)"
                                       value="${item.reviewed && item.corrected_departing ? item.corrected_departing.join(', ') : item.original_departing.join(', ')}">
                                <div class="help-text">Separate multiple runways with commas</div>
                            </div>

                            <div class="form-group">
                                <label>Notes (Optional)</label>
                                <textarea id="notesInput" placeholder="Add any notes about this correction...">${item.reviewer_notes || ''}</textarea>
                            </div>

                            <div class="button-group">
                                <button type="button" class="btn-skip" onclick="skipItem()">
                                    ✓ Mark as Correct
                                </button>
                                <button type="submit" class="btn-submit">
                                    💾 Submit Correction
                                </button>
                            </div>
                        </form>
                    </div>
                `;

                document.getElementById('reviewForm').addEventListener('submit', submitReview);
            }

            async function navigateItem(direction, afterSubmit = false) {
                try {
                    // Build URL with filters
                    let url = `/api/review/navigate/${currentItem.id}/${direction}`;
                    const params = [];
                    if (showReviewed) {
                        params.push('include_reviewed=true');
                    }
                    if (sourceFilter) {
                        params.push(`source_filter=${sourceFilter}`);
                    }
                    if (params.length > 0) {
                        url += '?' + params.join('&');
                    }

                    const response = await fetch(url);
                    const data = await response.json();

                    if (data.next_id) {
                        // Preserve filters in URL
                        let newUrl = `/review?config_id=${data.next_id}`;
                        if (showReviewed) newUrl += '&show_reviewed=true';
                        if (sourceFilter) newUrl += `&source_filter=${sourceFilter}`;
                        window.location.href = newUrl;
                    } else {
                        // No more items
                        if (afterSubmit) {
                            // After submit/skip, redirect to review queue to show completion
                            let newUrl = '/review';
                            const urlParams = [];
                            if (showReviewed) urlParams.push('show_reviewed=true');
                            if (sourceFilter) urlParams.push(`source_filter=${sourceFilter}`);
                            if (urlParams.length > 0) {
                                newUrl += '?' + urlParams.join('&');
                            }
                            window.location.href = newUrl;
                        } else {
                            // Manual navigation, just show message
                            alert(data.message || 'No more items in this direction');
                        }
                    }
                } catch (error) {
                    console.error('Navigation error:', error);
                    alert('Failed to navigate');
                }
            }

            function detectReciprocalRunways(runways) {
                // Extract runway numbers (without L/C/R suffix)
                const runwayNumbers = runways.map(rwy => {
                    const match = rwy.match(/^(\d{1,2})/);
                    return match ? parseInt(match[1]) : null;
                }).filter(n => n !== null);

                // Check all pairs for reciprocals (differ by 18)
                for (let i = 0; i < runwayNumbers.length; i++) {
                    for (let j = i + 1; j < runwayNumbers.length; j++) {
                        const diff = Math.abs(runwayNumbers[i] - runwayNumbers[j]);
                        if (diff === 18) {
                            return true;  // Found reciprocal runways
                        }
                    }
                }
                return false;
            }

            async function submitReview(event) {
                event.preventDefault();

                const arrivingText = document.getElementById('arrivingInput').value.trim();
                const departingText = document.getElementById('departingInput').value.trim();
                const notes = document.getElementById('notesInput').value.trim();

                const correctedArriving = arrivingText ? arrivingText.split(',').map(r => r.trim()).filter(r => r) : [];
                const correctedDeparting = departingText ? departingText.split(',').map(r => r.trim()).filter(r => r) : [];

                // Check for reciprocal runways in the correction
                const allRunways = [...correctedArriving, ...correctedDeparting];
                if (detectReciprocalRunways(allRunways)) {
                    const confirmed = confirm(
                        '⚠️ WARNING: Reciprocal Runways Detected!\\n\\n' +
                        'Your correction contains opposite ends of the same runway (e.g., 16/34, 09/27).\\n' +
                        'Aircraft cannot use opposite runway ends simultaneously.\\n\\n' +
                        'This is probably WRONG data.\\n\\n' +
                        'Click OK to submit anyway, or Cancel to fix it.'
                    );
                    if (!confirmed) {
                        return;  // User canceled, don't submit
                    }
                }

                try {
                    const response = await fetch(`/api/review/submit/${currentItem.id}`, {
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
                        // Navigate to next item after submitting
                        navigateItem('next', true);
                    } else {
                        const error = await response.json();
                        alert(`Failed to submit review: ${error.detail || 'Unknown error'}`);
                    }
                } catch (error) {
                    console.error('Submit error:', error);
                    alert('Failed to submit review');
                }
            }

            async function skipItem() {
                try {
                    const response = await fetch(`/api/review/skip/${currentItem.id}`, {
                        method: 'POST'
                    });

                    if (response.ok) {
                        loadStats();
                        // Navigate to next item after skipping
                        navigateItem('next', true);
                    } else {
                        alert('Failed to skip item');
                    }
                } catch (error) {
                    console.error('Skip error:', error);
                    alert('Failed to skip item');
                }
            }

            // Initial load
            initializeCheckboxState();
            loadStats();
            const configId = getConfigIdFromUrl();
            loadReviewItem(configId);

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

        # Use CTE to get most recent report per airport, THEN filter by confidence
        # IMPORTANT: Exclude airports that have ANY reviewed report in the last 2 hours
        # This prevents the same airport from showing up repeatedly after being reviewed
        cursor.execute(f"""
            WITH recently_reviewed_airports AS (
                -- Airports with any reviewed report in the last 2 hours
                SELECT DISTINCT airport_code
                FROM error_reports
                WHERE reviewed = TRUE
                  AND reviewed_at > NOW() - INTERVAL '2 hours'
            ),
            recent_reports AS (
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
                  AND airport_code NOT IN (SELECT airport_code FROM recently_reviewed_airports)
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
            # ALWAYS show ARR above DEP (since arrival input is above departure input)
            if report['paired_atis_id']:
                # Determine which is ARR and which is DEP
                if 'ARR INFO' in report['current_atis_text']:
                    # Current is ARR, paired is DEP
                    atis_text = f"ARR INFO:\n{report['current_atis_text']}\n\n{'='*60}\n\nDEP INFO:\n{report['paired_atis_text']}"
                else:
                    # Current is DEP, paired is ARR - swap order to show ARR first
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
                warnings.append("⚠️ RECIPROCAL RUNWAYS DETECTED - Data probably wrong")

            # Add source indicator
            if report['reported_by'] == 'computer':
                warnings.append("🤖 Computer-detected issue")
            else:
                warnings.append("👤 User-reported error")

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

        # Create a parsing correction entry if this represents an actual correction
        # (not just confirming the parser was correct)
        original_arriving = error_report['parsed_arriving_runways'] or []
        original_departing = error_report['parsed_departing_runways'] or []

        # Convert to sets for comparison (order-independent)
        orig_arr_set = set(original_arriving) if isinstance(original_arriving, list) else set(json.loads(original_arriving or '[]'))
        orig_dep_set = set(original_departing) if isinstance(original_departing, list) else set(json.loads(original_departing or '[]'))
        corr_arr_set = set(submission.corrected_arriving)
        corr_dep_set = set(submission.corrected_departing)

        is_actual_correction = (orig_arr_set != corr_arr_set) or (orig_dep_set != corr_dep_set)

        if is_actual_correction:
            # Extract a pattern from the ATIS text for future matching
            # Use a normalized hash of the key phrases that determine runway assignment
            atis_text = error_report['datis_text'] or ''

            # Create a pattern fingerprint - extract runway-related phrases
            # This helps identify similar ATIS patterns in the future
            import re
            pattern_phrases = []

            # Extract approach type mentions (ILS, VISUAL, RNAV, etc.)
            approach_matches = re.findall(r'(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:APCH|APPROACH|RWY|RY)', atis_text, re.IGNORECASE)
            pattern_phrases.extend(approach_matches[:3])  # Limit to first 3

            # Extract runway action phrases (LANDING, DEPARTING, etc.)
            action_matches = re.findall(r'(?:LANDING|DEPARTING|DEPG|LNDG|ARRIVALS?|DEPARTURES?)\s+(?:AND\s+)?(?:RWYS?|RYS?|RY)?', atis_text, re.IGNORECASE)
            pattern_phrases.extend(action_matches[:3])

            # Create pattern string (unique identifier for this ATIS type)
            pattern_key = f"{error_report['airport_code']}:{' | '.join(sorted(set(p.upper() for p in pattern_phrases)))}"

            # Insert into parsing_corrections (ON CONFLICT update if same pattern exists)
            cursor.execute("""
                INSERT INTO parsing_corrections
                (airport_code, atis_pattern, correction_type, expected_arriving, expected_departing,
                 success_rate, times_applied, created_from_review_id)
                VALUES (%s, %s, 'human_review', %s, %s, 1.0, 0, %s)
                ON CONFLICT (airport_code, atis_pattern) DO UPDATE
                SET expected_arriving = EXCLUDED.expected_arriving,
                    expected_departing = EXCLUDED.expected_departing,
                    created_from_review_id = EXCLUDED.created_from_review_id,
                    success_rate = 1.0
            """, (
                error_report['airport_code'],
                pattern_key,
                json.dumps(submission.corrected_arriving),
                json.dumps(submission.corrected_departing),
                review_id
            ))

            logger.info(f"Created parsing correction for {error_report['airport_code']}: {pattern_key}")

        conn.commit()

        return {
            "status": "success",
            "review_id": review_id,
            "message": "Review submitted successfully",
            "correction_created": is_actual_correction
        }

    except HTTPException:
        # Re-raise HTTP exceptions as-is (validation errors, not found, etc.)
        conn.rollback()
        raise
    except Exception as e:
        import traceback
        error_details = f"{type(e).__name__}: {str(e) or repr(e)}"
        logger.error(f"Failed to submit review {review_id}: {error_details}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        try:
            conn.rollback()
        except Exception as rollback_err:
            logger.error(f"Rollback also failed: {rollback_err}")
        raise HTTPException(status_code=500, detail=f"Failed to submit review: {error_details}")
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

@app.get("/api/review/item/{config_id}", response_model=ReviewItem)
async def get_review_item(config_id: int):
    """Get a specific review item by error report ID"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                er.id,
                er.airport_code,
                er.reported_at,
                er.current_atis_id,
                er.paired_atis_id,
                er.parsed_arriving_runways,
                er.parsed_departing_runways,
                er.confidence_score,
                er.reviewed,
                er.corrected_arriving_runways,
                er.corrected_departing_runways,
                er.reviewer_notes,
                ad_current.datis_text as current_atis_text,
                ad_current.collected_at as current_collected_at,
                ad_paired.datis_text as paired_atis_text,
                ad_paired.collected_at as paired_collected_at
            FROM error_reports er
            JOIN atis_data ad_current ON er.current_atis_id = ad_current.id
            LEFT JOIN atis_data ad_paired ON er.paired_atis_id = ad_paired.id
            WHERE er.id = %s
        """, (config_id,))

        report = cursor.fetchone()

        if not report:
            raise HTTPException(status_code=404, detail="Review item not found")

        # Build combined ATIS text if there's a pair
        # ALWAYS show ARR above DEP (since arrival input is above departure input)
        if report['paired_atis_id']:
            if 'ARR INFO' in report['current_atis_text']:
                # Current is ARR, paired is DEP
                atis_text = f"ARR INFO:\n{report['current_atis_text']}\n\n{'='*60}\n\nDEP INFO:\n{report['paired_atis_text']}"
            else:
                # Current is DEP, paired is ARR - swap order to show ARR first
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
            warnings.append("⚠️ RECIPROCAL RUNWAYS DETECTED - Data probably wrong")

        # Determine source for warning
        if report.get('reported_by') == 'computer':
            warnings.append("🤖 Computer-detected issue")
        else:
            warnings.append("👤 User-reported error")

        return ReviewItem(
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
        )

    finally:
        conn.close()

@app.get("/api/review/navigate/{config_id}/{direction}")
async def navigate_review(
    config_id: int,
    direction: str,
    include_reviewed: bool = False,
    source_filter: Optional[str] = Query(default=None, description="Filter by reported_by: 'user', 'computer', or 'all'")
):
    """Get next or previous review item with optional source filtering"""

    if direction not in ['next', 'prev']:
        raise HTTPException(status_code=400, detail="Direction must be 'next' or 'prev'")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Build WHERE clause based on filters
        where_conditions = []

        if not include_reviewed:
            where_conditions.append("er.reviewed = FALSE")

        if source_filter and source_filter != 'all':
            where_conditions.append(f"er.reported_by = '{source_filter}'")

        where_clause = "AND " + " AND ".join(where_conditions) if where_conditions else ""

        # Find next/previous config
        if direction == 'next':
            cursor.execute(f"""
                SELECT er.id
                FROM error_reports er
                WHERE er.id > %s
                  {where_clause}
                ORDER BY er.id ASC
                LIMIT 1
            """, (config_id,))
        else:  # prev
            cursor.execute(f"""
                SELECT er.id
                FROM error_reports er
                WHERE er.id < %s
                  {where_clause}
                ORDER BY er.id DESC
                LIMIT 1
            """, (config_id,))

        result = cursor.fetchone()

        if not result:
            return {"next_id": None, "message": "No more items in this direction"}

        return {"next_id": result['id']}

    finally:
        conn.close()

@app.get("/api/review/stats", response_model=ReviewStats)
async def get_review_stats():
    """Get review statistics for error reports (excludes recently reviewed airports)"""

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Count pending: airports with low-confidence reports that haven't been reviewed in last 2 hours
        # This matches the pending query logic exactly
        cursor.execute("""
            WITH recently_reviewed_airports AS (
                SELECT DISTINCT airport_code
                FROM error_reports
                WHERE reviewed = TRUE
                  AND reviewed_at > NOW() - INTERVAL '2 hours'
            ),
            recent_reports AS (
                SELECT DISTINCT ON (airport_code)
                    id,
                    airport_code,
                    reviewed,
                    reported_by,
                    reported_at,
                    confidence_score
                FROM error_reports
                WHERE reported_at > NOW() - INTERVAL '1 hour'
                  AND airport_code NOT IN (SELECT airport_code FROM recently_reviewed_airports)
                ORDER BY airport_code, reported_at DESC
            )
            SELECT
                COUNT(*) FILTER (WHERE confidence_score < 1.0) as pending
            FROM recent_reports
        """)
        pending = cursor.fetchone()['pending']

        # Count reviewed in the last 2 hours (how many airports you've handled)
        cursor.execute("""
            SELECT COUNT(DISTINCT airport_code) as reviewed
            FROM error_reports
            WHERE reviewed = TRUE
              AND reviewed_at > NOW() - INTERVAL '2 hours'
        """)
        reviewed = cursor.fetchone()['reviewed']

        # Count by source (for the pending items only)
        cursor.execute("""
            WITH recently_reviewed_airports AS (
                SELECT DISTINCT airport_code
                FROM error_reports
                WHERE reviewed = TRUE
                  AND reviewed_at > NOW() - INTERVAL '2 hours'
            ),
            recent_reports AS (
                SELECT DISTINCT ON (airport_code)
                    airport_code,
                    reported_by,
                    reported_at,
                    confidence_score
                FROM error_reports
                WHERE reported_at > NOW() - INTERVAL '1 hour'
                  AND airport_code NOT IN (SELECT airport_code FROM recently_reviewed_airports)
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
            total_reports=pending + reviewed,
            unreviewed_count=pending,
            reviewed_count=reviewed,
            by_source=by_source
        )

    finally:
        conn.close()

@app.get("/api/dashboard/current-airports", response_model=List[AirportStatus])
async def get_current_airports():
    """Get current status for all airports with their 4 most recent runway changes"""
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Get latest runway config for each airport
        cursor.execute("""
            WITH latest_configs AS (
                SELECT DISTINCT ON (airport_code)
                    airport_code,
                    arriving_runways,
                    departing_runways,
                    traffic_flow,
                    created_at
                FROM runway_configs
                ORDER BY airport_code, created_at DESC
            )
            SELECT * FROM latest_configs
            ORDER BY airport_code
        """)
        
        airports_data = cursor.fetchall()
        airport_statuses = []
        
        for airport_row in airports_data:
            airport_code = airport_row['airport_code']
            
            # Get 4 most recent runway changes for this airport
            cursor.execute("""
                SELECT
                    change_time,
                    from_config,
                    to_config,
                    duration_minutes
                FROM runway_changes
                WHERE airport_code = %s
                ORDER BY change_time DESC
                LIMIT 4
            """, (airport_code,))
            
            changes = cursor.fetchall()
            recent_changes = []
            
            for change in changes:
                recent_changes.append(RunwayChangeItem(
                    time=change['change_time'].isoformat(),
                    from_arriving=change['from_config'].get('arriving', []) if change['from_config'] else [],
                    from_departing=change['from_config'].get('departing', []) if change['from_config'] else [],
                    to_arriving=change['to_config'].get('arriving', []) if change['to_config'] else [],
                    to_departing=change['to_config'].get('departing', []) if change['to_config'] else [],
                    duration_minutes=change['duration_minutes']
                ))
            
            airport_statuses.append(AirportStatus(
                airport_code=airport_code,
                arriving=airport_row['arriving_runways'] or [],
                departing=airport_row['departing_runways'] or [],
                flow=airport_row['traffic_flow'] or 'UNKNOWN',
                last_change=airport_row['created_at'].isoformat() if airport_row['created_at'] else None,
                recent_changes=recent_changes
            ))
        
        return airport_statuses
        
    finally:
        conn.close()

# Privacy Policy
@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    """Serve the privacy policy page"""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Privacy Policy - Runways in Use</title>
        <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            :root {
                --bg-primary: #0a0e14;
                --bg-secondary: #151a21;
                --border-color: #2d3748;
                --text-primary: #e5e7eb;
                --text-secondary: #9ca3af;
                --accent-blue: #3b82f6;
            }
            body {
                font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: var(--bg-primary);
                color: var(--text-primary);
                line-height: 1.8;
            }
            .container {
                max-width: 800px;
                margin: 0 auto;
                padding: 48px 24px;
            }
            h1 {
                font-size: 28px;
                font-weight: 600;
                margin-bottom: 8px;
            }
            .subtitle {
                color: var(--text-secondary);
                font-size: 14px;
                margin-bottom: 32px;
            }
            h2 {
                font-size: 18px;
                font-weight: 600;
                margin-top: 32px;
                margin-bottom: 12px;
                color: var(--accent-blue);
            }
            p, ul {
                margin-bottom: 16px;
                color: var(--text-secondary);
            }
            ul {
                padding-left: 24px;
            }
            li {
                margin-bottom: 8px;
            }
            a {
                color: var(--accent-blue);
                text-decoration: none;
            }
            a:hover {
                text-decoration: underline;
            }
            .back-link {
                display: inline-block;
                margin-bottom: 24px;
                font-size: 14px;
            }
            .footer {
                margin-top: 48px;
                padding-top: 24px;
                border-top: 1px solid var(--border-color);
                text-align: center;
                color: var(--text-secondary);
                font-size: 12px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/dashboard" class="back-link">&larr; Back to Dashboard</a>
            <h1>Privacy Policy</h1>
            <p class="subtitle">Last updated: November 2024</p>

            <h2>Overview</h2>
            <p>Runways in Use is committed to protecting your privacy. This policy explains what information we collect and how we use it.</p>

            <h2>Information We Collect</h2>
            <p><strong>We do not collect any personal information.</strong> Specifically:</p>
            <ul>
                <li>We do not require user registration or accounts</li>
                <li>We do not collect names, email addresses, or contact information</li>
                <li>We do not use tracking cookies or analytics services</li>
                <li>We do not collect IP addresses for tracking purposes</li>
            </ul>

            <h2>Local Storage</h2>
            <p>The website uses your browser's local storage to remember your pinned airports. This data:</p>
            <ul>
                <li>Is stored only on your device</li>
                <li>Is never transmitted to our servers</li>
                <li>Can be cleared by clearing your browser data</li>
            </ul>

            <h2>Data Sources</h2>
            <p>All runway information displayed on this site is derived from publicly available D-ATIS (Digital Automatic Terminal Information Service) data provided by the FAA.</p>

            <h2>Third-Party Links</h2>
            <p>This site contains links to FlightRadar24 and other third-party websites. We are not responsible for the privacy practices of these external sites.</p>

            <h2>Changes to This Policy</h2>
            <p>We may update this privacy policy from time to time. Any changes will be posted on this page.</p>

            <h2>Contact</h2>
            <p>For questions about this privacy policy, please visit our <a href="https://github.com/L13w" target="_blank">GitHub page</a>.</p>

            <div class="footer">
                <p>&copy; 2024 Inertial Navigation LLC. <a href="/terms">Terms of Use</a> | <a href="/privacy">Privacy Policy</a></p>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# Terms of Use
@app.get("/terms", response_class=HTMLResponse)
async def terms_of_use():
    """Serve the terms of use page"""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Terms of Use - Runways in Use</title>
        <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            :root {
                --bg-primary: #0a0e14;
                --bg-secondary: #151a21;
                --border-color: #2d3748;
                --text-primary: #e5e7eb;
                --text-secondary: #9ca3af;
                --accent-blue: #3b82f6;
                --accent-amber: #f59e0b;
            }
            body {
                font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: var(--bg-primary);
                color: var(--text-primary);
                line-height: 1.8;
            }
            .container {
                max-width: 800px;
                margin: 0 auto;
                padding: 48px 24px;
            }
            h1 {
                font-size: 28px;
                font-weight: 600;
                margin-bottom: 8px;
            }
            .subtitle {
                color: var(--text-secondary);
                font-size: 14px;
                margin-bottom: 32px;
            }
            h2 {
                font-size: 18px;
                font-weight: 600;
                margin-top: 32px;
                margin-bottom: 12px;
                color: var(--accent-blue);
            }
            p, ul {
                margin-bottom: 16px;
                color: var(--text-secondary);
            }
            ul {
                padding-left: 24px;
            }
            li {
                margin-bottom: 8px;
            }
            a {
                color: var(--accent-blue);
                text-decoration: none;
            }
            a:hover {
                text-decoration: underline;
            }
            .back-link {
                display: inline-block;
                margin-bottom: 24px;
                font-size: 14px;
            }
            .warning-box {
                background: rgba(245, 158, 11, 0.15);
                border: 1px solid var(--accent-amber);
                border-radius: 8px;
                padding: 16px;
                margin: 24px 0;
                color: #fbbf24;
            }
            .warning-box strong {
                display: block;
                margin-bottom: 8px;
            }
            .footer {
                margin-top: 48px;
                padding-top: 24px;
                border-top: 1px solid var(--border-color);
                text-align: center;
                color: var(--text-secondary);
                font-size: 12px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <a href="/dashboard" class="back-link">&larr; Back to Dashboard</a>
            <h1>Terms of Use</h1>
            <p class="subtitle">Last updated: November 2024</p>

            <div class="warning-box">
                <strong>IMPORTANT DISCLAIMER</strong>
                This website is experimental and provided for informational purposes only. The data displayed is NOT guaranteed to be accurate and should NEVER be used for flight planning, navigation, or any aviation operational decisions.
            </div>

            <h2>Acceptance of Terms</h2>
            <p>By accessing and using Runways in Use ("the Service"), you accept and agree to be bound by these Terms of Use.</p>

            <h2>Service Description</h2>
            <p>Runways in Use is an experimental service that attempts to parse and display runway configuration information from publicly available D-ATIS data. The service is provided on an "as is" and "as available" basis.</p>

            <h2>No Warranties</h2>
            <p>We make no warranties or representations about the accuracy, reliability, completeness, or timeliness of the information provided. Specifically:</p>
            <ul>
                <li>Data may be incorrect, incomplete, or outdated</li>
                <li>The parsing algorithms are experimental and may produce errors</li>
                <li>Service availability is not guaranteed</li>
                <li>The service may be discontinued at any time without notice</li>
            </ul>

            <h2>Limitation of Liability</h2>
            <p>Under no circumstances shall Inertial Navigation LLC, its owners, employees, or contributors be liable for any direct, indirect, incidental, special, consequential, or exemplary damages arising from:</p>
            <ul>
                <li>Your use of or inability to use the Service</li>
                <li>Any errors or inaccuracies in the displayed information</li>
                <li>Any decisions made based on information from this Service</li>
                <li>Service interruptions or discontinuation</li>
            </ul>

            <h2>Appropriate Use</h2>
            <p>This Service is intended for:</p>
            <ul>
                <li>General interest and educational purposes</li>
                <li>Flight simulation and virtual aviation</li>
                <li>Situational awareness (with verification from official sources)</li>
            </ul>
            <p>This Service is NOT intended for:</p>
            <ul>
                <li>Actual flight planning or navigation</li>
                <li>Operational aviation decisions</li>
                <li>Any use where accuracy is critical</li>
            </ul>

            <h2>Official Sources</h2>
            <p>For accurate and authoritative runway information, always consult official sources such as:</p>
            <ul>
                <li>Live ATIS broadcasts on published frequencies</li>
                <li>Air Traffic Control</li>
                <li>Official FAA publications and NOTAMs</li>
                <li>Your airline or flight operations department</li>
            </ul>

            <h2>Service Availability</h2>
            <p>We make no guarantees regarding:</p>
            <ul>
                <li>Uptime or availability of the Service</li>
                <li>Continued operation of the Service</li>
                <li>Response time or performance</li>
                <li>Data freshness or update frequency</li>
            </ul>

            <h2>Changes to Terms</h2>
            <p>We reserve the right to modify these terms at any time. Continued use of the Service after changes constitutes acceptance of the new terms.</p>

            <h2>License</h2>
            <p>This software is licensed under the <a href="https://polyformproject.org/licenses/noncommercial/1.0.0/" target="_blank">PolyForm Noncommercial 1.0.0</a> license.</p>

            <h2>Contact</h2>
            <p>For questions about these terms, please visit our <a href="https://github.com/L13w" target="_blank">GitHub page</a>.</p>

            <div class="footer">
                <p>&copy; 2024 Inertial Navigation LLC. <a href="/terms">Terms of Use</a> | <a href="/privacy">Privacy Policy</a></p>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Simple health check endpoint"""
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
