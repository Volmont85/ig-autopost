import base64,hashlib,json,os,sys,time
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError
ROOT='publication/nb-r01-dashboard-20261029/'
REPO=os.environ['GITHUB_REPOSITORY']
def gh(path,body=None,missing=False):
 req=Request('https://api.github.com/repos/'+REPO+'/contents/'+path,data=json.dumps(body).encode() if body else None,headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':'application/json'})
 try:
  with urlopen(req,timeout=40) as r:return json.load(r)
 except HTTPError as e:
  if missing and e.code==404:return None
  raise RuntimeError('GitHub HTTP '+str(e.code)) from None
def save(state,prev=None):
 body={'message':'Record approved R01 publication state [skip ci]','content':base64.b64encode(json.dumps(state,ensure_ascii=False,indent=2).encode()).decode()}
 if prev:body['sha']=prev['sha']
 gh(ROOT+'receipt.json',body);return gh(ROOT+'receipt.json')
def ig(path,params,post=False):
 params=dict(params,access_token=os.environ['IG_NEYROBYURO_TOKEN'])
 url='https://graph.instagram.com/v21.0/'+path
 req=Request(url if post else url+'?'+urlencode(params),data=urlencode(params).encode() if post else None)
 try:
  with urlopen(req,timeout=60) as r:return json.load(r)
 except HTTPError as e:
  try:msg=json.loads(e.read()).get('error',{}).get('message','')[:250]
  except:msg=''
  raise RuntimeError('Instagram HTTP '+str(e.code)+' '+msg) from None
def main():
 account=os.environ['IG_NEYROBYURO_ID']
 profile=ig(account,{'fields':'id,username'})
 assert profile['username']=='neyrobyuro','Unexpected account'
 payload=json.load(open(ROOT+'payload.json'))
 assert payload['username']=='neyrobyuro' and len(payload['caption'])<=2200 and payload['caption'].count('#')==5
 recent=ig(account+'/media',{'fields':'id,caption,permalink,timestamp','limit':20})['data']
 matches=[m for m in recent if 'Из неаккуратной таблицы — в дашборд' in m.get('caption','')]
 prev=gh(ROOT+'receipt.json',missing=True)
 if os.environ['PUBLISH_MODE']=='preflight':
  print(json.dumps({'username':profile['username'],'matching_posts':matches,'existing_receipt':bool(prev),'latest_posts':[{k:m.get(k) for k in ['id','permalink','timestamp']} for m in recent[:3]]},ensure_ascii=False));return
 assert not prev and not matches,'Existing publication or claim; inspect before retry'
 for key in ['video_url','cover_url']:
  assert payload[key].startswith('https://neyrobyuro.ru/materials/dashboard/')
  with urlopen(payload[key],timeout=30) as r:data=r.read()
  assert hashlib.sha256(data).hexdigest()==payload[key.replace('_url','_sha256')]
 state={'status':'claimed','username':profile['username'],'run_id':os.environ['GITHUB_RUN_ID'],'payload_sha256':hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()}
 prev=save(state)
 try:
  container=ig(account+'/media',{'media_type':'REELS','video_url':payload['video_url'],'cover_url':payload['cover_url'],'caption':payload['caption'],'share_to_feed':'true'},True)['id']
  state.update(status='processing',container_id=container);prev=save(state,prev)
  for _ in range(60):
   status=ig(container,{'fields':'status_code'}).get('status_code')
   if status=='FINISHED':break
   assert status not in ['ERROR','EXPIRED'],'Container rejected: '+str(status)
   time.sleep(5)
  else:raise RuntimeError('Processing timeout; do not retry publication')
  state['status']='publishing';prev=save(state,prev)
  media=ig(account+'/media_publish',{'creation_id':container},True)['id']
  state.update(status='published',media_id=media);prev=save(state,prev)
  meta=ig(media,{'fields':'id,username,permalink,caption,timestamp,media_type,media_product_type'})
  state['verification']=meta;save(state,prev)
  print(json.dumps(state,ensure_ascii=False))
 except Exception:
  print('Stopped. Inspect durable receipt and account before any retry.',file=sys.stderr);raise
if __name__=='__main__':
 try:main()
 except Exception as e:print(str(e) if isinstance(e,(RuntimeError,AssertionError)) else type(e).__name__,file=sys.stderr);sys.exit(1)
