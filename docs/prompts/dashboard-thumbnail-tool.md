# 요청: 대시보드 영상 화면에 「썸네일 생성」 추가

ves-workspace 작업 화면(`src/workbench.js`)의 오른쪽 검수 패널에서 **「편집실 열기」 버튼 바로 왼쪽**에 **「썸네일 생성」** 버튼을 넣고,
누르면 그 영상의 썸네일 후보를 만들어 보여 주고 사람이 골라 최종 썸네일을 받을 수 있게 해 주세요.
엔진(썸네일을 뽑고 합성하는 쪽)은 ai-video 에 이미 있습니다. 대시보드는 **엔진을 부르고, 결과 파일을 읽고, 사람의 선택을 파일로 넘기는 일**만 하면 됩니다.
ai-video 코드는 고치지 마세요 — 계약이 부족하면 ai-video 세션에 요청해 주세요.

## 1. 엔진

```
<ai-video>/.venv/bin/python -m app.tikitaka.thumbnail <job_dir> <suffix> [--no-llm]
```

- `job_dir` = 로컬 잡 폴더(대시보드가 이미 읽는 `outputs_tikitaka_grid/<잡>`), `suffix` = 영상 키의 뒷부분(예: `v12`).
  `local_videos_api.resolve(key)` 가 푸는 그 두 값입니다.
- 환경 변수: `FFMPEG_BIN`·`FFPROBE_BIN`(ffmpeg 7 — 편집실 적용 `start_apply` 가 쓰는 것과 같게), Flash 호출용 `GEMINI_API_KEY`(ai-video `.env` 를 엔진이 스스로 읽습니다).
- 작업 폴더: `<ai-video>` (모듈 경로 때문에 cwd 는 ai-video 레포 루트).
- 걸리는 시간: 첫 실행 약 15초(프레임 추출 + Flash 1회), 같은 영상 재실행은 프레임·Flash 결과를 캐시에서 읽어 수 초.
- 비용: 영상당 Flash 이미지 호출 1회(몇십 원). `--no-llm` 이면 호출 없이 점수 상위로만.
- 편집실 적용처럼 **백그라운드 프로세스로 띄우고 상태를 폴링**하는 방식을 권합니다(`start_apply`·`apply_state` 패턴 재사용).
  같은 영상에 두 번 동시에 띄우지 않게 막아 주세요.

## 2. 결과 파일 — `<job_dir>/thumbnails/<suffix>/`

| 파일 | 내용 |
|---|---|
| `thumbnails.json` | 기록(아래 스키마) — **이것만 읽으면 된다** |
| `thumb_1.png` … `thumb_N.png` | 최종 썸네일(1080×1920, 제목·로고 띠 + 라벨 포함) — 추천/선택 순 |
| `compare.png` | 최종 썸네일 가로 비교 시트 |
| `frames/cNN_KK.jpg` | 후보가 될 수 있는 **모든** 깨끗한 프레임(1080×1920, 라벨 없음 · 클립 NN 의 KK번째 0.5초 표본) |
| `sheet.jpg` | 전체 프레임 축소 시트(참고용) |
| `manual.json` | **사람의 선택** — 대시보드가 쓰는 파일(아래 4) |
| `select.json` | Flash 응답 캐시(대시보드는 안 건드림) |

`thumbnails.json` (schema `tikitaka_thumbnails/v1`):

```json
{
 "schema": "tikitaka_thumbnails/v1", "suffix": "v12", "title": "만취 상사 택시 태우기\n직장인의 눈물겨운 밤",
 "how": "flash | manual | score",            // 이번 결과를 누가 골랐나
 "band": [450, 1080],                        // 영상 밴드 (윗변 y, 높이) — 캔버스 1080×1920 기준
 "picks": [                                  // 최종 썸네일 = thumb_{rank}.png
  {"rank": 1, "file": "thumb_1.png", "frame": "c21_02", "clip": 21, "clip_time_sec": 0.5, "source_sec": 1233.15,
   "label": "(당 / 황)", "style": "split", "color": "yellow", "y": 935, "size": 80, "x": [161, 775],
   "person": "", "why": "고른 이유 한 문장"}
 ],
 "candidates": [ {"id": "c21_02", "clip": 21, "clip_time_sec": 0.5, "source_sec": 1233.15, "score": 0.81, ...} ],  // 점수 상위 16장(Flash 가 본 후보)
 "frames": [ {"id": "c00_01", "file": "frames/c00_01.jpg", "clip": 0, "clip_time_sec": 0.0, "source_sec": 1256.57,
              "score": 0.34, "faces": [[510, 955, 59, 75]]} ],   // 전체 프레임 · 얼굴 상자 [x, y, w, h] (캔버스 좌표)
 "colors": ["lime", "neon", "peach", "sky", "white", "yellow"],
 "safe_x": [100, 980]                        // 폰 재생 화면에서 글자가 안 잘리는 가로 범위
}
```

