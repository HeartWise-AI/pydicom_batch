"""The default configuration must not publish an archive endpoint.

These tests fail while the tracked default config, the sample anonymizer
script, or the README still contain a real archive address, application
entity title, site, or project. They pass once those values are placeholders
and the operator config filename is gitignored.
"""

import ipaddress
import logging
import re
import socket
import subprocess
import sys
from pathlib import Path

import pydicom.uid as uid

# pydicom 3.0.2 (requirements.txt) removed this name. scp.py still imports it
# while loading common.py, which is before the association code runs.
if not hasattr(uid, "PILSupportedCompressedPixelTransferSyntaxes"):
    uid.PILSupportedCompressedPixelTransferSyntaxes = []

import pytest
import yaml
from pynetdicom import AE, evt
from pynetdicom.pdu import A_ASSOCIATE_RQ
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
)

from common import merge_configs
from main import parse_arguments
from scp import SCP
from scu import SCU, create_dataset, create_requests, preflight_check

ROOT = Path(__file__).resolve().parents[1]

_PLACEHOLDER_AET = re.compile(
    r"(?i)^(?:pacs_aet|my_aet|your_aet|local_aet|example_aet|changeme|placeholder|example)$"
)
_PLACEHOLDER_LABEL = re.compile(
    r"(?i)^(?:project|site|sitename|trial|example|placeholder|changeme|"
    r"your[-_ ]?site|your[-_ ]?project|your[-_ ]?trial)$"
)
_IPV4 = re.compile(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SCRIPT_PARAM = re.compile(r'<p t="(PROJECTNAME|SITENAME|TRIALNAME)">([^<]*)</p>')
_DOC_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("2001:db8::/32"),
)
_UNIVERSAL = {"", "*"}
_IDENTIFIER_KEYS = ("PatientID", "PatientName", "AccessionNumber", "StudyInstanceUID")


def is_placeholder_host(host):
    value = str(host).strip().strip("[]").lower().rstrip(".")
    if value in {"localhost", "example", "pacs.example", "pacs.example.com"}:
        return True
    if value.endswith((".example", ".example.com", ".invalid", ".test", ".localhost")):
        return True
    if any(token in value for token in ("placeholder", "changeme", "your_pacs", "your-pacs", "example")):
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified:
        return True
    return any(address in network for network in _DOC_NETWORKS)


def is_placeholder_aet(title):
    return _PLACEHOLDER_AET.fullmatch(str(title).strip()) is not None


def is_placeholder_label(value):
    return _PLACEHOLDER_LABEL.fullmatch(str(value).strip()) is not None


@pytest.fixture(autouse=True)
def _in_repo(monkeypatch):
    monkeypatch.chdir(ROOT)
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def default_config_path():
    argv = sys.argv
    sys.argv = ["main.py"]
    try:
        args = parse_arguments()
    finally:
        sys.argv = argv
    path = Path(args.config)
    if not path.is_file():
        raise AssertionError(f"default config {args.config} is not a shipped file")
    return path


def load_operation(operation, output_dir):
    with default_config_path().open() as handle:
        config = yaml.load(handle, Loader=yaml.FullLoader)
    merged = merge_configs(config, operation)
    merged["output"]["directory"] = str(output_dir)
    merged["output"]["database_file"] = "database.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    return merged


class _BlockedTCP:
    def __init__(self):
        self.connects = []
        self.sends = []
        self._orig_connect = socket.socket.connect
        self._orig_send = socket.socket.send
        self._orig_recv = socket.socket.recv

    def __enter__(self):
        blocker = self

        def connect(sock, address):
            if sock.type == socket.SOCK_STREAM:
                blocker.connects.append(address)
                return None
            return blocker._orig_connect(sock, address)

        def send(sock, data, *args, **kwargs):
            if sock.type == socket.SOCK_STREAM:
                payload = bytes(data)
                blocker.sends.append(payload)
                return len(payload)
            return blocker._orig_send(sock, data, *args, **kwargs)

        def recv(sock, bufsize, *args, **kwargs):
            if sock.type == socket.SOCK_STREAM:
                return b""
            return blocker._orig_recv(sock, bufsize, *args, **kwargs)

        socket.socket.connect = connect
        socket.socket.send = send
        socket.socket.recv = recv
        return self

    def __exit__(self, exc_type, exc, tb):
        socket.socket.connect = self._orig_connect
        socket.socket.send = self._orig_send
        socket.socket.recv = self._orig_recv


