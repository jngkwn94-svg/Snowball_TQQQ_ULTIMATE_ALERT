# Snowball TQQQ ULTIMATE v2.0 — Telegram Alert

`규칙.txt`의 **눈덩이 TQQQ ULTIMATE v2.0** 규칙을 기준으로 일일 신호를 계산하고 Telegram으로 알리는 프로젝트입니다.

## 구성

- `Snowball_TQQQ_ULTIMATE_ALERT_v2.0.py` — 신호 계산 + Telegram 전송
- `requirements.txt` — Python 패키지
- `.env.example` — 필요한 환경변수 예시
- `.gitignore` — 토큰/상태파일 등의 Git 제외
- `.github/workflows/snowball_ultimate_alert.yml` — GitHub Actions 자동 실행
- `data/README.md` — 로컬 데이터 폴더 안내

## 중요

이 프로젝트는 **자동매매 주문기가 아니라 알림 엔진**입니다.

신호는 원칙적으로:
**신호일 일봉 종가 확정 → 다음 정규장 시가 기준 실행**

으로 안내합니다.

Python/yfinance와 TradingView 사이에는 데이터 제공원, 세션, 수정주가 처리 등의 차이가 있을 수 있으므로 운용 전에 TradingView의 확인된 신호와 대조하십시오.

## 1. Telegram Bot 만들기

Telegram에서 `@BotFather`로 봇을 만든 후 Bot Token을 확보합니다.

그 다음 봇에게 메시지를 보내고 Chat ID를 확인합니다.

## 2. GitHub Secrets 등록

GitHub 저장소:
**Settings → Secrets and variables → Actions**

다음 두 개를 추가합니다.

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

값은 절대로 소스코드에 직접 입력하지 마십시오.

## 3. GitHub Actions

`Actions` 탭에서 `Snowball TQQQ ULTIMATE Alert` workflow를 선택합니다.

`Run workflow`로 수동 테스트할 수 있습니다.

자동 실행 스케줄은 기존 김째매매법에서 사용하던 `15 20 * * 1-5`를 그대로 유지합니다. GitHub Actions는 UTC 기준으로 실행하며, Python이 당일 미국 정규장 TQQQ 신규 일봉이 존재하는지 확인합니다. 따라서 미국장 휴장일에는 알림을 보내지 않습니다.

## 4. 알림 종류

- DIP1
- DIP2
- GC
- TP1
- TP2 DOWN / NONE / UP / BOTTOM
- TP3
- DC

## 5. 상태

현재 엔진은 Stage, TP1/TP2 완료, TP3 Lock, cycleBaseQty 등의 상태를 고려하도록 구성되어 있습니다.

`Snowball_TQQQ_ULTIMATE_ALERT_v2.0_state.json`은 로컬 실행 시 상태 저장용입니다.

GitHub Actions는 실행 환경이 일회성이므로, **장기 운용 전에 상태 저장 방식을 GitHub artifact/commit 또는 외부 저장소 방식으로 확정하는 것을 권장합니다.**
현재 패키지는 먼저 신호 검증을 위한 단계입니다.

## 6. 로컬 실행

```bash
pip install -r requirements.txt
```

환경변수 설정 후:

```bash
python Snowball_TQQQ_ULTIMATE_ALERT_v2.0.py
```

## 7. 검증 순서

운용 전에 다음 TradingView 신호를 확인합니다.

- 2026-03-18 DC
- 2026-03-27 DIP1
- 2026-04-08 TP1
- 2026-04-14 GC

이 네 이벤트가 동일하게 나오는지 먼저 확인한 뒤 자동 알림을 활성화하는 것을 권장합니다.