미디어는 기존 `/api/local-videos/media` 처럼 잡 폴더 안 파일만 서빙해 주세요(경로 탈출 금지 — `resolve` 와 같은 검사).

## 3. 화면 흐름

1. 영상 선택 → 검수 패널 상단 버튼 줄: **[썸네일 생성] [편집실 열기]** (썸네일 생성이 왼쪽).
   - 이미 `thumbnails.json` 이 있으면 버튼 이름을 「썸네일 보기」로 하고 바로 결과를 연다(다시 만들기는 결과 화면 안에).
   - 편집실 열기와 같은 조건(영상 묶음 `ready`)에서만 활성.
2. 누르면 엔진 실행(진행 표시) → 끝나면 썸네일 창(대화 상자 또는 오른쪽 패널 확장):
   - **추천 썸네일**: `picks` 의 `thumb_N.png` 를 나란히(각각 라벨·고른 이유 `why` 표시) + 개별 다운로드.
   - **다른 장면 고르기**: `frames` 전체를 클립 순 격자로(점수 높은 16장 `candidates` 는 표시) — 눌러서 후보에 추가.
3. 후보 하나를 누르면 라벨 편집:
   - 문구(빈칸 = 라벨 없음), 형식 `line`(한 줄) / `split`(얼굴 양옆 둘로 나눔 — 예 「(긴」 「장)」, 입력칸 둘),
     색(`colors` 중 선택 — `neon` 은 형광 번짐), 세로 위치(선택 — 비우면 엔진이 얼굴을 피해 자동 배치).
   - 미리보기는 브라우저에서 대략만(프레임 위에 텍스트 겹쳐 보이기) — 정확한 결과는 4의 합성으로.
4. **[이 목록으로 만들기]** → `manual.json` 을 쓰고 엔진을 다시 실행 → 새 `thumb_N.png` 표시.
5. 결과에서 **[처음 추천으로 되돌리기]** = `manual.json` 을 지우고(또는 `manual.prev_<시각>.json` 으로 옮기고) 엔진 재실행.

## 4. `manual.json` — 사람이 고른 목록(대시보드가 쓰는 유일한 파일)

```json
[
 {"frame": "c21_02", "style": "split", "parts": ["(당", "황)"], "color": "yellow", "why": "메모(선택)"},
 {"frame": "c22_03", "label": "(이걸로 한 잔만!)", "color": "white", "y": 1400},
 {"frame": "c25_05", "label": "한 잔만 더 하자고~", "color": "neon"}
]
```

- 배열 순서 = `thumb_1`, `thumb_2`, … 순서. `frame` 은 `thumbnails.json.frames[].id` 중 하나(없는 id 는 엔진이 건너뜀).
- `label` 빈 문자열이면 라벨 없는 썸네일. `style: "split"` 이면 `parts` 두 칸 필수(얼굴 옆자리가 모자라면 엔진이 한 줄로 바꿈).
- `color` 는 `colors` 중 하나(모르면 white). `y` 는 라벨 중심의 캔버스 y(선택).
- 이 파일이 있으면 엔진은 Flash 를 부르지 않고 이 목록으로만 합성한다(`how: "manual"`).
- 저장할 때 작성자·시각을 옆 파일(예: `manual.meta.json`)에 남겨 주면 작업 이력에 쓸 수 있습니다(엔진은 안 읽음).

## 5. 주의

- 썸네일은 **영상 묶음과 따로** 있다 — 썸네일을 만들어도 영상·편집실 기록은 바뀌지 않습니다.
  반대로 영상을 다시 렌더하면 크롭이 바뀔 수 있어, `thumbnails.json` 이 영상 렌더보다 오래됐으면
  「영상이 바뀌었어요 — 다시 만들기」 안내를 띄워 주세요(비교: `videos/<suffix>/video.json` 의 `render_fingerprint`·`exported_at` 과 파일 시각).
- 라벨 문구는 Flash 제안이 **틀린 인물의 대사**일 수 있습니다(대화 장면의 리액션 컷). 추천 썸네일 옆에 「라벨 확인 필요」 한 줄을 붙여 주세요.
- 폰 안전 폭(`safe_x`) 밖으로 나가는 라벨은 엔진이 글자 크기를 줄여 맞춥니다 — 대시보드가 따로 검사할 필요는 없습니다.
- 채널 미배정 로컬 영상만 대상입니다(`source: local-bundle`). Supabase 쪽 영상에는 버튼을 숨겨 주세요.
