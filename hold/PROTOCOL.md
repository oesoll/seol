# peer-hold — 프로세스는 기기가 들고, 화면은 앞단이 그린다

코딩 에이전트 터미널은 sm과 Fine이 그린다. 코딩 에이전트가 어느 기기에서 돌든 화면과 프로세스의 수명은 분리된다. 그래서 기기 쪽에는 tmux가 필요 없다. 기기는 프로세스와 현재 VT 화면 상태를 들고 있고, 실제 렌더링은 앞단이 한다.

`peer-hold`는 에이전트마다 하나씩 떠서 그 일을 한다.
- 하네스(claude·codex 또는 그 래퍼)를 새 PTY에서 부모 없이 띄운다. 부모는 시스템(ppid 1)이 된다. 띄운 쪽이 꺼지거나 맥 뚜껑을 닫아도 에이전트는 산다.
- 보는 사람이 없어도 PTY 출력을 계속 읽는다. 읽지 않으면 TUI가 멈추기 때문이다. 최근 출력(`--ring`, 기본 1MB)은 진단용 로그로, 현재 화면은 `vt100` 상태 모델로 따로 기억한다.
- 붙은 뒤의 live 바이트는 그대로 넘긴다. 재접속에는 현재 화면·색·커서·입력 모드만 보내며 과거 출력·터미널 질의·벨·클립보드 동작은 재생하지 않는다. 화면 복원은 synchronized output(`CSI ?2026 h/l`) 한 묶음이다. 대체 화면이면 돌아갈 기본 화면도 함께 복원한다.
- 배치·창·키 바인딩은 없다. 그건 sm의 일이다.

## 명령
```
peer-hold start --sock PATH [--cwd DIR] [--env K=V]... [--cols C --rows R] [--ring BYTES] -- CMD...
peer-hold attach --sock PATH [--detach-key] [--passive]
peer-hold status --sock PATH
peer-hold tail --sock PATH [--lines N] [--raw]
peer-hold screen --file PATH [--cols C --rows R]
```
- `start`는 소켓이 살아난 뒤에 반환하고 `{"holder":PID,"child":PID,"sock":PATH}`를 출력한다. `PATH.json`에는 cmd·cwd·시작 시각이 있고, CMD가 끝나면 `PATH.exit`에 종료 코드가 남는다(소켓과 .json은 지워진다).
- CMD는 `PEER_HOLD_SOCK=PATH`를 받는다. 띄운 쪽의 `FINE_TAB_ID`·`SM_PANE_ID`·`TMUX_PANE`은 물려받지 않는다. 물려받으면 peer가 그 세션을 띄운 쪽의 탭이나 패널로 오인한다. sm 패널에 묶으려면 `--env SM_PANE_ID=<UUID>`로 명시해서 넘긴다.
- `attach`는 지금 터미널을 붙인다. 터미널을 닫으면 떨어지고, 에이전트는 계속 돈다. 같은 크기의 재접속은 PTY 크기나 에이전트를 건드리지 않는다. 한 줄 줄였다가 80ms 뒤 되돌리던 redraw nudge는 제거했다.
- `tail`은 화면 복원이 아닌 진단용 출력 로그다. `--raw`에는 과거 질의도 포함되므로 터미널에 재생하지 않는다.
- 종료 시 `.screen`은 `PHSC` + 버전 byte `1` + cols/rows(u16 BE) + frozen ANSI snapshot이다. 남은 마지막 PTY 출력까지 반영하고, 미완성 escape/UTF-8 접두사는 버린다. 이 파일은 직접 출력하지 않고 `peer-hold screen --file …`로 읽는다.
- `screen`은 새 형식의 snapshot을 꺼내고, 구형 raw ring 파일은 읽을 때 화면으로 변환한다. 구형 파일에는 크기가 없으므로 기본 120×40(필요하면 옵션으로 지정)에서 해석한다. 원본 파일은 보존한다. `sm screen`과 일반 `sm attach`의 마지막 화면도 이 경로를 사용한다.

## 소켓 메시지 (1단계와 2단계가 같다)
메시지 하나는 `[type u8][len u32 big-endian][payload]`다.

| type | 방향 | payload |
|---|---|---|
| 1 HELLO | 앞단→holder | cols u16, rows u16 (BE). 현재 화면 snapshot 하나를 OUTPUT으로 보낸다. 0×0은 passive 뷰어. 실제 크기가 달라질 때만 PTY 크기를 바꾼다 |
| 2 INPUT | 앞단→holder | PTY에 넣을 바이트(키 입력·붙여넣기) |
| 3 RESIZE | 앞단→holder | cols u16, rows u16 |
| 4 STATUS | 앞단→holder | 없음 |
| 5 TAIL | 앞단→holder | 없음, 진단용 raw ring 요청 |
| 10 OUTPUT | holder→앞단 | 첫 프레임은 화면 snapshot, 그 뒤는 live PTY 바이트 그대로 |
| 11 EXIT | holder→앞단 | i32 종료 코드. 이 뒤에 연결이 닫힌다 |
| 12 STATUS | holder→앞단 | JSON: holder, child, cols, rows, viewers, active, seen, ring, started, cwd |
| 13 TAIL | holder→앞단 | 진단용 raw ring |

