#!/usr/bin/env python3
"""
눈덩이티큐 ULTIMATE (DC 이후 DIP 재확인 + RSI 보너스) - 종가 직후 텔레그램 알림 봇 [최종판]

동작
- 야후 파이낸스에서 TQQQ / QQQ / ^VIX / ^VIX3M 일봉을 받아, 2010-02부터 상태(stage, 평단, TP/LOCK/재확인 플래그)를
  처음부터 다시 계산합니다. (TradingView 지표가 차트 첫 봉부터 상태를 쌓는 것과 같은 방식)
- NYSE 거래소 달력(exchange_calendars)으로 휴장일, 주말, 조기 마감일(13:00 ET), 서머타임을 자동 처리합니다.
  종가 확정 시각 = 그날의 실제 마감 시각 + 10분.
- 야후 종가가 최종값으로 올라왔는지 확인하고(연속 2회 조회 값이 같을 때) 보냅니다.
- 이전에 보낸 날짜(state/last_notified.json) 이후에 놓친 신호가 있으면 "놓친 신호"로 먼저 보내고, 이어서 오늘 메시지를 보냅니다.
- 마감 후 45분이 지나도 데이터가 안 올라오거나 오류가 나면 텔레그램으로 경고를 보냅니다(하루 1회).

필요한 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택:
  NOTIFY_MODE = always(기본, 매 거래일 요약 발송) | signals(신호 날만 발송)
  RSI_MODE    = 끄기 | 축소형 | 표대로(기본)   (잘못된 값이면 표대로로 대체하고 경고)
  FORCE_SEND  = 1  마감/중복 확인을 무시하고 가장 최근 봉으로 지금 바로 발송(테스트용, 기록은 저장하지 않음)
"""
import json
import os
import sys
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ---------------- 파라미터 (TradingView 지표 입력값과 동일) ----------------
P = dict(
    ma5=5, ma220=220, ma200=200,
    gc_pct=2.90, dc_pct=-0.10,
    dip_lookback=126,
    dip1_base=-10.0, dip1_vix=-12.0, dip1_extreme=-14.0,
    dip2=-22.0, dip2_z200_max=-7.0, max_dip=-40.0,
    use_vix_dip=True, vix_normal=1.03, vix_extreme=1.08,
    tp1=15.0, tp1_sell=50, tp2=100.0, tp2_sell=35, tp3=350.0,
)
ET = ZoneInfo("America/New_York")
STATE_PATH = os.path.join("state", "last_notified.json")
HISTORY_START = "2009-01-01"

# 타이밍 설정
BUFFER_MIN = 10     # 마감 후 이 시간(분)이 지나야 종가 확정으로 봄
LATE_MIN = 45       # 마감 후 이 시간(분)이 지나도 못 보내면 경고/불완전 데이터로 발송
MAX_TRIES = 8       # 한 번 실행 안에서 야후 조회 재시도 횟수
RETRY_WAIT = 60     # 봉이 아직 없을 때 대기(초)
STABLE_WAIT = 45    # 종가 안정 확인 사이 대기(초)
CATCHUP_MAX_BARS = 40  # 놓친 신호를 찾아볼 최대 거래일 수

# RSI 보너스 (TradingView 지표 입력값과 동일하게 맞출 것)
RSI_MODE = os.getenv("RSI_MODE", "표대로")  # 끄기 | 축소형 | 표대로
RSI_LEN = 14
RSI_MID = 35.0       # RSI 이 값 이하: 보너스
RSI_EXTREME = 30.0   # RSI 이 값 이하: 극단 보너스
DIP1_BASE_W = 30.0   # DIP1 기본 목표 비중(%)
DIP2_BASE_W = 70.0   # DIP2 기본 목표 비중(%)
BONUS_TABLE = {"끄기": (0.0, 0.0), "축소형": (5.0, 7.5), "표대로": (10.0, 15.0)}  # (mid, extreme) %p
if RSI_MODE not in BONUS_TABLE:
    print(f"[WARN] RSI_MODE='{RSI_MODE}' 은(는) 올바른 값이 아닙니다(끄기/축소형/표대로). '표대로'로 대체합니다.")
    RSI_MODE = "표대로"

