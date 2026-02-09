import sys
from pynetdicom import AE
from pynetdicom.apps.common import ElementPath
from pydicom.dataset import Dataset
import os.path
import csv
import time
import datetime
import pytz
import signal
import tqdm
import concurrent.futures
import numpy as np
import inquirer
import ast
import threading
import logging
import logging.handlers
from collections import defaultdict

# Module logger
logger = logging.getLogger('pydicom_batch')

def setup_logging(output_dir, log_level=logging.INFO):
    """Setup logging with both file and console handlers.
    
    Args:
        output_dir: Directory where log files will be stored
        log_level: Logging level (default: INFO)
    
    Returns:
        Logger instance
    """
    # Create logs directory if it doesn't exist
    log_dir = os.path.join(output_dir, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    
    # Create logger
    logger = logging.getLogger('pydicom_batch')
    logger.setLevel(log_level)
    
    # Clear any existing handlers
    logger.handlers.clear()
    
    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(funcName)-25s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S'
    )
    
    # File handler with rotation (10MB max, keep 5 backup files)
    timestamp = datetime.datetime.now().strftime('%Y%m%d')
    log_file = os.path.join(log_dir, f'pydicom_batch_{timestamp}.log')
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)  # File gets all details
    file_handler.setFormatter(detailed_formatter)
    
    # Console handler (less verbose)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    
    # Add handlers to logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    logger.info(f"Logging initialized. Log file: {log_file}")
    
    return logger

from pynetdicom import (
    AE, QueryRetrievePresentationContexts
)

from pynetdicom.sop_class import (
    PatientRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelFind,
    PatientStudyOnlyQueryRetrieveInformationModelFind,
    PatientRootQueryRetrieveInformationModelMove,
    StudyRootQueryRetrieveInformationModelMove,
    PatientStudyOnlyQueryRetrieveInformationModelMove,
    Verification
)

continue_extraction = True

# Global statistics tracker
class BatchStatistics:
    """Track statistics for batch processing."""
    
    def __init__(self):
        self.reset()
        self._lock = threading.Lock()
    
    def reset(self):
        self.start_time = None
        self.end_time = None
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.retried_requests = 0
        self.total_retries = 0
        self.request_times = []  # List of (request_id, duration_seconds)
    
    def start(self, total_requests):
        self.reset()
        self.start_time = time.time()
        self.total_requests = total_requests
    
    def record_success(self, duration=None):
        with self._lock:
            self.successful_requests += 1
            if duration:
                self.request_times.append(duration)
    
    def record_failure(self):
        with self._lock:
            self.failed_requests += 1
    
    def record_retry(self):
        with self._lock:
            self.retried_requests += 1
            self.total_retries += 1
    
    def finish(self):
        self.end_time = time.time()
    
    def get_summary(self):
        elapsed = (self.end_time or time.time()) - (self.start_time or time.time())
        avg_time = sum(self.request_times) / len(self.request_times) if self.request_times else 0
        success_rate = (self.successful_requests / self.total_requests * 100) if self.total_requests > 0 else 0
        requests_per_min = (self.successful_requests + self.failed_requests) / (elapsed / 60) if elapsed > 0 else 0
        
        return {
            'total_requests': self.total_requests,
            'successful': self.successful_requests,
            'failed': self.failed_requests,
            'success_rate': success_rate,
            'retried_requests': self.retried_requests,
            'total_retries': self.total_retries,
            'elapsed_seconds': elapsed,
            'avg_request_time': avg_time,
            'requests_per_minute': requests_per_min
        }
    
    def print_summary(self):
        stats = self.get_summary()
        
        summary_lines = [
            "",
            "=" * 60,
            "BATCH PROCESSING STATISTICS",
            "=" * 60,
            f"Total Requests:      {stats['total_requests']}",
            f"Successful:          {stats['successful']} ({stats['success_rate']:.1f}%)",
            f"Failed:              {stats['failed']}",
            f"Retried Requests:    {stats['retried_requests']} ({stats['total_retries']} total retries)",
            "-" * 60,
            f"Total Time:          {stats['elapsed_seconds']:.1f} seconds ({stats['elapsed_seconds']/60:.1f} min)",
            f"Avg Request Time:    {stats['avg_request_time']:.2f} seconds",
            f"Throughput:          {stats['requests_per_minute']:.1f} requests/minute",
            "=" * 60,
        ]
        
        for line in summary_lines:
            if line.strip():
                logger.info(line)
        
        print("\n".join(summary_lines))
        
        return stats


