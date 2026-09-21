"""Opt-in shot-fragment audit/repair. Never edits narration, S/A cuts or row clocks.

Scene boundaries are candidates, not editing instructions. Refine only selected
edge neighborhoods at native fps; inspect existing interior boundaries too.
Repairs stay inside previously selected span envelopes. Unrepairable edge remnants
block opt-in rendering; intact short shots and S/A findings are review-only.
No model/API calls.
"""
from __future__ import annotations

import bisect
import copy
import math
import re
import subprocess

from app.tikitaka.common import find_bin
from app.tikitaka.grid import fingerprint, source_identity

SCHEMA = "cut_guard/v1"
FPS = 30
MIN_SHOT = .30                 # Review threshold, NOT a universal minimum shot length.
RADIUS = .45
THRESHOLD = .22


def _frames(c):
    return round(c['dur'] * FPS)


def shot_fragments(cuts, boundaries):
    """Actual visible shots, coalescing continuous source across technical chunks."""
    shots, offset = [], 0.
    edges = sorted(set(boundaries))
    for i, c in enumerate(cuts):
        a, z = c['in'], c['out']
        dur = _frames(c) / FPS
        pts = [a] + [b for b in edges if a + 1e-7 < b < z - 1e-7] + [z]
        for s, e in zip(pts, pts[1:]):
            t0 = offset + (s-a)/(z-a)*dur
            t1 = offset + (e-a)/(z-a)*dur
            shot = bisect.bisect_right(edges, s + 1e-7)
            if shots and shots[-1]['shot'] == shot and abs(shots[-1]['out']-s) < 1e-6:
                shots[-1].update(out=e, end=t1, last=i)
            else:
                shots.append(dict(shot=shot, start=t0, end=t1, **{'in': s, 'out': e}, first=i, last=i))
        offset += dur
    return [s for s in shots if s['end']-s['start'] < MIN_SHOT - 1e-6]


def refine_boundaries(job, table, coarse, source_duration):
    """10fps candidates + native-fps windows at every selected endpoint.

    Also scans endpoints absent from coarse detection (low-contrast cuts).
    Candidate neighborhoods are bounded; no full-episode decode or paid calls.
    """
    windows = []
    for row in table['rows']:
        for c in row['cuts']:
            points = [c['in'], c['out']] + [b for b in coarse if c['in'] < b < c['out']]
            for p in points:
                windows.append((max(0., p-RADIUS), min(source_duration, p+RADIUS)))
    merged = []
    for a, z in sorted(windows):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(z, merged[-1][1]))
        else:
            merged.append((a,z))
    found = []
    for a,z in merged:
        key = fingerprint([SCHEMA, source_identity(job.source), a,z,THRESHOLD])
        name = f'cut_guard_probes/{key}.json'
        if job.has(name):
            measured = job.load(name)['cuts']
        else:
            # Input -t bounds decoding before select; output -t alone can scan on
            # until the next selected frame. Preserve native fps and exact PTS.
            proc = subprocess.run([find_bin('ffmpeg'), '-hide_banner', '-nostdin',
                '-ss', f'{a:.9f}', '-t', f'{z-a:.9f}', '-i', str(job.source),
                '-vf', f"scale=320:-2,select='gt(scene,{THRESHOLD})',showinfo",
                '-an', '-f', 'null', '-'], capture_output=True, text=True, timeout=60)
            if proc.returncode:
                raise RuntimeError(f'cut-guard frame scan failed: {proc.stderr[-500:]}')
            measured = [a+float(t) for t in re.findall(r'pts_time:([0-9.]+)', proc.stderr)]
            measured = [t for t in measured if a < t < z]
            job.save(name, dict(cuts=measured, window=[a,z]))
        found.extend(measured)
    # Replace coarse candidates inside inspected windows, not double-count both.
    found += [b for b in coarse if not any(a <= b <= z for a,z in merged)]
    return sorted(set(round(b,6) for b in found))


def _runs(cuts):
    runs = []
    for c in cuts:
        if runs and abs(runs[-1][-1]['out']-c['in']) < 1e-6:
            runs[-1].append(c)
        else:
            runs.append([c])
    return runs


