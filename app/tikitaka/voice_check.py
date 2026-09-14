"""3.5단계 — 목소리 기준 화자 대조(2026-09-11, 사용자 지적 "v1 자막과 배우가 잘못 매핑" — 폐건물 장면 5줄이 통째로 안수정↔박경희 뒤바뀜).

인덱스(3단계)는 10분 창을 1fps 저해상으로 보고 화자를 배정하므로 어두운 장면에서 배우를 통째로 바꿔 적을 수 있다. 여기서는 같은 오디오를
ElevenLabs Scribe 에 **화자 분리(diarize)** 로 다시 보내 단어마다 목소리 ID 를 받고, 줄마다 목소리 ID(겹침 시간 다수)를 붙인 뒤,
**같은 목소리 클러스터 안에서 인덱스가 붙인 이름의 다수결**과 다른 줄을 그 이름으로 고친다(클러스터 크기·다수 비율 조건). 전사 줄 ID·시각은
그대로다(전사를 갈아끼우지 않는다 — 대본이 줄 ID 를 가리킨다). 목소리 ID 는 **창(10분)마다 새로 매겨지므로** 대조도 창 안에서만 한다.
산출: transcript.json 줄 `voice`·`speaker`(교정) · index.json speakers · `voice_check.json`(클러스터·교정 목록). 캐시 `diarize_windows/`.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from app.tikitaka.common import Job, find_bin, fmt_tc

WINDOW_SEC = None        # None = 파일 전체를 한 요청으로(목소리 ID 가 회차 전체에서 같다). 창으로 나누면 ID 가 창마다 새로 매겨져
                         # 창 안 다수결이 인덱스 오류(창4: 박경희 9 vs 오표기 21)에 진다 — 2026-09-11 실측
MIN_CLUSTER = 5          # 이름 붙은 줄이 이보다 적은 클러스터는 판단하지 않는다
MIN_SHARE = 0.7          # 다수 이름 비율이 이보다 낮으면(두 목소리가 섞인 클러스터) 판단하지 않는다
AMBIGUOUS_SHARE = 0.3   # 한 클러스터에서 이름 둘이 각각 이 비율 이상이면 '두 이름' 경고
MAX_OTHER_SHARE = 0.25   # 고칠 줄의 현재 이름이 클러스터에서 이보다 많이 나오면 '섞임'으로 보고 손대지 않는다
MIN_OVERLAP = 0.3        # 줄과 목소리 단어의 겹침이 이 비율 미만이면 목소리 미상
UNKNOWN = ("미상", "", None)


def diarize_windows(job: Job, audio_path: Path, *, window: float | None = WINDOW_SEC) -> list[dict]:
    """Scribe(diarize=true) → [{start, end, voice}] 절대초. window=None 이면 파일 전체 한 요청(voice = "g:{speaker_id}", 캐시 full.json),
    아니면 창마다(voice = "w{창}:{speaker_id}" — 창을 넘어 같은 사람이 아니다)."""
    from app.modules import stt_elevenlabs as EL
    api_key = EL.ensure_api_key()
    dur = float(job.load("probe.json")["duration_sec"])
    ffmpeg = find_bin("ffmpeg")
    wdir = job.path("diarize_windows")
    wdir.mkdir(exist_ok=True)
    out: list[dict] = []
    if window is None:
        cache = wdir / "full.json"
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
        else:
            payload = EL._post_speech_to_text(audio_path, api_key, language="ko", keyterms=None, is_raw=False, diarize=True)
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for w in payload.get("words") or []:
            if w.get("type", "word") != "word" or w.get("speaker_id") is None:
                continue
            out.append({"start": float(w["start"]), "end": float(w["end"]), "voice": f"g:{w['speaker_id']}"})
        job.log(f"[voice] 전체 {dur:.0f}s 한 요청 · 목소리 단어 {len(out)} · 화자 {len({x['voice'] for x in out})}")
        out.sort(key=lambda x: x["start"])
        return out
    t, k = 0.0, 0
    while t < dur - 0.5:
        e = min(dur, t + window)
        if dur - e < 60.0:
            e = dur
        cache = wdir / f"win_{k:02d}.json"
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
        else:
            seg = wdir / f"win_{k:02d}.wav"
            subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{t:.3f}", "-t", f"{e-t:.3f}", "-i", str(audio_path),
                            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(seg)], check=True)
            payload = EL._post_speech_to_text(seg, api_key, language="ko", keyterms=None, is_raw=False, diarize=True)
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            seg.unlink(missing_ok=True)
        n = 0
        for w in payload.get("words") or []:
            if w.get("type", "word") != "word" or w.get("speaker_id") is None:
                continue
            out.append({"start": float(w["start"]) + t, "end": float(w["end"]) + t, "voice": f"w{k}:{w['speaker_id']}"})
            n += 1
        job.log(f"[voice] 창{k:02d} {t:.0f}~{e:.0f}s 목소리 단어 {n} · 화자 {len({x['voice'] for x in out if x['voice'].startswith(f'w{k}:')})}")
        t = e
        k += 1
    out.sort(key=lambda x: x["start"])
    return out


def assign_voices(lines: list[dict], diar: list[dict], *, min_overlap: float = MIN_OVERLAP) -> int:
    """줄마다 겹침 시간이 가장 긴 목소리 ID 를 `voice` 로(제자리). 겹침이 줄 길이의 min_overlap 미만이면 None. 순수 — 테스트 대상."""
    n = 0
    j = 0
    for l in sorted(lines, key=lambda x: x["start"]):
        while j < len(diar) and diar[j]["end"] < l["start"]:
            j += 1
        acc: dict[str, float] = defaultdict(float)
        k = j
        while k < len(diar) and diar[k]["start"] < l["end"]:
            ov = min(l["end"], diar[k]["end"]) - max(l["start"], diar[k]["start"])
            if ov > 0:
                acc[diar[k]["voice"]] += ov
            k += 1
        if acc:
            v, ov = max(acc.items(), key=lambda kv: kv[1])
            if ov >= min_overlap * max(0.2, l["end"] - l["start"]):
                l["voice"] = v
                n += 1
                continue
        l["voice"] = None
    return n


def speaker_consistency(lines: list[dict], *, min_cluster: int = MIN_CLUSTER, min_share: float = MIN_SHARE,
                        max_other_share: float = MAX_OTHER_SHARE) -> tuple[dict[str, str], list[dict]]:
    """목소리 클러스터별 인덱스 이름 다수결 → 다수와 다른 줄의 교정 {줄ID: 이름} + 클러스터 보고. 순수 — 테스트 대상.
    조건: 이름 붙은 줄(미상 제외) ≥ min_cluster · 다수 비율 ≥ min_share · 고칠 줄의 현재 이름 비율 ≤ max_other_share(두 목소리가 한
    클러스터로 합쳐진 경우 소수 화자의 줄을 통째로 뒤집지 않는다). 미상 줄은 조건만 맞으면 다수 이름을 받는다."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for l in lines:
        if l.get("voice"):
            groups[l["voice"]].append(l)
    stats: dict[str, tuple[Counter, int]] = {}
    for voice, ls in groups.items():
        named = [l for l in ls if l.get("speaker") not in UNKNOWN]
        stats[voice] = (Counter(l["speaker"] for l in named), len(named))
    # 이름의 '홈 클러스터' — 그 이름이 다수(조건 충족)인 클러스터가 따로 있으면 그 목소리를 화자 분리가 구분한 것이다.
    # 홈이 있는 이름이 남의 클러스터에 붙어 있으면 인덱스 오류(예: 박경희 목소리에 안수정 40줄). 홈이 없는 이름(주민서·하실장처럼 화자 분리가
    # 남의 목소리에 합친 조연)은 뒤집지 않는다 — 실제 그 사람의 대사일 수 있다.
    home: set[str] = set()
    for voice, (cnt, n_named) in stats.items():
        if cnt and n_named >= min_cluster:
            top, top_n = cnt.most_common(1)[0]
            if top_n / n_named >= min_share:
                home.add(top)
    fixes: dict[str, str] = {}
    report: list[dict] = []
    for voice, ls in groups.items():
        cnt, n_named = stats[voice]
        top, top_n = (cnt.most_common(1)[0] if cnt else (None, 0))
        share = top_n / n_named if n_named else 0.0
        entry = {"voice": voice, "lines": len(ls), "names": dict(cnt), "unknown": sum(1 for l in ls if l.get("speaker") in UNKNOWN),
                 "majority": top, "share": round(share, 2), "fixed": [], "skipped": {}}
        # 한 목소리에 이름 둘이 각각 30% 이상 = 인덱스가 두 인물을 창마다 바꿔 적었을 가능성(2026-09-13 3화: 안수정 99·박경희 69).
        # 어느 쪽이 맞는지는 화면을 봐야 알므로 자동 교정하지 않고 크게 남긴다 — 사람이 프레임을 보고 클러스터 기준으로 고친다.
        if n_named >= min_cluster:
            big = [nm for nm, c in cnt.most_common(2) if c / n_named >= AMBIGUOUS_SHARE]
            if len(big) >= 2:
                entry["ambiguous"] = big
        if top and n_named >= min_cluster and share >= min_share:
            for l in ls:
                cur = l.get("speaker")
                if cur == top:
                    continue
                if cur not in UNKNOWN and (cnt.get(cur, 0) / n_named > max_other_share or cur not in home):
                    entry["skipped"][cur] = entry["skipped"].get(cur, 0) + 1
                    continue
                fixes[l["id"]] = top
                entry["fixed"].append(l["id"])
        report.append(entry)
    return fixes, report


