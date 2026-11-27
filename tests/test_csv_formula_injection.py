"""Study exports must neutralize archive text that spreadsheets treat as formulas.

A leading =', '+', '-', '@', tab, or carriage return is prefixed with a single
quote. Other archive values are written unchanged.
"""

import csv
import os
import socket
import sys
import tempfile
import unittest

from pydicom.dataset import Dataset
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
)

import retry_failed
import scu

FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

FIND_FIELDS = {
    "PatientName": "=1+1",
    "StudyDescription": "=1+1,2",
    "PatientID": "-3+3",
    "AccessionNumber": "@SUM(1+1)",
    "InstitutionName": "+2+2",
    "ReferringPhysicianName": "\t=1+1",
    "StudyComments": "\r=1+1",
    "SeriesDescription": "=2+2",
    "Manufacturer": "ACME",
    "QueryRetrieveLevel": "STUDY",
    "StudyInstanceUID": "1.2.840.99999.1",
    "StudyDate": "20240101",
    "ModalitiesInStudy": "XA",
}

QUERY_KEYS = ("QueryRetrieveLevel", "StudyDate", "ModalitiesInStudy")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def read_rows(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def assert_export_row(testcase, actual, expected):
    for field, original in expected.items():
        with testcase.subTest(field=field):
            cell = actual[field]
            if original[:1] in FORMULA_TRIGGERS:
                testcase.assertEqual("'" + original, cell)
            else:
                testcase.assertEqual(original, cell)


def query_elements(fields):
    elements = []
    for key, value in fields.items():
        if key in QUERY_KEYS:
            elements.append(f"{key}={value}")
        else:
            elements.append(f"{key}=*")
    return elements


def dataset_from(fields):
    identifier = Dataset()
    for key, value in fields.items():
        setattr(identifier, key, value)
    return identifier


class Archive:
    def __init__(self, contexts, handlers):
        self.ae = AE(ae_title="ARCHIVE")
        for context in contexts:
            self.ae.add_supported_context(context)
        self.port = free_port()
        self.ae.start_server(
            ("127.0.0.1", self.port),
            block=False,
            evt_handlers=handlers,
        )

    def stop(self):
        self.ae.shutdown()


def run_find(directory, elements, handler, description_filter=None):
    archive = Archive(
        [StudyRootQueryRetrieveInformationModelFind],
        [(evt.EVT_C_FIND, handler)],
    )
    config = {
        "local": {"aet": "BATCH_SCU", "port": free_port()},
        "pacs": {"hostname": "127.0.0.1", "port": archive.port, "aet": "ARCHIVE"},
        "request": {
            "type": "c-find",
            "model": "study",
            "elements": elements,
            "throttle_time": 0,
            "threads": 1,
        },
        "output": {"directory": directory, "database_file": "database.csv"},
    }
    if description_filter is not None:
        config["request"]["description_filter"] = description_filter
    tool = scu.SCU(config)
    try:
        tool.association = tool.establish_association()
        if not tool.association.is_established:
            raise AssertionError("C-FIND association was not established")
        tool.send_find(
            {
                "type": "c-find",
                "model": "study",
                "elements": list(elements),
                "throttle_time": 0,
                "threads": 1,
            }
        )
    finally:
        association = getattr(tool, "association", None)
        if association is not None and association.is_established:
            association.release()
        archive.stop()
    return os.path.join(directory, "database.csv")


class FormulaInjectionTests(unittest.TestCase):
    def test_cfind_pending_matches_neutralize_formula_fields(self):
        second = dict(FIND_FIELDS)
        second["StudyInstanceUID"] = "1.2.840.99999.2"
        second["PatientName"] = "=9+9"

        def handle_find(event):
            yield 0xFF00, dataset_from(FIND_FIELDS)
            yield 0xFF01, dataset_from(second)

        with tempfile.TemporaryDirectory() as directory:
            path = run_find(directory, query_elements(FIND_FIELDS), handle_find)
            rows = read_rows(path)

        self.assertEqual(2, len(rows))
        by_uid = {row["StudyInstanceUID"]: row for row in rows}
        for uid, expected in (
            ("1.2.840.99999.1", FIND_FIELDS),
            ("1.2.840.99999.2", second),
        ):
            with self.subTest(uid=uid):
                assert_export_row(self, by_uid[uid], expected)

    def test_cfind_description_filter_keeps_matching_formula_fields(self):
        matched = dict(FIND_FIELDS)
        matched["StudyDescription"] = "=1+1ECHO"
        matched["SeriesDescription"] = "PLAIN"
        skipped = dict(FIND_FIELDS)
        skipped["StudyInstanceUID"] = "1.2.840.99999.6"
        skipped["StudyDescription"] = "NOMATCH"
        skipped["SeriesDescription"] = "PLAIN"
        series = dict(FIND_FIELDS)
        series["StudyInstanceUID"] = "1.2.840.99999.7"
        series["QueryRetrieveLevel"] = "SERIES"
        series["StudyDescription"] = "NOMATCH"
        series["SeriesDescription"] = "=2+2HEART"

        def handle_find(event):
            yield 0xFF00, dataset_from(matched)
            yield 0xFF00, dataset_from(skipped)
            yield 0xFF00, dataset_from(series)

        description_filter = {
            "enabled": True,
            "study_description": ["ECHO"],
            "series_description": ["HEART"],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = run_find(
                directory,
                query_elements(FIND_FIELDS),
                handle_find,
                description_filter,
            )
            rows = read_rows(path)

        self.assertEqual(
            ["1.2.840.99999.1", "1.2.840.99999.7"],
            [row["StudyInstanceUID"] for row in rows],
        )
        by_uid = {row["StudyInstanceUID"]: row for row in rows}
        for uid, expected in (
            ("1.2.840.99999.1", matched),
            ("1.2.840.99999.7", series),
        ):
            with self.subTest(uid=uid):
                assert_export_row(self, by_uid[uid], expected)

    def test_cmove_and_retry_neutralize_archive_fields_from_batch(self):
        # C-MOVE reads the study spreadsheet and writes those archive fields again.
        batch_fields = dict(FIND_FIELDS)
        with tempfile.TemporaryDirectory() as directory:
            batch_path = os.path.join(directory, "database.csv")
            with open(batch_path, "w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=sorted(batch_fields))
                writer.writeheader()
                writer.writerow(batch_fields)

            move_dir = os.path.join(directory, "move")
            os.makedirs(move_dir)
            elements = query_elements(batch_fields)
            config = {
                "local": {"aet": "BATCH_SCU", "port": free_port()},
                "pacs": {"hostname": "127.0.0.1", "port": 0, "aet": "ARCHIVE"},
                "request": {
                    "type": "c-move",
                    "model": "study",
                    "elements": elements,
                    "elements_batch_file": batch_path,
                    "throttle_time": 0,
                    "threads": 1,
                },
                "output": {"directory": move_dir, "database_file": "database.csv"},
            }

            def handle_move(event):
                yield (None, None)

            archive = Archive(
                [StudyRootQueryRetrieveInformationModelMove],
                [(evt.EVT_C_MOVE, handle_move)],
            )
            config["pacs"]["port"] = archive.port
            try:
                requests = scu.create_requests(config)
                self.assertEqual(1, len(requests))
                tool = scu.SCU(config)
                tool.association = tool.establish_association()
                if not tool.association.is_established:
                    raise AssertionError("C-MOVE association was not established")
                try:
                    tool.send_move(requests[0])
                finally:
                    if tool.association.is_established:
                        tool.association.release()
            finally:
                archive.stop()

            rows = read_rows(os.path.join(move_dir, "database.csv"))
            retry_path = os.path.join(move_dir, "retry.csv")
            argv = sys.argv
            sys.argv = [
                "retry_failed.py",
                os.path.join(move_dir, "requests.failed"),
                "-o",
                retry_path,
            ]
            try:
                retry_failed.main()
            finally:
                sys.argv = argv
            retried = read_rows(retry_path)

        with self.subTest(export="c-move"):
            self.assertEqual(1, len(rows))
            assert_export_row(self, rows[0], batch_fields)
            self.assertNotIn(rows[0]["Status"][:1], FORMULA_TRIGGERS)
        with self.subTest(export="retry"):
            self.assertEqual(1, len(retried))
            assert_export_row(
                self,
                retried[0],
                {
                    "PatientID": batch_fields["PatientID"],
                    "AccessionNumber": batch_fields["AccessionNumber"],
                    "StudyInstanceUID": batch_fields["StudyInstanceUID"],
                },
            )


if __name__ == "__main__":
    unittest.main()