_ctx = {"late": False, "expected": None}  # 오류 알림 판단용


class BotError(Exception):
    pass


# ---------------- 데이터 ----------------
def _hist(ticker: str, tries: int = 3) -> pd.DataFrame:
    import yfinance as yf
    err = None
    for k in range(tries):
        try:
            h = yf.Ticker(ticker).history(start=HISTORY_START, auto_adjust=False)
            if h is not None and not h.empty:
                h.index = pd.to_datetime(h.index.date)  # 거래일 날짜만 사용
                return h[~h.index.duplicated(keep="last")]
            err = RuntimeError(f"{ticker} 데이터가 비어 있습니다")
        except Exception as e:  # 네트워크 일시 오류 등
            err = e
        _time.sleep(5 * (k + 1))
    raise BotError(f"{ticker} 다운로드 실패: {err}")


def fetch_data():
    """(df, last) 반환. last 는 각 시리즈의 마지막 날짜."""
    tq, qq, vx, v3 = _hist("TQQQ"), _hist("QQQ"), _hist("^VIX"), _hist("^VIX3M")
    qq = qq.copy()
    qq["hh"] = qq["High"].rolling(P["dip_lookback"]).max()  # ta.highest(high, 126) (QQQ 일봉)
    df = pd.DataFrame({"c": tq["Close"]})
    df = df[df.index >= "2010-02-11"]
    df["qc"] = qq["Close"].reindex(df.index).ffill()
    df["qh"] = qq["hh"].reindex(df.index).ffill()
    df["vix"] = vx["Close"].reindex(df.index).ffill()
    df["vix3"] = v3["Close"].reindex(df.index).ffill()
    last = {"TQQQ": tq.index.max(), "QQQ": qq.index.max(), "VIX": vx.index.max(), "VIX3M": v3.index.max()}
    return df.dropna(subset=["c"]), last


# ---------------- 거래소 달력 / 마감 판단 ----------------
def get_calendar():
    try:
        import exchange_calendars as xcals
        return xcals.get_calendar("XNYS")
    except Exception as e:
        print(f"[WARN] 거래소 달력을 불러오지 못해 '평일 16:00 ET' 기준으로 대체합니다: {e}")
        return None


def _is_session(cal, d: date) -> bool:
    if cal is not None:
        try:
            return bool(cal.is_session(pd.Timestamp(d)))
        except Exception:
            pass
    return d.weekday() < 5


def session_close_utc(cal, d: date) -> datetime:
    """그날 정규장 실제 마감 시각(UTC). 조기 마감일은 13:00 ET."""
    if cal is not None:
        try:
            if cal.is_session(pd.Timestamp(d)):
                return cal.session_close(pd.Timestamp(d)).to_pydatetime().astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.combine(d, time(16, 0), tzinfo=ET).astimezone(timezone.utc)


def latest_completed_session(now_utc: datetime, cal):
    """now 시점에 '마감 + BUFFER_MIN' 이 지난 가장 최근 거래일(ET 날짜)."""
    today = now_utc.astimezone(ET).date()
    for back in range(0, 14):
        d = today - timedelta(days=back)
        if not _is_session(cal, d):
            continue
        if now_utc >= session_close_utc(cal, d) + timedelta(minutes=BUFFER_MIN):
            return d
    return None


# ---------------- RSI (TradingView ta.rsi 와 동일한 RMA 방식) ----------------
def rsi_tv(close, n: int = 14) -> np.ndarray:
    c = pd.Series(close, dtype=float)
    ch = c.diff()
    up, dn = ch.clip(lower=0), (-ch).clip(lower=0)

    def rma(x):
        out = np.full(len(x), np.nan)
        v = x.values
        if len(v) > n:
            out[n] = np.mean(v[1:n + 1])
            for i in range(n + 1, len(v)):
                out[i] = (out[i - 1] * (n - 1) + v[i]) / n
        return out

    ru, rd = rma(up), rma(dn)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = 100 - 100 / (1 + ru / rd)
    r = np.where(rd == 0, 100.0, r)
    r[:n] = np.nan
    return r


