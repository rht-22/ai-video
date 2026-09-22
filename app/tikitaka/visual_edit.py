"""Pre-render visual continuity: shared native-fps shot index, compact per-edit view.

No speaker identity guesses. Narration footage may be reallocated inside selected
spans; spoken audio is protected. A short-shot threshold is a review heuristic,
not a universal minimum. Unresolved cases block strict publishing, not inspection.
"""
from __future__ import annotations
import copy
import math
import re
import subprocess
from app.tikitaka.common import find_bin
from app.tikitaka.grid import fingerprint, source_identity
from app.v3 import assemble, watch_trim

SCHEMA = 'visual_edit/v1'
FPS = 30
MIN_SHOT = .4
WINDOW = 10


def frames(c):
    return round(assemble.clip_duration(assemble.clip_len(c), FPS)*FPS)


def scan(job, plan, source_duration):
    """Canonical 10s source tiles: the same scene in 14 versions is decoded once."""
    tiles = sorted({k for c in plan['timeline'] for k in range(
        int(c['clip_start_sec']//WINDOW), int((c['clip_end_sec']-1e-8)//WINDOW)+1)})
    identity = fingerprint([SCHEMA, source_identity(job.source), .22, WINDOW])
    cuts = []
    for k in tiles:
        a=max(0,k*WINDOW-.5);z=min(source_duration,(k+1)*WINDOW+.5)
        name=f'visual_sources/{identity}/{k:06d}.json'
        if job.has(name): measured=job.load(name)['cuts']
        else:
            p=subprocess.run([find_bin('ffmpeg'),'-hide_banner','-nostdin','-ss',f'{a:.9f}',
                '-t',f'{z-a:.9f}','-i',str(job.source),'-vf',"scale=320:-2,select='gt(scene,0.22)',showinfo",
                '-an','-f','null','-'],capture_output=True,text=True,timeout=90)
            if p.returncode: raise RuntimeError(f'visual scan failed: {p.stderr[-300:]}')
            measured=[round(a+float(t),6) for t in re.findall(r'pts_time:([0-9.]+)',p.stderr)
                      if k*WINDOW <= a+float(t) < (k+1)*WINDOW]
            job.save(name,{'cuts':measured,'source_window':[a,z]})
        cuts.extend(measured)
    return sorted(set(cuts))


def shots(plan, boundaries):
    """Output-frame clock; coalesce technical chunks only when source is continuous."""
    import bisect
    result=[];off=0
    for i,c in enumerate(plan['timeline']):
        a,z=c['clip_start_sec'],c['clip_end_sec'];n=frames(c);rate=c.get('playback_speed') or 1.
        points=[(0,a)]+[(min(n,max(0,math.ceil((b-a)/rate*FPS-1e-7))),b)
                           for b in boundaries if a+1e-7<b<z-1e-7]+[(n,z)]
        for (f,s),(g,e) in zip(points,points[1:]):
            if g<=f:continue
            sid=bisect.bisect_right(boundaries,s+1e-7)
            part={'clip':i,'in':s,'out':e,'frames':g-f}
            if result and result[-1]['shot']==sid and abs(result[-1]['parts'][-1]['out']-s)<1e-5:
                result[-1]['end']=off+g;result[-1]['parts'].append(part)
            else:result.append({'shot':sid,'start':off+f,'end':off+g,'parts':[part]})
        off+=n
    return result


def _allocate_cover(clips, boundaries, spans, holes, reserved):
    """Same output frames, 1..1.2x, no freeze or reverse. Atomic per beat."""
    pieces=[]
    for c in clips:
        if c.get('reframe') or c.get('hold_sec'):return None
        a,z=c['clip_start_sec'],c['clip_end_sec']
        points=sorted(set([a,z]+[b for b in boundaries if a<b<z]))
        for s,e in zip(points,points[1:]):
            if any(min(e,y)-max(s,x)>1e-6 for x,y in holes):continue
            if pieces and abs(pieces[-1]['out']-s)<1e-6 and not any(abs(s-b)<1e-6 for b in boundaries):
                pieces[-1]['out']=e;pieces[-1]['ids']=list(dict.fromkeys(pieces[-1]['ids']+c.get('span_ids',[])))
            else:pieces.append({'in':s,'out':e,'base':c,'ids':list(c.get('span_ids',[]))})
    if not pieces:return None
    target=sum(frames(c) for c in clips)
    capacity=lambda p:math.floor((p['out']-p['in'])*FPS+1e-6)
    deficit=target-sum(capacity(p) for p in pieces)
    # Borrow <= MIN_SHOT from an approved, unused part of the SAME source shot.
    for p in pieces:
        if deficit<=0:break
        a,z=p['in'],p['out'];hi=z
        for sid in p['ids']:
            sp=spans.get(sid,{})
            if sp.get('t_in',math.inf)<=z<=sp.get('t_out',-math.inf)+1e-6:hi=max(hi,sp['t_out'])
        hi=min(hi,z+MIN_SHOT,min((b for b in boundaries if b>=z-1e-6),default=hi))
        occupied = [(other['in'], other['out']) for other in pieces if other is not p]
        for x,y in holes+reserved+occupied:
            if x<hi and y>z:hi=min(hi,max(z,x))
        before=capacity(p);p['out']=min(hi,z+(deficit+1)/FPS);deficit-=capacity(p)-before
    if deficit>0:return None
    counts=[max(1,math.ceil((p['out']-p['in'])/1.2*FPS-1e-6)) for p in pieces]
    remaining=target-sum(counts)
    if remaining<0:return None
    for i,p in enumerate(pieces):
        n=min(remaining,capacity(p)-counts[i]);counts[i]+=n;remaining-=n
    if remaining:return None
    out=[]
    for p,n in zip(pieces,counts):
        rate=(p['out']-p['in'])/(n/FPS)
        if not 1-1e-6<=rate<=1.2+1e-6:return None
        c=copy.deepcopy(p['base']);c.update(clip_start_sec=p['in'],clip_end_sec=p['out'],
            playback_speed=max(1.,min(1.2,rate)),span_ids=p['ids'])
        c.pop('narration_framing',None)
        out.append(c)
    return out


def remove_frames(plan, cuts):
    """Quantized timeline surgery. Untouched speed/hold clips retain their exact clock."""
    out=copy.deepcopy(plan);out['timeline']=[];off=0
    for c in plan['timeline']:
        n=frames(c);holes=[(max(0,a-off),min(n,b-off)) for a,b in cuts if a<off+n and b>off]
        if not holes:out['timeline'].append(copy.deepcopy(c));off+=n;continue
        if c.get('cover') or c.get('hold_sec') or c.get('playback_speed',1)!=1:
            raise ValueError('audio trim on a retimed/cover clip is forbidden')
        cursor=0
        for a,b in sorted(holes)+[(n,n)]:
            if a>cursor:
                p=copy.deepcopy(c);p['clip_start_sec']=c['clip_start_sec']+cursor/FPS
                p['clip_end_sec']=c['clip_start_sec']+a/FPS
                out['timeline'].append(p)
            cursor=max(cursor,b)
        off+=n
    return out


def run(job, plan, segments, resources, grid, *, boundaries=None, blocked=(), protected_spans=(), silent_intervals=()):
    original=copy.deepcopy(plan);plan=copy.deepcopy(plan)
    edges=scan(job,plan,grid['source']['duration_sec']) if boundaries is None else sorted(boundaries)
    spans={s['id']:s for s in grid['span_candidates']}
    before=shots(plan,edges);findings=[s for s in before if s['end']-s['start']<MIN_SHOT*FPS]
    changes=[]
    # Repair N before touching any output time. Every narration beat retains its frame budget.
    for beat in sorted({c.get('beat',0) for c in plan['timeline'] if c.get('cover')}):
        indices=[i for i,c in enumerate(plan['timeline']) if c.get('cover') and c.get('beat',0)==beat]
        holes=[(p['in'],p['out']) for s in shots(plan,edges) if s['end']-s['start']<MIN_SHOT*FPS
               and all(p['clip'] in indices for p in s['parts']) for p in s['parts']]
        if not holes:continue
        clips=[plan['timeline'][i] for i in indices]
        # A surviving span ID is not proof that its key evidence survived. Keep
        # the entire explicitly designated evidence interval unless reviewed.
        if any(min(y, spans[sid]['t_out']) > max(x, spans[sid]['t_in'])
               for sid in protected_spans if sid in spans for x,y in holes):continue
        reserved=list(blocked)+[(c['clip_start_sec'],c['clip_end_sec']) for i,c in enumerate(plan['timeline']) if i not in indices]
        replacement=_allocate_cover(clips,edges,spans,holes,reserved)
        if replacement is None:continue
        # Dropping a unique evidence span is not a successful visual repair.
        ids={sid for c in replacement for sid in c['span_ids'] if sid in spans and
             min(c['clip_end_sec'],spans[sid]['t_out'])>max(c['clip_start_sec'],spans[sid]['t_in'])}
        if (set(protected_spans)&{s for c in clips for s in c['span_ids']})-ids:continue
        if indices != list(range(indices[0],indices[-1]+1)):continue
        plan['timeline'][indices[0]:indices[-1]+1]=replacement
        if holes:changes.append({'beat':beat,'action':'cover_reallocate','removed_source':holes})
    assert sum(frames(c) for c in plan['timeline'])==sum(frames(c) for c in original['timeline'])
    remaining=shots(plan,edges);cuts=[]
    protected=[(s['start_sec']-.06,s['end_sec']+.06) for s in segments]
    protected += [(f['cue']['start_sec'],f['cue']['end_sec']) for f in resources.get('tts_cue_files',[])]
    for s in remaining:
        if s['end']-s['start']>=MIN_SHOT*FPS:continue
        cs=[plan['timeline'][p['clip']] for p in s['parts']]
        # No transcript means unknown audio, not silence. Manual edits also win.
        a,z=s['start']/FPS,s['end']/FPS
        off=0;windows={}
        for i,c in enumerate(plan['timeline']):windows[i]=(off/FPS,(off+frames(c))/FPS);off+=frames(c)
        has_dialogue=all(any(min(windows[p['clip']][1],v['end_sec'])>max(windows[p['clip']][0],v['start_sec']) for v in segments) for p in s['parts'])
        # Subtitle gaps alone do not certify silence: STT can miss a short word.
        # Production supplies no verified silence, so all original-audio short
        # shots remain unchanged. A separate audio check may supply these later.
        if (any(x <= a and z <= y for x,y in silent_intervals) and has_dialogue
                and all(not c.get('cover') and not c.get('reframe') and not c.get('hold_sec')
                and c.get('playback_speed',1)==1 for c in cs)
                and not any(min(z,y)>max(a,x) for x,y in protected)):
            cuts.append((s['start'],s['end']))
    # Adjacent cuts are one removal interval; otherwise source-frame rounding can leave slivers.
    merged=[]
    for a,z in sorted(cuts):
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(z,merged[-1][1]))
        else:merged.append((a,z))
    plan=remove_frames(plan,merged)
    removal=[{'start':a/FPS,'end':z/FPS} for a,z in merged]
    total=sum(frames(c) for c in plan['timeline'])/FPS
    new_segments=watch_trim.remap_segments(segments,removal,total) if removal else copy.deepcopy(segments)
    new_resources=watch_trim.remap_resources(resources,removal,total) if removal else copy.deepcopy(resources)
    for item in new_resources.get('tts_caption_segments',[]):
        for key in ('start_sec','end_sec'):
            if key in item:item[key]=watch_trim.remap_edited(item[key],removal)
    for f in new_resources.get('tts_cue_files',[]):
        cs=[c for c in plan['timeline'] if c.get('cover') and c.get('beat')==f['cue'].get('beat')]
        if cs:f['cue'].update(source_time_sec=cs[0]['clip_start_sec'],source_end_sec=cs[-1]['clip_end_sec'],
                              muted_span_ids=list(dict.fromkeys(s for c in cs for s in c['span_ids'])))
    after=shots(plan,edges)
    pending=[dict(s,reason='대사/내레이션 보호 또는 대체 화면 부족') for s in after if s['end']-s['start']<MIN_SHOT*FPS]
    audit={'schema':SCHEMA,'threshold_sec':MIN_SHOT,'before':findings,'changes':changes,
           'removed_output':removal,'remaining':pending,'blocked':bool(pending),
           'shots':[{'id':f"S{s['shot']}",'frames':[s['start'],s['end']],
                     'source':[[p['in'],p['out']] for p in s['parts']]} for s in after]}
    plan['visual_policy']={'fill_only':True,'min_shot_sec':MIN_SHOT}
    job.save('visual_edit.json',audit)
    job.log(f"[visual-edit] 짧은 샷 {len(findings)} → {len(pending)}, 무대사 삭제 {sum(z-a for a,z in merged)/FPS:.3f}s")
    return plan,new_segments,new_resources,audit


def review(job, plan, audit, *, mode):
    """Persist a human-readable queue; only explicit, exact-case keeps clear the gate.

    Decisions live in review_vN/visual_review_decisions.json, never in model output.
    A changed source, edit clock, crop or finding invalidates the corresponding keep.
    """
    identity = fingerprint(source_identity(job.source))
    windows = []; off = 0
    for c in plan['timeline']:
        windows.append([off/FPS, (off+frames(c))/FPS]); off += frames(c)
    issues = []
    for s in audit['remaining']:
        issues.append({'kind': 'short_shot', 'time': [s['start']/FPS, s['end']/FPS],
                       'source': [[p['in'], p['out']] for p in s['parts']], 'reason': s['reason']})
    for f in audit.get('framing_issues', []):
        c = plan['timeline'][f['clip']]
        issues.append({'kind': 'framing', 'time': windows[f['clip']],
                       'source': [[c['clip_start_sec'], c['clip_end_sec']]],
                       'target': c.get('narration_framing'), 'reason': f['reason']})
    for f in audit.get('render_issues', []):
        issues.append(copy.deepcopy(f))
    saved = job.load('visual_review_decisions.json') if job.has('visual_review_decisions.json') else {}
    keeps = {}
    if saved.get('source_identity') == identity:
        for d in saved.get('decisions', []):
            if d.get('action') == 'keep' and isinstance(d.get('reason'), str) and d['reason'].strip():
                keeps[d.get('id')] = d['reason']
    for issue in issues:
        issue['id'] = fingerprint([identity, issue])[:20]
        issue['status'] = 'accepted' if issue['id'] in keeps else 'pending'
        if issue['status'] == 'accepted': issue['decision_reason'] = keeps[issue['id']]
    audit.update(source_identity=identity, review_items=issues, mode=mode,
                 blocked=any(i['status'] == 'pending' for i in issues))
    job.save('visual_edit.json', audit)
    lines = ['# 시각 편집 검토', '',
             '대사·내레이션을 보존한 미리보기입니다. 미해결 항목은 원래 화면 또는 기존 크롭을 유지합니다.',
             '미해결 항목이 있으면 strict 최종 출력을 중단합니다. 대시보드 승인 UI는 아직 연결하지 않았습니다.', '',
             '| 출력 구간 | 종류 | 상태 | 사유 |', '|---|---|---|---|']
    labels = {'short_shot': '짧게 튀는 화면', 'framing': '구도', 'face_safe': '얼굴 가장자리'}
    for i in sorted(issues, key=lambda x: x['time']):
        reason = i['reason'].replace('|', '/').replace('\n', ' ')
        when = f"{i['time'][0]:.2f}초 부근" if i['time'][0] == i['time'][1] else f"{i['time'][0]:.2f}–{i['time'][1]:.2f}초"
        status = '그대로 사용 승인' if i['status'] == 'accepted' else '확인 필요'
        lines.append(f"| {when} | {labels.get(i['kind'], i['kind'])} | {status} | {reason} |")
    lines += ['', '영상을 확인한 뒤 이 대화에서 “22.43초는 그대로 사용”처럼 결정하거나 수정할 내용을 알려주세요.',
              '수정한 항목은 다시 검사합니다. 소스·시간·구도가 달라지면 이전 승인을 그대로 적용하지 않습니다.',
              '검토 목록에 기록된 것만으로 수정 완료나 승인으로 처리하지 않습니다.', '']
    job.path('visual_review.md').write_text('\n'.join(lines), encoding='utf-8')
    if mode == 'strict' and audit['blocked']:
        raise ValueError(f"시각 편집 검토 필요 — {job.path('visual_review.md')}")
    return audit
