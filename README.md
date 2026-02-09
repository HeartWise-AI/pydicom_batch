# PYDICOM Batch

<div align="center">

![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

**Robust batch DICOM image export from PACS servers**

</div>

## Overview

PYDICOM Batch is a production-ready tool for extracting medical images from PACS servers using the DICOM protocol. It supports large-scale batch operations with built-in resilience features like automatic retries, monthly data organization, and comprehensive logging.

### Key Features

- **C-FIND**: Query PACS for available studies (auto-splits into 5-day intervals)
- **C-MOVE**: Retrieve DICOM images with monthly organization
- **Resilience**: Pre-flight checks, automatic retries with exponential backoff
- **Monitoring**: Real-time progress, statistics, and detailed logging
- **Recovery**: Resume interrupted extractions, retry failed requests
- **Dry-run mode**: Validate configuration without executing

## Quick Start

### Installation

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# or: .venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt
```

### Configuration

```bash
# Copy template
cp config/dicom-template.yml.example config/dicom.yml

# Edit with your PACS settings
nano config/dicom.yml
```

### Basic Usage

```bash
# Query studies (C-FIND)
python main.py -c config/dicom.yml -o c-find

# Retrieve images (C-MOVE)
python main.py -c config/dicom.yml -o c-move

# Validate config without executing
python main.py -c config/dicom.yml -o c-move --dry-run
```

## Configuration Reference

### Config File Structure

```yaml
common:
  pacs:
    hostname: 10.128.228.146    # PACS server IP/hostname
    port: 5000                   # PACS port
    aet: PACS_AET               # PACS Application Entity Title
  local:
    port: 4000                   # Local port for receiving images
    aet: MY_AET                  # Your Application Entity Title
  schedule:
    enabled: false               # Enable scheduled extraction windows
    start_time: '18:30'
    end_time: '06:45'
    timezone: America/New_York

c-find:
  threads: 16
  throttle_time: 0               # Delay between requests (seconds)
  model: study                   # Query model: study, patient, psonly
  elements:
    - PatientID=*
    - StudyDate=20250101-20251231  # Date range (auto-splits into 5-day chunks)
    - PatientName=*
    - AccessionNumber=*
    - StudyInstanceUID=*
    - StudyDescription=*
    - ModalitiesInStudy=XA
    - QueryRetrieveLevel=STUDY
  output:
    directory: data
    database_file: data/studies.csv

c-move:
  threads: 8
  throttle_time: 0
  model: study
  elements_batch_file: data/studies.csv  # CSV from C-FIND results
  elements:
    - PatientID=*
    - StudyInstanceUID=*
    - QueryRetrieveLevel=STUDY
  output:
    directory: /path/to/output
    database_file: /path/to/output/database.csv
    directory_structure: PatientID/StudyInstanceUID/SeriesInstanceUID
    filename: SOPInstanceUID
    decompress: false
```

## Features In-Depth

### Pre-flight Checks

Before processing, the tool automatically:

1. **C-ECHO test**: Verifies PACS is reachable and responding
2. **CSV validation**: Ensures batch file has required columns
3. **Directory check**: Creates output directories if needed

```
Running pre-flight checks...
Testing PACS connectivity: 10.128.228.146:5000
C-ECHO successful - PACS is reachable
CSV validated: 1710 studies, columns: ['AccessionNumber', 'PatientID', ...]
All pre-flight checks passed
```

### Monthly Organization

Studies are processed in chronological order by month to minimize data loss:

```
Processing 1710 studies grouped by month (chronological order)
  2025-01: 145 studies
  2025-02: 138 studies
  ...
```

### Automatic Retry

Failed requests are automatically retried with exponential backoff:

- Up to 3 retries per request
- Delays: 1s → 2s → 4s (max 30s)
- Handles transient PACS errors (resource limitations)
- Re-establishes connections on network failures

### Statistics & Monitoring

After processing:

```
============================================================
BATCH PROCESSING STATISTICS
============================================================
Total Requests:      1710
Successful:          1695 (99.1%)
Failed:              15
Retried Requests:    8 (12 total retries)
------------------------------------------------------------
Total Time:          3845.2 seconds (64.1 min)
Avg Request Time:    2.24 seconds
Throughput:          26.4 requests/minute
============================================================

============================================================
MONTHLY PROGRESS SUMMARY
============================================================
Month      Expected     Completed    Failed     Status
------------------------------------------------------------
2025-01    145          145          0          Complete
2025-02    138          138          0          Complete
2025-03    142          140          2          Has failures
...
============================================================
```

### Logging

Logs are written to `{output_directory}/logs/`:

```
logs/
├── pydicom_batch_20250207.log
├── pydicom_batch_20250207.log.1  # Rotated logs
└── ...
```

Log format:
```
2025-02-07 14:32:15 | INFO     | process_request_batch     | Starting C-MOVE batch processing
2025-02-07 14:32:16 | WARNING  | send_move                 | C-MOVE transient failure (attempt 1/4): PatientID=123456
2025-02-07 14:32:18 | DEBUG    | send_move                 | C-MOVE completed: PatientID=123456 (2.34s)
```

### Recovery

**Resume interrupted extraction:**
```bash
# Just re-run the same command - it detects previous progress
python main.py -c config/dicom.yml -o c-move
```

**Retry failed requests:**
```bash
# Failed requests are tracked in requests.failed
# Re-running automatically retries them
python main.py -c config/dicom.yml -o c-move
```

## Command Line Reference

```
usage: main.py [-h] [-c CONFIG] [-o {c-find,c-move}] [--dry-run]

PYDICOM Batch Processing Tool

options:
  -h, --help            show this help message and exit
  -c, --config CONFIG   Path to config file (default: config/dicom-template.yml)
  -o, --operation       Operation: c-find or c-move (default: c-find)
  --dry-run             Validate config without executing

Examples:
  python main.py -c config/dicom.yml -o c-find
  python main.py -c config/dicom.yml -o c-move
  python main.py -c config/dicom.yml -o c-move --dry-run
```

## Project Structure

```
pydicom-batch-v2/
├── main.py              # Entry point
├── common.py            # Config loading and orchestration
├── scu.py               # DICOM SCU (query/retrieve operations)
├── scp.py               # DICOM SCP (receive images)
├── config/
│   ├── dicom-template.yml.example
│   └── dicom.yml        # Your configuration
├── data/
│   ├── studies.csv      # C-FIND results
│   ├── requests.whole   # All requests
│   ├── requests.completed
│   └── requests.failed
├── logs/
│   └── pydicom_batch_YYYYMMDD.log
└── requirements.txt
```

## Firewall Configuration

If not using Docker/containers, open the local port for receiving DICOM images:

```bash
# Linux (firewalld)
sudo firewall-cmd --add-port=4000/tcp --permanent
sudo firewall-cmd --reload

# Verify
sudo firewall-cmd --list-ports
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Could not establish association" | Check PACS hostname/port, verify AET is registered |
| "C-ECHO failed" | PACS may be down or rejecting connections |
| "CSV missing required columns" | Ensure CSV has `StudyInstanceUID` and `PatientID` |
| Large response warnings | Date range too wide, use smaller intervals |
| Slow throughput | Reduce threads, increase throttle_time |

## Acknowledgments

Based on [pydicom-batch](https://github.com/therlaup/pydicom-batch) by [@therlaup](https://github.com/therlaup).

Built with:
- [pydicom](https://github.com/pydicom/pydicom) - DICOM file handling
- [pynetdicom](https://github.com/pydicom/pynetdicom) - DICOM networking

## License

MIT License - see [LICENSE](LICENSE) for details.
