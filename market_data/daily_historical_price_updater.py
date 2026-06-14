import argparse
import csv
import datetime as dt
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

import requests


YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
CSV_HEADERS = ["trade_date", "symbol", "open", "close", "volume"]


@dataclass(frozen=True)
class DailyPriceRow:
    trade_date: str
    symbol: str
    open_price: float
    adjusted_close: float
    volume: Optional[int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch last-trade-date daily prices (open + adjusted close) and append "
            "missing rows to a CSV file."
        )
    )
    parser.add_argument(
        "--csv-path",
        default=str(Path(__file__).resolve().parent / "historical_prices.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--config-path",
        default=str(Path(__file__).resolve().parent / "md_producer_config.json"),
        help="JSON config path with a `symbols` array.",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Symbols to fetch. If omitted, uses symbols from --config-path.",
    )
    return parser.parse_args()


def load_symbols(config_path: Path, cli_symbols: Optional[List[str]]) -> List[str]:
    if cli_symbols:
        symbols = [str(s).strip().upper() for s in cli_symbols if str(s).strip()]
    else:
        with config_path.open("r", encoding="utf-8") as f:
            config = json.load(f)
        raw_symbols = config.get("symbols", [])
        symbols = [str(s).strip().upper() for s in raw_symbols if str(s).strip()]

    deduped = list(dict.fromkeys(symbols))
    if not deduped:
        raise ValueError("No symbols provided. Pass --symbols or set `symbols` in config.")
    return deduped


def _safe_float(values: object, index: int) -> Optional[float]:
    if not isinstance(values, list) or index < 0 or index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(values: object, index: int) -> Optional[int]:
    if not isinstance(values, list) or index < 0 or index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fetch_chart_payload_with_retry(
    session: requests.Session, symbol: str, max_attempts: int = 6
) -> dict:
    url = YF_CHART_URL.format(symbol=symbol)
    params = {
        "interval": "1d",
        "range": "5d",
        "events": "div,splits",
    }

    for attempt in range(1, max_attempts + 1):
        resp = session.get(url, params=params, timeout=20)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait_s = float(retry_after)
                except ValueError:
                    wait_s = min(30.0, 1.5 * attempt)
            else:
                wait_s = min(30.0, 1.5 * attempt + random.uniform(0.0, 0.8))
            if attempt == max_attempts:
                resp.raise_for_status()
            time.sleep(wait_s)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"Failed to fetch Yahoo chart payload for {symbol}")


def fetch_recent_daily_rows(session: requests.Session, symbol: str) -> List[DailyPriceRow]:
    payload = _fetch_chart_payload_with_retry(session, symbol)
    chart = payload.get("chart", {})
    error_obj = chart.get("error")
    if error_obj:
        raise RuntimeError(f"Yahoo returned error for {symbol}: {error_obj}")

    result_list = chart.get("result")
    if not isinstance(result_list, list) or not result_list:
        raise RuntimeError(f"No chart result for {symbol}")

    result = result_list[0]
    timestamps = result.get("timestamp")
    indicators = result.get("indicators", {})
    quote_list = indicators.get("quote")
    adjclose_list = indicators.get("adjclose")

    if not isinstance(timestamps, list) or not timestamps:
        raise RuntimeError(f"No daily bars returned for {symbol}")
    if not isinstance(quote_list, list) or not quote_list:
        raise RuntimeError(f"No quote block returned for {symbol}")
    if not isinstance(adjclose_list, list) or not adjclose_list:
        raise RuntimeError(f"No adjusted close block returned for {symbol}")

    quote = quote_list[0] if isinstance(quote_list[0], dict) else {}
    adjclose = adjclose_list[0] if isinstance(adjclose_list[0], dict) else {}
    opens = quote.get("open")
    volumes = quote.get("volume")
    adj_closes = adjclose.get("adjclose")

    out_rows: List[DailyPriceRow] = []
    for i in range(len(timestamps)):
        open_price = _safe_float(opens, i)
        close_price = _safe_float(adj_closes, i)
        if open_price is None or close_price is None:
            continue

        ts = timestamps[i]
        if ts is None:
            continue
        trade_date = dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).date().isoformat()
        volume = _safe_int(volumes, i)
        out_rows.append(
            DailyPriceRow(
                trade_date=trade_date,
                symbol=symbol,
                open_price=open_price,
                adjusted_close=close_price,
                volume=volume,
            )
        )

    if not out_rows:
        raise RuntimeError(f"No valid open/adjusted close bar found for {symbol}")
    return out_rows


