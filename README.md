# Project Zomboid Mod Conflict Checker FAST

프로젝트 좀보이드 모드 폴더를 빠르게 스캔해서 충돌 가능성이 있는 항목을 보여주는 Python Tkinter GUI 도구입니다.

## 실행

```bash
python pz_mod_conflict_checker_fast.py
```

Windows에서 `python` 명령이 없다면 Python을 설치한 뒤 다시 실행하세요.

## 입력 경로 예시

```text
C:\Users\사용자이름\Zomboid\mods
```

또는 Steam Workshop 경로:

```text
C:\Program Files (x86)\Steam\steamapps\workshop\content\108600
```

## 주요 기능

- `mod.info`의 필수 모드 누락 의심 검사
- 동일 Lua 상대 경로 중복 검사
- 아이템, 차량, 레시피 중복 정의 검사
- Lua 이벤트 다중 후킹 확인
- Lua 전역/네임스페이스 중복 확인
- 맵 폴더명 중복 확인
- 결과 테이블 세로/가로 스크롤
- 컬럼 클릭 정렬
- 더블클릭 상세 보기
- `Ctrl+C`로 선택한 결과 복사
- CSV 저장

## 속도 개선

- 전체 `media` 폴더를 무작정 탐색하지 않고 `media/lua`, `media/scripts`, `media/maps` 중심으로 스캔합니다.
- 모드 단위와 파일 단위 분석에 `ThreadPoolExecutor`를 사용합니다.
- 분석 결과를 `~/.pz_mod_conflict_checker_cache.json`에 저장해서 같은 모드는 다음 검사에서 더 빠르게 처리합니다.

## 주의

이 도구는 정적 분석 기반이라 실제 게임 로드 순서, 모드 옵션, 런타임 패치까지 완벽히 판단하지는 못합니다. 결과는 “충돌 확정”이 아니라 “확인할 가치가 있는 의심 항목”으로 보는 것이 안전합니다.

캐시 사용 중 모드를 수정했는데 결과가 이상하면 GUI에서 `캐시 삭제` 후 다시 검사하세요.
