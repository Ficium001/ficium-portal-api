"""Portal DB TLS: verify-full when Supabase's CA is supplied, require (with a warning) otherwise."""
import os

import pytest

from app.core.db import ssl_connect_args

PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----"


def test_without_ca_is_encrypted_only():
    assert ssl_connect_args("") == {"sslmode": "require"}


def test_pem_text_is_written_to_a_private_file_and_verified():
    args = ssl_connect_args(PEM)
    assert args["sslmode"] == "verify-full"
    path = args["sslrootcert"]
    try:
        assert open(path).read().strip() == PEM
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    finally:
        os.remove(path)


def test_path_is_used_as_is(tmp_path):
    f = tmp_path / "ca.crt"
    f.write_text(PEM)
    assert ssl_connect_args(str(f)) == {"sslmode": "verify-full", "sslrootcert": str(f)}


def test_mode_override(tmp_path):
    f = tmp_path / "ca.crt"
    f.write_text(PEM)
    assert ssl_connect_args(str(f), "verify-ca")["sslmode"] == "verify-ca"


@pytest.mark.parametrize("cert,mode", [("", "verify-full"), ("", "disable"), ("/no/such/file", "")])
def test_refuses_unsafe_or_broken_settings(cert, mode):
    with pytest.raises(ValueError):
        ssl_connect_args(cert, mode)
