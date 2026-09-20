#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
눈덩이 TQQQ ULTIMATE - TradingView Pine MATCH Alert
----------------------------------------------------
목적:
  현재 확정된 TradingView Pine v6 코드의 일봉 신호를
  Python에서 최대한 동일한 조건으로 재현하고
  신호가 발생한 당일 종가 확정 후 Telegram으로 알림.

핵심 원칙:
  1) Pine의 GC/DC Gap 조건 그대로 사용
  2) Pine의 QQQ DD / TQQQ Z200 / VIX / Credit 조건 그대로 사용
  3) Pine의 자동 시장상태 DOWN / UP/BOTTOM / NEUTRAL 그대로 사용
  4) Pine의 TP2 자동 연동(+90/45, +100/35, +115/25) 그대로 사용
  5) Pine의 한 봉 한 액션 우선순위 그대로 사용
  6) Pine의 Stage / avgPrice / TP 상태 변이를 그대로 재현
  7) 알림은 '신호일 종가' 기준으로 전송
  8) 별도의 다음날 OPEN 체결 로직은 사용하지 않음

주의:
  - TradingView와 Yahoo Finance 데이터가 완전히 동일하지 않을 수 있으므로,
    데이터 제공처 차이 때문에 극히 일부 날짜에서 차이가 날 가능성은 있음.
  - 아래 전략 상수는 현재 TradingView Pine 코드의 기본값과 동일하게 맞춰져 있음.
  - TradingView에서 입력값을 바꾸면 이 Python 상수도 동일하게 바꿔야 완전 동일해짐.

필수 환경변수:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

설치:
  pip install yfinance pandas requests

실행:
  python Snowball_TQQQ_ULTIMATE_TV_MATCH.py
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
# 0. SETTINGS - 현재 TradingView Pine v6 기본값과 동일
# ============================================================

START_DATE = "2010-02-11"
DOWNLOAD_START = "2009-01-01"

FAST_LEN = 5
SLOW_LEN = 220
MA200_LEN = 200

GC_PCT = 2.90
DC_PCT = -0.10

DIP_LOOKBACK = 126
DIP1_BASE_PCT = -10.0
DIP1_VIX_PCT = -12.0
DIP1_EXTREME_PCT = -14.0
DIP2_PCT = -22.0
DIP2_Z200_MAX = -7.0
MAX_DIP_LIMIT = -40.0
USE_VIX_DIP = True

VIX_NORMAL = 1.03
VIX_EXTREME = 1.08

USE_CREDIT = True
CREDIT_ROC_LEN = 5
QQQ_CRASH_LEN = 20
QQQ_CRASH_PCT = -8.0

TP1_PCT = 15.0
TP1_SELL_PCT = 50

TP2_DOWN_PCT = 90.0
TP2_DOWN_SELL_PCT = 45

TP2_NONE_PCT = 100.0
TP2_NONE_SELL_PCT = 35

TP2_UP_PCT = 115.0
TP2_UP_SELL_PCT = 25

TP3_PCT = 350.0

MY_AVG_PRICE = 0.0

INITIAL_CAPITAL = 10000.0

STATE_FILE = Path(__file__).with_name(
    "snowball_tv_match_state.json"
)


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

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )
    response.raise_for_status()


# ============================================================
# 2. DATA
# ============================================================

SYMBOLS = {
    "TQQQ": "TQQQ",
    "QQQ": "QQQ",
    "VIX": "^VIX",
    "VIX3M": "^VIX3M",
    "HYG": "HYG",
    "LQD": "LQD",
}


def _download_series(ticker: str, start: str, end: str) -> pd.Series:
    data = yf.download(
        ticker,
        start=start,
        end=end,
        auto_adjust=False,
        progress=False,
        threads=False,
        group_by="column",
    )

    if data.empty:
        raise RuntimeError(f"{ticker}: 데이터를 받지 못했습니다.")

    if isinstance(data.columns, pd.MultiIndex):
        # yfinance가 (Price, Ticker) 또는 (Ticker, Price) 중 무엇을 주든 처리
        if ticker in data.columns.get_level_values(0):
            data = data[ticker]
        elif ticker in data.columns.get_level_values(1):
            data = data.xs(ticker, axis=1, level=1)

    if "Close" not in data.columns:
        raise RuntimeError(f"{ticker}: Close 컬럼이 없습니다.")

    series = pd.to_numeric(data["Close"], errors="coerce")
    series.index = pd.to_datetime(series.index).tz_localize(None)
    return series.rename(ticker)


