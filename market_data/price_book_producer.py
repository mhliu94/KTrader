import json
import logging
import os
import time
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, List
from kafka import KafkaProducer

import requests
try:
    from .data_types import PriceLevel, PriceBookSide, PriceBook
    from .trading_volume import TradingVolumePublisher, daily_volume, normalized_symbols, select_quote, trade_timestamp, utc_iso
except ImportError:  # Executed directly by start_tiger_listener.sh / a service.
    from data_types import PriceLevel, PriceBookSide, PriceBook
    from trading_volume import TradingVolumePublisher, daily_volume, normalized_symbols, select_quote, trade_timestamp, utc_iso
from pathlib import Path


TS_API_BASE = "https://api.tradestation.com"
TS_OAUTH_TOKEN_URL = "https://signin.tradestation.com/oauth/token"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
LOGGER = logging.getLogger("market-data.price-book-producer")

_day_volume_by_symbol: Dict[str, int] = {}
_day_volume_lock = threading.Lock()
_depth_book_cache: Dict[str, Dict[str, Any]] = {}
_depth_book_lock = threading.Lock()


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )


@dataclass
class OAuthConfig:
    client_id: str  # This is just the API key
    client_secret: Optional[str]  # required for standard auth-code flow; optional for PKCE
    refresh_token: str


