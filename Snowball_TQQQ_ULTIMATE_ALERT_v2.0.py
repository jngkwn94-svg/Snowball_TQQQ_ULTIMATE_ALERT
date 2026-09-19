#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
눈덩이 TQQQ ULTIMATE v2.0 - TradingView Sync + Daily Status
------------------------------------------------------------
목적:
  규칙.txt의 "Snowball TQQQ ULTIMATE FINAL v2.0" 신호 구조를
  Python에서 일일 종가 기준으로 재현하고 Telegram으로 알립니다.

TradingView Sync 패치:
  1. GC memory는 최초 GC만 저장
  2. 신호일 종가에서 BUY 수량 계산
  3. 계산된 pending_qty를 다음 봉 OPEN에서 그대로 체결
  4. 평균단가는 실제 다음 봉 OPEN 체결가격으로 계산
  5. DC 발생 시 GC memory 제거
  6. pending_qty 상태 저장/초기화

Daily Status:
  - 매일 마지막 확정 거래일 기준 현황 전송
  - TQQQ 종가 + 전일 대비 등락률
  - QQQ 종가
  - Nasdaq-100 선물(NQ=F)
  - 현재 시장상태: NONE / UP/BOTTOM / DOWN
  - 오늘 행동지침

중요:
  - 자동매매 주문기가 아니라 "알림 엔진"입니다.
  - 신호는 "신호일 종가 확정 -> 다음 정규장 OPEN" 기준입니다.
  - 실제 증권계좌 체결과 Python 가상 포지션은 다를 수 있습니다.
  - EQDD/TP2/cycleBaseQty/TP3 Lock을 포함하기 위해
    로컬 상태 파일을 사용합니다.
  - 첫 실행 시 2010-02-11부터 데이터를 재생해 상태를 복원합니다.

필수 환경변수:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

설치:
  pip install yfinance pandas requests

실행:
  python Snowball_TQQQ_ULTIMATE_ALERT_v2.0_TV_SYNC_PATCHED.py
