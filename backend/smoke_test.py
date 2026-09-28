import urllib.request, json

base = 'http://localhost:8000'

# Health
h = json.loads(urllib.request.urlopen(f'{base}/api/health').read())
print('[Health]', h)

# Machines
m = json.loads(urllib.request.urlopen(f'{base}/api/machines').read())
top = m[0]
print(f'[Machines] {len(m)} machines')
print(f'  Top: {top["name"]} | risk={top.get("latest_risk")} | sev={top.get("severity")}')

# Alerts
a = json.loads(urllib.request.urlopen(f'{base}/api/alerts').read())
print(f'[Alerts] {len(a)} alerts')
if a:
    print(f'  Top: machine={a[0]["machine_name"]} risk={a[0]["risk_score"]} sev={a[0]["severity"]}')

# OEE
o = json.loads(urllib.request.urlopen(f'{base}/api/oee').read())
print(f'[OEE] plant_oee={o["plant_oee"]}%  machines={len(o["per_machine"])}  trend_pts={len(o["trend"])}')

# Work orders
wo = json.loads(urllib.request.urlopen(f'{base}/api/workorders').read())
print(f'[WorkOrders] {len(wo)} work orders')

print('\nAll endpoints OK!')
