import argparse
from common import pydicombatch

def parse_arguments():
    parser = argparse.ArgumentParser(
        description='PYDICOM Batch Processing Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python main.py -c config/dicom-template.yml -o c-find
  python main.py -c config/dicom-template.yml -o c-move
  python main.py -c config/dicom-template.yml -o c-move --dry-run
        '''
    )
    parser.add_argument(
        '-c', '--config',
        default='config/dicom-template.yml',
        help='Path to config file (default: config/dicom-template.yml)'
    )
    parser.add_argument(
        '-o', '--operation',
        choices=['c-find', 'c-move'],
        default='c-find',
        help='Operation to perform (default: c-find)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Validate config and show what would be done without executing'
    )
    return parser.parse_args()

print("\n█▀█ █▄█ █▀▄ █ █▀▀ █▀█ █▀▄▀█   █▄▄ ▄▀█ ▀█▀ █▀▀ █░█\n█▀▀ ░█░ █▄▀ █ █▄▄ █▄█ █░▀░█   █▄█ █▀█ ░█░ █▄▄ █▀█\n")

def main():
    args = parse_arguments()
    pydicombatch(args.config, args.operation.lower(), dry_run=args.dry_run)

if __name__ == "__main__":
    main()
