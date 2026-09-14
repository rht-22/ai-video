"""제작 가이드 — 작품·회차별 제작 지침(2026-09-11 사용자 요청 "각 작품과 회차에 맞는 가이드를 참고해서 제작").

가이드는 마크다운/텍스트 파일 하나 이상이다. 본문은 **그대로** 대본을 쓰는 프롬프트(리빌딩·영상 확인 패스·문구 다듬기)에
"[제작 가이드]" 절로 들어가고, 아래 **구조 키** 줄은 코드가 읽어 기계적으로 강제·설정한다(줄 맨 앞, `키: 값`):

  지양 단어: 콘돔, 피임          ← 제목·내레이션·효과자막에서 걸리면 다듬기 호출로 고쳐 쓰고, 이 단어가 든 대사 줄(S)은 드롭한다
  로고: assets/logo.png          ← 작품명 대신 이 이미지(가이드 파일 위치 기준 상대경로 가능)
  카피: 풀 영상은 쿠팡플레이에서 시청하세요
  카피 위치: 아래                ← 위|아래 (로고/작품명 기준)
  활용 불가: 22:06~22:50 (호텔 씬) / 44:00~엔딩 (반전 대사)   ← 원본 절대 시각. 그 구간의 대사·순간은 소스 스크립트에서 빠지고
                                  S/A 항목·N 컷 어디에도 못 쓴다(코드 강제). "없음" 이면 무시. 회차 파일에 두는 것이 보통
  해시태그: #쿠팡플레이 #지금불륜이문제가아닙니다  ← 발행 메모(publish_v{n}.json)에 실린다
  배우: 박경희=김혜수, 안수정=조여정        ← 극중 이름 → 배우 이름. 제목·내레이션·효과자막(프롬프트+치환 벨트)과 자막 화자 라벨에 적용

자동 탐색: `guides/tikitaka/<작품명>.md` + `guides/tikitaka/<작품명>/<회차>.md` (둘 다 있으면 둘 다 — 회차 파일이 뒤에 와서
같은 키는 회차가 이긴다). CLI `--guide` 를 주면 그 파일들만 쓴다. CLI `--logo/--copy/--copy-pos` 는 가이드 키보다 우선한다.
가이드가 없으면 프롬프트는 종전과 바이트 동일하다(guide_block 이 빈 문자열).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE_DIR = REPO_ROOT / "guides" / "tikitaka"

_KEY_RE = re.compile(r"^\s*(?:[-*]\s*)?(지양\s*단어|금지어|피할\s*단어|로고|카피\s*문구|카피\s*위치|카피|활용\s*불가|제외\s*구간|해시태그|배우|배우\s*표기)\s*[:：]\s*(.+?)\s*$")
_SPLIT_RE = re.compile(r"\s*[,、/]\s*")
_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\s*[~\-–]\s*(엔딩|끝|end|(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?)", re.I)


def _tc(mm: str, ss: str, ms: str | None) -> float:
    return int(mm) * 60 + int(ss) + (int((ms or "0").ljust(3, "0")[:3]) / 1000.0)


def parse_ranges(val: str) -> list[dict]:
    """`22:06~22:50 (호텔 씬) / 44:00~엔딩 (반전 대사)` → [{start, end(None=끝까지), note}]. 시각은 원본 절대초. 순수 — 테스트 대상."""
    out: list[dict] = []
    for part in re.split(r"\s*/\s*|\s*;\s*", val):
        m = _RANGE_RE.search(part)
        if not m:
            continue
        start = _tc(m.group(1), m.group(2), m.group(3))
        end = None if m.group(5) is None else _tc(m.group(5), m.group(6), m.group(7))
        note = part[m.end():].strip(" ()（）[]-–—:")
        if end is not None and end <= start:
            continue
        out.append({"start": round(start, 3), "end": (round(end, 3) if end is not None else None), "note": note})
    return out


def parse_guide_text(text: str) -> dict:
    """가이드 본문 → {avoid: [...], logo, copy, copy_pos}. 구조 키 줄만 읽고 나머지는 본문(프롬프트용)이다. 순수 — 테스트 대상."""
    out: dict = {"avoid": [], "logo": None, "copy": None, "copy_pos": None, "exclude": [], "hashtags": [], "actors": {}}
    for line in text.splitlines():
        m = _KEY_RE.match(line)
        if not m:
            continue
        key = re.sub(r"\s+", "", m.group(1))
        val = m.group(2).strip()
        if key in ("지양단어", "금지어", "피할단어"):
            out["avoid"].extend(w.strip(" '\"") for w in _SPLIT_RE.split(val) if w.strip(" '\""))
        elif key == "로고":
            out["logo"] = val
        elif key in ("카피", "카피문구"):
            out["copy"] = val.strip("'\"")
        elif key == "카피위치":
            v = val.lower()
            out["copy_pos"] = "above" if v.startswith(("위", "above", "top")) else "below" if v.startswith(("아래", "below", "bottom")) else None
        elif key in ("활용불가", "제외구간"):
            if not val.startswith(("없음", "해당 사항 없음", "해당사항 없음")):
                out["exclude"].extend(parse_ranges(val))
        elif key == "해시태그":
            out["hashtags"].extend(t if t.startswith("#") else f"#{t}" for t in re.split(r"[\s,]+", val) if t.strip("#"))
        elif key in ("배우", "배우표기"):                       # "박경희=김혜수, 안수정=조여정" — 극중 이름 → 배우 이름
            for pair in _SPLIT_RE.split(val):
                if "=" in pair or "→" in pair or ":" in pair:
                    ch, ac = re.split(r"\s*(?:=|→|:)\s*", pair, maxsplit=1)
                    if ch.strip() and ac.strip():
                        out["actors"][ch.strip()] = ac.strip()
    seen: list[str] = []
    for w in out["avoid"]:
        if w not in seen:
            seen.append(w)
    out["avoid"] = seen
    return out


def discover_guides(title: str, episode: str = "", base: Path = GUIDE_DIR) -> list[Path]:
    """작품명·회차로 가이드 파일을 찾는다(있는 것만, 작품 → 회차 순)."""
    found: list[Path] = []
    for cand in (base / f"{title}.md", base / f"{title}.txt"):
        if cand.exists():
            found.append(cand)
            break
    if episode:
        labels = [str(episode)]
        match = re.fullmatch(r"(\d+)(?:회|화)?", str(episode))
        if match:
            labels += [match[1], match[1]+"화", match[1]+"회"]
        for cand in (base / title / f"{label}{ext}" for label in dict.fromkeys(labels) for ext in (".md", ".txt")):
            if cand.exists():
                found.append(cand)
                break
    return found


def load_guides(paths: list[Path]) -> dict | None:
    """가이드 파일들 → 하나로 합친 dict {text, avoid, logo, copy, copy_pos, files, sha}. 파일이 없으면 None.
    로고 상대경로는 그 가이드 파일 위치 기준으로 푼다."""
    if not paths:
        return None
    texts: list[str] = []
    merged: dict = {"avoid": [], "logo": None, "copy": None, "copy_pos": None, "exclude": [], "hashtags": [], "actors": {}, "files": [], "sha": ""}
    h = hashlib.sha1()
    for p in paths:
        p = Path(p)
        raw = p.read_text(encoding="utf-8")
        h.update(raw.encode("utf-8"))
        texts.append(f"### {p.stem}\n{raw.strip()}")
        part = parse_guide_text(raw)
        for w in part["avoid"]:
            if w not in merged["avoid"]:
                merged["avoid"].append(w)
        if part["logo"]:
            lp = Path(part["logo"]).expanduser()
            merged["logo"] = str(lp if lp.is_absolute() else (p.parent / lp).resolve())
        if part["copy"]:
            merged["copy"] = part["copy"]
        if part["copy_pos"]:
            merged["copy_pos"] = part["copy_pos"]
        merged["exclude"].extend(part["exclude"])
        merged["actors"].update(part["actors"])
        for t in part["hashtags"]:
            if t not in merged["hashtags"]:
                merged["hashtags"].append(t)
        merged["files"].append(str(p))
    merged["text"] = "\n\n".join(texts)
    merged["sha"] = h.hexdigest()[:12]
    return merged


def guide_block(guide: dict | None) -> str:
    """프롬프트에 붙일 절. 가이드가 없으면 빈 문자열(프롬프트 바이트 동일)."""
    if not guide or not guide.get("text"):
        return ""
    avoid = guide.get("avoid") or []
    lines = ["", "", "## [제작 가이드 — 이 작품·회차 전용 지침. 반드시 따른다]", guide["text"].strip()]
    if avoid:
        lines.append(f"- **지양 단어**: {', '.join(avoid)} — 제목·내레이션·효과자막에 쓰지 말고, 이 단어가 든 원본 대사 줄(L-xxx)도 고르지 마라. "
                     "꼭 그 상황을 말해야 하면 직접 지칭하지 않는 완곡한 표현으로 돌려 말한다.")
    if guide.get("copy"):
        lines.append(f"- 카피 문구 \"{guide['copy']}\" 는 화면 하단에 이미 박혀 나간다 — 내레이션·제목·효과자막에 다시 쓰지 마라.")
    actors = guide.get("actors") or {}
    if actors:
        lines.append("- **인물 표기는 배우 이름으로**: " + ", ".join(f"{c}→{a}" for c, a in actors.items()) +
                     " — 제목·내레이션·효과자막에서 극중 이름 대신 배우 이름을 쓴다(자막 화자 라벨도 배우 이름으로 나간다). "
                     "목록에 없는 인물은 극중 이름 그대로.")
    ex = guide.get("exclude") or []
    if ex:
        from app.tikitaka.common import fmt_tc
        spans = ", ".join(f"{fmt_tc(r['start'])}~{fmt_tc(r['end']) if r['end'] is not None else '끝'}" + (f"({r['note']})" if r.get("note") else "")
                          for r in ex)
        lines.append(f"- **활용 불가 구간(원본 절대 시각)**: {spans} — 이 구간의 대사·순간·장면은 소스 스크립트에서 이미 뺐고, "
                     "어떤 항목(S/A/N 컷)에도 쓰면 안 된다. 그 구간의 내용을 내레이션으로 설명하지도 마라.")
    return "\n".join(lines)


def excluded_ranges(guide: dict | None, duration: float) -> list[tuple[float, float]]:
    """가이드의 활용 불가 구간 → [(start, end)] 절대초('끝'은 duration). 없으면 []."""
    out: list[tuple[float, float]] = []
    for r in (guide or {}).get("exclude") or []:
        end = float(duration) if r.get("end") is None else float(r["end"])
        if end > float(r["start"]):
            out.append((float(r["start"]), end))
    return sorted(out)


def in_excluded(start: float, end: float, ranges: list[tuple[float, float]]) -> bool:
    """[start, end) 가 활용 불가 구간과 조금이라도 겹치는가."""
    return any(start < e and end > s for s, e in ranges)


def avoid_hits(texts: list[str], avoid: list[str]) -> list[tuple[str, str]]:
    """(지양 단어, 그 단어가 든 문구) 목록 — 부분 문자열 일치(한국어는 어절 경계가 없다). 순수 — 테스트 대상."""
    hits: list[tuple[str, str]] = []
    for t in texts:
        for w in avoid:
            if w and w in (t or ""):
                hits.append((w, t))
    return hits


def ranges_complement(include: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """재료 구간(include)의 **여집합** — `--range` 는 이 여집합을 활용 불가 구간과 합쳐 같은 배제 기계를 탄다. 순수 — 테스트 대상."""
    out: list[tuple[float, float]] = []
    t = 0.0
    for a, b in sorted((max(0.0, a), min(float(duration), b)) for a, b in include if b > a):
        if a > t:
            out.append((t, a))
        t = max(t, b)
    if t < duration:
        out.append((t, float(duration)))
    return out