# Global stats instance
batch_stats = BatchStatistics()


def retry_with_backoff(max_retries=3, base_delay=1.0, max_delay=30.0):
    """Decorator for retrying functions with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(f"Attempt {attempt + 1}/{max_retries + 1} failed: {str(e)}. Retrying in {delay:.1f}s...")
                        batch_stats.record_retry()
                        time.sleep(delay)
                    else:
                        logger.error(f"All {max_retries + 1} attempts failed: {str(e)}")
            raise last_exception
        return wrapper
    return decorator


def sigint_handler(signal, frame):
    global continue_extraction
    continue_extraction = False
    raise KeyboardInterrupt

def watch_sigint():
    signal.signal(signal.SIGINT, sigint_handler)


def preflight_check(config):
    """Perform pre-flight checks before starting batch processing.
    
    Checks:
    1. C-ECHO to verify PACS connectivity
    2. CSV file exists and has required columns (for C-MOVE)
    
    Returns:
        tuple: (success: bool, message: str)
    """
    logger.info("Running pre-flight checks...")
    
    # Check 1: C-ECHO to PACS
    logger.info(f"Testing PACS connectivity: {config['pacs']['hostname']}:{config['pacs']['port']}")
    
    ae = AE(ae_title=config['local']['aet'])
    ae.add_requested_context(Verification)
    ae.acse_timeout = 30
    ae.dimse_timeout = 30
    ae.network_timeout = 30
    
    try:
        assoc = ae.associate(
            config['pacs']['hostname'],
            config['pacs']['port'],
            ae_title=config['pacs']['aet']
        )
        
        if assoc.is_established:
            status = assoc.send_c_echo()
            assoc.release()
            
            if status and status.Status == 0x0000:
                logger.info("C-ECHO successful - PACS is reachable")
            else:
                logger.error(f"C-ECHO failed with status: 0x{status.Status:04X}" if status else "C-ECHO failed: No response")
                return False, "C-ECHO failed - PACS may be unreachable or rejecting requests"
        else:
            reject_info = ""
            if assoc.rejected_permanent:
                reject_info = f" (Permanent rejection: {assoc.rejection_reason})"
            elif assoc.rejected_transient:
                reject_info = f" (Transient rejection: {assoc.rejection_reason})"
            logger.error(f"Could not establish association with PACS{reject_info}")
            return False, f"Could not establish association with PACS{reject_info}"
    except Exception as e:
        logger.error(f"Pre-flight check failed: {str(e)}")
        return False, f"Connection error: {str(e)}"
    
    # Check 2: Validate CSV file for C-MOVE
    if config['request']['type'].lower() == 'c-move':
        if 'elements_batch_file' in config['request']:
            csv_file = config['request']['elements_batch_file']
            validation_result = validate_csv_schema(csv_file, config)
            if not validation_result[0]:
                return validation_result
    
    logger.info("All pre-flight checks passed")
    return True, "All checks passed"


def validate_csv_schema(csv_file, config):
    """Validate CSV file exists and has required columns.
    
    Args:
        csv_file: Path to CSV file
        config: Configuration dictionary
        
    Returns:
        tuple: (success: bool, message: str)
    """
    if not os.path.exists(csv_file):
        logger.error(f"CSV file not found: {csv_file}")
        return False, f"CSV file not found: {csv_file}"
    
    required_columns = ['StudyInstanceUID', 'PatientID']
    recommended_columns = ['StudyDate', 'AccessionNumber']
    
    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            
            # Check for required columns
            missing_required = [col for col in required_columns if col not in headers]
            if missing_required:
                logger.error(f"CSV missing required columns: {missing_required}")
                return False, f"CSV missing required columns: {missing_required}"
            
            # Warn about recommended columns
            missing_recommended = [col for col in recommended_columns if col not in headers]
            if missing_recommended:
                logger.warning(f"CSV missing recommended columns: {missing_recommended}")
            
            # Count rows
            row_count = sum(1 for _ in reader)
            logger.info(f"CSV validated: {row_count} studies, columns: {headers}")
            
            if row_count == 0:
                logger.error("CSV file is empty (no data rows)")
                return False, "CSV file is empty (no data rows)"
                
    except Exception as e:
        logger.error(f"Error reading CSV file: {str(e)}")
        return False, f"Error reading CSV file: {str(e)}"
    
    return True, f"CSV valid: {row_count} studies"


class hashabledict(dict):
    def __hash__(self):
        # Convert all values to strings to avoid comparison issues
        safe_items = []
        for key, value in self.items():
            safe_items.append((key, str(value) if value is not None else ""))
        return hash(tuple(sorted(safe_items)))

def dataset_to_csv(ds, filepath, fieldnames):
    write_dict = {}
    for key in fieldnames:
        write_dict[key] = str(ds[key].value)

    dict_to_csv(write_dict, filepath, fieldnames)

def request_from_csv(filepath):
    if os.path.exists(filepath):
        with open(filepath, 'r') as csvfile:
            reader = csv.DictReader(csvfile)
            rows = []
            for row in reader:
                # Filter out None keys and ensure all values are strings
                clean_row = {}
                for key, value in row.items():
                    if key is not None:  # Skip None keys from malformed CSV
                        clean_row[key] = str(value) if value is not None else ""
                rows.append(hashabledict(clean_row))
        return rows
    else:
        return []

def dict_to_csv(write_dict, filepath, fieldnames):
    if os.path.exists(filepath):
        with open(filepath, 'a', newline='', ) as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=sorted(fieldnames), dialect='excel')
            writer.writerow(write_dict)
    else:
        with open(filepath, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=sorted(fieldnames), dialect='excel')
            writer.writeheader()
            writer.writerow(write_dict)

def add_element_to_list(element_list, key, value):
    if [x if ((x.partition('=')[0] == key) or (x == key)) else None for x in element_list]:
        return [f'{key}={value}' if ((x.partition('=')[0] == key) or (x == key)) else x for x in element_list]
    else:
        element_list.append(f'{key}={value}')
        return element_list

def create_dataset(request):
    ds = Dataset()
    try:
        elements = [ElementPath(path) for path in request['elements']]
        for elem in elements:
            ds = elem.update(ds)
    except Exception as exc:
        raise exc
    return ds

def split_date_range(date_range):
    """Split a date range string (YYYYMMDD-YYYYMMDD) into 5-day intervals"""
    if not date_range or '-' not in date_range:
        return [date_range]
        
    start_date_str, end_date_str = date_range.split('-')
    start_date = datetime.datetime.strptime(start_date_str, '%Y%m%d')
    end_date = datetime.datetime.strptime(end_date_str, '%Y%m%d')
    
    date_ranges = []
    current_date = start_date
    while current_date <= end_date:
        # Get the date 5 days from current date
        next_period = current_date + datetime.timedelta(days=5)
            
        # Adjust end date to be either end of current 5-day period or final end date
        period_end_date = min(next_period - datetime.timedelta(days=1), end_date)
        
        date_range_str = f"{current_date.strftime('%Y%m%d')}-{period_end_date.strftime('%Y%m%d')}"
        date_ranges.append(date_range_str)
        
        current_date = next_period
        
    return date_ranges

def group_rows_by_month(rows):
    """Group CSV rows by month (YYYYMM) based on StudyDate field.
    
    Returns rows sorted chronologically by month to ensure data is processed
    in order, reducing risk of data loss from mixed processing.
    """
    monthly_groups = defaultdict(list)
    
    for row in rows:
        study_date = row.get('StudyDate', '')
        if len(study_date) >= 6:
            month_key = study_date[:6]  # YYYYMM
        else:
            month_key = '000000'  # Unknown dates go first
        monthly_groups[month_key].append(row)
    
    # Return rows sorted by month (chronological order)
    sorted_rows = []
    for month_key in sorted(monthly_groups.keys()):
        sorted_rows.extend(monthly_groups[month_key])
    
    return sorted_rows

def get_month_from_request(request):
    """Extract month (YYYYMM) from a request's elements."""
    elements = request.get('elements', [])
    if isinstance(elements, str):
        elements = ast.literal_eval(elements)
    for elem in elements:
        if elem.startswith('StudyDate='):
            study_date = elem.split('=')[1]
            if len(study_date) >= 6:
                return study_date[:6]
    return '000000'

