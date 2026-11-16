"""Snapshot download for the Snowflake load (LOD-02, D-11, H3).

The source CSV is a live file replaced monthly, so the Snowflake copy is downloaded once, hashed
(sha256 of the bytes as downloaded), gzipped deterministically and staged. The sha256 identifies
the snapshot on every record. ClickHouse reads the same URL through `url()`; D-11 asks for the two
loads to happen within the same hour (`within_load_window`), and parity proves they match.

The download function is injectable, so tests use a small CSV fixture and never touch the network.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import re
import shutil
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentic_analytics.load._common import Clock, LoaderError, utc_now
from agentic_analytics.packs import Pack, read_pack_text

Downloader = Callable[[str, Path], None]
CHUNK = 1 << 20
LOAD_WINDOW = dt.timedelta(hours=1)
_URL_CALL = re.compile(r"\burl\(\s*'([^']+)'", re.I)


@dataclass(frozen=True)
class SnapshotInfo:
    url: str
    csv_path: Path
    gz_path: Path
    sha256: str
    size_bytes: int
    lines: int
    downloaded_at: dt.datetime

    @property
    def stage_name(self) -> str:
        return self.gz_path.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "file": self.csv_path.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "lines": self.lines,
            "downloaded_at": self.downloaded_at.isoformat(),
        }


def source_csv_url(pack: Pack) -> str:
    """The CSV URL that the pack's ClickHouse load reads with `url()`, so both engines load the
    same source (D-11). Exactly one `url('…')` call must be present."""
    sql = read_pack_text(pack, "load/clickhouse.sql")
    urls = _URL_CALL.findall(sql)
    if len(urls) != 1:
        raise LoaderError(f"load/clickhouse.sql must read exactly one url(), found {len(urls)}")
    return urls[0]


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LoaderError(f"snapshot URL must be http(s) with a host: {url!r}")


def _urlopen(req: urllib.request.Request) -> Any:  # pragma: no cover - network, live run only
    """The one network call of this module, so the offline test guard can refuse it whatever
    imported `http_download` or `content_length` (tests/conftest.py, L-6)."""
    return urllib.request.urlopen(req, timeout=60)


def http_download(url: str, dest: Path) -> None:  # pragma: no cover - network, live run only
    """Stream `url` to `dest` (urllib follows redirects)."""
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "agentic-analytics-loader"})
    with _urlopen(req) as resp, dest.open(mode="wb") as out:
        shutil.copyfileobj(resp, out, CHUNK)


def content_length(url: str) -> int | None:  # pragma: no cover - network, live run only
    """The source's size from its response headers, or None when the server does not say (for
    the disk-space check before a download)."""
    _check_url(url)
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "agentic-analytics-loader"}
    )
    with _urlopen(req) as resp:
        value = resp.headers.get("Content-Length")
    return int(value) if value and value.isdigit() else None


def hash_and_count(path: Path) -> tuple[str, int, int]:
    """sha256 hex, size in bytes and newline-terminated line count, in one streaming pass."""
    h = hashlib.sha256()
    size = lines = 0
    last = b""
    with path.open(mode="rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if size and last != b"\n":
        lines += 1  # a final record without a trailing newline
    return h.hexdigest(), size, lines


def gzip_file(src: Path, dst: Path) -> None:
    """Gzip deterministically (no name, mtime 0), so the same CSV always stages the same bytes."""
    with (
        src.open(mode="rb") as fin,
        dst.open(mode="wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
    ):
        shutil.copyfileobj(fin, gz, CHUNK)


def download_snapshot(
    url: str,
    workdir: Path,
    *,
    download: Downloader = http_download,
    now: Clock = utc_now,
) -> SnapshotInfo:
    """Download `url` into `workdir`, hash it and write a gzipped copy next to it."""
    _check_url(url)
    name = Path(urlsplit(url).path).name or "snapshot.csv"
    workdir.mkdir(parents=True, exist_ok=True)
    csv_path = workdir / name
    gz_path = workdir / f"{name}.gz"
    downloaded_at = now()
    download(url, csv_path)
    if not csv_path.is_file():
        raise LoaderError(f"download of {url} produced no file")
    sha, size, lines = hash_and_count(csv_path)
    if size == 0:
        raise LoaderError(f"download of {url} is empty")
    gzip_file(csv_path, gz_path)
    return SnapshotInfo(url, csv_path, gz_path, sha, size, lines, downloaded_at)


def write_snapshot_record(info: SnapshotInfo | Mapping[str, Any], out_dir: Path) -> Path:
    """`docs/measurements/snapshot-<date>.json` with the URL, sha256, size and time. Takes the
    `SnapshotInfo` or its `to_dict()` (as a `LoadResult.snapshot` carries it)."""
    record = info.to_dict() if isinstance(info, SnapshotInfo) else dict(info)
    day = dt.datetime.fromisoformat(str(record["downloaded_at"])).date()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"snapshot-{day.isoformat()}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def within_load_window(
    clickhouse_loaded_at: dt.datetime,
    snapshot_downloaded_at: dt.datetime,
    window: dt.timedelta = LOAD_WINDOW,
) -> bool:
    """D-11: the Snowflake copy is downloaded within an hour of the ClickHouse `url()` load."""
    for t in (clickhouse_loaded_at, snapshot_downloaded_at):
        if t.tzinfo is None or t.utcoffset() is None:
            raise ValueError("load times must be timezone-aware")
    return abs(clickhouse_loaded_at - snapshot_downloaded_at) <= window


__all__ = [
    "LOAD_WINDOW",
    "Downloader",
    "SnapshotInfo",
    "content_length",
    "download_snapshot",
    "gzip_file",
    "hash_and_count",
    "http_download",
    "source_csv_url",
    "within_load_window",
    "write_snapshot_record",
]
