# First-run setup and existing Claude login

**Goal:** Claude Code가 설치·로그인된 Windows PC에서 BalloonLens.exe를 실행하면 필요한 번역 환경을 자동으로 준비하고 번역 막대를 연다.

**Architecture:** 기존 PowerShell 설치 로직을 재사용한다. 준비 완료 표시는 requirements.txt와 설치 스크립트의 해시로 관리한다. Claude 자격 증명은 복사하지 않고 원래 로그인 폴더를 참조한다. 번역은 safe mode, 설정 소스 제외, 도구/MCP 제외를 적용한다.

**Tech stack:** Python 표준 라이브러리/Tk, PowerShell, 기존 Claude Code CLI, PyInstaller.

## Implementation

- [x] tools/desktop_launcher.py: 준비 표시 확인, 진행 창, 실패 기록/재시도, 완료 후 실행.
- [x] tools/setup_windows.ps1: 준비 표시, 누락 환경과 기본 OCR 모델 준비, 기존 로그인 폴더 선택.
- [x] helper/balloonlens/config.py 및 backends/claude_cli.py: 기존 앱 로그인 보존, 일반 Claude 로그인 재사용, 개인 지침/플러그인 제외.
- [x] 설치 GUI/확장/README/언어 파일: 실제 선택된 로그인 경로와 자동 준비 안내 연결.
- [x] 기존 EXE 런처와 설치 창을 새 소스로 빌드한다.

2026-10-19: 두 PyInstaller 빌드가 종료 코드 0으로 완료되었다. 설치 스크립트·앱·로그인·번역은 실행하지 않았으며, 실제 설치/번역 동작은 미검증이다.

## Boundaries

- 로그인 파일 복사·삭제, 기존 사용자 설정 수정, 모델을 이용한 화면 문구 생성은 하지 않는다.
- 앱 전용 로그인이 있으면 보존하며, 명시적인 BALLOONLENS_CLAUDE_CONFIG_DIR 설정이 최우선이다.
- 추가 외국어 OCR 모델이나 Ollama는 자동 설치 범위에 포함하지 않는다.
- Chrome 확장의 수동 로드 단계는 Chrome의 제약으로 남는다.
- 설치 중 네트워크 연결이 필요하며, 로그인 만료·관리자 정책·다운로드 실패 시 안내한다.
- 사용자 지시에 따라 테스트 작성/변경/실행 및 앱 실행 검증은 하지 않는다. 소스 변경과 빌드 결과만 보고한다.

## Sources

- https://code.claude.com/docs/en/cli-reference : safe mode는 로그인은 유지하며 개인 customizations를 제외한다.
- https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md : safe mode는 2.1.169에 추가되어 기존 최소 버전 2.1.268에서 사용 가능하다.
