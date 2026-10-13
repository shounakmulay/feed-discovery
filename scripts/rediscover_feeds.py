import concurrent.futures
import importlib.util
import json
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

spec=importlib.util.spec_from_file_location('collector', str(Path(__file__).with_name('collect_feeds.py')))
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class Links(HTMLParser):
 def __init__(self):
  super().__init__(); self.links=[]
 def handle_starttag(self,tag,attrs):
  attrs=dict(attrs)
  if tag=='link' and 'alternate' in attrs.get('rel','').lower().split() and attrs.get('type','').lower() in ('application/rss+xml','application/atom+xml','application/rdf+xml') and attrs.get('href'):
   self.links.append((attrs['href'],attrs.get('title','')))
results=m.read_json(m.ROOT/'validation-results.json'); pages={}
for result in results:
 if result['accepted'] or result['http_status'] not in (200,404,410): continue
 candidate=result['candidate']; site=candidate.get('website_url')
 if not site: continue
 try: site=m.normalize_url(site)
 except ValueError: continue
 if urllib.parse.urlsplit(site).hostname in ('medium.com','www.medium.com'): continue
 pages.setdefault(site,[]).append(candidate)
def discover(item):
 site,candidates=item
 try:
  response,body=m.fetch(site,timeout=20)
  if response.get('error') or response['status']!=200: return []
  parser=Links(); parser.feed(body.decode('utf-8',errors='replace'))
  found=[]
  for href,title in parser.links[:6]:
   url=m.public_link(href,response['url'])
   if not url or 'comment' in (url+' '+title).lower(): continue
   for candidate in candidates:
    for category in candidate['categories']:
     found.append({'feed_url':url,'title':title or candidate['title'],'category':category,'website_url':response['url'],'source_url':response['url']})
  return found
 except Exception: return []
found=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
 for index,values in enumerate(executor.map(discover,pages.items()),1):
  found.extend(values)
  if index%25==0: print('Publisher pages checked',index,'/',len(pages),'feed links',len(found),flush=True)
existing=m.read_json(m.ROOT/'supplemental-sources.json')
supplemental = {json.dumps(item, sort_keys=True): item for item in existing + found}
m.write_json(m.ROOT/'supplemental-sources.json', list(supplemental.values()))
before=m.read_json(m.ROOT/'candidates.json'); merged=m.merge_candidates(before+found)
m.write_json(m.ROOT/'candidates.json',merged)
m.write_json(m.ROOT/'rediscovery-report.json',{'checked_at':m.now(),'publisher_pages_checked':len(pages),'new_unique_candidates':len(merged)-len(before),'advertised_feed_links':found})
print('Recovered',len(merged)-len(before),'new unique candidates from',len(pages),'publisher pages',flush=True)
