"""Corrected dialogue on measured word times, split by shared v3 phrasing."""
from __future__ import annotations

import copy
import difflib
import re

from app.v3.assemble import _lines_for_span, strip_dialogue_period

SCHEMA = "tikitaka_phrase_subtitles/v1"


def filter_singing(table, spans, windows):
    """Reuse v3's acoustic windows and per-span evidence; preserve N/A and clocks."""
    from app.v3.assemble import span_sings
    out = copy.deepcopy(table)
    audit = []
    for row in out['rows']:
        if row['mode'] != 'S':
            continue
        kept = []
        ids = {sid for cut in row['cuts'] for sid in cut.get('span_ids', [])}
        for sub in row.get('sub_lines', []):
            mid = (sub['start'] + sub['end']) / 2
            drop = False
            if any(a <= mid < z for a, z in windows):
                evidence = [spans[sid] for sid in ids if sid in spans
                            and spans[sid]['t_in'] <= mid < spans[sid]['t_out']]
                drop = bool(evidence) and all(span_sings(sp) for sp in evidence)
                audit.append({**sub, 'kept': not drop})
            if not drop:
                kept.append(sub)
        row['sub_lines'] = kept
    return out, audit


def corrected_words(line, words):
    """Keep measured boundaries. Unalignable replacement groups stay together.

    Never redistribute a sentence uniformly over invented word timestamps.
    Original STT words remain unchanged; only the subtitle copy is corrected.
    """
    source = [words[i] for i in line.get('word_i', []) if 0 <= i < len(words)]
    tokens = str(line['text']).split()
    if not source or not tokens:
        return []
    old = [str(w.get('text', '')).strip() for w in source]
    normalize = lambda t: t.strip('.,!?…~')
    matcher = difflib.SequenceMatcher(None, list(map(normalize, old)),
                                     list(map(normalize, tokens)), autojunk=False)
    out, prefix = [], ''
    for tag, i, j, a, b in matcher.get_opcodes():
        if a == b:
            continue
        if i == j:
            text = ' '.join(tokens[a:b])
            if out:
                out[-1]['text'] += ' ' + text
            else:
                prefix = text + ' '
            continue
        groups = [(i+k, i+k+1, tokens[a+k]) for k in range(j-i)] if j-i == b-a else [
            (i, j, ' '.join(tokens[a:b]))]
        for x, z, text in groups:
            start = max(float(line['start']), float(source[x]['start']))
            end = min(float(line['end']), float(source[z-1]['end']))
            if end <= start:
                limit = float(source[z]['start']) if z < len(source) else float(line['end'])
                end = min(float(line['end']), max(start, min(start+.1, limit)))
            out.append({'t0': start, 't1': end, 'text': prefix + text})
            prefix = ''
    return out


# 성+직함·이름+호칭은 한 덩어리 — 줄이 그 사이에서 갈리면 '입사 선배인 양 / 대리가 탕비실에서' ·
# '말을 튕겨내자, 공 / 팀장은' 처럼 성이 앞 줄 꼬리에 매달린다(2026-09-18 로또 clip01 내레이션 실측).
# 규칙은 둘뿐이다: (1) 한 글자 어절(성씨·'그'·'두' 같은 관형사) 뒤 어절이 직함으로 시작 (2) 뒤 어절이 '씨'·'님'(+조사).
TITLE_WORDS = ("본부장", "팀장", "과장", "부장", "차장", "실장", "대리", "사원", "주임", "선배", "후배", "사장", "회장", "이사",
               "상무", "전무", "대표", "선생", "기자", "형사", "반장", "계장", "국장", "소장", "원장", "교수", "박사", "작가", "감독")
_ONE_SYLLABLE = re.compile(r"[가-힣]")
_HONORIFIC = re.compile(r"(씨|님)(이|가|은|는|을|를|도|의|께|께서|한테|에게|랑|이랑|요|이요)?")
_PUNCT = ".,!?…~'\"“”‘’"


def glues_to_next(prev: str, nxt: str) -> bool:
    """두 어절 사이에서 줄을 끊으면 안 되는가(성+직함 · 이름+호칭). 순수 — 테스트 대상."""
    prev, nxt = str(prev or "").strip(), str(nxt or "").strip()
    if not prev or not nxt or prev[-1] in _PUNCT:
        return False
    core = nxt.strip(_PUNCT)
    if _HONORIFIC.fullmatch(core):
        return True
    return bool(_ONE_SYLLABLE.fullmatch(prev)) and any(core.startswith(t) and len(core) - len(t) <= 3 for t in TITLE_WORDS)