# ---------------- 신호/상태 계산 (Pine 로직 그대로 + RSI 보너스) ----------------
def compute(df: pd.DataFrame, p: dict = P, mode: str = None) -> pd.DataFrame:
    mode = mode or RSI_MODE
    b_mid, b_ext = BONUS_TABLE[mode]
    d = df.copy()
    d["rsi"] = rsi_tv(d.c.values, RSI_LEN)
    rs = d["rsi"].values
    bonus = np.where(np.isnan(rs), 0.0, np.where(rs <= RSI_EXTREME, b_ext, np.where(rs <= RSI_MID, b_mid, 0.0)))
    d["bonus"] = bonus
    d["dip1_t"] = DIP1_BASE_W + bonus
    d["dip2_t"] = DIP2_BASE_W + bonus
    d["ma5"] = d.c.rolling(p["ma5"]).mean()
    d["ma200"] = d.c.rolling(p["ma200"]).mean()
    d["ma220"] = d.c.rolling(p["ma220"]).mean()
    d["gap"] = (d.ma5 - d.ma220) / d.ma220
    gct, dct = p["gc_pct"] / 100.0, abs(p["dc_pct"]) / 100.0
    gcx = ((d.gap.shift(1) < gct) & (d.gap >= gct)).values
    dcx = ((d.gap.shift(1) > -dct) & (d.gap <= -dct)).values
    d["dd"] = (d.qc / d.qh - 1.0) * 100.0
    d["z200"] = (d.c / d.ma200 - 1.0) * 100.0
    d["vr"] = d.vix / d.vix3
    vr = d["vr"].values
    if p["use_vix_dip"]:
        trig = np.where(np.isnan(vr), p["dip1_base"],
                        np.where(vr > p["vix_extreme"], p["dip1_extreme"],
                                 np.where(vr > p["vix_normal"], p["dip1_vix"], p["dip1_base"])))
    else:
        trig = np.full(len(d), p["dip1_base"])
    d["trig"] = trig
    dd, z = d["dd"].values, d["z200"].values
    d1c = np.nan_to_num((dd <= trig) & (dd > p["max_dip"])).astype(bool)
    d2c = np.nan_to_num((dd <= p["dip2"]) & (dd > p["max_dip"]) & (z <= p["dip2_z200_max"])).astype(bool)
    d1p = np.r_[False, d1c[:-1]]
    d2p = np.r_[False, d2c[:-1]]
    c = d.c.values

    stage = 0
    avg = np.nan
    t1 = t2 = t3 = lock = False
    reent = rech = False
    wcur = 0.0  # 현재 단계의 TQQQ 목표 비중(0~1), 평단 가중 계산용
    out = {k: [] for k in ("act", "stage_prev", "avg_prev", "stage", "avg", "t1", "t2", "t3", "lock", "reent", "rech", "w_prev", "w")}

    for i in range(len(d)):
        stage_prev, avg_prev, w_prev = stage, avg, wcur
        hasavg = not np.isnan(avg)
        hit1 = stage >= 1 and not t1 and hasavg and c[i] >= avg * (1 + p["tp1"] / 100)
        hit2 = stage >= 1 and t1 and not t2 and hasavg and c[i] >= avg * (1 + p["tp2"] / 100)
        hit3 = stage >= 1 and not t3 and not lock and hasavg and c[i] >= avg * (1 + p["tp3"] / 100)
        gok = (not np.isnan(dd[i])) and dd[i] > p["max_dip"]
        a = ""
        if dcx[i] and (stage > 0 or lock):
            a = "DC"
        elif hit3:
            a = "TP3"
        elif hit1:
            a = "TP1"
        elif hit2:
            a = "TP2"
        elif gcx[i] and stage < 3 and not lock and gok:
            a = "GC"
        elif stage == 0 and d1c[i] and ((not d1p[i]) or reent):
            a = "DIP1"
        elif stage == 1 and d2c[i] and ((not d2p[i]) or rech):
            a = "DIP2"

        if a == "DC":
            stage = 0; avg = np.nan; t1 = t2 = t3 = lock = False; reent = True; rech = False; wcur = 0.0
        elif a == "TP3":
            stage = 0; avg = np.nan; t1 = t2 = False; t3 = True; lock = True; reent = True; rech = False; wcur = 0.0
        elif a == "TP1":
            t1 = True
        elif a == "TP2":
            t2 = True
        elif a == "GC":
            reent = False; rech = False
            if stage == 0:
                avg = c[i]; stage = 3; t1 = t2 = t3 = lock = False
            elif stage in (1, 2):
                avg = (avg * wcur + c[i] * (1 - wcur)) if hasavg else c[i]; stage = 3
            wcur = 1.0
        elif a == "DIP1":
            lock = False; reent = False; rech = True
            if stage == 0:
                avg = c[i]; stage = 1; t1 = t2 = t3 = False; wcur = d["dip1_t"].values[i] / 100.0
        elif a == "DIP2":
            rech = False
            if stage == 1:
                w2 = max(d["dip2_t"].values[i] / 100.0, wcur + 0.01)
                avg = (avg * wcur + c[i] * (w2 - wcur)) / w2 if hasavg else c[i]; wcur = w2; stage = 2

        for k, v in (("act", a), ("stage_prev", stage_prev), ("avg_prev", avg_prev), ("stage", stage), ("avg", avg),
                     ("t1", t1), ("t2", t2), ("t3", t3), ("lock", lock), ("reent", reent), ("rech", rech),
                     ("w_prev", w_prev), ("w", wcur)):
            out[k].append(v)
    for k, v in out.items():
        d[k] = v
    return d


