# v4 실측 프로브 (2026-09-01~02)

v4 기획서(`../v4-pipeline-plan.md`) §2 A~H 의 "확정" 표시는 전부 이 디렉토리의 스크립트로
**Gemini API 를 직접 호출해** 얻은 값이다. 문서만 믿지 말고 여기서 재현하라.

- 모델 `gemini-3.7-flash` · REST `generateContent` · 표준 라이브러리 `urllib` 만 사용(SDK 불필요).
- 키는 `GEMINI_API_KEY` 환경변수 우선, 없으면 레포 `.env` 를 읽는다.
- ffmpeg 는 `/opt/homebrew/bin/ffmpeg` 하드코딩 — 노드에 맞게 고칠 것. 이 빌드엔 libass·drawtext 가
  없어 화면 글자는 전부 PIL 로 그린다.
- 업로드한 Files API 파일은 각 스크립트가 끝에서 DELETE 한다. `results/*.json` 의 `files/…` URI 는
  이미 만료된 임시 id 라 무해하다.
- ⚠ **countTokens 를 쓰지 마라** — 멀티파트 영상을 3.8배 과소 계산한다(`mrcheck2.py` 가 그 증거).
  과금은 반드시 `usageMetadata` 로.
- ⚠ **`tokens/` 는 `count_tokens` 단위, 나머지는 `usageMetadata` 단위다** — 두 산식이
  다르다(71/32 vs 66/25 · 비율 0.883). 섞어 인용하지 마라: **상한 초과 400 판정은
  count 쪽**, **과금·예산 집계는 usage 쪽**이다(CLAUDE.md 「산식은 둘이고 쓰는 자리가
  다르다」). `tokens/` 는 2026-09-03 에 `scripts/gemini_tokens_probe/` 에서 옮겨 왔다.

| 스크립트 | 답한 질문 | 기획서 절 |
|---|---|---|
| `mrcheck.py` | media_resolution 미지정/LOW/MEDIUM/HIGH 토큰 · fps 비례 · offset 단일/멀티파트(countTokens) | §2-A·C |
| `mrcheck2.py` | countTokens 가 멀티파트를 과소 계산하는 패턴 | §2-C |
| `mrcheck3.py` | **결정적**: 6색 12초 소재로 offset 멀티파트 동작·첨부 순서=편집 순서·과금=조각 합계(generateContent) | §2-B |
| `fps_gain.py` | 200ms 마다 바뀌는 숫자 60개 — fps 1/2/5 × 기본/HIGH 회수량 | §2-D |
| `fps_gain_long.py` | 431초 희소 신호(400ms 번쩍임 20곳) — 장편에서도 fps 비례인가 | §2-D |
| `seam_equiv.py` | 실제 프록시(유미의 세포들 시즌3) 후보 6개 × 2라운드 — offset vs 실렌더 이음새 판정·순위 일치 | §2-E |
| `fps_cap_check.py` | fps 하드캡(24) · 파일 fps 초과 요청의 과금/정보 | §2-F |
| `agent_fps_ceiling/` | (워크플로 에이전트) fps 1~24 회수율·출력 절단·오디오 항 · 파일 fps 초과 | §2-F |
| `agent_small_text/` | (에이전트) 글자 높이별 기본 vs HIGH · 한글/라틴 · 크롭 대안 · 실소재(도깨비) | §2-H |
| `agent_proxy_res/` | (에이전트) 프록시 480p/720p/1080p × 기본/HIGH · 비용(신병4 10분) · 실소재(가왕쇼) | §2-G |
| `results/latency_*.json` | (에이전트) 600s·2400s 프록시 fps 별 지연 5회 반복 → 회귀 `8.05 + 75.7×(토큰/1M)` | §2-F |
| `results/parallel_probe.json` | (검수 반박자) 시각 플래그 8콜 병렬 실측 | 검수 보고 |
| `tokens/measure.py` | 60초 보정본 fps·media_resolution 별 **count_tokens** → 프레임당 71 · 오디오 32 | CLAUDE.md fps 계약 |
| `tokens/long.py` | 3시간 실물 업로드 → 길이 하드 상한 없음 · fps 0.85 성공 / 1.0 은 400 | CLAUDE.md fps 계약 |
| `tokens/res.py` | 해상도별 토큰 동일성 재확인 | §2-G |

소재 mp4/png 는 크기 때문에 커밋하지 않았다 — 스크립트가 다시 만든다(합성) 또는 레포 `outputs_ab/` 의
프록시를 쓴다(실소재). `seam_equiv.py` 의 `src.mp4` 는
`outputs_ab/yumi_ep1/유미의_세포들_시즌3_c7/유미의 세포들 시즌3_480.mp4` 사본이다.
