#!/usr/bin/env python3
"""폰트가 실제로 그릴 수 있는 글자인가 — cmap 직접 조회 (2026-09-01).

발단: AI 연출(E15)이 텔롭에 이모지를 썼고("즉석 라이브 폭발 💥") 화면에 두부(⊠)로
나갔다. 제목에는 이모지 차단이 이중으로 있었지만(STORY_COMPOSITION_PROMPT 의
"이모지는 사용하지 않음" + pipeline._strip_emoji) **연출 텔롭에는 한 겹도 없었다** —
STYLE_COMPOSITION_PROMPT 도, style_compose 검증기도 글자 내용을 안 본다.

왜 정규식이 아니라 cmap 인가:
  · 이모지 범위를 정규식으로 지우면 살려야 할 것까지 지운다. 번들 폰트 실측(아래)로
    ★(2605)·♪(266A)·‼(203C)는 Jalnan 계열에 **있다** — 쓸 수 있는 기호다. 그런데
    _strip_emoji 의 U+2600–26FF·U+2702–27B0 은 ♪ 를 통째로 삼킨다.
  · 반대로 Griun 은 ★·♪ 마저 없다. "이모지만 막으면 된다"가 애초에 틀린 명제다 —
    막아야 하는 것은 **이 폰트에 글리프가 없는 글자**이고, 그건 폰트마다 다르다.

    ┌──────────────────┬──────────────────┬───────────┐
    │                  │ 💥🔥⚡😂✨❗⭐ │ ★ ♪ ‼    │
    ├──────────────────┼──────────────────┼───────────┤
    │ Jalnan·JalnanG   │ 전부 없음        │ 셋 다 있음│
    │ mulmaru          │ 전부 없음        │ ★·♪ 만    │
    │ Griun            │ 전부 없음        │ 전부 없음 │
    └──────────────────┴──────────────────┴───────────┘

왜 fontTools 를 안 쓰나: 엔진은 6대 맥에 배포된다. 렌더 경로에 새 의존성을 넣으면
배포가 밀리는 동안 노드마다 동작이 갈린다(스모크 하나가 전면 정지를 부르는 구조다).
cmap 은 표준이고 우리가 읽을 부분은 서브테이블 네 종뿐이라 표준 라이브러리로 충분하다.

렌더가 실제로 쓰는 경로와 같은 파일을 본다(config.get_font_path) — 이름만 보고
판단하면 fontconfig 폴백과 어긋나 '검사는 통과했는데 화면은 두부'가 된다.
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path
from typing import Any

# 폰트에 없어도 정상인 글자 — 지우면 안 된다. 줄바꿈은 제목의 2줄 위계이고(엔진 계약),
# 탭·캐리지리턴은 입력에 섞여 들어올 수 있다. 공백은 대개 폰트에 있지만 함께 지킨다.
ALWAYS_OK = frozenset({0x09, 0x0A, 0x0D, 0x20})


# ───────── cmap 파싱 (파일 I/O — 캐시된다) ─────────
@lru_cache(maxsize=32)
def charset(font_path: str) -> frozenset[int]:
    """폰트 파일 → 실제로 그릴 수 있는 유니코드 코드포인트 집합.

    글리프 0(.notdef)으로 매핑되는 코드포인트는 **뺀다** — cmap 세그먼트 안에 있어도
    그리면 두부다. 읽기에 실패하면 빈 집합이 아니라 **None 대신 전체 허용**을 뜻하는
    센티넬(ANY)을 쓰지 않고 빈 집합을 돌려주지 않는다: 아래 missing_chars 가
    '빈 집합 = 판정 불가'로 읽어 통과시킨다(폰트를 못 읽었다고 문구를 지우면 안 된다)."""
    try:
        data = Path(font_path).read_bytes()
    except OSError:
        return frozenset()
    try:
        return frozenset(_parse_cmap(data))
    except (struct.error, IndexError, ValueError):
        return frozenset()      # 판정 불가 — 호출부가 통과시킨다(fail-open)


def _parse_cmap(data: bytes) -> set[int]:
    """sfnt → cmap 의 모든 서브테이블을 합친 코드포인트 집합. 순수(바이트 입력)."""
    off = 0
    if data[:4] == b"ttcf":                       # 컬렉션은 첫 폰트만 본다
        off = struct.unpack_from(">I", data, 12)[0]
    num_tables = struct.unpack_from(">H", data, off + 4)[0]
    cmap_off = None
    for i in range(num_tables):
        rec = off + 12 + 16 * i
        if data[rec:rec + 4] == b"cmap":
            cmap_off = struct.unpack_from(">I", data, rec + 8)[0]
            break
    if cmap_off is None:
        return set()
    out: set[int] = set()
    for i in range(struct.unpack_from(">H", data, cmap_off + 2)[0]):
        rec = cmap_off + 4 + 8 * i
        sub = cmap_off + struct.unpack_from(">I", data, rec + 4)[0]
        fmt = struct.unpack_from(">H", data, sub)[0]
        if fmt == 0:
            out |= {c for c in range(256) if data[sub + 6 + c]}
        elif fmt == 4:
            out |= _fmt4(data, sub)
        elif fmt == 6:
            first, count = struct.unpack_from(">HH", data, sub + 6)
            out |= {first + k for k in range(count)
                    if struct.unpack_from(">H", data, sub + 10 + 2 * k)[0]}
        elif fmt == 12:
            for g in range(struct.unpack_from(">I", data, sub + 12)[0]):
                s, e, _gid = struct.unpack_from(">III", data, sub + 16 + 12 * g)
                if e >= s and e - s < 0x110000:
                    out |= set(range(s, e + 1))
    return out


def _fmt4(data: bytes, sub: int) -> set[int]:
    """cmap 포맷 4 — idRangeOffset·idDelta 까지 풀어 **글리프 0 을 제외**한다.

    세그먼트 범위만 보고 판정하면 안 된다: 한 세그먼트가 미매핑 구멍을 품을 수 있고,
    그 구멍의 글자는 그리면 두부다(우리가 잡으려는 바로 그 증상)."""
    seg_x2 = struct.unpack_from(">H", data, sub + 6)[0]
    seg = seg_x2 // 2
    ends = sub + 14
    starts = ends + seg_x2 + 2
    deltas = starts + seg_x2
    ranges = deltas + seg_x2
    out: set[int] = set()
    for k in range(seg):
        end = struct.unpack_from(">H", data, ends + 2 * k)[0]
        start = struct.unpack_from(">H", data, starts + 2 * k)[0]
        delta = struct.unpack_from(">h", data, deltas + 2 * k)[0]
        r_off = struct.unpack_from(">H", data, ranges + 2 * k)[0]
        if start > end or start == 0xFFFF:
            continue
        for cp in range(start, end + 1):
            if r_off == 0:
                gid = (cp + delta) & 0xFFFF
            else:
                gi = ranges + 2 * k + r_off + 2 * (cp - start)
                if gi + 2 > len(data):
                    continue
                gid = struct.unpack_from(">H", data, gi)[0]
                if gid:
                    gid = (gid + delta) & 0xFFFF
            if gid:
                out.add(cp)
    return out


def font_charset(font_name: str, app_root: Path) -> frozenset[int]:
    """폰트 이름(파일명 stem·한글명) → 코드포인트 집합. 렌더와 **같은 경로 해석**."""
    from app.config import get_font_path
    try:
        return charset(str(get_font_path(str(font_name), app_root)))
    except Exception:                       # noqa: BLE001 — 판정 불가는 통과(fail-open)
        return frozenset()


# ───────── 판정·정리 (순수 — 테스트 대상) ─────────
def missing_chars(text: Any, chars: frozenset[int]) -> list[str]:
    """폰트에 글리프가 없는 글자들(등장 순서, 중복 제거). 순수.

    chars 가 비면 **판정 불가**로 보고 빈 목록을 돌려준다 — 폰트를 못 읽었다는 이유로
    사람이 쓴 문구를 지우거나 재렌더를 거절하면 안 된다(fail-open)."""
    if not chars:
        return []
    out: list[str] = []
    for ch in str(text or ""):
        cp = ord(ch)
        if cp in ALWAYS_OK or cp in chars or ch in out:
            continue
        out.append(ch)
    return out


def strip_missing(text: Any, chars: frozenset[int]) -> tuple[str, list[str]]:
    """(글리프 없는 글자를 뺀 문구, 뺀 글자들). 순수.

    뺀 자리에 생긴 이중 공백·양끝 공백은 정리한다 — "폭발 💥" 가 "폭발 " 로 남으면
    가운데 정렬 텔롭이 눈에 띄게 치우친다. 줄바꿈은 보존한다(제목 2줄 위계)."""
    bad = missing_chars(text, chars)
    if not bad:
        return str(text or ""), []
    drop = set(bad)
    kept = "".join(c for c in str(text) if c not in drop)
    kept = "\n".join(" ".join(line.split()) for line in kept.split("\n")).strip()
    return kept, bad


# ───────── 사람이 보낸 값(편집 오버라이드) — 지우지 않고 거절한다 ─────────
def check_overrides(ov: dict[str, Any] | None, app_root: Path,
                    *, title_font: Any = None) -> None:
    """편집실 오버라이드의 문구를 렌더 전에 검사. 위반이면 EditOverrideError.

    AI 산출(style_compose)은 조용히 **제거**하지만 사람 값은 **거절**이다 — 편집실에서
    고친 문구를 우리가 말없이 지우면 '사람이 고친 값이 반영 안 된 채 영상이 나가는 것'
    (EditOverrideError 머리말의 최악)을 우리 손으로 만드는 셈이다. 무거운 단계를 돌기
    전에 실패시켜 검수함에 이유가 남게 한다.

    폰트를 못 읽으면 통과시킨다(fail-open) — 판정 불가로 재렌더를 막지 않는다."""
    from app.modules.edit_overrides import EditOverrideError

    if not ov:
        return
    bad: list[str] = []
    for i, t in enumerate(ov.get("texts") or []):
        if not isinstance(t, dict):
            continue
        font = t.get("font") or "Jalnan"
        miss = missing_chars(t.get("text"), font_charset(str(font), app_root))
        if miss:
            bad.append(f"texts[{i}] {str(t.get('text'))[:16]!r} — {font} 폰트에 없는 글자 "
                       f"{''.join(miss)}")
    title = ov.get("title") or {}
    if title and title_font:
        chars = font_charset(str(title_font), app_root)
        for label, text in ([("title.top_title", title.get("top_title"))]
                            + [(f"title.segments[{i}]", sg.get("text"))
                               for i, sg in enumerate(title.get("segments") or [])
                               if isinstance(sg, dict)]):
            if text is None:
                continue
            miss = missing_chars(text, chars)
            if miss:
                bad.append(f"{label} {str(text)[:16]!r} — {title_font} 폰트에 없는 글자 "
                           f"{''.join(miss)}")
    if bad:
        raise EditOverrideError(
            "폰트에 글리프가 없는 글자가 있습니다 — 그대로 렌더하면 화면에 두부(□)로 "
            "나갑니다. 그 글자를 빼고 다시 보내세요:\n  · " + "\n  · ".join(bad))
