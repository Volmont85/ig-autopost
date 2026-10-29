"""Scoped one-off AVANS channel publication; claims before send, never retries writes."""
import base64,hashlib,json,os,re,sys,uuid
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from datetime import datetime,timezone
REPO=os.environ.get('GITHUB_REPOSITORY','Volmont85/ig-autopost')
PAYLOAD_PATH='publication/avans-20261029/telegram_payload.json'
STATE_PATH='publication/avans-20261029/telegram_receipt.json'

def gh(path,body=None,method='GET',missing=False):
 data=json.dumps(body).encode() if body is not None else None
 req=Request('https://api.github.com/repos/'+REPO+'/'+path,data=data,method=method,headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':'application/json'})
 try:
  with urlopen(req,timeout=45) as r:return json.load(r)
 except HTTPError as e:
  if e.code==404 and missing:return None
  raise RuntimeError('GitHub operation failed: HTTP '+str(e.code)) from None

def put_state(value,previous=None):
 body={'message':'Record AVANS Telegram publication state','content':base64.b64encode((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()).decode()}
 if previous:body['sha']=previous['sha']
 gh('contents/'+STATE_PATH,body,'PUT')
 return gh('contents/'+STATE_PATH)

def tg(method,payload,files=None):
 # No retry: an uncertain outcome must be inspected using saved claim/run evidence.
 try:
  data=json.dumps(payload).encode();ctype='application/json'
  if files:
   boundary='Avans'+uuid.uuid4().hex;parts=[]
   for key,value in payload.items():
    parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+str(value)+'\r\n').encode())
   for key,value in files.items():
    parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"; filename="'+key+'.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode()+value+b'\r\n')
   data=b''.join(parts)+('--'+boundary+'--\r\n').encode();ctype='multipart/form-data; boundary='+boundary
  req=Request('https://api.telegram.org/bot'+os.environ['TG_LEADMAGNET_BOT_TOKEN']+'/'+method,data=data,headers={'Content-Type':ctype})
  with urlopen(req,timeout=60) as r:d=json.load(r)
  if not d.get('ok'):raise RuntimeError('Telegram API rejected '+method)
  return d['result']
 except HTTPError as e:
  try:detail=json.loads(e.read());message=re.sub(r'bot[0-9]+:[A-Za-z0-9_-]+','[redacted]',str(detail.get('description','')))[:250]
  except Exception:message='No JSON description'
  raise RuntimeError('Telegram '+method+' rejected HTTP '+str(e.code)+': '+message) from None
 except (URLError,TimeoutError) as e:raise RuntimeError('Telegram '+method+' uncertain or rejected; no retry; '+type(e).__name__) from None

def main():
 raw=gh('contents/'+PAYLOAD_PATH);p=json.loads(base64.b64decode(raw['content']))
 assert p['channel_username']=='goszakupki_help' and len(p['photos'])==4
 assert len(re.sub('<[^>]*>','',p['caption']))<1024
 me=tg('getMe',{});chat=tg('getChat',{'chat_id':os.environ['TG_GOSZAKUPKI_HELP_CHAT_ID']})
 assert chat.get('type')=='channel' and chat.get('username')=='goszakupki_help','Unexpected channel'
 role=tg('getChatMember',{'chat_id':chat['id'],'user_id':me['id']})
 assert role.get('status') in ['administrator','creator'] and role.get('can_post_messages',role.get('status')=='creator') is True,'Bot cannot post to channel'
 for url in p['photos']:
  assert url.startswith(('https://goszakupki.info/media/releases/avans-20261029/','https://raw.githubusercontent.com/Volmont85/ig-autopost/'))
  with urlopen(Request(url,method='HEAD'),timeout=30) as r:assert r.status==200
 prev=gh('contents/'+STATE_PATH,missing=True)
 if os.environ.get('PUBLISH_MODE')=='preflight':
  with urlopen('https://t.me/s/goszakupki_help',timeout=30) as r:html=r.read().decode()
  posts=re.findall(r'data-post="(goszakupki_help/[0-9]+)"',html)
  print(json.dumps({'public_latest_posts':posts[-12:],'contains_avans_publication':'Аванс в проекте — ещё не деньги на поставку.' in html,'existing_receipt':json.loads(base64.b64decode(prev['content'])) if prev else None},ensure_ascii=False));return
 if prev:raise RuntimeError('Publication receipt/claim already exists; inspect before any resend')
 if os.environ.get('PUBLISH_MODE')!='publish':
  print(json.dumps({'mode':'preflight','channel':chat['username'],'bot_can_post':True,'photos':4,'existing_claim':False}));return
 state={'status':'claimed_before_send','channel':'goszakupki_help','run_id':os.environ.get('GITHUB_RUN_ID'),'payload_sha256':hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),'claimed_at':datetime.now(timezone.utc).isoformat()}
 prev=put_state(state)
 media=[{'type':'photo','media':u} for u in p['photos']];media[0].update(caption=p['caption'],parse_mode='HTML')
 try:result=tg('sendMediaGroup',{'chat_id':chat['id'],'media':media})
 except Exception:
  state['status']='uncertain_do_not_retry';put_state(state,prev);raise
 state.update(status='published',message_ids=[x['message_id'] for x in result],links=['https://t.me/goszakupki_help/'+str(x['message_id']) for x in result],published_at=datetime.now(timezone.utc).isoformat());put_state(state,prev)
 print(json.dumps({k:state[k] for k in ['status','message_ids','links']},ensure_ascii=False))
if __name__=='__main__':
 try:main()
 except Exception as exc:print(str(exc) if isinstance(exc,(AssertionError,RuntimeError)) else 'Scoped publication stopped: '+type(exc).__name__,file=sys.stderr);sys.exit(1)
