"""Minimal Kaggle API client over the stdlib.

The official `kaggle` package pulls a dependency tree into what is
otherwise a dependency-free orchestration layer, and all we need is
four endpoints. Auth is HTTP Basic with (username, key).

Credentials are read from, in order:
  1. KAGGLE_USERNAME / KAGGLE_KEY environment variables
  2. ~/.kaggle/kaggle.json   (the file the Kaggle UI hands you)
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

API = "https://www.kaggle.com/api/v1"

# Terminal kernel states. "complete" still needs its output checked -
# a kernel that raised is "complete" with an error in the log.
TERMINAL_STATES = {"complete", "error", "cancelAcknowledged", "cancelRequested"}


class KaggleError(RuntimeError):
    pass


class CredentialsMissing(KaggleError):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str
    key: str

    @property
    def header(self) -> str:
        token = base64.b64encode(f"{self.username}:{self.key}".encode()).decode()
        return f"Basic {token}"


def load_credentials(path: Path | None = None) -> Credentials:
    username, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
    if username and key:
        return Credentials(username, key)

    path = path or Path.home() / ".kaggle" / "kaggle.json"
    if not path.exists():
        raise CredentialsMissing(
            f"No Kaggle credentials. Set KAGGLE_USERNAME and KAGGLE_KEY, or place "
            f"kaggle.json at {path}. Get one from https://www.kaggle.com/settings "
            f"-> API -> Create New Token."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("username") or not data.get("key"):
        raise CredentialsMissing(f"{path} has no 'username'/'key' pair.")

    return Credentials(data["username"], data["key"])


def request(
    method: str,
    endpoint: str,
    credentials: Credentials,
    *,
    params: dict | None = None,
    payload: dict | None = None,
    raw: bool = False,
):
    url = f"{API}{endpoint}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", credentials.header)
    req.add_header("User-Agent", "ai-document-intelligence/0.1")
    if body:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        if exc.code in (401, 403):
            raise CredentialsMissing(
                f"Kaggle rejected the credentials ({exc.code}). "
                f"Check the token is current. {detail}"
            ) from exc
        raise KaggleError(f"{method} {endpoint} -> {exc.code}: {detail}") from exc

    return content if raw else json.loads(content or b"{}")


def push_kernel(metadata: dict, source: str, credentials: Credentials) -> dict:
    """Create or update a kernel and queue a run.

    `source` is the notebook/script body; Kaggle takes it inline, so no
    file upload dance is required.
    """
    payload = {
        "id": f"{credentials.username}/{metadata['id'].split('/')[-1]}",
        "title": metadata["title"],
        "codeFile": metadata.get("code_file", "main.py"),
        "language": metadata.get("language", "python"),
        "kernelType": metadata.get("kernel_type", "script"),
        "isPrivate": metadata.get("is_private", True),
        "enableGpu": metadata.get("enable_gpu", False),
        "enableInternet": metadata.get("enable_internet", True),
        "datasetDataSources": metadata.get("dataset_sources", []),
        "kernelDataSources": metadata.get("kernel_sources", []),
        "competitionDataSources": metadata.get("competition_sources", []),
        "text": source,
    }
    return request("POST", "/kernels/push", credentials, payload=payload)


def kernel_status(owner: str, slug: str, credentials: Credentials) -> dict:
    return request(
        "GET", "/kernels/status", credentials, params={"userName": owner, "kernelSlug": slug}
    )


def wait_for_kernel(
    owner: str,
    slug: str,
    credentials: Credentials,
    *,
    poll_seconds: int = 60,
    timeout_seconds: int = 12 * 3600,
    on_poll=None,
) -> dict:
    deadline = time.monotonic() + timeout_seconds

    while True:
        status = kernel_status(owner, slug, credentials)
        state = (status.get("status") or "").strip()

        if on_poll:
            on_poll(state, status)

        if state in TERMINAL_STATES:
            return status

        if time.monotonic() > deadline:
            raise KaggleError(
                f"kernel {owner}/{slug} still {state!r} after "
                f"{timeout_seconds}s; check https://www.kaggle.com/code/{owner}/{slug}"
            )

        time.sleep(poll_seconds)


def download_output(owner: str, slug: str, credentials: Credentials, destination: Path) -> list:
    """Fetch a finished kernel's output files."""
    destination.mkdir(parents=True, exist_ok=True)
    content = request(
        "GET",
        "/kernels/output/download",
        credentials,
        params={"userName": owner, "kernelSlug": slug},
        raw=True,
    )

    archive = destination / f"{slug}-output.zip"
    archive.write_bytes(content)

    written = []
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
            written = bundle.namelist()
        archive.unlink()
    except zipfile.BadZipFile:
        # Some kernels return a single uncompressed file.
        written = [archive.name]

    return written