def _download_ohlc(ticker: str, start: str, end: str) -> pd.DataFrame:
    data = yf.download(
        ticker,
        start=start,
        end=end,
        auto_adjust=False,
        progress=False,
        threads=False,
        group_by="column",
    )

    if data.empty:
        raise RuntimeError(f"{ticker}: OHLC 데이터를 받지 못했습니다.")

    if isinstance(data.columns, pd.MultiIndex):
        if ticker in data.columns.get_level_values(0):
            data = data[ticker]
        elif ticker in data.columns.get_level_values(1):
            data = data.xs(ticker, axis=1, level=1)

    needed = ["Open", "High", "Low", "Close"]
    missing = [c for c in needed if c not in data.columns]
    if missing:
        raise RuntimeError(f"{ticker}: OHLC 컬럼 누락 {missing}")

    data = data[needed].copy()
    for col in needed:
        data[col] = pd.to_numeric(data[col], errors="coerce")

    data.index = pd.to_datetime(data.index).tz_localize(None)
    return data


def download_data() -> pd.DataFrame:
    # 현재 날짜 + 여유 구간
    end = (
        pd.Timestamp.now(tz="America/New_York").normalize()
        + pd.Timedelta(days=3)
    ).strftime("%Y-%m-%d")

    tqqq_ohlc = _download_ohlc("TQQQ", DOWNLOAD_START, end)
    qqq_ohlc = _download_ohlc("QQQ", DOWNLOAD_START, end)

    vix = _download_series("^VIX", DOWNLOAD_START, end).rename("VIX")
    vix3m = _download_series("^VIX3M", DOWNLOAD_START, end).rename("VIX3M")
    hyg = _download_series("HYG", DOWNLOAD_START, end)
    lqd = _download_series("LQD", DOWNLOAD_START, end)

    df = pd.concat(
        [
            tqqq_ohlc.add_prefix("TQQQ_"),
            qqq_ohlc.add_prefix("QQQ_"),
            vix,
            vix3m,
            hyg,
            lqd,
        ],
        axis=1,
        sort=False,
    ).sort_index()

    df = df[~df.index.duplicated(keep="last")]
    return df


# ============================================================
# 3. WEEKLY VALUES - Pine request.security(..., "W", ...)
# ============================================================


def add_weekly_state_columns(x: pd.DataFrame) -> pd.DataFrame:
    """
    Pine:
      request.security(
        syminfo.tickerid,
        "W",
        [
          close[1],
          ta.sma(close, 5)[1],
          ta.sma(close, 20)[1],
          ta.sma(close, 5)[3]
        ],
        lookahead=barmerge.lookahead_on
      )

    현재 일봉 차트에서는 '현재 진행 중인 주'에 대해
    이전 완료 주봉의 값들을 반환하는 형태로 맞춘다.
    """

    tclose = x["TQQQ_Close"].copy()

    weekly = tclose.resample("W-FRI").last().dropna()
    weekly["w5"] = weekly.rolling(5, min_periods=5).mean()
    weekly["w20"] = weekly.rolling(20, min_periods=20).mean()

    # 현재 주 -> 이전 완료 주봉 값
    weekly["prev_close"] = weekly["close"].shift(1)
    weekly["prev_w5"] = weekly["w5"].shift(1)
    weekly["prev_w20"] = weekly["w20"].shift(1)

    # Pine ta.sma(close, 5)[3] on current weekly bar.
    # current week index 0 -> prior week index 1 -> 2 weeks before index 3.
    weekly["prev_w5_2w"] = weekly["w5"].shift(3)

    # Each daily bar in a given week receives that week's previous-week values.
    week_key = x.index.to_period("W-FRI").end_time.normalize()
    week_key = pd.DatetimeIndex(week_key)

    lookup = weekly[
        ["prev_close", "prev_w5", "prev_w20", "prev_w5_2w"]
    ].copy()
    lookup.index = pd.DatetimeIndex(lookup.index).normalize()

    mapped = lookup.reindex(week_key)
    mapped.index = x.index

    x["stateWeeklyClose"] = mapped["prev_close"].to_numpy()
    x["stateWeeklyMA5"] = mapped["prev_w5"].to_numpy()
    x["stateWeeklyMA20"] = mapped["prev_w20"].to_numpy()
    x["stateWeeklyMA5_2W"] = mapped["prev_w5_2w"].to_numpy()

    return x


