"""TLS for every DICOM association this tool opens or accepts.

Associations carry patient identifiers and images, so there is no cleartext
mode. `common.security` in the batch config must name:

- `ca_file`: trust anchor for the archive certificate. The certificate must be
  valid for `common.pacs.hostname`.
- `cert_file` / `key_file`: certificate and key the C-MOVE storage listener
  presents to the archive.
"""

import ssl


def _security_file(config, key):
    value = (config.get("security") or {}).get(key)
    if not value:
        raise ValueError(
            f"TLS is required: set common.security.{key} in the config file"
        )
    return value


def client_tls_args(config):
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cafile=_security_file(config, "ca_file")
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context, config["pacs"]["hostname"]


def server_ssl_context(config):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        _security_file(config, "cert_file"), _security_file(config, "key_file")
    )
    return context


def validate_security(config, operation):
    client_tls_args(config)
    if operation == "c-move":
        server_ssl_context(config)
