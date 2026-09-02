import json,sys
from collections import defaultdict
d=sys.argv[1]
t=json.load(open(f'{d}/truth.json')); rs=json.load(open(f'{d}/results.json'))
H=[l['height_px'] for l in t[0]['lines']]
sc=float(sys.argv[2]) if len(sys.argv)>2 else 0.4448
T={(f['frame'],l['row']):l['code'] for f in t for l in f['lines']}
def cer(a,b):
    m,n=len(a),len(b); dd=list(range(n+1))
    for i in range(1,m+1):
        prev=dd[0]; dd[0]=i
        for j in range(1,n+1):
            cur=dd[j]; dd[j]=min(dd[j]+1,dd[j-1]+1,prev+(a[i-1]!=b[j-1])); prev=cur
    return dd[n]/max(1,m)
agg=defaultdict(lambda: defaultdict(int)); cers=defaultdict(list)
for r in rs:
    for fi,s in enumerate(r['screens']):
        for ri,h in enumerate(H):
            exp=T[(fi,ri)]; got=(s[ri] if ri<len(s) else '').strip()
            if not got: agg[(r['label'],h)]['missing']+=1; cers[(r['label'],h)].append(1.0); continue
            c=cer(exp,got); cers[(r['label'],h)].append(min(c,1.0))
            agg[(r['label'],h)]['exact' if got==exp else ('near' if c<=0.4 else 'wrong')]+=1
print(f'### {d}  캔버스 글자높이 = 1080p값 x {sc}')
print(f"{'1080p':>6} {'실제px':>7} | {'미지정 정확':>11} {'근접':>4} {'오답':>4} {'누락':>4} {'CER':>6} | {'HIGH 정확':>10} {'근접':>4} {'오답':>4} {'누락':>4} {'CER':>6}")
for h in H:
    row=f'{h:>6} {h*sc:>7.1f} |'
    for lab in ('unspecified','HIGH'):
        a=agg[(lab,h)]; cc=sum(cers[(lab,h)])/len(cers[(lab,h)])
        row+=f" {a['exact']:>3}/24 {a['exact']/24*100:>4.0f}% {a['near']:>4} {a['wrong']:>4} {a['missing']:>4} {cc:>6.3f} |"
    print(row)