"""

import html
import json
import math
import os
from pathlib import Path

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
SLIPPAGE_TICKS = 1
MIN_TICK = 0.01

STATE_FILE = Path(__file__).with_name(
    "snowball_ultimate_alert_state.json"
)


# ============================================================
# 1. TELEGRAM
# ============================================================

BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN"
)

CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID"
)


def send_telegram(msg: str):

    if not BOT_TOKEN or not CHAT_ID:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
            "환경변수가 없습니다."
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

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

    # --------------------------------------------------------
    # 200/220일선 및 초기 warm-up 확보
    # --------------------------------------------------------
    end = (
        pd.Timestamp.now(
            tz="America/New_York"
        ).normalize()
        + pd.Timedelta(days=2)
    ).strftime("%Y-%m-%d")

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

        raise RuntimeError(
            "Yahoo Finance 데이터를 받지 못했습니다."
        )

    out = {}

    for name, ticker in TICKERS.items():

        try:

            if isinstance(
                raw.columns,
                pd.MultiIndex
            ):

                if (
                    ticker
                    in raw.columns.get_level_values(0)
                ):

                    df = raw[ticker].copy()

                elif (
                    ticker
                    in raw.columns.get_level_values(1)
                ):

                    df = raw.xs(
                        ticker,
                        axis=1,
                        level=1
                    ).copy()

                else:

                    raise KeyError(ticker)

            else:

                df = raw.copy()

            if isinstance(
                df,
                pd.DataFrame
            ):

                if "Close" not in df.columns:

                    raise KeyError(
                        f"{ticker}: Close 없음"
                    )

                s = df["Close"]

            else:

                s = df

            s = pd.to_numeric(
                s,
                errors="coerce"
            )

            s.index = pd.to_datetime(
                s.index
            ).tz_localize(None)

            out[name] = s.rename(name)

        except Exception as e:

            raise RuntimeError(
                f"{name}({ticker}) 데이터 처리 실패: {e}"
            )

    # --------------------------------------------------------
    # TQQQ OHLC
    # --------------------------------------------------------
    t = yf.download(
        "TQQQ",
        start="2009-01-01",
        end=end,
        auto_adjust=False,
        progress=False,
    )

    if isinstance(
        t.columns,
        pd.MultiIndex
    ):

        t.columns = (
            t.columns
            .get_level_values(0)
        )

    t.index = pd.to_datetime(
        t.index
    ).tz_localize(None)

    for c in [
        "Open",
        "High",
        "Low",
        "Close"
    ]:

        t[c] = pd.to_numeric(
            t[c],
            errors="coerce"
        )

    out["TQQQ_OPEN"] = (
        t["Open"]
        .rename("TQQQ_OPEN")
    )

    out["TQQQ_CLOSE"] = (
        t["Close"]
        .rename("TQQQ_CLOSE")
    )

    data = pd.concat(
        out.values(),
        axis=1
    ).sort_index()

    data = data[
        ~data.index.duplicated(
            keep="last"
        )
    ]

    return data


# ============================================================
# 2-1. Nasdaq-100 Futures
# ============================================================

def get_nasdaq_futures():
    """
    Nasdaq-100 E-mini 선물:
        Yahoo Finance ticker = NQ=F

    전략 계산에는 사용하지 않고
    Telegram 일일현황 표시용으로만 사용한다.
    """

    try:

        nq = yf.download(
            "NQ=F",
            period="5d",
            interval="5m",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

        if nq.empty:

            return None, None

        if isinstance(
            nq.columns,
            pd.MultiIndex
        ):

            nq.columns = (
                nq.columns
                .get_level_values(0)
            )

        nq = nq.dropna(
            subset=["Close"]
        )

        if nq.empty:

            return None, None

        latest_price = float(
            nq["Close"].iloc[-1]
        )

        # ----------------------------------------------------
        # 전일 종가
        # ----------------------------------------------------
        daily = yf.download(
            "NQ=F",
            period="10d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )

        if isinstance(
            daily.columns,
            pd.MultiIndex
        ):

            daily.columns = (
                daily.columns
                .get_level_values(0)
            )

        daily = daily.dropna(
            subset=["Close"]
        )

        if len(daily) >= 2:

            prev_close = float(
                daily["Close"].iloc[-2]
            )

        else:

            prev_close = None

        if (
            prev_close is not None
            and prev_close > 0
        ):

            change_pct = (
                latest_price
                / prev_close
                - 1.0
            ) * 100.0

        else:

            change_pct = None

        return (
            latest_price,
            change_pct
        )

    except Exception as e:

        print(
            f"[WARN] NQ=F 데이터 조회 실패: {e}"
        )

        return None, None


# ============================================================
# 3. INDICATORS
# ============================================================

def rsi(
    series,
    length=14
):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / length,
        min_periods=length,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        min_periods=length,
        adjust=False
    ).mean()

    rs = (
        avg_gain
        / avg_loss.replace(
            0,
            pd.NA
        )
    )

    return 100 - (
        100 / (1 + rs)
    )


def prepare_indicators(df):

    x = df.copy()

    q = x["QQQ"]
    t = x["TQQQ_CLOSE"]

    # --------------------------------------------------------
    # QQQ DD
    # --------------------------------------------------------
    x["q_peak"] = q.rolling(
        PEAK_LEN
    ).max()

    x["q_dd"] = (
        q / x["q_peak"]
        - 1.0
    ) * 100.0

    # --------------------------------------------------------
    # QQQ filters
    # --------------------------------------------------------
    x["q_fast"] = q.rolling(
        FAST_LEN
    ).mean()

    x["q_slow"] = q.rolling(
        SLOW_LEN
    ).mean()

    x["q_filter"] = q.rolling(
        DIP2_FILTER_LEN
    ).mean()

    # --------------------------------------------------------
    # TQQQ MA
    # --------------------------------------------------------
    x["t_fast"] = t.rolling(
        FAST_LEN
    ).mean()

    x["t_slow"] = t.rolling(
        SLOW_LEN
    ).mean()

    x["t_200"] = t.rolling(
        DIP2_FILTER_LEN
    ).mean()

    # --------------------------------------------------------
    # GC / DC
    # --------------------------------------------------------
    prev_fast = (
        x["t_fast"].shift(1)
    )

    prev_slow = (
        x["t_slow"].shift(1)
    )

    x["gc_cross"] = (
        (prev_fast <= prev_slow)
        &
        (x["t_fast"] > x["t_slow"])
    )

    x["dc_cross"] = (
        (prev_fast >= prev_slow)
        &
        (x["t_fast"] < x["t_slow"])
    )

    x["gc_band"] = (
        x["t_fast"]
        >= x["t_slow"]
        * (
            1
            + GC_BAND / 100
        )
    )

    x["dc_band"] = (
        x["t_fast"]
        <= x["t_slow"]
        * (
            1
            + DC_BAND / 100
        )
    )

    x["gc_confirmed"] = (
        x["gc_cross"]
        & x["gc_band"]
    )

    x["dc_confirmed"] = (
        x["dc_cross"]
        & x["dc_band"]
    )

    # --------------------------------------------------------
    # VIX
    # --------------------------------------------------------
    x["vix_ratio"] = (
        x["VIX"]
        / x["VIX3M"]
    )

    x["dip1_threshold"] = DIP1

    x.loc[
        x["vix_ratio"] > 1.03,
        "dip1_threshold"
    ] = 12.0

    x.loc[
        x["vix_ratio"] > 1.08,
        "dip1_threshold"
    ] = 14.0

    if not USE_VIX:

        x["dip1_threshold"] = DIP1

    # --------------------------------------------------------
    # Credit
    # --------------------------------------------------------
    x["hyg_roc5"] = (
        x["HYG"].pct_change(5)
        * 100
    )

    x["lqd_roc5"] = (
        x["LQD"].pct_change(5)
        * 100
    )

    x["credit_roc"] = (
        x["hyg_roc5"]
        + x["lqd_roc5"]
    ) / 2

    x["credit_ok"] = (
        True
        if not USE_CREDIT
        else (
            x["credit_roc"] > 0
        )
    )

    # --------------------------------------------------------
    # Weekly trend
    # --------------------------------------------------------
    weekly = (
        t.resample("W-FRI")
        .last()
        .to_frame("wclose")
    )

    weekly["w5"] = (
        weekly["wclose"]
        .rolling(5)
        .mean()
    )

    weekly["w20"] = (
        weekly["wclose"]
        .rolling(20)
        .mean()
    )

    weekly["w5_rising"] = (
        weekly["w5"]
        > weekly["w5"].shift(2)
    )

    x["wclose"] = (
        weekly["wclose"]
        .reindex(
            x.index,
            method="ffill"
        )
    )

    x["w5"] = (
        weekly["w5"]
        .reindex(
            x.index,
            method="ffill"
        )
    )

    x["w20"] = (
        weekly["w20"]
        .reindex(
            x.index,
            method="ffill"
        )
    )

    x["w5_rising"] = (
        weekly["w5_rising"]
        .reindex(
            x.index,
            method="ffill"
        )
    )

    # --------------------------------------------------------
    # UP / BOTTOM
    # --------------------------------------------------------
    x["up_raw"] = (
        (x["w5"] > x["w20"])
        &
        (x["wclose"] > x["w20"])
    )

    x["bottom"] = (
        (x["q_dd"] <= -15.0)
        &
        x["w5_rising"].fillna(False)
    )

    # --------------------------------------------------------
    # Hazard
    # --------------------------------------------------------
    hazard1 = (
        x["vix_ratio"] > 1.08
    )

    hazard2 = (
        (x["hyg_roc5"] < 0)
        &
        (x["lqd_roc5"] < 0)
    )

    hazard3 = (
        x["QQQ"].pct_change(20)
        < -0.08
    )

    x["hazard_count"] = (
        hazard1
        .fillna(False)
        .astype(int)
    )

    x["hazard_count"] += (
        hazard2
        .fillna(False)
        .astype(int)
    )

    x["hazard_count"] += (
        hazard3
        .fillna(False)
        .astype(int)
    )

    # --------------------------------------------------------
    # DOWN / UP
    # --------------------------------------------------------
    x["down_state"] = (
        (
            (x["w5"] < x["w20"])
            &
            (x["wclose"] < x["w20"])
        )
        |
        (x["hazard_count"] >= 2)
    )

    x["up_state"] = (
        ~x["down_state"]
        &
        (
            x["up_raw"]
            |
            x["bottom"]
        )
    )

    # --------------------------------------------------------
    # TP2 state
    # --------------------------------------------------------
    x["tp2_state"] = "NONE"

    x.loc[
        x["down_state"],
        "tp2_state"
    ] = "DOWN"

    x.loc[
        x["up_state"] & x["bottom"],
        "tp2_state"
    ] = "BOTTOM"

    x.loc[
        x["up_state"] & ~x["bottom"],
        "tp2_state"
    ] = "UP"

    x["tp2_trigger"] = TP2_NONE

    x.loc[
        x["down_state"],
        "tp2_trigger"
    ] = TP2_DOWN

    x.loc[
        x["up_state"],
        "tp2_trigger"
    ] = TP2_UP

    x["tp2_sell_pct"] = TP2_NONE_SELL

    x.loc[
        x["down_state"],
        "tp2_sell_pct"
    ] = TP2_DOWN_SELL

    x.loc[
        x["up_state"],
        "tp2_sell_pct"
    ] = TP2_UP_SELL

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------
    x["rsi"] = rsi(
        t,
        RSI_LEN
    )

    x["rsi_bonus"] = 0.0

    if USE_RSI:

        x.loc[
            x["rsi"] < 35,
            "rsi_bonus"
        ] = 5.0

        x.loc[
            x["rsi"] < 30,
            "rsi_bonus"
        ] = 7.5

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
        "pending_qty": 0.0,

        "pending_reason": "",
        "pending_signal_date": None,

        "cash": INITIAL_CAPITAL,
        "position_qty": 0.0,
        "avg_price": 0.0,

        "equity_peak": INITIAL_CAPITAL,

        "last_alert_key": "",
        "last_daily_key": "",
        "last_processed_date": "",
    }


def load_state():

    if not STATE_FILE.exists():

        return default_state()

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:

            s = default_state()

            s.update(
                json.load(f)
            )

            return s

    except Exception:

        return default_state()


def save_state(state):

    tmp = STATE_FILE.with_suffix(
        ".tmp"
    )

    with tmp.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    tmp.replace(
        STATE_FILE
    )


# ============================================================
# 5. PORTFOLIO / EXECUTION
# ============================================================

def round_qty(qty):

    return max(
        0,
        math.floor(qty)
    )


def mark_equity(
    state,
    close
):

    return max(
        state["cash"]
        + state["position_qty"] * close,
        0.0
    )


def buy_fill(
    state,
    qty,
    price
):

    if qty <= 0:

        return

    cost = (
        qty
        * price
    )

    fee = (
        cost
        * COMMISSION
    )

    state["cash"] -= (
        cost
        + fee
    )

    old_qty = state["position_qty"]
    old_avg = state["avg_price"]

    new_qty = (
        old_qty
        + qty
    )

    if new_qty > 0:

        state["avg_price"] = (
            old_qty * old_avg
            + qty * price
        ) / new_qty

    state["position_qty"] = (
        new_qty
    )


def sell_fill(
    state,
    qty,
    price
):

    qty = min(
        max(qty, 0),
        state["position_qty"]
    )

    if qty <= 0:

        return

    proceeds = (
        qty
        * price
    )

    fee = (
        proceeds
        * COMMISSION
    )

    state["cash"] += (
        proceeds
        - fee
    )

    state["position_qty"] -= (
        qty
    )

    if state["position_qty"] <= 0:

        state["position_qty"] = 0.0
        state["avg_price"] = 0.0


# ============================================================
# TradingView Sync BUY Sizing
# ============================================================

def buy_qty(
    state,
    signal_close,
    equity_now,
    target_value
):

    current_position_value = (
        max(
            state["position_qty"],
            0
        )
        * signal_close
    )

    available_cash = (
        max(
            equity_now
            - current_position_value,
            0
        )
        * (
            1
            - CASH_BUFFER / 100
        )
    )

    estimated_fill = (
        signal_close
        + MIN_TICK
    )

    target_qty = (
        max(
            target_value
            - current_position_value,
            0
        )
        / estimated_fill
    )

    cash_qty = (
        available_cash
        / (
            estimated_fill
            * 1.0005
        )
    )

    return round_qty(
        min(
            target_qty,
            cash_qty
        )
    )


# ============================================================
# 6. SIGNAL / REPLAY ENGINE
# ============================================================

def replay(
    df,
    state,
    send_alerts=False
):

    """
    2010-02-11부터 최근 거래일까지 일봉 재생.

    신호 발생일:
        종가에서 BUY 수량 계산
        ↓
        pending_qty 저장

    다음 거래일:
        OPEN
        ↓
        pending_qty 그대로 체결
    """

    dates = df.index[
        df.index >= pd.Timestamp(
            START_DATE
        )
    ]

    if len(dates) == 0:

        raise RuntimeError(
            "START_DATE 이후 데이터가 없습니다."
        )

    gc_bar_index = None
    last_full_exit_idx = None

    # --------------------------------------------------------
    # 상태 reset
    # --------------------------------------------------------

    state["gc_bar"] = None
    state["last_full_exit_bar"] = None

    state["pending_action"] = 0
    state["pending_bar"] = None
    state["pending_position"] = 0.0
    state["pending_qty"] = 0.0

    state["pending_reason"] = ""
    state["pending_signal_date"] = None

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

        close = (
            float(
                row["TQQQ_CLOSE"]
            )
            if pd.notna(
                row["TQQQ_CLOSE"]
            )
            else None
        )

        open_ = (
            float(
                row["TQQQ_OPEN"]
            )
            if pd.notna(
                row["TQQQ_OPEN"]
            )
            else None
        )

        if close is None or open_ is None:

            continue

        # ====================================================
        # A. 전일 Signal -> 오늘 OPEN 체결
        # ====================================================

        if (
            state["pending_action"] != 0
            and state["pending_bar"] == i - 1
        ):

            action = (
                state["pending_action"]
            )

            reason = (
                state["pending_reason"]
            )

            # ------------------------------------------------
            # TradingView:
            #
            # process_orders_on_close=false
            # → 신호 다음 봉 OPEN 체결
            #
            # 평균단가에는 ±1 tick 추가하지 않는다.
            # ------------------------------------------------

            fill_price = open_

            # ------------------------------------------------
            # DIP1
            # ------------------------------------------------
            if action == 1:

                qty = round_qty(
                    state.get(
                        "pending_qty",
                        0.0
                    )
                )

                if qty >= 1:

                    buy_fill(
                        state,
                        qty,
                        fill_price
                    )

                state["stage"] = 1

                state["cycle_base_qty"] = (
                    state["position_qty"]
                )

                state["tp3_lock"] = False

            # ------------------------------------------------
            # DIP2
            # ------------------------------------------------
            elif action == 2:

                qty = round_qty(
                    state.get(
                        "pending_qty",
                        0.0
                    )
                )

                if qty >= 1:

                    buy_fill(
                        state,
                        qty,
                        fill_price
                    )

                state["stage"] = 2

                if (
                    state["cycle_base_qty"]
                    is None
                ):

                    state["cycle_base_qty"] = (
                        state["position_qty"]
                    )

            # ------------------------------------------------
            # GC
            # ------------------------------------------------
            elif action == 3:

                qty = round_qty(
                    state.get(
                        "pending_qty",
                        0.0
                    )
                )

                if qty >= 1:

                    buy_fill(
                        state,
                        qty,
                        fill_price
                    )

                state["stage"] = 3

                state["cycle_base_qty"] = (
                    state["position_qty"]
                )

                state["gc_bar"] = None
                state["tp3_lock"] = False

            # ------------------------------------------------
            # TP1
            # ------------------------------------------------
            elif action == -1:

                qty = round_qty(
                    state["position_qty"]
                    * TP1_SELL_PCT
                    / 100
                )

                sell_fill(
                    state,
                    qty,
                    fill_price
                )

                state["tp1_done"] = True

            # ------------------------------------------------
            # TP2
            # ------------------------------------------------
            elif action == -2:

                sell_pct = float(
                    state.get(
                        "_pending_tp2_sell",
                        TP2_NONE_SELL
                    )
                )

                base = float(
                    state["cycle_base_qty"]
                    or 0
                )

                qty = round_qty(
                    min(
                        base
                        * sell_pct
                        / 100,
                        state["position_qty"]
                    )
                )

                sell_fill(
                    state,
                    qty,
                    fill_price
                )

                state["tp2_done"] = True

            # ------------------------------------------------
            # DC
            # ------------------------------------------------
            elif action == -3:

                sell_fill(
                    state,
                    state["position_qty"],
                    fill_price
                )

                state["stage"] = 0

                state["tp1_done"] = False
                state["tp2_done"] = False

                state["cycle_base_qty"] = None

                state["gc_bar"] = None

                state["last_full_exit_bar"] = i

            # ------------------------------------------------
            # TP3
            # ------------------------------------------------
            elif action == -4:

                sell_fill(
                    state,
                    state["position_qty"],
                    fill_price
                )

                state["stage"] = 0

                state["tp1_done"] = False
                state["tp2_done"] = False

                state["cycle_base_qty"] = None

                state["gc_bar"] = None

                state["last_full_exit_bar"] = i

                state["tp3_lock"] = True

            if send_alerts:

                new_alerts.append(
                    (
                        "FILL",
                        dt,
                        reason,
                        fill_price,
                        state["position_qty"]
                    )
                )

            # ------------------------------------------------
            # pending reset
            # ------------------------------------------------

            state["pending_action"] = 0
            state["pending_bar"] = None
            state["pending_position"] = 0.0
            state["pending_qty"] = 0.0
            state["pending_reason"] = ""
            state["pending_signal_date"] = None

            state.pop(
                "_pending_eq_factor",
                None
            )

            state.pop(
                "_pending_tp2_sell",
                None
            )

        # ====================================================
        # B. 현재 종가 기준 Equity / EQDD
        # ====================================================

        equity = mark_equity(
            state,
            close
        )

        state["equity_peak"] = max(
            state["equity_peak"],
            equity
        )

        eq_dd = (
            (
                equity
                / state["equity_peak"]
                - 1
            )
            * 100
            if state["equity_peak"] > 0
            else 0
        )

        if eq_dd >= -EQ_START:

            eq_factor = 1.0

        elif eq_dd <= -EQ_FULL:

            eq_factor = EQ_MIN

        else:

            eq_factor = 1 - (
                (1 - EQ_MIN)
                * (
                    (
                        -eq_dd
                        - EQ_START
                    )
                    / (
                        EQ_FULL
                        - EQ_START
                    )
                )
            )

        if not USE_EQDD:

            eq_factor = 1.0

        eq_factor = max(
            EQ_MIN,
            min(
                1.0,
                eq_factor
            )
        )

        # ====================================================
        # C. GC memory
        # ====================================================

        # TradingView와 동일하게 최초 GC만 memory에 저장
        if bool(
            row["gc_confirmed"]
        ):

            if gc_bar_index is None:

                gc_bar_index = i
                state["gc_bar"] = i

        gc_eligible = (
            gc_bar_index is not None
            and i - gc_bar_index >= GC_DELAY
            and bool(
                row["t_fast"]
                > row["t_slow"]
            )
            and bool(
                row["gc_band"]
            )
        )

        can_buy_after_exit = (
            last_full_exit_idx is None
            or i - last_full_exit_idx
            > COOLDOWN_DAYS
        )

        # ----------------------------------------------------
        # 필수 데이터 준비 여부
        # ----------------------------------------------------

        ready = all(
            pd.notna(
                row.get(c)
            )
            for c in [
                "q_dd",
                "q_filter",
                "t_fast",
                "t_slow",
                "vix_ratio",
                "credit_roc"
            ]
        )

        if not ready:

            continue

        max_dip_reached = (
            float(row["q_dd"])
            <= -MAX_DIP
        )

        has_position = (
            state["position_qty"] > 0
        )

        # ====================================================
        # D. Order priority
        #
        # DC -> TP3 -> TP1 -> TP2 -> BUY
        # ====================================================

        signal = None
        reason = None

        # ----------------------------------------------------
        # DC
        # ----------------------------------------------------

        dc_condition = (
            has_position
            and bool(
                row["dc_confirmed"]
            )
        )

        # ----------------------------------------------------
        # TP3
        # ----------------------------------------------------

        tp3_condition = (
            has_position
            and not state["tp3_lock"]
            and state["cycle_base_qty"]
            is not None
            and state["avg_price"] > 0
            and close
            >= state["avg_price"]
            * (
                1
                + TP3_PCT / 100
            )
        )

        # ----------------------------------------------------
        # TP1
        # ----------------------------------------------------

        tp1_condition = (
            has_position
            and not state["tp1_done"]
            and state["avg_price"] > 0
            and close
            >= state["avg_price"]
            * (
                1
                + TP1_PCT / 100
            )
            and round_qty(
                state["position_qty"]
                * TP1_SELL_PCT
                / 100
            ) >= 1
        )

        # ----------------------------------------------------
        # TP2
        # ----------------------------------------------------

        tp2_trigger = float(
            row["tp2_trigger"]
        )

        tp2_sell_pct = float(
            row["tp2_sell_pct"]
        )

        tp2_qty = round_qty(
            min(
                float(
                    state["cycle_base_qty"]
                    or 0
                )
                * tp2_sell_pct
                / 100,
                state["position_qty"]
            )
        )

        tp2_condition = (
            has_position
            and state["tp1_done"]
            and not state["tp2_done"]
            and state["avg_price"] > 0
            and close
            >= state["avg_price"]
            * (
                1
                + tp2_trigger / 100
            )
            and tp2_qty >= 1
        )

        # ----------------------------------------------------
        # BUY allowed
        # ----------------------------------------------------

        buy_allowed = (
            can_buy_after_exit
            and not max_dip_reached
        )

        # ----------------------------------------------------
        # DIP1
        # ----------------------------------------------------

        dip1_condition = (
            buy_allowed
            and state["stage"] == 0
            and not has_position
            and float(row["q_dd"])
            <= -float(
                row["dip1_threshold"]
            )
        )

        # ----------------------------------------------------
        # DIP2
        # ----------------------------------------------------

        dip2_condition = (
            buy_allowed
            and state["stage"] == 1
            and has_position
            and float(row["q_dd"])
            <= -DIP2
            and float(row["QQQ"])
            <= float(
                row["q_filter"]
            )
            * (
                1
                + DIP2_FILTER_PCT
                / 100
            )
            and bool(
                row["credit_ok"]
            )
        )

        # ----------------------------------------------------
        # GC
        # ----------------------------------------------------

        gc_condition = (
            buy_allowed
            and gc_eligible
            and not state["tp3_lock"]
        )

        # ====================================================
        # Signal priority
        # ====================================================

        if dc_condition:

            signal, reason = -3, "DC"

            # TradingView:
            # DC 주문 발생 시 gcBar := na
            gc_bar_index = None
            state["gc_bar"] = None

        elif tp3_condition:

            signal, reason = -4, "TP3"

        elif tp1_condition:

            signal, reason = -1, "TP1"

        elif tp2_condition:

            signal, reason = (
                -2,
                f"TP2 {row['tp2_state']}"
            )

        elif gc_condition:

            signal, reason = 3, "GC"

        elif dip1_condition:

            signal, reason = 1, "DIP1"

        elif dip2_condition:

            signal, reason = 2, "DIP2"

        # ====================================================
        # E. Signal -> Pending
        # ====================================================

        if signal is not None:

            # ------------------------------------------------
            # TradingView SIZING:
            # 신호일 종가에서 주문수량 확정
            # ------------------------------------------------

            dip1_target_value = (
                equity
                * min(
                    DIP1_WEIGHT
                    + float(
                        row["rsi_bonus"]
                    ),
                    100
                )
                / 100
            )

            dip2_target_value = (
                equity
                * min(
                    DIP2_WEIGHT
                    * eq_factor
                    + float(
                        row["rsi_bonus"]
                    ),
                    100
                )
                / 100
            )

            gc_target_value = (
                equity
                * SAFETY_FACTOR
                / 100
            )

            pending_qty = 0.0

            if signal > 0:

                if signal == 1:

                    target_value = (
                        dip1_target_value
                    )

                elif signal == 2:

                    target_value = (
                        dip2_target_value
                    )

                else:

                    target_value = (
                        gc_target_value
                    )

                pending_qty = buy_qty(
                    state=state,
                    signal_close=close,
                    equity_now=equity,
                    target_value=target_value,
                )

            # ------------------------------------------------
            # pending 저장
            # ------------------------------------------------

            state["pending_action"] = signal

            state["pending_bar"] = i

            state["pending_position"] = (
                state["position_qty"]
            )

            state["pending_qty"] = (
                pending_qty
            )

            state["pending_reason"] = reason

            state["pending_signal_date"] = (
                dt.strftime("%Y-%m-%d")
            )

            if signal == 2:

                state["_pending_eq_factor"] = (
                    eq_factor
                )

            if signal == -2:

                state["_pending_tp2_sell"] = (
                    tp2_sell_pct
                )

            if send_alerts:

                new_alerts.append(
                    (
                        "SIGNAL",
                        dt,
                        reason,
                        close,
                        {
                            "qdd": float(
                                row["q_dd"]
                            ),
                            "vixratio": float(
                                row["vix_ratio"]
                            ),
                            "eqdd": float(
                                eq_dd
                            ),
                            "eqfactor": float(
                                eq_factor
                            ),
                            "stage": int(
                                state["stage"]
                            ),
                            "tp2state": str(
                                row["tp2_state"]
                            ),
                            "tp2trigger": (
                                tp2_trigger
                            ),
                            "tp2sell": (
                                tp2_sell_pct
                            ),
                        }
                    )
                )

        state["last_processed_date"] = (
            dt.strftime("%Y-%m-%d")
        )

    return state, new_alerts


# ============================================================
# 7. TELEGRAM MESSAGE
# ============================================================

def fmt(
    v,
    n=2
):

    try:

        return f"{float(v):.{n}f}"

    except Exception:

        return "-"


def build_signal_message(
    dt,
    reason,
    close,
    meta,
    row
):

    direction = (
        "🟢 매수 신호"
        if reason
        in (
            "DIP1",
            "DIP2",
            "GC"
        )
        else
        "🔴 매도 신호"
    )

    lines = [

        "❄️ <b>눈덩이 TQQQ · ULTIMATE v2.0</b>",
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


# ============================================================
# 7-1. DAILY STATUS MESSAGE
# ============================================================

def build_daily_status_message(
    dt,
    state,
    row,
    signal_today=None,
    prev_close=None,
    nq_price=None,
    nq_change_pct=None
):

    close = float(
        row["TQQQ_CLOSE"]
    )

    # ========================================================
    # TQQQ 전일 대비
    # ========================================================

    if (
        prev_close is not None
        and prev_close > 0
    ):

        chg_pct = (
            close
            / prev_close
            - 1.0
        ) * 100.0

    else:

        chg_pct = float("nan")

    # ========================================================
    # 현재 시장상태
    #
    # Python Ultimate 자동 종합 상태
    # ========================================================

    if bool(
        row["down_state"]
    ):

        market_state = "DOWN"

    elif bool(
        row["up_state"]
    ):

        market_state = "UP/BOTTOM"

    else:

        market_state = "NONE"

    # ========================================================
    # 오늘 행동지침
    # ========================================================

    if signal_today:

        # ----------------------------------------------------
        # DIP1
        # ----------------------------------------------------

        if signal_today == "DIP1":

            action_text = (
                "🟢 DIP1 매수\n"
                "➡️ 다음 정규장 시가 기준 목표비중 30%"
            )

        # ----------------------------------------------------
        # DIP2
        # ----------------------------------------------------

        elif signal_today == "DIP2":

            action_text = (
                "🟢 DIP2 추가매수\n"
                "➡️ 다음 정규장 시가 기준 "
                "EQDD 반영 최대 70%"
            )

        # ----------------------------------------------------
        # GC
        # ----------------------------------------------------

        elif signal_today == "GC":

            action_text = (
                "🟢 GC 매수\n"
                "➡️ 다음 정규장 시가 기준 목표비중 99%"
            )

        # ----------------------------------------------------
        # TP1
        # ----------------------------------------------------

        elif signal_today == "TP1":

            action_text = (
                "🔴 TP1 매도\n"
                "➡️ 다음 정규장 시가 기준 보유수량 50% 매도"
            )

        # ----------------------------------------------------
        # TP2
        # ----------------------------------------------------

        elif signal_today.startswith("TP2"):

            tp2_state = str(
                row["tp2_state"]
            )

            tp2_sell_pct = float(
                row["tp2_sell_pct"]
            )

            tp2_trigger = float(
                row["tp2_trigger"]
            )

            action_text = (
                f"🔴 TP2 {tp2_state} 매도\n"
                f"➡️ 다음 정규장 시가 기준 "
                f"기준수량 {tp2_sell_pct:.0f}% 매도"
                f" (TP2 +{tp2_trigger:.0f}%)"
            )

        # ----------------------------------------------------
        # TP3
        # ----------------------------------------------------

        elif signal_today == "TP3":

            action_text = (
                "🔴 TP3 전량매도\n"
                "➡️ 다음 정규장 시가 기준 "
                "잔여 전량 매도 · Lock ON"
            )

        # ----------------------------------------------------
        # DC
        # ----------------------------------------------------

        elif signal_today == "DC":

            action_text = (
                "🔴 DC 전량매도\n"
                "➡️ 다음 정규장 시가 기준 전량 매도"
            )

        else:

            action_text = (
                f"🚨 {html.escape(str(signal_today))}\n"
                "➡️ 다음 정규장 시가 기준 실행"
            )

    else:

        # ----------------------------------------------------
        # 신호 없는 날
        # ----------------------------------------------------

        stage = int(
            state.get(
                "stage",
                0
            )
        )

        if state.get(
            "tp3_lock",
            False
        ):

            action_text = (
                "⏸️ 매매 없음\n"
                "➡️ TP3 Lock 유지 · 다음 매수신호 대기"
            )

        elif stage == 0:

            action_text = (
                "⏸️ 매매 없음\n"
                "➡️ 신규 매수신호 대기"
            )

        elif stage == 1:

            action_text = (
                "⏸️ 매매 없음\n"
                "➡️ 기존 포지션 유지 · "
                "추가매수/익절 신호 대기"
            )

        elif stage == 2:

            action_text = (
                "⏸️ 매매 없음\n"
                "➡️ 기존 포지션 유지 · "
                "익절/GC/DC 신호 대기"
            )

        else:

            action_text = (
                "⏸️ 매매 없음\n"
                "➡️ 기존 포지션 유지 · "
                "익절/DC 신호 대기"
            )

    # ========================================================
    # 등락률
    # ========================================================

    change_text = (
        f"{chg_pct:+.2f}%"
        if pd.notna(chg_pct)
        else "-"
    )

    # ========================================================
    # Nasdaq-100 Futures
    # ========================================================

    if nq_price is not None:

        nq_change_text = (
            f"{nq_change_pct:+.2f}%"
            if nq_change_pct is not None
            else "-"
        )

        nq_line = (
            f"나스닥100 선물: "
            f"<b>{nq_price:,.2f}</b> "
            f"({nq_change_text})"
        )

    else:

        nq_line = (
            "나스닥100 선물: -"
        )

    # ========================================================
    # 최종 메시지
    # ========================================================

    lines = [

        "❄️ <b>눈덩이 티큐 궁극</b>",
        "",
        "📊 <b>일일 현황</b>",
        f"평가기준일: {dt.strftime('%Y-%m-%d')}",
        "",
        f"TQQQ 종가: <b>{fmt(close)}</b> "
        f"({change_text})",
        f"QQQ 종가: <b>{fmt(row['QQQ'])}</b>",
        nq_line,
        "",
        f"🧭 <b>현재 시장상태: {market_state}</b>",
        "",
        "📌 <b>오늘 행동지침</b>",
        action_text,
    ]

    return "\n".join(lines)


# ============================================================
# 8. MAIN
# ============================================================

def calc_signal():

    # ========================================================
    # 데이터
    # ========================================================

    data = download_data()

    data = prepare_indicators(
        data
    )

    # ========================================================
    # 마지막 확정 TQQQ 거래일
    # ========================================================

    latest_tqqq = (
        data["TQQQ_CLOSE"]
        .dropna()
        .index
        .max()
    )

    if pd.isna(
        latest_tqqq
    ):

        print(
            "[SKIP] 유효한 TQQQ 일봉 데이터가 없습니다."
        )

        return

    today = latest_tqqq

    print(
        "[DATA] Ultimate 기준일: "
        f"{today.strftime('%Y-%m-%d')}"
    )

    # ========================================================
    # Nasdaq-100 Futures
    # ========================================================

    nq_price, nq_change_pct = (
        get_nasdaq_futures()
    )

    # ========================================================
    # State
    # ========================================================

    state = load_state()

    # 전체 replay
    state, events = replay(
        data,
        state,
        send_alerts=True
    )

    # ========================================================
    # 오늘 데이터
    # ========================================================

    last_date = today

    # ========================================================
    # TQQQ 전일 종가
    # ========================================================

    prev_close = None

    try:

        pos = data.index.get_loc(
            last_date
        )

        if pos > 0:

            prev_close = float(
                data.iloc[
                    pos - 1
                ]["TQQQ_CLOSE"]
            )

    except Exception:

        prev_close = None

    # ========================================================
    # 오늘 Signal 확인
    # ========================================================

    today_signal = None

    sent = 0

    for event in events:

        kind = event[0]
        dt = event[1]

        if dt != last_date:

            continue

        if kind == "SIGNAL":

            _, dt, reason, close, meta = event

            today_signal = reason

            key = (
                f"SIGNAL|"
                f"{dt.strftime('%Y-%m-%d')}|"
                f"{reason}"
            )

            if (
                state.get(
                    "last_alert_key"
                )
                == key
            ):

                continue

            row = data.loc[
                dt
            ]

            msg = build_signal_message(
                dt,
                reason,
                close,
                meta,
                row
            )

            send_telegram(
                msg
            )

            state["last_alert_key"] = key

            sent += 1

    # ========================================================
    # 매일 일일현황 전송
    # ========================================================

    daily_key = (
        f"DAILY|"
        f"{last_date.strftime('%Y-%m-%d')}"
    )

    if (
        state.get(
            "last_daily_key"
        )
        != daily_key
    ):

        latest_row = data.loc[
            last_date
        ]

        daily_msg = build_daily_status_message(
            last_date,
            state,
            latest_row,
            today_signal,
            prev_close,
            nq_price,
            nq_change_pct
        )

        send_telegram(
            daily_msg
        )

        state["last_daily_key"] = (
            daily_key
        )

    # ========================================================
    # State 저장
    # ========================================================

    save_state(
        state
    )

    print(
        f"[OK] "
        f"{today.strftime('%Y-%m-%d')} | "
        f"Stage={state['stage']} | "
        f"TP1={state['tp1_done']} | "
        f"TP2={state['tp2_done']} | "
        f"TP3Lock={state['tp3_lock']} | "
        f"NewAlerts={sent}"
    )


if __name__ == "__main__":

    calc_signal()
