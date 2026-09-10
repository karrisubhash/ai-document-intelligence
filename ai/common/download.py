import hashlib
import sys
import tarfile
import urllib.request
from pathlib import Path

USER_AGENT = "ai-document-intelligence/0.1"


def download(url: str, destination: Path, *, force: bool = False) -> Path:
    """Stream a URL to disk, skipping the work if it is already there."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and not force:
        print(f"  cached: {destination} ({destination.stat().st_size / 1e6:.1f} MB)")
        return destination

    partial = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(request) as response:
        total = int(response.headers.get("Content-Length") or 0)
        written = 0

        with partial.open("wb") as handle:
            while chunk := response.read(1 << 20):
                handle.write(chunk)
                written += len(chunk)
                if total:
                    percent = 100 * written / total
                    print(
                        f"\r  {destination.name}: {written / 1e6:.1f}/"
                        f"{total / 1e6:.1f} MB ({percent:.0f}%)",
                        end="",
                        file=sys.stderr,
                    )
        print("", file=sys.stderr)

    partial.replace(destination)
    return destination


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def extract_tar(archive: Path, destination: Path, *, marker: str | None = None) -> Path:
    """Extract a tarball once. `marker` is a path that, if present,
    means extraction already happened."""
    destination.mkdir(parents=True, exist_ok=True)

    if marker and (destination / marker).exists():
        print(f"  already extracted: {destination / marker}")
        return destination

    print(f"  extracting {archive.name} -> {destination}")
    with tarfile.open(archive) as tar:
        # filter="data" refuses absolute paths and traversal entries.
        tar.extractall(destination, filter="data")

    return destination
