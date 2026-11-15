"""Self-signed TLS for the phone page.

iOS only hands motion and compass data to pages loaded over HTTPS. With
--tls the server also listens on an HTTPS port using a certificate that is
generated here on first use (with the `openssl` command, so there is no
extra Python dependency) and kept in certs/, which git ignores.
"""

from __future__ import annotations

import re
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

CERT_NAME = "nudge.crt"
KEY_NAME = "nudge.key"


class TlsError(RuntimeError):
    pass


def local_addresses() -> list[str]:
    """IPv4 addresses this machine answers on."""
    found: list[str] = []

    def add(addr: str) -> None:
        if addr and addr not in found and not addr.startswith("127."):
            found.append(addr)

    if shutil.which("ip"):
        try:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                                 capture_output=True, text=True, timeout=5).stdout
            for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", out):
                add(match.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return found


def _find_openssl() -> str | None:
    exe = shutil.which("openssl")
    if exe:
        return exe
    if sys.platform == "win32":  # Git for Windows ships one
        for candidate in (r"C:\Program Files\Git\mingw64\bin\openssl.exe",
                          r"C:\Program Files\Git\usr\bin\openssl.exe"):
            if Path(candidate).is_file():
                return candidate
    return None


def ensure_certificate(cert_dir: Path) -> tuple[Path, Path]:
    """Return (certificate, private key), creating a self-signed pair if needed."""
    crt, key = cert_dir / CERT_NAME, cert_dir / KEY_NAME
    if crt.is_file() and key.is_file():
        return crt, key
    openssl = _find_openssl()
    if not openssl:
        raise TlsError("--tls needs the `openssl` command to create a certificate "
                       "(sudo apt install openssl), or supply --tls-cert and --tls-key")
    host = socket.gethostname()
    names = [f"DNS:{host}.local", f"DNS:{host}", "DNS:localhost", "IP:127.0.0.1"]
    names += [f"IP:{addr}" for addr in local_addresses()]
    cert_dir.mkdir(parents=True, exist_ok=True)
    cmd = [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "365",
           "-keyout", str(key), "-out", str(crt), "-subj", f"/CN={host}.local",
           "-addext", "subjectAltName=" + ",".join(names)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TlsError(f"could not run openssl: {exc}") from exc
    if result.returncode != 0 or not crt.is_file() or not key.is_file():
        raise TlsError(f"openssl failed: {result.stderr.strip()[-400:]}")
    try:
        key.chmod(0o600)
    except OSError:
        pass
    return crt, key


def server_context(crt: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(str(crt), str(key))
    except (OSError, ssl.SSLError) as exc:
        raise TlsError(f"cannot load TLS certificate {crt}: {exc}") from exc
    return ctx