# ============================================================
# 4. INDICATORS - Pine 계산 재현
# ============================================================


def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()

    # --------------------------------------------------------
    # TQQQ MAs
    # --------------------------------------------------------
    close = x["TQQQ_Close"]

    x["ma5"] = close.rolling(FAST_LEN, min_periods=FAST_LEN).mean()
    x["ma200"] = close.rolling(MA200_LEN, min_periods=MA200_LEN).mean()
    x["ma220"] = close.rolling(SLOW_LEN, min_periods=SLOW_LEN).mean()

    # --------------------------------------------------------
    # GC / DC Gap
    # --------------------------------------------------------
    x["gc_gap"] = (x["ma5"] - x["ma220"]) / x["ma220"]
    x["gc_gap_prev"] = x["gc_gap"].shift(1)

    gc_threshold = GC_PCT / 100.0
    dc_threshold = abs(DC_PCT) / 100.0

    x["goldCross"] = (
        x["gc_gap"].notna()
        & x["gc_gap_prev"].notna()
        & (x["gc_gap_prev"] < gc_threshold)
        & (x["gc_gap"] >= gc_threshold)
    )

    x["deadCross"] = (
        x["gc_gap"].notna()
        & x["gc_gap_prev"].notna()
        & (x["gc_gap_prev"] > -dc_threshold)
        & (x["gc_gap"] <= -dc_threshold)
    )

    # --------------------------------------------------------
    # QQQ calculations
    # --------------------------------------------------------
    qqq_close = x["QQQ_Close"]
    qqq_high = x["QQQ_High"]

    x["qqq_high_126"] = qqq_high.rolling(
        DIP_LOOKBACK,
        min_periods=DIP_LOOKBACK,
    ).max()

    x["qqq_dd"] = (
        x["QQQ_Close"] / x["qqq_high_126"] - 1.0
    ) * 100.0

    x["qqq_ma200"] = qqq_close.rolling(
        MA200_LEN,
        min_periods=MA200_LEN,
    ).mean()

    x["qqq_z200"] = (
        qqq_close / x["qqq_ma200"] - 1.0
    ) * 100.0

    x["tqqq_z200"] = (
        close / x["ma200"] - 1.0
    ) * 100.0

    # --------------------------------------------------------
    # VIX / VIX3M
    # --------------------------------------------------------
    x["vixRatio"] = x["VIX"] / x["VIX3M"]

    def calc_dip1_trigger(row):
        ratio = row["vixRatio"]
        if (not USE_VIX_DIP) or pd.isna(ratio):
            return DIP1_BASE_PCT
        if ratio > VIX_EXTREME:
            return DIP1_EXTREME_PCT
        if ratio > VIX_NORMAL:
            return DIP1_VIX_PCT
        return DIP1_BASE_PCT

    x["dip1Trigger"] = x.apply(calc_dip1_trigger, axis=1)

    # --------------------------------------------------------
    # Credit / QQQ risk
    # --------------------------------------------------------
    x["hygROC"] = x["HYG"].pct_change(CREDIT_ROC_LEN) * 100.0
    x["lqdROC"] = x["LQD"].pct_change(CREDIT_ROC_LEN) * 100.0
    x["qqqROC"] = qqq_close.pct_change(QQQ_CRASH_LEN) * 100.0

    x["vixHazard"] = (
        x["vixRatio"].notna()
        & (x["vixRatio"] > VIX_EXTREME)
    )

    x["creditHazard"] = (
        USE_CREDIT
        & x["hygROC"].notna()
        & x["lqdROC"].notna()
        & (x["hygROC"] < 0)
        & (x["lqdROC"] < 0)
    )

    x["qqqHazard"] = (
        x["qqqROC"].notna()
        & (x["qqqROC"] < QQQ_CRASH_PCT)
    )

    x["hazardCount"] = (
        x["vixHazard"].fillna(False).astype(int)
        + x["creditHazard"].fillna(False).astype(int)
        + x["qqqHazard"].fillna(False).astype(int)
    )

    x["riskStatus"] = "LOW"
    x.loc[x["hazardCount"] == 1, "riskStatus"] = "WATCH"
    x.loc[x["hazardCount"] >= 2, "riskStatus"] = "HIGH"

    # --------------------------------------------------------
    # Weekly state
    # --------------------------------------------------------
    x = add_weekly_state_columns(x)

    x["stateWeeklyDown"] = (
        x["stateWeeklyClose"].notna()
        & x["stateWeeklyMA5"].notna()
        & x["stateWeeklyMA20"].notna()
        & (x["stateWeeklyMA5"] < x["stateWeeklyMA20"])
        & (x["stateWeeklyClose"] < x["stateWeeklyMA20"])
    )

    x["stateHazardDown"] = x["hazardCount"] >= 2

    x["stateAutoDown"] = (
        x["stateWeeklyDown"]
        | x["stateHazardDown"]
    )

    x["stateWeeklyUp"] = (
        x["stateWeeklyClose"].notna()
        & x["stateWeeklyMA5"].notna()
        & x["stateWeeklyMA20"].notna()
        & (x["stateWeeklyMA5"] > x["stateWeeklyMA20"])
        & (x["stateWeeklyClose"] > x["stateWeeklyMA20"])
    )

    x["stateBottomCondition"] = (
        x["qqq_dd"].notna()
        & (x["qqq_dd"] <= -15.0)
        & x["stateWeeklyMA5"].notna()
        & x["stateWeeklyMA5_2W"].notna()
        & (x["stateWeeklyMA5"] > x["stateWeeklyMA5_2W"])
    )

    x["autoMarketState"] = "NONE"
    x.loc[x["stateAutoDown"], "autoMarketState"] = "DOWN"
    x.loc[
        (~x["stateAutoDown"])
        & (x["stateWeeklyUp"] | x["stateBottomCondition"]),
        "autoMarketState",
    ] = "UP/BOTTOM"

    x["autoMarketStateDisplay"] = x["autoMarketState"].replace(
        "NONE",
        "NEUTRAL",
    )

    # --------------------------------------------------------
    # TP2 automatic mapping
    # --------------------------------------------------------
    x["tp2Pct"] = TP2_NONE_PCT
    x["tp2SellPct"] = TP2_NONE_SELL_PCT

    x.loc[
        x["autoMarketState"] == "DOWN",
        "tp2Pct",
    ] = TP2_DOWN_PCT
    x.loc[
        x["autoMarketState"] == "DOWN",
        "tp2SellPct",
    ] = TP2_DOWN_SELL_PCT

    x.loc[
        x["autoMarketState"] == "UP/BOTTOM",
        "tp2Pct",
    ] = TP2_UP_PCT
    x.loc[
        x["autoMarketState"] == "UP/BOTTOM",
        "tp2SellPct",
    ] = TP2_UP_SELL_PCT

    # --------------------------------------------------------
    # DIP conditions
    # --------------------------------------------------------
    x["dip1Cond"] = (
        x["qqq_dd"].notna()
        & (x["qqq_dd"] <= x["dip1Trigger"])
        & (x["qqq_dd"] > MAX_DIP_LIMIT)
    )

    x["dip2Cond"] = (
        x["qqq_dd"].notna()
        & (x["qqq_dd"] <= DIP2_PCT)
        & (x["qqq_dd"] > MAX_DIP_LIMIT)
        & x["tqqq_z200"].notna()
        & (x["tqqq_z200"] <= DIP2_Z200_MAX)
    )

    x["dip1SignalRaw"] = (
        x["dip1Cond"]
        & ~x["dip1Cond"].shift(1).fillna(False)
    )

    x["dip2SignalRaw"] = (
        x["dip2Cond"]
        & ~x["dip2Cond"].shift(1).fillna(False)
    )

    return x


