"""Offline cut-guard A/B using saved tables. No Gemini or TTS calls.

python -m scripts.check_cover_cuts --job-dir JOB --versions 2 4 --out NEW_DIR
Input tables/media are never overwritten. Outputs include refined boundaries,
unchanged/guarded frame budgets and source windows for inspecting each finding.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.tikitaka.common import Job
from app.tikitaka.cut_guard import guard_table


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--job-dir',type=Path,required=True)
    ap.add_argument('--versions',type=int,nargs='+',required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--blocked-json',type=Path,help='Additional excluded [start,end] pairs (guide/channel restrictions)')
    a=ap.parse_args()
    if a.out.resolve()==a.job_dir.resolve():
        ap.error('--out must be separate from the input job')
    a.out.mkdir(parents=True,exist_ok=True)
    load=lambda name:json.loads((a.job_dir/name).read_text(encoding='utf-8'))
    identity=load('pipeline_identity.json')
    job=Job(Path(identity['source']['path']),a.out,'cut-guard offline test')
    grid=load('grid.json')
    blocked=[]
    if (a.job_dir/'index.json').exists():
        blocked.extend(load('index.json').get('analysis_excluded_ranges',[]))
    if (a.job_dir/'blackspans.json').exists():
        blocked.extend(load('blackspans.json').get('spans',[]))
    if a.blocked_json:
        blocked.extend(json.loads(a.blocked_json.read_text(encoding='utf-8')))
    summary=[]
    for v in a.versions:
        original=load(f'grid_table_v{v}.json')
        # Frozen tables already exclude prohibited footage. Offline checking may
        # only borrow from their own approved spans; no newly selected IDs.
        guarded,audit=guard_table(job,original,grid,blocked=blocked)
        job.save(f'guarded_v{v}.json',guarded)
        job.save(f'audit_v{v}.json',audit)
        counts=lambda t:[sum(round(c['dur']*30) for c in r['cuts']) for r in t['rows']]
        assert counts(guarded)==counts(original),'row clocks changed'
        assert all({k:v for k,v in r.items() if k!='cuts'}=={k:v for k,v in s.items() if k!='cuts'}
                   for r,s in zip(original['rows'],guarded['rows'])),'nonvisual row fields changed'
        summary.append(dict(version=v,row_frames=counts(guarded),blocked=audit['blocked'],
                            repaired_rows=[r['row'] for r in audit['rows'] if r['status']=='repaired'],
                            remaining_candidates=len(audit['remaining'])))
    job.save('summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
