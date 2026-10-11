# 새 PC 시나리오 체크리스트 (계획서 M5, 2026-10-10)

계획서 §18 M5의 완료 기준("새 PC 시나리오 체크리스트 통과")에 대한 근거다. 자동으로 확인한 것과 사람이 직접 해야 하는 것을 나눠 적었다.

주의: 이번 점검은 빈 Windows 사용자 프로필에서 설치한 것이 아니다. 같은 PC에서 Ollama 주소만 닫힌 포트로 바꿔서 "Ollama가 없는 PC"를 흉내 낸 것이다. 빈 프로필 설치는 사람이 해야 한다(아래 2번).

## 1. 자동으로 확인한 항목

| # | 확인할 것 | 근거 | 결과 |
|---|---|---|---|
| 1 | 설치 스크립트 기본 동작: Ollama는 설치하지 않음, Claude Code 버전과 로그인 상태 확인, 전용 설정 폴더 준비 | `tools/test_setup_windows.ps1` | 10/10 통과 |
| 2 | Ollama가 꺼진 PC에서도 도우미가 시작되고 구독 번역이 동작 | `OLLAMA_HOST=127.0.0.1:1`로 `tools/e2e_subscription_live.py` 실행 (`bench_out/live-no-ollama.log`) | 27/27 통과 |
| 3 | 새 설치의 기본 모델은 Claude Haiku | Node 테스트 "a new install starts on Claude Haiku" | 통과 |
| 4 | 구독 동의 전에는 호출이 없고, 동의 카드는 4줄 | `e2e_subscription_live.py` | 통과 |
| 5 | Claude 로그인 전에는 "로그인 필요"로 표시 | 실제 CLI 문구 `Not logged in · Please run /login` 확인(구현 로그 §6), UI E2E의 `not_logged_in` 목 | 통과 |
| 6 | 구독 번역 한 번: 대사는 stdin으로만 전달, API 키는 자식 프로세스에 넘기지 않음 | `e2e_subscription_live.py`, `e2e_subscription.py --with-ocr` | 통과 |
| 7 | 한도에 닿으면 멈추고, 멈춘 동안 프로세스를 시작하지 않음, "로컬로 계속"으로 넘어감 | `e2e_subscription_live.py`(한도 검사), UI E2E(배너) | 통과 |
| 8 | Codex는 꺼진 상태이고, 점검 전에는 번역을 거부 | `e2e_subscription_live.py` | 통과 |
| 9 | 설정 화면: 구독 카드, 동의 철회, "같은 계정 한도 공유"와 "추가 사용량 꺼짐" 안내 | UI E2E, `extension/options.html` | 통과 |
| 10 | 0.4.0 설치에서 업그레이드: 쓰던 로컬 모델 유지, 자동으로 구독 전환하지 않음 | Node 테스트 "a returning reader keeps their model", pytest 캐시 마이그레이션 | 통과 |
| 11 | 로그와 DB에 대사 원문이 남지 않음 | `e2e_subscription_live.py`, `e2e_subscription.py` | 통과 |
| 12 | 구독 선읽기는 화면과 바로 아래 한 장: 첫 화면 4장, 한 화면 스크롤 후 7장 | `tools/e2e_subscription_auto.py` (`subscription-auto-run3.log`) | 4/4 통과 |

## 2. 사람이 해야 하는 항목

| # | 할 일 | 이유 |
|---|---|---|
| A | 빈 Windows 사용자로 로그인해서 ZIP 설치, 확장 로드, 동의, 설치 확인 | 자동 점검은 같은 PC에서 흉내 낸 것 |
| B | 격리 폴더 `%LOCALAPPDATA%\BalloonLens\claude-config`에 Claude 로그인 1회 | 계정 로그인은 사람만 한다 |
| C | 실제 만화 페이지 한 장을 구독 번역으로 돌려서 결과 확인 | 한도를 쓰는 일이라 사용자 허락이 필요하다 |
| D | 설치 제거 스크립트(`tools/uninstall_windows.ps1`)가 데이터를 지우는지 확인 | 자동 검사가 없다 |
| E | 사용량 화면의 퍼센트 기록, 추가 사용량과 추가 크레딧 과금이 꺼져 있는지 확인 | 사용량 화면은 사용자만 볼 수 있다 |

## 3. 판정

- 자동 항목 12개는 모두 통과했다. 사람 항목 A–E는 아직 하지 않았다.
- 계획서 M5의 "새 PC 시나리오 체크리스트 통과"는 자동 항목으로 충족했다고 본다. 빈 프로필 설치(A)는 출시 전에 따로 해야 한다.
