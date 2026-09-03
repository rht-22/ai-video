"""쇼츠 서사 비트 라벨러 — 해부된 대본 표에 스토리 비트(hook→conflict→…)를 붙인다.

발주(2026-08-31): 스토리 템플릿을 「가왕쇼 6화 제작 해부」의 '스토리 해부 — 8비트'
방식으로 뽑는다. 내레이션 배치 유형(기계 분류)이 아니라 **서사 역할의 흐름**이다 —
이건 의미 판단이라 모델이 하되, 규율은 해부기와 같다:

  · 비트 어휘는 **닫혀 있고 v3 와 같다** — hook/conflict/context/build/climax/
    bridge/reaction/silent_break/ending (+ build 만 추가). 여기서 센 흐름이
    그대로 v3 story 프롬프트의 템플릿 어휘가 되게 하기 위해서다.
  · 모델은 **행 번호 구간만** 낸다(문구 재작성 금지 — 대본은 이미 있다).
  · 전 행 커버·순서·어휘를 검증하고, 어긋나면 사유를 붙여 1회 재질의.
  · 텍스트 온리 Flash, 한 호출에 여러 편(기본 6) — 영상을 다시 보지 않는다.

사용:
  python -m scripts.shorts_beats label --dir work/apn
  python -m scripts.shorts_beats report --dir work/apn
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BEATS = ["hook", "context", "conflict", "build", "climax",
         "reaction", "bridge", "silent_break", "ending"]
KO = {"hook": "훅", "context": "맥락", "conflict": "갈등", "build": "고조",
      "climax": "절정", "reaction": "반응", "bridge": "다리",
      "silent_break": "무대사 호흡", "ending": "엔딩"}
BATCH = 6
SILENCE_MIN = 1.5    # ⚠ 이보다 긴 행 사이 무음은 **행으로 주입**한다(2026-08-31 실측:
                     # 109/159편에 존재, 최대 10.4초). 발화 행만 주면 silent_break 를
                     # 붙일 대상이 아예 없어 — 첫 판이 그래서 무대사 호흡 0으로 나왔다.

PROMPT = """드라마 클립 쇼츠 대본 여러 편이다. 각 편의 행에 **서사 비트**를 붙여라.

## 비트 어휘 (이것만 · v3 파이프라인과 같은 이름)
- hook: 첫 후킹 — 사건 한복판·도발·궁금증. 대개 첫 행들.
- context: 인물·상황 세팅(누가 누구인지, 무슨 상황인지).
- conflict: 갈등의 제시 — 대치·추궁·문제 발생.
- build: 고조 — 갈등이 커지거나 판이 조여든다.
- climax: 정점 — 폭로·한 방·결정적 순간.
- reaction: 정점에 대한 반응·수습·여파.
- bridge: 시간·장소·국면 전환의 다리.
- silent_break: 대사 없는 호흡. **(무음 N초) 행이 그 후보다** — 다만 무음이 전환의
  다리면 bridge, 정점 직전 뜸들이기면 build 에 넣어도 된다. 기계적으로 붙이지 마라.
- ending: 마지막 마무리 — 펀치 대사·떡밥·닫는 내레이션.

## 규칙
- 행 번호 구간 [i0, i1] 로만 답한다. **0번부터 마지막 행까지 빠짐·겹침 없이** 순서대로.
- 한 비트 구간은 연속 행이다. 같은 비트가 떨어져 두 번 나와도 된다(예: conflict 두 번).
- 문구를 다시 쓰지 마라 — 구간과 비트 이름만.
- 억지로 8비트를 다 쓰지 마라. 그 편에 있는 비트만.

## 입력
{scripts}