def _associate_pdu(payloads):
    for payload in payloads:
        if payload[:1] == b"\x01":
            pdu = A_ASSOCIATE_RQ()
            pdu.decode(payload)
            return pdu
    raise AssertionError("association did not encode an A-ASSOCIATE-RQ")


def _assert_endpoint(host, port, called, calling, config):
    if host != config["pacs"]["hostname"] or int(port) != int(config["pacs"]["port"]):
        raise AssertionError("association target is not the default config archive")
    if called != str(config["pacs"]["aet"]).strip():
        raise AssertionError("called AE title is not the default config archive title")
    if calling != str(config["local"]["aet"]).strip():
        raise AssertionError("calling AE title is not the default config local title")
    if not is_placeholder_host(host):
        raise AssertionError("default config archive host used for association is not a placeholder")
    if not is_placeholder_aet(called):
        raise AssertionError("default config called AE title used for association is not a placeholder")
    if not is_placeholder_aet(calling):
        raise AssertionError("default config calling AE title used for association is not a placeholder")


def _capture_scu_association(config):
    scu = SCU(config)
    scu.ae.acse_timeout = 1
    scu.ae.network_timeout = 1
    scu.ae.dimse_timeout = 1
    with _BlockedTCP() as blocked:
        association = scu.establish_association()
    try:
        pdu = _associate_pdu(blocked.sends)
        if not blocked.connects:
            raise AssertionError("association did not open a TCP connection")
        host, port = blocked.connects[0]
        _assert_endpoint(host, port, pdu.called_ae_title.strip(), pdu.calling_ae_title.strip(), config)
    finally:
        association.kill()


def test_default_cfind_association_uses_placeholders(tmp_path):
    _capture_scu_association(load_operation("c-find", tmp_path))


def test_default_cmove_association_uses_placeholders(tmp_path):
    _capture_scu_association(load_operation("c-move", tmp_path))


def test_preflight_echo_uses_placeholders(tmp_path):
    config = load_operation("c-find", tmp_path)
    with _BlockedTCP() as blocked:
        preflight_check(config)
    pdu = _associate_pdu(blocked.sends)
    if not blocked.connects:
        raise AssertionError("preflight did not open a TCP connection")
    host, port = blocked.connects[0]
    _assert_endpoint(host, port, pdu.called_ae_title.strip(), pdu.calling_ae_title.strip(), config)


def test_storage_scp_uses_placeholder_local_title(tmp_path):
    config = load_operation("c-move", tmp_path)
    scp = SCP(config)
    if not is_placeholder_aet(scp.ae.ae_title):
        raise AssertionError("storage SCP AE title from the default config is not a placeholder")


def _element_value(elements, keyword):
    for element in elements:
        key, sep, value = element.partition("=")
        if sep and key == keyword:
            return value
    return None


def _assert_universal_identifier(elements, dataset):
    for keyword in _IDENTIFIER_KEYS:
        configured = _element_value(elements, keyword)
        if configured is None:
            continue
        if configured not in _UNIVERSAL:
            raise AssertionError(f"default {keyword} query is not a universal match")
        if str(getattr(dataset, keyword)) != configured:
            raise AssertionError(f"request identifier {keyword} does not match the default config")


def _requests_for(config):
    requests = create_requests(config)
    if not requests:
        raise AssertionError(f"default {config['request']['type']} config produced no request")
    return requests