# ============================================================
# 5. STATE
# ============================================================


def default_state():
    return {
        "stage": 0,
        "avgPrice": None,
        "tp1Fired": False,
        "tp2Fired": False,
        "tp3Fired": False,
        "tp3Lock": False,
        "last_alert_key": "",
        "last_daily_key": "",
        "last_processed_date": "",
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = default_state()
            state.update(json.load(f))
            return state
    except Exception:
        return default_state()


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


# ============================================================
# 6. HELPERS
# ============================================================


def fmt(value, digits=2):
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):.{digits}f}"


def stage_text(stage: int) -> str:
    if stage == 0:
        return "현금 대기"
    if stage == 1:
        return "DIP1"
    if stage == 2:
        return "DIP2"
    return "GC 보유"


def vix_status(ratio):
    if ratio is None or pd.isna(ratio):
        return "N/A"
    if ratio > VIX_EXTREME:
        return "EXTREME"
    if ratio > VIX_NORMAL:
        return "RISK"
    return "NORMAL"


# ============================================================
# 7. PINE STATE REPLAY
# ============================================================


def replay_pine_signals(df: pd.DataFrame, start_date: str):
    """
    현재 Pine 코드의 순서 그대로 일봉을 재생한다.

    중요:
      - TP/신호 조건은 '액션 mutation 전' state를 사용
      - 액션 후 stage/avgPrice/tp flags를 변경
      - Pine의 one-bar-one-action 우선순위 동일
    """

    state = default_state()
    events = []

    dates = df.index[df.index >= pd.Timestamp(start_date)]
    if len(dates) == 0:
        raise RuntimeError("START_DATE 이후 데이터가 없습니다.")

    manual_avg = MY_AVG_PRICE > 0.0
    display_avg = None

    prev_dip1_cond = False
    prev_dip2_cond = False

    for dt in dates:
        row = df.loc[dt]

        close = row["TQQQ_Close"]
        if pd.isna(close):
            continue
        close = float(close)

        # ----------------------------------------------------
        # Pine displayAvg
        # ----------------------------------------------------
        if manual_avg:
            display_avg = MY_AVG_PRICE
        else:
            display_avg = state["avgPrice"]

        # ----------------------------------------------------
        # Pine TP prices - mutation 전 상태
        # ----------------------------------------------------
        tp1_price = (
            display_avg * (1.0 + TP1_PCT / 100.0)
            if display_avg is not None
            else None
        )

        current_tp2_pct = float(row["tp2Pct"])
        current_tp2_sell_pct = float(row["tp2SellPct"])

        tp2_price = (
            display_avg * (1.0 + current_tp2_pct / 100.0)
            if display_avg is not None
            else None
        )

        tp3_price = (
            display_avg * (1.0 + TP3_PCT / 100.0)
            if display_avg is not None
            else None
        )

        in_position = (
            state["stage"] >= 1
            or manual_avg
        )

        tp1_hit = (
            in_position
            and not state["tp1Fired"]
            and tp1_price is not None
            and close >= tp1_price
        )

        tp2_hit = (
            in_position
            and state["tp1Fired"]
            and not state["tp2Fired"]
            and tp2_price is not None
            and close >= tp2_price
        )

        tp3_hit = (
            in_position
            and not state["tp3Fired"]
            and not state["tp3Lock"]
            and tp3_price is not None
            and close >= tp3_price
        )

        # ----------------------------------------------------
        # DIP conditions
        # ----------------------------------------------------
        dip1_cond = bool(row["dip1Cond"]) if pd.notna(row["dip1Cond"]) else False
        dip2_cond = bool(row["dip2Cond"]) if pd.notna(row["dip2Cond"]) else False

        dip1_signal = (
            state["stage"] == 0
            and dip1_cond
            and not prev_dip1_cond
        )

        dip2_signal = (
            state["stage"] == 1
            and dip2_cond
            and not prev_dip2_cond
        )

        prev_dip1_cond = dip1_cond
        prev_dip2_cond = dip2_cond

        # ----------------------------------------------------
        # Pine one-bar-one-action priority
        # ----------------------------------------------------
        action = ""

        if bool(row["deadCross"]) and state["stage"] > 0:
            action = "DC"
        elif tp3_hit:
            action = "TP3"
        elif tp1_hit:
            action = "TP1"
        elif tp2_hit:
            action = "TP2"
        elif (
            bool(row["goldCross"])
            and state["stage"] < 3
            and not state["tp3Lock"]
            and float(row["qqq_dd"]) > MAX_DIP_LIMIT
        ):
            action = "GC"
        elif dip1_signal:
            action = "DIP1"
        elif dip2_signal:
            action = "DIP2"

        # ----------------------------------------------------
        # Capture state before mutation
        # ----------------------------------------------------
        pre_stage = state["stage"]
        pre_avg = state["avgPrice"]

        # ----------------------------------------------------
        # Pine mutation - 그대로
        # ----------------------------------------------------
        if action == "DC":
            state["stage"] = 0
            state["avgPrice"] = None
            state["tp1Fired"] = False
            state["tp2Fired"] = False
            state["tp3Fired"] = False
            state["tp3Lock"] = False

        elif action == "TP3":
            state["stage"] = 0
            state["avgPrice"] = None
            state["tp1Fired"] = False
            state["tp2Fired"] = False
            state["tp3Fired"] = True
            state["tp3Lock"] = True

        elif action == "TP1":
            state["tp1Fired"] = True

        elif action == "TP2":
            state["tp2Fired"] = True

        elif action == "GC":
            if state["stage"] == 0:
                state["avgPrice"] = close
                state["stage"] = 3
                state["tp1Fired"] = False
                state["tp2Fired"] = False
                state["tp3Fired"] = False
                state["tp3Lock"] = False

            elif state["stage"] == 1:
                state["avgPrice"] = (
                    (state["avgPrice"] + close) / 2.0
                    if state["avgPrice"] is not None
                    else close
                )
                state["stage"] = 3
                # TP state preserved

            elif state["stage"] == 2:
                state["avgPrice"] = (
                    state["avgPrice"] * 0.7 + close * 0.3
                    if state["avgPrice"] is not None
                    else close
                )
                state["stage"] = 3
                # TP state preserved

        elif action == "DIP1":
            state["tp3Lock"] = False
            if state["stage"] == 0:
                state["avgPrice"] = close
                state["stage"] = 1
                state["tp1Fired"] = False
                state["tp2Fired"] = False
                state["tp3Fired"] = False

        elif action == "DIP2":
            if state["stage"] == 1:
                state["avgPrice"] = (
                    (
                        state["avgPrice"] * 0.3
                        + close * 0.4
                    ) / 0.7
                    if state["avgPrice"] is not None
                    else close
                )
                state["stage"] = 2

        # ----------------------------------------------------
        # Event after Pine mutation (label stageText also sees
        # the mutated state because it is calculated later).
        # ----------------------------------------------------
        if action:
            events.append(
                {
                    "date": dt,
                    "action": action,
                    "close": close,
                    "pre_stage": pre_stage,
                    "post_stage": state["stage"],
                    "pre_avg": pre_avg,
                    "post_avg": state["avgPrice"],
                    "autoMarketState": str(row["autoMarketState"]),
                    "autoMarketStateDisplay": str(row["autoMarketStateDisplay"]),
                    "tp2Pct": current_tp2_pct,
                    "tp2SellPct": current_tp2_sell_pct,
                    "qqq_dd": float(row["qqq_dd"]) if pd.notna(row["qqq_dd"]) else None,
                    "tqqq_z200": float(row["tqqq_z200"]) if pd.notna(row["tqqq_z200"]) else None,
                    "vixRatio": float(row["vixRatio"]) if pd.notna(row["vixRatio"]) else None,
                    "riskStatus": str(row["riskStatus"]),
                    "dip1Trigger": float(row["dip1Trigger"]),
                    "gc_gap": float(row["gc_gap"]) if pd.notna(row["gc_gap"]) else None,
                }
            )

    state["last_processed_date"] = dates[-1].strftime("%Y-%m-%d")
    return state, events