class TokenManager:
    """
    Minimal refresh-token based auth.
    You can replace this with your existing auth flow; just provide get_access_token().
    """
    def __init__(self, cfg: OAuthConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._access_token: Optional[str] = None
        self._access_token_expires_at: float = 0.0  # epoch seconds

    def get_access_token(self) -> str:
        # Refresh a bit early.
        if self._access_token and time.time() < self._access_token_expires_at - 30:
            return self._access_token
        with self._lock:
            if self._access_token and time.time() < self._access_token_expires_at - 30:
                return self._access_token
            self._refresh()
            assert self._access_token is not None
            return self._access_token

    def _refresh(self) -> None:
        data = {
            "grant_type": "refresh_token",
            "client_id": self.cfg.client_id,
            "refresh_token": self.cfg.refresh_token,
        }
        if self.cfg.client_secret:
            data["client_secret"] = self.cfg.client_secret

        resp = requests.post(
            TS_OAUTH_TOKEN_URL,
            data=data,
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()

        self._access_token = payload["access_token"]
        # TradeStation returns expires_in (seconds) for access_token in typical OAuth flows.
        expires_in = int(payload.get("expires_in", 20 * 60))  # fallback: 20 min
        self._access_token_expires_at = time.time() + expires_in

        # If TS is configured for rotating refresh tokens, update it.
        new_refresh = payload.get("refresh_token")
        if new_refresh:
            self.cfg.refresh_token = new_refresh


class TSHttpStream:
    """
    Generic TradeStation HTTP streaming client:
    - Handles chunked transfer encoding
    - Buffers across chunks and splits JSON objects by newline
    - Detects StreamStatus (EndSnapshot/GoAway) and errors
    """
    def __init__(
        self,
        token_manager: TokenManager,
        on_message: Callable[[Dict[str, Any]], None],
        on_error: Optional[Callable[[Exception], None]] = None,
        request_timeout_s: int = 30,
    ):
        self.tm = token_manager
        self.on_message = on_message
        self.on_error = on_error or (lambda e: None)
        self.request_timeout_s = request_timeout_s
        self._stop = threading.Event()
        self.seq_num = 1
        self.feed_id = uuid.uuid4().hex

    def stop(self) -> None:
        self._stop.set()

    def stream_market_depth_quotes(self, symbol: str) -> None:
        url = f"{TS_API_BASE}/v3/marketdata/stream/marketdepth/quotes/{symbol}"
        self._run_stream(url, symbol)

    def stream_market_depth_aggregates(self, symbol: str) -> None:
        url = f"{TS_API_BASE}/v3/marketdata/stream/marketdepth/aggregates/{symbol}"
        self._run_stream(url, symbol)

    def _run_stream(self, url: str, symbol: str) -> None:
        backoff = 1.0
        session = requests.Session()

        while not self._stop.is_set():
            try:
                token = self.tm.get_access_token()
                headers = {
                    "Authorization": f"Bearer {token}",
                    # TS streams return Content-Type like application/vnd.tradestation.streams.*+json
                    "Accept": "application/json",
                    "Connection": "keep-alive",
                }

                with session.get(url, headers=headers, stream=True, timeout=self.request_timeout_s) as r:
                    r.raise_for_status()
                    self.feed_id = uuid.uuid4().hex
                    self.seq_num = 1
                    backoff = 1.0  # reset after successful connect

                    buffer = ""
                    for chunk in r.iter_content(chunk_size=4096, decode_unicode=True):
                        if self._stop.is_set():
                            return
                        if not chunk:
                            continue

                        if isinstance(chunk, bytes):
                            chunk = chunk.decode("utf-8")
                        buffer += chunk

                        # TradeStation docs note newline-delimited JSON objects (but chunk boundaries are arbitrary).
                        while True:
                            nl = buffer.find("\n")
                            if nl == -1:
                                break
                            line = buffer[:nl].strip()
                            buffer = buffer[nl + 1 :]

                            if not line:
                                continue

                            obj = json.loads(line)

                            # StreamStatus is used by some v3 streams:
                            # {"StreamStatus":"EndSnapshot"} then later updates; {"StreamStatus":"GoAway"} => restart.
                            status = obj.get("StreamStatus")
                            obj["Symbol"] = symbol  # TS doesn't provide symbol in their API, so set it here
                            obj["SequenceNumber"] = self.seq_num
                            obj["FeedID"] = self.feed_id
                            self.seq_num += 1
                            if status == "GoAway":
                                raise RuntimeError("Server requested stream restart (GoAway).")
                            if status in ("EndSnapshot",):
                                # optional: you can emit it or ignore it
                                self.on_message(obj)
                                continue

                            # Some errors appear as JSON objects like {"Symbol":"AAPL","Error":"DualLogon"}.
                            if "Error" in obj:
                                raise RuntimeError(f"Stream error object: {obj}")

                            self.on_message(obj)

            except requests.HTTPError as e:
                # If token expired, refresh and retry quickly; otherwise backoff.
                self.on_error(e)
            except (requests.RequestException, json.JSONDecodeError, RuntimeError) as e:
                self.on_error(e)

            # Reconnect with exponential backoff (cap it).
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _extract_day_volume_from_obj(obj: Dict[str, Any]) -> Optional[int]:
    # v3 Quote.Volume is cumulative daily volume; LastSize/TradeVolume are not.
    try:
        return daily_volume(obj.get("Volume"))
    except ValueError:
        return None


class TSQuotePoller:
    """
    Poll TradeStation quote endpoint periodically and cache day volume per symbol.
    """
    def __init__(
        self,
        token_manager: TokenManager,
        symbols: List[str],
        on_error: Optional[Callable[[Exception], None]] = None,
        poll_interval_s: float = 5.0,
        request_timeout_s: int = 15,
        volume_publisher: Optional[TradingVolumePublisher] = None,
    ):
        self.tm = token_manager
        self.symbols = normalized_symbols(symbols)
        self.volume_publisher = volume_publisher
        self.on_error = on_error or (lambda e: None)
        self.poll_interval_s = poll_interval_s
        self.request_timeout_s = request_timeout_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ts-quote-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        with requests.Session() as session:
            while not self._stop.is_set():
                self.poll_once(session)
                self._stop.wait(self.poll_interval_s)

    def poll_once(self, session: requests.Session) -> None:
        # Official v3 GetQuoteSnapshots allows up to 100 comma-separated
        # symbols. Reuse each snapshot for the book cache and volume topic.
        for offset in range(0, len(self.symbols), 100):
            if self._stop.is_set():
                return
            symbols = self.symbols[offset:offset + 100]
            started, timer = time.time(), time.monotonic()
            try:
                payload = self._fetch_quotes(session, symbols)
            except Exception as error:
                observed = time.time()
                duration = (time.monotonic() - timer) * 1000
                self.on_error(error)
                for symbol in symbols:
                    if self.volume_publisher:
                        try:
                            self.volume_publisher.publish_error(
                                symbol, "quote request failed: " + type(error).__name__,
                                request_started_at=started, observed_at=observed,
                                request_duration_ms=duration)
                        except Exception as publish_error:
                            self.on_error(publish_error)
                continue
            observed = time.time()
            duration = (time.monotonic() - timer) * 1000
            for symbol in symbols:
                try:
                    quote = select_quote(payload, symbol)
                except ValueError as error:
                    self.on_error(error)
                    if self.volume_publisher:
                        try:
                            self.volume_publisher.publish_error(
                                symbol, str(error), request_started_at=started,
                                observed_at=observed, request_duration_ms=duration)
                        except Exception as publish_error:
                            self.on_error(publish_error)
                    continue
                day_volume = _extract_day_volume_from_obj(quote)
                if day_volume is not None:
                    with _day_volume_lock:
                        _day_volume_by_symbol[symbol] = day_volume
                if self.volume_publisher:
                    try:
                        self.volume_publisher.publish_quote(
                            symbol, quote, request_started_at=started,
                            observed_at=observed, request_duration_ms=duration)
                    except Exception as error:
                        self.on_error(error)

    def _fetch_quotes(self, session: requests.Session, symbols: List[str]) -> Dict[str, Any]:
        from urllib.parse import quote
        encoded_symbols = quote(",".join(symbols), safe=",.$@_-" )
        url = f"{TS_API_BASE}/v3/marketdata/quotes/{encoded_symbols}"
        token = self.tm.get_access_token()
        response = session.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self.request_timeout_s,
        )
        response.raise_for_status()
        return response.json()

    def _fetch_day_volume(self, session: requests.Session, symbol: str) -> Optional[int]:
        # Kept for callers of the old single-symbol helper.
        quote = select_quote(self._fetch_quotes(session, [symbol]), symbol)
        return _extract_day_volume_from_obj(quote)


def _publish_depth_heartbeat(evt: Dict[str, Any]) -> None:
    """Only an actual documented TS depth heartbeat can refresh an idle book.

    https://api.tradestation.com/docs/specification/ documents Heartbeat2 with
    Timestamp (RFC3339), sent after five seconds on an idle aggregate stream.
    A fresh connection cannot reuse a snapshot from its previous connection.
    """
    symbol = str(evt.get("Symbol") or "").strip().upper()
    feed_id = str(evt.get("FeedID") or "")
    with _depth_book_lock:
        cached = _depth_book_cache.get(symbol)
        if not cached or not feed_id or cached.get("feed_id") != feed_id:
            return
        payload = dict(cached)
    observed = time.time()
    try:
        source_time = trade_timestamp(evt.get("Timestamp"))
    except ValueError:
        source_time = None
    healthy = source_time is not None and -1 <= observed - source_time <= 10
    payload.update(kind="PRICE_BOOK_HEARTBEAT", feed_id=feed_id,
                   book_observed_at=utc_iso(source_time) if source_time is not None else None,
                   observed_at=utc_iso(observed), healthy=healthy,
                   sequence_num=evt.get("SequenceNumber"))
    if source_time is not None:
        payload["update_time"] = int(source_time * 1e9)
    try:
        producer.send(topic=kafka_topic, key="price-book", value=json.dumps(payload))
    except Exception as error:
        LOGGER.warning("depth heartbeat publish error %s", type(error).__name__)


# ---------- Example usage ----------
def print_depth_event(evt: Dict[str, Any]) -> None:
    # You'll want to inspect the actual payload fields for your instrument type.
    # For quick visibility:
    # print(evt)
    if "Heartbeat" in evt:
        _publish_depth_heartbeat(evt)
        return
    if evt.get("StreamStatus"):
        return

    update_time = int(time.time() * 1e9) # epoch nanos

    try:
        symbol = str(evt.get("Symbol", "")).strip().upper()
        if not symbol:
            return

        stream_day_vol = _extract_day_volume_from_obj(evt)
        if stream_day_vol is not None:
            with _day_volume_lock:
                _day_volume_by_symbol[symbol] = stream_day_vol

        with _day_volume_lock:
            day_volume = _day_volume_by_symbol.get(symbol)

        bl_list = list()
        if "Bids" in evt:
            bid_levels = evt["Bids"]
            levels_from_best = 0
            for bid_level in bid_levels:
                bl = PriceLevel(
                    bid_level["Price"],
                    bid_level["TotalSize"],
                    bid_level["TotalOrderCount"],
                    levels_from_best)
                bl_list.append(bl)
                levels_from_best += 1
        bid_side = PriceBookSide(True, update_time, bl_list)

        al_list = list()
        if "Asks" in evt:
            ask_levels = evt["Asks"]
            levels_from_best = 0
            for ask_level in ask_levels:
                al = PriceLevel(
                    ask_level["Price"],
                    ask_level["TotalSize"],
                    ask_level["TotalOrderCount"],
                    levels_from_best)
                al_list.append(al)
                levels_from_best += 1
        ask_side = PriceBookSide(False, update_time, al_list)
        curr_book = PriceBook(symbol, bid_side, ask_side, update_time, evt["SequenceNumber"])
        payload = json.loads(curr_book.to_json())
        payload.update(kind="PRICE_BOOK", feed_id=str(evt.get("FeedID") or "legacy"),
                       book_observed_at=utc_iso(update_time / 1e9),
                       observed_at=utc_iso(update_time / 1e9), source="TradeStation")
        if day_volume is not None:
            payload["volume"] = day_volume
        with _depth_book_lock:
            _depth_book_cache[symbol] = dict(payload)

        producer.send(
            topic=kafka_topic,
            key="price-book",
            value=json.dumps(payload)
        )
    except Exception as e:
        LOGGER.warning("publish error %s: %s", type(e).__name__, e)


def print_err(e: Exception) -> None:
    LOGGER.warning("stream error %s: %s", type(e).__name__, e)


def sanity_check_quote(ts_api_base: str, access_token: str):
    url = f"{ts_api_base}/v3/marketdata/quotes/AAPL"
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    LOGGER.info("status: %s", r.status_code)
    LOGGER.info("body: %s", r.text[:500])
    r.raise_for_status()


if __name__ == "__main__":
    configure_logging()
    script_path = Path(__file__)
    script_dir = script_path.parent
    config_path = script_dir / 'md_producer_config.json'

    with config_path.open('r') as config_file:
        content = config_file.read()
        json_content = json.loads(content)
        kafka_server_ip = json_content["kafka_server_ip"]
        kafka_port = json_content["kafka_port"]
        kafka_topic = json_content["kafka_topic"]
        trading_symbols = json_content["symbols"]
        trading_symbols = [str(s).strip() for s in trading_symbols if str(s).strip()]
        trading_symbols = normalized_symbols(trading_symbols)  # dedupe, preserve order
        if not trading_symbols:
            raise ValueError("Config 'symbols' must contain at least one symbol.")
        volume_symbols = normalized_symbols(json_content.get("trading_volume_symbols", []))
        volume_topic = str(json_content.get("trading_volume_topic", "trading-volume")).strip()
        volume_poll_interval = float(json_content.get("trading_volume_poll_interval_seconds", 5))
        if not 1 <= volume_poll_interval <= 300:
            raise ValueError("trading_volume_poll_interval_seconds must be between 1 and 300")

    producer = KafkaProducer(
        bootstrap_servers=f"{kafka_server_ip}:{kafka_port}",
        value_serializer=lambda v: v.encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
        acks="all",  # wait for full commit
        retries=3
    )

    tm = TokenManager(
        OAuthConfig(
            client_id="0ASr6k5r5HjdzRoDVRLDvGZizAJEM5mn",
            client_secret="B_FhjW3h5wtecxgKnn1KHPE3L_Ph7X-DJOCCUEnuXVdLNClGgzTAlzwG2pFJedVv",
            refresh_token="88GMk1FJxRHN8ZiVCzlQHup_y7zuzFCzfRkYD93wYvcId",
        )
    )

    streams: List[TSHttpStream] = []
    threads: List[threading.Thread] = []
    volume_publisher = TradingVolumePublisher(producer, volume_symbols, topic=volume_topic,
                                               max_request_seconds=max(10, 2 * volume_poll_interval))
    quote_symbols = list(dict.fromkeys(trading_symbols + volume_symbols))
    quote_poller = TSQuotePoller(token_manager=tm, symbols=quote_symbols, on_error=print_err,
                                poll_interval_s=volume_poll_interval,
                                volume_publisher=volume_publisher)
    try:
        quote_poller.start()
        LOGGER.info("Started quote poller; volume topic=%s, volume symbols=%s", volume_topic, volume_symbols)

        for symbol in trading_symbols:
            stream = TSHttpStream(token_manager=tm, on_message=print_depth_event, on_error=print_err)
            thread = threading.Thread(
                target=stream.stream_market_depth_aggregates,
                args=(symbol,),
                name=f"ts-depth-{symbol}",
                daemon=True,
            )
            streams.append(stream)
            threads.append(thread)
            thread.start()
            LOGGER.info("Started market depth stream for %s", symbol)

        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        LOGGER.info("Stopping market depth streams...")
    finally:
        quote_poller.stop()
        for stream in streams:
            stream.stop()
        for thread in threads:
            thread.join(timeout=5.0)
        producer.flush()
        producer.close()