# ---------------- 메시지 ----------------
STAGE_TXT = {0: "현금 대기", 1: "DIP1", 2: "DIP2", 3: "GC 보유"}
ACTION_TXT = {
    "DC": ("🔴", "DC", "전량 매도"),
    "TP3": ("🔴", "TP3", "전량 매도 후 LOCK (다음 DIP1에서 해제)"),
    "TP1": ("🟡", "TP1", "보유 수량의 50% 매도"),
    "TP2": ("🟡", "TP2", "보유 수량의 35% 매도"),
    "GC": ("🟢", "GC", "전체 자산의 100%까지 매수"),
    "DIP1": ("🟢", "DIP1", ""),   # 비중은 RSI에 따라 달라서 build_message 에서 채움
    "DIP2": ("🟢", "DIP2", ""),
}


def wtxt(x) -> str:
    return f"{x:g}%"


def fmt(x, nd=2, pct=False):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    return f"{x:.{nd}f}" + ("%" if pct else "")


def build_message(d: pd.DataFrame, missed: bool = False, warnings=None) -> str:
    r = d.iloc[-1]
    prev_close = d.c.iloc[-2] if len(d) > 1 else np.nan
    chg = (r.c / prev_close - 1) * 100 if not np.isnan(prev_close) else np.nan
    date = d.index[-1].strftime("%Y-%m-%d")
    head = f"⚠️ 놓친 신호 · {date} 미국장 마감" if missed else f"❄️ 눈덩이티큐 ULTIMATE · {date} 미국장 마감"
    lines = [head, ""]
    if r.act:
        emo, name, what = ACTION_TXT[r.act]
        if r.act in ("DIP1", "DIP2"):
            tgt = r.dip1_t if r.act == "DIP1" else r.dip2_t
            what = f"전체 자산의 {wtxt(tgt)}까지 매수"
            lines += [f"{emo} 신호: {name} → {what}"]
            lines += [f"   RSI {fmt(r.rsi, 1)} · 보너스 +{r.bonus:g}%p ({RSI_MODE})"]
        else:
            lines += [f"{emo} 신호: {name} → {what}"]
        if r.act in ("DC", "TP1", "TP2", "TP3") and not np.isnan(r.avg_prev):
            lines += [f"   (전략 평단 {fmt(r.avg_prev)} 대비 {fmt((r.c / r.avg_prev - 1) * 100, 2, True)})"]
        lines += [f"   상태: {STAGE_TXT[r.stage_prev]} → {STAGE_TXT[r.stage]}", ""]
    else:
        lines += ["✅ 오늘 신호 없음", f"   상태: {STAGE_TXT[r.stage]}", ""]
    lines += [
        f"TQQQ 종가 {fmt(r.c)} ({fmt(chg, 2, True)})",
        f"RSI {fmt(r.rsi, 1)} · 지금 DIP가 나면 DIP1 {wtxt(r.dip1_t)} / DIP2 {wtxt(r.dip2_t)} ({RSI_MODE})",
        f"QQQ 고점 대비 {fmt(r.dd, 1, True)} · DIP1 기준 {fmt(r.trig, 1, True)} · VIX비율 {fmt(r.vr)}",
        f"GC Gap {fmt(r.gap * 100, 2, True)} · Z200 {fmt(r.z200, 2, True)} · 5MA {fmt(r.ma5)} / 220MA {fmt(r.ma220)}",
    ]
    if r.stage >= 1 and not np.isnan(r.avg):
        tp = []
        if not r.t1:
            tp.append(f"TP1 {fmt(r.avg * (1 + P['tp1'] / 100))}")
        if r.t1 and not r.t2:
            tp.append(f"TP2 {fmt(r.avg * (1 + P['tp2'] / 100))}")
        if not r.t3 and not r.lock:
            tp.append(f"TP3 {fmt(r.avg * (1 + P['tp3'] / 100))}")
        lines += [f"전략 평단 {fmt(r.avg)} · 다음 목표 " + " / ".join(tp)]
    if r.reent:
        lines += ["⏳ DC/TP3 이후 DIP1 재확인 대기 중 (조건이 이미 참이어도 다음 거래일 신호 가능)"]
    if r.rech and r.stage == 1:
        lines += ["⏳ DIP2 재확인 대기 중"]
    for w in (warnings or []):
        lines += [f"⚠️ {w}"]
    lines += ["", "※ 평단과 수량은 전략 내부 추적값입니다. 실제 계좌와 다를 수 있습니다."]
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    import requests
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    err = None
    for k in range(4):
        try:
            resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat, "text": text}, timeout=20)
            if resp.status_code == 200:
                return
            err = f"{resp.status_code} {resp.text[:200]}"
            if resp.status_code == 429:  # 너무 빠른 전송: 안내된 시간만큼 대기
                try:
                    _time.sleep(float(resp.json().get("parameters", {}).get("retry_after", 3)) + 1)
                    continue
                except Exception:
                    pass
        except Exception as e:
            err = repr(e)
        _time.sleep(3 * (k + 1))
    raise BotError(f"텔레그램 발송 실패: {err}")


