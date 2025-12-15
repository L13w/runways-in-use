#!/usr/bin/env python3
"""
ATIS Data Collector
Fetches D-ATIS data from clowd.io API and stores in database
Run every 5 minutes via cron or scheduler
"""

import requests
import json
import hashlib
import psycopg2
from psycopg2.extras import Json
from datetime import datetime
import logging
import os
from typing import Dict, List, Optional

# Import runway parser
from runway_parser import RunwayParser, RunwayConfiguration

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
DATIS_API_URL = "https://datis.clowd.io/api/all"
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'database': os.getenv('DB_NAME', 'runway-detection'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD', 'postgres'),
    'port': os.getenv('DB_PORT', '5432'),
    'sslmode': os.getenv('DB_SSLMODE', 'prefer')  # 'require' for production, 'prefer' for local
}

class ATISCollector:
    def __init__(self):
        self.conn = None
        self.parser = RunwayParser()
        self.connect_db()
        
    def connect_db(self):
        """Establish database connection"""
        try:
            self.conn = psycopg2.connect(**DB_CONFIG)
            logger.info("Database connection established")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise
    
    def fetch_atis_data(self) -> Optional[List[Dict]]:
        """Fetch current ATIS data from API"""
        try:
            response = requests.get(DATIS_API_URL, timeout=30)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Fetched ATIS data for {len(data)} airports")
            return data
        except requests.RequestException as e:
            logger.error(f"Failed to fetch ATIS data: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse ATIS JSON: {e}")
            return None
    
    def calculate_hash(self, text: str) -> str:
        """Calculate MD5 hash of ATIS text for change detection"""
        return hashlib.md5(text.encode('utf-8')).hexdigest()
    
    def store_atis_snapshot(self, airports_data: List[Dict]):
        """Store ATIS data in database"""
        cursor = self.conn.cursor()
        collected_at = datetime.utcnow()

        new_records = 0
        changed_records = 0
        unchanged_records = 0

        # Track split ATIS pairs for merging after individual processing
        # Key: airport_code, Value: {'arr': {...}, 'dep': {...}}
        split_atis_pending = {}

        for airport in airports_data:
            try:
                airport_code = airport.get('airport')
                datis_text = airport.get('datis', '')
                
                if not airport_code or not datis_text:
                    continue
                
                # Extract information letter (usually first letter after airport code)
                info_letter = self.extract_info_letter(datis_text)
                content_hash = self.calculate_hash(datis_text)
                
                # Check if this is a new/changed ATIS
                cursor.execute("""
                    SELECT content_hash 
                    FROM atis_data 
                    WHERE airport_code = %s 
                    ORDER BY collected_at DESC 
                    LIMIT 1
                """, (airport_code,))
                
                last_hash = cursor.fetchone()
                
                if not last_hash:
                    # First record for this airport
                    new_records += 1
                    is_changed = True
                elif last_hash[0] != content_hash:
                    # ATIS has changed
                    changed_records += 1
                    is_changed = True
                else:
                    # No change
                    unchanged_records += 1
                    is_changed = False
                
                # Store the snapshot (always store for historical record)
                cursor.execute("""
                    INSERT INTO atis_data
                    (airport_code, collected_at, information_letter, datis_text, content_hash, is_changed)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (airport_code, collected_at, info_letter, datis_text, content_hash, is_changed))

                atis_id = cursor.fetchone()[0]

                # Check if this is a split ATIS (ARR INFO or DEP INFO)
                # We need to track these even if unchanged, for potential merging with changed partner
                text_upper = datis_text.upper()
                is_arr_info = 'ARR INFO' in text_upper or 'ARR ATIS' in text_upper
                is_dep_info = 'DEP INFO' in text_upper or 'DEP ATIS' in text_upper
                is_split_atis = is_arr_info or is_dep_info

                # Parse and store runway configuration if ATIS changed OR if it's a split ATIS
                # (we need to parse unchanged split ATIS to have both halves for merging)
                if is_changed or is_split_atis:
                    try:
                        config = self.parser.parse(airport_code, datis_text, info_letter)

                        if is_split_atis:
                            # This is a split ATIS - collect for potential merging
                            if airport_code not in split_atis_pending:
                                split_atis_pending[airport_code] = {}

                            atis_type = 'arr' if is_arr_info else 'dep'
                            split_atis_pending[airport_code][atis_type] = {
                                'atis_id': atis_id,
                                'config': config,
                                'info_letter': info_letter,
                                'is_changed': is_changed
                            }
                            logger.debug(f"Collected {atis_type.upper()} INFO for {airport_code} (changed={is_changed})")

                        # Only store individual config if ATIS changed
                        if is_changed:
                            cursor.execute("""
                                INSERT INTO runway_configs
                                (airport_code, atis_id, arriving_runways, departing_runways,
                                 traffic_flow, configuration_name, confidence_score)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (airport_code, atis_id) DO NOTHING
                            """, (
                                airport_code,
                                atis_id,
                                json.dumps(config.arriving_runways),
                                json.dumps(config.departing_runways),
                                config.traffic_flow,
                                config.configuration_name,
                                config.confidence_score
                            ))

                            # Validate configuration and create error report if issues found
                            # BUT skip for split ATIS - these will be validated after merging
                            if not is_split_atis:
                                issues = self.parser.validate_configuration(config)
                                if issues:
                                    self.create_error_report(cursor, airport_code, atis_id, config, issues)

                    except Exception as parse_error:
                        logger.debug(f"Failed to parse runway config for {airport_code}: {parse_error}")

            except Exception as e:
                logger.error(f"Error storing ATIS for {airport_code}: {e}")
                continue

        # Process split ATIS pairs - merge ARR and DEP into combined configs
        merged_count = self._merge_split_atis_pairs(cursor, split_atis_pending)

        self.conn.commit()
        if merged_count > 0:
            logger.info(f"Stored ATIS data: {new_records} new, {changed_records} changed, {unchanged_records} unchanged, {merged_count} merged pairs")
        else:
            logger.info(f"Stored ATIS data: {new_records} new, {changed_records} changed, {unchanged_records} unchanged")

    def _merge_split_atis_pairs(self, cursor, split_atis_pending: Dict) -> int:
        """
        Merge ARR INFO and DEP INFO broadcasts into a single runway_configs row.

        For airports that publish separate arrival and departure ATIS broadcasts,
        this combines them into a single configuration with:
        - Arrivals from ARR INFO
        - Departures from DEP INFO
        - merged_from_pair = TRUE
        - component_confidence tracking individual confidence scores

        Args:
            cursor: Database cursor
            split_atis_pending: Dict of {airport_code: {'arr': {...}, 'dep': {...}}}

        Returns:
            Number of successfully merged pairs
        """
        merged_count = 0

        for airport_code, atis_pair in split_atis_pending.items():
            try:
                # Only merge if we have BOTH arr and dep for this airport
                if 'arr' not in atis_pair or 'dep' not in atis_pair:
                    logger.debug(f"Incomplete split ATIS for {airport_code}: only have {list(atis_pair.keys())}")
                    continue

                arr_data = atis_pair['arr']
                dep_data = atis_pair['dep']

                # Only merge if at least one of the pair changed
                if not arr_data.get('is_changed') and not dep_data.get('is_changed'):
                    continue

                arr_config = arr_data['config']
                dep_config = dep_data['config']

                # Merge: take arrivals from ARR INFO, departures from DEP INFO
                merged_arriving = list(arr_config.arriving_runways)
                merged_departing = list(dep_config.departing_runways)

                # If ARR INFO also had departures or DEP INFO had arrivals, include them too
                # (some airports include partial info in both)
                for rwy in arr_config.departing_runways:
                    if rwy not in merged_departing:
                        merged_departing.append(rwy)
                for rwy in dep_config.arriving_runways:
                    if rwy not in merged_arriving:
                        merged_arriving.append(rwy)

                # Calculate merged confidence (average of both, boosted if both are high)
                arr_conf = arr_config.confidence_score
                dep_conf = dep_config.confidence_score
                if arr_conf >= 0.9 and dep_conf >= 0.9:
                    merged_confidence = 1.0  # Both high = full confidence
                else:
                    merged_confidence = (arr_conf + dep_conf) / 2

                # Determine traffic flow from merged runways
                merged_flow_enum = self.parser.determine_traffic_flow(set(merged_arriving), set(merged_departing))
                merged_flow = merged_flow_enum.value if hasattr(merged_flow_enum, 'value') else str(merged_flow_enum)

                # Create configuration name
                config_name = f"Merged: ARR {arr_data['info_letter'] or '?'} + DEP {dep_data['info_letter'] or '?'}"

                # Component confidence for tracking
                component_confidence = {
                    'arrivals': arr_conf,
                    'departures': dep_conf,
                    'arr_atis_id': arr_data['atis_id'],
                    'dep_atis_id': dep_data['atis_id']
                }

                # Insert merged configuration
                # Use the ARR INFO atis_id as the primary reference
                cursor.execute("""
                    INSERT INTO runway_configs
                    (airport_code, atis_id, arriving_runways, departing_runways,
                     traffic_flow, configuration_name, confidence_score,
                     merged_from_pair, component_confidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                    ON CONFLICT (airport_code, atis_id) DO UPDATE SET
                        arriving_runways = EXCLUDED.arriving_runways,
                        departing_runways = EXCLUDED.departing_runways,
                        traffic_flow = EXCLUDED.traffic_flow,
                        configuration_name = EXCLUDED.configuration_name,
                        confidence_score = EXCLUDED.confidence_score,
                        merged_from_pair = TRUE,
                        component_confidence = EXCLUDED.component_confidence
                """, (
                    airport_code,
                    arr_data['atis_id'],  # Use ARR ATIS as primary
                    json.dumps(merged_arriving),
                    json.dumps(merged_departing),
                    merged_flow,
                    config_name,
                    merged_confidence,
                    json.dumps(component_confidence)
                ))

                merged_count += 1
                logger.info(f"Merged split ATIS for {airport_code}: ARR={merged_arriving}, DEP={merged_departing} (conf={merged_confidence:.1%})")

                # Validate the merged configuration and create error report if issues found
                # Combine raw text from both ATIS for carry-forward pattern matching
                combined_raw_text = f"{arr_config.raw_text}\n---\n{dep_config.raw_text}"
                merged_config = RunwayConfiguration(
                    airport_code=airport_code,
                    timestamp=datetime.utcnow(),
                    information_letter=arr_data['info_letter'],
                    arriving_runways=merged_arriving,
                    departing_runways=merged_departing,
                    traffic_flow=merged_flow,
                    configuration_name=config_name,
                    raw_text=combined_raw_text,  # Include both ATIS for carry-forward matching
                    confidence_score=merged_confidence
                )
                issues = self.parser.validate_configuration(merged_config)
                if issues:
                    self.create_error_report(cursor, airport_code, arr_data['atis_id'], merged_config, issues)

            except Exception as e:
                logger.warning(f"Failed to merge split ATIS for {airport_code}: {e}")
                continue

        return merged_count

    def try_carry_forward_review(self, cursor, airport_code: str,
                                  arriving_runways: List[str], departing_runways: List[str],
                                  atis_text: str = None) -> Optional[Dict]:
        """
        Check if there's a recent reviewed error report for this airport with the same
        parsed runways, OR a stored parsing correction pattern that matches.

        This allows human reviews to automatically apply to future reports with identical
        parse results, reducing the review burden when the same parse pattern repeats.

        Returns:
            Dict with 'source_id', 'corrected_arriving', 'corrected_departing' if match found
            None if no matching reviewed report exists
        """
        try:
            import re

            # Sort runway lists for consistent comparison
            arriving_sorted = sorted(arriving_runways) if arriving_runways else []
            departing_sorted = sorted(departing_runways) if departing_runways else []

            # METHOD 1: Look for reviewed reports from the last 24 hours with matching parse results
            cursor.execute("""
                SELECT id,
                       corrected_arriving_runways,
                       corrected_departing_runways,
                       parsed_arriving_runways,
                       parsed_departing_runways
                FROM error_reports
                WHERE airport_code = %s
                  AND reviewed = TRUE
                  AND reviewed_at > NOW() - INTERVAL '24 hours'
                  AND corrected_arriving_runways IS NOT NULL
                ORDER BY reviewed_at DESC
                LIMIT 10
            """, (airport_code,))

            for row in cursor.fetchall():
                report_id, corr_arr, corr_dep, parsed_arr, parsed_dep = row

                # Parse the JSONB fields (they come back as Python lists from psycopg2)
                parsed_arr_list = parsed_arr if isinstance(parsed_arr, list) else json.loads(parsed_arr or '[]')
                parsed_dep_list = parsed_dep if isinstance(parsed_dep, list) else json.loads(parsed_dep or '[]')

                # Sort for comparison
                parsed_arr_sorted = sorted(parsed_arr_list) if parsed_arr_list else []
                parsed_dep_sorted = sorted(parsed_dep_list) if parsed_dep_list else []

                # Check if parse results match
                if parsed_arr_sorted == arriving_sorted and parsed_dep_sorted == departing_sorted:
                    # Found a match! Return the correction
                    corr_arr_list = corr_arr if isinstance(corr_arr, list) else json.loads(corr_arr or '[]')
                    corr_dep_list = corr_dep if isinstance(corr_dep, list) else json.loads(corr_dep or '[]')

                    logger.debug(f"Found carry-forward match for {airport_code}: report #{report_id}")
                    return {
                        'source_id': report_id,
                        'corrected_arriving': corr_arr_list,
                        'corrected_departing': corr_dep_list
                    }

            # METHOD 2: Check parsing_corrections table for pattern-based corrections
            if atis_text:
                # Generate pattern key from this ATIS text (same logic as API)
                pattern_phrases = []

                # Extract approach type mentions
                approach_matches = re.findall(r'(?:ILS|VISUAL|RNAV|VOR|GPS|LOC)\s+(?:APCH|APPROACH|RWY|RY)', atis_text, re.IGNORECASE)
                pattern_phrases.extend(approach_matches[:3])

                # Extract runway action phrases
                action_matches = re.findall(r'(?:LANDING|DEPARTING|DEPG|LNDG|ARRIVALS?|DEPARTURES?)\s+(?:AND\s+)?(?:RWYS?|RYS?|RY)?', atis_text, re.IGNORECASE)
                pattern_phrases.extend(action_matches[:3])

                # Create pattern key
                pattern_key = f"{airport_code}:{' | '.join(sorted(set(p.upper() for p in pattern_phrases)))}"

                # Look for matching pattern in parsing_corrections
                cursor.execute("""
                    SELECT id, expected_arriving, expected_departing, times_applied
                    FROM parsing_corrections
                    WHERE airport_code = %s
                      AND atis_pattern = %s
                      AND success_rate >= 0.8
                """, (airport_code, pattern_key))

                correction = cursor.fetchone()
                if correction:
                    corr_id, exp_arr, exp_dep, times_applied = correction

                    # Parse JSONB fields
                    exp_arr_list = exp_arr if isinstance(exp_arr, list) else json.loads(exp_arr or '[]')
                    exp_dep_list = exp_dep if isinstance(exp_dep, list) else json.loads(exp_dep or '[]')

                    # Update times_applied counter
                    cursor.execute("""
                        UPDATE parsing_corrections
                        SET times_applied = times_applied + 1
                        WHERE id = %s
                    """, (corr_id,))

                    logger.info(f"Applied parsing correction #{corr_id} for {airport_code} (pattern match)")
                    return {
                        'source_id': f"correction_{corr_id}",
                        'corrected_arriving': exp_arr_list,
                        'corrected_departing': exp_dep_list
                    }

            return None

        except Exception as e:
            logger.warning(f"Error checking carry-forward for {airport_code}: {e}")
            return None

    def create_error_report(self, cursor, airport_code: str, atis_id: int, config, issues: List[str]):
        """
        Create an automated error report for a configuration with issues.
        Uses ON CONFLICT to avoid duplicate reports for the same ATIS.
        Implements carry-forward: if a recent reviewed report has similar parse results,
        auto-apply that correction to the new report.
        """
        try:
            # Format issues as comma-separated string for notes
            issue_description = ', '.join(issues)
            notes = f"Computer-detected issues: {issue_description}"

            # Check if this is a split ATIS pair (for KDEN, KCLE, etc.)
            # Look for a matching paired ATIS within 30 minutes
            paired_atis_id = None
            text_upper = config.raw_text.upper()
            if 'DEP INFO' in text_upper or 'ARR INFO' in text_upper:
                is_dep = 'DEP INFO' in text_upper
                search_keyword = 'ARR INFO' if is_dep else 'DEP INFO'

                cursor.execute("""
                    SELECT id FROM atis_data
                    WHERE airport_code = %s
                      AND UPPER(datis_text) LIKE %s
                      AND collected_at BETWEEN
                          (SELECT collected_at FROM atis_data WHERE id = %s) - INTERVAL '30 minutes'
                          AND
                          (SELECT collected_at FROM atis_data WHERE id = %s) + INTERVAL '30 minutes'
                      AND id != %s
                    ORDER BY ABS(EXTRACT(EPOCH FROM (collected_at - (SELECT collected_at FROM atis_data WHERE id = %s))))
                    LIMIT 1
                """, (airport_code, f'%{search_keyword}%', atis_id, atis_id, atis_id, atis_id))

                pair = cursor.fetchone()
                if pair:
                    paired_atis_id = pair[0]

            # CARRY-FORWARD: Check for recent reviewed reports with similar parse results
            # OR matching patterns from parsing_corrections table
            # If found, auto-apply the correction to this new report
            carry_forward_applied = self.try_carry_forward_review(
                cursor, airport_code, config.arriving_runways, config.departing_runways,
                atis_text=config.raw_text
            )

            if carry_forward_applied:
                # Insert as already-reviewed with carried-forward correction
                cursor.execute("""
                    INSERT INTO error_reports
                    (airport_code, current_atis_id, paired_atis_id, parsed_arriving_runways,
                     parsed_departing_runways, confidence_score, reported_by, reviewer_notes,
                     reviewed, reviewed_at, corrected_arriving_runways, corrected_departing_runways)
                    VALUES (%s, %s, %s, %s, %s, %s, 'computer', %s, TRUE, NOW(), %s, %s)
                    ON CONFLICT (airport_code, current_atis_id) DO NOTHING
                """, (
                    airport_code,
                    atis_id,
                    paired_atis_id,
                    json.dumps(config.arriving_runways),
                    json.dumps(config.departing_runways),
                    config.confidence_score,
                    notes + f" | Auto-corrected via carry-forward from previous review",
                    json.dumps(carry_forward_applied['corrected_arriving']),
                    json.dumps(carry_forward_applied['corrected_departing'])
                ))

                if cursor.rowcount > 0:
                    logger.info(f"Created carry-forward reviewed report for {airport_code}: applied correction from report #{carry_forward_applied['source_id']}")
            else:
                # Insert error report (ON CONFLICT will skip if duplicate exists for same airport/ATIS)
                cursor.execute("""
                    INSERT INTO error_reports
                    (airport_code, current_atis_id, paired_atis_id, parsed_arriving_runways,
                     parsed_departing_runways, confidence_score, reported_by, reviewer_notes)
                    VALUES (%s, %s, %s, %s, %s, %s, 'computer', %s)
                    ON CONFLICT (airport_code, current_atis_id) DO NOTHING
                """, (
                    airport_code,
                    atis_id,
                    paired_atis_id,
                    json.dumps(config.arriving_runways),
                    json.dumps(config.departing_runways),
                    config.confidence_score,
                    notes
                ))

                if cursor.rowcount > 0:
                    logger.info(f"Created error report for {airport_code}: {issue_description}")

        except Exception as e:
            logger.warning(f"Failed to create error report for {airport_code}: {e}")

    def extract_info_letter(self, datis_text: str) -> Optional[str]:
        """Extract ATIS information letter from text"""
        import re

        # Common patterns for info letter
        patterns = [
            r'ATIS\s+(?:INFO|INFORMATION)\s+([A-Z])',
            r'INFORMATION\s+([A-Z])\s',
            r'ATIS\s+([A-Z])\s+\d{4}',
            r'^[A-Z]{3,4}\s+ATIS\s+([A-Z])\s',
        ]

        text_upper = datis_text.upper()
        for pattern in patterns:
            match = re.search(pattern, text_upper)
            if match:
                return match.group(1)

        return None
    
    def cleanup_old_data(self, days_to_keep: int = 90):
        """Remove old ATIS data to manage storage"""
        cursor = self.conn.cursor()
        cursor.execute("""
            DELETE FROM atis_data 
            WHERE collected_at < NOW() - INTERVAL '%s days'
        """, (days_to_keep,))
        deleted = cursor.rowcount
        self.conn.commit()
        
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old ATIS records")
    
    def cleanup_old_computer_reports(self, hours_to_keep: int = 2):
        """Remove old computer-generated error reports to keep queue fresh.

        IMPORTANT: Never delete reports that:
        - Have been reviewed (reviewed = TRUE) - these are training data
        - Have user-provided corrections (corrected_* is not null) - these are valuable
        - Were reported by users (reported_by != 'computer') - these need human attention
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            DELETE FROM error_reports
            WHERE reported_by = 'computer'
              AND reviewed = FALSE
              AND corrected_arriving_runways IS NULL
              AND corrected_departing_runways IS NULL
              AND reported_at < NOW() - INTERVAL '%s hours'
        """, (hours_to_keep,))
        deleted = cursor.rowcount
        self.conn.commit()

        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old computer-generated error reports (>{hours_to_keep}h)")

    def run(self):
        """Main execution method"""
        try:
            # Fetch current ATIS data
            airports_data = self.fetch_atis_data()

            if airports_data:
                # Store in database
                self.store_atis_snapshot(airports_data)

                # Cleanup old computer-generated error reports (every run)
                # This keeps the review queue fresh and focused on recent data
                self.cleanup_old_computer_reports(hours_to_keep=2)

                # Cleanup old ATIS data (run occasionally)
                from random import random
                if random() < 0.01:  # 1% chance each run
                    self.cleanup_old_data()

        except Exception as e:
            logger.error(f"Collector run failed: {e}")
            raise
        finally:
            if self.conn:
                self.conn.close()

def main():
    """Entry point for script"""
    collector = ATISCollector()
    collector.run()

if __name__ == "__main__":
    main()