def compare_monthly_progress(config):
    """Compare completed vs expected studies by month and print summary."""
    filepath_requests = os.path.join(config['output']['directory'], 'requests.whole')
    filepath_requests_completed = os.path.join(config['output']['directory'], 'requests.completed')
    filepath_requests_failed = os.path.join(config['output']['directory'], 'requests.failed')
    
    whole_requests = request_from_csv(filepath_requests) if os.path.exists(filepath_requests) else []
    completed_requests = request_from_csv(filepath_requests_completed) if os.path.exists(filepath_requests_completed) else []
    failed_requests_list = request_from_csv(filepath_requests_failed) if os.path.exists(filepath_requests_failed) else []
    
    # Group by month
    whole_by_month = defaultdict(int)
    completed_by_month = defaultdict(int)
    failed_by_month = defaultdict(int)
    
    for req in whole_requests:
        month = get_month_from_request(req)
        whole_by_month[month] += 1
    
    for req in completed_requests:
        month = get_month_from_request(req)
        completed_by_month[month] += 1
    
    for req in failed_requests_list:
        month = get_month_from_request(req)
        failed_by_month[month] += 1
    
    # Print summary
    all_months = sorted(set(whole_by_month.keys()) | set(completed_by_month.keys()) | set(failed_by_month.keys()))
    
    summary_lines = [
        "",
        "=" * 60,
        "MONTHLY PROGRESS SUMMARY",
        "=" * 60,
        f"{'Month':<10} {'Expected':<12} {'Completed':<12} {'Failed':<10} {'Status'}",
        "-" * 60
    ]
    
    total_whole = 0
    total_completed = 0
    total_failed = 0
    
    for month in all_months:
        whole = whole_by_month[month]
        completed = completed_by_month[month]
        failed = failed_by_month[month]
        total_whole += whole
        total_completed += completed
        total_failed += failed
        
        if whole == completed and failed == 0:
            status = "Complete"
        elif failed > 0:
            status = "Has failures"
        elif completed < whole:
            status = "In progress"
        else:
            status = "?"
        
        # Format month as YYYY-MM
        month_display = f"{month[:4]}-{month[4:]}" if len(month) == 6 and month != '000000' else month
        summary_lines.append(f"{month_display:<10} {whole:<12} {completed:<12} {failed:<10} {status}")
        
        # Log individual month status
        if failed > 0:
            logger.warning(f"Month {month_display}: {failed} failed out of {whole} studies")
        elif whole == completed:
            logger.info(f"Month {month_display}: All {whole} studies completed successfully")
    
    summary_lines.append("-" * 60)
    summary_lines.append(f"{'TOTAL':<10} {total_whole:<12} {total_completed:<12} {total_failed:<10}")
    summary_lines.append("=" * 60)
    
    # Log the full summary
    for line in summary_lines:
        logger.info(line) if line.strip() else None
    
    # Also print to console for visibility
    print("\n".join(summary_lines))
    
    return {
        'whole_by_month': dict(whole_by_month),
        'completed_by_month': dict(completed_by_month),
        'failed_by_month': dict(failed_by_month)
    }

