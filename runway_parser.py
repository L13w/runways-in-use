#!/usr/bin/env python3
"""
Runway Parser
Extracts runway configuration from ATIS text using pattern matching
"""

import re
import logging
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)

class TrafficFlow(Enum):
    NORTH = "NORTH"
    SOUTH = "SOUTH"
    EAST = "EAST"
    WEST = "WEST"
    NORTHEAST = "NORTHEAST"
    NORTHWEST = "NORTHWEST"
    SOUTHEAST = "SOUTHEAST"
    SOUTHWEST = "SOUTHWEST"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"

@dataclass
class RunwayConfiguration:
    """Standardized runway configuration"""
    airport_code: str
    timestamp: datetime
    information_letter: Optional[str]
    arriving_runways: List[str]
    departing_runways: List[str]
    traffic_flow: str
    configuration_name: Optional[str]
    raw_text: str
    confidence_score: float
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        return d

class RunwayParser:
    def __init__(self):
        # Compile regex patterns for efficiency
        self.approach_patterns = [
            # "APPROACH IN USE ILS 22L, ILS 22R" OR "APPROACH IN USE ILS RY 22L, ILS RY 22R"
            # Multiple ILS/RNAV keywords - RWY keyword is OPTIONAL
            re.compile(r'(?:APCH|APPROACH|APCHS|APPROACHES)\s+(?:IN\s+USE\s+)?(?:ILS|RNAV|VISUAL|VOR|GPS|LOC)\s+(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?)(?:\s*,\s*(?:ILS|RNAV|VISUAL|VOR|GPS|LOC)\s+(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # "ILS 27, ILS 22L APCH" or "EXPECT ILS 27, ILS 22L APCH" - comma-separated ILS with APCH at end
            # Handles KBOS case where "APCH" comes after the last runway, not after each one
            re.compile(r'(?:EXPECT\s+)?(?:ILS|RNAV|VISUAL|VOR|GPS|LOC)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*(?:ILS|RNAV|VISUAL|VOR|GPS|LOC)\s+([0-9]{1,2}[LCR]?))+\s+(?:APCH|APPROACH)', re.IGNORECASE),
            # "SIMULTANEOUS APCHS IN USE VIS 26R, ILS 27L, VIS 28" - mixed visual and ILS approaches
            # Handles KATL case with comma-separated mix of VIS and ILS
            re.compile(r'(?:SIMUL|SIMULTANEOUS)\s+(?:APCH|APPROACH|APCHS|APPROACHES)\s+IN\s+USE\s+(?:(?:VIS|VISUAL|ILS|RNAV|VOR|GPS|LOC)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*)?)+', re.IGNORECASE),
            # "SIMULTANEOUS APPCHS, ILS RWY 17L, 18R" - SIMUL APPCHS with comma then approach type
            re.compile(r'(?:SIMUL|SIMULTANEOUS)\s+(?:APCH|APPROACH|APCHS|APPROACHES)\s*,\s*(?:ILS|RNAV|VISUAL|VOR|GPS|LOC)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # "RNAV Y RWY 10L, SIMUL, ILS, RWY 10R" - different approach types comma-separated
            re.compile(r'(?:ILS|RNAV|VISUAL|VOR|GPS|LOC)\s+(?:[YZ]\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*(?:SIMUL|SIMULTANEOUS)?\s*,?\s*(?:ILS|RNAV|VISUAL|VOR|GPS|LOC)\s*,?\s*(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # "ILS, RWY 28L, AND, RWY 28R" - comma-separated with AND, multiple RWY keywords
            re.compile(r'(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s*,\s*(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*(?:AND\s*,\s*)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # Abbreviated format: "ILS 22L, DEP 22R" or "ILS RWY 27, DEP 33L" - capture ILS/RNAV runway before DEP keyword
            # RWY keyword is optional, handles both "ILS 27" and "ILS RWY 27"
            re.compile(r'(?:ILS|RNAV|VOR|GPS|LOC)\s+(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?)(?=\s*[,\.]\s*DEP)', re.IGNORECASE),
            # SIMUL VISUAL APCH TO RWYS, 36L, 35C, 35R, 31R - captures comma-separated runway lists
            re.compile(r'(?:SIMUL|SIMULTANEOUS)?\s*(?:VISUAL|ILS|RNAV)?\s*(?:APCH|APPROACH|APCHS|APPROACHES)\s+(?:TO\s+)?(?:RWYS?|RYS|RY)\s*,\s*([0-9]{1,2}[LCR]?)(?:\s*,\s*([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "EXPECT VISUAL APCH RWYS 36C 36L 36R" - space-separated runways after APCH RWYS (KCLT pattern)
            re.compile(r'(?:EXPECT\s+)?(?:SIMUL|SIMULTANEOUS)?\s*(?:VISUAL|ILS|RNAV)?\s*(?:APCH|APPROACH|APCHS|APPROACHES)\s+(?:TO\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s+([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # Pattern for abbreviated approaches: "ILS, AND VA, RWYS 30 AND 28R" (VA = Visual Approach)
            re.compile(r'(?:ILS|VISUAL|RNAV|VOR|GPS|LOC|VA)\s*,\s*(?:AND\s+)?(?:ILS|VISUAL|RNAV|VOR|GPS|LOC|VA)?\s*,?\s*(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # Pattern for: "ILS, RYS 16R AND 16L, APCH IN USE" (runway info between approach type and APCH keyword)
            re.compile(r'(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s*,\s*(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*(?:\s*,\s*)?(?:APCH|APPROACH|APCHS|APPROACHES|VISUAL\s+APPROACH)', re.IGNORECASE),
            # ARRIVALS EXPECT ILS RWY 8R, RWY 9, RWY 12 - handles multiple RWY keywords in comma list
            re.compile(r'(?:ARRIVALS?)\s+(?:EXPECT\s+)?(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # Complex ARRIVALS EXPECT patterns: "ARRIVALS EXPECT ILS OR RNAV Y RY 26R, ILS OR RNAV Y RY 26L, ILS OR RNAV Y RY 27"
            # Matches multiple comma-separated approach types with runways
            re.compile(r'(?:ARRIVALS?)\s+(?:EXPECT\s+)?(?:(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:OR\s+)?(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)?\s*(?:[YZ]\s+)?(?:RWYS?|RYS|RY)\s+[0-9]{1,2}[LCR]?(?:\s*,\s*)?)+', re.IGNORECASE),
            # Make RWY keyword optional for comma-separated runways (like departure patterns)
            # Added RYS to handle common ATIS typo (e.g., "RYS 16R AND 16L")
            re.compile(r'(?:EXPECT\s+)?(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:OR\s+)?(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)?\s*(?:APCH|APPROACH|APCHS|APPROACHES)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "EXPECT VISUAL APPROACH TO RWY X, RWY Y" - KCVG pattern with "TO" before RWY
            re.compile(r'(?:EXPECT\s+)?(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:OR\s+)?(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)?\s*(?:APCH|APPROACH|APCHS|APPROACHES)\s+TO\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            re.compile(r'(?:APCH|APPROACH|APCHS|APPROACHES)\s+(?:IN\s+USE\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "SIMULTANEOUS ARRIVAL AND, DEPARTURE OPERATIONS ARE IN USE, ON RY 22R AND RY 22L"
            # Extract runways from this pattern (used for both arrivals and departures)
            re.compile(r'(?:SIMUL|SIMULTANEOUS)\s+(?:ARRIVAL\s+AND\s*,?\s*DEPARTURE\s+OPERATIONS|DEPENDENT)\s+(?:ARE\s+)?(?:IN\s+USE\s*)?,?\s*(?:ON\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s+(?:AND|OR)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # "LNDG/DEPG RWYS 4/8" - slash-separated runways (used for both arrivals and departures)
            re.compile(r'(?:LNDG|LANDING)\/(?:DEPG|DEPARTING)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)\/([0-9]{1,2}[LCR]?)(?:\/([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # LNDG/LANDING/LDG + RWYS: "LNDG RWYS 35L AND RIGHT" or "LNDG RWY 28L, 28R"
            # Also handles "LNDG AND DEPG RWY X AND RWY Y" for combined statements
            # BUT NOT "LANDING AND DEPARTING" (that's a departure-only pattern despite the word "LANDING")
            re.compile(r'(?:LNDG|LDG|LAND|ARVNG)\s+(?:AND\s+(?:DEPG|DEPARTING)\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # Standalone "LANDING" (not followed by AND DEPARTING) - arrival only
            re.compile(r'LANDING\s+(?!AND\s+DEPARTING)(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            re.compile(r'(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*\s+(?:FOR\s+)?(?:APCH|APPROACH|LANDING|ARRIVAL)', re.IGNORECASE),
            # RNAV AND VISUAL APCHS: "RNAV AND VISUAL APCHS IN USE" (without specific runways mentioned before this)
            re.compile(r'(?:RNAV|ILS|VISUAL)\s+(?:AND\s+)?(?:RNAV|ILS|VISUAL)?\s+(?:APCHS|APPROACHES)\s+IN\s+USE', re.IGNORECASE),
            # Shortened RNAV approach: "RNAV 27" or "RNAV Y 27" or "RNAV Z 27"
            re.compile(r'RNAV\s+(?:[YZ]\s+)?([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:RNAV\s+)?(?:[YZ]\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "ILS RY 34R RNAV Y RY 35 RNAV Z RY 34L" - multiple approach types with RY (KSLC pattern)
            # Captures sequence of ILS/RNAV [Y/Z] RY XX separated by spaces
            re.compile(r'(?:ILS|RNAV|VOR|GPS|LOC)\s+(?:[YZ]\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s+(?:ILS|RNAV|VOR|GPS|LOC)\s+(?:[YZ]\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # Named visual approaches: "FMS BRIDGE RY 28R AND TIPP TOE RY 28L APP IN USE"
            # Matches: [approach name] RY [runway] [AND [approach name] RY [runway]]* APP IN USE
            re.compile(r'(?:[A-Z]+(?:\s+[A-Z]+)*\s+)?RY\s+([0-9]{1,2}[LCR]?)(?:\s+AND\s+(?:[A-Z]+(?:\s+[A-Z]+)*\s+)?RY\s+([0-9]{1,2}[LCR]?))*\s+APP\s+IN\s+USE', re.IGNORECASE),
            # "ILS RWY 23 IN USE" - simple ILS runway in use (KPVD pattern)
            re.compile(r'(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)\s+IN\s+USE', re.IGNORECASE),
            # "LAND RY 31" - simple LAND keyword (KLGA pattern)
            re.compile(r'LAND\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "EXPECT ILS RWY 23L, 23R" - ILS/approach type with RWY without APPROACH keyword (KRDU pattern)
            re.compile(r'(?:EXPECT\s+)?(?:ILS|RNAV|VOR|GPS|LOC)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*)([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "ILS RWY 35L AND 35R" - ILS with multiple runways using AND separator (KOKC pattern)
            re.compile(r'(?:ILS|RNAV|VOR|GPS|LOC)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s+(?:AND|OR)\s+([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
        ]

        self.departure_patterns = [
            # "SIMULTANEOUS ARRIVAL AND, DEPARTURE OPERATIONS ARE IN USE, ON RY 22R AND RY 22L"
            # Extract runways from this pattern (used for both arrivals and departures)
            re.compile(r'(?:SIMUL|SIMULTANEOUS)\s+(?:ARRIVAL\s+AND\s*,?\s*DEPARTURE\s+OPERATIONS|DEPENDENT)\s+(?:ARE\s+)?(?:IN\s+USE\s*)?,?\s*(?:ON\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s+(?:AND|OR)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # "SIMUL DEPS IN USE, EXPECT RY 18L, RY 18C" - simultaneous departures with comma-separated runways
            re.compile(r'(?:SIMUL|SIMULTANEOUS)\s+(?:DEPS?|DEPARTURES?)\s+IN\s+USE\s*,?\s*(?:EXPECT\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # "SIMUL DEPS IN USE RY 18R 18C 18L" - space-separated runways (KMEM pattern)
            re.compile(r'(?:SIMUL|SIMULTANEOUS)\s+(?:DEPS?|DEPARTURES?)\s+IN\s+USE\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s+([0-9]{1,2}[LCR]?))+', re.IGNORECASE),
            # NOTE: "LNDG/DEPG RWYS 4/8" moved to combined_patterns (line 125)
            # "DEPG RWYS RWY 10L AND 10R" - double RWY keyword (KFLL case)
            # Negative lookbehind prevents matching when part of "LANDING AND DEPARTING"
            re.compile(r'(?<!LNDG/)(?<!LANDING/)(?<!LANDING\sAND\s)(?:DEPG|DEP|DEPARTURE|DEPARTING|DERARTING|DEPS|DEPARTURES)\s+(?:RWYS?|RYS|RY)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:\s+(?:AND|OR)\s+([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "DEPG RWYS, 26L, 27R" OR "DEPG RWYS 36C, 36L, 36R" - comma after RWYS OR space before first runway
            # Handles both formats: "DEPG RWYS, X, Y" and "DEPG RWYS X, Y, Z"
            # Negative lookbehind prevents matching when part of "LNDG/DEPG" or "LANDING AND DEPARTING"
            re.compile(r'(?<!LNDG/)(?<!LANDING/)(?<!LANDING\sAND\s)(?:DEPG|DEP|DEPARTURE|DEPARTING|DERARTING|DEPS|DEPARTURES)\s+(?:RWYS?|RYS|RY)\s*,?\s*([0-9]{1,2}[LCR]?)(?:\s*,\s*([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # DEPG/DEP with RWYS - allow comma-separated without repeating RWYS: "DEPG RWYS 1L, 1R"
            # Added RYS to handle common ATIS typo
            # Added DERARTING to handle common ATIS typo (KSEA example)
            # Negative lookbehind prevents matching when part of "LNDG/DEPG" or "LANDING AND DEPARTING"
            re.compile(r'(?<!LNDG/)(?<!LANDING/)(?<!LANDING\s)(?<!LNDG\s)(?<!LANDING\sAND\s)(?:DEPG|DEP|DEPARTURE|DEPARTING|DERARTING|DEPS|DEPARTURES)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            re.compile(r'(?:TAKEOFF|TKOF|TAKE\s+OFF)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            re.compile(r'(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)([0-9]{1,2}[LCR]?))*\s+(?:FOR\s+)?(?:DEPG|DEP|DEPARTURE|TAKEOFF)', re.IGNORECASE),
            # Shortened departure: "DEP 33L" or "DEPG 16R" (without RWY keyword)
            re.compile(r'(?:DEPG|DEP)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:DEPG|DEP\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "DEPART RY 31" - DEPART keyword with RY (KLGA pattern)
            re.compile(r'DEPART\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "IN PROG" (in progress) patterns: "SIMUL INSTR DEPARTURES IN PROG RWYS 24 AND 25"
            re.compile(r'(?:SIMUL\s+)?(?:INSTR\s+)?(?:DEPARTURES?|DEPS?)\s+IN\s+PROG(?:RESS)?\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
            # "FOR BOTH RWYS X AND Y" patterns in departure context
            re.compile(r'(?:FOR\s+)?BOTH\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)\s+AND\s+(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?)', re.IGNORECASE),
            # "DEPS EXP RWYS 22L 28R" - departures expect runways (KORD pattern)
            re.compile(r'(?:DEPS?)\s+(?:EXP(?:ECT)?)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s+|\s*,\s*)([0-9]{1,2}[LCR]?))*', re.IGNORECASE),
        ]

        self.combined_patterns = [
            # Patterns that indicate runways used for BOTH arrivals and departures
            # These patterns should ONLY match when there's true ambiguity (no arrival/departure context)

            # "LNDG/DEPG RWYS 4/8" or "LNDG AND DEPG RWY 28L, 28R" - landing and departing with slash or comma
            re.compile(r'(?:LNDG|LANDING)\s*(?:/|AND)\s*(?:DEPG|DEP|DEPARTING)\s+(?:RWYS?|RYS|RY)\s*([0-9]{1,2}[LCR]?)(?:(?:\s*/\s*|\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s*)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),

            # "ILS APCH 14R, 14L, 18 IN USE" - approach type with "IN USE" and no arrival/departure keyword
            # REQUIRES "IN USE" to avoid matching arrival-only approach patterns
            re.compile(r'(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:APCH|APPROACH|APCHS|APPROACHES)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*([0-9]{1,2}[LCR]?))*\s+IN\s+USE', re.IGNORECASE),

            # "VISUAL APCH 5R, 5L" without "IN USE" - explicit visual approach comma-separated
            re.compile(r'VISUAL\s+(?:APCH|APPROACH|APCHS|APPROACHES)\s+([0-9]{1,2}[LCR]?)(?:\s*,\s*([0-9]{1,2}[LCR]?))+', re.IGNORECASE),

            # "LANDING AND DEPARTING 34, 29" or "LANDING AND DEPARTING RWY 27L" - explicit statement for both operations
            # NOTE: This pattern also handles single runway: "LANDING AND DEPARTING 16" → both ARR and DEP get 16
            re.compile(r'LANDING\s+AND\s+DEPARTING\s+(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),

            # "ARVNG AND DEPG RWY 8 AND RWY 15" - explicit statement for both operations
            re.compile(r'(?:ARVNG|ARRIVING)\s+AND\s+(?:DEPG|DEP|DEPARTING)\s+(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),

            # Generic "RWYS IN USE" without arrival/departure context
            re.compile(r'(?:RWYS?|RYS|RY)\s+(?:IN\s+USE\s+)?([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),

            # Simultaneous approaches
            re.compile(r'(?:SIMUL|SIMULTANEOUS)\s+(?:APCHS|APPROACHES)\s+(?:IN\s+USE\s*,?\s*)?(?:TO\s+)?(?:RWYS?|RYS|RY)\s+([0-9]{1,2}[LCR]?)(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?))*', re.IGNORECASE),

            # "17L, 17R & 13 IN USE" - runway numbers without RWY prefix, comma/ampersand separated
            # Handles KOKC-style pattern where runways listed directly without RWY keyword
            re.compile(r'([0-9]{1,2}[LCR]?)(?:\s*[,&]\s*|\s+(?:AND|OR)\s+)([0-9]{1,2}[LCR]?)(?:(?:\s*[,&]\s*|\s+(?:AND|OR)\s+)([0-9]{1,2}[LCR]?))*\s+IN\s+USE', re.IGNORECASE),
        ]
        
        # Airport-specific configuration names
        self.airport_configs = {
            'KSEA': {
                'south': ['16L', '16C', '16R'],
                'north': ['34L', '34C', '34R']
            },
            'KSFO': {
                'west': ['28L', '28R'],
                'east': ['10L', '10R'],
                'southeast': ['19L', '19R'],
                'northwest': ['01L', '01R']
            },
            'KLAX': {
                'west': ['24L', '24R', '25L', '25R'],
                'east': ['06L', '06R', '07L', '07R']
            }
        }

        # Airports known to publish arrival-only ATIS (departures not mentioned)
        # These should get 100% confidence when arrivals are found and departures are empty
        self.arrival_only_airports = {
            'KADW', 'KALB', 'KRSW', 'KPVD', 'KOAK', 'KPDX', 'KDAL',
            'KCMH', 'KAUS', 'KFLL', 'KIND', 'KTPA', 'KTUL', 'KBWI',
            'KJFK', 'KBOS', 'KORD',  # These sometimes publish arrival-only
            'KGSO', 'KLIT', 'KMCI',  # Verified from human reviews
            'KCHS', 'KMDW', 'KPHL', 'KPIT', 'KPBI', 'KIAH',  # Added from Nov 2024 human reviews
            'KHOU', 'KRDU',  # Ground assigns departure runways
            'KMIA', 'KSNA', 'KSLC',  # More arrival-only airports
            'KOKC', 'KSDF', 'KSMF'  # Added Dec 2024 - verified from ATIS patterns
        }
    
    def parse(self, airport_code: str, atis_text: str, info_letter: Optional[str] = None) -> RunwayConfiguration:
        """Main parsing method"""
        timestamp = datetime.utcnow()

        # Clean and prepare text
        cleaned_text = self.clean_text(atis_text)
        text_upper = cleaned_text.upper()

        # Use original ATIS text for header detection (ARR INFO/DEP INFO)
        # because clean_text strips the header portion
        original_upper = atis_text.upper()

        # Check for explicit combined operation patterns (these override specific patterns)
        has_landing_and_departing = 'LANDING AND DEPARTING' in text_upper or 'LNDG AND DEPG' in text_upper or 'LNDG/DEPG' in text_upper

        # Extract runways
        arriving = self.extract_arriving_runways(cleaned_text)
        departing = self.extract_departing_runways(cleaned_text)

        # If "LANDING AND DEPARTING" appears, use combined patterns for BOTH
        # This handles cases like "ILS, RWY 16 IN USE. LANDING AND DEPARTING 16."
        # where the arrival pattern matches first but we need the combined pattern
        if has_landing_and_departing:
            combined = self.extract_combined_runways(cleaned_text)
            if combined:
                # If combined found runways, use them for BOTH operations
                arriving = combined
                departing = combined

        # Split ATIS handling: Many airports publish separate ARR INFO and DEP INFO
        # Use original_upper (not cleaned text) because clean_text strips the header
        is_split_arr = 'ARR INFO' in original_upper or 'ARR ATIS' in original_upper
        is_split_dep = 'DEP INFO' in original_upper or 'DEP ATIS' in original_upper

        # For ARR INFO: clear any departures that may have been extracted erroneously
        # ARR INFO only contains arrival information - departures come from separate DEP INFO
        if is_split_arr:
            # If approach patterns didn't find arrivals, try combined patterns
            if not arriving:
                combined = self.extract_combined_runways(cleaned_text)
                if combined:
                    arriving = combined
            # Always clear departures for ARR INFO - they come from separate broadcast
            departing = set()

        # For DEP INFO: clear any arrivals that may have been extracted erroneously
        # DEP INFO only contains departure information - arrivals come from separate ARR INFO
        if is_split_dep:
            # Always clear arrivals for DEP INFO - they come from separate broadcast
            arriving = set()

        # FIX BUG #2 (KADW): Don't use combined patterns to fill departures if arrivals were explicitly found
        # Only use combined patterns when BOTH are missing (true ambiguity)
        # For DEP INFO: if departure patterns didn't find anything, don't use combined
        # For other airports: only use combined if NEITHER arrival NOR departure found
        if not arriving and not departing and not (is_split_dep or is_split_arr) and not has_landing_and_departing:
            combined = self.extract_combined_runways(cleaned_text)
            arriving = combined
            departing = combined
        # NEW: If we found arrivals but no departures, do NOT copy arrivals to departures
        # This fixes KADW where "ILS RWY 19R APPROACH IN USE" was duplicated to departures

        # Determine traffic flow
        flow = self.determine_traffic_flow(arriving, departing)
        
        # Get configuration name if available
        config_name = self.get_configuration_name(airport_code, arriving, departing)
        
        # Calculate confidence score
        confidence = self.calculate_confidence(arriving, departing, cleaned_text)

        # Split ATIS confidence boost: Valid split ATIS should have 100% confidence
        # Use original_upper which still contains the header
        is_split_atis = is_split_arr or is_split_dep
        if is_split_atis:
            # ARR INFO with arrivals only is valid (departures published separately)
            if is_split_arr and arriving and not departing:
                confidence = 1.0
            # DEP INFO with departures only is valid (arrivals published separately)
            elif is_split_dep and departing and not arriving:
                confidence = 1.0
            # Both present means they were paired - also 100% confidence
            elif arriving and departing:
                confidence = 1.0

        # Arrival-only airport whitelist: Boost confidence for known arrival-only airports
        if airport_code in self.arrival_only_airports and arriving and not departing:
            confidence = 1.0

        return RunwayConfiguration(
            airport_code=airport_code,
            timestamp=timestamp,
            information_letter=info_letter,
            arriving_runways=sorted(list(arriving)),
            departing_runways=sorted(list(departing)),
            traffic_flow=flow.value,
            configuration_name=config_name,
            raw_text=atis_text,
            confidence_score=confidence
        )
    
    def extract_relevant_section(self, text: str) -> str:
        """Extract only the relevant section of ATIS text for runway parsing.

        ATIS structure typically:
        1. Header (airport, ATIS letter, time, weather) - IGNORE
        2. Altimeter reading: "A3018 (THREE ZERO ONE EIGHT)" - START MARKER
        3. Runway information - EXTRACT THIS
        4. NOTAMs/Notices: "NOTICE TO AIRMEN", "NOTAM", "READBACK" - END MARKER
        5. Closing - IGNORE

        Returns the section between altimeter and end markers, or full text if markers not found.
        """
        text_upper = text.upper()

        # Find start marker: Altimeter reading pattern "A####" followed by spoken form
        # Examples: "A3018 (THREE ZERO ONE EIGHT)", "A2997 (TWO NINER NINER SEVEN)"
        start_match = re.search(r'A\d{4}\s*\([A-Z\s]+\)', text_upper)

        # Find end markers - these typically come AFTER runway information
        end_patterns = [
            r'\bNOTICE\s+TO\s+AIR\w*\b',  # "NOTICE TO AIRMEN", "NOTICE TO AIR MISSIONS"
            r'\bNOTAMS?\b\.{0,3}',  # "NOTAM", "NOTAMS", "NOTAMS..."
            r'\bREADBACK\s+ALL\s+RWY\b',  # "READBACK ALL RWY HOLD SHORT"
            r'\bADVISE\s+ON\s+INITIAL\b',  # "ADVISE ON INITIAL CONTACT"
            r'\bPILOTS?\s+(?:ARE\s+)?(?:ADVISED|CAUTIONED)\b',  # "PILOTS ARE ADVISED"
            r'\bBIRD\s+ACT(?:IVITY|VTY)\b',  # "BIRD ACTIVITY" - often starts NOTAM section
            r'\.{3}ADVS\s+YOU\s+HAVE\b',  # "...ADVS YOU HAVE INFO X" - closing
        ]

        end_pos = len(text)
        for pattern in end_patterns:
            match = re.search(pattern, text_upper)
            if match and match.start() < end_pos:
                # Only use this end marker if it comes AFTER the start marker
                if start_match is None or match.start() > start_match.end():
                    end_pos = match.start()

        # Extract the relevant section
        if start_match:
            start_pos = start_match.end()
            relevant_section = text[start_pos:end_pos]
            logger.debug(f"Extracted relevant section: chars {start_pos}-{end_pos} of {len(text)}")
            return relevant_section
        else:
            # No altimeter found - return up to end marker
            return text[:end_pos]

    def clean_text(self, text: str) -> str:
        """Clean ATIS text for better pattern matching"""
        # First, extract only the relevant section (between altimeter and NOTAMs)
        text = self.extract_relevant_section(text)

        # Remove extra whitespace
        text = ' '.join(text.split())

        # Remove advisory/warning text that mentions runways but isn't runway assignment
        # Examples: "RWY 30 DEPARTURES ARE ADVISED TO AVOID...", "CAUTION RWY 16L...", etc.
        # IMPORTANT: Be very conservative - only remove text that explicitly contains advisory keywords
        # adjacent to runway mentions to avoid removing legitimate runway assignments
        advisory_patterns = [
            # "RWY 30 DEPARTURES ARE ADVISED TO AVOID" - explicit advisory with departure keyword
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+(?:DEPARTURES?|ARRIVALS?)\s+(?:ARE\s+)?(?:ADVISED|CAUTIONED|WARNED)[^.]*?\.?',
            # "LOW CLOSE IN OBSTACLES FOR RWY 30 DEPARTURES" - NOTAM about obstacles/hazards
            r'(?:OBSTACLES?|HAZARDS?)[^.]{0,50}?FOR\s+RWY?\s+[0-9]{1,2}[LCR]?\s+(?:DEPARTURES?|ARRIVALS?)[^.]*?\.?',
            # Too aggressive - removed: r'(?:CAUTION|WARNING|NOTICE|ADVISE)[:\s]+.*?RWY?\s+[0-9]{1,2}[LCR]?',
            # "RWY 16L AVOID TURNING LEFT" - runway followed by avoid/caution within same phrase
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+[^.]{0,50}?(?:AVOID|WARNING)[^.]*?\.?',
        ]
        for pattern in advisory_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        # Fix spaced runway notation: "R Y 14" → "RY 14", "R W Y 16L" → "RWY 16L"
        # Handles typos where spaces are inserted within runway keywords
        text = re.sub(r'R\s+Y\s+([0-9]{1,2}[LCR]?)', r'RY \1', text, flags=re.IGNORECASE)
        text = re.sub(r'R\s+W\s+Y\s+([0-9]{1,2}[LCR]?)', r'RWY \1', text, flags=re.IGNORECASE)
        text = re.sub(r'R\s+W\s+Y\s+S\s+([0-9]{1,2}[LCR]?)', r'RWYS \1', text, flags=re.IGNORECASE)

        # Convert digit-by-digit runway callouts to standard format
        # "RUNWAY 3 4 LEFT" -> "RWY 34L", "RWY 1 6 RIGHT" -> "RWY 16R"
        def consolidate_runway(match):
            prefix = match.group(1)
            digit1 = match.group(2)
            digit2 = match.group(3)
            suffix = match.group(4) if match.group(4) else ''
            # Convert suffix words to letters
            suffix_map = {'LEFT': 'L', 'RIGHT': 'R', 'CENTER': 'C'}
            suffix_letter = suffix_map.get(suffix.upper(), suffix) if suffix else ''
            return f"{prefix} {digit1}{digit2}{suffix_letter}"

        # Pattern: RUNWAY 3 4 LEFT, RWY 1 6 RIGHT, etc.
        text = re.sub(
            r'(RUNWAY|RUNWAYS|RWY?S?|RY)\s+([0-9])\s+([0-9])\s*(LEFT|RIGHT|CENTER|L|R|C)?',
            consolidate_runway,
            text,
            flags=re.IGNORECASE
        )

        # NEW: Handle "RUNWAY 16 LEFT" (two-digit runway with spelled-out suffix)
        # "RUNWAY 16 LEFT" -> "RWY 16L", "RUNWAY 27 RIGHT" -> "RWY 27R"
        def normalize_spelled_suffix(match):
            prefix = match.group(1) or 'RWY'
            number = match.group(2)
            suffix = match.group(3)
            suffix_map = {'LEFT': 'L', 'RIGHT': 'R', 'CENTER': 'C'}
            suffix_letter = suffix_map.get(suffix.upper(), suffix)
            return f"{prefix} {number}{suffix_letter}"

        text = re.sub(
            r'(RUNWAY|RUNWAYS|RWY?S?|RY)\s+([0-9]{1,2})\s+(LEFT|RIGHT|CENTER)\b',
            normalize_spelled_suffix,
            text,
            flags=re.IGNORECASE
        )

        # Filter out NOTAMs with closures (including digit-by-digit format)
        # "RWY 1 6 LEFT 3 4 RIGHT CLOSED" or "RWY 16L CLOSED"
        closure_patterns = [
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+(?:CLSD|CLOSED)',  # Standard: RWY 16L CLOSED
            r'RWY?\s+[0-9]\s+[0-9]\s+(?:LEFT|RIGHT|CENTER|L|R|C)?\s+(?:CLSD|CLOSED)',  # Digit-by-digit
        ]
        for pattern in closure_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        # Filter out other NOTAMs and equipment status - these are NOT runway operations
        notam_patterns = [
            # GPS/RNAV procedure names: "GPS YANKEE RWY 36C DISREGARD NOTE" (after normalization)
            r'(?:GPS|RNAV|ILS|VOR|LOC)\s+[A-Z]+\s+RWY?\s+[0-9]{1,2}[LCR]?\s+(?:DISREGARD|NOT\s+AVAILABLE|UNAVAILABLE)',
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+(?:INNER|OUTER|MIDDLE)\s+MARKER\s+(?:OTS|OUT\s+OF\s+SERVICE|INOP|U\/S)',
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+(?:REIL|ALS|PAPI|VASI|ILS|LOC|GS|GLIDESLOPE|ALSF|MALSR|MALS|SSALR|SSALS)\s+(?:OTS|OUT\s+OF\s+SERVICE|INOP|U\/S)',
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+(?:OTS|OUT\s+OF\s+SERVICE|INOP|U\/S)',
            # "RY 18R, 36C ALS OTS" or "RY 18R, 36L PAPI OTS" - Comma-separated runways with equipment status
            # This catches the KCLT case where reciprocal runways in equipment NOTAMs were being extracted
            r'RY?\s+[0-9]{1,2}[LCR]?\s*,\s*[0-9]{1,2}[LCR]?\s+(?:REIL|ALS|PAPI|VASI|ILS|LOC|GS|GLIDESLOPE|ALSF|MALSR|MALS|SSALR|SSALS|ERGL)\s+(?:OTS|OUT\s+OF\s+SERVICE|INOP|U\/S)',
            # "RUNWAY 36C, 36L, AND 18R OUTER MARKER OUT OF SERVICE" or "RWY 36C, 36L, AND 18R OUTER MARKER OTS"
            # Matches: RWY X, Y, AND Z MARKER or RWY X, Y, Z MARKER or RWY X AND Y MARKER
            r'RWY?\s+[0-9]{1,2}[LCR]?(?:\s*,\s*[0-9]{1,2}[LCR]?)*(?:,?\s+AND\s+[0-9]{1,2}[LCR]?)?\s+(?:INNER|OUTER|MIDDLE)\s+MARKER\s+(?:OTS|OUT\s+OF\s+SERVICE|INOP|U\/S)',
            # "RWY 33 APCH END" - Approach end markers/equipment (not active runway)
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+(?:APCH|APPROACH)\s+END\b',
            # "RWY 33 DEP END" - Departure end markers
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+(?:DEP|DEPARTURE)\s+END\b',
            # "TWY Y2 CLSD OFF RWY 1C" - taxiway status mentions runway (KIAD issue)
            # Also handles "Y2 CLSD OFF RWY 1C" (without TWY prefix)
            r'(?:TWY\s+)?[A-Z0-9]+\s+(?:CLSD|CLOSED)\s+(?:OFF|BTN|BETWEEN)\s+(?:RUNWAY|RWY)\s+[0-9]{1,2}[LCR]?',
            # "TWY S CLSD BTN RWY, 18C AND TWY W" - taxiway closure between runway and taxiway
            r'TWY?\s+[A-Z0-9]+\s+(?:CLSD|CLOSED)\s+BTN\s+(?:RUNWAY|RWY)\s*,\s*[0-9]{1,2}[LCR]?\s+AND\s+TWY',
            # "PLAN TO EXIT Y4 OR Y6 WHEN LANDING RUNWAY X" - exit instructions (not active runway)
            r'(?:PLAN\s+TO\s+EXIT|EXIT)\s+[A-Z0-9]+\s+(?:OR\s+[A-Z0-9]+\s+)?(?:WHEN\s+)?LANDING\s+(?:RUNWAY|RWY)\s+[0-9]{1,2}[LCR]?',
            # "BAK-12" or "A-GEAR" or "ERGL" equipment mentions with runway
            r'RWY?\s+[0-9]{1,2}[LCR]?\s+(?:DEP|DEPARTURE|APCH|APPROACH)\s+END\s+[A-Z0-9\-]+\s+(?:OTS|OUT\s+OF\s+SERVICE|INOP|U\/S)',
            # "ERGL RWY 18C OTS" - Equipment runway guidance lights out of service
            r'(?:ERGL|REIL|ALS|PAPI|VASI)\s+RWY?\s+[0-9]{1,2}[LCR]?\s+(?:OTS|OUT\s+OF\s+SERVICE|INOP|U\/S)',
        ]
        for pattern in notam_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        # Expand "AND RIGHT" / "AND LEFT" patterns before general processing
        # "35L AND RIGHT" -> "35L AND 35R"
        # "35R AND LEFT" -> "35R AND 35L"
        text = self.expand_and_right_left(text)

        # Standardize runway notation
        text = re.sub(r'RUNWAY', 'RWY', text, flags=re.IGNORECASE)
        text = re.sub(r'RUNWAYS', 'RWYS', text, flags=re.IGNORECASE)

        # Add space between RWY/RY and runway number (e.g., "RWY17L" -> "RWY 17L")
        text = re.sub(r'(RWY?S?|RY)([0-9]{1,2}[LCR]?)', r'\1 \2', text, flags=re.IGNORECASE)

        # Remove periods that might interfere
        text = text.replace('.', ' ')

        return text

    def expand_and_right_left(self, text: str) -> str:
        """Expand 'AND RIGHT' / 'AND LEFT' patterns to explicit runway numbers
        Examples:
          'RWY 35L AND RIGHT' -> 'RWY 35L AND RWY 35R'
          'RWY 35R AND LEFT' -> 'RWY 35R AND RWY 35L'
          'RWY 16C AND LEFT' -> 'RWY 16C AND RWY 16L'
        """
        # Pattern: (optional RWY/RWYS/RY) runway number followed by "AND RIGHT/LEFT"
        def expand_match(match):
            rwy_keyword = match.group(1) or ''  # "RWY", "RWYS", "RY", or empty
            runway = match.group(2)  # Full runway (e.g., "35L")
            direction = match.group(3).upper()  # "RIGHT" or "LEFT"

            # Extract base number and current suffix
            rwy_match = re.match(r'([0-9]{1,2})([LCR])?', runway)
            if not rwy_match:
                return match.group(0)  # Return unchanged if can't parse

            base_num = rwy_match.group(1)
            current_suffix = rwy_match.group(2) or ''

            # Determine new suffix based on direction
            if direction == 'RIGHT':
                new_suffix = 'R'
            elif direction == 'LEFT':
                new_suffix = 'L'
            else:
                return match.group(0)  # Return unchanged

            # Build new runway designation
            new_runway = f"{base_num}{new_suffix}"

            # Return expanded form with RWY keyword if it was present
            if rwy_keyword:
                return f"{rwy_keyword} {runway} AND {rwy_keyword} {new_runway}"
            else:
                return f"{runway} AND {new_runway}"

        # Match pattern: "RWY 35L AND RIGHT" or just "35L AND RIGHT"
        pattern = r'(?:(RWY?S?|RY)\s+)?([0-9]{1,2}[LCR]?)\s+AND\s+(RIGHT|LEFT)\b'
        text = re.sub(pattern, expand_match, text, flags=re.IGNORECASE)

        return text

    def _remove_departure_sections(self, text: str) -> str:
        """Remove explicit departure sections from text to avoid contaminating arrival extraction
        Examples:
          'ARRIVALS RWY 3, 8. DEPG RWY 8.' -> 'ARRIVALS RWY 3, 8. '
          'LANDING RWY 16L. DEPARTING RWY 16C.' -> 'LANDING RWY 16L. '

        IMPORTANT: Do NOT remove departure sections that are part of combined "LNDG AND DEPG" or "ARVNG AND DEPG" statements
        """
        # Remove standalone departure patterns (NOT preceded by LNDG/ARVNG/LANDING)
        departure_removal_patterns = [
            # Standalone DEPG patterns (not preceded by arrival keywords)
            r'(?<!LNDG\s)(?<!LANDING\s)(?<!ARVNG\s)(?<!LAND\s)(?<!LDG\s)(?<!AND\s)(?:DEPG|DEP|DEPARTURE|DEPARTING|DERARTING|DEPS|DEPARTURES)\s+(?:RWYS?|RYS|RY)\s+[0-9]{1,2}[LCR]?(?:\s*,\s*(?:RWYS?|RYS|RY)?\s*[0-9]{1,2}[LCR]?)*',
            # Takeoff patterns
            r'(?:TAKEOFF|TKOF|TAKE\s+OFF)\s+(?:RWYS?|RYS|RY)\s+[0-9]{1,2}[LCR]?(?:\s*,\s*(?:RWYS?|RYS|RY)?\s*[0-9]{1,2}[LCR]?)*',
            # Shortened standalone departure: "DEPG 16L" (not preceded by arrival keywords)
            r'(?<!LNDG\s)(?<!LANDING\s)(?<!ARVNG\s)(?<!LAND\s)(?<!LDG\s)(?<!AND\s)(?:DEPG|DEP)\s+[0-9]{1,2}[LCR]?(?:\s*,\s*[0-9]{1,2}[LCR]?)*(?=\s|$|\.)',
        ]

        for pattern in departure_removal_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        return text

    def _remove_arrival_sections(self, text: str) -> str:
        """Remove explicit arrival sections from text to avoid contaminating departure extraction

        IMPORTANT: Do NOT remove arrival sections that are part of combined "LNDG AND DEPG" or "ARVNG AND DEPG" statements
        """
        arrival_removal_patterns = [
            # Standalone ARRIVALS/LANDING patterns (not followed by AND DEPG/DEPARTING)
            r'(?:ARRIVALS?|LANDING|LNDG|LDG|LAND)\s+(?!AND\s+(?:DEPG|DEP|DEPARTING))(?:EXPECT\s+)?(?:VISUAL\s+)?(?:APCH|APPROACH|APCHS|APPROACHES)?\s*(?:RWYS?|RYS|RY)?\s*[0-9]{1,2}[LCR]?(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?[0-9]{1,2}[LCR]?)*',
            # Standalone ARVNG patterns (not followed by AND DEPG)
            r'(?:ARVNG|ARRIVING)\s+(?!AND\s+(?:DEPG|DEP|DEPARTING))(?:RWYS?|RYS|RY)\s+[0-9]{1,2}[LCR]?(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?[0-9]{1,2}[LCR]?)*',
            # ILS/VISUAL/RNAV APPROACH patterns
            r'(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:OR\s+(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+)?(?:APCH|APPROACH|APCHS|APPROACHES)\s+(?:IN\s+USE\s+)?(?:RWYS?|RYS|RY)\s+[0-9]{1,2}[LCR]?(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?[0-9]{1,2}[LCR]?)*',
            # "APCH IN USE RY X" patterns
            r'(?:APCH|APPROACH|APCHS|APPROACHES)\s+IN\s+USE\s+(?:RWYS?|RYS|RY)\s+[0-9]{1,2}[LCR]?(?:(?:\s*,\s*|\s+(?:AND|OR)\s+)(?:(?:RWYS?|RYS|RY)\s+)?[0-9]{1,2}[LCR]?)*',
        ]

        for pattern in arrival_removal_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        return text

    def extract_arriving_runways(self, text: str) -> Set[str]:
        """Extract arrival runway information"""
        runways = set()

        # FIRST: Check for abbreviated format patterns that use lookahead for DEP keyword
        # These patterns need to run on the ORIGINAL text before departure sections are removed
        # Example: "ILS RWY 27, DEP 33L" - the lookahead (?=..DEP) needs DEP to be present
        abbreviated_pattern = re.compile(
            r'(?:ILS|RNAV|VOR|GPS|LOC)\s+(?:(?:RWYS?|RYS|RY)\s+)?([0-9]{1,2}[LCR]?)(?=\s*[,\.]\s*DEP)',
            re.IGNORECASE
        )
        for match in abbreviated_pattern.finditer(text):
            rwy = match.group(1)
            if rwy and re.match(r'^[0-9]{1,2}[LCR]?$', rwy):
                num_part = re.match(r'^([0-9]{1,2})', rwy)
                if num_part and 1 <= int(num_part.group(1)) <= 36:
                    runways.add(self.normalize_runway(rwy))

        # FIX: Remove departure-specific sections before extracting arrivals
        # to prevent "ARRIVALS RWY 3, RWY 8. DEPG RWY 8" from capturing 8 as arrival
        arrival_text = self._remove_departure_sections(text)

        for pattern in self.approach_patterns:
            matches = pattern.finditer(arrival_text)
            for match in matches:
                # Extract all runway numbers from the matched text
                matched_text = match.group(0)
                runway_matches = re.findall(r'\b([0-9]{1,2}[LCR]?)\b', matched_text)
                for rwy in runway_matches:
                    if re.match(r'^[0-9]{1,2}[LCR]?$', rwy):
                        # Validate runway number range (01-36)
                        num_part = re.match(r'^([0-9]{1,2})', rwy)
                        if num_part and 1 <= int(num_part.group(1)) <= 36:
                            runways.add(self.normalize_runway(rwy))

        return runways

    def extract_departing_runways(self, text: str) -> Set[str]:
        """Extract departure runway information"""
        runways = set()

        # FIX: Remove arrival-specific sections before extracting departures
        # to prevent "ARRIVALS RWY 3, RWY 8. DEPG RWY 8" contamination
        departure_text = self._remove_arrival_sections(text)

        for pattern in self.departure_patterns:
            matches = pattern.finditer(departure_text)
            for match in matches:
                # Extract all runway numbers from the matched text
                matched_text = match.group(0)
                runway_matches = re.findall(r'\b([0-9]{1,2}[LCR]?)\b', matched_text)
                for rwy in runway_matches:
                    if re.match(r'^[0-9]{1,2}[LCR]?$', rwy):
                        # Validate runway number range (01-36)
                        num_part = re.match(r'^([0-9]{1,2})', rwy)
                        if num_part and 1 <= int(num_part.group(1)) <= 36:
                            runways.add(self.normalize_runway(rwy))

        return runways
    
    def extract_combined_runways(self, text: str) -> Set[str]:
        """Extract runways when arrival/departure not specified"""
        runways = set()

        for pattern in self.combined_patterns:
            matches = pattern.finditer(text)
            for match in matches:
                # Extract all runway numbers from the matched text
                matched_text = match.group(0)
                runway_matches = re.findall(r'\b([0-9]{1,2}[LCR]?)\b', matched_text)
                for rwy in runway_matches:
                    if re.match(r'^[0-9]{1,2}[LCR]?$', rwy):
                        # Validate runway number range (01-36)
                        num_part = re.match(r'^([0-9]{1,2})', rwy)
                        if num_part and 1 <= int(num_part.group(1)) <= 36:
                            runways.add(self.normalize_runway(rwy))

        return runways
    
    def normalize_runway(self, runway: str) -> str:
        """Normalize runway format (preserve original format from ATIS)"""
        # Extract number and suffix - preserve single vs double digit as it appears in ATIS
        match = re.match(r'^([0-9]{1,2})([LCR])?$', runway)
        if match:
            number = match.group(1)  # Don't pad with zeros - preserve original format
            suffix = match.group(2) or ''
            return f"{number}{suffix}"
        return runway
    
    def determine_traffic_flow(self, arriving: Set[str], departing: Set[str]) -> TrafficFlow:
        """Determine overall traffic flow direction"""
        all_runways = arriving.union(departing)

        if not all_runways:
            return TrafficFlow.UNKNOWN

        # Get runway headings
        headings = []
        for runway in all_runways:
            # Extract numeric portion only (e.g., "9R" -> "9", "28L" -> "28", "10" -> "10")
            import re
            match = re.match(r'^(\d{1,2})', runway)
            if match:
                try:
                    heading = int(match.group(1)) * 10
                    headings.append(heading)
                except ValueError:
                    continue

        if not headings:
            return TrafficFlow.UNKNOWN
        
        # Calculate average heading
        avg_heading = sum(headings) / len(headings)
        
        # Determine flow direction based on heading
        if 337.5 <= avg_heading or avg_heading < 22.5:
            return TrafficFlow.NORTH
        elif 22.5 <= avg_heading < 67.5:
            return TrafficFlow.NORTHEAST
        elif 67.5 <= avg_heading < 112.5:
            return TrafficFlow.EAST
        elif 112.5 <= avg_heading < 157.5:
            return TrafficFlow.SOUTHEAST
        elif 157.5 <= avg_heading < 202.5:
            return TrafficFlow.SOUTH
        elif 202.5 <= avg_heading < 247.5:
            return TrafficFlow.SOUTHWEST
        elif 247.5 <= avg_heading < 292.5:
            return TrafficFlow.WEST
        elif 292.5 <= avg_heading < 337.5:
            return TrafficFlow.NORTHWEST
        
        return TrafficFlow.UNKNOWN
    
    def get_configuration_name(self, airport_code: str, arriving: Set[str], departing: Set[str]) -> Optional[str]:
        """Get airport-specific configuration name"""
        if airport_code not in self.airport_configs:
            return None
        
        configs = self.airport_configs[airport_code]
        all_runways = arriving.union(departing)
        
        for config_name, config_runways in configs.items():
            if any(rwy in config_runways for rwy in all_runways):
                return f"{config_name.capitalize()} Flow"
        
        return None
    
    def calculate_confidence(self, arriving: Set[str], departing: Set[str], text: str) -> float:
        """Calculate confidence score for extraction (recalibrated based on human review data)"""
        text_upper = text.upper()

        # If we found nothing, confidence is 0
        if not arriving and not departing:
            return 0.0

        # Start with conservative baseline
        confidence = 0.7

        # High confidence patterns (verified from human reviews as 100% accurate)
        high_conf_patterns = [
            r'ILS\s+(?:RWYS?|RYS|RY)\s+[0-9]{1,2}[LCR]?\s+(?:APCH|APPROACH)\s+IN\s+USE',  # Simple ILS
            r'VISUAL\s+(?:APCH|APPROACH)\s+(?:RWYS?|RYS|RY)\s+[0-9]{1,2}[LCR]?\s+IN\s+USE',  # Simple visual
            r'(?:SIMUL|SIMULTANEOUS)\s+VISUAL\s+APPROACHES\s+(?:RWYS?|RYS|RY)',  # Simultaneous visual
            r'PARL\s+ILS\s+(?:RWYS?|RYS|RY)',  # Parallel ILS
        ]

        for pattern in high_conf_patterns:
            if re.search(pattern, text_upper):
                return 1.0

        # Found both arrivals and departures with clear keywords
        if arriving and departing:
            arrival_keywords = ['LANDING', 'APPROACH', 'APCH', 'ARRIVALS', 'ARVNG', 'ILS', 'VISUAL', 'RNAV']
            departure_keywords = ['DEPG', 'DEP ', 'DEPARTURE', 'DEPARTING', 'TAKEOFF']

            has_arrival_kw = any(word in text_upper for word in arrival_keywords)
            has_departure_kw = any(word in text_upper for word in departure_keywords)

            if has_arrival_kw and has_departure_kw:
                # Both operations found with clear keywords = high confidence
                return 1.0
            else:
                # Both found but keywords unclear = good confidence
                confidence = 0.9

        # Found only arrivals or only departures (less confidence unless it's clearly one-sided)
        elif arriving or departing:
            arrival_keywords = ['LANDING', 'APPROACH', 'APCH', 'ARRIVALS', 'ARVNG', 'ILS', 'VISUAL', 'RNAV']
            departure_keywords = ['DEPG', 'DEP ', 'DEPARTURE', 'DEPARTING', 'TAKEOFF']

            has_arrival_kw = any(word in text_upper for word in arrival_keywords)
            has_departure_kw = any(word in text_upper for word in departure_keywords)

            # If we found arrivals with arrival keywords OR departures with departure keywords = good
            if (arriving and has_arrival_kw) or (departing and has_departure_kw):
                confidence = 0.8
            else:
                # Found runways but unclear context
                confidence = 0.6

        # Lower confidence for ambiguous patterns (known to fail frequently)
        ambiguous_patterns = [
            r'LANDING\s+AND\s+DEPARTING',  # Known failure pattern (sometimes misses runways)
            r'SIMUL.*(?:APCH|APPROACH).*TO\s+(?:RWYS?|RYS|RY)\s*,',  # Comma-list pattern (often incomplete)
        ]

        for pattern in ambiguous_patterns:
            if re.search(pattern, text_upper):
                confidence = min(confidence, 0.7)
                break

        return confidence

    def validate_configuration(self, config: RunwayConfiguration) -> List[str]:
        """
        Validate a runway configuration and return list of issues detected.
        Used for automated error reporting.

        Returns:
            List of issue types: ['low_confidence', 'missing_arrivals', 'missing_departures',
                                   'reciprocal_runways', 'too_many_runways']
            Empty list if no issues detected
        """
        issues = []
        text_upper = config.raw_text.upper()

        # Check if this is a split ATIS (KDEN, KCLE, etc.)
        is_split_atis = ('DEP INFO' in text_upper or 'ARR INFO' in text_upper)
        is_dep_only = 'DEP INFO' in text_upper
        is_arr_only = 'ARR INFO' in text_upper

        # 1. Low confidence threshold (< 0.9 is concerning)
        if config.confidence_score < 0.9:
            issues.append('low_confidence')

        # 2. Missing data (unless it's a valid split ATIS or arrival-only airport)
        if not is_split_atis:
            # Regular ATIS should have both arrivals and departures
            if not config.arriving_runways:
                issues.append('missing_arrivals')
            # Only flag missing departures if NOT an arrival-only airport
            if not config.departing_runways and config.airport_code not in self.arrival_only_airports:
                issues.append('missing_departures')
        else:
            # Split ATIS: DEP INFO should have departures, ARR INFO should have arrivals
            if is_dep_only and not config.departing_runways:
                issues.append('missing_departures')
            if is_arr_only and not config.arriving_runways:
                issues.append('missing_arrivals')

        # 3. Reciprocal runways (e.g., both 16L and 34L in same array - probable error)
        all_runways = set(config.arriving_runways + config.departing_runways)
        for runway in all_runways:
            # Extract runway number (e.g., "16L" -> 16)
            match = re.match(r'^(\d{1,2})', runway)
            if match:
                num = int(match.group(1))
                suffix = runway[len(match.group(1)):]  # Get L/C/R suffix

                # Calculate reciprocal (opposite end)
                reciprocal_num = (num + 18) % 36
                if reciprocal_num == 0:
                    reciprocal_num = 36

                # Check if reciprocal exists with same suffix
                reciprocal_runway = f"{reciprocal_num}{suffix}"
                if reciprocal_runway in all_runways:
                    issues.append('reciprocal_runways')
                    break  # Only report once

        # 4. Unusual runway count (>6 runways total seems suspicious)
        if len(all_runways) > 6:
            issues.append('too_many_runways')

        return issues

# Example usage
if __name__ == "__main__":
    parser = RunwayParser()
    
    # Test with sample ATIS text
    sample_atis = """
    SEA ATIS INFO C 0053Z. 11010KT 10SM FEW015 BKN250 11/07 A3012 
    RMK AO2 SLP202 T01110072. ILS APPROACHES IN USE. LANDING RUNWAY 16L 16C AND 16R. 
    DEPARTING RUNWAY 16L 16C AND 16R. NOTAMS: RUNWAY 16L CLSD BTN 0600 AND 1400Z DAILY.
    """
    
    result = parser.parse("KSEA", sample_atis, "C")
    print(f"Airport: {result.airport_code}")
    print(f"Arriving: {result.arriving_runways}")
    print(f"Departing: {result.departing_runways}")
    print(f"Traffic Flow: {result.traffic_flow}")
    print(f"Confidence: {result.confidence_score:.2f}")