# L-P4 실측 회귀 0 — mm-06 실행 절차

계획서 §8-3: **잔망루피 10편, CER · 라우드니스(-16 LUFS) · 세그먼트 정렬**이 구본과 같아야
한다. 이 문서는 그것을 재는 순서다.

⚠ **대조 기준이 job_queue 가 아니다.** VES 를 통한 overlay 잡은 3건뿐이고 마지막이
2026-08-13, route C·BC 는 한 번도 안 돌았다. 잔망루피 실운영은 **mm-06 의 vlp autopilot**
(자체 SQLite 원장)이므로 기준 산출물도 거기 있다.

🛑 **`scripts/localize_ab.py` 를 쓰지 마라.** 그것은 rerender 전용이라 overlay 산출
디렉토리에서 파일을 하나도 못 찾고 **거짓 합격**을 낸다. overlay 는 `scripts/overlay_ab.py` 다.

---

## 0. 준비 — ⚠ 운영 체크아웃을 건드리지 않는다

노드의 `$R/ai-video` 는 updater 가 SHA 로 고정한 **detached HEAD** 다. `git pull` 은
"You are not currently on a branch" 로 거절되고, 억지로 옮기면 **잡을 돌리는 중인 엔진을
바꾸는 것**이 된다. P1 때와 같이 **별도 워크트리**에서 돌린다.

```zsh
R=/opt/ves/engines
AIV=$R/ai-video/.venv/bin/python        # 인터프리터는 운영 venv 를 그대로 쓴다
W=/tmp/aiv-p4                            # 대조용 워크트리

cd $R/ai-video && git fetch origin main
git worktree add -f $W origin/main       # 운영 체크아웃은 그대로 둔다
cd $W && git log --oneline -1
```

끝나면 정리: `cd $R/ai-video && git worktree remove $W --force`

⚠ 워크트리에는 `.venv` 가 없다 — 위 `$AIV`(운영 venv 의 인터프리터)를 쓰고 **cwd 만**
워크트리로 둔다. 그래야 새 코드가 돌면서 설치된 의존성을 그대로 쓴다.

## 1. 이식이 vlp 를 따라잡고 있는지부터

vlp 는 이식 중에 **두 번** 앞서갔다(P2b·E16). 대조 전에 먼저 확인한다.

```zsh
cd $W
VLP_ROOT=$R/video-localization-project $AIV -m scripts.overlay_port_diff --verbose
```

`예상 밖 차이 0` 이 아니면 **여기서 멈춘다** — 그 차이를 이식하기 전에는 A/B 가 무의미하다.

## 2. 기준(구 엔진) 산출물 고르기

autopilot 이 이미 처리한 영상의 `outputs/<video_id>` 가 기준이다. **자격이 되는 것만**
뽑는다 — `translations.json`·`ja_events.json` 이 둘 다 있어야 CER·정렬을 잰다.

⚠ 아래 블록에 주석을 넣지 마라. 노드 zsh 는 `interactive_comments` 가 꺼져 있어
`#` 가 glob 문자로 읽힌다(이 세션에서 실제로 `unknown file attribute` 로 죽었다).

```zsh
O=$R/video-localization-project/outputs
for d in $O/*(/); do
  b=${d:t}
  [ -f $d/translations.json ] && [ -f $d/ja_events.json ] && \
    echo "$b  ev=$(grep -c '"start"' $d/ja_events.json)  final=$([ -f $d/final_draft.mp4 ] && echo Y || echo -)"
done
```

`ev` 는 자막 이벤트 수다 — **10건 이상**인 편이 대조로서 의미가 있다(2~3건짜리는
어긋나도 안 드러난다). `final=Y` 인 편이라야 라우드니스·길이까지 잰다.

원본 mp4 는 여기 있다:

```zsh
ls $R/video-localization-project/data/source | head
```

## 3. 같은 소재로 신 엔진 실행

```zsh
VID=<위에서 고른 video_id>
SRC=<그 원본 mp4 경로>          # autopilot 이 받아 둔 것 (data/source/ 아래)

cd $W
$AIV -m app.cli localize --mode overlay --video "$SRC" --video-id "${VID}_new" --route B
```

⚠ **OCR 백엔드가 양쪽에서 같아야 한다.** config 기본은 `paddleocr` 다. 로그에 어느
백엔드가 잡혔는지 찍히니 확인하고, 갈렸으면 `--inpaint-backend` 가 아니라 config 의
`detect.ocr_backend` 를 맞춘다(인페인트와 OCR 은 다른 노브다).

## 4. 대조

```zsh
$AIV -m scripts.overlay_ab \
     --a $R/video-localization-project/outputs/$VID \
     --b $W/outputs/${VID}_new
```

읽는 법:

| 줄 | 뜻 |
|---|---|
| `원문(OCR·탐지)` | **회귀 판정 대상.** 달라지면 OCR·탐지가 흔들린 것 — 번역보다 상류라 더 무겁다 |
| `세그먼트 정렬` | **회귀 판정 대상.** 어긋나면 자막이 딴 장면에 뜬다 (허용 0.05s) |
| `최종본 길이` · `라우드니스` | **회귀 판정 대상** (허용 ±1.0 LUFS) |
| `번역문 CER` | **판정에서 뺀다** — LLM 비결정성이다. 크기만 본다 |