def create_requests(config):
    requests = []
    if 'elements_batch_file' in config['request']:
        if os.path.exists(config['request']['elements_batch_file']):
            with open(config['request']['elements_batch_file'], newline='') as csvfile:
                reader = csv.DictReader(csvfile, dialect='excel')
                all_rows = list(reader)
                
                # Group rows by month for chronological processing (C-MOVE)
                sorted_rows = group_rows_by_month(all_rows)
                logger.info(f"Processing {len(sorted_rows)} studies grouped by month (chronological order)")
                
                for row in sorted_rows:
                    row_request = config['request'].copy()
                    for key in row:
                        row_request['elements'] = add_element_to_list(row_request['elements'], key, row[key])
                    row_request['elements'] = sorted(row_request['elements'])
                    requests.append(row_request)
        else:
            requests.append(config['request'])
    else:
        # Find StudyDate element and split into weekly intervals if present
        study_date_element = None
        for element in config['request']['elements']:
            if element.startswith('StudyDate='):
                study_date_element = element
                break
                
        if study_date_element:
            date_range = study_date_element.split('=')[1]
            date_ranges = split_date_range(date_range)
            
            # Create a request for each weekly interval
            for weekly_range in date_ranges:
                weekly_request = config['request'].copy()
                weekly_request['elements'] = weekly_request['elements'].copy()
                
                # Replace the StudyDate element with the weekly range
                weekly_request['elements'] = [
                    f"StudyDate={weekly_range}" if e.startswith('StudyDate=') else e 
                    for e in weekly_request['elements']
                ]
                requests.append(weekly_request)
        else:
            requests.append(config['request'])

    filepath_requests = os.path.join(config['output']['directory'], 'requests.whole')
    filepath_requests_completed = os.path.join(config['output']['directory'], 'requests.completed')
    filepath_requests_failed = os.path.join(config['output']['directory'], 'requests.failed')
    if os.path.exists(filepath_requests):
        os.remove(filepath_requests)
    if os.path.exists(filepath_requests_completed):
        os.remove(filepath_requests_completed)
    if os.path.exists(filepath_requests_failed):
        os.remove(filepath_requests_failed)

    for request in requests:
        dict_to_csv(request, filepath_requests, request.keys())

    return requests

