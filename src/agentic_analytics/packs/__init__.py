"""Dataset packs: discovery and file access (LOD-07, OWNER D-29, Tech Spec §2.7).

`load_pack("uk_price_paid")` opens a pack bundled in the wheel (through `importlib.resources`, so
it works from an installed wheel or a zip); `load_pack(Path("my_pack"))` or any string with a path
separator opens a pack directory (`--pack PATH`). Either way `pack.yaml` is validated into
`DatasetPack`. `pack_file(pack, "load/clickhouse.sql")` resolves a file inside the pack and
refuses paths that would leave it. Loaders read only through these two functions.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

import yaml
from pydantic import ValidationError

from agentic_analytics.schemas import DatasetPack
from agentic_analytics.schemas.config import _UniqueKeyLoader

MANIFEST = "pack.yaml"
_BARE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class PackError(ValueError):
    """A pack could not be found, read or validated; the message names the pack and file."""


@dataclass(frozen=True)
class Pack:
    """A validated pack manifest plus where its files live."""

    manifest: DatasetPack
    root: Traversable
    bundled: bool

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def ref(self) -> str:
        return self.manifest.ref

    def __repr__(self) -> str:
        where = "bundled" if self.bundled else str(self.root)
        return f"Pack({self.ref}, {where})"


def bundled_root() -> Traversable:
    return resources.files(__name__)


def bundled_packs() -> tuple[str, ...]:
    """Names of the packs shipped in the wheel (directories holding a `pack.yaml`)."""
    root = bundled_root()
    return tuple(
        sorted(
            p.name
            for p in root.iterdir()
            if p.is_dir() and _BARE_NAME.match(p.name) and p.joinpath(MANIFEST).is_file()
        )
    )


def _is_path(spec: str | os.PathLike[str]) -> bool:
    if isinstance(spec, os.PathLike):
        return True
    return (
        "/" in spec
        or os.sep in spec
        or (os.altsep is not None and os.altsep in spec)
        or spec.startswith((".", "~"))
    )


def _parse_manifest(text: str, where: str) -> DatasetPack:
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise PackError(f"{where}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError(f"{where}: top level must be a mapping")
    try:
        return DatasetPack.model_validate(data)
    except ValidationError as exc:
        raise PackError(f"{where}: {exc}") from exc


def load_pack(name_or_path: str | os.PathLike[str]) -> Pack:
    """Open a bundled pack by name, or a pack directory by path, and validate its manifest."""
    if _is_path(name_or_path):
        root_path = Path(os.fspath(name_or_path)).expanduser()
        if not root_path.is_dir():
            raise PackError(f"pack directory {root_path} does not exist")
        manifest = root_path / MANIFEST
        if not manifest.is_file():
            raise PackError(f"{root_path}: no {MANIFEST}")
        try:
            text = manifest.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PackError(f"{manifest}: cannot read ({type(exc).__name__})") from exc
        return Pack(_parse_manifest(text, str(manifest)), root_path.resolve(), bundled=False)

    name = str(name_or_path)
    names = bundled_packs()
    if name not in names:
        hint = f"bundled packs: {', '.join(names) or 'none'}; use ./{name} for a directory"
        raise PackError(f"no bundled pack named {name!r} ({hint})")
    root = bundled_root().joinpath(name)
    text = root.joinpath(MANIFEST).read_text(encoding="utf-8")
    manifest = _parse_manifest(text, f"bundled pack {name}/{MANIFEST}")
    if manifest.name != name:
        raise PackError(f"bundled pack {name}: pack.yaml names itself {manifest.name!r}")
    return Pack(manifest, root, bundled=True)


def pack_file(pack: Pack, relpath: str) -> Traversable:
    """A file inside the pack, by a relative POSIX path. Paths that leave the pack are refused."""
    if not isinstance(relpath, str) or not relpath or "\\" in relpath or "\x00" in relpath:
        raise PackError(f"bad pack file path {relpath!r}")
    pure = PurePosixPath(relpath)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in relpath.split("/")):
        raise PackError(f"pack file path {relpath!r} must stay inside the pack")
    target = pack.root.joinpath(*pure.parts)
    if (
        isinstance(pack.root, Path)
        and isinstance(target, Path)
        and not target.resolve().is_relative_to(pack.root.resolve())
    ):
        raise PackError(f"pack file {relpath!r} resolves outside the pack")
    if not target.is_file():
        raise PackError(f"pack {pack.ref} has no file {relpath!r}")
    return target


def read_pack_text(pack: Pack, relpath: str) -> str:
    return pack_file(pack, relpath).read_text(encoding="utf-8")


__all__ = [
    "MANIFEST",
    "Pack",
    "PackError",
    "bundled_packs",
    "load_pack",
    "pack_file",
    "read_pack_text",
]