def voice_check(job: Job, transcript: dict, index: dict, audio_path: Path, *, dry_run: bool = False) -> dict:
    """전 단계를 묶는다(캐시 `voice_check.json` — 있으면 교정은 이미 적용된 상태). 교정은 transcript.json·index.json 에 저장.
    dry_run 이면 교정 목록·클러스터만 돌려주고 아무것도 저장하지 않는다."""
    if job.has("voice_check.json") and not dry_run:
        return job.load("voice_check.json")
    lines = transcript["lines"]
    diar = diarize_windows(job, audio_path)
    n_voice = assign_voices(lines, diar)
    fixes, report = speaker_consistency(lines)
    for e in report:
        if e.get("ambiguous"):
            job.log(f"[voice] ⚠⚠ 한 목소리에 두 이름 {e['voice']}: {e['names']} — 인덱스가 두 인물을 바꿔 적었을 수 있다. 프레임을 보고 "
                    f"클러스터 기준으로 화자를 고친 뒤 --redo digest (자동 교정 안 함)")
    if dry_run:
        return {"voiced_lines": n_voice, "lines": len(lines), "fixes": fixes, "clusters": report}
    by = {l["id"]: l for l in lines}
    for lid, name in fixes.items():
        by[lid]["speaker_index"] = by[lid].get("speaker")
        by[lid]["speaker"] = name
        index.setdefault("speakers", {})[lid] = name
    # 장면 등장인물도 교정된 줄 기준으로 보강(장면 chars 에 없던 이름 추가)
    for sc in index.get("scenes") or []:
        names = {by[lid]["speaker"] for lid in fixes if sc["start"] <= by[lid]["start"] < sc["end"]}
        for nm in names:
            if nm not in (sc.get("chars") or []):
                sc.setdefault("chars", []).append(nm)
    job.save("transcript.json", transcript)
    job.save("index.json", index)
    res = {"voiced_lines": n_voice, "lines": len(lines), "fixes": fixes, "clusters": report}
    job.save("voice_check.json", res)
    for lid, name in list(fixes.items())[:20]:
        job.log(f"[voice]   {lid} {fmt_tc(by[lid]['start'])} {by[lid].get('speaker_index')} → {name}: {by[lid]['text'][:30]!r}")
    job.log(f"[voice] 완료 — 목소리 붙은 줄 {n_voice}/{len(lines)} · 클러스터 {len(report)} · 화자 교정 {len(fixes)}줄")
    job.record_step("voice_check", voiced=n_voice, lines=len(lines), fixes=len(fixes), clusters=len(report))
    return res