def pending_requests(config):
    """
    Returns pending requests to enable resuming
    """
    filepath_requests = os.path.join(config['output']['directory'], 'requests.whole')
    filepath_requests_completed = os.path.join(config['output']['directory'], 'requests.completed')

    reqs1 = request_from_csv(filepath_requests)
    reqs2 = []
    if os.path.isfile(filepath_requests_completed):
        reqs2 = request_from_csv(filepath_requests_completed)

    requests = list(set(reqs1) - set(reqs2))
    for request in requests:
        request['elements'] = list(sorted(ast.literal_eval(request['elements'])))
        request['throttle_time'] = float(request['throttle_time'])
        request['threads'] = int(request['threads'])
    
    # questions = []
    # if requests:
    #     questions = [
    #     inquirer.List('resume',
    #                     message="A partial extraction was detected. Do you want to resume or overwrite?",
    #                     choices=['Resume', 'Overwrite'],
    #                 ),
    #     ]
    # else:
    #     questions = [
    #     inquirer.List('resume',
    #                     message="A completed extraction was detected. Do you want to overwrite?",
    #                     choices=['Cancel', 'Overwrite'],
    #                 ),
    #     ]

    # answers = inquirer.prompt(questions)
    # if answers['resume'] == 'Overwrite':
    #     requests = create_requests(config)
    # Automatically select "Overwrite" to avoid interactive prompts
    logger.info("Starting fresh extraction (overwrite mode)")
    requests = create_requests(config)    
    return requests

def failed_requests(config):
    """
    Returns failed requests to enable re-trying.
    Reads from requests.failed, removes the file, and returns those requests for retry.
    """
    filepath_requests_failed = os.path.join(config['output']['directory'], 'requests.failed')

    reqs_failed = request_from_csv(filepath_requests_failed)
    requests = list(set(reqs_failed))
    
    if requests:
        logger.warning(f"Found {len(requests)} failed requests to retry")
        # Group by month for visibility
        failed_by_month = defaultdict(int)
        for req in requests:
            month = get_month_from_request(req)
            failed_by_month[month] += 1
        
        logger.info("Failed requests by month:")
        for month in sorted(failed_by_month.keys()):
            month_display = f"{month[:4]}-{month[4:]}" if len(month) == 6 and month != '000000' else month
            logger.info(f"  {month_display}: {failed_by_month[month]} studies")
        
        for request in requests:
            request['elements'] = list(sorted(ast.literal_eval(request['elements'])))
            request['throttle_time'] = float(request['throttle_time'])
            request['threads'] = int(request['threads'])
        
        # Remove the failed file so retried requests get fresh tracking
        if os.path.exists(filepath_requests_failed):
            os.remove(filepath_requests_failed)
            logger.debug(f"Removed {filepath_requests_failed} for fresh retry tracking")
    else:
        logger.info("No failed requests found, checking for pending requests...")
        requests = pending_requests(config)
    
    return requests

def thread_scu_function(config, pbar, pbar_lock, requests):
    scu = SCU(config)
    scu.pbar = pbar
    scu.pbar_lock = pbar_lock  # Pass the shared progress bar lock
    scu.process_requests_batch(list(requests))
    return

