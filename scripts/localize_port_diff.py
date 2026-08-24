"""L-P1 — 이식 충실성 대조: vlp 원본 함수 vs ai-video `app/localize` 이식본.

발주서: ves-orchestrator `docs/LOCALIZE_UNIFY.md` §9 P1.
같은 입력을 두 구현에 먹여 **산출이 바이트까지 같은지** 본다. 회귀 0 이 이관의 조건이라
"옮겼다"가 아니라 "같은 답을 낸다"를 증명해야 한다.

    python -m scripts.localize_port_diff            # vlp 가 형제로 있을 때만

⚠ vlp 는 이관이 끝나면 동결된다(기획서 §2). 그때 이 스크립트도 함께 은퇴한다 —
그때까지는 컷오버 판정의 근거다. vlp 를 못 찾으면 조용히 건너뛴다(CI 안전).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

AIV = Path(__file__).resolve().parent.parent
VLP = Path(os.environ.get("VLP_ROOT") or AIV.parent / "video-localization-project")

if not (VLP / "scripts" / "localize_run.py").exists():
    print(f"[port_diff] vlp 원본이 없어 건너뜁니다: {VLP}")
    raise SystemExit(0)

sys.path.insert(0, str(VLP))
sys.path.insert(0, str(AIV))

_spec = importlib.util.spec_from_file_location(
    "localize_run", VLP / "scripts" / "localize_run.py")
lr = importlib.util.module_from_spec(_spec)
sys.modules["localize_run"] = lr
_spec.loader.exec_module(lr)

from app.localize import BACKUP_FILES  # noqa: E402
from app.localize import apply as A  # noqa: E402
from app.localize import meta as M  # noqa: E402
from app.localize import overrides as O  # noqa: E402
from app.localize import rerender as R  # noqa: E402
from app.localize import style_texts as ST  # noqa: E402


def _vlp_sha() -> str:
    import subprocess
    r = subprocess.run(["git", "-C", str(VLP), "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "?"


# vlp 쪽에 있어야 하는 기능들 — 없으면 **버전이 다른 것**이지 이식 결함이 아니다.
# 이 확인이 없으면 낡은 체크아웃과 대조해 놓고 "이식이 틀렸다"고 읽게 된다(실제로 났다).
REQUIRED_VLP_FEATURES = [
    ("apply_overrides 줄 스타일 검증(8/20)", lambda: _raises(
        lambda: lr.apply_overrides({"segments": [{"index": 0}]},
                                   {"subs": {"0": {"style": {"fontsize": 1}}}}))),
    ("build_telop_ass 줄 오버라이드(8/20)", lambda: _telop_applies_style()),
    ("l3_apply 소프트 삭제(E6-0)", lambda: _l3_drops_unused()),
    ("E16 화면 글자 현지화(8/24)", lambda: hasattr(lr, "apply_style_translation")),
]


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:                                     # noqa: BLE001
        return True


def _telop_applies_style() -> bool:
    with tempfile.TemporaryDirectory() as t:
        out = Path(t) / "x.ass"
        lr.build_telop_ass([{"orig_index": 0, "start_sec": 1.0, "end_sec": 2.0, "text_ko": "가"}],
                           {"telops": [{"index": 0, "use": True, "ja": "T",
                                        "style": {"size": 64}}]}, "F", out)
        return "\\fs64" in out.read_text(encoding="utf-8")


def _l3_drops_unused() -> bool:
    with tempfile.TemporaryDirectory() as t:
        job = Path(t) / "j"; job.mkdir()
        bk = job / "bk"; bk.mkdir()
        od = job / "o"; od.mkdir()
        (bk / "subtitle_segments.json").write_text(json.dumps(
            [{"start_sec": 0.0, "end_sec": 1.0, "text": "a"},
             {"start_sec": 2.0, "end_sec": 3.0, "text": "b"}]))
        (bk / "checkpoint_story.json").write_text(json.dumps({"title_text": "T"}))
        (bk / "edit_plan.json").write_text(json.dumps({"layout": {"top_title": "T"}}))
        (bk / "checkpoint_resources.json").write_text(json.dumps({"tts_cue_files": []}))
        lr.l3_apply(job, bk, {"top_title_ja": "T",
                              "segments": [{"index": 0, "ja": "A", "use": False},
                                           {"index": 1, "ja": "B"}],
                              "tts_cues": [], "telops": []},
                    [], {"display": "X"}, {"telop_font": "F"}, od)
        return len(json.loads((job / "subtitle_segments.json").read_text())) == 1


def _check_vlp_version() -> None:
    missing = [name for name, probe in REQUIRED_VLP_FEATURES if not probe()]
    print(f"[port_diff] vlp = {VLP} @ {_vlp_sha()}")
    if missing:
        print("\n🛑 이 vlp 체크아웃에 없는 기능이 있습니다 — **버전 차이**지 이식 결함이 아닙니다:")
        for m in missing:
            print(f"   - {m}")
        print("   `git -C <vlp> pull` 로 main 을 맞춘 뒤 다시 돌리세요.")
        print("   ⚠ 회귀 0 의 기준은 **플릿이 실제로 도는 sha** 입니다 —")
        print("     SELECT last_seen_sha FROM deployments WHERE engine='localization';")
        raise SystemExit(2)


# brain 은 **양쪽을 같은 값으로 못박는다.** 두 모듈 모두 형제 디렉토리에서 BRAIN 을
# 추론하는데, 이식본이 워크트리(/tmp/...)에서 돌면 서로 다른 brain 을 보게 되어
# render_flags 폴백이 갈린다 — 로직 차이가 아니라 대조 환경의 차이다(실제로 났다).
def _pin_brain(path: Path) -> None:
    lr.BRAIN = path
    R.BRAIN = path


_check_vlp_version()

fails = []
def eq(name, a, b):
    if a != b:
        fails.append(f"{name}\n   원본: {a!r}\n   이식: {b!r}")
    print(f"  {'OK' if a == b else '!!'} {name}")

# ── render_flags (brain 을 양쪽 동일하게 고정한 뒤 비교)
print("[render_flags]")
_brain_tmp = tempfile.TemporaryDirectory()
_pin_brain(Path(_brain_tmp.name) / "no-brain")
cases = [
    {"provenance": {"config": {"app": {"silence_cut_profile": "aggressive",
      "target_duration_sec": 45, "max_duration_sec": 50, "max_duration_tolerance": 1.1}}}},
    {"provenance": {"config": {"app": {"silence_cut_profile": "conservative",
      "target_duration_sec": 60, "max_duration_sec": 70, "max_duration_tolerance": 1.5}}}},
    {"provenance": {"config": {"app": {}}}},
    {},
]
for i, rl in enumerate(cases):
    eq(f"case{i}(brain 없음)", lr.render_flags(rl), R.render_flags(rl))

# 폴백 경로도 같은 정책 파일로 양쪽 동일하게
_policy = Path(_brain_tmp.name) / "brain"
(_policy / "config").mkdir(parents=True, exist_ok=True)
(_policy / "config" / "loop_policy.json").write_text(
    '{"gen_flags_base": ["--silence-profile", "aggressive", "--length-profile", "tight"]}',
    encoding="utf-8")
_pin_brain(_policy)
for i, rl in enumerate(cases):
    eq(f"case{i}(policy 폴백)", lr.render_flags(rl), R.render_flags(rl))

# ── _ass_escape / _fmt_ts
print("[escape/ts]")
for t in ["a\nb", "{x}", "普通", "a{b}c\nd"]:
    eq(f"escape {t!r}", lr._ass_escape(t), A._ass_escape(t))
for s in [0.0, 3661.5, 59.999, 123.456]:
    eq(f"ts {s}", lr._fmt_ts(s), A._fmt_ts(s))

# ── build_telop_ass (바이트 동일성)
print("[build_telop_ass]")
refined = [{"orig_index": 0, "start_sec": 1.0, "end_sec": 2.0, "text_ko": "가"},
           {"orig_index": 1, "start_sec": 3.0, "end_sec": 4.0, "text_ko": "나"},
           {"orig_index": 2, "start_sec": 5.0, "end_sec": 6.0, "text_ko": "다"}]
tr_t = {"telops": [
    {"index": 0, "use": True, "ja": "テロップ{注}",
     "style": {"size": 64, "color": "#FFDD00", "rotate": -8, "y": 0.5},
     "start_sec": 3.5, "end_sec": 6.0},
    {"index": 1, "use": False, "ja": "消える"},
    {"index": 2, "use": True, "ja": "そのまま"}]}
with tempfile.TemporaryDirectory() as tmp:
    pa, pb = Path(tmp)/"a.ass", Path(tmp)/"b.ass"
    na = lr.build_telop_ass(refined, tr_t, "ArialUnicode", pa)
    nb = A.build_telop_ass(refined, tr_t, "ArialUnicode", pb)
    eq("count", na, nb)
    eq("bytes", pa.read_bytes(), pb.read_bytes())

# ── apply_overrides
print("[apply_overrides]")
base = {"youtube_title_ja": "旧", "top_title_ja": "旧\n上", "description_ja": "説明",
        "segments": [{"index": 0, "ja": "一"}, {"index": 1, "ja": "二"}],
        "tts_cues": [{"index": 0, "ja": "ナレ"}],
        "telops": [{"index": 0, "use": True, "ja": "テロップ"}]}
ovs = [
    {"youtube_title_ja": "新", "subs": {"1": "修正", "9": "無視"}},
    {"subs": {"0": {"ja": "新一", "style": {"size": 64, "color": "#ffdd00"},
                    "start_sec": 1.5, "end_sec": 4.0}}},
    {"telops": {"0": {"style": {"rotate": -8, "y": 0.5}, "use": False}}},
    {"subs": {"0": {"use": False}}},
    {"youtube_title_ja": "  ", "subs": {"abc": "x", "0": "  "}},
]
for i, ov in enumerate(ovs):
    eq(f"ov{i}", json.dumps(lr.apply_overrides(base, ov), ensure_ascii=False, sort_keys=True),
                 json.dumps(O.apply_overrides(base, ov), ensure_ascii=False, sort_keys=True))
# 예외도 같은가
for i, ov in enumerate([{"subs": {"0": {"style": {"fontsize": 1}}}},
                        {"subs": {"0": {"style": {"y": 1.5}}}},
                        {"subs": {"0": {"start_sec": 5.0, "end_sec": 5.0}}},
                        {"tts": {"0": {"style": {"size": 40}}}},
                        {"subs": {"0": {"use": "false"}}}]):
    ea = eb = None
    try: lr.apply_overrides(base, ov)
    except Exception as e: ea = type(e).__name__
    try: O.apply_overrides(base, ov)
    except Exception as e: eb = type(e).__name__
    eq(f"raise{i}", ea, eb)

# ── E16 화면 글자 (style_texts·style_titles·editor_texts)
print("[style_texts]")
_PLAN = {"schema": "style_plan/v1",
         "texts": [{"text": "쿵!", "source_time_sec": 105.0, "duration_sec": 1.2,
                    "x": 0.7, "y": 0.25, "size": 110, "color": "#FFDD00",
                    "stroke": "dark", "fx": "pop", "rotate": -8, "font": "Jalnan"},
                   {"text": "설마…", "source_time_sec": 155.0, "duration_sec": 1.5,
                    "x": 0.3, "y": 0.66, "size": 78, "font": "Jalnan"}],
         "title_segments": [{"text": "반전 주의", "from_anchor": 150.0, "to_anchor": 160.0}],
         "images": [{"file": "style_assets/a.png", "source_time_sec": 107.0,
                     "duration_sec": 2.0, "x": 0.5, "y": 0.3, "w": 0.2}],
         "subtitle_styles": [{"source_time_sec": 106.0,
                              "style": {"size": 88, "color": "#FF4444"}}]}
_TR16 = {"style_texts": [{"index": 0, "ja": "ドンッ！"}, {"index": 1, "ja": "まさか…"}],
         "style_titles": [{"index": 0, "ja": "どんでん返し注意"}],
         "editor_texts": [{"index": 0, "ja": "エモい"}]}
_VIS = {"schema": "edit_overrides/v3",
        "texts": [{"text": "감동", "x": 0.5, "y": 0.5, "size": 72}],
        "images": [{"file": "a.png"}]}
eq("STYLE_PLAN_NAME", lr.STYLE_PLAN_NAME, ST.STYLE_PLAN_NAME)
STYLE_PLAN_NAME_ = ST.STYLE_PLAN_NAME   # 위 eq 가 두 이름이 같음을 확인한 뒤 아래 픽스처가 쓴다
eq("BACKUP_FILES", lr.BACKUP_FILES, BACKUP_FILES)
for name, plan in (("plan", _PLAN), ("none", None), ("empty", {}),
                   ("images만", {"images": [1]})):
    eq(f"style_plan_strings {name}", lr.style_plan_strings(plan), ST.style_plan_strings(plan))
for name, ov in (("v3", _VIS), ("none", None), ("images만", {"images": []})):
    eq(f"editor_text_strings {name}", lr.editor_text_strings(ov), ST.editor_text_strings(ov))
# 인덱스 순서가 뒤섞인 응답 — index 가 좌표다
_shuffled = [{"index": 1, "ja": "B"}, {"index": 0, "ja": "A"}]
eq("ja_by_index 뒤섞임", lr._ja_by_index(_shuffled, 2, "x"), ST.ja_by_index(_shuffled, 2, "x"))
for name, font in (("폰트 지정", "ArialUnicode"), ("폰트 미지정", None)):
    eq(f"apply_style_translation {name}",
       json.dumps(lr.apply_style_translation(_PLAN, _TR16, font=font),
                  ensure_ascii=False, sort_keys=True),
       json.dumps(ST.apply_style_translation(_PLAN, _TR16, font=font),
                  ensure_ascii=False, sort_keys=True))
    eq(f"apply_editor_text_translation {name}",
       json.dumps(lr.apply_editor_text_translation(_VIS, _TR16, font=font),
                  ensure_ascii=False, sort_keys=True),
       json.dumps(ST.apply_editor_text_translation(_VIS, _TR16, font=font),
                  ensure_ascii=False, sort_keys=True))
eq("연출 없음 no-op",
   json.dumps(lr.apply_style_translation({}, {}), sort_keys=True),
   json.dumps(ST.apply_style_translation({}, {}), sort_keys=True))
# 정렬 위반 — 조용히 넘어가면 다른 문구가 다른 자리에 박힌다
for i, bad in enumerate([{"style_texts": [{"index": 0, "ja": "a"}]},
                         {"style_texts": []},
                         {},
                         {"style_texts": [{"index": 0, "ja": "a"}, {"index": 5, "ja": "b"}]}]):
    ea = eb = None
    try: lr.apply_style_translation(_PLAN, bad)
    except Exception as e: ea = f"{type(e).__name__}: {e}"
    try: ST.apply_style_translation(_PLAN, bad)
    except Exception as e: eb = f"{type(e).__name__}: {e}"
    eq(f"style raise{i}", ea, eb)

# ── build_ko_ja_pairs
print("[build_ko_ja_pairs]")
with tempfile.TemporaryDirectory() as tmp:
    bk, out = Path(tmp)/"bk", Path(tmp)/"out"; bk.mkdir(); out.mkdir()
    (bk/"edit_plan.json").write_text(json.dumps({"layout": {"top_title": "원제목"}}, ensure_ascii=False))
    (bk/"subtitle_segments.json").write_text(json.dumps(
        [{"start_sec": 0.0, "end_sec": 22.0, "text": "환각"},
         {"start_sec": 30.0, "end_sec": 50.0, "text": "지정"},
         {"start_sec": 60.0, "end_sec": 61.0, "text": "삭제"}], ensure_ascii=False))
    (bk/"checkpoint_resources.json").write_text(json.dumps(
        {"tts_cue_files": [{"path": "x.mp3", "cue_index": 0,
                            "cue": {"text": "내레이션", "start_sec": 1.0, "end_sec": 5.0}}]},
        ensure_ascii=False))
    (bk/STYLE_PLAN_NAME_).write_text(json.dumps(_PLAN, ensure_ascii=False))
    (out/"onscreen_refined.json").write_text(json.dumps(
        [{"text_ko": "텔롭1", "kind": "broadcast_telop", "orig_index": 0,
          "start_sec": 3.1, "end_sec": 6.2},
         {"text_ko": "텔롭2", "kind": "broadcast_telop", "orig_index": 1,
          "start_sec": 8.0, "end_sec": 9.5}], ensure_ascii=False))
    trp = {"top_title_ja": "新題",
           "segments": [{"index": 0, "ja": "短い"},
                        {"index": 1, "ja": "指定", "start_sec": 31.0, "end_sec": 48.0,
                         "style": {"color": "#FF0000"}},
                        {"index": 2, "ja": "消", "use": False}],
           "tts_cues": [{"index": 0, "ja": "ナレ"}],
           "telops": [{"index": 0, "use": True, "ja": "T1"}, {"index": 1, "use": False}],
           **_TR16}
    eq("pairs", json.dumps(lr.build_ko_ja_pairs(bk, out, trp), ensure_ascii=False, sort_keys=True),
               json.dumps(M.build_ko_ja_pairs(bk, out, trp), ensure_ascii=False, sort_keys=True))

# ── l3_apply (job 디렉토리 파일 바이트 동일성)
print("[l3_apply]")
def make_job(root):
    job = root/"job"; job.mkdir(); bk = job/"bk"; bk.mkdir(); out = job/"out"; out.mkdir()
    (bk/"subtitle_segments.json").write_text(json.dumps(
        [{"start_sec": 0.0, "end_sec": 22.0, "text": "환각"},
         {"start_sec": 30.0, "end_sec": 31.0, "text": "둘"},
         {"start_sec": 40.0, "end_sec": 41.0, "text": "셋"}], ensure_ascii=False))
    (bk/"checkpoint_story.json").write_text(json.dumps({"variants": [{"title_text": "구"}]}))
    (bk/"edit_plan.json").write_text(json.dumps({"layout": {"top_title": "구"}}))
    (bk/"checkpoint_resources.json").write_text(json.dumps(
        {"tts_cue_files": [{"path": "x.mp3", "cue_index": 0, "cue": {"text": "내레"}}]}))
    (bk/STYLE_PLAN_NAME_).write_text(json.dumps(_PLAN, ensure_ascii=False))   # E16
    return job, bk, out
tr3 = {"top_title_ja": "新\n題",
       "segments": [{"index": 0, "ja": "短い"}, {"index": 1, "ja": "二", "use": False},
                    {"index": 2, "ja": "三", "style": {"color": "#00FF00"}}],
       "tts_cues": [{"index": 0, "ja": "ナレ"}],
       "telops": [], **_TR16}
with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
    ja, ba, oa = make_job(Path(t1)); jb, bb, ob = make_job(Path(t2))
    lr.l3_apply(ja, ba, tr3, [], {"display": "X"}, {"telop_font": "Arial"}, oa)
    A.l3_apply(jb, bb, tr3, [], {"display": "X"}, {"telop_font": "Arial"}, ob)
    for name in ("subtitle_segments.json", "checkpoint_story.json", "edit_plan.json",
                 "checkpoint_resources.json", STYLE_PLAN_NAME_):
        eq(name, (ja/name).read_bytes(), (jb/name).read_bytes())
    eq("telops.ass", (oa/"telops.ass").read_bytes(), (ob/"telops.ass").read_bytes())

print()
print("=" * 60)
print(f"불일치 {len(fails)}건" if fails else "✅ 전 항목 산출 동일 — 이식 충실")
for f in fails: print(" -", f)
sys.exit(1 if fails else 0)
