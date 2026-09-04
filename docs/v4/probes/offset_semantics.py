"""v4 코드 경로(app/v4/video.call_video)로 잰 **offset 멀티파트의 의미론**.

2026-09-04 실측 · `gemini-3.7-flash` · google-genai 2.22.0 · 이 저장소 워크트리.
`UNVERIFIED.md` §1 의 1·2번과 §2 의 7번을 이 스크립트가 닫았다.

    python -m docs.v4.probes.offset_semantics        # GEMINI_API_KEY 필요

## 무엇을 확인했나

① **첨부 순서가 곧 편집 순서다.** 6색 12초 소재에서 파트 3개를 역순으로 붙이면 답도
   역순으로 온다(두 배치 모두 정확). 기획서 §2-B 의 REST 실측이 **SDK 경로에서도** 성립한다.

② **좌표계는 파트 수에 따라 갈린다.**
   · **단일 창** — 모델은 **원본 절대초**로 답한다. 원본 4.3~5.7 을 붙이고 "시작/끝"을
     물으면 `{start: 4.4, end: 5.4}` 다. 창 안 0초로 답하지 않는다.
   · **여러 파트** — 모델은 **이어 붙인 영상의 좌표**로 답한다. 1.4초짜리 파트 3개의
     이음새를 물으면 `[1.4, 2.8]`(정확) 또는 `[1.2, 2.4]`(5fps 표본 granularity).
   ⇒ `app/v4/flags.py` 가 이음새 시각을 **편집본 좌표**로 주는 것은 맞다(실측 확인).
   ⇒ 시각을 묻는 단일 창 설계였다면 틀렸을 것이다. `app/v4/boundary.py` 는 시각이 아니라
     **컷 후보 id** 를 받으므로 이 질문 자체를 비껴간다(③).

③ **경계 프로브의 좌표 프레이밍은 판정에 영향이 없다.** 창 [3,9] 안 컷 3개를 창 상대초로
   보여주든 원본 절대초로 보여주든 정답 픽이 3/3 로 같다 — 모델이 **내용**으로 고르기
   때문이다. 그래서 `boundary.py` 의 프롬프트 문구는 그대로 둔다(무효한 변경 금지 —
   E18-4 판례: 픽셀이 안 바뀌는 변경을 되돌린 그 규율).

④ **thinking 이 출력 예산을 먹는다 — `max_output_tokens` 를 작게 잡으면 절단된다.**
   8단계 모양 프롬프트 실측(예산 4096):
   | thinking | thoughts | output | 판정 |
   |---|---:|---:|---|
   | minimal | 0 | 32 | ✅ |
   | low | 466 | 50 | ✅ |
   | medium | **2050** | 44 | ✅ (예산의 51%) |
   | high | 1397 | 44 | ✅ |
   같은 프롬프트를 **512** 로 부르면 thinking 487 · 출력 11 로 **MAX_TOKENS 절단**이 났다.
   ⇒ `flags.FLAG_MAX_OUTPUT_TOKENS = 4096` 은 이 모양에서 여유가 있다. 다만 medium 이
   2050 을 쓰므로 프롬프트가 복잡해지면 다시 재야 한다(V3-M2 의 절단 사고와 같은 자리).

⑤ **판정이 실제로 의미 있다.** 청록→빨강→파랑을 붙인 짜집기에 8단계 프롬프트를 태우니
   `seam_jump: true`(설명 없는 색 전환 — 맞다) · `hook_weak: true`(첫 2초가 정지 화면 —
   맞다) · `evidence_sec: [1.4, 2.8]`(정확한 이음새)이 왔다.

## 소재

6색 × 2초 = 12초(854×480 · 10fps · 사인파 오디오). 레포 `mrcheck3.py` 와 같은 설계다.
창은 색 **안쪽**(0.3~1.7 …)으로 잡는다 — 경계에 걸치면 전환 프레임이 섞여 판정이 흐려진다
(첫 시도에서 3파트가 5색으로 보고됐고, 원인이 그 경계 프레임이었다).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

COLORS = ("red", "green", "blue", "yellow", "magenta", "cyan")
KO = ("빨강", "초록", "파랑", "노랑", "자홍", "청록")
# 색 안쪽 창 — 경계 프레임 혼입 방지
INSIDE = {ko: (i * 2 + 0.3, i * 2 + 1.7) for i, ko in enumerate(KO)}


def build_source(out: Path, ffmpeg: str = "ffmpeg") -> Path:
    if out.exists():
        return out
    args = [ffmpeg, "-y"]
    for c in COLORS:
        args += ["-f", "lavfi", "-i", f"color=c={c}:s=854x480:d=2"]
    args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=12",
             "-filter_complex", "[0][1][2][3][4][5]concat=n=6:v=1:a=0[v]",
             "-map", "[v]", "-map", "6:a", "-r", "10",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(out)]
    subprocess.run(args, check=True, capture_output=True)
    return out


def main() -> int:
    from app.modules.gemini_client import load_gemini_client
    from app.v4.video import Clip, call_video

    tmp = Path(tempfile.gettempdir()) / "v4_offset_probe.mp4"
    build_source(tmp)
    g = load_gemini_client()
    f = g.client.files.upload(file=str(tmp))
    while f.state.name == "PROCESSING":
        time.sleep(2)
        f = g.client.files.get(name=f.name)

    def ask(clips, prompt, **kw):
        return call_video(g, f, prompt, sample_fps=5.0, clips=clips,
                          max_output_tokens=kw.pop("mo", 4096),
                          thinking_level=kw.pop("tl", "minimal"),
                          model=g.config.flash_model_name, log=lambda *a: None)

    Q = ('보이는 순서대로 색을 나열하라. JSON 만: {"colors":["…"]}  '
         '(빨강 초록 파랑 노랑 자홍 청록)')
    cl = lambda names: [Clip(*INSIDE[n]) for n in names]                    # noqa: E731
    try:
        print("① 첨부 순서 = 편집 순서")
        for order in (["빨강", "파랑", "청록"], ["청록", "빨강", "파랑"],
                      ["노랑", "자홍", "초록"]):
            got, _u = ask(cl(order), Q)
            ok = got.get("colors") == order
            print(f"   {order} → {got.get('colors')}  {'✅' if ok else '❌'}")

        print("\n② 좌표계")
        got, _ = ask([Clip(*INSIDE["파랑"])],
                     '영상의 시작과 끝 시각을 초로. JSON 만: {"start":숫자,"end":숫자}')
        print(f"   단일 창(원본 4.3~5.7) → {got}   ← 원본 절대초")
        got, _ = ask(cl(["청록", "빨강", "파랑"]),
                     '장면 전환이 두 번 있다. 각 시각을 초로. 이어 붙인 이 영상의 맨 앞이 '
                     '0초다. JSON 만: {"cuts":[숫자,숫자]}', tl="medium")
        print(f"   3파트 이음새(각 1.4초) → {got}   ← 편집본 좌표(1.4, 2.8)")

        print("\n④ thinking 이 먹는 출력 예산")
        P = ('화면 사고만 찾아라 — 점수 금지. true/false 와 근거 시각만.\n'
             '  · seam_jump : 이음새에서 인물·장소가 설명 없이 바뀌는가 (이음새 1.4s, 2.8s)\n'
             '  · hook_weak : 첫 2초 안에 사건이 없는가\n'
             '출력 JSON 만: {"seam_jump":bool,"hook_weak":bool,"evidence_sec":[숫자]}')
        for tl in ("minimal", "low", "medium", "high"):
            got, u = ask(cl(["청록", "빨강", "파랑"]), P, tl=tl)
            print(f"   {tl:8} thoughts={str(u['thoughts']):>5} output={u['candidates']:>3}  {got}")
    finally:
        g.client.files.delete(name=f.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
