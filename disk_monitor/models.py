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
    allocated_size_bytes: int | None = None
    unique_allocated_size_bytes: int | None = None
    volume_serial_hex: str | None = None
    file_id: bytes | None = None
    link_count: int | None = None
    is_unique_owner: bool | None = None
    measurement_state: str = "legacy"


@dataclass(frozen=True, slots=True)
class NavigationItem:
    name: str
    kind: str
    size_bytes: int
    file_count: int
    modified_at: float = 0.0
    allocated_size_bytes: int | None = None
    unique_allocated_size_bytes: int | None = None
    volume_serial_hex: str | None = None
    file_id: bytes | None = None
    link_count: int | None = None
    is_unique_owner: bool | None = None
    measurement_state: str = "legacy"


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
    allocated_size_bytes: int | None = None
    unique_allocated_size_bytes: int | None = None
    measured_allocated_bytes: int = 0
    measured_unique_allocated_bytes: int = 0
    eligible_file_count: int = 0
    allocation_measured_file_count: int = 0
    identity_measured_file_count: int = 0
    metadata_error_count: int = 0
    measurement_state: str = "legacy"


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
    allocated_total_bytes: int | None = None
    unique_allocated_total_bytes: int | None = None
    measured_allocated_bytes: int = 0
    measured_unique_allocated_bytes: int = 0
    eligible_file_count: int = 0
    allocation_measured_file_count: int = 0
    identity_measured_file_count: int = 0
    metadata_error_count: int = 0
    measurement_state: str = "legacy"
    scan_config_version: int = 0
    scan_config_json: str | None = None
    excluded_rule_count: int = 0
    excluded_item_count: int = 0


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
