import os
import json
import time
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import ccxt

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

__version__ = "0.1.0"


class BybitVWAPStrategy:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbol: str = "BTCUSDT",
        direction: str = "Long",
        testnet: bool = True,
    ):
        self.direction = (direction or "LONG").strip().upper()
        if self.direction in {"L", "LONG"}:
            self.direction = "LONG"
        elif self.direction in {"S", "SHORT"}:
            self.direction = "SHORT"
        assert self.direction in {"LONG", "SHORT"}

        self.exchange = ccxt.bybit(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                # ВАЖНО: это фьючерсы (perpetual swaps), не spot
                # defaultSubType/settle закрепляет именно USDT linear swap, если у Bybit есть совпадающие id на spot.
                "options": {"defaultType": "swap", "defaultSubType": "linear", "defaultSettle": "USDT"},
                "timeout": 30000,
            }
        )
        self.exchange.set_sandbox_mode(testnet)
        self.exchange.load_markets()

        self.input_symbol = symbol
        self.market = self._resolve_market(symbol)
        self.symbol = self.market["symbol"]  # CCXT symbol
        self.api_symbol = self.market.get("id") or symbol  # Bybit API symbol/id (e.g., BTCUSDT)

        # Display symbol
        self.base_symbol = self.market["symbol"]

        self.testnet = testnet
        self.poll_interval = 7

        self.client_prefix = f"VWAP_{self.direction[0]}_{int(time.time())}_{str(uuid.uuid4())[:8]}"

        safe_symbol = (
            self.base_symbol.replace("/", "")
            .replace(":", "_")
            .replace(" ", "")
            .replace("-", "_")
        )
        self.state_file = f"strategy_state_{self.direction}_{safe_symbol}.json"
        self.config_file = f"strategy_config_{self.direction}_{safe_symbol}.json"

        self.load_state()
        if not self.load_config():
            self.configure_strategy()

        self.set_margin_and_leverage()
        self.klines_data: List[Dict[str, Any]] = []
        self.cum_pv = self.cum_vol = 0.0
        self.last_anchor_time: Optional[datetime] = None

        logger.info("Запуск бота — синхронизация состояния с биржей...")
        self.sync_state_with_exchange()

    def _has_exchange_position(self) -> bool:
        """Проверка фактической позиции на бирже по направлению (LONG/SHORT)."""
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            for p in positions:
                side_raw = (p.get("side") or "").lower()
                side = "buy" if side_raw in {"buy", "long"} else "sell" if side_raw in {"sell", "short"} else ""
                qty = float(p.get("contracts", 0) or 0)
                if qty <= 1e-6:
                    continue
                if (side == "buy" and self.direction == "LONG") or (side == "sell" and self.direction == "SHORT"):
                    return True
            return False
        except Exception:
            # В случае ошибки не делаем ложных выводов
            return False

    @staticmethod
    def _is_order_not_found(err: Exception) -> bool:
        """Грубая эвристика: Bybit/CCXT часто возвращают 'not found' разными текстами/кодами."""
        s = str(err).lower()
        needles = [
            "order not found",
            "not found",
            "does not exist",
            "unknown order",
            "110001",  # bybit: order not exists (часто)
            "order does not exist",
        ]
        return any(n in s for n in needles)

    def _sync_fills_from_trades(self) -> bool:
        """Подтверждает исполнения входных ордеров через историю сделок.

        Это надёжнее, чем ориентироваться только на исчезновение из open orders или fetch_order(),
        которое у Bybit/CCXT иногда может падать/возвращать неполные данные.
        """
        since = int(self.state.get("last_trade_ts") or 0)
        if since > 0:
            since = max(0, since - 60_000)  # небольшой overlap

        try:
            trades = self.exchange.fetch_my_trades(self.symbol, since=since, limit=200, params={"type": "swap"})
        except Exception:
            try:
                trades = self.exchange.fetch_my_trades(self.symbol, since=since, limit=200)
            except Exception:
                return False

        max_ts = int(self.state.get("last_trade_ts") or 0)
        updated = False

        for t in trades or []:
            ts = int(t.get("timestamp") or 0)
            if ts > max_ts:
                max_ts = ts
            order_id = t.get("order") or (t.get("info") or {}).get("orderId")
            if not order_id:
                continue

            for key, pos in self.state.get("positions", {}).items():
                if not isinstance(pos, dict):
                    continue
                if pos.get("active") or pos.get("filled"):
                    continue
                if pos.get("entry_order_id") != order_id:
                    continue

                pos["active"] = True
                pos["filled"] = True
                pos["filled_entry_order_id"] = order_id
                pos.pop("entry_order_id", None)
                logger.info(f"ВХОД {key} ПОДТВЕРЖДЁН ПО СДЕЛКАМ (orderId={order_id})")
                updated = True
                break

        if max_ts and max_ts != int(self.state.get("last_trade_ts") or 0):
            self.state["last_trade_ts"] = max_ts
            self.save_state()

        return updated

    def _mark_level_filled(self, key: str, pos: Dict[str, Any], order_id: str, reason: str):
        """Единая точка фиксации 'уровень сработал'."""
        pos["active"] = True
        pos["filled"] = True
        pos["filled_entry_order_id"] = order_id
        pos.pop("entry_order_id", None)
        pos.pop("entry_order_ts", None)
        logger.info(f"ВХОД {key} ПОДТВЕРЖДЁН ({reason})")

    def _resolve_market(self, symbol: str) -> Dict[str, Any]:
        """Resolve a market whether user passes CCXT symbol or Bybit id.

        Accepts e.g. "BTCUSDT" (id) or "BTC/USDT:USDT" (CCXT symbol).
        """
        if symbol in self.exchange.markets:
            return self.exchange.market(symbol)

        markets_by_id = getattr(self.exchange, "markets_by_id", {}) or {}
        if symbol in markets_by_id:
            m = markets_by_id[symbol]
            # ccxt may store list under markets_by_id
            if isinstance(m, list):
                # Bybit часто имеет одинаковый id для spot и swap (например BTCUSDT).
                # Нам нужен именно USDT linear swap (contract).
                preferred = []
                for mm in m:
                    if not isinstance(mm, dict):
                        continue
                    if mm.get("swap") or mm.get("type") == "swap" or mm.get("contract") is True:
                        # prefer linear USDT-settled swaps
                        if (mm.get("linear") is True) or (str(mm.get("settle", "")).upper() == "USDT"):
                            preferred.append(mm)
                if preferred:
                    # если несколько, берём самый первый подходящий
                    return preferred[0]
                # fallback: любой swap/contract
                for mm in m:
                    if isinstance(mm, dict) and (mm.get("swap") or mm.get("type") == "swap" or mm.get("contract") is True):
                        return mm
                # last resort
                return m[0]
            return m

        # Fallback: brute search by id (case-insensitive)
        s = symbol.upper()
        for m in self.exchange.markets.values():
            if str(m.get("id", "")).upper() == s:
                return m

        raise ValueError(
            f"Unknown symbol '{symbol}'. Try CCXT symbol like 'BTC/USDT:USDT' or Bybit id like 'BTCUSDT'."
        )

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
                logger.info(f"Состояние загружено → {self.state_file}")
            except Exception:
                self.state = {"positions": {}, "last_candle_time": None}
        else:
            self.state = {"positions": {}, "last_candle_time": None}
        # Миграция/нормализация полей состояния
        if "positions" not in self.state or not isinstance(self.state["positions"], dict):
            self.state["positions"] = {}
        if "last_trade_ts" not in self.state:
            # ms timestamp для инкрементальной синхронизации сделок
            self.state["last_trade_ts"] = 0
        for _, pos in self.state["positions"].items():
            if not isinstance(pos, dict):
                continue
            if "filled" not in pos:
                # если уровень уже активен, считаем что вход был (иначе на рестарте будет ставить лимитки снова)
                pos["filled"] = bool(pos.get("active", False))

    def save_state(self):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, default=str, indent=2)

    def load_config(self) -> bool:
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                for k, v in cfg.items():
                    setattr(self, k, v)
                logger.info(f"Конфиг загружен → {self.config_file}")
                print(f"Конфиг загружен. Удалите {self.config_file} для изменения.")
                return True
            except Exception as e:
                logger.error(f"Ошибка конфига: {e}")
        return False

    def save_config(self):
        attrs = [
            "levels_pct",
            "timeframe",
            "sl_pct",
            "tp_pct",
            "entry_size_usdt",
            "margin_mode",
            "leverage",
            "anchor_period",
            "approach_distance_pct",
        ]
        cfg = {k: getattr(self, k) for k in attrs if hasattr(self, k)}
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def configure_strategy(self):
        print(f"\n=== НАСТРОЙКА VWAP {self.direction} {self.base_symbol} ===")
        defaults = [0.3, 0.7, 1.2, 2.0]
        self.levels_pct = [
            float(input(f"Уровень {i} % (ум. {v}): ") or v) for i, v in enumerate(defaults, 1)
        ]
        self.timeframe = input("Таймфрейм (ум. 3m): ") or "3m"
        self.sl_pct = float(input("SL % от VWAP (ум. 2.5): ") or 2.5)
        self.tp_pct = float(input("TP % от VWAP (0 = на VWAP, ум. 0): ") or 0)
        self.entry_size_usdt = float(input("Размер входа USDT (ум. 30): ") or 30)
        self.margin_mode = input("Маржа isolated/cross (ум. isolated): ") or "isolated"
        self.leverage = int(input("Плечо (ум. 6): ") or 6)
        self.approach_distance_pct = float(input("Расстояние входа % (ум. 0.1): ") or 0.1)
        self.anchor_period = input("Якорь Session/Week/Month/Year (ум. Session): ") or "Session"
        self.save_config()

    def set_margin_and_leverage(self):
        try:
            self.exchange.set_margin_mode(self.margin_mode, self.symbol)
        except Exception:
            pass
        try:
            self.exchange.set_leverage(self.leverage, self.symbol)
        except Exception:
            pass

    def cancel_all_pending_entries(self):
        cancelled = 0
        try:
            open_orders = self.exchange.fetch_open_orders(self.symbol)
            for order in open_orders:
                cid = order.get("clientOrderId", "")
                if not cid or self.client_prefix not in cid or "_E" not in cid:
                    continue

                found = False
                for key, pos in self.state["positions"].items():
                    if pos.get("entry_order_id") == order["id"]:
                        if pos.get("active", False):
                            found = True
                            break

                        try:
                            self.exchange.cancel_order(order["id"], self.symbol)
                            cancelled += 1
                            pos.pop("entry_order_id", None)
                            logger.info(f"Отменена устаревшая лимитка {key}")
                        except Exception:
                            pass

                        found = True
                        break

                if not found:
                    try:
                        self.exchange.cancel_order(order["id"], self.symbol)
                        cancelled += 1
                    except Exception:
                        pass

            if cancelled:
                logger.info(f"Отменено {cancelled} устаревших входных лимиток")
        except Exception as e:
            logger.error(f"Ошибка при отмене лимиток: {e}")

    def cancel_all_pending_exits(self):
        """Отменяет TP/SL ордера, созданные этим запуском бота (по client_prefix)."""
        cancelled = 0
        try:
            open_orders = self.exchange.fetch_open_orders(self.symbol)
            for order in open_orders:
                cid = order.get("clientOrderId", "") or ""
                if not cid or self.client_prefix not in cid:
                    continue
                if "_TP" not in cid and "_SL" not in cid:
                    continue

                oid = order.get("id")
                if not oid:
                    continue

                try:
                    self.exchange.cancel_order(oid, self.symbol)
                    cancelled += 1
                except Exception:
                    pass

                # Почистим state, если там были эти id
                for pos in self.state.get("positions", {}).values():
                    if pos.get("tp_order_id") == oid:
                        pos["tp_order_id"] = None
                    if pos.get("sl_order_id") == oid:
                        pos["sl_order_id"] = None

            if cancelled:
                logger.info(f"Отменено {cancelled} TP/SL ордеров бота")
        except Exception as e:
            logger.error(f"Ошибка при отмене TP/SL: {e}")

    def fetch_klines(self, limit: int = 500):
        try:
            ohlcv = self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=limit)
            klines = [
                {
                    "timestamp": datetime.fromtimestamp(c[0] / 1000),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                }
                for c in ohlcv
            ]
            klines.sort(key=lambda x: x["timestamp"])
            self.klines_data = klines
            return klines
        except Exception:
            return []

    def calculate_vwap(self, klines: List[Dict[str, Any]]) -> float:
        if not klines:
            return float("nan")

        t = klines[-1]["timestamp"]
        anchor = self.get_anchor_start(t)
        if self.last_anchor_time is None or self.is_new_anchor_period(t):
            self.cum_pv = self.cum_vol = 0.0
            self.last_anchor_time = anchor
            logger.info("Новый якорный период")
            self.cancel_all_pending_entries()

        period = [k for k in klines if k["timestamp"] >= self.last_anchor_time]
        self.cum_pv = sum(((k["high"] + k["low"] + k["close"]) / 3) * k["volume"] for k in period)
        self.cum_vol = sum(k["volume"] for k in period)
        return self.cum_pv / self.cum_vol if self.cum_vol > 0 else float("nan")

    def get_levels(self, vwap: float) -> Dict[str, Any]:
        if self.direction == "LONG":
            entry = [vwap * (1 - p / 100) for p in self.levels_pct]
            tp = vwap * (1 + self.tp_pct / 100)
            sl = vwap * (1 - self.sl_pct / 100)
        else:
            entry = [vwap * (1 + p / 100) for p in self.levels_pct]
            tp = vwap * (1 - self.tp_pct / 100)
            sl = vwap * (1 + self.sl_pct / 100)
        return {"entry_levels": entry, "tp_price": tp, "sl_price": sl}

    def get_anchor_start(self, t: datetime) -> datetime:
        if self.anchor_period == "Session":
            return t.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.anchor_period == "Week":
            return (t - timedelta(days=t.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.anchor_period == "Month":
            return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if self.anchor_period == "Year":
            return t.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return t

    def is_new_anchor_period(self, t: datetime) -> bool:
        anchor = self.get_anchor_start(t)
        return self.last_anchor_time is None or anchor > self.get_anchor_start(self.last_anchor_time)

    def sync_state_with_exchange(self):
        try:
            positions = self.exchange.fetch_positions([self.symbol])
            total_qty = 0.0
            has_position = False

            for p in positions:
                side_raw = (p.get("side") or "").lower()
                # ccxt/bybit may return side as 'buy'/'sell' or 'long'/'short'
                side = "buy" if side_raw in {"buy", "long"} else "sell" if side_raw in {"sell", "short"} else ""
                qty = float(p.get("contracts", 0) or 0)
                if qty > 1e-6:
                    if (side == "buy" and self.direction == "LONG") or (side == "sell" and self.direction == "SHORT"):
                        total_qty += qty
                        has_position = True

            if has_position:
                active_keys = [k for k, v in self.state["positions"].items() if v.get("active", False)]
                if not active_keys:
                    logger.info("ОБНАРУЖЕНА ОТКРЫТАЯ ПОЗИЦИЯ — ВОССТАНАВЛИВАЕМ ФЛАГ")
                    if "level_1" in self.state["positions"]:
                        self.state["positions"]["level_1"]["active"] = True
                        self.state["positions"]["level_1"]["filled"] = True
                        logger.info("Восстановлен флаг level_1")
                        self.save_state()

            elif total_qty < 1e-6:
                closed_keys = []
                # Позиции на бирже нет: считаем цикл завершённым, сбрасываем уровни,
                # чтобы входы могли ставиться заново ТОЛЬКО после закрытия (TP/SL).
                for key, pos in self.state["positions"].items():
                    if pos.get("active", False) or pos.get("filled", False):
                        logger.info(f"НА БИРЖЕ НЕТ ПОЗИЦИИ — СБРАСЫВАЕМ ЦИКЛ {key}")
                        for oid_key in ["tp_order_id", "sl_order_id"]:
                            order_id = pos.get(oid_key)
                            if order_id:
                                try:
                                    self.exchange.cancel_order(order_id, self.symbol)
                                    logger.info(f"ОТМЕНЁН ОСТАВШИЙСЯ ОРДЕР {oid_key}: {order_id}")
                                except Exception:
                                    pass
                                pos[oid_key] = None
                        pos["active"] = False
                        pos["filled"] = False
                        pos.pop("entry_order_id", None)
                        closed_keys.append(key)

                if closed_keys:
                    self.save_state()
                    logger.info(f"Сброшено {len(closed_keys)} флагов/уровней (позиция закрыта)")

                # Доп. очистка: если позиции нет, но в state остались entry_order_id, которых уже нет на бирже,
                # бот может «залипнуть» и перестать ставить новые лимитки. Чистим осиротевшие ID.
                try:
                    open_orders = self.exchange.fetch_open_orders(self.symbol)
                    open_ids = {o.get("id") for o in open_orders if o.get("id")}
                except Exception:
                    open_ids = None

                if open_ids is not None:
                    orphaned = 0
                    for key, pos in self.state["positions"].items():
                        if not isinstance(pos, dict):
                            continue
                        oid = pos.get("entry_order_id")
                        if not oid:
                            continue
                        if oid in open_ids:
                            continue
                        pos.pop("entry_order_id", None)
                        pos.pop("entry_order_ts", None)
                        orphaned += 1
                    if orphaned:
                        self.save_state()
                        logger.info(f"Очищено {orphaned} осиротевших entry_order_id (позиции нет)")

        except Exception as e:
            logger.error(f"Ошибка синхронизации: {e}")

    def update_tp_sl_for_all(self, vwap: float):
        active_positions = [p for p in self.state["positions"].values() if p.get("active", False)]
        if not active_positions:
            return
        # Не ставим TP/SL, если на бирже фактически нет позиции (защита от ложных активных флагов)
        if not self._has_exchange_position():
            logger.info("TP/SL не выставляем — на бирже нет позиции")
            return

        levels = self.get_levels(vwap)
        tp_price = round(levels["tp_price"], 1)
        sl_price = round(levels["sl_price"], 1)
        side = "Sell" if self.direction == "LONG" else "Buy"

        logger.info(f"ПЕРЕУСТАНОВКА TP/SL → TP={tp_price} | SL={sl_price}")

        # На всякий случай: убираем выходные ордера, созданные этим запуском,
        # даже если state не полностью их отслеживает.
        self.cancel_all_pending_exits()

        # Cancel previous TP/SL
        for key, pos in self.state["positions"].items():
            if not pos.get("active"):
                continue
            for oid_key, label in [("tp_order_id", "TP"), ("sl_order_id", "SL")]:
                order_id = pos.get(oid_key)
                if order_id:
                    try:
                        self.exchange.cancel_order(order_id, self.symbol)
                        logger.info(f"ОТМЕНЁН СТАРЫЙ {label} для {key}: {order_id}")
                    except Exception:
                        pass
                    finally:
                        pos[oid_key] = None

        # Place new conditional TP/SL per filled entry chunk
        for key, pos in self.state["positions"].items():
            if not pos.get("active"):
                continue
            qty = str(pos["qty"])

            for target_price, direction, label in [
                (tp_price, 1 if self.direction == "LONG" else 2, "TP"),
                (sl_price, 2 if self.direction == "LONG" else 1, "SL"),
            ]:
                try:
                    client_id = f"{self.client_prefix}_{label}{key[-1]}_{int(time.time() * 1000)}"
                    resp = self.exchange.private_post_v5_order_create(
                        {
                            "category": "linear",
                            "symbol": self.api_symbol,
                            "side": side,
                            "orderType": "Limit",
                            "qty": qty,
                            "price": str(target_price),
                            "triggerPrice": str(target_price),
                            "triggerDirection": direction,
                            "triggerBy": "LastPrice",
                            "reduceOnly": True,
                            "closeOnTrigger": True,
                            "clientOrderId": client_id,
                        }
                    )
                    new_id = resp["result"]["orderId"]
                    if label == "TP":
                        pos["tp_order_id"] = new_id
                    else:
                        pos["sl_order_id"] = new_id
                    logger.info(f"{key} → {label} Conditional {target_price} УСТАНОВЛЕН")
                except Exception as e:
                    msg = str(e)
                    if "110009" in msg:
                        logger.warning("ДОСТИГНУТ ЛИМИТ STOP-ОРДЕРОВ — SL/TP НЕ СОЗДАН")
                    elif "trigger_price" in msg.lower():
                        logger.warning(f"{label} не создан — цена уже пройдена")
                    else:
                        logger.error(f"Ошибка создания {label}: {e}")

        self.save_state()

    def place_all_entry_orders(self, entry_levels: List[float]):
        logger.info("Расстановка новых лимиток (с удалением старых по ID)")

        # Снимем текущие открытые ордера, чтобы не «терять» факт исполнения при перевыставлении
        try:
            open_orders = self.exchange.fetch_open_orders(self.symbol)
            open_by_id = {o.get("id"): o for o in open_orders if o.get("id")}
            open_ids = set(open_by_id.keys())
        except Exception:
            open_by_id = {}
            open_ids = set()

        now_ms = int(time.time() * 1000)
        stale_ms = 3 * 60_000  # если статус не подтверждается слишком долго — считаем ID протухшим

        # Cancel old entry orders stored in state
        for key, pos in self.state["positions"].items():
            if pos.get("active", False):
                continue
            # Если уровень уже был исполнен в текущем цикле — не трогаем и не перевыставляем
            if pos.get("filled", False):
                continue
            order_id = pos.get("entry_order_id")
            if order_id:
                # Отменяем только если ордер действительно в открытых.
                if order_id in open_ids:
                    # Если ордер уже частично/полностью исполнился, НЕ перевыставляем его как вход:
                    # считаем уровень сработавшим и отменяем остаток.
                    try:
                        o = open_by_id.get(order_id) or {}
                        filled = float(o.get("filled") or 0)
                    except Exception:
                        filled = 0.0
                    if filled > 0:
                        self._mark_level_filled(key, pos, order_id, f"filled>0 в open_orders ({filled})")
                        try:
                            self.exchange.cancel_order(order_id, self.symbol)
                        except Exception:
                            pass
                        continue
                    try:
                        self.exchange.cancel_order(order_id, self.symbol)
                        logger.info(f"ОТМЕНЕНА СТАРАЯ ЛИМИТКА {key}: {order_id}")
                        pos.pop("entry_order_id", None)
                        pos.pop("entry_order_ts", None)
                    except Exception:
                        # Не удаляем entry_order_id, пока не подтвердим статус
                        logger.warning(f"Не удалось отменить лимитку {key}: {order_id} — статус не подтверждён")
                else:
                    # Ордер не в открытых: проверяем, не был ли он исполнен/отменён.
                    try:
                        o = self.exchange.fetch_order(order_id, self.symbol)
                        status = (o.get("status") or "").lower()
                        filled = float(o.get("filled") or 0)
                        # Важно: уровень считается сработавшим, если filled > 0, даже если ордер ещё может быть open/partial.
                        if filled > 0 and self._has_exchange_position():
                            self._mark_level_filled(key, pos, order_id, f"fetch_order filled={filled} status={status}")
                        elif status in {"canceled", "rejected", "expired"} or (status == "closed" and filled <= 0):
                            pos.pop("entry_order_id", None)
                            pos.pop("entry_order_ts", None)
                        else:
                            # неизвестно — не перевыставляем, чтобы не дублировать
                            logger.info(f"{key}: ордер {order_id} не в open, статус '{status}' — ждём подтверждения")
                    except Exception as e:
                        # ВАЖНО: если биржа говорит "ордера нет" — чистим ID, иначе бот зависнет и перестанет ставить лимитки.
                        if self._is_order_not_found(e):
                            logger.warning(f"{key}: ордер {order_id} не найден — очищаем entry_order_id")
                            pos.pop("entry_order_id", None)
                            pos.pop("entry_order_ts", None)
                        else:
                            # если ID очень старый и статус так и не подтверждается — считаем его протухшим
                            ts = int(pos.get("entry_order_ts") or 0)
                            if ts and (now_ms - ts) > stale_ms:
                                logger.warning(f"{key}: статус ордера {order_id} не подтверждается > {stale_ms/60000:.0f} мин — очищаем")
                                pos.pop("entry_order_id", None)
                                pos.pop("entry_order_ts", None)
                            else:
                                logger.warning(f"{key}: не удалось получить статус ордера {order_id} — ждём подтверждения")

        placed = 0
        for i, price in enumerate(entry_levels):
            key = f"level_{i + 1}"
            existing = self.state["positions"].get(key, {})
            if existing.get("active", False) or existing.get("filled", False):
                continue
            # Если по уровню есть «неподтверждённый» entry_order_id, не создаём новый
            if existing.get("entry_order_id"):
                continue

            # entry_size_usdt is intended as margin; margin requirement is ~entry_size_usdt (notional/leverage)
            required_margin = float(self.entry_size_usdt) * 1.05
            if self.get_balance() < required_margin:
                logger.warning(f"Недостаточно баланса для {key}")
                continue

            qty = self.entry_size_usdt * self.leverage / price
            qty = float(self.exchange.amount_to_precision(self.symbol, qty))
            side = "Buy" if self.direction == "LONG" else "Sell"

            client_order_id = f"{self.client_prefix}_E{i + 1}_{int(time.time() * 1000)}"

            try:
                o = self.exchange.create_order(
                    symbol=self.symbol,
                    type="limit",
                    side=side,
                    amount=qty,
                    price=price,
                    params={"postOnly": True, "clientOrderId": client_order_id},
                )
                # Важно: не перезатираем существующий pos целиком, чтобы не терять filled/active при гонках.
                pos = self.state["positions"].get(key, {}) if isinstance(self.state["positions"].get(key, {}), dict) else {}
                pos.setdefault("tp_order_id", None)
                pos.setdefault("sl_order_id", None)
                pos.setdefault("active", False)
                pos.setdefault("filled", False)
                pos["entry_price"] = price
                pos["qty"] = qty
                pos["entry_order_id"] = o["id"]
                pos["entry_order_ts"] = now_ms
                self.state["positions"][key] = pos
                logger.info(f"НОВАЯ ЛИМИТКА {side} {qty:.6f} @ {price:.1f} | {key}")
                placed += 1
            except Exception as e:
                logger.error(f"Ошибка создания {key}: {e}")

        if placed:
            logger.info(f"УСПЕШНО размещено {placed} новых лимиток")
        self.save_state()

    def check_and_handle_executions(self, vwap: float):
        executed = False
        try:
            # Сначала подтверждаем исполнения по истории сделок (самый надёжный источник для Bybit)
            if self._sync_fills_from_trades():
                executed = True

            open_orders = self.exchange.fetch_open_orders(self.symbol)
            open_by_id = {o.get("id"): o for o in open_orders if o.get("id")}
            open_ids = set(open_by_id.keys())
            has_pos = self._has_exchange_position()

            for key, pos in list(self.state["positions"].items()):
                if pos.get("active") or "entry_order_id" not in pos:
                    continue

                oid = pos["entry_order_id"]
                if oid in open_ids:
                    # Частичное исполнение: если filled > 0, уровень считаем сработавшим и отменяем остаток.
                    try:
                        o = open_by_id.get(oid) or {}
                        filled = float(o.get("filled") or 0)
                    except Exception:
                        filled = 0.0
                    if filled > 0 and has_pos:
                        self._mark_level_filled(key, pos, oid, f"partial fill в open_orders ({filled})")
                        try:
                            self.exchange.cancel_order(oid, self.symbol)
                        except Exception:
                            pass
                        executed = True
                    continue

                # Ордер не в открытых: проверяем, был ли он исполнен или отменён
                try:
                    o = self.exchange.fetch_order(oid, self.symbol)
                    status = (o.get("status") or "").lower()
                    filled = float(o.get("filled") or 0)
                    if status == "closed":
                        if filled > 0:
                            # Доп. защита: подтверждаем наличие позиции на бирже
                            if self._has_exchange_position():
                                self._mark_level_filled(key, pos, oid, f"fetch_order closed filled={filled}")
                                executed = True
                                logger.info(f"ВХОД {key} ИСПОЛНЕН")
                            else:
                                logger.warning(
                                    f"Ордер {key} закрыт с filled={filled}, но позиция не обнаружена — не активируем флаг"
                                )
                                pos.pop("entry_order_id", None)
                        else:
                            # Закрыт без исполнения
                            pos.pop("entry_order_id", None)
                            logger.info(f"ВХОД {key} ЗАКРЫТ БЕЗ ИСПОЛНЕНИЯ")
                    elif status in {"canceled", "rejected", "expired"}:
                        pos.pop("entry_order_id", None)
                        logger.info(f"ВХОД {key} ОТМЕНЁН ({status})")
                    else:
                        # Неизвестный статус — не делаем предположений, просто подождём следующего цикла
                        logger.info(f"ВХОД {key}: статус ордера '{status}', ждём подтверждения")
                except Exception as e:
                    # ВАЖНО: не ставим active без подтверждения — иначе зацикливание TP/SL.
                    if self._is_order_not_found(e):
                        # если ордер не существует — очищаем, иначе зависнем навсегда
                        logger.warning(f"{key}: входной ордер {oid} не найден — очищаем entry_order_id")
                        pos.pop("entry_order_id", None)
                        pos.pop("entry_order_ts", None)
                    elif not has_pos and oid not in open_ids:
                        # если позиции нет и ордера нет в open — очищаем, чтобы не залипнуть на старом state
                        logger.warning(f"{key}: позиции нет, ордер {oid} не подтверждается — очищаем entry_order_id")
                        pos.pop("entry_order_id", None)
                        pos.pop("entry_order_ts", None)
                    else:
                        logger.warning(
                            f"Не удалось подтвердить статус входа {key} (orderId={oid}) — ждём подтверждения по сделкам"
                        )

            if executed:
                self.update_tp_sl_for_all(vwap)
                self.save_state()
        except Exception as e:
            logger.debug(f"Проверка исполнения: {e}")

    def check_and_handle_position_closure(self):
        self.sync_state_with_exchange()

    def run(self):
        print(f"\nVWAP {self.direction} {self.base_symbol} — ЗАПУСК\n")
        last_vwap: Optional[float] = None

        while True:
            try:
                ticker = self.exchange.fetch_ticker(self.symbol)
                price = float(ticker["last"]) if ticker.get("last") is not None else float("nan")

                klines = self.fetch_klines()
                if not klines:
                    time.sleep(self.poll_interval)
                    continue

                vwap = self.calculate_vwap(klines)
                if vwap != vwap:
                    time.sleep(self.poll_interval)
                    continue

                levels = self.get_levels(vwap)

                cur_candle = klines[-1]["timestamp"].replace(second=0, microsecond=0)
                if self.state.get("last_candle_time") != cur_candle.isoformat():
                    self.state["last_candle_time"] = cur_candle.isoformat()
                    logger.info(f"НОВАЯ СВЕЧА {cur_candle} — ПОЛНОЕ ОБНОВЛЕНИЕ")

                    self.sync_state_with_exchange()

                    # На каждой новой свече принудительно перевыставляем TP/SL (если есть позиция)
                    if any(p.get("active", False) for p in self.state["positions"].values()):
                        self.update_tp_sl_for_all(vwap)

                    # На каждой новой свече принудительно перевыставляем входные лимитки
                    self.cancel_all_pending_entries()
                    self.place_all_entry_orders(levels["entry_levels"])

                if last_vwap and abs(vwap - last_vwap) / vwap > 0.0005:
                    if any(p.get("active", False) for p in self.state["positions"].values()):
                        self.update_tp_sl_for_all(vwap)
                last_vwap = vwap

                # ПРАВИЛЬНЫЙ ПОРЯДОК:
                self.check_and_handle_executions(vwap)  # Сначала исполнение
                self.check_and_handle_position_closure()  # Потом закрытие

                bal = self.get_balance()
                unreal = self.get_unrealized_pnl()
                active = [k for k, v in self.state["positions"].items() if v.get("active", False)]

                print(f"\n{'=' * 70}")
                print(
                    f"{datetime.now():%H:%M:%S} | {self.base_symbol} | {self.direction} | Цена: {price:.1f} | VWAP: {vwap:.1f}"
                )
                print(f"Входы: {[f'{x:.1f}' for x in levels['entry_levels']]}")
                print(f"TP: {levels['tp_price']:.1f} | SL: {levels['sl_price']:.1f} ← Conditional")
                print(f"Активно: {active or '—'} | Баланс: {bal:.2f} | Unreal: {unreal:+.2f}")
                print(f"{'=' * 70}")

                time.sleep(self.poll_interval)

            except KeyboardInterrupt:
                logger.info("Остановлено пользователем")
                break
            except Exception as e:
                logger.error(f"Критическая ошибка: {e}")
                time.sleep(self.poll_interval)

    def get_balance(self) -> float:
        try:
            b = self.exchange.fetch_balance(params={"type": "swap"})
            return float(b["USDT"]["free"])
        except Exception:
            return 0.0

    def get_unrealized_pnl(self) -> float:
        try:
            pos = self.exchange.fetch_positions([self.symbol])
            return sum(float(p.get("unrealisedPnl", 0) or 0) for p in pos if float(p.get("contracts", 0) or 0) > 0)
        except Exception:
            return 0.0


def main():
    api_key = os.getenv("BYBIT_API_KEY") or input("API Key: ").strip()
    api_secret = os.getenv("BYBIT_API_SECRET") or input("API Secret: ").strip()

    symbol = os.getenv("BYBIT_SYMBOL") or (input("Символ (ум. BTCUSDT): ") or "BTCUSDT").strip()
    direction = os.getenv("BYBIT_DIRECTION") or (input("Направление (Long / Short) (ум. Long): ") or "Long")

    testnet_env = os.getenv("BYBIT_TESTNET")
    if testnet_env is None:
        testnet = (input("Тестнет? (y/n) (ум. y): ") or "y").lower().startswith("y")
    else:
        testnet = testnet_env.strip().lower() in {"1", "true", "y", "yes"}

    strategy = BybitVWAPStrategy(api_key, api_secret, symbol, direction, testnet)
    strategy.run()


if __name__ == "__main__":
    main()
