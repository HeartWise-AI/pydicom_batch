#!/usr/bin/env python3
import argparse
import ast
import csv
import sys


def parse_elements(elements_str):
    """Parse the elements string into a dictionary."""
    elements = ast.literal_eval(elements_str)
    result = {}
    for item in elements:
        key, value = item.split('=', 1)
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description='Convert requests.failed to CSV for retry')
    parser.add_argument('input', help='Path to requests.failed file')
    parser.add_argument('-o', '--output', help='Output CSV file (default: stdout)')
    args = parser.parse_args()

    fieldnames = [
        'AccessionNumber',
        'ModalitiesInStudy',
        'PatientID',
        'PatientName',
        'QueryRetrieveLevel',
        'StudyDate',
        'StudyDescription',
        'StudyInstanceUID',
    ]

    rows = []
    with open(args.input, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            elements = parse_elements(row['elements'])
            out_row = {
                'AccessionNumber': elements.get('AccessionNumber', ''),
                'ModalitiesInStudy': elements.get('Modality', ''),
                'PatientID': elements.get('PatientID', ''),
                'PatientName': '',
                'QueryRetrieveLevel': elements.get('QueryRetrieveLevel', ''),
                'StudyDate': '',
                'StudyDescription': '',
                'StudyInstanceUID': elements.get('StudyInstanceUID', ''),
            }
            rows.append(out_row)

    if args.output:
        outfile = open(args.output, 'w', newline='')
    else:
        outfile = sys.stdout

    writer = csv.DictWriter(outfile, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

    if args.output:
        outfile.close()
        print(f'Wrote {len(rows)} rows to {args.output}', file=sys.stderr)


if __name__ == '__main__':
    main()


