"""레퍼런스 쇼츠 해부기 — 완성본 쇼츠 한 편을 '대본 표' 한 장으로 편다.

발주(2026-08-31 세션): v3 스토리 프롬프트를 지어내지 말고 **남의 잘 된 쇼츠에서
세어서** 쓰기 위한 재료 수집기. `eb_shorts_features` 에 이미 2,714편의 영상이
Storage 에 보관돼 있는데 **ASR 이 한 번도 안 돌았다**(asr_used=0/2828) — 그래서
"0:33 에 내레이션이 개입한다"까지는 기록돼 있어도 **그 내레이션이 뭐라고 했는지가
없다**. 이 도구가 그 빈칸을 채운다.

무엇을 재는가(사용자 지시 그대로):
  - 내레이션 **한 줄 한 줄** 의 문구와 시각
  - 그 내레이션이 깔린 동안 **화면에 무엇이 있었나**(under_shot)
  - **어느 자리에** 들어갔나(entry — 무음 틈/대사 위/장면 전환 순간)
  - **다음 대사로 어떻게 이어지나**(link_to_next — 티키타카)
  - 편 전체로 내레이션이 **왜 필요했나 / 왜 없어도 됐나**(닫힌 라벨 + 근거)
  - **마지막 3초를 어떻게 끝내나**

설계 규율 — 왜 라벨을 닫아 두는가:
  이 레포의 `eb_shorts_features.*.note` 가 반면교사다. 모델에게 자유 서술을 시켰더니
  "서사의 비약을 매끄럽게 연결하고 시청자의 이해를 돕는다" 같은 문장만 2,828편치가
  쌓였다 — 읽을 수는 있어도 **셀 수가 없다**. 그래서 여기서는
    ① 기계가 잴 수 있는 것은 **코드가 재서 모델에게 건넨다**(간격·컷 수·무음·화자 수).
       모델이 숫자를 지어낼 자리를 없앤다.
    ② 판단은 **닫힌 라벨 집합 중 택1** 로만 받는다.
    ③ 편 단위 판정은 **근거(evidence)에 ①의 숫자를 인용해야** 통과한다.
  라벨 집합 자체는 아직 가설이다 — 첫 배치(20편) 후 `report --labels` 로 미사용
  라벨과 `other` 쏠림을 보고 확정한다. 처음부터 맞다고 가정하지 않는다.

장르로 표집하지 않는 이유(사용자 지시 2026-08-31):
  같은 장르라도 분위기가 갈려서 장르는 축이 아닐 수 있다. 그래서 **성과(good/bad)만
  나누고 나머지는 무작위**로 뽑되 장르는 **기록만** 한다 — 무엇이 진짜 축인지는
  report 가 말하게 한다(가설: 축은 장르가 아니라 '떨어진 원본 장면을 몇 개 이었나').

사용:
  python -m scripts.shorts_anatomy sample --n 20 --out work/anatomy
  python -m scripts.shorts_anatomy add-urls --urls-file links.txt   # 사용자 선정 링크
  python -m scripts.shorts_anatomy fetch    --dir work/anatomy
  python -m scripts.shorts_anatomy anatomize --dir work/anatomy
  python -m scripts.shorts_anatomy report   --dir work/anatomy

필요 env:
  · GEMINI_API_KEY — anatomize (필수)
  · SUPABASE_URL · SUPABASE_SERVICE_KEY — sample 과 Storage 내려받기에만 필요.
    **유튜브 링크만 쓸 거면 없어도 된다**(add-urls → fetch → anatomize → report 가
    DB 없이 돈다). 있으면 add-urls 가 코퍼스 조회로 성과·장르를 얹어 준다.
없으면 즉시 실패한다 — 조용한 폴백 금지(app/replay/fetch.py 와 같은 규약).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.ffmpeg_utils import (  # noqa: E402
    ensure_ffmpeg_supported,
    find_ffmpeg_command,
)
from app.v3.audio import detect_silence_intervals  # noqa: E402
from app.v3.scenecut import detect_scene_cuts  # noqa: E402
from app.v3.timegrid import group_words_to_cues  # noqa: E402
from app.v3.transcribe import transcribe_words  # noqa: E402

STORAGE_BUCKET = "laeebly-shorts-video"
AT_CUT_TOL_SEC = 0.35        # 발화 시작이 장면 전환에서 이 안이면 '전환 순간'
# ⚠ 쇼츠는 **이미 편집된 완성본**이라 원본용 격자 임계(0.3)로는 컷을 놓친다.
#   실측(2026-08-31, 받은 20편 중 2편을 코퍼스 cut_count 와 대조):
#     -NzIOnA1Lzs (DB 6컷): 0.3→0 · 0.2→7 · 0.15→14
#     4zPG94guwoY (DB 24컷): 0.3→10 · 0.2→18 · 0.15→26
#   0.2 를 쓴다. app/v3/scenecut.SCENE_THRESHOLD(0.3)는 **건드리지 않는다** — 그건
#   원본 격자의 계약이고 여기와 용도가 다르다.
SHORTS_SCENE_THRESHOLD = 0.2

# ── 닫힌 라벨 집합 (가설 — 첫 배치 후 확정) ─────────────────────────────────
UNDER_SHOT = ["speaking_muted", "reaction", "action_no_speech", "establishing",
              "insert", "montage", "still_freeze", "at_scene_jump", "other"]
ENTRY = ["into_silence", "over_dialogue", "at_cut", "at_open", "at_close"]
LINK_NEXT = ["setup_then_line", "question_then_answer", "bridge_jump",
             "label_then_proof", "contrast", "summary_then_new", "none"]
WHY_NEEDED = ["context_missing", "time_jump", "character_intro",
              "dialogue_sparse", "hook_frontload", "pace_compression"]
WHY_NOT = ["ensemble_banter", "fast_dialogue", "single_beat",
           "visual_self_evident", "onscreen_text_carries"]
END_TYPE = ["line_cut", "reaction_hold", "narration_close",
            "cliffhanger_question", "fade"]
# 효과음 — 라벨은 코퍼스 audio_design 태그 어휘에 근거한다(지어내지 않았다).
# 실측 lift: 효과음활용 1.42 · 웃음소리 1.45 · BGM최소화 1.32 · 볼륨조절 1.72.
SFX_KIND = ["impact", "whoosh", "comic_boing", "laugh_track", "sting", "riser",
            "silence_drop", "bgm_start", "bgm_stop", "bgm_change",
            "original_diegetic", "unclear"]
SFX_ADDED = ["yes", "no", "unclear"]

# 화면 보조 텍스트 — 역할 이름은 레포 어휘에 맞춘다(지어내지 않는다):
#   state_paren·meme_tsukkomi·wordplay 는 style_tone._CATEGORY_TEXT 의 라벨 분류,
#   title_fixed·title_segment 는 E21 제목 계약, broadcast_telop 은 L2 텔롭 분류.
#   그래야 여기서 센 것이 v3 프롬프트가 쓸 수 있는 어휘로 바로 옮겨진다.
TEXT_ROLE = ["title_fixed", "title_segment", "subtitle", "narration_caption",
             "person_label", "action_note", "state_paren", "meme_tsukkomi",
             "wordplay", "emphasis_word", "context_note", "source_credit",
             "broadcast_telop", "cta", "other"]
# 대사·내레이션 자막은 '보조 텍스트'가 아니다 — 편집자가 **얹은 설명 레이어**만
# 따로 센다(사용자 지시 2026-08-31: "대사 자막 말고, 사람에 대한 라벨이나 행동에
# 대한 설명"). report 가 이 집합으로 자막과 갈라 찍는다.
AUX_ROLES = ["person_label", "action_note", "state_paren", "meme_tsukkomi",
             "wordplay", "emphasis_word", "context_note"]
TEXT_POS = ["top", "upper_third", "center", "lower_third", "bottom", "side"]
TEXT_ORIGIN = ["added", "burned_in", "unclear"]

# 내레이션 어미 분류 — **문구 자체를 세기 위한 것**. 라벨 집합과 같은 규율로
# 순서대로 먼저 맞는 것을 쓰고, 아무것도 안 맞으면 '기타'로 남겨 **원문을 보여준다**
# (기타가 많으면 분류가 현실을 못 덮는다는 신호 — 늘려야 한다).
ENDING_PATTERNS = [
    # ⚠ 쉼표 종결이 맨 앞이다 — 이 문법의 고유 장치라 다른 어미와 섞으면 안 된다.
    #   실측(2026-08-31, 드라마 176줄): 18%가 여기였는데 칸이 없어 '기타'로 빠졌다.
    #   "불륜녀를 발견한 김혜수," → 다음 줄 "그 와중에 불륜녀 챙기는 남편,"
    #   한 줄로 끝내지 않고 **끊어서 다음 줄이 받게** 하는 배치다.
    ("쉼표 종결", r"[,、]$"),
    ("의문", r"\?$|(까|나요|을까|ㄹ까|건가|는가)$"),
    ("연결형", r"(는데|은데|ㄴ데|다가|면서|지만|거든|어서|아서|니까)$"),
    ("해요체", r"(에요|예요|어요|아요|해요|죠|워요|세요)$"),
    ("~다체", r"(었다|았다|한다|된다|이다|난다|든다|는다|다)$"),
    ("반말·축약", r"(네|함|임|음|중|짝|컷)$"),
    ("명사형·체언", r"[가-힣]$"),
]


def classify_ending(text: str) -> str:
    """문구의 종결 유형. 순서대로 먼저 맞는 것 — 못 맞히면 '기타'."""
    t = (text or "").strip()
    t = re.sub(r"[.\u2026!~\s]+$", "", t)
    for name, pat in ENDING_PATTERNS:
        if re.search(pat, t):
            return name
    return "기타"


# ── 근거 계약 ───────────────────────────────────────────────────────────────
# ⚠ 왜 자유 서술을 안 받는가(2026-08-31 실측): 첫 판은 evidence 가 **빈 문자열이
#   아닌지만** 봤다. 그랬더니 `context_missing` 에 `"duration_sec 53.13"` 이 붙어
#   통과했다 — **값은 진짜인데 주장과 무관하다**. 정확성이 아니라 **관련성**이
#   문제라, 라벨마다 **댈 수 있는 근거 항목을 못박는다**. 목록 밖 항목을 대면 거절.
EVIDENCE_FIELDS = {
    # 내레이션이 필요했다
    "context_missing":     ["speakers", "first_speaker_at_sec", "scene_cuts"],
    "time_jump":           ["source_scenes", "scene_cuts"],
    "character_intro":     ["speakers", "first_speaker_at_sec"],
    "dialogue_sparse":     ["silence_ratio", "max_silence_sec", "dialogue_ratio"],
    "hook_frontload":      ["first_utterance_sec", "opens_with_narration"],
    "pace_compression":    ["source_scenes", "speech_ratio", "narration_ratio"],
    # 없어도 됐다
    "ensemble_banter":     ["speakers", "turns_per_10s"],
    "fast_dialogue":       ["speech_ratio", "max_silence_sec", "turns_per_10s"],
    "single_beat":         ["source_scenes", "scene_cuts", "duration_sec"],
    "visual_self_evident": ["speech_ratio", "silence_ratio", "cuts_per_10s"],
    "onscreen_text_carries": ["speech_ratio", "dialogue_ratio"],
}

# 라벨이 성립하려면 **측정값 자체가** 만족해야 하는 조건. 모델이 값을 옳게 인용해도
# 그 값이 주장과 반대면 거절한다(인용은 맞고 결론이 틀린 경우를 잡는다).
EVIDENCE_RULES = {
    "ensemble_banter":     lambda f: f["speakers"] >= 3,
    "fast_dialogue":       lambda f: f["max_silence_sec"] <= 1.5,
    "dialogue_sparse":     lambda f: f["silence_ratio"] >= 0.15 or f["max_silence_sec"] >= 2.0,
    "single_beat":         lambda f: (f.get("source_scenes") or 99) <= 1,
    "time_jump":           lambda f: (f.get("source_scenes") or 0) >= 2,
    "visual_self_evident": lambda f: f["speech_ratio"] <= 0.5,
    "hook_frontload":      lambda f: f["first_utterance_sec"] <= 3.0,
    "character_intro":     lambda f: f["speakers"] >= 1,
}
MAX_REASK = 2              # 반려·재질의 상한 — v3 파이프라인과 같은 규약


# ── Supabase (표준 라이브러리만 — 이 레포에 DB 드라이버가 없다) ─────────────
def _env() -> tuple[str, str]:
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_KEY 필요 — "
                         "조용한 폴백 금지(app/replay/fetch.py 와 같은 규약)")
    return url.rstrip("/"), key


def _get(path: str, params: dict[str, str]) -> Any:
    url, key = _env()
    full = f"{url}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _download(storage_path: str, dest: Path) -> int:
    """Storage 객체 1개 → 로컬 파일. 반환 바이트 수."""
    url, key = _env()
    full = f"{url}/storage/v1/object/{urllib.parse.quote(storage_path)}"
    req = urllib.request.Request(full, headers={
        "apikey": key, "Authorization": f"Bearer {key}"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=600) as r, dest.open("wb") as f:
        n = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            n += len(chunk)
    return n


# ── sample ─────────────────────────────────────────────────────────────────
def cmd_sample(args: argparse.Namespace) -> int:
    """성과만 층화하고 장르는 무시한 무작위 표집.

    good 6 : bad 4 로 뽑는다 — bad 를 섞어야 '잘 된 것의 특징'이 아니라
    **가르는 것**이 나온다(good 만 보면 모든 편에 공통인 것도 비결로 보인다)."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    n_good = round(args.n * 0.6)
    picks: list[dict] = []
    for label, k in (("good", n_good), ("bad", args.n - n_good)):
        # ⚠ 후보 **전량**을 받아 와서 뽑는다. 서버에서 limit 으로 잘라 오면 그건
        #   '모집단의 무작위 표본'이 아니라 'PostgREST 가 먼저 준 N개 중 무작위'다
        #   (order 없는 응답 순서는 보장도 없다). 후보는 수천 행이라 한 번에 받는다.
        rows = _get("/rest/v1/eb_shorts_features", {
            "select": "shorts_id,storage_path,duration_sec,perf_label,video_type,"
                      "genre,onscreen_title_text,avg_view_percentage,cut_count",
            "perf_label": f"eq.{label}",
            "storage_path": "not.is.null",
            "duration_sec": f"gte.{args.min_sec}",
            "order": "shorts_id.asc",
            "limit": "5000",
        })
        # 결정적 무작위 — shorts_id 해시 순(같은 인자면 같은 표본, 재현 가능)
        rows.sort(key=lambda r: hashlib.md5(r["shorts_id"].encode()).hexdigest())
        if len(rows) < k:
            raise SystemExit(f"{label} 후보가 {len(rows)}편뿐 — n 을 줄여라")
        print(f"[sample] {label} 후보 {len(rows)}편 중 {k}편")
        picks.extend(rows[:k])

    (out / "worklist.json").write_text(
        json.dumps({"bucket": STORAGE_BUCKET, "items": picks},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    g = sum(1 for p in picks if p["perf_label"] == "good")
    print(f"[sample] {len(picks)}편 (good {g} · bad {len(picks)-g}) → {out/'worklist.json'}")
    print("[sample] 장르 분포(기록만 — 표집 기준 아님): " + ", ".join(
        f"{k or '?'}:{v}" for k, v in Counter(
            p.get("video_type") for p in picks).most_common()))
    return 0


# ── add-urls ───────────────────────────────────────────────────────────────
def _load_worklist(d: Path) -> dict:
    p = d / "worklist.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"bucket": STORAGE_BUCKET, "items": []}


def _save_worklist(d: Path, wl: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "worklist.json").write_text(
        json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_add_urls(args: argparse.Namespace) -> int:
    """사용자가 고른 유튜브 링크를 작업 목록에 넣는다.

    표집(sample)이 뽑는 것은 '성과가 좋았던 편'이지 사용자가 **"이렇게 만들고 싶다"**
    고 생각하는 편이 아니다 — 그건 사람이 직접 줘야 한다. 그래서 이 경로는 표집과
    **별개 그룹**(perf_label='user')으로 들어가고 report 도 따로 찍는다.

    DB 조회는 **선택**이다(env 없으면 건너뛴다) — 이 명령과 fetch·anatomize 만으로
    Supabase 없이 유튜브 링크만 가지고 끝까지 돌 수 있다."""
    from app.modules.youtube_downloader import video_id_of

    urls: list[str] = list(args.url or [])
    if args.urls_file:
        urls += [ln.strip() for ln in Path(args.urls_file).read_text(
            encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    if not urls:
        raise SystemExit("--url 또는 --urls-file 로 링크를 줘라")

    d = Path(args.dir)
    wl = _load_worklist(d)
    have = {it["shorts_id"] for it in wl["items"]}
    added = dup = 0
    for u in urls:
        vid = video_id_of(u)
        if vid in have:
            dup += 1
            continue
        item = {"shorts_id": vid, "source": "youtube", "url": u,
                "perf_label": "user", "storage_path": None}
        # 이미 코퍼스에 있으면 성과·장르를 얹는다(있으면 좋고 없어도 그만)
        try:
            rows = _get("/rest/v1/eb_shorts_features", {
                "select": "perf_label,video_type,genre,duration_sec,cut_count,"
                          "onscreen_title_text,avg_view_percentage,storage_path",
                "shorts_id": f"eq.{vid}", "limit": "1"})
            if rows:
                item["in_corpus"] = True
                item["corpus_perf_label"] = rows[0].get("perf_label")
                for k in ("video_type", "genre", "duration_sec", "cut_count",
                          "onscreen_title_text", "avg_view_percentage"):
                    item[k] = rows[0].get(k)
        except SystemExit:
            pass          # SUPABASE env 없음 — 유튜브만으로 간다
        except Exception as e:  # noqa: BLE001 — 조회 실패가 링크 등록을 막지 않는다
            print(f"  · {vid} 코퍼스 조회 실패({type(e).__name__}) — 링크만 등록")
        wl["items"].append(item)
        have.add(vid)
        added += 1
        mark = " [코퍼스에 있음]" if item.get("in_corpus") else ""
        print(f"  + {vid}{mark}  {u}")
    _save_worklist(d, wl)
    print(f"[add-urls] 추가 {added} · 중복 {dup} · 목록 총 {len(wl['items'])}편")
    return 0


def cmd_add_local(args: argparse.Namespace) -> int:
    """로컬 mp4 를 작업 목록에 넣는다 — **우리 산출물을 같은 자로 재기 위한 경로**.

    레퍼런스 실측값은 반려 게이트가 아니라 계기판이다(2026-08-31 결정): 우리 완성본을
    같은 해부기에 통과시켜 레퍼런스 분포와 대조하고, 어긋나면 산출을 거절하는 게 아니라
    프롬프트를 고친다. 그러려면 우리 mp4 가 이 도구에 들어와야 한다.

    perf_label='ours' 로 들어가 report 에서 레퍼런스와 나란히 찍힌다."""
    import shutil

    paths: list[Path] = []
    for pat in (args.path or []):
        pp = Path(pat)
        paths += sorted(pp.glob("*.mp4")) if pp.is_dir() else [pp]
    if not paths:
        raise SystemExit("--path 로 mp4 파일이나 디렉토리를 줘라")

    d = Path(args.dir)
    wl = _load_worklist(d)
    have = {it["shorts_id"] for it in wl["items"]}
    vids = d / "video"
    vids.mkdir(parents=True, exist_ok=True)
    added = dup = 0
    for f in paths:
        if not f.is_file():
            print(f"  ✗ 파일 없음: {f}")
            continue
        # 우리 산출은 전부 shorts.mp4 라 파일명이 겹친다 — 잡 디렉토리 이름을 id 로
        sid = args.prefix + (f.parent.name if f.stem in ("shorts", "output", "final")
                             else f.stem)
        if sid in have:
            dup += 1
            continue
        shutil.copy2(f, vids / f"{sid}.mp4")
        wl["items"].append({"shorts_id": sid, "source": "local", "perf_label": "ours",
                            "src_path": str(f), "storage_path": None})
        have.add(sid)
        added += 1
        print(f"  + {sid}  ← {f}")
    _save_worklist(d, wl)
    print(f"[add-local] 추가 {added} · 중복 {dup} · 목록 총 {len(wl['items'])}편")
    return 0


# ── fetch ──────────────────────────────────────────────────────────────────
def _fetch_youtube(url: str, vid: str, d: Path, dest: Path) -> float:
    """기존 다운로더 재사용 — 403 회피 옵션·.part 이어받기 금지가 거기 박혀 있다
    (2026-08-18 실측). 여기서 yt-dlp 옵션을 새로 발명하면 그 지식을 잃는다."""
    import shutil

    from app.modules.youtube_downloader import download_youtube_assets

    src_dir = d / "ytsrc" / vid
    assets = download_youtube_assets(url, src_dir)
    src = Path(getattr(assets, "video_path", src_dir / "source.mp4"))
    if not src.is_file():
        raise RuntimeError(f"다운로드 산출물 없음: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest.stat().st_size / 1e6


def cmd_fetch(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    wl = json.loads((d / "worklist.json").read_text(encoding="utf-8"))
    vids = d / "video"
    ok = skip = fail = 0
    for it in wl["items"]:
        sid = it["shorts_id"]
        dest = vids / f"{sid}.mp4"
        if dest.exists() and dest.stat().st_size > 0:
            skip += 1
            continue
        try:
            if it.get("source") == "local":
                print(f"  · {sid} 로컬(이미 복사됨)")
                skip += 1
                continue
            if it.get("source") == "youtube":
                mb = _fetch_youtube(it["url"], sid, d, dest)
                print(f"  ↓ {sid} {mb:.1f}MB (youtube)")
            else:
                mb = _download(it["storage_path"], dest) / 1e6
                print(f"  ↓ {sid} {mb:.1f}MB (storage)")
            ok += 1
        except Exception as e:  # noqa: BLE001 — 편별 실패는 건너뛰되 건수를 남긴다
            print(f"  ✗ {sid}: {type(e).__name__}: {e}")
            fail += 1
    print(f"[fetch] 받음 {ok} · 이미 있음 {skip} · 실패 {fail} → {vids}")
    return 0 if fail == 0 else 1


# ── 기계 측정 ───────────────────────────────────────────────────────────────
def extract_audio(video: Path, dest: Path) -> None:
    ffmpeg = find_ffmpeg_command("ffmpeg")
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", str(dest)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"오디오 추출 실패: {proc.stderr[-300:]}")


def probe_duration(video: Path) -> float:
    ffprobe = find_ffmpeg_command("ffprobe")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 실패: {proc.stderr[-200:]}")
    return float(proc.stdout.strip())


def group_utterances(words: list[dict]) -> list[dict]:
    """단어 → 발화(cue). 묶음 규칙은 `stt_elevenlabs.words_to_segments` 정본을 탄다.

    ⚠ 처음엔 '0.4s 이상 벌어지면 끊기'만 썼다가 실측에서 깨졌다(2026-08-31,
    -NzIOnA1Lzs): **내레이션은 단어 사이가 벌어지지 않아** 30초가 한 덩어리로 붙었다
    (79단어 → 발화 2건). "한 줄 한 줄"을 보려는 도구가 30초를 한 줄로 내면 무의미하다.
    레포에 이미 답이 있었다 — 0.5s 공백·문장 종결부호·44자·6.0s 로 끊는 정본 묶음기.
    같은 규칙을 두 곳에서 따로 구현하지 않는다(E15 규율)."""
    cues = group_words_to_cues(words)
    return [{"t0": round(c["t_in"], 2), "t1": round(c["t_out"], 2),
             "text": " ".join(c["text"].split())} for c in cues]


def detect_sfx_candidates(pcm, duration: float, utts: list[dict],
                          *, max_events: int = 24) -> list[dict]:
    """**말이 아닌 소리 사건**의 시각을 찾는다 — 효과음·웃음·스팅·BGM 전환 후보.

    whisper 는 말만 받아 적으므로 효과음은 표에서 통째로 빠진다. 그런데 편집 문법의
    일부다(코퍼스 실측: 효과음활용 lift 1.42 · 웃음소리 1.45 · BGM최소화 1.32).

    여기서 하는 일은 **어디인지 찾는 것까지**다 — 무엇인지(라벨)는 모델이 붙인다.
    설계 규율 그대로: 시각은 기계가, 이름은 모델이.

    방법: 20ms RMS 포락선에서 국소 중앙값 대비 급상승 지점을 잡되, **발화 구간은
    제외**한다(말소리도 에너지가 크다). 여기서 잡히는 것은 어디까지나 후보이고,
    ⚠ 이것이 **원본에 없던 소리인지**는 이 함수가 판정하지 못한다 — 원본 대조가
    필요하고(has_source_video 편만 가능) 그건 별건이다."""
    import numpy as np

    sr = 16000
    hop = int(0.02 * sr)
    n = len(pcm) // hop
    if n < 10:
        return []
    rms = np.sqrt(np.maximum(
        (pcm[:n * hop].reshape(n, hop) ** 2).mean(axis=1), 1e-12))
    t = np.arange(n) * 0.02
    # 국소 기준선 — 앞뒤 1초 중앙값(전역 평균은 조용한 편에서 오검출한다)
    w = 50
    pad = np.pad(rms, w, mode="edge")
    base = np.array([np.median(pad[i:i + 2 * w + 1]) for i in range(n)])
    jump = rms / np.maximum(base, 1e-6)

    speech = np.zeros(n, dtype=bool)
    for u in utts:                      # 발화 ±0.15s 는 제외(말소리도 세다)
        speech[max(0, int((u["t0"] - 0.15) / 0.02)):
               min(n, int((u["t1"] + 0.15) / 0.02) + 1)] = True

    hits = np.where((jump > 3.0) & (rms > 0.02) & ~speech)[0]
    events: list[dict] = []
    for i in hits:
        if events and t[i] - events[-1]["t"] < 0.25:
            events[-1]["strength"] = max(events[-1]["strength"], round(float(jump[i]), 1))
            continue
        events.append({"t": round(float(t[i]), 2),
                       "strength": round(float(jump[i]), 1)})
    events.sort(key=lambda e: -e["strength"])
    return sorted(events[:max_events], key=lambda e: e["t"])


def measure(utts: list[dict], cuts: list[float], silences: list[tuple[float, float]],
            duration: float) -> dict:
    """모델에게 **건네줄** 숫자. 여기 있는 값을 모델이 다시 계산하지 않게 한다."""
    for i, u in enumerate(utts):
        u["i"] = i
        u["gap_before"] = round(u["t0"] - (utts[i - 1]["t1"] if i else 0.0), 2)
        u["gap_after"] = round((utts[i + 1]["t0"] if i + 1 < len(utts)
                                else duration) - u["t1"], 2)
        u["cuts_within"] = sum(1 for c in cuts if u["t0"] <= c <= u["t1"])
        u["at_cut"] = any(abs(c - u["t0"]) <= AT_CUT_TOL_SEC for c in cuts)
    speech = sum(u["t1"] - u["t0"] for u in utts)
    sil = sum(b - a for a, b in silences)
    return {
        "duration_sec": round(duration, 2),
        "utterance_count": len(utts),
        "speech_ratio": round(speech / duration, 3) if duration else 0.0,
        "silence_ratio": round(sil / duration, 3) if duration else 0.0,
        "max_silence_sec": round(max((b - a for a, b in silences), default=0.0), 2),
        "scene_cuts": len(cuts),
        "cuts_per_10s": round(len(cuts) / duration * 10, 2) if duration else 0.0,
        "last_3s_has_speech": any(u["t1"] > duration - 3.0 for u in utts),
    }


# ── 모델 판정 ───────────────────────────────────────────────────────────────
ANATOMY_PROMPT = """이 쇼츠 완성본을 **대본 표 한 장**으로 편다. 아래 [단어 목록] 은 전사로 이미 뽑아 두었다. 네가 할 일은 (1) 단어들을 **말하는 사람이 바뀌는 지점에서** 줄로 끊고 (2) 각 줄이 내레이션인지 원본 대사인지 가르고 (3) 내레이션 줄마다 화면·자리·다음 대사와의 관계를 라벨링하고 (4) 편 전체 판정을 근거와 함께 내는 것이다.

## 규칙 (가장 중요)
- **시각(초)을 쓰지 마라.** 줄의 경계는 단어 번호 `w0`~`w1` 로만 말한다 — 시각은 엔진이 단어에서 찾는다. 네가 초를 적어도 무시된다.
- **모든 단어가 정확히 한 줄에** 들어가야 한다. 0번부터 마지막까지 빠짐·겹침·건너뜀 없이 이어져야 한다.
- **화자가 바뀌면 반드시 끊어라.** 한 줄 안에 내레이션과 원본 대사가 섞이면 그 편은 폐기된다. 전사에는 화자 표시가 없으니 **영상의 소리로 판단하라** — 목소리·잔향·현장음이 바뀌는 지점이 경계다.
- 라벨은 **아래 목록에 있는 값만** 쓴다. 목록 밖 값을 지어내면 그 편은 폐기된다.
- 편 단위 판정의 `evidence` 는 **[측정값] 절의 숫자를 인용**해야 한다. 새 숫자를 계산하지 마라 — 이미 재 두었다.
- 확신이 없으면 `unclear`(발화 종류) 또는 `other`(under_shot)를 쓴다. 억지로 채우지 마라.

## 발화 종류 (kind) — **출처를 가르는 것이지 말투를 가르는 것이 아니다**
질문은 하나뿐이다: **이 소리가 원본 영상에 있었나, 쇼츠 편집자가 나중에 얹었나.**
- `narration` — **원본에 없던 목소리.** 편집자가 얹은 TTS·성우 해설. 스튜디오 녹음이라
  잔향·현장음이 없고, 화면 속 누구의 입도 움직이지 않는다.
- `dialogue` — **원본 영상에 있던 소리.** 등장인물 대사, 인터뷰, 리포터·MC·내레이터의 말,
  방청객 반응 — 원본에 있었으면 전부 여기다.
- `unclear` — 못 가르겠다.

⚠ **말투로 속지 마라**(실측 오분류 2026-08-31): 원본 속 리포터·MC·진행자는 카메라를 보고
  시청자에게 설명하는 말투를 쓴다("여기 보이시는 것처럼 한 끼 가격이 삼천 원인데요"). 이건
  설명체지만 **원본 오디오이므로 dialogue** 다. 실제로 이 문장이 narration 으로 잘못
  분류됐다 — 코미디 코너에서 리포터 역을 맡은 출연자의 대사였다.
  가르는 기준 셋: ① 화면 속 인물이 그 말을 하고 있나(입·몸짓·현장 반응) ② 현장 잔향·
  배경음이 함께 들리나 ③ 다른 인물이 그 말에 반응하나 — 하나라도 예면 dialogue 다.
  얹은 목소리는 그 편에서 **혼자 일관된 톤**으로 여러 번 나오고 아무도 반응하지 않는다.

speaker: 누구 목소리인지 짧게. 얹은 목소리는 "TTS남"/"TTS여", 원본은 인물명이나 역할
("리포터", "손님", "할머니", "불명"). 같은 사람은 편 내내 같은 표기로.

## 내레이션 줄에만 채우는 것
under_shot(내레이션이 깔린 **동안의 화면**): {under_shot}
  · speaking_muted=인물이 말하는 중인데 원본 소리를 죽였다 · reaction=듣는 얼굴
  · action_no_speech=대사 없는 행동 · establishing=장소·상황 보여주기
  · insert=소품·문자 클로즈업 · montage=빠른 몽타주 · still_freeze=정지
  · at_scene_jump=장면이 바뀌는 지점 위
entry(들어온 자리): {entry}
link_to_next(**다음 대사와의 관계**): {link_next}
  · setup_then_line=상황을 깔고 다음 대사가 터뜨린다 · question_then_answer=물음→답
  · bridge_jump=시간·장소 비약을 메운다 · label_then_proof=정체를 규정하고 대사가 증명
  · contrast=다음 대사가 반대 · summary_then_new=앞을 요약하고 새 국면 · none=안 이어짐

## 편 단위
verdict: needed | not_needed | overused | underused
why(택1~2): 내레이션이 있는 편이면 {why_needed} 중에서, 없는 편이면 {why_not} 중에서.
**evidence 는 근거 항목 **이름의 목록**이다 — 숫자는 쓰지 마라(엔진이 채운다).** 라벨마다 댈 수 있는 항목이 정해져 있고, 목록 밖 이름을 대면 거절된다(예: `context_missing` 에 `duration_sec` 은 못 쓴다). 그리고 **엔진이 잰 값이 그 주장을 지지하지 않으면** 역시 거절된다 — 라벨을 고르기 전에 [측정값]·[도출값]을 먼저 보라.
{evidence_fields}
  · context_missing=그냥 보면 누가 누군지·무슨 상황인지 모른다 · time_jump=구간 사이 비약
  · character_intro=인물 정체 규정 필요 · dialogue_sparse=대사가 비어 오디오가 빈다
  · hook_frontload=첫 3초에 판돈을 걸어야 한다 · pace_compression=압축하느라 생긴 구멍
  · ensemble_banter=여럿의 대화 자체가 재미 · fast_dialogue=대사가 촘촘해 틈이 없다
  · single_beat=원본 장면이 짧고 단일 사건 · visual_self_evident=화면만으로 읽힌다
  · onscreen_text_carries=자막·텔롭이 맥락을 대신한다
end_type: {end_type}
source_scenes: 이 쇼츠가 원본에서 **몇 개의 떨어진 장면**을 가져다 이었는가(정수 추정).
  같은 장면이 계속 이어지면 1. 시간·장소가 건너뛰면 그때마다 +1.

## [측정값] — 이미 재 두었다. 인용만 하라
{measured}

## [도출값] — 네가 붙일 줄 라벨에서 나온다. 판정과 어긋나면 거절된다
speakers(원본 화자 수) · turns_per_10s · narration_ratio · dialogue_ratio ·
first_utterance_sec · first_speaker_at_sec · opens_with_narration · source_scenes

## 효과음 (sfx)
아래 [소리 사건 후보] 는 **말이 아닌** 에너지 급상승 지점이다(기계 검출 — 어디인지만 안다).
각 후보가 무엇인지 라벨을 붙여라. 후보가 아닌 것도 들렸으면 추가해도 된다.
kind: {sfx_kind}
  · impact=타격·쿵 강조 · whoosh=전환음 · comic_boing=예능 뿅/띠용 · laugh_track=웃음·방청객
  · sting=긴장·충격 스팅 · riser=고조 · silence_drop=소리를 뚝 끊어 강조
  · bgm_start/stop/change=배경음악 시작·정지·전환 · original_diegetic=원본에 있던 현장음
added(**원본에 없던 것을 편집으로 얹었나**): {sfx_added}
  · 드라마·영화 원본에 없을 소리(예능 뿅·웃음소리·whoosh)는 yes.
  · 장면 안에서 실제로 난 소리(문 닫힘·유리 깨짐)는 no. 애매하면 unclear — 억지로 고르지 마라.
note: 그 소리가 **무엇을 강조하는지** 한 구절(예: "반전 대사 직전 정적").

## 화면 보조 텍스트 (screen_text)
화면에 **글자로** 나온 것을 전부 적는다(대사 자막 포함 — 역할로 구분한다).
role: {text_role}
  · title_fixed=편 내내 고정된 윗줄 제목 · title_segment=구간마다 바뀌는 제목
  · subtitle=대사 자막 · narration_caption=내레이션 자막
  · **person_label=인물이 누구인지**(이름·관계·정체·직함: "손자" "3년차 신입" "전직 국정원")
  · **action_note=지금 무슨 일이 벌어지는지**("(팩폭 시전)" "몰래 훔쳐보는 중")
  · state_paren=인물의 심리·상태("(놀람)" "(꿋꿋함)" "(두리번 두리번)")
  · meme_tsukkomi=편집자 드립·밈 훈수("웃참 2222" "난가???")
  · wordplay=의성어·추임새("똑 똑") · emphasis_word=강조 단어 팝업
  · context_note=상황 보조("3년 전" "회의실")
  · source_credit=출처·프로그램명 · broadcast_telop=원본에 박힌 방송 텔롭
  · cta=구독·좋아요 유도
pos(화면 위치): {text_pos}
origin: {text_origin} — **added=쇼츠 편집으로 얹은 것 / burned_in=원본 영상에 이미 있던 것.**
  대사 자막·상태 라벨·밈은 보통 added, 방송 텔롭·원본 자막은 burned_in.
purpose: 무엇을 하려고 쓴 글자인지 한 구절(예: "누가 말하는지 알려줌", "반응을 대신 말해줌").

## [소리 사건 후보] t | 세기(국소 중앙값 대비 배수)
{sfx_candidates}

## [장면 전환 시각]
{cut_times}

## [단어 목록] 번호 | 시각 | 낱말   ← 줄 경계는 이 번호로만 말한다
{words}

## 출력 (JSON 만)
{{"lines": [{{"w0": 0, "w1": 6, "kind": "narration", "speaker": "TTS남",
             "under_shot": "...", "entry": "...", "link_to_next": "..."}},
            {{"w0": 7, "w1": 22, "kind": "dialogue", "speaker": "이재성"}}],
  "verdict": "needed",
  "why": [{{"label": "time_jump", "evidence": ["source_scenes", "scene_cuts"]}}],
  "sfx": [{{"t": 12.4, "kind": "comic_boing", "added": "yes", "note": "반응 얼굴 위"}}],
  "screen_text": [{{"t0": 0.0, "t1": 53.1, "text": "콩쿠르 1등만 하던 손자",
                   "role": "title_fixed", "pos": "top", "origin": "added",
                   "purpose": "편 전체의 상황을 한 줄로 고정"}}],
  "bgm": "none | quiet | prominent",
  "end_type": "line_cut", "ends_mid_sentence": false, "source_scenes": 3,
  "one_line": "이 편이 무엇을 어떻게 했는지 한 문장(20자 내외)"}}"""


def build_prompt(measured: dict, words: list[dict], cuts: list[float],
                 sfx: list[dict] | None = None) -> str:
    word_rows = "\n".join(f"{i} | {w['t0']:.2f} | {w['text'].strip()}"
                          for i, w in enumerate(words))
    sfx_rows = "\n".join(f"{e['t']:.2f} | x{e['strength']}" for e in (sfx or [])) \
        or "(검출 없음 — 그래도 들리는 게 있으면 적어라)"
    return ANATOMY_PROMPT.format(
        sfx_kind=" | ".join(SFX_KIND), sfx_added=" | ".join(SFX_ADDED),
        sfx_candidates=sfx_rows,
        cut_times=", ".join(f"{c:.2f}" for c in cuts) or "(검출 없음)",
        text_role=" | ".join(TEXT_ROLE), text_pos=" | ".join(TEXT_POS),
        text_origin=" | ".join(TEXT_ORIGIN),
        evidence_fields="\n".join(
            f"  · {k}: {' | '.join(v)}" for k, v in EVIDENCE_FIELDS.items()),
        under_shot=" | ".join(UNDER_SHOT), entry=" | ".join(ENTRY),
        link_next=" | ".join(LINK_NEXT), why_needed=" | ".join(WHY_NEEDED),
        why_not=" | ".join(WHY_NOT), end_type=" | ".join(END_TYPE),
        measured=json.dumps(measured, ensure_ascii=False, indent=1),
        words=word_rows or "(발화 없음)")


def validate(resp: dict, words: list[dict]) -> None:
    """라벨 밖 값·근거 없는 판정·단어 누락은 **거절**한다 — 조용한 통과 금지."""
    lines = resp.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValueError("lines 배열 없음")
    # 단어를 빠짐·겹침 없이 덮는가 — v3 격자의 커버리지 검증과 같은 원칙
    cursor = 0
    for ln in lines:
        w0, w1 = ln.get("w0"), ln.get("w1")
        if not isinstance(w0, int) or not isinstance(w1, int) or w1 < w0:
            raise ValueError(f"w0/w1 이 정수 구간이 아니다: {w0}~{w1}")
        if w0 != cursor:
            raise ValueError(f"단어 {cursor} 에서 끊김 — 줄이 {w0} 부터 시작한다 "
                             f"(빠짐·겹침 금지)")
        cursor = w1 + 1
    if cursor != len(words):
        raise ValueError(f"단어 {cursor}/{len(words)} 만 덮었다 — 전량 커버 필요")

    for ln in lines:
        if ln.get("kind") not in ("narration", "dialogue", "unclear"):
            raise ValueError(f"kind 라벨 밖: {ln.get('kind')}")
        if ln["kind"] != "narration":
            continue
        for field, allowed in (("under_shot", UNDER_SHOT), ("entry", ENTRY),
                               ("link_to_next", LINK_NEXT)):
            if ln.get(field) not in allowed:
                raise ValueError(f"w{ln['w0']} {field} 라벨 밖: {ln.get(field)}")
    if resp.get("verdict") not in ("needed", "not_needed", "overused", "underused"):
        raise ValueError(f"verdict 라벨 밖: {resp.get('verdict')}")
    why = resp.get("why") or []
    if not why:
        raise ValueError("why 가 비었다 — 판정에는 근거가 붙어야 한다")
    for w in why:
        if w.get("label") not in WHY_NEEDED + WHY_NOT:
            raise ValueError(f"why 라벨 밖: {w.get('label')}")
        # evidence 의 관련성·정확성·성립성은 표를 만든 뒤 validate_evidence 가 본다
        # (도출 사실이 표에서 나오므로 여기서는 볼 수 없다)
    for e in resp.get("sfx") or []:
        if e.get("kind") not in SFX_KIND:
            raise ValueError(f"sfx kind 라벨 밖: {e.get('kind')}")
        if e.get("added") not in SFX_ADDED:
            raise ValueError(f"sfx added 라벨 밖: {e.get('added')}")
    for t in resp.get("screen_text") or []:
        for field, allowed in (("role", TEXT_ROLE), ("pos", TEXT_POS),
                               ("origin", TEXT_ORIGIN)):
            if t.get(field) not in allowed:
                raise ValueError(f"screen_text {field} 라벨 밖: {t.get(field)}")
        if not (t.get("text") or "").strip():
            raise ValueError("screen_text 에 문구가 비었다")
    if resp.get("end_type") not in END_TYPE:
        raise ValueError(f"end_type 라벨 밖: {resp.get('end_type')}")


def derive_facts(table: list[dict], measured: dict) -> dict:
    """**모델의 판정을 검증할 사실들.** 기계 측정 + 모델이 스스로 붙인 줄 라벨에서
    도출한다 — 후자도 검증 재료가 된다(편이 `ensemble_banter` 라고 주장하려면
    자기가 만든 표에 원본 화자가 실제로 3명 이상 있어야 한다: 내부 일관성)."""
    dia = [r for r in table if r["kind"] == "dialogue"]
    nar = [r for r in table if r["kind"] == "narration"]
    dur = measured["duration_sec"] or 1.0
    speakers = {r.get("speaker") for r in dia if r.get("speaker")}
    switches = sum(1 for a, b in zip(table, table[1:])
                   if a.get("speaker") != b.get("speaker"))
    return {
        "duration_sec": measured["duration_sec"],
        "speech_ratio": measured["speech_ratio"],
        "silence_ratio": measured["silence_ratio"],
        "max_silence_sec": measured["max_silence_sec"],
        "scene_cuts": measured["scene_cuts"],
        "cuts_per_10s": measured["cuts_per_10s"],
        "speakers": len(speakers),
        "turns_per_10s": round(switches / dur * 10, 2),
        "narration_ratio": round(sum(r["t1"] - r["t0"] for r in nar) / dur, 3),
        "dialogue_ratio": round(sum(r["t1"] - r["t0"] for r in dia) / dur, 3),
        "first_utterance_sec": round(table[0]["t0"], 2) if table else None,
        "first_speaker_at_sec": round(dia[0]["t0"], 2) if dia else None,
        "opens_with_narration": bool(table and table[0]["kind"] == "narration"),
    }


def validate_evidence(why: list[dict], facts: dict, source_scenes: int | None) -> None:
    """근거는 **항목 이름만** 받는다. 숫자는 코드가 채우고 코드가 판정한다.

    ⚠ 왜 값 인용을 걷어냈나(2026-08-31 실측): 처음엔 모델에게 값까지 인용시키고
    실측과 대조했다. 그런데 `turns_per_10s`·`speakers` 같은 도출값은 **모델 자신의
    표에서 나온다** — 답을 고치면 실측도 같이 움직인다. 실제로 한 편이 이렇게 죽었다:

        1차 2.85 냄 → "실측 2.66" 반려 → 2차 2.66 냄 → "실측 2.47" 반려
        → 3차 2.47 냄 → "실측 2.66" 실패

    맞힐 수 없는 과녁이었다. 그리고 값을 안 받으면 **지어낼 자리가 아예 없어져서**
    그 검사가 필요 없어진다. 남는 두 겹이 실질이다:

      ① 관련성 — 그 라벨이 댈 수 있는 항목인가(EVIDENCE_FIELDS).
         `context_missing` 에 `duration_sec` 을 대는 것을 여기서 막는다.
      ② 성립성 — **코드가 잰 값**이 그 주장을 지지하는가(EVIDENCE_RULES).
    """
    pool = dict(facts, source_scenes=source_scenes)
    for w in why:
        label = w.get("label")
        allowed = EVIDENCE_FIELDS.get(label, [])
        ev = w.get("evidence")
        # 목록·객체 둘 다 받는다(객체로 오면 키만 쓰고 값은 버린다 — 값은 코드 것이다)
        names = list(ev.keys()) if isinstance(ev, dict) else (
            ev if isinstance(ev, list) else [])
        if not names:
            raise ValueError(
                f"'{label}' 의 evidence 가 비었다 — 근거 **항목 이름**을 "
                f"목록으로 대라(숫자는 엔진이 채운다). 댈 수 있는 항목: {allowed}")
        for k in names:
            if k not in allowed:                                     # ①
                raise ValueError(
                    f"'{label}' 은 '{k}' 를 근거로 쓸 수 없다 — "
                    f"이 라벨이 댈 수 있는 항목: {allowed}")
            if pool.get(k) is None:
                raise ValueError(f"'{label}' 의 근거 '{k}' 를 잴 수 없었다")
        rule = EVIDENCE_RULES.get(label)                              # ②
        if rule is not None and not rule(pool):
            raise ValueError(
                f"'{label}' 은 측정값이 지지하지 않는다 — 실측 "
                f"{{{', '.join(f'{k}={pool.get(k)}' for k in allowed)}}}. "
                f"이 라벨이 맞다고 보면 줄 라벨(kind·speaker)을 다시 보라.")
        # 코드가 잰 값을 근거에 박아 넣는다 — 리포트·재현이 이 값을 본다
        w["evidence"] = {k: pool.get(k) for k in names}


def load_fp_segments(d: Path, sid: str) -> list[tuple[float, float]]:
    """지문 매칭 결과가 있으면 '원본 오디오가 그대로 들리는 구간'을 돌려준다.

    쓰임은 **반증 하나뿐**이다 — 이 구간의 발화를 모델이 narration(편집자가 얹은 목소리)
    이라고 하면 틀렸다. 원본과 소리가 일치했다는 것이 곧 원본 오디오라는 증거다.
    ⚠ 반대는 성립하지 않는다: 안 맞았다고 얹은 목소리인 것은 아니다(BGM·덮임·미보유
    회차로도 안 맞는다 — 실측 덮음 중앙 20%). 그래서 '아니다'만 말하고 '맞다'는 안 한다."""
    f = d / "fp" / f"{sid}.json"
    if not f.is_file():
        return []
    try:
        return [(s["short_start"], s["short_end"])
                for s in json.loads(f.read_text(encoding="utf-8")).get("segments") or []]
    except Exception:  # noqa: BLE001 — 검증 보조라 없으면 없는 대로 간다
        return []


def check_kind_against_source(table: list[dict], src: list[tuple[float, float]]) -> None:
    """원본 오디오와 일치한 구간을 narration 이라고 했으면 거절."""
    if not src:
        return
    bad = []
    for r in table:
        if r["kind"] != "narration":
            continue
        for a, b in src:
            ov = min(r["t1"], b) - max(r["t0"], a)
            if ov > 0.5 * (r["t1"] - r["t0"]):      # 절반 넘게 겹치면 원본이다
                bad.append(f"{r['t0']:.1f}~{r['t1']:.1f} {r['text'][:24]!r}")
                break
    if bad:
        raise ValueError(
            f"원본 오디오와 소리가 일치하는 구간을 narration 이라고 했다 — "
            f"편집자가 얹은 목소리가 아니라 **원본에 있던 소리**다: {'; '.join(bad[:3])}"
            + (f" 외 {len(bad)-3}건" if len(bad) > 3 else ""))


def lines_to_table(lines: list[dict], words: list[dict], cuts: list[float],
                   duration: float) -> list[dict]:
    """모델이 준 **단어 번호** → 대본 표. 시각·간격·컷은 여기서 **코드가** 채운다.

    모델은 끝내 초를 쓰지 않는다(v3 격자와 같은 규율) — 초를 쓰게 두면 전사와
    어긋난 시각이 표에 섞이고, 그 표로 센 숫자가 프롬프트에 박힌다."""
    out: list[dict] = []
    for k, ln in enumerate(lines):
        w0, w1 = ln["w0"], ln["w1"]
        t0, t1 = words[w0]["t0"], words[w1]["t1"]
        prev_end = words[lines[k - 1]["w1"]]["t1"] if k else 0.0
        next_start = words[lines[k + 1]["w0"]]["t0"] if k + 1 < len(lines) else duration
        out.append({
            "w0": w0, "w1": w1,
            "t0": round(t0, 2), "t1": round(t1, 2),
            "kind": ln["kind"], "speaker": ln.get("speaker"),
            "text": " ".join(w["text"].strip() for w in words[w0:w1 + 1]),
            "gap_before": round(t0 - prev_end, 2),
            "gap_after": round(next_start - t1, 2),
            "cuts_within": sum(1 for c in cuts if t0 <= c <= t1),
            "at_cut": any(abs(c - t0) <= AT_CUT_TOL_SEC for c in cuts),
            "under_shot": ln.get("under_shot"), "entry": ln.get("entry"),
            "link_to_next": ln.get("link_to_next"),
            "next_line": None,   # 아래에서 채운다
        })
    for k, r in enumerate(out):
        if r["kind"] == "narration" and k + 1 < len(out):
            r["next_line"] = out[k + 1]["text"][:60]
    return out


def cmd_anatomize(args: argparse.Namespace) -> int:
    from app.modules.gemini_client import load_gemini_client

    d = Path(args.dir)
    wl = json.loads((d / "worklist.json").read_text(encoding="utf-8"))
    outdir = d / "anatomy"
    outdir.mkdir(parents=True, exist_ok=True)
    ensure_ffmpeg_supported()
    gemini = load_gemini_client()
    ok = fail = 0

    for n, it in enumerate(wl["items"]):
        if args.shard and n % args.shard[1] != args.shard[0]:
            continue
        sid = it["shorts_id"]
        dest = outdir / f"{sid}.json"
        if dest.exists() and not args.force:
            continue
        video = d / "video" / f"{sid}.mp4"
        if not video.is_file():
            print(f"  - {sid}: 영상 없음(fetch 먼저)")
            continue
        try:
            duration = probe_duration(video)
            audio = d / "audio" / f"{sid}.wav"
            if not audio.is_file():
                extract_audio(video, audio)
            words, failed_windows = transcribe_words(audio, duration, log=lambda *_: None)
            utts = group_utterances(words)
            cuts = detect_scene_cuts(video, threshold=SHORTS_SCENE_THRESHOLD)
            sils = detect_silence_intervals(audio, duration)
            measured = measure(utts, cuts, sils, duration)
            from app.v3.audio import load_pcm
            sfx_cand = detect_sfx_candidates(load_pcm(audio), duration, utts)
            measured["sfx_candidates"] = len(sfx_cand)
            base = build_prompt(measured, words, cuts, sfx_cand)
            reject = ""
            for attempt in range(MAX_REASK + 1):
                resp = _call_vision(gemini, video, base + reject)
                try:
                    validate(resp, words)
                    table = lines_to_table(resp["lines"], words, cuts, duration)
                    check_kind_against_source(table, load_fp_segments(d, sid))
                    facts = derive_facts(table, measured)
                    validate_evidence(resp.get("why") or [], facts,
                                      resp.get("source_scenes"))
                    break
                except ValueError as ve:
                    if attempt == MAX_REASK:
                        raise
                    print(f"    ↺ {sid} 반려({attempt + 1}/{MAX_REASK}): {ve}")
                    reject = (f"\n\n## ⚠ 직전 답이 거절됐다 — 고쳐서 다시 내라\n{ve}\n")
            dest.write_text(json.dumps({
                "shorts_id": sid, "meta": it, "measured": measured,
                "failed_windows": failed_windows,
                "utterances": utts, "words": words, "table": table,
                "sfx_candidates": sfx_cand, "facts": facts, "anatomy": resp,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            n_nar = sum(1 for r in table if r["kind"] == "narration")
            spk = len({r.get("speaker") for r in table if r["kind"] == "dialogue"})
            n_sfx = sum(1 for e in (resp.get("sfx") or []) if e.get("added") == "yes")
            print(f"  ✓ {sid} 줄 {len(table)} (내레이션 {n_nar} · 원본화자 {spk}) · "
                  f"효과음 {len(resp.get('sfx') or [])}(얹은 것 {n_sfx}) · "
                  f"{resp['verdict']} · {resp.get('one_line','')}")
            ok += 1
        except Exception as e:  # noqa: BLE001 — 편별 실패는 건수로 남기고 계속
            print(f"  ✗ {sid}: {type(e).__name__}: {e}")
            fail += 1
    print(f"[anatomize] 성공 {ok} · 실패 {fail} → {outdir}")
    return 0


def _call_vision(gemini, video: Path, prompt: str) -> dict:
    """Files API 업로드 + Pro 1회. 영상을 실제로 보는 호출이라 Pro 규칙 그대로."""
    f = gemini.client.files.upload(file=str(video))
    try:
        resp = gemini.client.models.generate_content(
            model=gemini.config.model_name,
            contents=[f, prompt],
            config=gemini.types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.0))
        from app.modules.gemini_client import _extract_json_from_markdown
        return json.loads(_extract_json_from_markdown(resp.text.strip()))
    finally:
        try:
            gemini.client.files.delete(name=f.name)
        except Exception:  # noqa: BLE001 — 정리 실패가 분석을 막지 않는다
            pass


# ── report ─────────────────────────────────────────────────────────────────
def cmd_report(args: argparse.Namespace) -> int:
    d = Path(args.dir) / "anatomy"
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))]
    # 구 형식(화자 분할 전 · table 없음)은 집계에서 뺀다 — 섞어서 세면 두 계약의
    # 숫자가 한 표에 들어간다. 죽지 않고 건수를 알린다(조용한 누락 금지).
    legacy = [x for x in docs if "table" not in x]
    docs = [x for x in docs if "table" in x]
    if legacy:
        print(f"⚠ 구 형식 {len(legacy)}편 제외(재해부 필요): "
              + ", ".join(x.get("shorts_id", "?") for x in legacy[:6])
              + (" …" if len(legacy) > 6 else ""))
    if not docs:
        raise SystemExit(f"해부 결과 없음(신 형식): {d}")

    def bucket(doc):
        return doc["meta"].get("perf_label", "?")

    # 그룹은 하드코딩하지 않는다 — 사용자가 준 링크는 perf_label='user' 로 들어와
    # good/bad 와 나란히 찍혀야 한다("성과가 좋은 편"과 "이렇게 만들고 싶은 편"은
    # 다른 질문이다). 있는 그룹만, 정해진 순서로.
    # ⚠ good/bad 로 가르지 않는다(사용자 지시 2026-08-31: "지금은 비교하지 말고
    #   데이터 쌓는 것만"). 표본이 20편일 때 대조를 앞세우면 노이즈를 신호로 읽게
    #   된다 — 지금 할 일은 분포를 모으는 것이다. 성과 라벨은 편별 덤프에 남으므로
    #   나중에 --split 로 되살릴 수 있다(그때는 표본이 충분해야 한다).
    labs = ["전체"] if not args.split else (
        [g for g in ["ours", "good", "bad", "user"] if g in {bucket(x) for x in docs}]
        + sorted({bucket(x) for x in docs} - {"ours", "good", "bad", "user"}))

    def in_group(x, lab):
        return True if lab == "전체" else bucket(x) == lab

    print(f"\n=== 표본 {len(docs)}편 (구성: " + " · ".join(
        f"{k} {v}" for k, v in Counter(map(bucket, docs)).most_common())
        + ") ===\n")

    # 내레이션 유무 × 성과 — '내레이션을 쓰느냐'가 성패를 가르는지부터
    print("[내레이션 유무 × 성과]")
    for lab in labs:
        sub = [x for x in docs if in_group(x, lab)]
        if not sub:
            continue
        with_nar = [x for x in sub if any(
            ln["kind"] == "narration" for ln in x["table"])]
        print(f"  {lab:4} n={len(sub):3}  내레이션 있음 {len(with_nar)/len(sub)*100:5.1f}%")

    # 사용자가 준 축들
    for field, title in (("under_shot", "내레이션 동안의 화면"),
                         ("entry", "내레이션이 들어온 자리"),
                         ("link_to_next", "다음 대사와의 관계")):
        print(f"\n[{title}]")
        for lab in labs:
            c = Counter(ln.get(field) for x in docs if in_group(x, lab)
                        for ln in x["table"] if ln["kind"] == "narration")
            tot = sum(c.values()) or 1
            top = " · ".join(f"{k} {v/tot*100:.0f}%" for k, v in c.most_common(5))
            print(f"  {lab:4} n={tot:3}  {top}")

    print("\n[판정 · 이유]")
    for lab in labs:
        c = Counter(w["label"] for x in docs if in_group(x, lab)
                    for w in x["anatomy"].get("why", []))
        print(f"  {lab:4} " + (" · ".join(f"{k} {v}" for k, v in c.most_common(6)) or "-"))

    print("\n[내레이션 문구 — 말투]")
    print("  ⚠ 어미 분류는 방향만 본다 — ㅁ 종결이 명사형(권장)인지 축약(금지)인지는")
    print("     어휘로 안 갈린다('장악한 희로' vs '뒤집힘'). 판단은 아래 원문으로.")
    for lab in labs:
        nar = [r for x in docs if in_group(x, lab)
               for r in x["table"] if r["kind"] == "narration"]
        if not nar:
            print(f"  {lab:4} 내레이션 없음")
            continue
        chars = sorted(len(r["text"]) for r in nar)
        cps = sorted((len(r["text"]) / max(r["t1"] - r["t0"], 0.1)) for r in nar)
        end = Counter(classify_ending(r["text"]) for r in nar)
        tot = len(nar)
        print(f"  {lab:4} n={tot:3}  글자수 중앙 {chars[len(chars)//2]} · p90 "
              f"{chars[int(0.9*(len(chars)-1))]}  |  초당 {cps[len(cps)//2]:.1f}자")
        print(f"       어미: " + " · ".join(
            f"{k} {v/tot*100:.0f}%" for k, v in end.most_common(6)))
        etc = [r["text"] for r in nar if classify_ending(r["text"]) == "기타"]
        if etc:
            print(f"       기타 원문: " + " | ".join(repr(t) for t in etc[:4]))

    print("\n[내레이션이 채우는 시간 · 대사와의 간격]")
    for lab in labs:
        sub = [x for x in docs if in_group(x, lab)]
        withn = [x for x in sub if any(r["kind"] == "narration" for r in x["table"])]
        if not withn:
            print(f"  {lab:4} 내레이션 쓴 편 없음")
            continue
        nr = sorted(x["facts"]["narration_ratio"] for x in withn if x.get("facts"))
        dr = sorted(x["facts"]["dialogue_ratio"] for x in withn if x.get("facts"))
        per = sorted(sum(1 for r in x["table"] if r["kind"] == "narration") for x in withn)
        print(f"  {lab:4} 내레이션 쓴 편 {len(withn)}/{len(sub)}"
              f"  편당 줄 수 중앙 {per[len(per)//2]} (p90 {per[int(.9*(len(per)-1))]})")
        if nr:
            print(f"       차지 시간 비중: 내레이션 중앙 {nr[len(nr)//2]*100:.0f}% · "
                  f"원본대사 중앙 {dr[len(dr)//2]*100:.0f}%")
        # 내레이션 → 다음 대사 간격 (미리 말하고 대사가 확인하는 문법의 핵심 수치)
        gaps = [r["gap_after"] for x in withn for r in x["table"]
                if r["kind"] == "narration" and r.get("gap_after") is not None]
        if gaps:
            gaps.sort()
            over = sum(1 for g in gaps if g < 0)
            print(f"       내레이션 끝 → 다음 발화까지: 중앙 {gaps[len(gaps)//2]:.2f}s · "
                  f"p10 {gaps[len(gaps)//10]:.2f}s · p90 {gaps[int(.9*(len(gaps)-1))]:.2f}s"
                  f"  (붙여 쓴 것 {sum(1 for g in gaps if g <= 0.3)}/{len(gaps)})")
        # 내레이션 사이 간격 = 얼마나 촘촘히 깔리나
        runs = []
        for x in withn:
            ns = [r for r in x["table"] if r["kind"] == "narration"]
            runs += [b["t0"] - a["t1"] for a, b in zip(ns, ns[1:])]
        if runs:
            runs.sort()
            print(f"       내레이션끼리 간격: 중앙 {runs[len(runs)//2]:.1f}s")

    if args.lines:
        print(f"\n[내레이션 원문 — 편마다 앞 {args.lines}줄]")
        for x in docs:
            nar = [r for r in x["table"] if r["kind"] == "narration"]
            if not nar:
                continue
            v = x["anatomy"]
            print(f"\n  ── {x['shorts_id']} [{bucket(x)}] "
                  f"{v['verdict']} · {v.get('end_type')} · "
                  f"{[w['label'] for w in v.get('why', [])]}")
            for r in nar[:args.lines]:
                print(f"     {r['t0']:6.2f} {r['entry']:13} {r['under_shot']:16} "
                      f"{r['text'][:44]}")
                if r.get("next_line"):
                    print(f"            └→ {r['link_to_next']}: {r['next_line'][:40]}")

    print("\n[보조 텍스트 — 편집자가 얹은 설명 레이어 (대사 자막 제외)]")
    for lab in labs:
        sub = [x for x in docs if in_group(x, lab)]
        aux = [t for x in sub for t in (x["anatomy"].get("screen_text") or [])
               if t.get("role") in AUX_ROLES and t.get("origin") == "added"]
        c = Counter(t["role"] for t in aux)
        pos = Counter(t["pos"] for t in aux)
        print(f"  {lab:4} 편당 {len(aux)/max(len(sub),1):.1f}개  "
              + (" · ".join(f"{k} {v}" for k, v in c.most_common(6)) or "-"))
        if pos:
            print(f"       위치: " + " · ".join(f"{k} {v}" for k, v in pos.most_common(4)))
    ex = [t for x in docs for t in (x["anatomy"].get("screen_text") or [])
          if t.get("role") in AUX_ROLES and t.get("origin") == "added"]
    for t in ex[:8]:
        print(f"       예) [{t['role']}·{t['pos']}] {t['text'][:26]!r} — {(t.get('purpose') or '')[:38]}")

    print("\n[효과음 — 원본에 없던 것을 얹었나]")
    for lab in labs:
        sub = [x for x in docs if in_group(x, lab)]
        ev = [e for x in sub for e in (x["anatomy"].get("sfx") or [])]
        added = [e for e in ev if e.get("added") == "yes"]
        per = len(added) / len(sub) if sub else 0
        kinds = Counter(e["kind"] for e in added).most_common(5)
        print(f"  {lab:4} 편당 얹은 효과음 {per:.1f}개  " +
              (" · ".join(f"{k} {v}" for k, v in kinds) or "-"))
    print("  BGM: " + " · ".join(
        f"{lab}={dict(Counter(x['anatomy'].get('bgm') for x in docs if bucket(x)==lab))}"
        for lab in labs))

    print("\n[엔딩]")
    for lab in labs:
        sub = [x for x in docs if in_group(x, lab)]
        c = Counter(x["anatomy"].get("end_type") for x in sub)
        mid = sum(1 for x in sub if x["anatomy"].get("ends_mid_sentence"))
        print(f"  {lab:4} " + " · ".join(f"{k} {v}" for k, v in c.most_common()) +
              f"  | 문장 중간에 끊음 {mid}/{len(sub)}")

    # 가설 검증 — 진짜 축은 장르가 아니라 '이어붙인 장면 수'인가
    print("\n[가설: 축은 장르가 아니라 이어붙인 원본 장면 수]")
    for lo, hi, name in ((1, 1, "1개(한 장면 통째)"), (2, 3, "2~3개"), (4, 99, "4개+")):
        sub = [x for x in docs if lo <= (x["anatomy"].get("source_scenes") or 0) <= hi]
        if not sub:
            continue
        wn = sum(1 for x in sub if any(ln["kind"] == "narration"
                                       for ln in x["table"]))
        print(f"  {name:16} n={len(sub):3}  내레이션 있음 {wn/len(sub)*100:5.1f}%")
    print("\n[참고 — 장르는 표집 기준이 아니었다. 분포만]")
    print("  " + " · ".join(f"{k or '?'}:{v}" for k, v in Counter(
        x["meta"].get("video_type") for x in docs).most_common(8)))

    if args.labels:
        print("\n[라벨 점검 — 미사용 라벨은 빼고, other 쏠림은 라벨을 늘리라는 신호]")
        used = Counter(ln.get("under_shot") for x in docs
                       for ln in x["table"] if ln["kind"] == "narration")
        print(f"  미사용 under_shot: {sorted(set(UNDER_SHOT) - set(used)) or '없음'}")
        print(f"  other 비율: {used.get('other',0)}/{sum(used.values()) or 1}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shorts_anatomy",
                                description="레퍼런스 쇼츠 해부기 (v3 프롬프트 재료)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="성과만 층화한 무작위 표집(장르 무시)")
    s.add_argument("--n", type=int, default=30)
    s.add_argument("--min-sec", type=float, default=25.0)
    s.add_argument("--out", default="work/anatomy")
    s.set_defaults(fn=cmd_sample)

    a2 = sub.add_parser("add-urls", help="유튜브 링크를 목록에 추가(사용자 선정)")
    a2.add_argument("--dir", default="work/anatomy")
    a2.add_argument("--url", action="append", help="유튜브 링크(여러 번 가능)")
    a2.add_argument("--urls-file", help="링크 파일(한 줄에 하나 · # 주석 허용)")
    a2.set_defaults(fn=cmd_add_urls)

    a3 = sub.add_parser("add-local", help="로컬 mp4 추가(우리 산출물 대조용)")
    a3.add_argument("--dir", default="work/anatomy")
    a3.add_argument("--path", action="append",
                    help="mp4 파일 또는 디렉토리(여러 번 가능)")
    a3.add_argument("--prefix", default="ours_", help="id 접두(기본 ours_)")
    a3.set_defaults(fn=cmd_add_local)

    f = sub.add_parser("fetch", help="영상 내려받기(Storage · 유튜브)")
    f.add_argument("--dir", default="work/anatomy")
    f.set_defaults(fn=cmd_fetch)

    a = sub.add_parser("anatomize", help="전사 + 장면·무음 측정 + 판정")
    a.add_argument("--dir", default="work/anatomy")
    a.add_argument("--force", action="store_true")
    a.add_argument("--shard", type=lambda v: tuple(int(x) for x in v.split("/")),
                   default=None, metavar="i/N",
                   help="i 번째 조각만 처리(0-based) — 여러 프로세스로 나눠 돌릴 때. "
                        "⚠ faster-whisper 는 Apple Silicon GPU 를 못 쓴다(CTranslate2 에 "
                        "Metal 백엔드 없음) — M4 에서도 CPU/int8 이라 편당 ~2분이다. "
                        "모델을 줄이는 대신 프로세스를 나눠 성능 코어를 채운다.")
    a.set_defaults(fn=cmd_anatomize)

    r = sub.add_parser("report", help="집계")
    r.add_argument("--dir", default="work/anatomy")
    r.add_argument("--labels", action="store_true", help="라벨 집합 점검")
    r.add_argument("--split", action="store_true",
                   help="성과(good/bad)로 갈라 찍기 — 표본이 충분할 때만")
    r.add_argument("--lines", type=int, default=0,
                   help="편마다 내레이션 원문 N줄 출력(0=끄기)")
    r.set_defaults(fn=cmd_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
