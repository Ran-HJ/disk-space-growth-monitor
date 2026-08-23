from __future__ import annotations


def format_bytes(value: int) -> str:
    sign = "-" if value < 0 else ""
    size = float(abs(value))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{sign}{int(size)} {unit}"
            return f"{sign}{size:.1f} {unit}"
        size /= 1024
    return f"{sign}{size:.1f} PB"