# ============================================================
# 8. TELEGRAM MESSAGE
# ============================================================


def build_signal_message(event: dict) -> str:
    action = event["action"]
    direction = (
        "🟢 매수 신호"
        if action in ("GC", "DIP1", "DIP2")
        else "🔴 매도 신호"
    )

    lines = [
        "❄️ <b>눈덩이 TQQQ · ULTIMATE TV MATCH</b>",
        "",
        f"<b>{direction}</b>",
        f"신호: <b>{html.escape(action)}</b>",
        f"신호일: {event['date'].strftime('%Y-%m-%d')}",
        f"TQQQ 종가: <b>{fmt(event['close'])}</b>",
        "",
        f"자동 시장상태: <b>{html.escape(event['autoMarketStateDisplay'])}</b>",
        f"TP2 기준: +{fmt(event['tp2Pct'], 0)}% / {fmt(event['tp2SellPct'], 0)}%",
        f"QQQ DD: {fmt(event['qqq_dd'])}%",
        f"TQQQ Z200: {fmt(event['tqqq_z200'])}%",
        f"VIX Ratio: {fmt(event['vixRatio'], 2)} · {html.escape(vix_status(event['vixRatio']))}",
        f"Risk: {html.escape(event['riskStatus'])}",
        f"Stage: {stage_text(event['post_stage'])}",
    ]

    if action == "DIP1":
        lines += [
            "",
            f"DIP1 기준: {fmt(event['dip1Trigger'])}%",
            "TQQQ 30%",
        ]
    elif action == "DIP2":
        lines += [
            "",
            f"DIP2 QQQ DD: {fmt(DIP2_PCT, 0)}%",
            f"TQQQ Z200: {fmt(DIP2_Z200_MAX, 0)}% 이하",
            "TQQQ 70%",
        ]
    elif action == "GC":
        lines += [
            "",
            f"GC Gap: {fmt(event['gc_gap'] * 100 if event['gc_gap'] is not None else None)}%",
            f"GC 기준: +{GC_PCT:.2f}%",
            "TQQQ 100%",
        ]
    elif action == "TP1":
        lines += [
            "",
            f"TP1: +{TP1_PCT:.0f}%",
            f"매도: {TP1_SELL_PCT}%",
        ]
    elif action == "TP2":
        lines += [
            "",
            f"TP2 상태: {html.escape(event['autoMarketStateDisplay'])}",
            f"TP2: +{event['tp2Pct']:.0f}%",
            f"기준수량 매도: {event['tp2SellPct']:.0f}%",
        ]
    elif action == "TP3":
        lines += [
            "",
            f"TP3: +{TP3_PCT:.0f}%",
            "전량 매도 / Lock ON",
        ]
    elif action == "DC":
        lines += [
            "",
            f"DC 기준: {DC_PCT:.2f}%",
            "전량 매도",
        ]

    lines += [
        "",
        "📌 TradingView Pine MATCH 신호",
        "➡️ 신호 발생일 종가 확정 기준",
    ]

    return "\n".join(lines)