def pick_latest_common_trade_date(rows_by_symbol: List[Tuple[str, List[DailyPriceRow]]]) -> str:
    common_dates: Optional[Set[str]] = None
    for _, rows in rows_by_symbol:
        symbol_dates = {row.trade_date for row in rows}
        if common_dates is None:
            common_dates = symbol_dates
        else:
            common_dates = common_dates.intersection(symbol_dates)

    if not common_dates:
        raise RuntimeError("No common trade date exists across the requested symbols.")
    return max(common_dates)


def row_for_trade_date(rows: List[DailyPriceRow], trade_date: str) -> Optional[DailyPriceRow]:
    for row in rows:
        if row.trade_date == trade_date:
            return row
    return None


def read_existing_keys(csv_path: Path) -> Set[Tuple[str, str]]:
    if not csv_path.exists():
        return set()

    keys: Set[Tuple[str, str]] = set()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trade_date = str(row.get("trade_date", "")).strip()
            symbol = str(row.get("symbol", "")).strip().upper()
            if trade_date and symbol:
                keys.add((trade_date, symbol))
    return keys


def ensure_csv(csv_path: Path) -> None:
    if csv_path.exists():
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)


def append_rows(csv_path: Path, rows: Iterable[DailyPriceRow]) -> int:
    count = 0
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(
                [
                    row.trade_date,
                    row.symbol,
                    f"{row.open_price:.6f}",
                    f"{row.adjusted_close:.6f}",
                    "" if row.volume is None else row.volume,
                ]
            )
            count += 1
    return count


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv_path).resolve()
    config_path = Path(args.config_path).resolve()
    symbols = load_symbols(config_path=config_path, cli_symbols=args.symbols)

    session = requests.Session()
    # A browser-like user-agent lowers Yahoo anti-bot false positives.
    session.headers.update(
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KTrader/1.0"}
    )
    rows_by_symbol: List[Tuple[str, List[DailyPriceRow]]] = []
    for symbol in symbols:
        rows = fetch_recent_daily_rows(session, symbol)
        rows_by_symbol.append((symbol, rows))
        # Small inter-request spacing to reduce 429 bursts.
        time.sleep(0.25)

    target_trade_date = pick_latest_common_trade_date(rows_by_symbol)
    print(f"Target trade date: {target_trade_date}")

    fetched_rows: List[DailyPriceRow] = []
    for symbol, rows in rows_by_symbol:
        row = row_for_trade_date(rows, target_trade_date)
        if row is None:
            raise RuntimeError(
                f"{symbol} has no daily bar for target trade date {target_trade_date}."
            )
        fetched_rows.append(row)
        print(
            f"Fetched {symbol}: trade_date={row.trade_date}, "
            f"open={row.open_price:.6f}, adjusted_close={row.adjusted_close:.6f}, "
            f"volume={row.volume if row.volume is not None else 'NA'}"
        )

    # Common daily run behavior: if all target rows already exist, do nothing.
    existing_keys = read_existing_keys(csv_path)
    target_keys = {(r.trade_date, r.symbol) for r in fetched_rows}
    if target_keys.issubset(existing_keys):
        target_dates = sorted({r.trade_date for r in fetched_rows})
        print(
            "Data already exists for target date(s): "
            + ", ".join(target_dates)
            + ". No rows appended."
        )
        return

    ensure_csv(csv_path)
    missing_rows = [r for r in fetched_rows if (r.trade_date, r.symbol) not in existing_keys]
    appended = append_rows(csv_path, missing_rows)
    appended_dates = sorted({r.trade_date for r in missing_rows})
    print(
        f"Appended {appended} row(s) to {csv_path} for date(s): " + ", ".join(appended_dates)
    )


if __name__ == "__main__":
    main()
