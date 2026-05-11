from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

import orjson

from .models import EXPORT_TIMEZONE, MaterializedAsset, NormalizedMessage
from .paths import build_timestamp_token

StrictMissingMode = Literal["off", "collect", "abort", "threshold"]
ForensicDepth = Literal["standard", "deep"]

DEFAULT_MAX_INCIDENTS = 100
DEFAULT_MAX_UNIQUE_FAILURES = 24
DEFAULT_MAX_FORENSIC_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DIR_SNAPSHOTS = 64
DEFAULT_MAX_DIR_ENTRIES = 48


@dataclass(frozen=True, slots=True)
class StrictMissingPolicy:
    mode: StrictMissingMode = "off"
    threshold: int | None = None
    max_incidents: int = DEFAULT_MAX_INCIDENTS
    max_unique_failure_fingerprints: int = DEFAULT_MAX_UNIQUE_FAILURES
    max_forensic_bytes: int = DEFAULT_MAX_FORENSIC_BYTES
    max_dir_snapshots: int = DEFAULT_MAX_DIR_SNAPSHOTS
    forensic_depth: ForensicDepth = "standard"

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @classmethod
    def parse(
        cls,
        value: str | None,
        *,
        default: "StrictMissingPolicy | None" = None,
    ) -> "StrictMissingPolicy":
        if value is None:
            return default or cls()
        raw = str(value).strip().lower()
        if not raw or raw in {"0", "off", "false", "no"}:
            return cls(mode="off")
        if raw in {"1", "collect", "true", "on"}:
            return cls(mode="collect")
        if raw == "abort":
            return cls(mode="abort")
        if raw.startswith("threshold:"):
            threshold_text = raw.split(":", 1)[1].strip()
            threshold = int(threshold_text)
            if threshold <= 0:
                raise ValueError("strict missing threshold must be >= 1")
            return cls(mode="threshold", threshold=threshold)
        raise ValueError("strict missing mode must be one of: off, collect, abort, threshold:N")

    def should_abort(self, *, incident_count: int) -> bool:
        if not self.enabled:
            return False
        if self.mode == "abort":
            return incident_count >= 1
        if self.mode == "threshold":
            return incident_count >= int(self.threshold or 1)
        return False


@dataclass(frozen=True, slots=True)
class ForensicsRecordResult:
    incident_id: str
    failure_fingerprint: str
    asset_fingerprint: str
    reason_category: str
    incident_path: Path | None
    is_new_incident: bool
    should_abort: bool
    occurrence_count: int


class ExportInvestigativeFailure(RuntimeError):
    def __init__(
        self,
        *,
        incident_id: str,
        forensic_summary_path: Path | None,
        incident_path: Path | None,
        reason_category: str,
    ) -> None:
        summary_hint = f" forensic={forensic_summary_path}" if forensic_summary_path else ""
        incident_hint = f" incident={incident_path}" if incident_path else ""
        super().__init__(
            f"strict missing aborted export incident={incident_id} reason={reason_category}{summary_hint}{incident_hint}"
        )
        self.incident_id = incident_id
        self.forensic_summary_path = forensic_summary_path
        self.incident_path = incident_path
        self.reason_category = reason_category


