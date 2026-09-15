"""Corrected dialogue on measured word times, split by shared v3 phrasing."""
from __future__ import annotations

import copy
import difflib

from app.v3.assemble import _lines_for_span, strip_dialogue_period

SCHEMA = "tikitaka_phrase_subtitles/v1"


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


def phrase_lines(line, words):
    aligned = corrected_words(line, words)
    if not aligned or any(w['t1'] <= w['t0'] for w in aligned):
        return [{'start': line['start'], 'end': line['end'], 'text': line['text'],
                 'timing': 'line_fallback'}]
    return [{**p, 'text': strip_dialogue_period(p['text']), 'timing': 'stt_words'}
            for p in _lines_for_span(aligned, line['start'], line['end'])
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
