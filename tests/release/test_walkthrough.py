"""Portfolio walkthrough: docs/assets/walkthrough.gif, its shot list (docs/walkthrough.md) and
the portfolio entry (portfolio.yml). The GIF is parsed here with no imaging library."""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest
import yaml

from secret_scan import scan
from tests.release.test_readme import LOCAL, SCHEDULE

ROOT = Path(__file__).resolve().parents[2]
GIF = ROOT / "docs" / "assets" / "walkthrough.gif"
SHOTS = ROOT / "docs" / "walkthrough.md"
PORTFOLIO = ROOT / "portfolio.yml"
HOLD_CS = 250  # each screen is held 2.5 s (GIF delays are in hundredths of a second)
MAX_BYTES = 3 * 1024 * 1024
REPO_URL = "https://github.com/biplob819/agentic-analytics"


def _skip_sub_blocks(data: bytes, i: int) -> int:
    while data[i]:
        i += data[i] + 1
    return i + 1


def parse_gif(data: bytes) -> dict:
    """Header, logical width/height, loop count, frame count, total delay and comment blocks."""
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("not a GIF")
    width, height, flags = struct.unpack("<HHB", data[6:11])
    i = 13
    if flags & 0x80:  # global colour table
        i += 3 * 2 ** ((flags & 7) + 1)
    out = {"width": width, "height": height, "loop": None, "frames": 0, "delay_cs": 0}
    out["comments"] = 0
    while i < len(data):
        b = data[i]
        if b == 0x3B:  # trailer
            out["complete"] = True
            return out
        if b == 0x21:
            label = data[i + 1]
            if label == 0xF9:  # graphic control: delay
                out["delay_cs"] += struct.unpack("<H", data[i + 4 : i + 6])[0]
            elif label == 0xFF and data[i + 3 : i + 14] == b"NETSCAPE2.0":
                out["loop"] = struct.unpack("<H", data[i + 16 : i + 18])[0]
            elif label == 0xFE:
                out["comments"] += 1
            i = _skip_sub_blocks(data, i + 2)
        elif b == 0x2C:  # image descriptor
            out["frames"] += 1
            lflags = data[i + 9]
            i += 10
            if lflags & 0x80:
                i += 3 * 2 ** ((lflags & 7) + 1)
            i = _skip_sub_blocks(data, i + 1)  # LZW minimum code size, then image data
        else:
            raise ValueError(f"bad GIF block 0x{b:02x} at {i}")
    raise ValueError("GIF has no trailer")


def shot_rows() -> list[list[str]]:
    rows = []
    for line in SHOTS.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells[0].isdigit():
            rows.append(cells)
    return rows


def test_gif_is_a_looping_960px_gif_under_3mb():
    data = GIF.read_bytes()
    assert len(data) <= MAX_BYTES
    gif = parse_gif(data)
    assert gif["complete"]
    assert gif["width"] == 960
    assert gif["height"] == 600  # 1280x800 captures scaled to 960 wide
    assert gif["loop"] == 0  # loops forever
    assert gif["comments"] == 0


def test_gif_holds_six_to_ten_screens_matching_the_shot_list():
    gif = parse_gif(GIF.read_bytes())
    screens, rest = divmod(gif["delay_cs"], HOLD_CS)
    assert rest == 0
    assert 6 <= screens <= 10
    assert gif["frames"] >= screens
    rows = shot_rows()
    assert [int(r[0]) for r in rows] == list(range(1, screens + 1))


def test_gif_parser_bites():
    with pytest.raises(ValueError):
        parse_gif(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
    with pytest.raises(ValueError):
        parse_gif(GIF.read_bytes()[:-1])  # no trailer


def test_every_shot_source_exists():
    rows = shot_rows()
    assert rows and rows[0][2].startswith("`")  # the first row names its own source
    for number, screen, source in rows:
        assert screen, number
        paths = [p for p in re.findall(r"`([^`]+)`", source) if "/" in p and " " not in p]
        if not source.startswith("same"):
            assert paths, f"shot {number} names no source file"
        for p in paths:
            assert (ROOT / p).exists(), f"shot {number}: {p}"


def test_shot_list_says_how_it_was_captured_and_holds_no_dates():
    text = SHOTS.read_text(encoding="utf-8")
    assert "1280×800" in text and "ffmpeg" in text
    assert "2.5 seconds" in text and "960 px" in text
    assert SCHEDULE.findall(text) == []  # OWNER D-20


def _repo_path(url: str) -> Path | None:
    m = re.fullmatch(re.escape(REPO_URL) + r"/(?:blob|tree)/main/(.+)", url)
    return ROOT / m.group(1) if m else None


def test_portfolio_entry_parses_and_every_path_exists():
    entry = yaml.safe_load(PORTFOLIO.read_text(encoding="utf-8"))
    for key in ("title", "slug", "summary", "case_study", "repo", "links", "images", "tech"):
        assert entry.get(key), key
    assert entry["slug"] == "agentic-analytics"
    assert entry["repo"] == REPO_URL
    assert (ROOT / entry["case_study"]).is_file()
    assert entry["images"]["walkthrough"] == "docs/assets/walkthrough.gif"
    for name, path in entry["images"].items():
        assert (ROOT / path).is_file(), name
    for name in ("architecture", "dataflow"):
        assert entry["images"][name].endswith(".svg")
    for name, url in entry["links"].items():
        if url is None:
            continue
        assert url.startswith(REPO_URL), name
        local = _repo_path(url)
        if local is not None:
            assert local.exists(), name
    assert entry["links"]["demo"] is None  # set when the demo is live
    assert entry["links"]["dashboard"] is None  # set when the dashboard is deployed
    assert isinstance(entry["tech"], list)


def test_portfolio_entry_is_not_approved():
    entry = yaml.safe_load(PORTFOLIO.read_text(encoding="utf-8"))
    assert entry["status"] != "approved"  # the owner approves
    assert "approved" not in PORTFOLIO.read_text(encoding="utf-8").split("status:")[1]


def test_walkthrough_files_hold_no_secret_local_path_or_email():
    files = [SHOTS, PORTFOLIO]
    assert scan(files) == []
    for f in files:
        assert not LOCAL.findall(f.read_text(encoding="utf-8")), f.name
    raw = GIF.read_bytes()  # no metadata naming a local path
    assert not LOCAL.findall(raw.decode("latin-1"))