def glue_name_titles(aligned):
    """측정된 어절 목록에서 끊으면 안 되는 두 어절을 한 어절로 묶는다(시각은 앞 t0 ~ 뒤 t1, 표시 텍스트는 공백 그대로).
    원본 목록은 건드리지 않는다. 순수 — 테스트 대상."""
    out = []
    for w in aligned:
        if out and glues_to_next(out[-1]['text'].split()[-1], w['text']):
            out[-1] = {**out[-1], 't1': w['t1'], 'text': out[-1]['text'] + ' ' + w['text']}
        else:
            out.append(dict(w))
    return out


def phrase_lines(line, words):
    aligned = corrected_words(line, words)
    if not aligned or any(w['t1'] <= w['t0'] for w in aligned):
        return [{'start': line['start'], 'end': line['end'], 'text': line['text'],
                 'timing': 'line_fallback'}]
    return [{**p, 'text': strip_dialogue_period(p['text']), 'timing': 'stt_words'}
            for p in _lines_for_span(glue_name_titles(aligned), line['start'], line['end'])
            if p['end'] > p['start']]


def refresh_table(table, transcript):
    """Subtitle-only refresh: keep cut/TTS clocks and selection unchanged."""
    out = copy.deepcopy(table)
    by_id = {l['id']: l for l in transcript['lines']}
    audit = []
    for row in out['rows']:
        if row['mode'] != 'S':
            continue
        ids = row.get('src') or []
        if not ids or any(lid not in by_id for lid in ids):
            continue
        subs = []
        for lid in ids:
            line = by_id[lid]
            parts = phrase_lines(line, transcript['words'])
            subs.extend(parts)
            audit.append({'line_id': lid, 'text': line['text'], 'parts': parts})
        row['sub_lines'] = subs
        row['text'] = ' '.join(by_id[lid]['text'] for lid in ids)
    out['subtitle_revision'] = {'schema': SCHEMA, 'lines': audit,
                                'polish_fingerprint': transcript.get('polish', {}).get('fingerprint')}
    return out


def narration_captions(job, resources, *, transcribe=None):
    """Phrase captions on measured synthesized-audio words; audio stays intact."""
    import hashlib
    from pathlib import Path
    from app.tikitaka.grid import fingerprint
    from app.tikitaka.transcribe import scribe_words_to_words
    from app.modules import stt_elevenlabs as el
    if transcribe is None:
        def transcribe(path):
            return el._post_speech_to_text(path, el.ensure_api_key(), language='ko', keyterms=None, is_raw=False)
    captions = []
    for f in resources.get('tts_cue_files', []):
        cue = f['cue']; path = Path(f['path'])
        key = fingerprint([SCHEMA, cue['text'], hashlib.sha256(path.read_bytes()).hexdigest()])
        name = f'tts_word_alignment/{key}.json'
        if job.has(name):
            cached = job.load(name)
            # 캐시는 측정(단어 시각)이다 — 구절은 지금 규칙으로 다시 나눈다(줄 나눔 규칙이 바뀌어도 STT 를 다시 부르지 않는다)
            words = cached['words']
            cached['phrases'] = phrase_lines({'text': cue['text'], 'start': 0., 'end': cue['duration_sec'],
                                              'word_i': list(range(len(words)))}, words)
        else:
            payload = transcribe(path)
            words = scribe_words_to_words(payload.get('words', []), 0.)
            if not words:
                raise ValueError('내레이션 구절 자막: 단어 시각 없음')
            line = {'text': cue['text'], 'start': 0., 'end': cue['duration_sec'],
                    'word_i': list(range(len(words)))}
            cached = {'words': words, 'phrases': phrase_lines(line, words),
                      'text': cue['text'], 'timing': 'scribe_synthesized_audio'}
            job.save(name, cached)
            job.record_step('tts_caption_alignment', text=cue['text'],
                            audio_sec=cue['duration_sec'], words=len(words))
        for phrase in cached['phrases']:
            a = max(float(cue['start_sec']), float(cue['start_sec']) + phrase['start'])
            z = min(float(cue['end_sec']), float(cue['start_sec']) + phrase['end'])
            if z > a:
                captions.append({'start_sec': a, 'end_sec': z, 'text': phrase['text'],
                                 'timing': cached['timing']})
    return captions