def test_default_cfind_query_is_the_configured_universal_match(tmp_path):
    config = load_operation("c-find", tmp_path)
    request = _requests_for(config)[0]
    received = []

    def handle_find(event):
        received.append(event.identifier)
        yield 0x0000, None

    server = AE(ae_title="PACS_AET")
    server.add_supported_context(StudyRootQueryRetrieveInformationModelFind)
    listener = server.start_server(
        ("127.0.0.1", 0),
        block=False,
        evt_handlers=[(evt.EVT_C_FIND, handle_find)],
    )
    try:
        scu = SCU(config)
        requestor = AE(ae_title="MY_AET")
        requestor.requested_contexts = scu.ae.requested_contexts
        association = requestor.associate(
            "127.0.0.1",
            listener.server_address[1],
            ae_title="PACS_AET",
        )
        if not association.is_established:
            raise AssertionError("local find peer did not accept the association")
        scu.association = association
        scu.send_find(request)
        association.release()
    finally:
        listener.shutdown()

    if len(received) != 1:
        raise AssertionError("default c-find did not send one identifier")
    _assert_universal_identifier(request["elements"], received[0])


def test_default_cmove_query_uses_placeholder_move_destination(tmp_path):
    config = load_operation("c-move", tmp_path)
    request = _requests_for(config)[0]
    received = []

    def handle_move(event):
        received.append((event.identifier, event.move_destination))
        yield 0x0000, None

    server = AE(ae_title="PACS_AET")
    server.add_supported_context(StudyRootQueryRetrieveInformationModelMove)
    listener = server.start_server(
        ("127.0.0.1", 0),
        block=False,
        evt_handlers=[(evt.EVT_C_MOVE, handle_move)],
    )
    try:
        scu = SCU(config)
        requestor = AE(ae_title="MY_AET")
        requestor.requested_contexts = scu.ae.requested_contexts
        association = requestor.associate(
            "127.0.0.1",
            listener.server_address[1],
            ae_title="PACS_AET",
        )
        if not association.is_established:
            raise AssertionError("local move peer did not accept the association")
        scu.association = association
        scu.send_move(request)
        association.release()
    finally:
        listener.shutdown()

    if len(received) != 1:
        raise AssertionError("default c-move did not send one identifier")
    identifier, move_destination = received[0]
    _assert_universal_identifier(request["elements"], identifier)
    built = create_dataset(request)
    if str(identifier.PatientID) != str(built.PatientID):
        raise AssertionError("c-move identifier does not match create_dataset")
    if move_destination != str(config["local"]["aet"]).strip():
        raise AssertionError("c-move destination is not the default config local title")
    if not is_placeholder_aet(move_destination):
        raise AssertionError("c-move destination from the default config is not a placeholder")


def test_tracked_files_do_not_publish_archive_addresses():
    listed = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    offenders = []
    for relative in listed.splitlines():
        path = ROOT / relative
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            continue
        for number, line in enumerate(data.splitlines(), 1):
            for match in _IPV4.findall(line):
                try:
                    ipaddress.ip_address(match.decode())
                except ValueError:
                    continue
                if not is_placeholder_host(match.decode()):
                    offenders.append(f"{relative}:{number}")
    if offenders:
        raise AssertionError(
            "tracked files publish a non-placeholder address: " + ", ".join(offenders)
        )


def test_anonymizer_scripts_use_placeholder_site_and_project():
    sample = "config/sample-dicom-anonymizer.script"
    listed = subprocess.check_output(["git", "ls-files", "*.script"], cwd=ROOT, text=True)
    paths = listed.splitlines()
    if sample not in paths:
        raise AssertionError(f"{sample} is not a tracked file")
    for relative in paths:
        text = (ROOT / relative).read_text(encoding="utf-8", errors="replace")
        found = _SCRIPT_PARAM.findall(text)
        if relative == sample and not found:
            raise AssertionError(f"{sample} has no project, site, or trial parameter")
        for name, value in found:
            if not is_placeholder_label(value):
                raise AssertionError(f"{relative} {name} is not a placeholder")


def test_operator_config_filename_is_gitignored():
    completed = subprocess.run(
        ["git", "check-ignore", "-q", "config/dicom.yml"],
        cwd=ROOT,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "config/dicom.yml is not gitignored; the operator config filename must be ignored"
        )
