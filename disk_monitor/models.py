from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class DiskSample:
    recorded_at: datetime
    drive: str
    total_bytes: int
    used_bytes: int
    free_bytes: int


@dataclass(frozen=True)
class ScanItem:
    path: str
    parent_path: str
    name: str
    kind: str
    size_bytes: int
    file_count: int
    depth: int
    modified_at: float = 0.0


@dataclass(frozen=True, slots=True)
class NavigationItem:
    name: str
    kind: str
    size_bytes: int
    file_count: int
    modified_at: float = 0.0


@dataclass(frozen=True, slots=True)
class NavigationNode:
    total_bytes: int
    file_count: int
    directory_count: int
    error_count: int
    modified_at: float
    direct_file_bytes: int
    direct_file_count: int
    children: tuple[NavigationItem, ...] = ()


@dataclass
class DirectorySkeleton:
    root_path: str
    started_at: datetime
    finished_at: datetime
    nodes: dict[str, NavigationNode] = field(default_factory=dict)
    estimated_bytes: int = 0
    degraded: bool = False


@dataclass
class ScanResult:
    root_path: str
    started_at: datetime
    finished_at: datetime
    total_bytes: int
    file_count: int
    directory_count: int
    error_count: int
    items: list[ScanItem] = field(default_factory=list)
    snapshot_id: int | None = None
    skeleton: DirectorySkeleton | None = None


@dataclass(frozen=True)
class ScanProgress:
    current_path: str
    bytes_seen: int
    file_count: int
    directory_count: int
    error_count: int


@dataclass(frozen=True)
class GrowthItem:
    path: str
    parent_path: str
    name: str
    kind: str
    old_size_bytes: int
    new_size_bytes: int

    @property
    def change_bytes(self) -> int:
        return self.new_size_bytes - self.old_size_bytes


@dataclass(frozen=True)
class SnapshotInfo:
    id: int
    root_path: str
    finished_at: datetime
    total_bytes: int
    note: str
    source: str


@dataclass(frozen=True)
class BlindSpotResult:
    current_at: datetime
    previous_at: datetime | None
    change_bytes: int | None
    threshold_bytes: int

    @property
    def elapsed(self) -> timedelta | None:
        if self.previous_at is None:
            return None
        return self.current_at - self.previous_at

    @property
    def is_significant(self) -> bool:
        return (
            self.change_bytes is not None
            and abs(self.change_bytes) > self.threshold_bytes
        )


@dataclass(frozen=True)
class MonitorSession:
    id: int
    drive: str
    root_path: str
    started_at: datetime
    ended_at: datetime | None
    start_used_bytes: int
    end_used_bytes: int | None
    start_snapshot_id: int | None
    end_snapshot_id: int | None
    end_reason: str | None
    status: str

    @property
    def change_bytes(self) -> int:
        if self.end_used_bytes is None:
            return 0
        return self.end_used_bytes - self.start_used_bytes


@dataclass(frozen=True)
class SessionBoundary:
    occurred_at: datetime
    kind: str