class ExportForensicsCollector:
    def __init__(
        self,
        state_dir: Path,
        *,
        chat_type: str,
        chat_id: str,
        policy: StrictMissingPolicy,
        command_context: dict[str, Any] | None = None,
    ) -> None:
        self._state_dir = state_dir
        self._chat_type = chat_type
        self._chat_id = chat_id
        self._policy = policy
        self._command_context = dict(command_context or {})
        stamp = build_timestamp_token(include_pid=True)
        self._run_id = f"{chat_type}_{chat_id}_{stamp}"
        self._run_dir = self._state_dir / "export_forensics" / self._run_id
        self._preflight_path = self._run_dir / "preflight.json"
        self._summary_path = self._run_dir / "run_summary.json"
        self._preflight_written = False
        self._dir_snapshots_taken = 0
        self._forensic_bytes_written = 0
        self._incident_records: dict[str, dict[str, Any]] = {}
        self._occurrences_by_failure: dict[str, list[dict[str, Any]]] = {}
        self._occurrence_count = 0
        self._incident_counter = 0
        self._budget_events: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._policy.enabled

    @property
    def run_dir(self) -> Path | None:
        return self._run_dir if self.enabled else None

    @property
    def summary_path(self) -> Path | None:
        if not self.enabled:
            return None
        return self._summary_path

    @property
    def incident_count(self) -> int:
        return len(self._incident_records)

    def capture_preflight(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self._ensure_run_dir()
        merged = {
            "run_id": self._run_id,
            "chat_type": self._chat_type,
            "chat_id": self._chat_id,
            "captured_at": datetime.now(EXPORT_TIMEZONE).isoformat(),
            "strict_missing_policy": self._policy_to_dict(),
            "command_context": self._command_context,
            **payload,
        }
        self._write_json(self._preflight_path, merged)
        self._preflight_written = True

    def record_investigative_missing(
        self,
        *,
        message: NormalizedMessage,
        candidate: dict[str, Any],
        asset: MaterializedAsset,
        route_attempts: list[dict[str, Any]],
        pre_path_evidence: dict[str, Any] | None = None,
    ) -> ForensicsRecordResult | None:
        if not self.enabled:
            return None
        if asset.status != "missing":
            return None
        if self._occurrence_count >= self._policy.max_incidents:
            self._budget_events.append(
                {
                    "kind": "incident_budget_exhausted",
                    "reason": "max_incidents",
                }
            )
            return None
        reason_category = self._classify_reason_category(
            candidate=candidate,
            asset=asset,
            route_attempts=route_attempts,
        )
        if reason_category is None:
            return None

        self._ensure_run_dir()
        if not self._preflight_written:
            self.capture_preflight({})

        failure_fingerprint = self._failure_fingerprint(
            reason_category=reason_category,
            candidate=candidate,
            asset=asset,
            route_attempts=route_attempts,
        )
        asset_fingerprint = self._asset_fingerprint(candidate=candidate, asset=asset)
        occurrence = {
            "message_id": message.message_id,
            "message_seq": message.message_seq,
            "timestamp_iso": message.timestamp_iso,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "file_name": asset.file_name,
            "source_path": asset.source_path,
            "missing_kind": asset.missing_kind,
            "resolver": asset.resolver,
        }

        existing = self._incident_records.get(failure_fingerprint)
        if existing is not None:
            self._occurrence_count += 1
            occurrences = self._occurrences_by_failure.setdefault(failure_fingerprint, [])
            occurrences.append(occurrence)
            existing["occurrence_count"] = len(occurrences)
            return ForensicsRecordResult(
                incident_id=str(existing["incident_id"]),
                failure_fingerprint=failure_fingerprint,
                asset_fingerprint=asset_fingerprint,
                reason_category=reason_category,
                incident_path=Path(existing["incident_path"]) if existing.get("incident_path") else None,
                is_new_incident=False,
                should_abort=self._policy.should_abort(incident_count=len(self._incident_records)),
                occurrence_count=len(occurrences),
            )

        if len(self._incident_records) >= self._policy.max_unique_failure_fingerprints:
            self._budget_events.append(
                {
                    "kind": "unique_failure_budget_exhausted",
                    "failure_fingerprint": failure_fingerprint,
                    "reason_category": reason_category,
                }
            )
            return None

        self._incident_counter += 1
        self._occurrence_count += 1
        incident_id = f"incident_{self._incident_counter:03d}"
        occurrences = self._occurrences_by_failure.setdefault(failure_fingerprint, [])
        occurrences.append(occurrence)
        incident_payload = {
            "incident_id": incident_id,
            "run_id": self._run_id,
            "reason_category": reason_category,
            "failure_fingerprint": failure_fingerprint,
            "asset_fingerprint": asset_fingerprint,
            "captured_at": datetime.now(EXPORT_TIMEZONE).isoformat(),
            "chat_type": self._chat_type,
            "chat_id": self._chat_id,
            "message_context": {
                "message_id": message.message_id,
                "message_seq": message.message_seq,
                "timestamp_iso": message.timestamp_iso,
                "sender_id": message.sender_id,
                "sender_name": message.sender_name,
                "chat_name": message.chat_name,
            },
            "asset": asset.model_dump(mode="json"),
            "candidate": candidate,
            "route_ledger": self._normalize_route_attempts(route_attempts),
            "route_attempts": route_attempts,
            "pre_path_evidence": pre_path_evidence,
            "post_path_evidence": self._collect_path_evidence(candidate=candidate, asset=asset),
            "command_context": self._command_context,
            "occurrence_count": len(occurrences),
        }
        incident_payload["path_evidence_diff"] = self._diff_path_evidence(
            incident_payload.get("pre_path_evidence"),
            incident_payload.get("post_path_evidence"),
        )
        incident_path = self._run_dir / f"{incident_id}.json"
        encoded_size = len(orjson.dumps(incident_payload))
        if self._forensic_bytes_written + encoded_size > self._policy.max_forensic_bytes:
            self._budget_events.append(
                {
                    "kind": "forensic_bytes_budget_exhausted",
                    "failure_fingerprint": failure_fingerprint,
                    "reason_category": reason_category,
                }
            )
            incident_path_value: Path | None = None
        else:
            self._write_json(incident_path, incident_payload)
            incident_path_value = incident_path
        self._incident_records[failure_fingerprint] = {
            "incident_id": incident_id,
            "incident_path": str(incident_path_value) if incident_path_value is not None else None,
            "reason_category": reason_category,
            "asset_fingerprint": asset_fingerprint,
            "asset_type": asset.asset_type,
            "file_name": asset.file_name,
            "missing_kind": asset.missing_kind,
            "occurrence_count": len(occurrences),
        }
        return ForensicsRecordResult(
            incident_id=incident_id,
            failure_fingerprint=failure_fingerprint,
            asset_fingerprint=asset_fingerprint,
            reason_category=reason_category,
            incident_path=incident_path_value,
            is_new_incident=True,
            should_abort=self._policy.should_abort(incident_count=len(self._incident_records)),
            occurrence_count=len(occurrences),
        )

    def collect_candidate_path_evidence(
        self,
        *,
        candidate: dict[str, Any],
        asset_type: str,
        file_name: str | None,
        source_path: str | None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        synthetic_asset = MaterializedAsset(
            sender_id="forensics",
            timestamp_iso=datetime.now(EXPORT_TIMEZONE).isoformat(),
            asset_type=str(asset_type),
            file_name=file_name,
            source_path=source_path,
        )
        return self._collect_path_evidence(candidate=candidate, asset=synthetic_asset)

    def finalize(
        self,
        *,
        export_completed: bool,
        aborted: bool,
        data_path: Path | None = None,
        manifest_path: Path | None = None,
        trace_path: Path | None = None,
        log_path: Path | None = None,
        error: str | None = None,
    ) -> Path | None:
        if not self.enabled:
            return None
        self._ensure_run_dir()
        payload = {
            "run_id": self._run_id,
            "chat_type": self._chat_type,
            "chat_id": self._chat_id,
            "strict_missing_policy": self._policy_to_dict(),
            "command_context": self._command_context,
            "export_completed": export_completed,
            "aborted": aborted,
            "error": error,
            "incident_count": len(self._incident_records),
            "occurrence_count": self._occurrence_count,
            "forensic_bytes_written": self._forensic_bytes_written,
            "dir_snapshots_taken": self._dir_snapshots_taken,
            "budget_events": self._budget_events,
            "budget_status": self._budget_status(),
            "data_path": str(data_path) if data_path is not None else None,
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
            "trace_path": str(trace_path) if trace_path is not None else None,
            "log_path": str(log_path) if log_path is not None else None,
            "incidents": [
                {
                    **record,
                    "failure_fingerprint": failure_fingerprint,
                }
                for failure_fingerprint, record in sorted(self._incident_records.items())
            ],
            "occurrences_by_failure": self._occurrences_by_failure,
            "grouped_summary": self._grouped_summary(),
        }
        self._write_json(self._summary_path, payload)
        return self._summary_path

    def _policy_to_dict(self) -> dict[str, Any]:
        return {
            "mode": self._policy.mode,
            "threshold": self._policy.threshold,
            "max_incidents": self._policy.max_incidents,
            "max_unique_failure_fingerprints": self._policy.max_unique_failure_fingerprints,
            "max_forensic_bytes": self._policy.max_forensic_bytes,
            "max_dir_snapshots": self._policy.max_dir_snapshots,
            "forensic_depth": self._policy.forensic_depth,
        }

    def _classify_reason_category(
        self,
        *,
        candidate: dict[str, Any],
        asset: MaterializedAsset,
        route_attempts: list[dict[str, Any]],
    ) -> str | None:
        missing_kind = str(asset.missing_kind or "").strip()
        if not missing_kind:
            return "missing_kind_absent"
        if missing_kind in {"qq_expired_after_napcat", "qq_not_downloaded_local_placeholder"}:
            # Known-expired assets are valid terminal outcomes in release and
            # debug runs. Do not spend incident budget on them unless the
            # missing explanation itself is contradictory or absent.
            if self._has_path_leaf_conflict(asset.file_name, asset.source_path):
                return "hint_path_conflicts_with_file_name"
            return None
        if self._has_path_leaf_conflict(asset.file_name, asset.source_path):
            return "hint_path_conflicts_with_file_name"
        if self._hinted_local_path_missing(candidate, asset):
            return "hint_path_missing"
        if any(
            str(attempt.get("status") or "").strip() in {"timeout", "unavailable"}
            for attempt in route_attempts
        ):
            return "route_timeout_with_missing"
        if missing_kind == "missing_after_napcat" and not route_attempts:
            return "generic_missing_without_route_ledger"
        return None

    def _collect_path_evidence(
        self,
        *,
        candidate: dict[str, Any],
        asset: MaterializedAsset,
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "paths": [],
            "directory_snapshots": [],
        }
        seen_paths: set[str] = set()
        focus_name = str(asset.file_name or "").strip() or None
        download_hint = candidate.get("download_hint") if isinstance(candidate.get("download_hint"), dict) else {}
        for label, value in [
            ("source_path", asset.source_path),
            ("hint_path", download_hint.get("path")),
            ("hint_file", download_hint.get("file")),
            ("hint_url", download_hint.get("url")),
        ]:
            text = str(value or "").strip()
            if not text:
                continue
            if text in seen_paths:
                continue
            seen_paths.add(text)
            path_entry = self._path_entry(label=label, value=text)
            evidence["paths"].append(path_entry)
            if not self._should_snapshot_path(label=label, value=text):
                continue
            if not self._can_take_dir_snapshot():
                continue
            snapshot_dirs = self._candidate_snapshot_dirs(text, asset.asset_type)
            for directory in snapshot_dirs:
                snapshot = self._snapshot_directory(directory, focus_name=focus_name)
                if snapshot is None:
                    continue
                evidence["directory_snapshots"].append(snapshot)
        return evidence

    def _path_entry(self, *, label: str, value: str) -> dict[str, Any]:
        path = Path(PureWindowsPath(value))
        try:
            exists = path.exists()
            is_file = path.is_file() if exists else False
            is_dir = path.is_dir() if exists else False
        except OSError:
            exists = False
            is_file = False
            is_dir = False
        return {
            "label": label,
            "path": value,
            "exists": exists,
            "is_file": is_file,
            "is_dir": is_dir,
        }

    def _candidate_snapshot_dirs(self, value: str, asset_type: str) -> list[Path]:
        path = Path(PureWindowsPath(value))
        if path.suffix:
            base_dir = path.parent
        else:
            base_dir = path
        candidates: list[Path] = []
        for directory in [base_dir, *self._sibling_media_dirs(base_dir, asset_type)]:
            if directory not in candidates:
                candidates.append(directory)
        return candidates

    def _sibling_media_dirs(self, base_dir: Path, asset_type: str) -> list[Path]:
        siblings: list[Path] = []
        parent = base_dir.parent
        for sibling_name in ("Ori", "OriTemp", "Thumb"):
            candidate = parent / sibling_name
            if candidate != base_dir:
                siblings.append(candidate)
        return siblings

    def _snapshot_directory(self, directory: Path, *, focus_name: str | None) -> dict[str, Any] | None:
        if not self._can_take_dir_snapshot():
            return None
        try:
            exists = directory.exists()
            is_dir = directory.is_dir() if exists else False
        except OSError:
            exists = False
            is_dir = False
        snapshot: dict[str, Any] = {
            "directory": str(directory),
            "exists": exists,
            "is_dir": is_dir,
            "entries": [],
        }
        self._dir_snapshots_taken += 1
        if not exists or not is_dir:
            return snapshot
        entries: list[dict[str, Any]] = []
        focus_stem = Path(focus_name).stem.casefold() if focus_name else ""
        focus_suffix = Path(focus_name).suffix.casefold() if focus_name else ""
        try:
            children = [child for child in directory.iterdir() if child.is_file()]
        except OSError:
            children = []
        children.sort(
            key=lambda child: self._directory_entry_priority(
                child,
                focus_stem=focus_stem,
                focus_suffix=focus_suffix,
            )
        )
        for child in children[:DEFAULT_MAX_DIR_ENTRIES]:
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=EXPORT_TIMEZONE).isoformat(),
                    "created_at": datetime.fromtimestamp(stat.st_ctime, tz=EXPORT_TIMEZONE).isoformat(),
                }
            )
        snapshot["entries"] = entries
        return snapshot

    @staticmethod
    def _directory_entry_priority(
        child: Path,
        *,
        focus_stem: str,
        focus_suffix: str,
    ) -> tuple[int, int, str]:
        stem = child.stem.casefold()
        suffix = child.suffix.casefold()
        stem_rank = 0 if focus_stem and focus_stem in stem else 1
        suffix_rank = 0 if focus_suffix and suffix == focus_suffix else 1
        return (stem_rank, suffix_rank, child.name.casefold())

    def _can_take_dir_snapshot(self) -> bool:
        return self._dir_snapshots_taken < self._policy.max_dir_snapshots

    def _normalize_route_attempts(self, route_attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for attempt in route_attempts:
            normalized.append(
                {
                    "stage": str(attempt.get("stage") or "").strip() or None,
                    "route": str(attempt.get("substep") or "").strip() or None,
                    "status": str(attempt.get("status") or "").strip() or None,
                    "timeout_s": attempt.get("timeout_s"),
                    "elapsed_s": attempt.get("elapsed_s"),
                    "detail": attempt.get("detail"),
                    "message_id_raw": attempt.get("message_id_raw"),
                    "element_id": attempt.get("element_id"),
                    "hint_file_id": attempt.get("hint_file_id"),
                    "hint_url": attempt.get("hint_url"),
                    "forward_parent_message_id_raw": attempt.get("forward_parent_message_id_raw"),
                    "forward_parent_element_id": attempt.get("forward_parent_element_id"),
                }
            )
        return normalized

    def _budget_status(self) -> dict[str, Any]:
        event_kinds = [str(event.get("kind") or "") for event in self._budget_events]
        return {
            "max_incidents": self._policy.max_incidents,
            "max_unique_failure_fingerprints": self._policy.max_unique_failure_fingerprints,
            "max_forensic_bytes": self._policy.max_forensic_bytes,
            "max_dir_snapshots": self._policy.max_dir_snapshots,
            "incident_budget_exhausted": "incident_budget_exhausted" in event_kinds,
            "unique_failure_budget_exhausted": "unique_failure_budget_exhausted" in event_kinds,
            "forensic_bytes_budget_exhausted": "forensic_bytes_budget_exhausted" in event_kinds,
            "budget_event_count": len(self._budget_events),
        }

    def _grouped_summary(self) -> dict[str, Any]:
        by_reason: dict[str, dict[str, Any]] = {}
        by_asset_type: dict[str, dict[str, Any]] = {}
        repeated: list[dict[str, Any]] = []
        for failure_fingerprint, record in self._incident_records.items():
            reason = str(record.get("reason_category") or "unknown")
            asset_type = str(record.get("asset_type") or "unknown")
            occurrence_count = int(record.get("occurrence_count") or 0)
            reason_entry = by_reason.setdefault(reason, {"incident_count": 0, "occurrence_count": 0})
            reason_entry["incident_count"] += 1
            reason_entry["occurrence_count"] += occurrence_count
            asset_entry = by_asset_type.setdefault(asset_type, {"incident_count": 0, "occurrence_count": 0})
            asset_entry["incident_count"] += 1
            asset_entry["occurrence_count"] += occurrence_count
            repeated.append(
                {
                    "failure_fingerprint": failure_fingerprint,
                    "incident_id": record.get("incident_id"),
                    "reason_category": reason,
                    "asset_type": asset_type,
                    "file_name": record.get("file_name"),
                    "missing_kind": record.get("missing_kind"),
                    "occurrence_count": occurrence_count,
                }
            )
        repeated.sort(
            key=lambda item: (
                -int(item.get("occurrence_count") or 0),
                str(item.get("reason_category") or ""),
                str(item.get("file_name") or ""),
            )
        )
        return {
            "by_reason_category": by_reason,
            "by_asset_type": by_asset_type,
            "top_repeated_incidents": repeated[:10],
        }

    @staticmethod
    def _should_snapshot_path(*, label: str, value: str) -> bool:
        if label in {"source_path", "hint_path"}:
            return True
        if label in {"hint_file", "hint_url"}:
            return _looks_like_local_path(value)
        return False

    @staticmethod
    def _diff_path_evidence(
        pre_path_evidence: dict[str, Any] | None,
        post_path_evidence: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not pre_path_evidence and not post_path_evidence:
            return None
        pre_snapshots = ExportForensicsCollector._snapshots_by_directory(pre_path_evidence)
        post_snapshots = ExportForensicsCollector._snapshots_by_directory(post_path_evidence)
        directories = sorted(set(pre_snapshots) | set(post_snapshots))
        diffs: list[dict[str, Any]] = []
        for directory in directories:
            pre_snapshot = pre_snapshots.get(directory)
            post_snapshot = post_snapshots.get(directory)
            pre_entries = ExportForensicsCollector._entry_map(pre_snapshot)
            post_entries = ExportForensicsCollector._entry_map(post_snapshot)
            added = sorted(name for name in post_entries if name not in pre_entries)
            removed = sorted(name for name in pre_entries if name not in post_entries)
            changed: list[dict[str, Any]] = []
            for name in sorted(set(pre_entries) & set(post_entries)):
                pre_entry = pre_entries[name]
                post_entry = post_entries[name]
                if (
                    pre_entry.get("size") != post_entry.get("size")
                    or pre_entry.get("modified_at") != post_entry.get("modified_at")
                    or pre_entry.get("created_at") != post_entry.get("created_at")
                ):
                    changed.append(
                        {
                            "name": name,
                            "before": pre_entry,
                            "after": post_entry,
                        }
                    )
            if not added and not removed and not changed:
                continue
            diffs.append(
                {
                    "directory": directory,
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                }
            )
        return {"directories": diffs}

    @staticmethod
    def _snapshots_by_directory(path_evidence: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not isinstance(path_evidence, dict):
            return {}
        snapshots = path_evidence.get("directory_snapshots")
        if not isinstance(snapshots, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            directory = str(snapshot.get("directory") or "").strip()
            if not directory:
                continue
            result[directory] = snapshot
        return result

    @staticmethod
    def _entry_map(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not isinstance(snapshot, dict):
            return {}
        entries = snapshot.get("entries")
        if not isinstance(entries, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            result[name] = entry
        return result

    def _asset_fingerprint(self, *, candidate: dict[str, Any], asset: MaterializedAsset) -> str:
        hint = candidate.get("download_hint") if isinstance(candidate.get("download_hint"), dict) else {}
        payload = "|".join(
            [
                str(asset.asset_type or ""),
                str(asset.asset_role or ""),
                str(asset.file_name or ""),
                str(candidate.get("md5") or ""),
                str(asset.source_path or ""),
                str(hint.get("file_id") or ""),
                str(hint.get("message_id_raw") or ""),
                str(hint.get("element_id") or ""),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _failure_fingerprint(
        self,
        *,
        reason_category: str,
        candidate: dict[str, Any],
        asset: MaterializedAsset,
        route_attempts: list[dict[str, Any]],
    ) -> str:
        route_shape = ",".join(
            sorted(
                {
                    f"{str(attempt.get('substep') or '')}:{str(attempt.get('status') or '')}"
                    for attempt in route_attempts
                    if str(attempt.get("stage") or "") == "done"
                }
            )
        )
        hint = candidate.get("download_hint") if isinstance(candidate.get("download_hint"), dict) else {}
        source_leaf = Path(asset.source_path).name if asset.source_path else ""
        payload = "|".join(
            [
                reason_category,
                str(asset.asset_type or ""),
                str(asset.asset_role or ""),
                str(asset.file_name or ""),
                source_leaf,
                str(hint.get("file_id") or ""),
                route_shape,
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _has_path_leaf_conflict(file_name: str | None, source_path: str | None) -> bool:
        if not file_name or not source_path:
            return False
        try:
            source_leaf = Path(PureWindowsPath(source_path)).name
        except Exception:
            return False
        if not source_leaf:
            return False
        return (
            ExportForensicsCollector._canonical_leaf_identity(file_name)
            != ExportForensicsCollector._canonical_leaf_identity(source_leaf)
        )

    @staticmethod
    def _canonical_leaf_identity(value: str) -> str:
        stem = Path(str(value or "")).stem.casefold().strip()
        if not stem:
            return ""
        stripped = stem.strip("{}")
        compact = re.sub(r"[-_]", "", stripped)
        if compact and re.fullmatch(r"[0-9a-f]+", compact):
            return compact
        return stripped

    @staticmethod
    def _hinted_local_path_missing(candidate: dict[str, Any], asset: MaterializedAsset) -> bool:
        if asset.source_path:
            try:
                if not Path(PureWindowsPath(asset.source_path)).exists():
                    return True
            except OSError:
                return True
        hint = candidate.get("download_hint") if isinstance(candidate.get("download_hint"), dict) else {}
        for key in ("path", "file", "url"):
            value = str(hint.get(key) or "").strip()
            if not value or not _looks_like_local_path(value):
                continue
            try:
                if not Path(PureWindowsPath(value)).exists():
                    return True
            except OSError:
                return True
        return False

    def _ensure_run_dir(self) -> None:
        self._run_dir.mkdir(parents=True, exist_ok=True)

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        encoded = orjson.dumps(payload, option=orjson.OPT_INDENT_2)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_bytes(encoded)
        temp_path.replace(path)
        self._forensic_bytes_written += len(encoded)


def resolve_strict_missing_policy(
    requested: str | None,
    *,
    env: dict[str, str] | None = None,
) -> StrictMissingPolicy:
    effective_env = env if env is not None else {}
    return StrictMissingPolicy.parse(
        requested if requested is not None else effective_env.get("EXPORT_STRICT_MISSING")
    )


def _looks_like_local_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text.startswith("\\\\") or (len(text) >= 3 and text[1:3] in {":\\", ":/"})
