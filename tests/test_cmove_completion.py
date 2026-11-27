"""A C-MOVE counts as completed only when the SCP returns final Success and moved at least one image;
a Success with no completed sub-operations is recorded as failed, so it is retried."""

import os
import tempfile
import threading
import unittest

from pydicom.dataset import Dataset

import scu


def status(code, completed=None):
    s = Dataset()
    s.Status = code
    if completed is not None:
        s.NumberOfCompletedSuboperations = completed
    return s


class FakeAssociation:
    is_established = True

    def __init__(self, responses):
        self.responses = responses

    def send_c_move(self, identifier, aet, model):
        return iter(self.responses)


class TestMoveCompletion(unittest.TestCase):

    def run_move(self, responses):
        tmp = tempfile.mkdtemp()
        client = object.__new__(scu.SCU)
        client.association = FakeAssociation(responses)
        client.config = {"local": {"aet": "LOCAL"}, "output": {"directory": tmp, "database_file": "db.csv"}}
        client.query_model = None
        client.pbar, client.pbar_lock = None, threading.Lock()
        request = {"elements": ["PatientID=P1", "StudyInstanceUID=1.2.3", "QueryRetrieveLevel=STUDY"]}
        client.send_move(request, max_retries=0)
        return (os.path.exists(os.path.join(tmp, "requests.completed")),
                os.path.exists(os.path.join(tmp, "requests.failed")))

    def test_success_with_images_is_completed(self):
        self.assertEqual(self.run_move([(status(0xFF00, 3), None), (status(0x0000, 5), None)]), (True, False))

    def test_count_reported_only_on_pending_messages_counts(self):
        self.assertEqual(self.run_move([(status(0xFF00, 2), None), (status(0x0000), None)]), (True, False))

    def test_success_with_zero_images_is_failed(self):
        self.assertEqual(self.run_move([(status(0x0000, 0), None)]), (False, True))
        self.assertEqual(self.run_move([(status(0x0000), None)]), (False, True))


if __name__ == "__main__":
    unittest.main()
