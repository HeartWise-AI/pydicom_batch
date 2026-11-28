"""Patient identifiers and images must not cross the network in cleartext.

The operator config names an archive. C-FIND, C-MOVE, the connectivity check,
and the storage listener then have to authenticate that peer with TLS and
keep every patient identifier and stored object inside TLS. A peer that does
not negotiate TLS, or whose certificate is not signed by the configured CA,
must not receive a query identifier and must not supply the export.

`common.security` in the batch config supplies `ca_file` (trust anchor for the
archive certificate), plus `cert_file` and `key_file` (the certificate the
storage listener presents). The certificate has to be valid for the configured
archive hostname. The batch runner has to pass those values through to the
connectivity check, the C-FIND/C-MOVE association, and the storage listener.

These tests fail while those associations are plain DICOM.
"""

import os
import select
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest

import pydicom.uid as uid

# pydicom 3.0.2 removed this name. scp.py still imports it while loading.
if not hasattr(uid, "PILSupportedCompressedPixelTransferSyntaxes"):
    uid.PILSupportedCompressedPixelTransferSyntaxes = []

import yaml
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

from common import pydicombatch
from scp import SCP
from scu import preflight_check

QUERY_ID = "QRYPATIENT000001"
RESPONSE_ID = "RSPPATIENT000001"
EVIL_ID = "EVILPATIENT00001"
IMAGE_ID = "IMG0000000000001"
EVIL_IMAGE_ID = "EVL0000000000001"
ATTACKER_ID = "ATKPATIENT0000001"
STUDY_UID = "1.2.826.0.1.3680043.10.543.1"
SERIES_UID = "1.2.826.0.1.3680043.10.543.3"
INSTANCE_UID = "1.2.826.0.1.3680043.10.543.9"
HOST = "127.0.0.1"


def free_port():
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def write_certs(directory):
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-nodes",
            "-subj", "/CN=Synthetic CA",
        ],
        cwd=directory, check=True, capture_output=True,
    )
    ext = os.path.join(directory, "ext.cnf")
    with open(ext, "w") as handle:
        handle.write(
            "subjectAltName=IP:127.0.0.1\n"
            "extendedKeyUsage=serverAuth\n"
            "basicConstraints=CA:FALSE\n"
        )
    subprocess.run(
        [
            "openssl", "req", "-newkey", "rsa:2048",
            "-keyout", "server.key", "-out", "server.csr", "-nodes",
            "-subj", "/CN=127.0.0.1",
        ],
        cwd=directory, check=True, capture_output=True,
    )
    subprocess.run(
        [
            "openssl", "x509", "-req", "-in", "server.csr",
            "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "server.pem", "-days", "2", "-extfile", "ext.cnf",
        ],
        cwd=directory, check=True, capture_output=True,
    )
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", "bad.key", "-out", "bad.pem", "-days", "2", "-nodes",
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
            "-addext", "extendedKeyUsage=serverAuth",
        ],
        cwd=directory, check=True, capture_output=True,
    )
    return {
        "ca": os.path.join(directory, "ca.pem"),
        "cert": os.path.join(directory, "server.pem"),
        "key": os.path.join(directory, "server.key"),
        "bad_cert": os.path.join(directory, "bad.pem"),
        "bad_key": os.path.join(directory, "bad.key"),
    }


def server_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