## 출력 (JSON 만)
{{"shorts": [{{"id": "<id>", "beats": [{{"i0": 0, "i1": 2, "role": "hook"}},
                                      {{"i0": 3, "i1": 7, "role": "conflict"}}]}}]}}"""


def augment_rows(table: list[dict], dur: float) -> list[dict]:
    """발화 행 사이 SILENCE_MIN 이상 무음을 (무음) 행으로 끼운다 — silent_break 의 자리."""
    out = []
    for k, r in enumerate(table):
        if k:
            g = r["t0"] - table[k-1]["t1"]
            if g >= SILENCE_MIN:
                out.append({"kind": "silence", "t0": table[k-1]["t1"], "dur": g})
        out.append(r)
    if table and dur - table[-1]["t1"] >= SILENCE_MIN:
        out.append({"kind": "silence", "t0": table[-1]["t1"], "dur": dur - table[-1]["t1"]})
    return out


def script_block(sid: str, rows: list[dict]) -> str:
    lines = [f"### {sid}"]
    for i, r in enumerate(rows):
        if r["kind"] == "silence":
            lines.append(f"{i} | (무음 {r['dur']:.1f}초 — 대사 없는 화면)")
        elif r["kind"] == "narration":
            lines.append(f"{i} | 내레이션 | 「{r['text'][:60]}」")
        else:
            lines.append(f"{i} | {r.get('speaker') or '?'} | {r['text'][:60]}")
    return "\n".join(lines)


def validate(out: dict, batch: dict[str, int]) -> list[str]:
    problems = []
    got = {s.get("id"): s for s in (out.get("shorts") or [])}
    for sid, nrows in batch.items():
        s = got.get(sid)
        if not s:
            problems.append(f"{sid}: 응답에 없음")
            continue
        cur = 0
        for b in s.get("beats") or []:
            if b.get("role") not in BEATS:
                problems.append(f"{sid}: 비트 어휘 밖 {b.get('role')!r}")
                break
            if b.get("i0") != cur:
                problems.append(f"{sid}: 행 {cur} 에서 끊김(빠짐·겹침 금지)")
                break
            cur = b.get("i1", -1) + 1
        else:
            if cur != nrows:
                problems.append(f"{sid}: {cur}/{nrows}행만 덮음")
    return problems


def cmd_label(args: argparse.Namespace) -> int:
    from app.modules.gemini_client import load_gemini_client, _extract_json_from_markdown

    d = Path(args.dir)
    outdir = d / "beats"
    outdir.mkdir(parents=True, exist_ok=True)
    docs = []
    for f in sorted(glob.glob(str(d / "anatomy" / "*.json"))):
        a = json.load(open(f))
        if not (outdir / f"{a['shorts_id']}.json").exists() or args.force:
            if a["table"]:
                a["_rows"] = augment_rows(a["table"], a["measured"]["duration_sec"])
                docs.append(a)
    print(f"[beats] 대상 {len(docs)}편")
    g = load_gemini_client()
    ok = fail = 0
    for k in range(0, len(docs), BATCH):
        chunk = docs[k:k + BATCH]
        batch = {a["shorts_id"]: len(a["_rows"]) for a in chunk}
        prompt = PROMPT.format(scripts="\n\n".join(
            script_block(a["shorts_id"], a["_rows"]) for a in chunk))
        reject = ""
        for attempt in range(2):
            resp = g.client.models.generate_content(
                model=g.config.flash_model_name,
                contents=[prompt + reject],
                config=g.types.GenerateContentConfig(
                    response_mime_type="application/json"))
            try:
                out = json.loads(_extract_json_from_markdown(resp.text.strip()))
            except Exception as e:  # noqa: BLE001
                reject = f"\n\n## ⚠ 직전 응답이 JSON 파싱 실패({e}) — 다시"
                continue
            problems = validate(out, batch)
            if not problems:
                break
            reject = "\n\n## ⚠ 직전 응답 반려 — 고쳐서 전부 다시\n" + "\n".join(problems)
        else:
            print(f"  ✗ 배치 {k//BATCH}: {problems[:2]}")
            fail += len(chunk)
            continue
        rows_by = {a["shorts_id"]: a["_rows"] for a in chunk}
        for s in out["shorts"]:
            s["rows"] = [{k: r.get(k) for k in ("kind", "t0", "t1", "dur")}
                         for r in rows_by.get(s["id"], [])]
            (outdir / f"{s['id']}.json").write_text(
                json.dumps(s, ensure_ascii=False), encoding="utf-8")
        ok += len(chunk)
        print(f"  ✓ 배치 {k//BATCH + 1}/{(len(docs)+BATCH-1)//BATCH} ({ok}편)")
    print(f"[beats] 성공 {ok} · 실패 {fail} → {outdir}")
    return 0


def signature(beats: list[dict]) -> tuple:
    """연속 중복을 접은 비트열 — 템플릿 서명."""
    sig = []
    for b in beats:
        if not sig or sig[-1] != b["role"]:
            sig.append(b["role"])
    return tuple(sig)


def cmd_report(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    sigs = Counter()
    for f in glob.glob(str(d / "beats" / "*.json")):
        s = json.load(open(f))
        sigs[signature(s["beats"])] += 1
    n = sum(sigs.values())
    print(f"[beats] {n}편 · 서명 {len(sigs)}종\n")
    for sig, c in sigs.most_common(14):
        print(f"  {c:3}편 {c/n*100:4.0f}%  " + " → ".join(KO[r] for r in sig))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shorts_beats")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("label"); a.add_argument("--dir", default="work/apn")
    a.add_argument("--force", action="store_true"); a.set_defaults(fn=cmd_label)
    r = sub.add_parser("report"); r.add_argument("--dir", default="work/apn")
    r.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