def process_request_batch(config, dry_run=False):
    """Process batch requests with pre-flight checks and statistics.
    
    Args:
        config: Configuration dictionary
        dry_run: If True, validate config and show what would be done without executing
    """
    # Initialize logging
    setup_logging(config['output']['directory'])
    
    logger.info(f"Starting {config['request']['type'].upper()} batch processing")
    logger.info(f"Output directory: {config['output']['directory']}")
    logger.debug(f"Config: threads={config['request']['threads']}, throttle={config['request'].get('throttle_time', 0)}")
    
    # Run pre-flight checks
    preflight_success, preflight_msg = preflight_check(config)
    if not preflight_success:
        logger.error(f"Pre-flight check failed: {preflight_msg}")
        print(f"\nPre-flight check FAILED: {preflight_msg}")
        print("Fix the issue and try again.")
        return
    
    requests = []
    filepath_requests = os.path.join(config['output']['directory'], 'requests.whole')
    filepath_requests_completed = os.path.join(config['output']['directory'], 'requests.completed')
    filepath_requests_failed = os.path.join(config['output']['directory'], 'requests.failed')

    if (os.path.isfile(filepath_requests)):
        # Previous extraction detected
        logger.info("Previous extraction detected")
        if os.path.isfile(filepath_requests_failed):
            # Failed requests detected
            logger.warning("Failed requests file found - will retry failed requests")
            requests = failed_requests(config)
        else:
            # No failed requests detected, return pending requests
            requests = pending_requests(config)
    else:
        # No previous extraction detected
        logger.info("No previous extraction detected - starting fresh")
        requests = create_requests(config)
    
    if requests:
        # Dry run mode - show what would be done without executing
        if dry_run:
            logger.info("=" * 60)
            logger.info("DRY RUN MODE - No requests will be sent")
            logger.info("=" * 60)
            logger.info(f"Would process {len(requests)} requests")
            logger.info(f"Using {config['request']['threads']} threads")
            logger.info(f"Throttle time: {config['request'].get('throttle_time', 0)} seconds")
            
            # Show monthly breakdown
            monthly_counts = defaultdict(int)
            for req in requests:
                month = get_month_from_request(req)
                monthly_counts[month] += 1
            
            logger.info("Requests by month:")
            for month in sorted(monthly_counts.keys()):
                month_display = f"{month[:4]}-{month[4:]}" if len(month) == 6 and month != '000000' else month
                logger.info(f"  {month_display}: {monthly_counts[month]} studies")
            
            logger.info("=" * 60)
            logger.info("Dry run complete. Remove --dry-run flag to execute.")
            return
        
        # Start statistics tracking
        batch_stats.start(len(requests))
        
        watch_sigint()
        split_requests = np.array_split(requests, config['request']['threads'])
        logger.info(f"Processing {len(requests)} requests using {config['request']['threads']} threads")
        logger.info("To stop extraction, press CTRL-C. Extraction can be resumed at a later time.")
        pbar = tqdm.tqdm(total=len(requests), 
            desc='Sending {} requests '.format(config['request']['type']), 
            unit='rqst')
        pbar_lock = threading.Lock()  # Create a shared lock for the progress bar
        fn = lambda x : thread_scu_function(config, pbar, pbar_lock, x)
        if config['request']['threads'] > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=config['request']['threads']) as executor:
                executor.map(fn, split_requests)
        else:
            fn(split_requests[0])
        pbar.close()
        
        # Finish statistics and show summary
        batch_stats.finish()
        logger.info("Batch processing completed")
        
        # Show statistics summary
        batch_stats.print_summary()
        
        # Show monthly progress summary after processing
        compare_monthly_progress(config)
        
        # Check if there are failed requests that need retry
        if os.path.isfile(filepath_requests_failed):
            logger.warning("Some requests failed. Re-run the same command to retry failed requests.")
    else:
        logger.info('No further requests pending')
        # Still show summary even if no new requests
        compare_monthly_progress(config)


def seconds_until(time_str, tzname):
    """
    Returns the number of seconds from now until the time in format HH:mm
    """
    
    list_time_str = time_str.split(':')
    

    h = int(list_time_str[0])
    m = int(list_time_str[1])
    tz = pytz.timezone(tzname)
    now = datetime.datetime.now(tz=tz)
    return int((datetime.timedelta(hours=24) 
        - (now - now.replace(hour=h, minute=m, second=0, microsecond=0)))
        .total_seconds() % (24 * 3600))

