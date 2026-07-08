import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ..models import AccountSnapshot


LOGGER = logging.getLogger("trading-ui.operations-log")

SnapshotProvider = Callable[[], Any]


def _utc_datetime(value: Optional[datetime] = None) -> datetime:
    dt = value or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_iso(value: Optional[datetime] = None) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


def _filename_timestamp(value: datetime) -> str:
    return _utc_datetime(value).strftime("%Y%m%dT%H%M%SZ")


class OperationsLog:
    def __init__(
        self,
        log_dir: str | os.PathLike[str] = "./logs",
        server_started_at: Optional[datetime] = None,
        snapshots_provider: Optional[SnapshotProvider] = None,
    ) -> None:
        self.server_started_at = _utc_datetime(server_started_at)
        self.server_started_at_iso = _utc_iso(self.server_started_at)
        self._snapshots_provider = snapshots_provider
        self._lock = threading.RLock()
        self.path = Path(log_dir) / f"operations_{_filename_timestamp(self.server_started_at)}.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_document()

    def record_algo_started(
        self,
        command: Dict[str, Any],
        *,
        strategy_key: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self._record_event(
            "algo_trading_started",
            command,
            strategy_key=strategy_key,
            session_id=session_id,
        )

    def record_algo_ended(
        self,
        command: Dict[str, Any],
        *,
        strategy_key: Optional[str] = None,
        session_id: Optional[str] = None,
        reason: Optional[str] = None,
        cycles: Optional[int] = None,
        published_commands: Optional[int] = None,
    ) -> None:
        details: Dict[str, Any] = {}
        if reason:
            details["reason"] = reason
        if cycles is not None:
            details["cycles"] = cycles
        if published_commands is not None:
            details["published_commands"] = published_commands
        self._record_event(
            "algo_trading_ended",
            command,
            strategy_key=strategy_key,
            session_id=session_id,
            details=details,
        )

    def _record_event(
        self,
        event_name: str,
        command: Dict[str, Any],
        *,
        strategy_key: Optional[str] = None,
        session_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        event: Dict[str, Any] = {
            "event": event_name,
            "recorded_at": _utc_iso(),
            "ui_server_started_at": self.server_started_at_iso,
            "session": self._session(command, strategy_key=strategy_key, session_id=session_id),
            "holdings": self._holdings(),
        }
        if details:
            event["details"] = details

        try:
            with self._lock:
                document = self._read_document()
                document["events"].append(event)
                self._write_document(document)
        except Exception:
            LOGGER.exception("Failed writing operations log path=%s event=%s", self.path, event_name)

    def _ensure_document(self) -> None:
        with self._lock:
            if self.path.exists() and self.path.stat().st_size > 0:
                return
            self._write_document(
                {
                    "ui_server_started_at": self.server_started_at_iso,
                    "events": [],
                }
            )

    def _read_document(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                document = json.load(fh)
        except Exception:
            document = {}
        if not isinstance(document, dict):
            document = {}
        events = document.get("events")
        if not isinstance(events, list):
            events = []
        return {
            "ui_server_started_at": str(document.get("ui_server_started_at") or self.server_started_at_iso),
            "events": events,
        }

    def _write_document(self, document: Dict[str, Any]) -> None:
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2, sort_keys=True)
            fh.write("\n")
        tmp_path.replace(self.path)

    def _session(
        self,
        command: Dict[str, Any],
        *,
        strategy_key: Optional[str],
        session_id: Optional[str],
    ) -> Dict[str, Any]:
        command_id = command.get("command_id")
        out: Dict[str, Any] = {
            "session_id": session_id or command_id or strategy_key,
            "command_id": command_id,
            "strategy_key": strategy_key,
            "trading_mode": command.get("trading_mode"),
            "symbol": command.get("symbol"),
            "end_time_et": command.get("end_time_et"),
        }
        if "fast_trading_test_mode" in command:
            out["fast_trading_test_mode"] = bool(command.get("fast_trading_test_mode"))
        return out

    def _holdings(self) -> Dict[str, Any]:
        if self._snapshots_provider is None:
            return {"source": None, "account_count": 0, "accounts": []}

        try:
            provided = self._snapshots_provider()
            accounts, source = self._normalize_provider_result(provided)
        except Exception as exc:
            LOGGER.exception("Failed loading account snapshots for operations log")
            return {"source": None, "error": str(exc), "account_count": 0, "accounts": []}

        return {
            "source": source,
            "account_count": len(accounts),
            "accounts": [
                self._account_holdings(snapshot)
                for _, snapshot in sorted(accounts.items(), key=self._account_sort_key)
            ],
        }

    @staticmethod
    def _normalize_provider_result(provided: Any) -> Tuple[Dict[str, AccountSnapshot], Optional[str]]:
        if isinstance(provided, tuple) and len(provided) == 2:
            accounts, source = provided
        else:
            accounts, source = provided, None
        if not isinstance(accounts, dict):
            return {}, str(source) if source is not None else None
        return accounts, str(source) if source is not None else None

    @staticmethod
    def _account_sort_key(item: Tuple[str, AccountSnapshot]) -> Tuple[int, str]:
        account_id, snapshot = item
        num_id = getattr(snapshot, "account_num_id", None)
        return (int(num_id) if num_id is not None else 999999, str(account_id))

    @staticmethod
    def _account_holdings(snapshot: AccountSnapshot) -> Dict[str, Any]:
        securities = [
            {
                "symbol": position.symbol,
                "qty": position.qty,
                "avg_price": position.avg_price,
            }
            for position in sorted(snapshot.positions, key=lambda pos: str(pos.symbol))
        ]
        return {
            "account_id": snapshot.account_id,
            "account_num_id": snapshot.account_num_id,
            "cash": snapshot.cash,
            "cash_by_currency": dict(sorted(snapshot.cash_by_currency.items())),
            "securities": securities,
            "ts": snapshot.ts,
            "trading_enabled": snapshot.trading_enabled,
        }
