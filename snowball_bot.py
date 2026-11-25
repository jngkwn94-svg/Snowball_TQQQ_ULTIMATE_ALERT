```python
#!/usr/bin/env python3
"""
눈덩이티큐 ULTIMATE (DC 이후 DIP 재확인 + RSI 보너스) - 종가 직후 텔레그램 알림 봇 [개선판]

개선 사항
1. fetch_final()에서 TQQQ뿐만 아니라 QQQ, ^VIX, ^VIX3M 4개 종목 모두 당일 봉이 올라올 때까지 대기
2. 야후 1d 일봉 반영이 지연될 경우, 당일 정규장(09:30~마감시각 ET) 5분봉으로 종가/고가를 보완
3. 수동 매매 상태(MANUAL_STAGE / MANUAL_SHARES) 반영 가능
4. 가독성 개선형 텔레그램 요약(다음 신호까지 남은 거리 및 RSI_MODE 목표 비중 명시)

필요한 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
선택 환경변수:
  NOTIFY_MODE = always(기본) | signals
  RSI_MODE    = 끄기 | 축소형 | 표대로(기본)
  FORCE_SEND  = 1 (테스트용, 기록 저장 안 함)
  MANUAL_STAGE = 1 / MANUAL_SHARES = 0 (실제 계좌 상태 덮어쓰기)
"""
import html
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
KST = ZoneInfo("Asia/Seoul")
STATE_PATH = os.path.join("state", "last_notified.json")
HISTORY_START = "2009-01-01"

# 타이밍 설정 (외부 정각 크론 연동에 맞춰 빠른 재시도)
BUFFER_MIN = 8
LATE_MIN = 45
MAX_TRIES = 10
RETRY_WAIT = 30
STABLE_WAIT = 20
CATCHUP_MAX_BARS = 40

# RSI 보너스
RSI_MODE = os.getenv("RSI_MODE", "축소형")
RSI_LEN = 14
RSI_MID = 35.0
RSI_EXTREME = 30.0
DIP1_BASE_W = 30.0
DIP2_BASE_W = 70.0
BONUS_TABLE = {"끄기": (0.0, 0.0), "축소형": (5.0, 7.5), "표대로": (10.0, 15.0)}
if RSI_MODE not in BONUS_TABLE:
    print(f"[WARN] RSI_MODE='{RSI_MODE}' 잘못된 값 -> '축소형'으로 대체")
    RSI_MODE = "축소형"

_ctx = {"late": False, "expected": None}


class BotError(Exception):
    pass


# ---------------- 거래소 달력 / 마감 판단 ----------------
def get_calendar():
    try:
        import exchange_calendars as xcals
        return xcals.get_calendar("XNYS")
    except Exception as e:
        print(f"[WARN] 거래소 달력 로드 실패, 평일 16:00 ET 대체: {e}")
        return None


def _is_session(cal, d: date) -> bool:
    if cal is not None:
        try:
            return bool(cal.is_session(pd.Timestamp(d)))
        except Exception:
            pass
    return d.weekday() < 5


def session_close_utc(cal, d: date) -> datetime:
    if cal is not None:
        try:
            if cal.is_session(pd.Timestamp(d)):
                return cal.session_close(pd.Timestamp(d)).to_pydatetime().astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.combine(d, time(16, 0), tzinfo=ET).astimezone(timezone.utc)


def latest_completed_session(now_utc: datetime, cal):
    today = now_utc.astimezone(ET).date()
    for back in range(0, 14):
        d = today - timedelta(days=back)
        if not _is_session(cal, d):
            continue
        if now_utc >= session_close_utc(cal, d) + timedelta(minutes=BUFFER_MIN):
            return d
    return None


# ---------------- 데이터 (1d 일봉 + 지연 시 5m 정규장 폴백) ----------------
def _hist(ticker: str, tries: int = 3) -> pd.DataFrame:
    import yfinance as yf
    err = None
    for k in range(tries):
        try:
            h = yf.Ticker(ticker).history(start=HISTORY_START, auto_adjust=False)
            if h is not None and not h.empty:
                h.index = pd.to_datetime(h.index.date)
                return h[~h.index.duplicated(keep="last")]
            err = RuntimeError(f"{ticker} 데이터가 비어 있습니다")
        except Exception as e:
            err = e
        _time.sleep(3 * (k + 1))
    raise BotError(f"{ticker} 다운로드 실패: {err}")


def _intraday_regular_bar(ticker: str, target_date: date, close_et: time):
    """1d 일봉이 아직 안 올라왔을 때 당일 정규장(09:30~마감) 5분봉으로 종가/고가 보완."""
    import yfinance as yf
    try:
        m = yf.Ticker(ticker).history(period="5d", interval="5m", prepost=False, auto_adjust=False)
        if m is None or m.empty:
            return None
        if m.index.tz is None:
            m.index = m.index.tz_localize(ET)
        else:
            m.index = m.index.tz_convert(ET)
        day_df = m[m.index.date == target_date]
        day_df = day_df[(day_df.index.time >= time(9, 30)) & (day_df.index.time <= close_et)]
        if day_df.empty:
            return None
        last_t = day_df.index[-1].time()
        cutoff = (datetime.combine(target_date, close_et) - timedelta(minutes=10)).time()
        if last_t < cutoff:
            return None
        return {
            "Open": float(day_df["Open"].iloc[0]),
            "High": float(day_df["High"].max()),
            "Low": float(day_df["Low"].min()),
            "Close": float(day_df["Close"].iloc[-1]),
        }
    except Exception:
        return None


def fetch_data(expected: date = None, cal=None, allow_5m_fallback: bool = True):
    """(df, last, fallback_used) 반환."""
    raw = {t: _hist(t) for t in ("TQQQ", "QQQ", "^VIX", "^VIX3M")}
    fallback_used = []

    if expected is not None and allow_5m_fallback:
        close_et = session_close_utc(cal, expected).astimezone(ET).time()
        ts = pd.Timestamp(expected)
        for t in ("TQQQ", "QQQ", "^VIX", "^VIX3M"):
            if raw[t].index.max().date() < expected:
                bar = _intraday_regular_bar(t, expected, close_et)
                if bar is not None:
                    row = pd.DataFrame([bar], index=[ts])
                    raw[t] = pd.concat([raw[t], row])
                    raw[t] = raw[t][~raw[t].index.duplicated(keep="last")].sort_index()
                    fallback_used.append(t)

    tq, qq, vx, v3 = raw["TQQQ"], raw["QQQ"].copy(), raw["^VIX"], raw["^VIX3M"]
    qq["hh"] = qq["High"].rolling(P["dip_lookback"]).max()
    df = pd.DataFrame({"c": tq["Close"]})
    df = df[df.index >= "2010-02-11"]
    df["qc"] = qq["Close"].reindex(df.index).ffill()
    df["qh"] = qq["hh"].reindex(df.index).ffill()
    df["vix"] = vx["Close"].reindex(df.index).ffill()
    df["vix3"] = v3["Close"].reindex(df.index).ffill()
    last = {"TQQQ": tq.index.max(), "QQQ": qq.index.max(), "VIX": vx.index.max(), "VIX3M": v3.index.max()}
    return df.dropna(subset=["c"]), last, fallback_used


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


# ---------------- 신호/상태 계산 ----------------
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
    wcur = 0.0
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
    "TP3": ("🔴", "TP3", "전량 매도 후 LOCK"),
    "TP1": ("🟡", "TP1", "보유 수량 50% 매도"),
    "TP2": ("🟡", "TP2", "보유 수량 35% 매도"),
    "GC": ("🟢", "GC", "전체 자산 100%까지 매수"),
    "DIP1": ("🟢", "DIP1", ""),
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
    prev_tqqq = d.c.iloc[-2] if len(d) > 1 else np.nan
    prev_qqq = d.qc.iloc[-2] if len(d) > 1 else np.nan
    chg_t = (r.c / prev_tqqq - 1) * 100 if not np.isnan(prev_tqqq) else np.nan
    chg_q = (r.qc / prev_qqq - 1) * 100 if not np.isnan(prev_qqq) else np.nan
    dt_str = d.index[-1].strftime("%m-%d")

    manual_stage = os.getenv("MANUAL_STAGE")
    manual_shares = os.getenv("MANUAL_SHARES")
    manual_avg = os.getenv("MANUAL_AVG")
    eff_stage = int(manual_stage) if manual_stage is not None and manual_stage.isdigit() else int(r.stage)
    has_manual_avg = False
    try:
        if manual_avg is not None and float(manual_avg) > 0:
            eff_avg = float(manual_avg)
            has_manual_avg = True
        else:
            eff_avg = float(r.avg)
    except ValueError:
        eff_avg = float(r.avg)
    eff_t1 = False if has_manual_avg else bool(r.t1)
    eff_act = r.act
    if has_manual_avg and not eff_t1 and not eff_act and eff_stage >= 1 and r.c >= eff_avg * (1.0 + P["tp1"] / 100.0):
        eff_act = "TP1"

    # 1. 잠결에도 1초 만에 보이는 최상단 배너
    if missed:
        banner = f"⚠️ <b>[놓친 신호 · {dt_str}] {r.act}</b>"
    elif eff_act in ("DIP1", "DIP2"):
        tgt = r.dip1_t if eff_act == "DIP1" else r.dip2_t
        banner = f"🟢🟢 <b>[매수] {eff_act} — 자산 {wtxt(tgt)}까지 매수!</b>"
    elif eff_act == "GC":
        banner = "🟢🟢 <b>[매수] GC 돌파 — 자산 100%까지 매수!</b>"
    elif eff_act in ("TP1", "TP2"):
        pct_sell = P["tp1_sell"] if eff_act == "TP1" else P["tp2_sell"]
        banner = f"🟡🟡 <b>[익절] {eff_act} — 보유 {pct_sell}% 매도!</b>"
    elif eff_act in ("DC", "TP3"):
        banner = f"🔴🔴 <b>[전량 매도] {eff_act} 발동!</b>"
    else:
        banner = "⏸️ <b>오늘은 매매 없음 (대기)</b>"

    lines = [
        banner,
        f"🗓️ {dt_str} 미국장 마감 · Stage {eff_stage} ({STAGE_TXT[eff_stage]}) · {RSI_MODE}",
        "",
    ]

    # 2. 다음 단계 기준가 / 임박 알림 (가장 중요한 행동 기준)
    next_guides = []
    if eff_stage == 0 and not np.isnan(r.qh):
        d1_px = r.qh * (1.0 + r.trig / 100.0)
        d1_pct = (d1_px / r.qc - 1.0) * 100.0
        tag = "🚨 <b>임박!</b> " if (r.dd - r.trig) <= 3.0 else ""
        next_guides.append(f"• {tag}QQQ <b>${d1_px:.2f}</b> 이하 ({d1_pct:+.2f}%) → DIP1 <b>{wtxt(r.dip1_t)}</b>")
    elif eff_stage == 1 and not np.isnan(r.qh):
        d2_px = r.qh * (1.0 + P["dip2"] / 100.0)
        d2_pct = (d2_px / r.qc - 1.0) * 100.0
        tag = "🚨 <b>임박!</b> " if (r.dd - P["dip2"]) <= 3.0 else ""
        next_guides.append(f"• {tag}QQQ <b>${d2_px:.2f}</b> 이하 ({d2_pct:+.2f}%) → DIP2 <b>{wtxt(r.dip2_t)}</b>")
        if r.z200 > P["dip2_z200_max"] and not np.isnan(r.ma200):
            next_guides.append(f"  (TQQQ ${r.ma200 * 0.93:.2f} 이하 동시 필요)")
    if eff_stage >= 1 and not np.isnan(eff_avg) and manual_shares != "0":
        if not eff_t1:
            next_guides.append(f"• TQQQ <b>${eff_avg * (1.0 + P['tp1'] / 100.0):.2f}</b> 이상 → TP1 (50% 매도)")
        elif not r.t2:
            next_guides.append(f"• TQQQ <b>${eff_avg * (1.0 + P['tp2'] / 100.0):.2f}</b> 이상 → TP2 (35% 매도)")
    if eff_stage >= 1 and r.gap * 100.0 < 1.5:
        next_guides.append(f"• ⚠️ 5/220MA 갭 {fmt(r.gap * 100, 2, True)} (-0.10% 시 DC)")

    if next_guides:
        lines += ["🔔 <b>다음 기준가</b>", *next_guides, ""]

    # 3. 한눈에 들어오는 짧은 수치 (모바일 줄바꿈 방지)
    lines += [
        "──────────",
        f"• <b>TQQQ</b> ${fmt(r.c)} ({fmt(chg_t, 2, True)})",
        f"• <b>QQQ</b> ${fmt(r.qc)} ({fmt(chg_q, 2, True)}) | DD <b>{fmt(r.dd, 2, True)}</b>",
        f"• <b>RSI</b> {fmt(r.rsi, 1)} | <b>VIX비</b> {fmt(r.vr)} ({fmt(r.trig, 0, True)})",
        f"• <b>Z200</b> {fmt(r.z200, 2, True)} | <b>GC</b> {fmt(r.gap * 100, 2, True)}",
    ]
    if eff_stage >= 1 and not np.isnan(eff_avg) and manual_shares != "0":
        lines.append(f"• <b>{'실제평단' if has_manual_avg else '전략평단'}</b> ${fmt(eff_avg)} ({fmt((r.c / eff_avg - 1) * 100, 2, True)})")

    for w in (warnings or []):
        lines.append(f"⚠️ {html.escape(str(w))}")
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    import requests
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    err = None
    for k in range(4):
        try:
            resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=20)
            if resp.status_code == 200:
                return
            err = f"{resp.status_code} {resp.text[:200]}"
            if resp.status_code == 429:
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


# ---------------- 야후 4종목 동시 확정 대기 ----------------
def fetch_final(expected: date, cal, sleep=_time.sleep):
    """TQQQ/QQQ/^VIX/^VIX3M 4개 모두 expected 봉이 올라오고 안정될 때까지 대기."""
    prev, df, last, fb = None, None, None, []
    for _ in range(MAX_TRIES):
        df, last, fb = fetch_data(expected=expected, cal=cal, allow_5m_fallback=True)
        all_ready = all(last[k].date() >= expected for k in ("TQQQ", "QQQ", "VIX", "VIX3M"))
        if all_ready:
            sig = (round(float(df.c.iloc[-1]), 2), round(float(df.qc.iloc[-1]), 2), round(float(df.vix.iloc[-1]), 2))
            if prev == sig:
                return df, last, fb, True
            prev = sig
            sleep(STABLE_WAIT)
        else:
            prev = None
            missing = [k for k in ("TQQQ", "QQQ", "VIX", "VIX3M") if last[k].date() < expected]
            print(f"[WAIT] {expected} 미반영: {', '.join(missing)}. {RETRY_WAIT}초 후 재시도")
            sleep(RETRY_WAIT)
    return df, last, fb, False


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

    df, last, fb, final = fetch_final(expected, cal, sleep=sleep)
    have_bar = df.index[-1].date() >= expected
    warnings = []
    if fb:
        warnings.append(f"일봉 지연으로 정규장 5분봉 마감값 사용: {', '.join(fb)}")
    if not have_bar:
        msg = f"야후에 {expected} 봉이 올라오지 않았습니다(마지막 {df.index[-1].date()})."
        if late and not force:
            raise BotError(msg)
        print(f"[SKIP] {msg}")
        return 0
    if not final:
        if not late and not force:
            print("[SKIP] 4종목 종가 확정 대기 중. 다음 실행에서 재시도.")
            return 0
        warnings.append("일부 종목 종가가 아직 확정 전일 수 있습니다.")
    stale = [k for k in ("QQQ", "VIX", "VIX3M") if last[k].date() < expected]
    if stale:
        if not late and not force:
            print(f"[SKIP] {', '.join(stale)} 미반영.")
            return 0
        warnings.append(f"{', '.join(stale)} 미반영으로 직전값 사용")

    df = df[df.index <= pd.Timestamp(expected)]
    d = compute(df)
    latest = d.index[-1]

    missed = []
    if not force and state.get("last_notified"):
        start = pd.Timestamp(state["last_notified"])
        bars = [t for t in d.index if t > start][-CATCHUP_MAX_BARS:]
        missed = [t for t in bars if t < latest and d.loc[t, "act"]]
    for t in missed:
        send_telegram(build_message(d.loc[:t], missed=True))
        save_state(last_notified=str(t.date()))

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
    if not _ctx["late"]:
        return
    st = load_state()
    if st.get("last_error_date") == _ctx["expected"]:
        return
    send_telegram(f"⚠️ 눈덩이티큐 봇 오류 ({_ctx['expected']} 마감 기준)\n{e}")
    save_state(last_error_date=_ctx["expected"])


if __name__ == "__main__":
    try:
        sys.exit(run(force=os.getenv("FORCE_SEND") == "1"))
    except Exception as e:
        print(f"[ERROR] {e!r}")
        try:
            notify_error(e)
        except Exception as e2:
            print(f"[ERROR] 오류 알림 발송 실패: {e2!r}")
        sys.exit(1)
```