# ---------------- 상태 파일 ----------------
def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(**kw) -> None:
    st = load_state()
    st.update(kw)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


# ---------------- 야후 종가 확정 대기 ----------------
def fetch_final(expected: date, sleep=_time.sleep):
    """expected 일봉이 야후에 올라오고 값이 안정될 때까지 기다림. (df, last, final) 반환."""
    prev, df, last = None, None, None
    for _ in range(MAX_TRIES):
        df, last = fetch_data()
        if df.index[-1].date() >= expected:
            sig = (round(float(df.c.iloc[-1]), 2), round(float(df.qc.iloc[-1]), 2))
            if prev == sig:
                return df, last, True
            prev = sig
            sleep(STABLE_WAIT)
        else:
            prev = None
            print(f"[WAIT] 야후에 {expected} 봉이 아직 없습니다(마지막 {df.index[-1].date()}). {RETRY_WAIT}초 후 재시도")
            sleep(RETRY_WAIT)
    return df, last, False


# ---------------- 메인 ----------------
def run(now_utc: datetime = None, force: bool = False, sleep=_time.sleep) -> int:
    now = now_utc or datetime.now(timezone.utc)
    mode = os.getenv("NOTIFY_MODE", "always")
    cal = get_calendar()
    expected = latest_completed_session(now, cal)
    if expected is None:
        print("최근 거래일을 찾지 못했습니다. 종료.")
        return 0
    _ctx["expected"] = str(expected)
    close_utc = session_close_utc(cal, expected)
    late = now >= close_utc + timedelta(minutes=LATE_MIN)
    _ctx["late"] = late

    state = load_state()
    if not force and state.get("last_notified") == str(expected):
        print(f"{expected} 은(는) 이미 처리했습니다. 종료.")
        return 0

    df, last, final = fetch_final(expected, sleep=sleep)
    have_bar = df.index[-1].date() >= expected
    warnings = []
    if not have_bar:
        msg = f"야후에 {expected} 봉이 올라오지 않았습니다(마지막 {df.index[-1].date()})."
        if late and not force:
            raise BotError(msg)
        print(f"[SKIP] {msg} 다음 실행에서 다시 시도합니다.")
        return 0
    if not final:
        if not late and not force:
            print("[SKIP] 종가가 아직 변동 중입니다. 다음 실행에서 다시 시도합니다.")
            return 0
        warnings.append("종가가 아직 확정 전일 수 있습니다(야후 값이 계속 변동). 증권사 종가와 비교해 보세요.")
    stale = [k for k in ("QQQ", "VIX", "VIX3M") if last[k].date() < expected]
    if stale:
        if not late and not force:
            print(f"[SKIP] {', '.join(stale)} 데이터가 아직 {expected} 까지 반영되지 않았습니다. 다음 실행에서 다시 시도합니다.")
            return 0
        warnings.append(f"{', '.join(stale)} 데이터가 아직 {expected}까지 반영되지 않아 이전 값으로 계산했습니다. DIP/GC 신호는 다시 확인하세요.")

    df = df[df.index <= pd.Timestamp(expected)]  # 장중 미완성 봉 등 expected 이후 데이터 제외
    d = compute(df)
    latest = d.index[-1]

    # 놓친 신호(이전 발송일 이후, 오늘 이전)
    missed = []
    if not force and state.get("last_notified"):
        start = pd.Timestamp(state["last_notified"])
        bars = [t for t in d.index if t > start][-CATCHUP_MAX_BARS:]
        missed = [t for t in bars if t < latest and d.loc[t, "act"]]
    for t in missed:
        send_telegram(build_message(d.loc[:t], missed=True))
        save_state(last_notified=str(t.date()))
        print(f"[ALERT] 놓친 신호 보충: {t.date()} {d.loc[t, 'act']}")

    has_signal = bool(d.act.iloc[-1])
    if mode == "signals" and not has_signal and not force and not warnings:
        print("신호 없음 (signals 모드): 발송 안 함.")
    else:
        send_telegram(build_message(d, warnings=warnings))
        print(f"[ALERT] {latest.date()} 발송 완료 (신호: {d.act.iloc[-1] or '없음'})")
    if not force:
        save_state(last_notified=str(latest.date()))
    return 0


def notify_error(e: Exception) -> None:
    """마감 후 LATE_MIN 이 지난 뒤의 오류만, 하루 1회 텔레그램으로 알림."""
    if not _ctx["late"]:
        return
    st = load_state()
    if st.get("last_error_date") == _ctx["expected"]:
        return
    send_telegram(f"⚠️ 눈덩이티큐 봇 오류 ({_ctx['expected']} 마감 기준)\n{e}\n\n"
                  "다음 실행에서 자동으로 다시 시도하고, 놓친 신호가 있으면 보충해서 알려드립니다.")
    save_state(last_error_date=_ctx["expected"])


if __name__ == "__main__":
    try:
        sys.exit(run(force=os.getenv("FORCE_SEND") == "1"))
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {e!r}")
        try:
            notify_error(e)
        except Exception as e2:  # noqa: BLE001
            print(f"[ERROR] 오류 알림 발송 실패: {e2!r}")
        sys.exit(1)
