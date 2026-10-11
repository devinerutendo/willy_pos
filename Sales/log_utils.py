"""
Utilities for monitoring and analyzing Sales signal logs.

Provides functions to:
- Query logs for specific sales operations
- Summarize inventory changes
- Identify failures and issues
- Generate audit reports
"""

import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any


logger = logging.getLogger(__name__)


class SalesLogAnalyzer:
    """
    Analyzer for Sales signal operation logs.
    Helps diagnose inventory synchronization issues.
    """
    
    def __init__(self, log_file_path: str = None):
        """
        Initialize analyzer.
        
        Args:
            log_file_path: Path to signals log file. If None, uses default.
        """
        if log_file_path is None:
            # Default: use signals.log in logs directory
            backend_dir = Path(__file__).parent.parent
            log_file_path = backend_dir / "logs" / "signals.log"
        
        self.log_file = Path(log_file_path)
        self.entries = []
    
    def load_logs(self, hours_back: int = 24) -> bool:
        """
        Load log entries from the past N hours.
        
        Args:
            hours_back: Number of hours to look back (default 24)
            
        Returns:
            True if loaded successfully, False otherwise
        """
        if not self.log_file.exists():
            logger.warning(f"Log file not found: {self.log_file}")
            return False
        
        cutoff_time = datetime.now() - timedelta(hours=hours_back)
        self.entries = []
        
        try:
            with open(self.log_file, 'r') as f:
                for line in f:
                    if not line.strip():
                        continue
                    
                    # Parse timestamp from log line
                    # Format: LEVEL YYYY-MM-DD HH:MM:SS module funcName lineno | message
                    try:
                        parts = line.split(' | ', 1)
                        if len(parts) == 2:
                            meta, message = parts
                            self.entries.append({
                                'raw': line.strip(),
                                'meta': meta,
                                'message': message.strip()
                            })
                    except Exception:
                        # Skip malformed lines
                        continue
            
            return True
        except Exception as e:
            logger.error(f"Error loading logs: {e}")
            return False
    
    def get_sales_summary(self) -> Dict[str, Any]:
        """
        Get summary of all sales operations from current logs.
        
        Returns:
            Dictionary with operation counts and status breakdown
        """
        summary = {
            'total_operations': 0,
            'reserve_operations': 0,
            'complete_operations': 0,
            'cancel_operations': 0,
            'errors': 0,
            'warnings': 0,
            'error_messages': []
        }
        
        for entry in self.entries:
            meta = entry['meta']
            message = entry['message']
            
            if 'ERROR' in meta:
                summary['errors'] += 1
                summary['error_messages'].append(message[:200])  # First 200 chars
            
            if 'WARNING' in meta:
                summary['warnings'] += 1
            
            if 'reservation' in message.lower() and 'INFO' in meta:
                summary['reserve_operations'] += 1
                summary['total_operations'] += 1
            
            if 'completion' in message.lower() and 'INFO' in meta:
                summary['complete_operations'] += 1
                summary['total_operations'] += 1
            
            if 'release' in message.lower() and 'INFO' in meta:
                summary['cancel_operations'] += 1
                summary['total_operations'] += 1
        
        return summary
    
    def get_sale_details(self, sale_ref: str) -> Dict[str, Any]:
        """
        Get detailed information about a specific sale operation.
        
        Args:
            sale_ref: Sale reference number (e.g., 'SALE-20260325120000-AB12')
            
        Returns:
            Dictionary with operation timeline and status
        """
        details = {
            'sale_ref': sale_ref,
            'operations': [],
            'errors': [],
            'status': 'NOT_FOUND',
            'timestamps': []
        }
        
        for entry in self.entries:
            if sale_ref in entry['message']:
                meta = entry['meta']
                message = entry['message']
                
                # Extract timestamp and level
                parts = meta.split()
                if len(parts) >= 2:
                    timestamp = f"{parts[1]} {parts[2]}"  # YYYY-MM-DD HH:MM:SS
                    level = parts[0]
                    
                    operation = {
                        'timestamp': timestamp,
                        'level': level,
                        'message': message
                    }
                    
                    if 'ERROR' in level:
                        details['errors'].append(operation)
                    else:
                        details['operations'].append(operation)
                    
                    details['timestamps'].append(timestamp)
        
        if details['operations'] or details['errors']:
            details['status'] = 'FOUND'
            if details['errors']:
                details['status'] = 'FAILED'
        
        return details
    
    def print_summary(self):
        """Print a formatted summary of all operations to console."""
        summary = self.get_sales_summary()
        
        print("\n" + "="*60)
        print("SALES SIGNAL OPERATIONS SUMMARY")
        print("="*60)
        print(f"Total Operations: {summary['total_operations']}")
        print(f"  - Reserve:   {summary['reserve_operations']}")
        print(f"  - Complete:  {summary['complete_operations']}")
        print(f"  - Cancel:    {summary['cancel_operations']}")
        print(f"\nWarnings:       {summary['warnings']}")
        print(f"Errors:         {summary['errors']}")
        
        if summary['error_messages']:
            print("\nLatest Errors:")
            for i, msg in enumerate(summary['error_messages'][-5:], 1):
                print(f"  {i}. {msg}")
        
        print("="*60 + "\n")
    
    def print_sale_timeline(self, sale_ref: str):
        """Print detailed timeline for a specific sale."""
        details = self.get_sale_details(sale_ref)
        
        print("\n" + "="*60)
        print(f"SALE TIMELINE: {sale_ref}")
        print(f"Status: {details['status']}")
        print("="*60)
        
        all_ops = sorted(
            details['operations'] + details['errors'],
            key=lambda x: x['timestamp']
        )
        
        for op in all_ops:
            status_icon = "✗" if op['level'] == 'ERROR' else "✓"
            print(f"{status_icon} [{op['timestamp']}] {op['level']}")
            print(f"   {op['message'][:100]}")
        
        if details['errors']:
            print(f"\n⚠ {len(details['errors'])} error(s) found")
        
        print("="*60 + "\n")


def check_sale_inventory_sync(sale_ref: str) -> Dict[str, Any]:
    """
    Quick check function to verify if a sale's inventory was properly synchronized.
    
    Args:
        sale_ref: Sale reference number
        
    Returns:
        Dictionary with sync status and details
    """
    analyzer = SalesLogAnalyzer()
    analyzer.load_logs(hours_back=24)
    
    details = analyzer.get_sale_details(sale_ref)
    
    result = {
        'sale_ref': sale_ref,
        'found_in_logs': details['status'] != 'NOT_FOUND',
        'has_errors': len(details['errors']) > 0,
        'completion_confirmed': False,
        'error_details': None
    }
    
    # Check if completion log exists
    for op in details['operations']:
        if 'Successfully completed inventory' in op['message']:
            result['completion_confirmed'] = True
    
    if result['has_errors']:
        result['error_details'] = [e['message'] for e in details['errors']]
    
    return result
