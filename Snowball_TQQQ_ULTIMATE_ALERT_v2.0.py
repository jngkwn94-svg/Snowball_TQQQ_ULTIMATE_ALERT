#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
눈덩이 TQQQ ULTIMATE v2.0 - Telegram Alert Engine
-------------------------------------------------
목적:
  규칙.txt의 "Snowball TQQQ ULTIMATE FINAL v2.0" 신호 구조를
  Python에서 일일 종가 기준으로 재현하고 Telegram으로 알립니다.

중요:
  - 이 파일은 자동매매 주문기가 아니라 "알림 엔진"입니다.
  - 신호는 TradingView와 동일한 철학으로 "신호일 종가 확정 -> 다음 정규장" 기준입니다.
  - 실제 증권계좌 체결과 Python의 가상 포지션은 다를 수 있으므로 주문 실행은 하지 않습니다.
  - EQDD/TP2/cycleBaseQty/TP3 Lock을 포함하기 위해 로컬 상태 파일을 사용합니다.
  - 첫 실행 시 2010-02-11부터 데이터를 재생해 상태를 복원합니다.

필수 환경변수:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

설치:
  pip install yfinance pandas requests

실행:
  python Snowball_TQQQ_ULTIMATE_ALERT_v2.0.py
"""

import html
import json
import math
import os
from pathlib import Path
from datetime import datetime

import pandas as pd
import requests
import yfinance as yf


# ============================================================
# 0. SETTINGS — 규칙.txt Ultimate v2.0
# ============================================================

START_DATE = "2010-02-11"

FAST_LEN = 5
SLOW_LEN = 220

GC_BAND = 2.90
DC_BAND = -0.10
GC_DELAY = 0

DIP1 = 10.0
DIP1_WEIGHT = 30.0

DIP2 = 22.0
DIP2_WEIGHT = 70.0
DIP2_FILTER_LEN = 200
DIP2_FILTER_PCT = -7.0

PEAK_LEN = 126

USE_VIX = True
USE_CREDIT = True
USE_EQDD = True
EQ_START = 27.5
EQ_FULL = 37.5
EQ_MIN = 0.475

USE_RSI = False
RSI_LEN = 14

SAFETY_FACTOR = 99.0
CASH_BUFFER = 0.2
MAX_DIP = 40.0

COOLDOWN_DAYS = 0

TP1_PCT = 15.0
TP1_SELL_PCT = 50.0

TP2_DOWN = 90.0
TP2_DOWN_SELL = 45.0

TP2_NONE = 100.0
TP2_NONE_SELL = 35.0

TP2_UP = 115.0
TP2_UP_SELL = 25.0

TP3_PCT = 350.0

INITIAL_CAPITAL = 10000.0
COMMISSION = 0.0005       # 0.05%
SLIPPAGE_TICKS = 1        # Pine slippage=1
MIN_TICK = 0.01           # TQQQ practical alert approximation

STATE_FILE = Path(__file__).with_name("snowball_ultimate_alert_state.json")


# ============================================================
# 1. TELEGRAM
# ============================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없습니다."
        )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )
    r.raise_for_status()


# ============================================================
# 2. DATA
# ============================================================

TICKERS = {
    "TQQQ": "TQQQ",
    "QQQ": "QQQ",
    "VIX": "^VIX",
    "VIX3M": "^VIX3M",
    "HYG": "HYG",
    "LQD": "LQD",
}


def download_data():
    # 2010년부터 충분한 warm-up 확보.
    end = (pd.Timestamp.now(tz="America/New_York").normalize() + pd.Timedelta(days=2)).strftime("%Y-%m-%d")

    raw = yf.download(
        list(TICKERS.values()),
        start="2009-01-01",
        end=end,
        auto_adjust=False,
        progress=False,
        group_by="column",
        threads=True,
    )

    if raw.empty:
        raise RuntimeError("Yahoo Finance 데이터를 받지 못했습니다.")

    out = {}

    for name, ticker in TICKERS.items():
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                # yfinance 버전별 컬럼 순서 차이를 안전하게 처리
                if ticker in raw.columns.get_level_values(0):
                    df = raw[ticker].copy()
                elif ticker in raw.columns.get_level_values(1):
                    df = raw.xs(ticker, axis=1, level=1).copy()
                else:
                    raise KeyError(ticker)
            else:
                df = raw.copy()

            if isinstance(df, pd.DataFrame):
                if "Close" not in df.columns:
                    raise KeyError(f"{ticker}: Close 없음")
                s = df["Close"]
            else:
                s = df

            s = pd.to_numeric(s, errors="coerce")
            s.index = pd.to_datetime(s.index).tz_localize(None)
            out[name] = s.rename(name)

        except Exception as e:
            raise RuntimeError(f"{name}({ticker}) 데이터 처리 실패: {e}")

    # OHLC가 필요한 TQQQ는 별도로 받는다.
    t = yf.download(
        "TQQQ",
        start="2009-01-01",
        end=end,
        auto_adjust=False,
        progress=False,
    )
    if isinstance(t.columns, pd.MultiIndex):
        t.columns = t.columns.get_level_values(0)

    t.index = pd.to_datetime(t.index).tz_localize(None)
    for c in ["Open", "High", "Low", "Close"]:
        t[c] = pd.to_numeric(t[c], errors="coerce")

    out["TQQQ_OPEN"] = t["Open"].rename("TQQQ_OPEN")
    out["TQQQ_CLOSE"] = t["Close"].rename("TQQQ_CLOSE")

    data = pd.concat(out.values(), axis=1).sort_index()

    # TradingView의 daily regular-session 판단에 맞춰 날짜별 마지막 값 사용.
    data = data[~data.index.duplicated(keep="last")]

    return data


# ============================================================
# 3. INDICATORS
# ============================================================

def rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def prepare_indicators(df):
    x = df.copy()

    q = x["QQQ"]
    t = x["TQQQ_CLOSE"]

    x["q_peak"] = q.rolling(PEAK_LEN).max()
    x["q_dd"] = (q / x["q_peak"] - 1.0) * 100.0

    x["q_fast"] = q.rolling(FAST_LEN).mean()
    x["q_slow"] = q.rolling(SLOW_LEN).mean()
    x["q_filter"] = q.rolling(DIP2_FILTER_LEN).mean()

    x["t_fast"] = t.rolling(FAST_LEN).mean()
    x["t_slow"] = t.rolling(SLOW_LEN).mean()
    x["t_200"] = t.rolling(DIP2_FILTER_LEN).mean()

    prev_fast = x["t_fast"].shift(1)
    prev_slow = x["t_slow"].shift(1)

    x["gc_cross"] = (prev_fast <= prev_slow) & (x["t_fast"] > x["t_slow"])
    x["dc_cross"] = (prev_fast >= prev_slow) & (x["t_fast"] < x["t_slow"])

    x["gc_band"] = x["t_fast"] >= x["t_slow"] * (1 + GC_BAND / 100)
    x["dc_band"] = x["t_fast"] <= x["t_slow"] * (1 + DC_BAND / 100)

    x["gc_confirmed"] = x["gc_cross"] & x["gc_band"]
    x["dc_confirmed"] = x["dc_cross"] & x["dc_band"]

    x["vix_ratio"] = x["VIX"] / x["VIX3M"]

    x["dip1_threshold"] = DIP1
    x.loc[x["vix_ratio"] > 1.03, "dip1_threshold"] = 12.0
    x.loc[x["vix_ratio"] > 1.08, "dip1_threshold"] = 14.0

    if not USE_VIX:
        x["dip1_threshold"] = DIP1

    x["hyg_roc5"] = x["HYG"].pct_change(5) * 100
    x["lqd_roc5"] = x["LQD"].pct_change(5) * 100
    x["credit_roc"] = (x["hyg_roc5"] + x["lqd_roc5"]) / 2
    x["credit_ok"] = True if not USE_CREDIT else (x["credit_roc"] > 0)

    # Weekly values: Pine request.security(... "W", ...)의 투명 proxy.
    weekly = t.resample("W-FRI").last().to_frame("wclose")
    weekly["w5"] = weekly["wclose"].rolling(5).mean()
    weekly["w20"] = weekly["wclose"].rolling(20).mean()
    weekly["w5_rising"] = weekly["w5"] > weekly["w5"].shift(2)

    x["wclose"] = weekly["wclose"].reindex(x.index, method="ffill")
    x["w5"] = weekly["w5"].reindex(x.index, method="ffill")
    x["w20"] = weekly["w20"].reindex(x.index, method="ffill")
    x["w5_rising"] = weekly["w5_rising"].reindex(x.index, method="ffill")

    x["up_raw"] = (
        (x["w5"] > x["w20"]) &
        (x["wclose"] > x["w20"])
    )

    x["bottom"] = (
        (x["q_dd"] <= -15.0) &
        x["w5_rising"].fillna(False)
    )

    hazard1 = x["vix_ratio"] > 1.08
    hazard2 = (x["hyg_roc5"] < 0) & (x["lqd_roc5"] < 0)
    hazard3 = x["QQQ"].pct_change(20) < -0.08

    x["hazard_count"] = hazard1.fillna(False).astype(int)
    x["hazard_count"] += hazard2.fillna(False).astype(int)
    x["hazard_count"] += hazard3.fillna(False).astype(int)

    x["down_state"] = (
        ((x["w5"] < x["w20"]) & (x["wclose"] < x["w20"])) |
        (x["hazard_count"] >= 2)
    )

    x["up_state"] = (
        ~x["down_state"] &
        (x["up_raw"] | x["bottom"])
    )

    x["tp2_state"] = "NONE"
    x.loc[x["down_state"], "tp2_state"] = "DOWN"
    x.loc[x["up_state"] & x["bottom"], "tp2_state"] = "BOTTOM"
    x.loc[x["up_state"] & ~x["bottom"], "tp2_state"] = "UP"

    x["tp2_trigger"] = TP2_NONE
    x.loc[x["down_state"], "tp2_trigger"] = TP2_DOWN
    x.loc[x["up_state"], "tp2_trigger"] = TP2_UP

    x["tp2_sell_pct"] = TP2_NONE_SELL
    x.loc[x["down_state"], "tp2_sell_pct"] = TP2_DOWN_SELL
    x.loc[x["up_state"], "tp2_sell_pct"] = TP2_UP_SELL

    x["rsi"] = rsi(t, RSI_LEN)
    x["rsi_bonus"] = 0.0
    if USE_RSI:
        x.loc[x["rsi"] < 35, "rsi_bonus"] = 5.0
        x.loc[x["rsi"] < 30, "rsi_bonus"] = 7.5

    return x


# ============================================================
# 4. STATE
# ============================================================

def default_state():
    return {
        "stage": 0,
        "gc_bar": None,
        "last_full_exit_bar": None,
        "tp1_done": False,
        "tp2_done": False,
        "tp3_lock": False,
        "cycle_base_qty": None,
        "pending_action": 0,
        "pending_bar": None,
        "pending_position": 0.0,
        "pending_reason": "",
        "pending_signal_date": None,
        "cash": INITIAL_CAPITAL,
        "position_qty": 0.0,
        "avg_price": 0.0,
        "equity_peak": INITIAL_CAPITAL,
        "last_alert_key": "",
        "last_processed_date": "",
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            s = default_state()
            s.update(json.load(f))
            return s
    except Exception:
        # 손상된 상태 파일은 새로 시작.
        return default_state()


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


# ============================================================
# 5. PORTFOLIO / EXECUTION MODEL
# ============================================================

def round_qty(qty):
    return max(0, math.floor(qty))


def mark_equity(state, close):
    return max(
        state["cash"] + state["position_qty"] * close,
        0.0,
    )


def buy_fill(state, qty, price):
    if qty <= 0:
        return
    cost = qty * price
    fee = cost * COMMISSION
    state["cash"] -= cost + fee

    old_qty = state["position_qty"]
    old_avg = state["avg_price"]

    new_qty = old_qty + qty
    if new_qty > 0:
        state["avg_price"] = (
            old_qty * old_avg + qty * price
        ) / new_qty

    state["position_qty"] = new_qty


def sell_fill(state, qty, price):
    qty = min(max(qty, 0), state["position_qty"])
    if qty <= 0:
        return

    proceeds = qty * price
    fee = proceeds * COMMISSION
    state["cash"] += proceeds - fee
    state["position_qty"] -= qty

    if state["position_qty"] <= 0:
        state["position_qty"] = 0.0
        state["avg_price"] = 0.0


def buy_qty(state, close, target_value):
    equity = mark_equity(state, close)
    current_value = max(state["position_qty"], 0) * close

    available_cash = max(equity - current_value, 0) * (1 - CASH_BUFFER / 100)
    estimated_fill = close + MIN_TICK

    target_qty = max(target_value - current_value, 0) / estimated_fill
    cash_qty = available_cash / (estimated_fill * (1 + COMMISSION))

    return round_qty(min(target_qty, cash_qty))


# ============================================================
# 6. SIGNAL / REPLAY ENGINE
# ============================================================

def replay(df, state, send_alerts=False):
    """
    2010-02-11부터 오늘까지 일봉을 순서대로 재생.
    신호 발생일에는 pending을 만들고 다음 거래일 OPEN에서 체결.
    """

    dates = df.index[df.index >= pd.Timestamp(START_DATE)]

    if len(dates) == 0:
        raise RuntimeError("START_DATE 이후 데이터가 없습니다.")

    gc_bar_index = None
    last_full_exit_idx = None

    # 저장된 bar memory가 있으면 날짜 기준으로 재구성하지 않고,
    # 전체 replay 중 다시 계산한다.
    state["gc_bar"] = None
    state["last_full_exit_bar"] = None
    state["pending_action"] = 0
    state["pending_bar"] = None
    state["pending_position"] = 0.0
    state["pending_reason"] = ""
    state["pending_signal_date"] = None

    # 재생은 기존 state를 사용할 수 없으므로 포트폴리오도 초기화하고
    # 매번 deterministic하게 현재 상태를 다시 계산한다.
    state["stage"] = 0
    state["tp1_done"] = False
    state["tp2_done"] = False
    state["tp3_lock"] = False
    state["cycle_base_qty"] = None
    state["cash"] = INITIAL_CAPITAL
    state["position_qty"] = 0.0
    state["avg_price"] = 0.0
    state["equity_peak"] = INITIAL_CAPITAL
    state["last_full_exit_bar"] = None
    state["gc_bar"] = None

    new_alerts = []

    for i, dt in enumerate(dates):
        row = df.loc[dt]

        close = float(row["TQQQ_CLOSE"]) if pd.notna(row["TQQQ_CLOSE"]) else None
        open_ = float(row["TQQQ_OPEN"]) if pd.notna(row["TQQQ_OPEN"]) else None

        if close is None or open_ is None:
            continue

        # ----------------------------------------------------
        # A. 전일 signal -> 오늘 OPEN 체결
        # ----------------------------------------------------
        if state["pending_action"] != 0 and state["pending_bar"] == i - 1:
            old_qty = state["position_qty"]
            action = state["pending_action"]
            reason = state["pending_reason"]

            # slippage=1 tick의 보수적 근사
            # TradingView:
            # process_orders_on_close=false
            # → 신호 다음 봉 OPEN 체결
            #
            # Pine의 fillPrice 역시 open이므로
            # 평균단가 계산에는 ±1 tick을 넣지 않는다.
            fill_price = open_

            if action == 1:  # DIP1
                equity_before = mark_equity(state, open_)
                target = equity_before * min(
                    DIP1_WEIGHT, 100.0
                ) / 100.0
                qty = buy_qty(state, open_, target)
                buy_fill(state, qty, fill_price)

                state["stage"] = 1
                state["cycle_base_qty"] = state["position_qty"]
                state["tp3_lock"] = False

            elif action == 2:  # DIP2
                eq = mark_equity(state, open_)
                # EQDD factor는 신호일 종가 equity를 기반으로 계산.
                target_factor = state.get("_pending_eq_factor", 1.0)
                target = eq * min(
                    DIP2_WEIGHT * target_factor,
                    100.0
                ) / 100.0
                qty = buy_qty(state, open_, target)
                buy_fill(state, qty, fill_price)

                state["stage"] = 2
                if state["cycle_base_qty"] is None:
                    state["cycle_base_qty"] = state["position_qty"]

            elif action == 3:  # GC
                eq = mark_equity(state, open_)
                target = eq * SAFETY_FACTOR / 100.0
                qty = buy_qty(state, open_, target)
                buy_fill(state, qty, fill_price)

                state["stage"] = 3
                state["cycle_base_qty"] = state["position_qty"]
                state["gc_bar"] = None
                state["tp3_lock"] = False

            elif action == -1:  # TP1
                qty = round_qty(
                    state["position_qty"] * TP1_SELL_PCT / 100
                )
                sell_fill(state, qty, fill_price)
                state["tp1_done"] = True

            elif action == -2:  # TP2
                sell_pct = float(state.get("_pending_tp2_sell", TP2_NONE_SELL))
                base = float(state["cycle_base_qty"] or 0)
                qty = round_qty(
                    min(
                        base * sell_pct / 100,
                        state["position_qty"]
                    )
                )
                sell_fill(state, qty, fill_price)
                state["tp2_done"] = True

            elif action == -3:  # DC
                sell_fill(state, state["position_qty"], fill_price)
                state["stage"] = 0
                state["tp1_done"] = False
                state["tp2_done"] = False
                state["cycle_base_qty"] = None
                state["gc_bar"] = None
                state["last_full_exit_bar"] = i

            elif action == -4:  # TP3
                sell_fill(state, state["position_qty"], fill_price)
                state["stage"] = 0
                state["tp1_done"] = False
                state["tp2_done"] = False
                state["cycle_base_qty"] = None
                state["gc_bar"] = None
                state["last_full_exit_bar"] = i
                state["tp3_lock"] = True

            if send_alerts:
                new_alerts.append(
                    ("FILL", dt, reason, fill_price, state["position_qty"])
                )

            state["pending_action"] = 0
            state["pending_bar"] = None
            state["pending_position"] = 0.0
            state["pending_reason"] = ""
            state["pending_signal_date"] = None
            state.pop("_pending_eq_factor", None)
            state.pop("_pending_tp2_sell", None)

        # ----------------------------------------------------
        # B. 현재 종가 기준 equity / EQDD
        # ----------------------------------------------------
        equity = mark_equity(state, close)
        state["equity_peak"] = max(state["equity_peak"], equity)

        eq_dd = (
            (equity / state["equity_peak"] - 1) * 100
            if state["equity_peak"] > 0 else 0
        )

        if eq_dd >= -EQ_START:
            eq_factor = 1.0
        elif eq_dd <= -EQ_FULL:
            eq_factor = EQ_MIN
        else:
            eq_factor = 1 - (
                (1 - EQ_MIN) *
                ((-eq_dd - EQ_START) / (EQ_FULL - EQ_START))
            )

        if not USE_EQDD:
            eq_factor = 1.0

        eq_factor = max(EQ_MIN, min(1.0, eq_factor))

        # ----------------------------------------------------
        # C. GC memory
        # ----------------------------------------------------
        # TradingView와 동일하게 최초 GC만 memory에 저장
        if bool(row["gc_confirmed"]):
            if gc_bar_index is None:
                gc_bar_index = i
                state["gc_bar"] = i

        gc_eligible = (
            gc_bar_index is not None
            and i - gc_bar_index >= GC_DELAY
            and bool(row["t_fast"] > row["t_slow"])
            and bool(row["gc_band"])
        )

        can_buy_after_exit = (
            last_full_exit_idx is None
            or i - last_full_exit_idx > COOLDOWN_DAYS
        )

        ready = all(
            pd.notna(row.get(c))
            for c in [
                "q_dd", "q_filter", "t_fast", "t_slow",
                "vix_ratio", "credit_roc"
            ]
        )

        if not ready:
            continue

        max_dip_reached = float(row["q_dd"]) <= -MAX_DIP

        has_position = state["position_qty"] > 0

        # ----------------------------------------------------
        # D. Order priority — 규칙.txt 그대로
        # DC -> TP3 -> TP1 -> TP2 -> BUY
        # ----------------------------------------------------
        signal = None
        reason = None
        pending_meta = {}

        dc_condition = (
            has_position and
            bool(row["dc_confirmed"])
        )

        tp3_condition = (
            has_position and
            not state["tp3_lock"] and
            state["cycle_base_qty"] is not None and
            state["avg_price"] > 0 and
            close >= state["avg_price"] * (1 + TP3_PCT / 100)
        )

        tp1_condition = (
            has_position and
            not state["tp1_done"] and
            state["avg_price"] > 0 and
            close >= state["avg_price"] * (1 + TP1_PCT / 100) and
            round_qty(state["position_qty"] * TP1_SELL_PCT / 100) >= 1
        )

        tp2_trigger = float(row["tp2_trigger"])
        tp2_sell_pct = float(row["tp2_sell_pct"])

        tp2_qty = round_qty(
            min(
                float(state["cycle_base_qty"] or 0) * tp2_sell_pct / 100,
                state["position_qty"]
            )
        )

        tp2_condition = (
            has_position and
            state["tp1_done"] and
            not state["tp2_done"] and
            state["avg_price"] > 0 and
            close >= state["avg_price"] * (1 + tp2_trigger / 100) and
            tp2_qty >= 1
        )

        buy_allowed = (
            can_buy_after_exit and
            not max_dip_reached
        )

        dip1_condition = (
            buy_allowed and
            state["stage"] == 0 and
            not has_position and
            float(row["q_dd"]) <= -float(row["dip1_threshold"])
        )

        dip2_condition = (
            buy_allowed and
            state["stage"] == 1 and
            has_position and
            float(row["q_dd"]) <= -DIP2 and
            float(row["QQQ"]) <= float(row["q_filter"]) * (
                1 + DIP2_FILTER_PCT / 100
            ) and
            bool(row["credit_ok"])
        )

        gc_condition = (
            buy_allowed and
            gc_eligible and
            not state["tp3_lock"]
        )

        if dc_condition:
            signal, reason = -3, "DC"

        elif tp3_condition:
            signal, reason = -4, "TP3"

        elif tp1_condition:
            signal, reason = -1, "TP1"

        elif tp2_condition:
            signal, reason = -2, f"TP2 {row['tp2_state']}"

        elif gc_condition:
            signal, reason = 3, "GC"

        elif dip1_condition:
            signal, reason = 1, "DIP1"

        elif dip2_condition:
            signal, reason = 2, "DIP2"

        if signal is not None:
            # pending 주문은 한 번만 생성
            state["pending_action"] = signal
            state["pending_bar"] = i
            state["pending_position"] = state["position_qty"]
            state["pending_reason"] = reason
            state["pending_signal_date"] = dt.strftime("%Y-%m-%d")

            if signal == 2:
                state["_pending_eq_factor"] = eq_factor
            if signal == -2:
                state["_pending_tp2_sell"] = tp2_sell_pct

            if send_alerts:
                new_alerts.append(
                    (
                        "SIGNAL",
                        dt,
                        reason,
                        close,
                        {
                            "qdd": float(row["q_dd"]),
                            "vixratio": float(row["vix_ratio"]),
                            "eqdd": float(eq_dd),
                            "eqfactor": float(eq_factor),
                            "stage": int(state["stage"]),
                            "tp2state": str(row["tp2_state"]),
                            "tp2trigger": tp2_trigger,
                            "tp2sell": tp2_sell_pct,
                        },
                    )
                )

        state["last_processed_date"] = dt.strftime("%Y-%m-%d")

    return state, new_alerts


# ============================================================
# 7. TELEGRAM MESSAGE
# ============================================================

def fmt(v, n=2):
    try:
        return f"{float(v):.{n}f}"
    except Exception:
        return "-"


def build_signal_message(dt, reason, close, meta, row):
    direction = "🟢 매수 신호" if reason in ("DIP1", "DIP2", "GC") else "🔴 매도 신호"

    lines = [
        f"❄️ <b>눈덩이 TQQQ · ULTIMATE v2.0</b>",
        "",
        f"<b>{direction}</b>",
        f"신호: <b>{html.escape(str(reason))}</b>",
        f"신호일: {dt.strftime('%Y-%m-%d')}",
        f"TQQQ 종가: <b>{fmt(close)}</b>",
        "",
        f"QQQ DD: {fmt(meta['qdd'])}%",
        f"VIX/VIX3M: {fmt(meta['vixratio'], 3)}",
        f"EQDD: {fmt(meta['eqdd'])}%",
        f"EQDD 계수: {fmt(meta['eqfactor'], 3)}",
        f"현재 Stage: {meta['stage']}",
    ]

    if reason == "DIP1":
        lines += [
            "",
            f"DIP1 기준: -{fmt(row['dip1_threshold'])}%",
            "목표 비중: 30%",
            "➡️ 다음 정규장 시가 기준 매수",
        ]

    elif reason == "DIP2":
        lines += [
            "",
            "DIP2 기준: -22%",
            f"TP2 상태: {html.escape(str(meta['tp2state']))}",
            "목표 비중: EQDD 반영 최대 70%",
            "➡️ 다음 정규장 시가 기준 추가매수",
        ]

    elif reason == "GC":
        lines += [
            "",
            "TQQQ SMA 5/220 GC",
            "GC Band: +2.90%",
            "목표 비중: 99%",
            "➡️ 다음 정규장 시가 기준 매수",
        ]

    elif reason == "TP1":
        lines += [
            "",
            "TP1: +15%",
            "현재 보유수량의 50% 매도",
            "➡️ 다음 정규장 시가 기준 매도",
        ]

    elif reason.startswith("TP2"):
        lines += [
            "",
            f"TP2 상태: {html.escape(str(meta['tp2state']))}",
            f"TP2 기준: +{fmt(meta['tp2trigger'])}%",
            f"기준수량 매도: {fmt(meta['tp2sell'])}%",
            "➡️ 다음 정규장 시가 기준 매도",
        ]

    elif reason == "TP3":
        lines += [
            "",
            "TP3: +350%",
            "잔여 전량 매도",
            "TP3 Lock 발동",
            "➡️ 다음 정규장 시가 기준 매도",
        ]

    elif reason == "DC":
        lines += [
            "",
            "DC Band: -0.10%",
            "전량 매도",
            "➡️ 다음 정규장 시가 기준 매도",
        ]

    return "\n".join(lines)


def build_fill_message(dt, reason, fill_price, qty, remaining):
    action = "매수" if reason in ("DIP1", "DIP2", "GC") else "매도"

    return "\n".join([
        f"❄️ <b>눈덩이 TQQQ · ULTIMATE v2.0</b>",
        "",
        f"✅ <b>{reason} {action} 체결 확인</b>",
        f"체결일: {dt.strftime('%Y-%m-%d')}",
        f"체결가(근사): <b>{fmt(fill_price)}</b>",
        f"수량: <b>{fmt(qty, 0)}주</b>",
        f"잔여 보유: <b>{fmt(remaining, 0)}주</b>",
    ])


# ============================================================
# 8. MAIN
# ============================================================

def calc_signal():
    data = download_data()
    data = prepare_indicators(data)

    # --------------------------------------------------------
    # 기존 alert.py 방식과 동일하게
    # Yahoo Finance에서 받은 마지막 유효 TQQQ 거래일을 사용한다.
    #
    # 중요:
    # - 미국 동부시간 '오늘 날짜'와 일치하는지 검사하지 않는다.
    # - 미국장 휴장일에는 가장 최근 확정 거래일을 사용한다.
    # - Ultimate 전략 계산/Replay 로직은 변경하지 않는다.
    # --------------------------------------------------------
    latest_tqqq = data["TQQQ_CLOSE"].dropna().index.max()

    if pd.isna(latest_tqqq):
        print("[SKIP] 유효한 TQQQ 일봉 데이터가 없습니다.")
        return

    # 마지막 확정 TQQQ 거래일
    today = latest_tqqq

    print(
        f"[DATA] Ultimate 기준일: "
        f"{today.strftime('%Y-%m-%d')}"
    )

    state = load_state()

    # 전체 replay로 deterministic 상태 복원.
    state, events = replay(data, state, send_alerts=True)

    # 이번 실행의 마지막 거래일 이벤트만 전송.
    last_date = today

    sent = 0

    for event in events:
        kind = event[0]
        dt = event[1]

        if dt != last_date:
            continue

        if kind == "SIGNAL":
            _, dt, reason, close, meta = event
            row = data.loc[dt]

            # 중복 방지 key
            key = f"SIGNAL|{dt.strftime('%Y-%m-%d')}|{reason}"

            if state.get("last_alert_key") == key:
                continue

            msg = build_signal_message(
                dt, reason, close, meta, row
            )
            send_telegram(msg)
            state["last_alert_key"] = key
            sent += 1

        elif kind == "FILL":
            _, dt, reason, fill_price, remaining = event
            key = f"FILL|{dt.strftime('%Y-%m-%d')}|{reason}"

            # FILL은 signal과 다른 key를 사용.
            if state.get("last_alert_key") == key:
                continue

            # replay에서 정확한 수량을 다시 계산할 수 있도록
            # 여기서는 알림용 근사값을 사용.
            # 실제 fill 수량은 state의 position 변화로 확인하려면
            # broker/TradingView 체결정보가 필요합니다.
            msg = build_fill_message(
                dt,
                reason,
                fill_price,
                0,
                remaining,
            )
            # 실사용에서는 FILL을 별도 전송하지 않고 SIGNAL만 보내는 것을 기본으로 한다.
            # 아래는 비활성화.
            # send_telegram(msg)

    save_state(state)

    print(
        f"[OK] {today.strftime('%Y-%m-%d')} | "
        f"Stage={state['stage']} | "
        f"Position={state['position_qty']:.0f} | "
        f"TP1={state['tp1_done']} | "
        f"TP2={state['tp2_done']} | "
        f"TP3Lock={state['tp3_lock']} | "
        f"NewAlerts={sent}"
    )


if __name__ == "__main__":
    calc_signal()
