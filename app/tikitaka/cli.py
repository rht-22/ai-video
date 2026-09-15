"""CLI — `python -m app.tikitaka --source <mp4> --title 포핸즈 [--cast 강비호,홍재인] [--version auto|N]`.

체크포인트가 있는 단계는 건너뛴다(`--redo` 로 강제). 렌더에는 FFMPEG_BIN(ffmpeg 6/7) 이 필요하다.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import sys
from pathlib import Path

from app.tikitaka.common import fmt_tc, Job, load_dotenv_if_any


REDO_ORDER = ["transcribe", "polish", "index", "digest", "rebuild", "verify", "agentic", "table", "framing", "effects", "render"]
REDO_FILES = [("transcribe", ["transcript.json", "transcript_polish.json", "stt_windows/*.json", "polish_windows/*.json"]),
              ("polish", ["transcript_polish.json", "polish_windows/*.json"]),
              ("index", ["index.json", "index_windows/*.json"]), ("digest", ["digest.json", "digest.md"]), ("rebuild", ["rebuild.json", "rebuild_raw.json"]),
              ("verify", ["verified_v*.json", "verify_raw_v*.json"]),
              ("agentic", ["table_agentic_raw_v*.json"]), ("table", ["table_v*.json"]), ("framing", ["framing_v*.json"]),
              ("effects", ["effects_v*.json"]), ("render", ["shorts_v*.mp4"])]


def cascade_redo(steps: set[str]) -> set[str]:
    """앞 단계를 다시 만들면 그 산출을 먹는 뒤 단계도 전부 다시 — 옛 줄 ID 위에 새 전사가 얹히는 조용한 불일치를 막는다. 순수."""
    out = set(steps)
    for st in steps:
        if st == "research":
            out.update(REDO_ORDER[REDO_ORDER.index("index"):])
        if st in REDO_ORDER:
            out.update(REDO_ORDER[REDO_ORDER.index(st):])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.tikitaka", description="티키타카 쇼츠 파이프라인")
    ap.add_argument("--pipeline", choices=("grid-review", "legacy"), default="grid-review",
                    help="기본 grid-review: grid 인덱스 + 편별 초안 재관찰 + v3 연출. legacy: 원격 tikitaka 경로")
    ap.add_argument("--design-preset", default=None, help="grid-review 렌더 디자인 프리셋")
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--title", required=True, help="작품명")
    ap.add_argument("--episode", default="", help="회차 표기(예: 1회)")
    ap.add_argument("--cast", default="", help="등장인물 후보(쉼표 구분)")
    ap.add_argument("--skip-research", action="store_true", help="grid-review의 v3 작품 리서치를 생략(가이드 인물 정보는 적용)")
    ap.add_argument("--retry-failed-chunks", action="store_true", help="v3 분석의 실패 청크만 다시 요청")
    ap.add_argument("--out", type=Path, default=None, help="잡 디렉토리(기본 outputs_tikitaka_grid/<제목>_<회차>, legacy는 outputs_tikitaka)")
    ap.add_argument("--version", default="auto", help="렌더할 리빌딩 버전 번호(쉼표로 여러 개 · 11~14 는 선형 서사 계열) 또는 auto(추천)")
    ap.add_argument("--range", default=None, help="재료 구간 MM:SS~MM:SS (쉼표로 여러 개) — 그 밖은 활용 불가로 배제. 산출은 자동 태그 r<시작>-<끝>")
    ap.add_argument("--seq-hook", choices=("on", "off"), default="on", help="구간 순차형(v11) 첫 3초 콜드오픈(구간에서 가장 센 한 줄) 여부")
    ap.add_argument("--rerank", choices=("draft", "verified"), default="draft",
                    help="재순위 기준 — draft(리빌딩 직후 자동) | verified(확인 패스가 끝난 최종본으로 렌더 뒤 다시 매긴다)")
    ap.add_argument("--all-versions", action="store_true", help="생성된 14전략 버전 전부 테이블+렌더")
    ap.add_argument("--count", type=int, default=1, help="조회수 기대 순위 상위 N개 버전을 각각 확인·테이블·렌더 (--version auto 일 때)")
    ap.add_argument("--cut-search", choices=("agentic", "index"), default="agentic", help="N 행 컷 소스 탐색 경로")
    ap.add_argument("--layout", choices=("fill", "band"), default=None, help="grid-review 미지정=V3 템플릿 · fill=5:6 · band=16:9 · legacy 기본=fill")
    ap.add_argument("--no-framing", action="store_true", help="5.5단계 Gemini 주인물 크롭을 건너뛴다(중앙 크롭)")
    ap.add_argument("--guide", action="append", default=None, help="제작 가이드 파일(반복 가능). 미지정이면 guides/tikitaka/<작품명>.md · <작품명>/<회차>.md 자동 탐색")
    ap.add_argument("--logo", default=None, help="작품명 대신 넣을 로고 이미지(PNG 알파). 가이드의 '로고:' 키보다 우선")
    ap.add_argument("--copy", default=None, help="작품명/로고 위·아래 카피 문구. 가이드의 '카피:' 키보다 우선")
    ap.add_argument("--copy-pos", choices=("above", "below"), default=None, help="카피 위치(기본 below). 가이드의 '카피 위치:' 키보다 우선")
    ap.add_argument("--voice", default="ko_female", help="내레이션 목소리 라벨(ko_female·ko_female_high·ko_male·ko_male_low·chat_*)")
    ap.add_argument("--tag", default="", help="산출 접미사 — table/effects/shorts/publish 를 v{n}_{tag} 로 따로 만든다(다른 목소리·속도 변형 등). 전사·인덱스·대본·컷 탐색·프레이밍 캐시는 공유")
    ap.add_argument("--speed", default="normal")
    ap.add_argument("--workers", type=int, default=3, help="인덱스 창 병렬 수")
    ap.add_argument("--stt", choices=("elevenlabs", "whisper"), default="elevenlabs", help="1단계 전사 백엔드(기본 ElevenLabs Scribe v2 · 키 없으면 즉시 실패)")
    ap.add_argument("--no-transcript-polish", action="store_true", help="1.5단계 Gemini 글자 교정을 건너뛴다")
    ap.add_argument("--no-voice-check", action="store_true", help="3.5단계 목소리(diarize) 기준 화자 대조를 건너뛴다")
    ap.add_argument("--no-verify", action="store_true", help="4.5단계 영상 확인 패스를 건너뛴다(텍스트 리빌딩 버전 그대로)")
    ap.add_argument("--redo", default="", help="다시 만들 단계(쉼표): research,transcribe,polish,index,digest,rebuild,verify,agentic,table,review,style,render. grid-review의 render는 관찰/연출을 재사용")
    ap.add_argument("--no-digest", action="store_true", help="3.7단계 작품 이해 문서(digest)를 만들지 않는다(종전 프롬프트)")
    ap.add_argument("--until", default="render", choices=("research", "transcribe", "polish", "index", "digest", "rebuild", "verify", "table", "render"))
    a = ap.parse_args(argv)
    if a.pipeline == "legacy" and a.layout is None:
        a.layout = "fill"
    from app.tikitaka import research as research_module
    research_episode = None
    if a.pipeline == "grid-review":
        try:
            research_episode = research_module.episode_number(a.episode)
        except ValueError as exc:
            ap.error(str(exc))
    elif a.until == "research" or "research" in a.redo.split(","):
        ap.error("research 단계는 grid-review 전용입니다")
    if a.tag and not re.fullmatch(r"[A-Za-z0-9_-]+", a.tag):
        ap.error("--tag는 영숫자, 밑줄, 하이픈만 사용할 수 있습니다")
    preset = None
    preset_episode = None
    if a.design_preset:
        if a.pipeline == "legacy":
            ap.error("--design-preset은 grid-review 전용입니다")
        from app.v3.cli import load_design_preset
        preset = load_design_preset(a.design_preset)
        if preset.get("banned"):
            episode_match = re.fullmatch(r"(\d+)(?:회|화)?", a.episode.strip())
            if episode_match is None:
                ap.error("활용 불가 구간 프리셋은 --episode 회차 숫자가 필요합니다")
            preset_episode = int(episode_match[1])
        if preset["options"].get("subtitle_skip_singing"):
            ap.error("이 프리셋은 v3 가창 분석이 필요합니다. grid-review에서는 지원하지 않습니다")

    load_dotenv_if_any()
    from app.tikitaka import probe as P, transcribe as T, scenecut as C, index as I, rebuild as R, table as TB, render as RD, report as RP
    from app.tikitaka import verify as V
    from app.tikitaka import transcript_polish as TP
    from app.tikitaka import framing as FR
    from app.tikitaka import digest as DG
    from app.tikitaka import effects as EF
    from app.tikitaka.llm import Gemini
    from app.tikitaka import guide as G

    out_dir = a.out or Path("outputs_tikitaka_grid" if a.pipeline == "grid-review" else "outputs_tikitaka") / f"{a.title}_{a.episode or 'ep'}".replace(" ", "_")
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    job = Job(source=a.source.resolve(), out_dir=out_dir, title=a.title)
    from app.tikitaka.grid import source_identity
    identity = {"pipeline": a.pipeline, "source": source_identity(job.source), "stt": a.stt}
    if job.has("pipeline_identity.json"):
        if job.load("pipeline_identity.json") != identity:
            ap.error("잡의 소스/파이프라인이 다릅니다. --out 으로 새 잡 디렉토리를 지정하세요")
    elif a.pipeline == "grid-review" and job.has("transcript.json"):
        ap.error("기존 잡 캐시와 grid-review를 섞을 수 없습니다. --out 으로 새 디렉토리를 지정하세요")
    job.save("pipeline_identity.json", identity)
    v3_client = None
    def get_v3():
        nonlocal v3_client
        if v3_client is None:
            from app.modules.gemini_client import load_gemini_client
            v3_client = load_gemini_client()
        return v3_client
    guide = G.load_guides([Path(x) for x in a.guide] if a.guide else G.discover_guides(a.title, a.episode))
    if preset and preset.get("editorial"):
        from app.tikitaka.grid import fingerprint
        guide = dict(guide or {"files": [], "avoid": [], "text": ""})
        guide["text"] += "\n채널 편집 지침:\n" + json.dumps(preset["editorial"], ensure_ascii=False)
        guide["sha"] = fingerprint([guide.get("sha"), preset["editorial"]])
    logo = Path(a.logo) if a.logo else (Path(guide["logo"]) if guide and guide.get("logo") else None)
    copy_text = a.copy if a.copy is not None else ((guide or {}).get("copy") or None)
    copy_pos = a.copy_pos or (guide or {}).get("copy_pos") or "below"
    if guide:
        job.log(f"[guide] 제작 가이드 {len(guide['files'])}개 로드({guide['sha']}) — 지양 단어 {guide['avoid']} · 로고 {logo} · 카피 {copy_text!r}({copy_pos})")
        job.log("[guide]   " + " · ".join(guide["files"]))
    if logo and not logo.exists():
        ap.error(f"로고 이미지가 없다: {logo}")
    redo = cascade_redo({s.strip() for s in a.redo.split(",") if s.strip()})
    unknown_redo = redo - set(REDO_ORDER) - {"research", "review", "style"}
    if unknown_redo:
        ap.error(f"알 수 없는 --redo 단계: {sorted(unknown_redo)}")
    if a.pipeline == "grid-review" and "index" in redo:
        # A rebuilt index needs its speaker corrections applied again.
        import time
        names = ["voice_check.json"]
        if "transcribe" in redo:
            names.append("diarize_windows")
        for name in names:
            if job.path(name).exists():
                job.path(name).rename(job.path(f"{name}.prev_{time.time_ns()}"))
    if a.pipeline == "grid-review" and "index" in redo and job.has("grid_index.json"):
        # Keep generated observations for audit while explicitly requesting new ones.
        import time
        for name in ("grid_index.json", "checkpoint_screen_text.json"):
            if job.has(name):
                job.path(name).rename(job.path(f"{name}.prev_{time.time_ns()}"))
        if job.path("grid_cache").exists():
            job.path("grid_cache").rename(job.path(f"grid_cache_prev_{time.time_ns()}"))
    tag_re = re.compile(r"_v\d+_(.+?)\.(json|md|mp4|txt)$")     # 태그 산출(v3_fast · v11_r0300-0700)은 그 태그 실행에서만 지운다
    for step, files in REDO_FILES:
        if step in redo:
            for pat in files:
                pats = [pat] + ([pat.replace(".json", f"_{a.tag}.json")] if a.tag and "*" not in pat else [])
                for pt in pats:
                    for p in out_dir.glob(pt):
                        m = tag_re.search(p.name)
                        if a.tag and (not m or m.group(1) != a.tag) and "*" in pt:
                            continue                                    # 태그 실행: 다른 태그·무태그 버전 파일은 보존
                        if not a.tag and m:
                            continue                                    # 무태그 실행: 태그 파일 보존
                        p.unlink()
    if "polish" in redo and "transcribe" not in redo and (out_dir / "transcript.json").exists():
        tr = json.loads((out_dir / "transcript.json").read_text(encoding="utf-8"))   # 교정만 다시: 원문으로 되돌리고 플래그 해제
        for l in tr.get("lines", []):
            if l.get("text_orig"):
                l["text"] = l.pop("text_orig")
        tr["polished"] = False
        (out_dir / "transcript.json").write_text(json.dumps(tr, ensure_ascii=False, indent=1), encoding="utf-8")
    cast = [c.strip() for c in a.cast.split(",") if c.strip()]
    observation_context = ""
    if a.pipeline == "grid-review":
        research = None if a.skip_research else research_module.run(
            job, get_v3, episode=research_episode, force="research" in redo)
        cast = research_module.cast_names(research, cast, guide)
        observation_context = research_module.context(research, cast, guide)
        guide = research_module.with_context(guide, observation_context)
        if a.until == "research":
            return 0

    info = P.probe(job)
    wav = P.build_audio(job)
    proxy = P.build_scan_proxy(job)
    cuts = C.detect_scene_cuts(job, P.build_cut_proxy(job))     # 샷 경계는 10fps 프록시(0.1s 정밀) — 스캔 프록시(1fps)가 아니다
    black = C.detect_black_spans(job, P.build_cut_proxy(job))   # 페이드·암전 — N/A 컷 후보에서 깎는다(2026-09-12 v1 35s 검은 화면)
    transcript = T.transcribe(job, wav, title=a.title, cast=cast, backend=a.stt)
    if a.until == "transcribe":
        return 0
    gemini = Gemini(log=job.log)
    if not a.no_transcript_polish:                        # 1.5 글자 교정(시각은 STT 그대로)
        transcript = TP.polish_transcript(job, gemini, transcript, wav, info["duration_sec"], title=a.title, cast=cast,
                                          research_context=observation_context)
    if a.until == "polish":
        return 0
    if job.has("transcript_dependents_dirty.json"):
        # Preserve previous outputs for comparison; caches with embedded old
        # dialogue must not survive a text-only correction.
        import time
        stamp = time.time_ns()
        patterns = [pat for step, pats in REDO_FILES if step in REDO_ORDER[2:] for pat in pats]
        patterns += ["grid_index.json", "voice_check.json", "grid_table_v*.json", "review_v*", "publish_v*.json"]
        for pat in patterns:
            for old in list(out_dir.glob(pat)):
                if ".prev_" not in old.name:
                    old.rename(old.with_name(f"{old.name}.prev_{stamp}"))
        job.path("transcript_dependents_dirty.json").rename(job.path(f"transcript_dependents_dirty.json.prev_{stamp}"))
        job.log("[polish] 전사 변경 → 이전 대본·편집·렌더 캐시 보존 후 무효화")
    grid = None
    if a.pipeline == "grid-review":
        from app.tikitaka import v3_analysis as grid_module
        index, grid = grid_module.build_index(job, gemini, transcript, proxy, info, cuts,
                                              title=a.title, cast=cast, get_v3=get_v3,
                                              research_context=observation_context,
                                              force="index" in redo, retry_failed=a.retry_failed_chunks)
    else:
        index = I.build_index(job, gemini, transcript, proxy, info["duration_sec"], title=a.title, cast=cast, workers=a.workers)
    transcript = job.load("transcript.json")
    if not a.no_voice_check and a.stt == "elevenlabs":              # 3.5 목소리 기준 화자 대조 — 인덱스의 장면 단위 화자 뒤바뀜을 잡는다
        from app.tikitaka import voice_check as VC
        VC.voice_check(job, transcript, index, wav)
        transcript, index = job.load("transcript.json"), job.load("index.json")
    if a.until == "index":
        return 0
    analysis_exclude = index.get("analysis_excluded_ranges", [])
    if analysis_exclude:
        from app.tikitaka.v3_analysis import overlaps
        transcript = {**transcript, "lines": [l for l in transcript["lines"]
                      if not overlaps(l["start"], l["end"], analysis_exclude)]}
        job.log(f"[v3/index] 제외/실패 구간 {len(analysis_exclude)}개를 대본·덮개·렌더에서 배제")
    digest = None
    if not a.no_digest:                                            # 3.7 작품 이해 — 대본을 짜기 전에 회차 전체(관계·동기·인과)를 문서로
        digest = DG.build_digest(job, gemini, index, transcript, title=a.title, episode_label=a.episode, guide=guide)
    if a.until == "digest":
        return 0
    if guide and job.has(f"rebuild{'_' + a.tag if a.tag else ''}.json") and job.load(f"rebuild{'_' + a.tag if a.tag else ''}.json").get("guide_sha") != guide["sha"]:
        job.log(f"[guide] ⚠ 리빌딩 캐시(rebuild.json)는 이 가이드({guide['sha']})로 만든 것이 아니다 — 가이드는 확인 패스·문구 벨트에만 적용된다. "
                "처음부터 반영하려면 --redo rebuild")
    exclude = sorted(G.excluded_ranges(guide, info["duration_sec"]) + [tuple(r) for r in analysis_exclude])
    preset_exclude = []
    if preset and preset.get("banned"):
        from app.v3.banned import banned_for_episode
        from app.v3.pipeline import _resolve_banned_for_job
        resolved = _resolve_banned_for_job(job.load("stage2.json"), grid,
            banned_for_episode(preset["banned"], preset_episode), log=job.log)
        job.save("checkpoint_banned.json", resolved)
        preset_exclude = [tuple(x) for x in resolved["intervals"]]
        exclude = sorted(exclude + preset_exclude)
    if exclude:
        job.log("[guide] 활용 불가 구간 " + ", ".join(f"{s0:.1f}~{e0:.1f}s" for s0, e0 in exclude) + " — 대본·컷 탐색·테이블에서 배제")
    range_label = None
    range_exclude: list[tuple[float, float]] = []
    if a.range:
        include = [(r["start"], r["end"] if r["end"] is not None else info["duration_sec"]) for r in G.parse_ranges(a.range.replace(",", " / "))]
        if not include:
            ap.error(f"--range 형식 오류: {a.range!r} (MM:SS~MM:SS)")
        range_exclude = G.ranges_complement(include, info["duration_sec"])
        range_label = ", ".join(f"{fmt_tc(s0)[:5]}~{fmt_tc(e0)[:5]}" for s0, e0 in include)
        if not a.tag:
            a.tag = "r" + "-".join(f"{int(s0)//60:02d}{int(s0)%60:02d}-{int(e0)//60:02d}{int(e0)%60:02d}" for s0, e0 in include)
        job.log(f"[cli] 재료 구간 {range_label} → 밖은 배제 · 산출 태그 {a.tag}")
        exclude = sorted(exclude + range_exclude)
    sfx = f"_{a.tag}" if a.tag else ""
    rebuild_name = f"rebuild{sfx}.json"
    if grid is not None:
        from app.tikitaka.grid import ensure_story_inputs
        ensure_story_inputs(job, [index["grid_fingerprint"], guide, exclude, a.seq_hook,
                                  [(l["id"], l["text"]) for l in transcript["lines"]]], tag=a.tag)
    rebuild = R.rebuild(job, gemini, index, transcript, title=a.title, episode_label=a.episode, duration=info["duration_sec"], guide=guide,
                        extra_exclude=range_exclude + preset_exclude + [tuple(r) for r in analysis_exclude], range_label=range_label, seq_hook=(a.seq_hook == "on"), cache_name=rebuild_name, digest=digest)
    if digest and not rebuild.get("digest"):
        job.log("[digest] ⚠ 리빌딩 캐시는 작품 이해 문서 없이 만든 것이다 — 처음부터 반영하려면 --redo rebuild")
    if not rebuild.get("rerank"):                                     # 재순위 단계 이전에 만든 캐시 — 초안 기준으로 한 번 매긴다(멱등)
        R.apply_rerank(job, gemini, rebuild, title=a.title, episode=a.episode, basis="draft")
        job.save(rebuild_name, rebuild)
    job.path(f"rebuild_versions{sfx}.md").write_text(RP.versions_md(rebuild, title=a.title), encoding="utf-8")
    if a.until == "rebuild":
        return 0
    if a.all_versions:
        targets = [v["n"] for v in rebuild["versions"]]
    elif a.version == "auto":
        ranking = rebuild.get("ranking") or [rebuild["recommended"]]
        targets = ranking[:max(1, a.count)]
    else:
        targets = [int(x) for x in a.version.split(",") if x.strip()]
        bad = [n for n in targets if n not in {v["n"] for v in rebuild["versions"]}]
        if bad:
            ap.error(f"없는 버전 {bad} — 있는 버전: {[v['n'] for v in rebuild['versions']]}")
    job.log(f"[cli] 렌더 대상 버전 {targets}")
    for n in targets:
        rb_for_table = rebuild
        if not a.no_verify:                                   # 4.5 영상 확인 패스 — 고정 구성(2026-09-10 사용자 결정)
            final = V.verify_version(job, gemini, rebuild, n, index, transcript, proxy, title=a.title, episode=a.episode, guide=guide,
                                     extra_exclude=range_exclude + preset_exclude, tag=a.tag, digest=digest)
            rb_for_table = {"versions": [final if v["n"] == n else v for v in rebuild["versions"]], "recommended": rebuild["recommended"]}
        ver = next(v for v in rb_for_table["versions"] if v["n"] == n)
        if R.apply_scene_order_version(ver, index, transcript, log=job.log):   # 장면 안 순서 벨트 — 캐시된 대본에도(멱등)
            vf = f"verified_v{n}{sfx}.json"
            if not a.no_verify and job.has(vf):
                doc = job.load(vf); doc["version"] = ver; job.save(vf, doc)
        if R.polish_character_names(gemini, ver, index, transcript, actors=(guide or {}).get("actors") or {}, digest=digest, log=job.log):
            vf = f"verified_v{n}{sfx}.json"                      # 인물 벨트 — 캐시된 대본에도(멱등): 장면에 없는 인물명을 고쳐 쓴다
            if not a.no_verify and job.has(vf):
                doc = job.load(vf); doc["version"] = ver; job.save(vf, doc)
        if guide:                                             # 가이드 벨트 — 캐시된 대본에도 지양 단어 문구 교정·배우 표기를 건다(멱등)
            if R.polish_guide(gemini, ver, guide, log=job.log) + R.apply_name_map(ver, guide.get("actors") or {}, log=job.log):
                vf = f"verified_v{n}{sfx}.json"
                if not a.no_verify and job.has(vf):
                    doc = job.load(vf)
                    doc["version"] = ver
                    job.save(vf, doc)
                elif a.no_verify:
                    job.save(rebuild_name, rebuild)
        if a.until == "verify":
            continue
        if a.pipeline == "grid-review":
            from app.tikitaka import grid_table
            table = grid_table.build_table(job, gemini, rb_for_table, index, transcript, cuts, info["duration_sec"], proxy,
                version_n=n, title=a.title, grid=grid, voice=a.voice, speed=a.speed, exclude=exclude, tag=a.tag, black=black,
                force="table" in redo, guide=guide)
        else:
            table = TB.build_table(job, gemini, rb_for_table, index, transcript, cuts, info["duration_sec"], proxy, version_n=n,
                                   title=a.title, cut_search=a.cut_search, voice=a.voice, speed=a.speed, exclude=exclude, tag=a.tag, black=black)
        job.path(f"master_table_v{n}{sfx}.md").write_text(RP.table_md(table, title=a.title), encoding="utf-8")
        if a.until == "table":
            continue
        if a.pipeline == "grid-review":
            from app.tikitaka import finish
            from app.v3.stage4 import get_style_preset
            template = get_style_preset("drama_clip")
            design = {k: template[k] for k in ("aspect_ratio", "video_y")}
            design["face_tracking"] = not a.no_framing
            if a.layout is not None:
                design["aspect_ratio"] = "5:6" if a.layout == "fill" else "16:9"
            if preset:
                design.update(preset["design"])
                if preset["options"].get("no_reframe"):
                    design["face_tracking"] = False
                if preset["options"].get("no_subtitles"):
                    design["subtitles"] = False
            if a.no_framing:
                design["face_tracking"] = False
            if design.get("platform_image"):
                from app.config import get_logo_path
                design["platform_image"] = get_logo_path(design["platform_image"], Path(__file__).resolve().parents[1])
            if logo:
                design.update(work_type="image", work_value=str(logo.resolve()),
                              work_image_width=600, work_image_height=240, work_image_align="center")
            if copy_text:
                if copy_text == design.get("platform_text"):
                    # The channel already places this same copy above its logo.
                    design.pop("work_caption", None)
                elif copy_pos == "below":
                    design["work_caption"] = copy_text
                else:
                    design.update(platform_text=copy_text, platform_placement="above_work")
            finish.run(job, table, grid, index, get_gemini=get_v3, design=design,
                       redo=bool(redo & {"review", "table"}), force_render="render" in redo,
                       force_style="style" in redo, exclude=exclude, tag=a.tag, split_narration=True)
            job.save(f"publish_v{n}{sfx}.json", {"title": table["version"]["title"], "work": a.title,
                "episode": a.episode, "hashtags": (guide or {}).get("hashtags") or [], "copy": copy_text,
                "review": f"review_v{n}{sfx}.json", "pipeline": "grid-review"})
            continue
        if not a.no_framing and a.layout == "fill":              # 5.5 화면 잡기(Gemini · 클로즈/투샷/와이드) — band 레이아웃은 크롭이 없다
            FR.frame_cuts(job, gemini, table, scene_cuts=cuts)   # 컷 안 샷 경계는 샷별로 따로 판정
        # 효과자막 배치는 렌더와 **같은** 기하를 봐야 한다 — 로고·카피가 아래 스택을 차지해 밴드 높이가 달라진다(2026-09-11 실측: 1034 vs 948)
        L = RD.compute_layout(a.layout, src_w=info["width"], src_h=info["height"], title=table["version"]["title"],
                              logo_size=RD._image_size(logo) if logo else None, copy_text=copy_text, copy_pos=copy_pos)
        EF.plan_effects(job, gemini, table, transcript, L, tag=a.tag)       # 5.6 효과자막 정밀 배치·타이밍
        RD.render(job, table, title=table["version"]["title"], layout=a.layout, work_title=a.title,
                  logo=logo, copy_text=copy_text, copy_pos=copy_pos, name_map=(guide or {}).get("actors") or None, tag=a.tag)
        job.save(f"publish_v{n}{sfx}.json", {"title": table["version"]["title"], "work": a.title, "episode": a.episode,
                                         "hashtags": (guide or {}).get("hashtags") or [], "copy": copy_text,
                                         "description": " ".join(filter(None, [table["version"]["title"], copy_text,
                                                                              " ".join((guide or {}).get("hashtags") or [])]))})
    if a.rerank == "verified" and not a.no_verify:                   # 최종본(확인 패스 뒤) 기준 재순위 — 렌더 순서에는 영향 없고 보고·추천만 갱신
        finals = []
        for v in rebuild["versions"]:
            vf = f"verified_v{v['n']}{sfx}.json"
            finals.append(job.load(vf)["version"] if job.has(vf) else v)
        data = dict(rebuild, versions=finals)
        R.apply_rerank(job, gemini, data, title=a.title, episode=a.episode, basis="verified")
        for key in ("ranking", "recommended", "reason", "rerank", "model_ranking"):
            if key in data:
                rebuild[key] = data[key]
        job.save(rebuild_name, rebuild)
        job.path(f"rebuild_versions{sfx}.md").write_text(RP.versions_md(rebuild, title=a.title), encoding="utf-8")
    job.save("llm_usage.json", gemini.usage.calls)
    if v3_client is not None:
        job.save("review_llm_usage.json", v3_client.usage.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
