"""Real local HTTP instrument preflight only. Sink never invokes a provider or emits LLM usage."""
import concurrent.futures
import copy
import hashlib
import http.client
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from o02_gateway_guard import GuardState, server_for, LLM_PATH

ROOT=Path('/private/tmp/dify-o02-guard-instrument-20260930')
KEY=secrets.token_urlsafe(32)  # disposable instrument transport secret; never printed
FIXTURE={name:str(uuid.uuid4()) for name in ['tenant_id','user_id','app_id','agent_id','agent_config_version_id']}
FIXTURE.update(user_from='account',invoke_from='debugger',agent_mode='agent_app',agent_config_version_kind='draft')
RUN=str(uuid.uuid4())
BODY_BYTES=b'event: transport_probe\ndata: {"no_model_fixture":true,"sequence":1}\n\nevent: transport_probe\ndata: {"no_model_fixture":true,"sequence":2}\n\n'
class Sink(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 count=0
 checks=[]
 gate=threading.Event()
 lock=threading.Lock()
 def log_message(self,*args): pass
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
  with type(self).lock:
   type(self).count+=1
   type(self).checks.append({'body_sha256':hashlib.sha256(raw).hexdigest(),'transport_key_unchanged':self.headers.get('X-Inner-Api-Key')==KEY,'content_type':self.headers.get('Content-Type')})
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('X-No-Model-Fixture','true');self.send_header('Content-Length',str(len(BODY_BYTES)));self.end_headers()
  # These are explicitly transport-probe events, not model output or usage.
  self.wfile.write(BODY_BYTES[:40]);self.wfile.flush()
  if self.headers.get('X-Transport-Preflight-Gate')=='true': type(self).gate.wait(2)
  else: time.sleep(0.015)
  self.wfile.write(BODY_BYTES[40:]);self.wfile.flush()

def start(server):
 thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();return thread

def policy(name):
 return {'package_id':f'no-model-preflight-{name}','forward_limit':2,'caller':FIXTURE,
 'provider':'langgenius/deepseek/deepseek','model':'deepseek-flash','max_output_tokens':8,
 'allowed_parameter_names':['max_tokens','temperature'],'prompt_json_char_limit':512,
 'allowed_tool_names':[],'max_body_bytes':65536,'upstream_timeout_seconds':1}

def body(index):
 caller={**FIXTURE,'agent_run_id':RUN,'call_index':index,'invocation_id':str(uuid.uuid5(uuid.NAMESPACE_URL,f'dify-agent:{RUN}:llm:{index}'))}
 return {'caller':caller,'target':{'provider':'langgenius/deepseek/deepseek','model':'deepseek-flash','prompt_messages':[{'role':'user','content':'NO-MODEL transport fixture'}],'model_parameters':{'max_tokens':8,'temperature':0.7},'tools':None,'stop':None,'stream':True}}

def send(server,payload,key=KEY):
 connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
 try:
  connection.request('POST',LLM_PATH,body=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Inner-Api-Key':key})
  response=connection.getresponse();data=response.read()
  return {'status':response.status,'content_type':response.getheader('Content-Type'),'no_model_fixture':response.getheader('X-No-Model-Fixture'),'body_sha256':hashlib.sha256(data).hexdigest()}
 finally: connection.close()

sink=ThreadingHTTPServer(('127.0.0.1',0),Sink);start(sink)
servers=[];reports=[]
working=Path(tempfile.mkdtemp(prefix='preflight-ledgers-',dir=ROOT));os.chmod(working,0o700)
def guard(name,upstream=None):
 state=GuardState(policy(name),KEY,working/f'{name}.json',upstream or f'http://127.0.0.1:{sink.server_port}')
 server=server_for(state);servers.append(server);start(server);return state,server

def check(name, condition, **safe):
 reports.append({'case':name,'passed':bool(condition),**safe})
 if not condition: raise AssertionError(name)
try:
 s,g=guard('two-slots'); before=Sink.count
 responses=[send(g,body(i)) for i in (1,2,3)]
 check('two_forwards_third_denied', [x['status'] for x in responses]==[200,200,429] and Sink.count-before==2 and s.ledger['reserved_forward_slots']==2,statuses=[x['status'] for x in responses],sink_receives=Sink.count-before,slots=s.ledger['reserved_forward_slots'])
 check('raw_stream_and_headers_preserved', all(x['body_sha256']==hashlib.sha256(BODY_BYTES).hexdigest() and x['content_type']=='text/event-stream' and x['no_model_fixture']=='true' for x in responses[:2]),response_sha256=responses[0]['body_sha256'])
 check('original_request_bytes_key_headers_preserved',all(x['transport_key_unchanged'] and x['content_type']=='application/json' for x in Sink.checks[:2]) and [x['body_sha256'] for x in Sink.checks[:2]]==[hashlib.sha256(json.dumps(body(i)).encode()).hexdigest() for i in (1,2)])
 s_progress,g_progress=guard('progressive-stream');Sink.gate.clear()
 c=http.client.HTTPConnection('127.0.0.1',g_progress.server_port,timeout=1)
 try:
  c.request('POST',LLM_PATH,body=json.dumps(body(1)).encode(),headers={'Content-Type':'application/json','X-Inner-Api-Key':KEY,'X-Transport-Preflight-Gate':'true'})
  r=c.getresponse();first=r.read(1)
  check('progressive_stream_before_upstream_completion',first==BODY_BYTES[:1] and not Sink.gate.is_set(),status=r.status)
  Sink.gate.set();complete=first+r.read()
  check('progressive_stream_full_hash_matches',hashlib.sha256(complete).hexdigest()==hashlib.sha256(BODY_BYTES).hexdigest())
 finally: Sink.gate.set();c.close()
 restored=GuardState(policy('two-slots'),KEY,working/'two-slots.json',f'http://127.0.0.1:{sink.server_port}')
 check('durable_restart_budget_unchanged',restored.ledger['reserved_forward_slots']==2 and restored.admit(body(4))[0]==429,slots=restored.ledger['reserved_forward_slots'])
 changed=policy('two-slots');changed['max_output_tokens']=64
 try: GuardState(changed,KEY,working/'two-slots.json',f'http://127.0.0.1:{sink.server_port}'); policy_rejected=False
 except ValueError: policy_rejected=True
 check('immutable_policy_hash',policy_rejected)
 s,g=guard('negative-scopes');before=Sink.count
 cases=[]
 for label,change in [('foreign_caller',lambda x:x['caller'].update(user_id=str(uuid.uuid4()))),('foreign_agent',lambda x:x['caller'].update(agent_id=str(uuid.uuid4()))),('different_model',lambda x:x['target'].update(model='unapproved')),('cap_above_eight',lambda x:x['target']['model_parameters'].update(max_tokens=9)),('alternate_token_parameter',lambda x:x['target']['model_parameters'].update(max_completion_tokens=64))]:
  payload=body(1);change(payload);r=send(g,payload);cases.append(r['status']);check(label,r['status']==403 and Sink.count==before,status=r['status'],additional_sink_receives=Sink.count-before)
 r=send(g,body(1),key='invalid-instrument-key');check('wrong_transport_key_zero_forward',r['status']==403 and Sink.count==before,status=r['status'])
 check('negative_requests_do_not_reserve_slots',s.ledger['reserved_forward_slots']==0,slots=s.ledger['reserved_forward_slots'])
 s,g=guard('duplicates');before=Sink.count;r1=send(g,body(1));r2=send(g,body(1));check('duplicate_invocation_not_replayed',r1['status']==200 and r2['status']==409 and Sink.count-before==1 and s.ledger['reserved_forward_slots']==1,statuses=[r1['status'],r2['status']],sink_receives=Sink.count-before)
 s,g=guard('concurrent');before=Sink.count
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(lambda i:send(g,body(i)),range(1,5)))
 statuses=sorted(x['status'] for x in results);check('atomic_concurrent_budget',statuses==[200,200,429,429] and Sink.count-before==2 and s.ledger['reserved_forward_slots']==2,statuses=statuses,sink_receives=Sink.count-before)
 # An unused local port simulates uncertain upstream connect without any model path.
 unavailable=ThreadingHTTPServer(('127.0.0.1',0),Sink);port=unavailable.server_port;unavailable.server_close()
 s,g=guard('unknown-send',f'http://127.0.0.1:{port}');before=Sink.count
 responses=[send(g,body(i)) for i in (1,2,3)]
 check('unknown_send_never_releases_slot',[x['status'] for x in responses]==[502,502,429] and s.ledger['reserved_forward_slots']==2 and Sink.count==before,statuses=[x['status'] for x in responses],slots=s.ledger['reserved_forward_slots'],sink_receives=Sink.count-before)
 check('unknown_outcomes_persisted',all(r['outcome']=='unknown_send_or_stream_outcome' for r in s.ledger['receipts']))
 report={'instrument':'acceptance-only local HTTP guard','model_calls':0,'provider_requests':0,'fake_model_responses':0,'sink_semantics':'explicit transport_probe SSE only, no LLM output or usage','cases':reports,'passed':all(x['passed'] for x in reports),'private_ledger_directory':str(working),'guard_sha256':hashlib.sha256(Path(__file__).with_name('o02_gateway_guard.py').read_bytes()).hexdigest()}
 (ROOT/'preflight-results.json').write_text(json.dumps(report,indent=2)+'\n');os.chmod(ROOT/'preflight-results.json',0o600)
 print(json.dumps({'passed':report['passed'],'cases':len(reports),'model_calls':0,'provider_requests':0,'guard_sha256':report['guard_sha256'],'result_path':str(ROOT/'preflight-results.json')}))
finally:
 for server in servers: server.shutdown();server.server_close()
 sink.shutdown();sink.server_close()