`⚠ 못 쟀다` 가 뜨면 그 항목은 **판정에 안 들어간 것**이다 — ffmpeg 이 비대화형 SSH 의
PATH 에 없을 때 그렇다. `FFMPEG_BIN`·`FFPROBE_BIN` 을 지정하고 다시 돌린다.

## 5. 10편 반복

```zsh
for VID in <id1> <id2> …; do
  $AIV -m scripts.overlay_ab --a $R/video-localization-project/outputs/$VID \
                             --b $W/outputs/${VID}_new --json \
    > /tmp/ab_$VID.json
  echo "$VID: $($AIV -c "import json,sys;print('OK' if json.load(open('/tmp/ab_$VID.json'))['ok'] else 'FAIL')")"
done
```

## 6. route C (더빙)까지

더빙은 **overlay 파이프라인이 부르지 않는다**(검수 게이트 뒤 별도 단계). 3번을
`--route C` 로 돌린 뒤 따로 실행한다:

```zsh
$AIV -m app.localize.overlay.dub --video-id="${VID}_new" --video="$SRC" \
     --level=C --voice=<이 채널의 elevenlabs voice_id>
```

⚠ **`voice_id` 를 반드시 준다.** 안 주면 config 기본값(잔망루피 클론 보이스)으로 떨어진다 —
다른 채널이면 루피 목소리로 더빙된다(어댑터 `dub_argv` 가 같은 이유로 강제한다).

route C 합격선은 **같은 `voice_id` 로 목소리가 같은지**까지다.

## 6-1. route BC — 기준본이 없으니 만들어서 잰다

BC 는 vlp·이식본 통틀어 **한 번도 안 돌았다.** autopilot 산출에 기준이 없으므로 route C
때와 같이 **구 엔진으로 한 판 만들어** 대조한다.

BC 가 C 와 다른 곳은 `render_mode` 하나다(`levels.BC`): `replace`(일본어 텍스트 재합성)
대신 `clean` — **지운 자리에 아무것도 안 그린다**(더빙 자막이 자막을 담당한다). 그래서
볼 것은 ① 인페인팅 결과가 같은가 ② 텍스트 렌더가 정말로 없는가 둘이다.

```zsh
R=/opt/ves/engines
VLP=$R/video-localization-project
AIV=$R/ai-video/.venv/bin/python
W=/tmp/aiv-p4
SRC=$VLP/data/source/<원본 mp4>

cd $VLP && .venv/bin/python -m src.process_video --video "$SRC" --video-id 5b2N_oldBC --level BC
cd $W   && $AIV -m app.cli localize --mode overlay --video "$SRC" --video-id 5b2N_newBC --route BC
$AIV -m scripts.overlay_ab --a $VLP/outputs/5b2N_oldBC --b $W/outputs/5b2N_newBC
```

🛑 **`--video-id` 를 반드시 새 값으로 둔다.** 운영 id 를 쓰면 autopilot 산출물을 덮어쓴다.

⚠ 인페인팅이 양쪽에 붙는다 — 11초짜리 소재로 한 판에 ~18분이다(2026-08-25 실측).

⚠ 더빙까지 이어 보려면 §6 의 명령에서 `--level=BC` 로 준다. 종전 게이트는 `C` 만
통과시켜 **BC 를 거부**했다(vlp `src/dub.py:31`) — 이식본이 `DUB_ROUTES` 정본으로
고쳤고 어댑터도 그 잡의 route 를 싣는다. 그 수정 전에는 이 단계가 아예 안 돌았다.

## 6-2. self-ref 프로브 — 지금 구성에서는 **도달 불가**다

`build_self_ref` 이하는 `dub_backend(config) == "gptsovits"` 일 때만 지난다. config 의
`tts_backend` 는 **`elevenlabs`** 다(2026-08-13 전환) — 그래서 실측 로그에 흔적이 없었던
것이지 refbank 에 항목이 있어서가 아니다. 실제로 재려면 백엔드를 되돌리고 GPT-SoVITS
모델을 노드에 얹어야 한다(롤백 경로 검증이고, 별건이다).

이식에서 고친 것은 그 경로가 **자기를 어떻게 다시 부르는가**(`_SELF_MODULE`)뿐이라,
모델 없이도 배선만 싸게 확인된다:

```zsh
$R/ai-video/.venv/bin/python -m app.localize.overlay.dub --probe-ref=/dev/null --prompt-text=x
```

`PROBE_FAIL: …` + exit 1 이면 통과다 — 모듈 해석이 됐다는 뜻이다.
`ModuleNotFoundError: No module named 'src'` 면 vlp 잔재가 남은 것이다.

**실측(2026-08-25 mm-06)**: `PROBE_FAIL: No module named 'soundfile'` — 배선은 정상.
곁다리로 `soundfile` 이 requirements 에서 빠져 있던 것을 잡았다(지연 임포트라 안 보였다).

## 7. 통과하면

어댑터 컷오버는 P2 와 같은 모양이다(스위치 한 값). 그때까지 `--mode overlay` 를 주는
코드가 없으므로 **지금 상태로는 아무것도 안 바뀐다**.