class SCU(object):
    """ SCU class
    This class is used to send batches of DIMSE requests (c-find, c-echo, c-move) to a remote SCP
    """
    def __init__(self, config):
        self.config = config
        self.ae = self.create_ae()
        self.query_model = self.create_query_model()
        self.pbar = None
        self.pbar_lock = threading.Lock()  # Add thread lock for progress bar updates
        
    def create_ae(self):
        # Create application entity
        ae = AE(ae_title=self.config['local']['aet'])

        # Set timeouts
        ae.acse_timeout = 300
        ae.dimse_timeout = 300
        ae.network_timeout = 300

        # Set the Presentation Contexts we are requesting the Find SCP support
        if self.config['request']['type'].lower() == 'c-find':
            ae.requested_contexts = QueryRetrievePresentationContexts
                
        elif self.config['request']['type'].lower() == 'c-echo':
            ae.requested_contexts = [VerificationSOPClass]

        elif self.config['request']['type'].lower() == 'c-move':
            ae.requested_contexts = QueryRetrievePresentationContexts

        return ae

    def establish_association(self):
        return self.ae.associate(self.config['pacs']['hostname'], 
            self.config['pacs']['port'],  
            ae_title=self.config['pacs']['aet'],
            max_pdu=16382)

    def wait_until_scheduled_time(self):
        
        if 'schedule' in self.config:
            if self.config['schedule']['enabled']:
                sec_until_start = seconds_until(self.config['schedule']['start_time'], self.config['schedule']['timezone'])
                sec_until_end = seconds_until(self.config['schedule']['end_time'], self.config['schedule']['timezone'])
    
                if sec_until_end > sec_until_start:
                    if self.pbar:
                        self.pbar.set_description('Extraction PAUSED (will resume at {})'.format(self.config['schedule']['start_time']))
                    time.sleep(sec_until_start)
                else:
                    if self.pbar:
                        self.pbar.set_description('Sending {} requests (will pause at {})'.format(self.config['request']['type'], self.config['schedule']['end_time']))
                    

    def retry_association(self):
        for i in range(100):
            if not self.association.is_established:
                time.sleep(1)
                logger.warning(f'Re-trying association (attempt {i+1}/100)')
                self.association = self.establish_association()
            else:
                logger.debug('Association re-established successfully')
                return
        
        logger.critical('Failed to establish association after 100 attempts')
        sys.exit(1)


    def create_query_model(self):
        request = self.config['request']
        query_model = None
        if request['type'].lower() == 'c-find':
            if request['model'] == 'study':
                query_model = StudyRootQueryRetrieveInformationModelFind
            elif self.config['request']['model'] == 'psonly':
                query_model = PatientStudyOnlyQueryRetrieveInformationModelFind
            else:
                query_model = PatientRootQueryRetrieveInformationModelFind
        elif request['type'].lower() == 'c-move':
            if request['model'] == 'study':
                query_model = StudyRootQueryRetrieveInformationModelMove
            elif self.config['request']['model'] == 'psonly':
                query_model = PatientStudyOnlyQueryRetrieveInformationModelMove
            else:
                query_model = PatientRootQueryRetrieveInformationModelMove
        return query_model

    def process_requests_batch(self, requests):
        global continue_extraction

        self.association = self.establish_association()
        for request in requests:
            if continue_extraction:
                self.wait_until_scheduled_time()
                self.process_request(request)
                time.sleep(request['throttle_time'])
        self.association.release()
        
    
    def process_request(self, request):
        if request['type'].lower() == 'c-find':
            self.send_find(request)
        if request['type'].lower() == 'c-move':
            self.send_move(request)
        return
    
    
    def send_find(self, request):
        request_start = time.time()
        identifier = create_dataset(request)
        keywords = [ElementPath(path).keyword for path in request['elements']]

        if not self.association.is_established:
            self.retry_association()
        
        if self.association.is_established:
            responses = self.association.send_c_find(identifier, self.query_model)
            response_count = 0
                    
            for (status, rsp_identifier) in responses:
                if status and status.Status in [0xFF00, 0xFF01]:
                    # Status pending
                    response_count += 1
                    # Apply description filters if enabled
                    should_save = True
                    if 'description_filter' in self.config['request'] and self.config['request']['description_filter']['enabled']:
                        should_save = False
                        # Check study description filter
                        if self.config['request']['description_filter']['study_description'] and 'StudyDescription' in rsp_identifier:
                            study_desc = str(rsp_identifier.StudyDescription).upper()
                            for filter_str in self.config['request']['description_filter']['study_description']:
                                if filter_str.upper() in study_desc:
                                    should_save = True
                                    break
                        
                        # Check series description filter if at SERIES level
                        if (not should_save and 
                            self.config['request']['description_filter']['series_description'] and 
                            'SeriesDescription' in rsp_identifier and
                            'QueryRetrieveLevel' in rsp_identifier and
                            rsp_identifier.QueryRetrieveLevel == 'SERIES'):
                            series_desc = str(rsp_identifier.SeriesDescription).upper()
                            for filter_str in self.config['request']['description_filter']['series_description']:
                                if filter_str.upper() in series_desc:
                                    should_save = True
                                    break
                    
                    if should_save:
                        path = os.path.join(self.config['output']['directory'], self.config['output']['database_file'])
                        dataset_to_csv(rsp_identifier, path, keywords)
                else:
                    if self.pbar:
                        with self.pbar_lock:  # Use lock when updating the progress bar
                            self.pbar.update(1)
                    # Status Success, Warning, Cancel, Failure
                    request_duration = time.time() - request_start
                    if status.Status in [0x0000]:
                        if response_count > 4500:
                            study_date = next((elem.split('=')[1] for elem in request['elements'] if elem.startswith('StudyDate=')), None)
                            logger.warning(f"Large response count ({response_count}) for date range {study_date}. Consider using smaller date ranges (Limit is 5000).")
                        filepath_requests_completed = os.path.join(self.config['output']['directory'], 'requests.completed')
                        dict_to_csv(request, filepath_requests_completed, request.keys())
                        logger.debug(f"C-FIND completed: {response_count} results in {request_duration:.2f}s")
                        batch_stats.record_success(request_duration)
                    else:
                        filepath_requests_failed = os.path.join(self.config['output']['directory'], 'requests.failed')
                        dict_to_csv(request, filepath_requests_failed, request.keys())
                        study_date = next((elem.split('=')[1] for elem in request['elements'] if elem.startswith('StudyDate=')), None)
                        logger.error(f"C-FIND failed for date range {study_date}: status=0x{status.Status:04X}")
                        batch_stats.record_failure()
                
                    

    def send_move(self, request, max_retries=3):
        """Send C-MOVE request with retry logic.
        
        Args:
            request: The request dictionary
            max_retries: Maximum number of retry attempts for transient failures
        """
        request_start = time.time()
        identifier = create_dataset(request)
        keywords = [ElementPath(path).keyword for path in request['elements']]
        
        # Extract study info for logging
        study_uid = next((elem.split('=')[1] for elem in request['elements'] if elem.startswith('StudyInstanceUID=')), 'unknown')
        patient_id = next((elem.split('=')[1] for elem in request['elements'] if elem.startswith('PatientID=')), 'unknown')

        if not self.association.is_established:
            self.retry_association()
        
        for attempt in range(max_retries + 1):
            if self.association.is_established:
                try:
                    responses = self.association.send_c_move(identifier, self.config['local']['aet'], self.query_model)
                    for (status, rsp_identifier) in responses:
                        if status and status.Status in [0xFF00]:
                            # Status pending
                            pass
                        else:
                            # Status Success, Warning, Cancel, Failure
                            if self.pbar:
                                with self.pbar_lock:  # Use lock when updating the progress bar
                                    self.pbar.update(1)
                            identifier.Status = hex(status.Status)
                            keywords.append('Status')
                            path = os.path.join(self.config['output']['directory'], self.config['output']['database_file'])
                            dataset_to_csv(identifier, path, keywords)
                            
                            request_duration = time.time() - request_start

                            if status.Status in [0x0000]:
                                filepath_requests_completed = os.path.join(self.config['output']['directory'], 'requests.completed')
                                dict_to_csv(request, filepath_requests_completed, request.keys())
                                logger.debug(f"C-MOVE completed: PatientID={patient_id}, StudyUID={study_uid[:30]}... ({request_duration:.2f}s)")
                                batch_stats.record_success(request_duration)
                                return  # Success, exit retry loop
                            else:
                                # Check if status is retryable (transient errors)
                                retryable_statuses = [0xA701, 0xA702, 0xA900, 0xC000]  # Resource limitation, unable to process
                                if status.Status in retryable_statuses and attempt < max_retries:
                                    delay = min(2 ** attempt, 30)  # Exponential backoff, max 30s
                                    logger.warning(f"C-MOVE transient failure (attempt {attempt+1}/{max_retries+1}): PatientID={patient_id}, status=0x{status.Status:04X}. Retrying in {delay}s...")
                                    batch_stats.record_retry()
                                    time.sleep(delay)
                                    break  # Break inner loop to retry
                                else:
                                    filepath_requests_failed = os.path.join(self.config['output']['directory'], 'requests.failed')
                                    dict_to_csv(request, filepath_requests_failed, request.keys())
                                    logger.error(f"C-MOVE failed: PatientID={patient_id}, StudyUID={study_uid[:30]}..., status=0x{status.Status:04X}")
                                    batch_stats.record_failure()
                                    return  # Permanent failure, exit
                    else:
                        # Loop completed without break (no retry needed)
                        return
                except Exception as e:
                    if attempt < max_retries:
                        delay = min(2 ** attempt, 30)
                        logger.warning(f"C-MOVE exception (attempt {attempt+1}/{max_retries+1}): {str(e)}. Retrying in {delay}s...")
                        batch_stats.record_retry()
                        time.sleep(delay)
                        # Try to re-establish association
                        self.retry_association()
                    else:
                        logger.error(f"C-MOVE exception: PatientID={patient_id}, StudyUID={study_uid[:30]}..., error={str(e)}")
                        filepath_requests_failed = os.path.join(self.config['output']['directory'], 'requests.failed')
                        dict_to_csv(request, filepath_requests_failed, request.keys())
                        batch_stats.record_failure()
                        return




