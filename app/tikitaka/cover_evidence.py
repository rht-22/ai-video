"""Retrieve action/scene evidence before choosing narration footage."""
from app.tikitaka.grid import fingerprint
from app.tikitaka.grid_table import overlap

SCHEMA = 'cover_evidence/v1'


def retrieve(job, gemini, rows, index, grid, excluded, guide=None):
    from app.tikitaka.production import SEMANTIC_RULES, narration_context
    scenes = [s for s in index.get('scenes', []) if s.get('id')]
    if not scenes:
        return {}
    # Retrieval sees the whole episode, not just the next dialogue's scene.
    material = [{'id': s['id'], 'summary': s.get('summary'), 'start': s['start'], 'end': s['end']}
                for s in scenes if not overlap(s['start'], s['end'], excluded)]
    narrations = [{'row': r['i'], 'text': r['text'], 'context': narration_context(rows, r)} for r in rows if r['mode'] == 'N']
    prompt = ('각 내레이션이 설명하는 행동이나 장면 자체를 직접 보여주는 씬을 찾아라. '
              '단순히 같은 인물이 있거나 다음 대사와 가깝다는 이유로 고르지 마라. '
              '이미 한 행동을 설명하면 앞선 실제 행동 씬을 자료화면으로 다시 보여줘도 된다. '
              '관점 전환·의견처럼 대응 행동이 없는 문장은 빈 목록. '
              '지갑에 추적기를 심었다면 지도 확인이나 차 운전이 아니라 실제 심는 씬이다. '
              '행별 최대 3개 씬을 적합도 순서로 고르고 근거를 남겨라. '
              + SEMANTIC_RULES + '\nJSON {rows:[{row:int, scene_ids:[string], reason:string}]}\n'
              f'내레이션: {narrations}\n장면: {material}\n제작 가이드: {guide or {}}')
    key = fingerprint([SCHEMA, prompt])
    name = f'cover_evidence/{key}.json'
    raw = job.load(name) if job.has(name) else gemini.text_json(prompt, kind='cover_evidence')
    job.save(name, raw)
    by_scene = {s['id']: s for s in scenes}
    by_grid = {s['id']: s for s in grid['span_candidates']}
    out = {}
    for item in raw.get('rows', []):
        chosen = [by_scene[sid] for sid in item.get('scene_ids', [])[:3] if sid in by_scene]
        candidates = {}
        for sid, fact in index['grid_facts'].items():
            sp = by_grid[sid]
            if not any(s['start'] <= sp['t_in'] and sp['t_out'] <= s['end'] for s in chosen):
                continue
            if overlap(sp['t_in'], sp['t_out'], excluded) or sp['t_out']-sp['t_in'] < .6:
                continue
            if any(term in fact.get('scene_script','') for term in (guide or {}).get('avoid',[]) if term):
                continue
            candidates[sid] = {**sp, **fact}
        out[item['row']] = {'candidates': candidates, 'reason': item.get('reason','')}
    return out


def tighten_opening(job, gemini, rows, grid):
    """Review long ambient opening independently of general watch_trim."""
    if not rows or rows[0]['mode'] != 'A' or len(rows[0]['cuts']) != 1:
        return None
    row = rows[0]; cut = row['cuts'][0]
    if cut['out'] - cut['in'] <= 1.2:
        return None
    spans = {s['id']: s for s in grid['span_candidates']}
    if any(spans.get(s, {}).get('is_audio') for s in cut.get('span_ids', [])):
        return None
    from app.tikitaka.probe import cut_proxy_clip
    prompt = ('쇼츠 첫 화면만 검수하라. 정적인 사물/시신/풍경을 오래 보여주며 사건의 변화가 없고 '
              '핵심 대사나 의미 있는 현장음도 없으면 1초로 줄일 수 있다. '
              '실제 행동 진행·놀람 반응·핵심 소리가 있으면 유지한다. '
              'JSON {static:bool, meaningful_audio:bool, reason:string}.')
    key = fingerprint([SCHEMA, cut, prompt])
    name = f'opening_review/{key}.json'
    if job.has(name):
        verdict = job.load(name)
    else:
        clip = cut_proxy_clip(job, cut['in'], cut['out'], f'opening_review/{key}.mp4')
        verdict = gemini.video_json(prompt, clip, kind='opening_review', fps=10)
        job.save(name, verdict)
    audit = {'before_sec': cut['out']-cut['in'], 'verdict': verdict, 'applied': False}
    if verdict.get('static') is True and verdict.get('meaningful_audio') is False:
        cut['out'] = cut['in'] + 1.
        cut['dur'] = row['dur'] = 1.
        audit.update(applied=True, after_sec=1.)
        job.log(f"[opening] 정적인 무대사 도입 {audit['before_sec']:.2f}초 → 1초: {verdict.get('reason')}")
    return audit