def build_daily_status_message(
    df: pd.DataFrame,
    state: dict,
    last_date: pd.Timestamp,
    today_event: dict | None,
):
    row = df.loc[last_date]

    close = float(row["TQQQ_Close"])
    prev_dates = df.index[df.index < last_date]
    prev_close = (
        float(df.loc[prev_dates[-1], "TQQQ_Close"])
        if len(prev_dates) else None
    )

    change_pct = (
        (close / prev_close - 1.0) * 100.0
        if prev_close and prev_close > 0
        else None
    )

    market_state = str(row["autoMarketStateDisplay"])

    if today_event:
        action_text = f"신호: {today_event['action']}"
    elif state["tp3Lock"]:
        action_text = "⏸️ 매매 없음 · TP3 Lock 유지"
    elif state["stage"] == 0:
        action_text = "⏸️ 매매 없음 · 신규 매수신호 대기"
    elif state["stage"] == 1:
        action_text = "⏸️ 매매 없음 · 추가매수/익절 신호 대기"
    elif state["stage"] == 2:
        action_text = "⏸️ 매매 없음 · 익절/GC/DC 신호 대기"
    else:
        action_text = "⏸️ 매매 없음 · 익절/DC 신호 대기"

    lines = [
        "❄️ <b>눈덩이 티큐 궁극</b>",
        "",
        "📊 <b>일일 현황</b>",
        f"평가기준일: {last_date.strftime('%Y-%m-%d')}",
        "",
        f"TQQQ 종가: <b>{fmt(close)}</b>",
        f"QQQ 종가: <b>{fmt(row['QQQ_Close'])}</b>",
        "",
        f"🧭 <b>현재 시장상태: {html.escape(market_state)}</b>",
        f"Risk: {html.escape(str(row['riskStatus']))}",
        f"QQQ DD: {fmt(row['qqq_dd'])}%",
        f"VIX Ratio: {fmt(row['vixRatio'], 2)}",
        f"TP2: +{float(row['tp2Pct']):.0f}% / {float(row['tp2SellPct']):.0f}%",
        f"Stage: {stage_text(state['stage'])}",
        "",
        "📌 <b>오늘 행동지침</b>",
        action_text,
    ]

    change_text = f"{change_pct:+.2f}%" if change_pct is not None else "-"
    lines[5] = f"TQQQ 종가: <b>{fmt(close)}</b> ({change_text})"

    if today_event:
        lines += [
            f"✅ 오늘 신호: <b>{html.escape(today_event['action'])}</b>",
        ]

    return "\n".join(lines)


