import yaml
from scu import process_request_batch, setup_logging
from scp import SCP
import os
import sys
import logging

logger = logging.getLogger('pydicom_batch')

def merge_configs(config, operation):
    """Merge common config with operation-specific config"""
    merged = {
        'pacs': config['common']['pacs'],
        'local': config['common']['local'],
        'schedule': config['common']['schedule'],
        'request': {
            'type': operation,
            **config[operation]
        }
    }
    
    # Handle output settings
    merged['output'] = {}
    # If common has output settings, use them as base
    if 'output' in config['common']:
        merged['output'].update(config['common']['output'])
    # Override/add operation-specific output settings
    if 'output' in config[operation]:
        merged['output'].update(config[operation]['output'])
        
    # Add anonymization settings for c-move if they exist
    if operation == 'c-move' and 'anonymization' in config[operation]:
        merged['anonymization'] = config[operation]['anonymization']
        
    return merged

def pydicombatch(config_file, operation, dry_run=False):
    """Run DICOM batch processing.
    
    Args:
        config_file: Path to YAML configuration file
        operation: Operation type ('c-find' or 'c-move')
        dry_run: If True, validate config and show what would be done without executing
    """
    with open(config_file) as file:
        config = yaml.load(file, Loader=yaml.FullLoader)
        
        # Merge common config with operation-specific config
        merged_config = merge_configs(config, operation)
        
        # Ensure output directory exists
        os.makedirs(merged_config['output']['directory'], exist_ok=True)
        
        # Initialize logging early
        setup_logging(merged_config['output']['directory'])
        
        if dry_run:
            logger.info("=" * 60)
            logger.info("DRY RUN MODE - Validating configuration")
            logger.info("=" * 60)
        
        logger.info(f"Running {operation.upper()} extraction defined in: {config_file}")
        logger.info(f"PACS: {merged_config['pacs']['hostname']}:{merged_config['pacs']['port']} (AET: {merged_config['pacs']['aet']})")
        logger.info(f"Local AET: {merged_config['local']['aet']}")

        if operation == 'c-move' and not dry_run:
            scp = SCP(merged_config)
            scp.start_server()
            logger.info(f"SCP server started on port {merged_config['local']['port']}")
        try:
            process_request_batch(merged_config, dry_run=dry_run)
        except KeyboardInterrupt:
            logger.warning("Extraction interrupted by user (CTRL-C)")
            print('\nExtraction stopped. To resume extraction, please re-execute the script.')
            if operation == 'c-move' and not dry_run:
                scp.stop_server()
                logger.info("SCP server stopped")

            # For c-move, use the output directory for failed requests
            # For c-find, use the directory of the database file
            if operation == 'c-move':
                failed_dir = merged_config['output']['directory']
            else:
                failed_dir = os.path.dirname(merged_config['output']['database_file'])
                
            filepath_requests_failed = os.path.join(failed_dir, 'requests.failed')
            if os.path.exists(filepath_requests_failed):
                logger.warning('Failed requests detected. To re-try failed request, re-run batch request.')
            sys.exit(0)