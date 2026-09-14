"""사람이 읽는 산출 — 리빌딩 10버전(프로토콜 출력 포맷) · 마스터 편집 테이블(지침서 출력 포맷)."""
from __future__ import annotations

from app.tikitaka.common import fmt_tc


def versions_md(rebuild: dict, *, title: str) -> str:
    out = [f"# 📜 티키타카 스크립트 리빌딩 — 「{title}」 10가지 고조회수 리빌딩 결과\n",
           f"**추천 버전:** {rebuild.get('recommended')} — {rebuild.get('reason','')}\n"]
    for v in rebuild["versions"]:
        out.append("\n---\n")
        out.append(f"**[버전 {v['n']}: {v['strategy']}]**  \n**제목:** {v['title']}  \n**재배치 구조:** {v['structure']}  \n"
                   f"**계획 길이:** {v['plan_sec']:.1f}초 · 항목 {len(v['items'])}\n\n**대본:**\n\n```text")
        for it in v["items"]:
            if it["type"] == "N":
                out.append(f"[내레이션]\n{it['text']}")
            elif it["type"] == "S":
                out.append(f"[{it.get('speaker') or '미상'}]  ({'+'.join(it['line_ids'])})\n\"{it['text']}\"")
            else:
                out.append(f"[현장음]  ({it['moment_id']})\n({it.get('sound') or it['desc']})")
            if it.get("effect"):
                out.append(f"<효과자막: {it['effect']}>")
            out.append("")
        out.append("```\n")
        a = v.get("analysis") or {}
        out.append("**[Content ID 회피 및 바이럴 정밀 분석]**\n")
        out.append(f"* 텍스트 일치율: {a.get('text_match')} · 구조 유사도: {a.get('structure_sim')} · 순서 유사도: {a.get('order_sim')} · "
                   f"키워드 변형률: {a.get('keyword_var')}\n* 🛡️ 최종 안전 등급: **{a.get('grade')}**\n"
                   f"* 🚀 바이럴 예상 포인트: {a.get('viral_point')}\n* 🔎 판정 코멘트: {a.get('comment')}")
        if v.get("issues"):
            out.append("\n* ⚠ 검증 메모: " + " / ".join(v["issues"]))
    return "\n".join(out) + "\n"


def _cell(s: str) -> str:
    return str(s).replace("|", "｜").replace("\n", " ")


def table_md(table: dict, *, title: str) -> str:
    v = table["version"]
    out = [f"# 🎬 마스터 편집 테이블 — 「{title}」 [버전 {v['n']}: {v['strategy']}]\n",
           f"**제목:** {v['title']}  \n**총 길이:** {table['total_sec']:.1f}초 · 컷 소스 탐색: {table.get('cut_search')} · "
           f"내레이션 목소리: {table.get('voice')} ({table.get('speed')})\n"]
    if table.get("notes"):
        out.append("**검증 메모:** " + " / ".join(table["notes"]) + "\n")
    out.append("| 순서 | **모드** | **오디오 내용 (대사/내레이션/현장음)** | **예상 시간** | **비디오 화면 지시 (정배속 멀티 컷/액션 싱크)** | **타임코드 소스 (MM:SS.ms)** |")
    out.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
    for r in table["rows"]:
        if r["mode"] == "N":
            audio = f"(내레이션) \"{r['text']}\""
            sec = f"**{r['dur_video']:.1f}초** (실측 TTS {r['dur']:.2f}s · 계획 {r['plan_sec']:.1f}s)"
        elif r["mode"] == "S":
            audio = f"**({r.get('speaker') or '미상'}) \"{r['text']}\"**"
            sec = f"**{r['dur']:.1f}초**"
        else:
            audio = f"**{r['text']}**"
            sec = f"**{r['dur']:.1f}초**"
        if r.get("effect"):
            audio += f" 〈효과자막: {r['effect']}〉"
        vids = " / ".join(f"({k}) {c['desc']} ({c['dur']:.1f}초)" for k, c in enumerate(r["cuts"], 1)) or "(컷 없음)"
        tcs = " / ".join(f"{fmt_tc(c['in'])}~{fmt_tc(c['out'])}" for c in r["cuts"]) or "-"
        out.append(f"| {r['i']} | **[{r['mode']}]** | {_cell(audio)} | {sec} | {_cell(vids)} | {tcs} |")
    return "\n".join(out) + "\n"
