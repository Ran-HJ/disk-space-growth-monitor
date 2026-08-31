from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import PurePosixPath


_GLOB_CHARS = "*?["


def _path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _validate_glob(pattern: str, line_number: int) -> None:
    if not pattern or pattern == ".":
        raise ValueError(f"排除规则第 {line_number} 行为空路径")
    if ".." in PurePosixPath(pattern).parts:
        raise ValueError(f"排除规则第 {line_number} 行不能包含 ..")
    if pattern.count("[") != pattern.count("]"):
        raise ValueError(f"排除规则第 {line_number} 行的 [] 不完整")


@dataclass(frozen=True, slots=True)
class ExclusionRule:
    kind: str
    value: str
    serialized: str
    directory_prefix: str = ""


class ExclusionMatcher:
    def __init__(self, root_path: str, rules: tuple[ExclusionRule, ...]) -> None:
        self.root_path = root_path
        self.rules = rules

    @property
    def serialized_rules(self) -> tuple[str, ...]:
        return tuple(rule.serialized for rule in self.rules)

    def matches(self, path: str) -> bool:
        normalized = os.path.normcase(os.path.abspath(path))
        relative = os.path.relpath(normalized, self.root_path).replace("\\", "/")
        folded_relative = relative.casefold()
        for rule in self.rules:
            if rule.kind == "path":
                if normalized == rule.value or _path_is_within(
                    normalized, rule.value
                ):
                    return True
                continue
            if fnmatch.fnmatchcase(folded_relative, rule.value.casefold()):
                return True
            if rule.directory_prefix and folded_relative == rule.directory_prefix:
                return True
        return False


def compile_exclusion_rules(
    root_path: str,
    raw_rules: list[str] | tuple[str, ...],
) -> ExclusionMatcher:
    root = os.path.normcase(os.path.abspath(root_path))
    rules: list[ExclusionRule] = []
    for line_number, raw_rule in enumerate(raw_rules, start=1):
        rule = raw_rule.strip()
        if not rule:
            continue
        if os.path.isabs(rule):
            if any(character in rule for character in _GLOB_CHARS):
                raise ValueError(
                    f"排除规则第 {line_number} 行：绝对路径不能包含通配符"
                )
            normalized = os.path.normcase(os.path.abspath(rule))
            if normalized == root:
                raise ValueError(
                    f"排除规则第 {line_number} 行不能排除扫描根目录"
                )
            if not _path_is_within(normalized, root):
                raise ValueError(
                    f"排除规则第 {line_number} 行不在扫描根目录内"
                )
            rules.append(
                ExclusionRule("path", normalized, f"path:{normalized}")
            )
            continue
        normalized_glob = rule.replace("\\", "/")
        while normalized_glob.startswith("./"):
            normalized_glob = normalized_glob[2:]
        normalized_glob = normalized_glob.rstrip("/")
        _validate_glob(normalized_glob, line_number)
        prefix = ""
        if normalized_glob.endswith("/**"):
            directory_pattern = normalized_glob[:-3].rstrip("/")
            if not any(
                character in directory_pattern for character in _GLOB_CHARS
            ):
                prefix = directory_pattern.casefold()
        rules.append(
            ExclusionRule(
                "glob",
                normalized_glob,
                f"glob:{normalized_glob}",
                prefix,
            )
        )
    return ExclusionMatcher(root, tuple(rules))
