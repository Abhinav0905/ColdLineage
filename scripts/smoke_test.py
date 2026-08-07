import json, urllib.request
BASE='http://localhost:8000/api'
def get(path): return json.load(urllib.request.urlopen(BASE+path))
def post(path, body):
    req=urllib.request.Request(BASE+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
    return json.load(urllib.request.urlopen(req))
assert get('/health')['ok']
ds=get('/datasets'); assert len(ds)>=3
pv=post('/datasets/1/preview',{'cutoff_date':'2024-07-01'}); assert pv['rows']>0
sm=post('/datasets/1/simulate',{'cutoff_date':'2024-07-01'}); assert sm['recommendation'] in ('SAFE_TO_ARCHIVE','ARCHIVE_WITH_REHYDRATION')
print('Smoke test passed:', {'datasets':len(ds),'candidate_rows':pv['rows'],'simulation':sm['recommendation']})