def repair_cover(cuts, boundaries, spans, blocked=()):
    """Remove only clipped shot heads/tails; hold row frames fixed, speed 1..1.2.

    Whole short shots inside the source are review-only. May borrow <=0.30s of
    unused footage inside selected spans, in the same surviving shot. No freeze.
    Returns original deep copy on any infeasible repair (atomic per N row).
    """
    original = copy.deepcopy(cuts)
    findings = shot_fragments(cuts, boundaries)
    if not findings:
        return original, {'status': 'clean', 'findings': []}
    groups = _runs(cuts)
    work, changes = [], []
    for group in groups:
        a,z = group[0]['in'],group[-1]['out']
        inner = [b for b in boundaries if a+1e-7 < b < z-1e-7]
        frames = sum(_frames(c) for c in group)
        rate = (z-a)/(frames/FPS)
        na,nz = a,z
        starts_on_cut = a < 1e-5 or any(abs(b-a) < 1e-5 for b in boundaries)
        ends_on_cut = any(abs(b-z) < 1e-5 for b in boundaries)
        if inner and (inner[0]-a)/rate < MIN_SHOT and not starts_on_cut:
            na = inner[0]
        if inner and (z-inner[-1])/rate < MIN_SHOT and not ends_on_cut:
            nz = inner[-1]
        if nz <= na:
            return original, dict(status='needs_reselection', findings=findings, reason='짧은 샷의 안전한 대체 화면 필요')
        ids = list(dict.fromkeys(s for c in group for s in c.get('span_ids', [c['src']])))
        approved = [spans[s] for s in ids if s in spans]
        lo,hi = a,z
        # Only extend the outer edges. Existing approved inter-span gaps remain
        # as assembled; this never bridges a new gap or crosses a shot boundary.
        for sp in approved:
            if sp['t_in'] <= a+1e-6 <= sp['t_out']:
                lo = min(lo, sp['t_in'])
            if sp['t_in'] <= z+1e-6 <= sp['t_out']:
                hi = max(hi, sp['t_out'])
        lo = max(lo, a-MIN_SHOT, max((b for b in boundaries if b <= na+1e-7), default=0))
        hi = min(hi, z+MIN_SHOT, min((b for b in boundaries if b >= nz-1e-7), default=hi))
        # Never restore a removed sliver.
        if na > a: lo = na
        if nz < z: hi = nz
        for ba,bz in blocked:
            if ba < na and bz > lo: lo = min(na,max(lo,bz))
            if bz > nz and ba < hi: hi = max(nz,min(hi,ba))
        work.append(dict(a=na,z=nz,lo=lo,hi=hi,frames=frames,group=group,ids=ids))
        if na != a or nz != z:
            changes.append({'before':[a,z], 'after':[na,nz]})
    if not changes:
        return original, dict(status='needs_review', findings=findings, reason='원본부터 짧은 샷 또는 짧은 독립 선택')
    target = sum(_frames(c) for c in cuts)
    # First use speed slack. If integer-frame capacity is still insufficient,
    # extend only within the already-approved same-shot envelopes.
    capacity = lambda w: math.floor((w['z']-w['a'])*FPS+1e-6)
    deficit = target-sum(capacity(w) for w in work)
    for w in work:
        if deficit <= 0: break
        need = deficit/FPS + 1/FPS  # fractional native/output frame alignment
        old = capacity(w)
        take = min(need, w['hi']-w['z']); w['z'] += take; need -= take
        take = min(need, w['a']-w['lo']); w['a'] -= take
        deficit -= capacity(w)-old
    if deficit > 0:
        return original, dict(status='needs_reselection', findings=findings, reason='내레이션 길이를 지킬 승인 화면 부족')
    counts = [min(w['frames'],capacity(w)) for w in work]
    for i,w in enumerate(work):
        counts[i] = max(counts[i], math.ceil((w['z']-w['a'])/1.2*FPS-1e-6))
    remaining = target-sum(counts)
    if remaining < 0:
        return original, dict(status='needs_reselection', findings=findings, reason='배속 상한 안에서 길이 배분 불가')
    for i,w in enumerate(work):
        add = min(remaining,capacity(w)-counts[i]); counts[i] += add; remaining -= add
    result = []
    for w,n in zip(work,counts):
        if n <= 0:
            return original, dict(status='needs_reselection', findings=findings, reason='빈 덮개')
        rate = (w['z']-w['a'])/(n/FPS)
        if not 1-1e-8 <= rate <= 1.2+1e-8:
            raise ValueError('cut-guard speed allocation failed')
        # Keep <=2s source chunks with a single rate and integer output frames.
        step = max(1,math.floor(2/rate*FPS))
        done = 0
        while done < n:
            take = min(step,n-done)
            a = w['a'] + done/FPS*rate; z = w['a']+(done+take)/FPS*rate
            representative = max(w['group'], key=lambda c: max(0.,min(z,c['out'])-max(a,c['in'])))
            c = copy.deepcopy(representative)
            ids = [s for s in w['ids'] if s in spans and spans[s]['t_in'] < z and spans[s]['t_out'] > a]
            if not ids: raise ValueError('cut-guard lost span provenance')
            c.update({'in':a,'out':z,'dur':take/FPS,'playback_speed':max(1.,min(1.2,rate)),
                      'src':ids[0], 'span_ids':ids})
            c.pop('reframe',None); c.pop('hold_sec',None)
            result.append(c); done += take
    after = shot_fragments(result,boundaries)
    if after:
        return original, dict(status='needs_reselection', findings=findings, reason='수정 뒤에도 짧은 화면이 남음')
    assert sum(_frames(c) for c in result) == target
    return result, dict(status='repaired', findings=findings, changes=changes,
                        source_after=[[c['in'],c['out']] for c in result])


