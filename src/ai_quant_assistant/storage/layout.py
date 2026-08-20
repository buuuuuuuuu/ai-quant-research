from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DataLayout:
    root: Path
    raw: Path
    silver: Path
    gold: Path
    snapshots: Path

    @classmethod
    def from_root(cls, root: Path) -> DataLayout:
        return cls(
            root=root,
            raw=root / "raw",
            silver=root / "silver",
            gold=root / "gold",
            snapshots=root / "snapshots",
        )

    def ensure(self) -> list[str]:
        for path in asdict(self).values():
            path.mkdir(parents=True, exist_ok=True)
        return [str(path) for path in asdict(self).values()]

    def status(self) -> dict[str, bool]:
        return {name: path.is_dir() for name, path in asdict(self).items()}
