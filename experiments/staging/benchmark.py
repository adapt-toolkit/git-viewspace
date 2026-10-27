"""Disposable exact-path staging comparison. No mounts, production data or cache flushing.
Caller supplies a NEW scratch directory. Slow baselines are bounded and reported
as timeouts, never treated as completed timings. Retain results outside scratch
before explicitly removing it. Git must support NUL pathspec input (2.25+).
"""
import argparse,json,os,platform,resource,shutil,subprocess,time
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--entries',type=int,default=500000);p.add_argument('--changes',default='1000,10000,140000');p.add_argument('--timeout',type=int,default=90);a=p.parse_args()
 a.root.mkdir(mode=0o700);repo=a.root/'repo';repo.mkdir();os.nice(10)
 def git(args,env=None,data=None,timeout=180):
  return subprocess.run(['git','-C',str(repo),*args],env=env,input=data,capture_output=True,check=True,timeout=timeout).stdout
 def emit(d):
  with (a.root/'results.jsonl').open('a') as f:f.write(json.dumps(d)+'\n')
  print(json.dumps(d),flush=True)
 git(['init','-q']);git(['config','core.autocrlf','false']);git(['config','core.filemode','true']);git(['config','gc.auto','0'])
 emit({'phase':'fixture','entries':a.entries,'git':git(['--version']).decode().strip(),'platform':platform.platform(),'cache':'uncontrolled warm; no system cache flushing','threads':'Git defaults','root':str(a.root)})
 start=time.monotonic()
 def path(i):return repo/f'data/partition-{i//1000:04d}/record-{i:07d}.json'
 for i in range(a.entries):
  f=path(i)
  if i%1000==0:f.parent.mkdir(parents=True)
  f.write_bytes(b'{"value":"base"}\n')
 git(['add','-A'],timeout=300);base=git(['write-tree']).decode().strip();shutil.copyfile(repo/'.git/index',a.root/'base-index')
 emit({'phase':'fixture-ready','seconds':time.monotonic()-start,'base_tree':base,'index_bytes':(a.root/'base-index').stat().st_size})
 previous=[]
 for n in map(int,a.changes.split(',')):
  for f,new in previous:
   if new:f.unlink()
   else:f.write_bytes(b'{"value":"base"}\n')
  previous=[];paths=[]
  for i in range(n):
   f=path(i)
   if i%10==0:f.unlink();previous.append((f,False))
   elif i%10==1:f=f.with_name('added-'+f.name);f.write_bytes(b'{"value":"added"}\n');previous.append((f,True))
   else:f.write_bytes(b'{"value":"changed"}\n');previous.append((f,False))
   paths.append(f.relative_to(repo).as_posix())
  payload=b''.join(p.encode()+b'\0' for p in sorted(paths));roots=sorted({str(Path(p).parent) for p in paths});root_payload=b''.join(p.encode()+b'\0' for p in roots)
  trees=set();timed_out=set()
  for rep,methods in enumerate([['literal','roots','exact'],['exact','roots','literal']]):
   for method in methods:
    if method in timed_out:continue
    idx=a.root/'trial-index';shutil.copyfile(a.root/'base-index',idx);lock=Path(str(idx)+'.lock')
    if lock.exists():lock.unlink()
    env=os.environ.copy();env['GIT_INDEX_FILE']=str(idx);env['GIT_TRACE2_PERF']=str(a.root/f'{n}-{rep}-{method}.trace')
    cmd=['git','-C',str(repo)]
    cmd+=['update-index','--add','--remove','-z','--stdin'] if method=='exact' else ['--literal-pathspecs','add','-A','--force','--pathspec-from-file=-','--pathspec-file-nul']
    before=resource.getrusage(resource.RUSAGE_CHILDREN);start=time.monotonic();timeout=False
    emit({'phase':'trial-start','changes':n,'method':method,'repeat':rep+1})
    try:
     out=subprocess.run(cmd,env=env,input=root_payload if method=='roots' else payload,capture_output=True,timeout=a.timeout,check=True)
    except subprocess.TimeoutExpired:timeout=True;timed_out.add(method)
    after=resource.getrusage(resource.RUSAGE_CHILDREN);elapsed=time.monotonic()-start
    row={'phase':'trial','entries':a.entries,'changes':n,'roots':len(roots),'method':method,'repeat':rep+1,'seconds':elapsed,'timeout':timeout,'cpu_seconds':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,'children_peak_rss_kib':after.ru_maxrss,'input_blocks':after.ru_inblock-before.ru_inblock,'output_blocks':after.ru_oublock-before.ru_oublock}
    if not timeout:
     started=time.monotonic();row['tree']=git(['write-tree'],env=env).decode().strip();row['write_tree_seconds']=time.monotonic()-started;trees.add(row['tree'])
     started=time.monotonic();delta=git(['diff-index','--cached','--no-renames','--name-only','-z',base,'--'],env=env);changed={x.decode() for x in delta.split(b'\0') if x};assert changed==set(paths)
     row['delta_validation_seconds']=time.monotonic()-started
    emit(row)
   assert len(trees)<=1,'tree mismatch'
 emit({'phase':'complete','scratch_retained_for_explicit_cleanup':True})
if __name__=='__main__':main()