# ============================================================
# 9. MAIN
# ============================================================


def main():
    print("[DATA] Yahoo Finance 데이터 다운로드 중...")
    data = download_data()

    print("[CALC] TradingView Pine MATCH 지표 계산 중...")
    data = prepare_indicators(data)

    valid = data["TQQQ_Close"].dropna()
    if valid.empty:
        raise RuntimeError("유효한 TQQQ 데이터가 없습니다.")

    latest_date = valid.index.max()
    print(f"[DATA] 기준일: {latest_date.strftime('%Y-%m-%d')}")

    state, events = replay_pine_signals(data, START_DATE)

    today_events = [e for e in events if e["date"] == latest_date]
    today_event = today_events[-1] if today_events else None

    # --------------------------------------------------------
    # Signal alert - 당일 종가 기준
    # --------------------------------------------------------
    sent = 0

    if today_event:
        action = today_event["action"]
        key = f"SIGNAL|{latest_date.strftime('%Y-%m-%d')}|{action}"

        if state.get("last_alert_key") != key:
            send_telegram(build_signal_message(today_event))
            state["last_alert_key"] = key
            sent = 1
            print(f"[ALERT] {latest_date.strftime('%Y-%m-%d')} {action}")
        else:
            print(f"[SKIP] 이미 전송한 신호: {key}")

    # --------------------------------------------------------
    # Daily status - 하루 1회
    # --------------------------------------------------------
    daily_key = f"DAILY|{latest_date.strftime('%Y-%m-%d')}"

    if state.get("last_daily_key") != daily_key:
        daily_msg = build_daily_status_message(
            data,
            state,
            latest_date,
            today_event,
        )
        send_telegram(daily_msg)
        state["last_daily_key"] = daily_key

    state["last_processed_date"] = latest_date.strftime("%Y-%m-%d")
    save_state(state)

    print(
        f"[OK] {latest_date.strftime('%Y-%m-%d')} | "
        f"Stage={state['stage']} | "
        f"TP1={state['tp1Fired']} | "
        f"TP2={state['tp2Fired']} | "
        f"TP3Lock={state['tp3Lock']} | "
        f"Market={data.loc[latest_date, 'autoMarketStateDisplay']} | "
        f"TP2={float(data.loc[latest_date, 'tp2Pct']):.0f}% / "
        f"{float(data.loc[latest_date, 'tp2SellPct']):.0f}% | "
        f"NewAlerts={sent}"
    )


if __name__ == "__main__":
    main()