def client_context(ca_file):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(ca_file)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class ByteProxy:
    """Forward a TCP connection and record each direction.

    `replace_from_client` / `replace_from_server` rewrite equal-length values
    on the way through, which is what an on-path attacker can do to plain DICOM.
    """

    def __init__(self, target_host, target_port, replace_from_client=None, replace_from_server=None):
        self.target = (target_host, target_port)
        self.replace_from_client = replace_from_client
        self.replace_from_server = replace_from_server
        self.client_bytes = bytearray()
        self.server_bytes = bytearray()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((HOST, 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        self.sock.close()

    def _accept(self):
        while not self._stop:
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._pipe, args=(client,), daemon=True).start()

    def _pipe(self, client):
        try:
            remote = socket.create_connection(self.target)
        except OSError:
            client.close()
            return
        client.setblocking(False)
        remote.setblocking(False)
        holds = {id(client): b"", id(remote): b""}
        idle = 0
        while True:
            try:
                ready, _, _ = select.select([client, remote], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not ready:
                idle += 1
                self._flush(client, remote, holds)
                if idle > 40:
                    break
                continue
            idle = 0
            if self._transfer(client, remote, holds, ready):
                break
        self._flush(client, remote, holds)
        for sock in (client, remote):
            try:
                sock.close()
            except OSError:
                pass

    def _transfer(self, client, remote, holds, ready):
        for source in ready:
            other = remote if source is client else client
            try:
                data = source.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                return True
            if not data:
                return True
            if source is client:
                self.client_bytes += data
            else:
                self.server_bytes += data
            holds[id(source)] += data
            outgoing, holds[id(source)] = self._split(holds[id(source)], source, client)
            if outgoing:
                other.sendall(self._rewrite(outgoing, source, client))
        return False

    def _flush(self, client, remote, holds):
        for source, other in ((client, remote), (remote, client)):
            pending = holds[id(source)]
            if not pending:
                continue
            other.sendall(self._rewrite(pending, source, client))
            holds[id(source)] = b""

    def _split(self, buf, source, client):
        needle = self._needle(source, client)
        if not needle:
            return buf, b""
        hold = 0
        limit = len(needle) - 1
        for size in range(1, min(len(buf), limit) + 1):
            if needle.startswith(buf[-size:]):
                hold = size
        if hold:
            return buf[:-hold], buf[-hold:]
        return buf, b""

    def _needle(self, source, client):
        pair = self.replace_from_client if source is client else self.replace_from_server
        if not pair:
            return b""
        return pair[0]

    def _rewrite(self, data, source, client):
        pair = self.replace_from_client if source is client else self.replace_from_server
        if not pair:
            return data
        old, new = pair
        return data.replace(old, new)


class TlsOrClearGate:
    """Present `server_ctx` when the peer starts TLS. Otherwise bridge cleartext."""

    def __init__(self, backend_port, server_ctx):
        self.backend_port = backend_port
        self.server_ctx = server_ctx
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((HOST, 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._stop = True
        self.sock.close()

    def _accept(self):
        while not self._stop:
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        client.settimeout(3)
        try:
            first = client.recv(1, socket.MSG_PEEK)
        except OSError:
            client.close()
            return
        if first == b"\x16":
            try:
                client = self.server_ctx.wrap_socket(client, server_side=True)
            except ssl.SSLError:
                try:
                    client.close()
                except OSError:
                    pass
                return
        try:
            remote = socket.create_connection((HOST, self.backend_port))
        except OSError:
            client.close()
            return
        client.setblocking(False)
        remote.setblocking(False)
        sockets = [client, remote]
        idle = 0
        while idle < 40:
            try:
                ready, _, _ = select.select(sockets, [], [], 0.05)
            except (OSError, ValueError):
                break
            if not ready:
                idle += 1
                continue
            idle = 0
            dead = False
            for source in ready:
                other = remote if source is client else client
                try:
                    data = source.recv(65536)
                except BlockingIOError:
                    continue
                except OSError:
                    dead = True
                    break
                if not data:
                    dead = True
                    break
                other.sendall(data)
            if dead:
                break
        for sock in (client, remote):
            try:
                sock.close()
            except OSError:
                pass


class TreeWatch:
    def __init__(self, root):
        self.root = root
        self._blobs = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        self._thread.join(timeout=1)
        self._capture()

    def _run(self):
        while not self._stop:
            self._capture()
            time.sleep(0.02)

    def _capture(self):
        for dirpath, _, filenames in os.walk(self.root):
            self._blobs.append(dirpath.encode())
            for name in filenames:
                path = os.path.join(dirpath, name)
                self._blobs.append(path.encode())
                try:
                    with open(path, "rb") as handle:
                        self._blobs.append(handle.read())
                except OSError:
                    pass

    def contains(self, text):
        self._capture()
        needle = text.encode()
        return any(needle in blob for blob in self._blobs)


def find_handler(patient_id):
    def handle(event):
        identifier = Dataset()
        identifier.PatientID = patient_id
        identifier.QueryRetrieveLevel = "STUDY"
        identifier.StudyInstanceUID = STUDY_UID
        yield (0xFF00, identifier)
        yield (0x0000, None)

    return handle


def image_dataset(patient_id):
    dataset = Dataset()
    dataset.file_meta = FileMetaDataset()
    dataset.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset.file_meta.MediaStorageSOPClassUID = CTImageStorage
    dataset.file_meta.MediaStorageSOPInstanceUID = INSTANCE_UID
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = INSTANCE_UID
    dataset.PatientID = patient_id
    dataset.PatientName = "Synthetic"
    dataset.StudyInstanceUID = STUDY_UID
    dataset.SeriesInstanceUID = SERIES_UID
    dataset.Rows = 1
    dataset.Columns = 1
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.PixelData = b"\x7f\x7f"
    return dataset


def move_handler(destination, patient_id, tls_args=None):
    def handle(event):
        if tls_args is None:
            yield (destination[0], destination[1])
        else:
            yield (destination[0], destination[1], {"tls_args": tls_args})
        yield 1
        yield (0xFF00, image_dataset(patient_id))

    return handle


def start_archive(contexts, handler, requested=(), ssl_ctx=None):
    ae = AE(ae_title="ARCHIVE")
    for context in contexts:
        ae.add_supported_context(context)
    for context in requested:
        ae.add_requested_context(context, ExplicitVRLittleEndian)
    server = ae.start_server(
        (HOST, 0),
        block=False,
        ssl_context=ssl_ctx,
        evt_handlers=[handler],
    )
    for _ in range(50):
        if getattr(server, "socket", None) is not None:
            break
        time.sleep(0.02)
    return server


def batch_config(directory, pacs_port, storage_port, operation, security=None):
    config = {
        "common": {
            "pacs": {"hostname": HOST, "port": pacs_port, "aet": "ARCHIVE"},
            "local": {"port": storage_port, "aet": "BATCH_SCU"},
            "schedule": {
                "enabled": False,
                "start_time": "00:00",
                "end_time": "23:59",
                "timezone": "UTC",
            },
        },
        operation: {
            "threads": 1,
            "throttle_time": 0,
            "model": "study",
            "elements": [
                f"PatientID={QUERY_ID}",
                "QueryRetrieveLevel=STUDY",
                f"StudyInstanceUID={STUDY_UID}",
            ],
            "output": {
                "directory": os.path.join(directory, "out"),
                "database_file": "database.csv",
                "directory_structure": "PatientID/StudyInstanceUID/SeriesInstanceUID",
                "filename": "SOPInstanceUID",
                "decompress": False,
            },
        },
    }
    if operation == "c-move":
        config[operation]["anonymization"] = {"enabled": False}
    if security:
        config["common"]["security"] = security
    path = os.path.join(directory, "dicom.yml")
    with open(path, "w") as handle:
        yaml.safe_dump(config, handle)
    return path


def merged_preflight(pacs_port, security=None):
    config = {
        "pacs": {"hostname": HOST, "port": pacs_port, "aet": "ARCHIVE"},
        "local": {"aet": "BATCH_SCU", "port": free_port()},
        "request": {"type": "c-find"},
        "output": {"directory": tempfile.mkdtemp(), "database_file": "database.csv"},
    }
    if security:
        config["security"] = security
    return config


class CleartextAssociationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certdir = tempfile.mkdtemp()
        cls.certs = write_certs(cls.certdir)

    def security(self):
        return {
            "ca_file": self.certs["ca"],
            "cert_file": self.certs["cert"],
            "key_file": self.certs["key"],
        }

    def test_cfind_cleartext_peer_cannot_supply_the_export(self):
        directory = tempfile.mkdtemp()
        archive = start_archive(
            [StudyRootQueryRetrieveInformationModelFind, Verification],
            (evt.EVT_C_FIND, find_handler(RESPONSE_ID)),
        )
        proxy = ByteProxy(
            HOST,
            archive.socket.getsockname()[1],
            replace_from_server=(RESPONSE_ID.encode(), EVIL_ID.encode()),
        )
        watch = TreeWatch(os.path.join(directory, "out"))
        try:
            pydicombatch(
                batch_config(directory, proxy.port, free_port(), "c-find"),
                "c-find",
            )
            self.assertNotIn(QUERY_ID.encode(), proxy.client_bytes)
            self.assertNotIn(RESPONSE_ID.encode(), proxy.server_bytes)
            self.assertFalse(watch.contains(RESPONSE_ID))
            self.assertFalse(watch.contains(EVIL_ID))
        finally:
            watch.stop()
            proxy.close()
            archive.shutdown()

    def test_cfind_untrusted_certificate_cannot_supply_the_export(self):
        directory = tempfile.mkdtemp()
        archive = start_archive(
            [StudyRootQueryRetrieveInformationModelFind, Verification],
            (evt.EVT_C_FIND, find_handler(ATTACKER_ID)),
        )
        gate = TlsOrClearGate(
            archive.socket.getsockname()[1],
            server_context(self.certs["bad_cert"], self.certs["bad_key"]),
        )
        proxy = ByteProxy(HOST, gate.port)
        watch = TreeWatch(os.path.join(directory, "out"))
        try:
            pydicombatch(
                batch_config(directory, proxy.port, free_port(), "c-find", self.security()),
                "c-find",
            )
            self.assertNotIn(QUERY_ID.encode(), proxy.client_bytes)
            self.assertFalse(watch.contains(ATTACKER_ID))
        finally:
            watch.stop()
            proxy.close()
            gate.close()
            archive.shutdown()

    def test_cfind_trusted_tls_exports_without_cleartext_identifiers(self):
        directory = tempfile.mkdtemp()
        archive = start_archive(
            [StudyRootQueryRetrieveInformationModelFind, Verification],
            (evt.EVT_C_FIND, find_handler(RESPONSE_ID)),
            ssl_ctx=server_context(self.certs["cert"], self.certs["key"]),
        )
        proxy = ByteProxy(HOST, archive.socket.getsockname()[1])
        try:
            pydicombatch(
                batch_config(directory, proxy.port, free_port(), "c-find", self.security()),
                "c-find",
            )
            database = os.path.join(directory, "out", "database.csv")
            self.assertTrue(os.path.isfile(database))
            with open(database) as handle:
                exported = handle.read()
            self.assertIn(RESPONSE_ID, exported)
            self.assertNotIn(QUERY_ID.encode(), proxy.client_bytes)
            self.assertNotIn(RESPONSE_ID.encode(), proxy.server_bytes)
            self.assertTrue(proxy.client_bytes)
            self.assertEqual(proxy.client_bytes[0], 0x16)
        finally:
            proxy.close()
            archive.shutdown()

    def test_preflight_refuses_cleartext_peer(self):
        archive = start_archive([Verification], (evt.EVT_C_ECHO, lambda event: 0x0000))
        try:
            success, _message = preflight_check(
                merged_preflight(archive.socket.getsockname()[1], self.security())
            )
            self.assertFalse(success)
        finally:
            archive.shutdown()

    def test_preflight_accepts_archive_certificate(self):
        archive = start_archive(
            [Verification],
            (evt.EVT_C_ECHO, lambda event: 0x0000),
            ssl_ctx=server_context(self.certs["cert"], self.certs["key"]),
        )
        proxy = ByteProxy(HOST, archive.socket.getsockname()[1])
        try:
            success, _message = preflight_check(
                merged_preflight(proxy.port, self.security())
            )
            self.assertTrue(success)
            self.assertTrue(proxy.client_bytes)
            self.assertEqual(proxy.client_bytes[0], 0x16)
        finally:
            proxy.close()
            archive.shutdown()

    def test_cmove_cleartext_image_is_not_written(self):
        directory = tempfile.mkdtemp()
        storage_port = free_port()
        store_proxy_box = {}

        def handle(event):
            proxy = store_proxy_box["proxy"]
            yield (HOST, proxy.port)
            yield 1
            yield (0xFF00, image_dataset(IMAGE_ID))

        archive = start_archive(
            [StudyRootQueryRetrieveInformationModelMove, Verification],
            (evt.EVT_C_MOVE, handle),
            requested=[CTImageStorage],
        )
        store_proxy = ByteProxy(
            HOST,
            storage_port,
            replace_from_client=(IMAGE_ID.encode(), EVIL_IMAGE_ID.encode()),
        )
        store_proxy_box["proxy"] = store_proxy
        query_proxy = ByteProxy(HOST, archive.socket.getsockname()[1])
        watch = TreeWatch(os.path.join(directory, "out"))
        try:
            pydicombatch(
                batch_config(directory, query_proxy.port, storage_port, "c-move"),
                "c-move",
            )
            time.sleep(0.3)
            self.assertNotIn(QUERY_ID.encode(), query_proxy.client_bytes)
            self.assertFalse(watch.contains(IMAGE_ID))
            self.assertFalse(watch.contains(EVIL_IMAGE_ID))
        finally:
            watch.stop()
            query_proxy.close()
            store_proxy.close()
            archive.shutdown()

    def test_cmove_trusted_tls_stores_image_without_cleartext(self):
        directory = tempfile.mkdtemp()
        storage_port = free_port()
        store_proxy = ByteProxy(HOST, storage_port)
        tls_args = (client_context(self.certs["ca"]), HOST)

        archive = start_archive(
            [StudyRootQueryRetrieveInformationModelMove, Verification],
            (evt.EVT_C_MOVE, move_handler((HOST, store_proxy.port), IMAGE_ID, tls_args)),
            requested=[CTImageStorage],
            ssl_ctx=server_context(self.certs["cert"], self.certs["key"]),
        )
        query_proxy = ByteProxy(HOST, archive.socket.getsockname()[1])
        try:
            pydicombatch(
                batch_config(
                    directory, query_proxy.port, storage_port, "c-move", self.security()
                ),
                "c-move",
            )
            time.sleep(0.3)
            self.assertTrue(query_proxy.client_bytes)
            self.assertEqual(query_proxy.client_bytes[0], 0x16)
            self.assertNotIn(QUERY_ID.encode(), query_proxy.client_bytes)
            self.assertNotIn(IMAGE_ID.encode(), query_proxy.client_bytes)
            self.assertTrue(store_proxy.client_bytes)
            self.assertEqual(store_proxy.client_bytes[0], 0x16)
            self.assertNotIn(IMAGE_ID.encode(), store_proxy.client_bytes)
            self.assertNotIn(IMAGE_ID.encode(), store_proxy.server_bytes)
            written = TreeWatch(os.path.join(directory, "out"))
            written.stop()
            self.assertTrue(written.contains(IMAGE_ID))
        finally:
            query_proxy.close()
            store_proxy.close()
            archive.shutdown()

    def test_storage_listener_rejects_cleartext_cstore(self):
        directory = tempfile.mkdtemp()
        port = free_port()
        config = {
            "pacs": {"hostname": HOST, "port": 1, "aet": "ARCHIVE"},
            "local": {"aet": "BATCH_SCU", "port": port},
            "request": {"type": "c-move", "threads": 1},
            "output": {
                "directory": directory,
                "directory_structure": "PatientID/StudyInstanceUID/SeriesInstanceUID",
                "filename": "SOPInstanceUID",
                "decompress": False,
            },
            "anonymization": {"enabled": False},
            "security": self.security(),
        }
        listener = SCP(config)
        listener.start_server()
        watch = TreeWatch(directory)
        requestor = AE(ae_title="ARCHIVE")
        requestor.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)
        try:
            association = requestor.associate(HOST, port, ae_title="BATCH_SCU")
            if association.is_established:
                association.send_c_store(image_dataset(IMAGE_ID))
                association.release()
            time.sleep(0.3)
            self.assertFalse(watch.contains(IMAGE_ID))
        finally:
            watch.stop()
            listener.scp.shutdown()


if __name__ == "__main__":
    unittest.main()
