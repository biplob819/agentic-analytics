"""LOD-02 (part): snapshot download with sha256 and a deterministic gzip copy (D-11, H3)."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from agentic_analytics.load import LoaderError
from agentic_analytics.load.snapshot import (
    download_snapshot,
    hash_and_count,
    source_csv_url,
    within_load_window,
    write_snapshot_record,
)
from agentic_analytics.packs import load_pack
from agentic_analytics.packs.uk_price_paid.transforms import SOURCE_CSV_URL
from tests.load.fakes import FIXTURE, fixed_clock, fixture_download

DOC_URL = (
    "http://prod1.publicdata.landregistry.gov.uk.s3-website-eu-west-1.amazonaws.com/pp-complete.csv"
)


def test_snowflake_downloads_the_same_url_clickhouse_reads() -> None:
    assert source_csv_url(load_pack("uk_price_paid")) == DOC_URL == SOURCE_CSV_URL


def test_download_hashes_counts_and_gzips(tmp_path: Path) -> None:
    dl = fixture_download()
    info = download_snapshot(DOC_URL, tmp_path, download=dl, now=fixed_clock())
    raw = FIXTURE.read_bytes()
    assert dl.calls == [DOC_URL]  # type: ignore[attr-defined]
    assert info.sha256 == hashlib.sha256(raw).hexdigest()
    assert info.size_bytes == len(raw) and info.lines == 30
    assert info.csv_path.name == "pp-complete.csv"
    assert info.stage_name == "pp-complete.csv.gz"
    assert gzip.decompress(info.gz_path.read_bytes()) == raw
    assert info.downloaded_at == dt.datetime(2026, 11, 15, 9, 0, tzinfo=dt.UTC)
    assert info.to_dict()["sha256"] == info.sha256


def test_gzip_copy_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    a = download_snapshot(DOC_URL, tmp_path / "a", download=fixture_download())
    b = download_snapshot(DOC_URL, tmp_path / "b", download=fixture_download())
    assert a.gz_path.read_bytes() == b.gz_path.read_bytes()
    # Two copies made in the same second match even with a clock mtime (verifier mutant), so pin
    # the header: no FNAME flag and MTIME 0 (RFC 1952 bytes 3 and 4-7).
    header = a.gz_path.read_bytes()[:10]
    assert header[:2] == b"\x1f\x8b" and header[3] & 0x08 == 0 and header[4:8] == b"\0\0\0\0"


def test_line_count_without_a_trailing_newline(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_bytes(b"a\nb\nc")
    assert hash_and_count(p)[1:] == (5, 3)
    p.write_bytes(b"a\nb\n")
    assert hash_and_count(p)[1:] == (4, 2)


def test_empty_or_missing_download_is_an_error(tmp_path: Path) -> None:
    def empty(url: str, dest: Path) -> None:
        dest.write_bytes(b"")

    def nothing(url: str, dest: Path) -> None:
        return None

    with pytest.raises(LoaderError, match="empty"):
        download_snapshot(DOC_URL, tmp_path, download=empty)
    with pytest.raises(LoaderError, match="no file"):
        download_snapshot(DOC_URL, tmp_path / "n", download=nothing)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y.csv", "http:///nohost.csv"])
def test_only_http_urls_with_a_host(tmp_path: Path, url: str) -> None:
    with pytest.raises(LoaderError, match="http"):
        download_snapshot(url, tmp_path, download=fixture_download())


def test_snapshot_record_is_written_with_its_date(tmp_path: Path) -> None:
    info = download_snapshot(DOC_URL, tmp_path, download=fixture_download(), now=fixed_clock())
    path = write_snapshot_record(info, tmp_path / "measurements")
    assert path.name == "snapshot-2026-11-15.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sha256"] == info.sha256 and data["url"] == DOC_URL


def test_load_window_is_one_hour_and_needs_aware_times() -> None:
    t = dt.datetime(2026, 11, 15, 9, 0, tzinfo=dt.UTC)
    assert within_load_window(t, t + dt.timedelta(minutes=59))
    assert within_load_window(t + dt.timedelta(hours=1), t)
    assert not within_load_window(t, t + dt.timedelta(hours=1, seconds=1))
    with pytest.raises(ValueError, match="timezone"):
        within_load_window(t.replace(tzinfo=None), t)