def guard_table(job, table, grid, *, blocked=()):
    out = copy.deepcopy(table)
    boundaries = refine_boundaries(job, table, grid.get('scene_cuts',[]), grid['source']['duration_sec'])
    spans = {s['id']:s for s in grid['span_candidates']}
    audit = dict(schema=SCHEMA, refined_boundaries=boundaries, rows=[])
    for row in out['rows']:
        if row['mode'] != 'N': continue
        reserved = list(blocked) + [(c['in'],c['out']) for r in out['rows']
            if r is not row and r['mode'] in {'A','N'} for c in r['cuts']]
        repaired, report = repair_cover(row['cuts'],boundaries,spans,reserved)
        before_ids = {s for c in row['cuts'] for s in c.get('span_ids',[c['src']])}
        after_ids = {s for c in repaired for s in c.get('span_ids',[c['src']])}
        evidence = {c['span_id'] for c in (row.get('production_plan') or {}).get('cover',[])
                    if c.get('role') == 'evidence'}
        if report['status'] == 'repaired' and (evidence & before_ids) - after_ids:
            report = dict(status='needs_reselection', findings=report['findings'],
                          reason='핵심 근거 화면이 사라지는 수정은 적용하지 않음')
        else:
            row['cuts'] = repaired
        audit['rows'].append(dict(row=row['i'],**report))
        job.log(f"[cut-guard] 행{row['i']}: {report['status']} ({len(report['findings'])}건)")
    flat = [c for r in out['rows'] for c in r['cuts']]
    audit['remaining'] = shot_fragments(flat,boundaries)
    owners = [(r['i'],r['mode']) for r in out['rows'] for _ in r['cuts']]
    for finding in audit['remaining']:
        finding['rows'] = sorted({owners[i][0] for i in range(finding['first'],finding['last']+1)})
        finding['modes'] = sorted({owners[i][1] for i in range(finding['first'],finding['last']+1)})
    audit['blocked'] = any(r['status'] == 'needs_reselection' for r in audit['rows'])
    out['cut_guard'] = audit
    out['fingerprint'] = fingerprint([table.get('fingerprint'),SCHEMA,out['rows'],audit])
    return out,audit