여러 앞단이 동시에 붙을 수 있다. 창 크기는 마지막으로 입력한 active 뷰어(입력 전에는 마지막으로 붙은 active 뷰어)를 따른다. passive 뷰어는 크기를 바꾸지 않는다. 8MB 넘게 밀린 앞단은 끊는다. 앞단이 느리다고 에이전트를 기다리게 하지는 않는다.

## 앞단이 붙는 법
- **1단계 (지금):** 패널 PTY에서 `peer-hold attach --sock …`를 실행한다. 원격이면 `ssh -t arch peer-hold attach --sock …`다.
- **2단계 (sm이 직접):** 기기마다 ssh 연결 하나를 열고(`ssh arch peer-hold mux`, 아직 만들지 않았다), 그 안에서 패널별 채널로 위 메시지를 주고받는다. 채널 메시지는 앞에 `chan u32`를 붙이고, 열기·닫기 제어 메시지를 더한다. sm은 보이는 패널만 연다. 나머지 패널은 peer 상태 JSON만 본다. 형식은 mux를 만들 때 이 절에서 확정한다.

## 시험

```sh
cargo test --manifest-path hold/Cargo.toml
cargo build --release --manifest-path hold/Cargo.toml
python3 -I hold/tests/test_hold.py hold/target/release/peer-hold
python3 -I hold/tests/measure_reconnect.py hold/target/release/peer-hold
python3 -I tests/sm/test_sm_session.py
```

마지막 sm 시험은 `bin/peer-hold`를 사용한다. `measure_reconnect.py`는 `SM_TEST_LAUNCHER`와 격리 SM/peer 상태를 사용하며 실제 모델을 시작하지 않는다. 터미널 응답은 결정적인 시험용 응답기다. 별도 앱 시험에서는 실제 libghostty로 과거 DA·DSR·OSC 11 질의가 입력으로 돌아옴을 확인했고, 새 snapshot은 현재 화면 표시 + 1.5초 동안 입력 0바이트를 확인했다.

재접속 5회, 120×40, 약 1.4MB의 지난 redraw가 있는 fixture에서 구형은 매번 약 1,048,500바이트·마지막 출력까지 81ms·SIGWINCH 총 10회였다. 새 버전은 현재 화면 분량만 보내며 과거 질의·추가 SIGWINCH가 없다. 시간은 로컬 소켓 수신 기준이고 150ms 무출력 확인 창과 렌더러 시간은 제외한다. 다른 작업의 CPU 부하가 있으므로 시간보다 바이트·신호·입력 오염 검증을 우선한다.

## 범위와 한계

- 재접속에는 보이는 화면(대체 화면이면 기본 화면도)만 복원한다. 전체 scrollback은 옮기지 않는다. 붙어 있는 뷰어의 scrollback과 `tail` 로그는 유지된다. live 출력 자체를 diff로 바꾸지는 않는다.
- 상태 모델은 `vt100`이 지원하는 텍스트·SGR·커서·대체 화면을 기준으로 한다. 이미지 프로토콜, OSC 8 링크 등 확장 렌더링은 snapshot에 복원하지 않는다. live 전달은 그대로다. 이런 상태의 복원이 필요하면 해당 기능을 모델에 추가하거나 더 완전한 VT 엔진으로 교체한다.
- 화면 너비 변경은 모델이 행을 자르거나 늘리는 방식이며 긴 문장을 재배치하지 않는다. 크기가 달라지면 실제 SIGWINCH에 따른 프로그램의 redraw가 뒤따를 수 있다. 같은 크기 재접속에는 강제 redraw가 없다.
- PTY read가 escape/UTF-8 중간에서 끝나도 live 재접속은 미완성 접두사를 이어 준다. 4KB를 넘는 미완성 제어 문자열은 새 뷰어에서 끝 기호까지 무시한다. frozen 화면에는 접두사를 넣지 않는다.
- 동시에 붙은 여러 live 뷰어가 새 질의에 각각 답하는 동작은 기존과 같다. 이번 수정은 과거 질의를 재접속마다 다시 실행하는 원인을 제거한다.
- 구형 `.screen`은 raw log를 한 번 해석해야 하므로 읽는 비용이 파일 크기에 비례한다. 새 holder가 저장한 화면부터는 파일 읽기·전송 모두 화면 크기에 비례한다.
- 재부팅되면 프로세스는 남지 않는다. peer 등록부의 세션 id로 같은 대화를 다시 부르는 일은 sm이 맡는다(`sm restore`, `sm resume NAME`).
