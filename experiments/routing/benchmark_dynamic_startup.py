"""Disposable large-namespace startup comparison; no materialized source files.

Run with git-viewspace on PATH and --scratch-root on ext4.
"""
import argparse,os,sys,subprocess,tempfile,time,json,statistics,shutil
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--scratch-root',type=Path,required=True)
parser.add_argument('--router',type=Path,required=True)
args=parser.parse_args()
base=Path(tempfile.mkdtemp(prefix='perf-',dir=args.scratch_root))
exe=shutil.which('git-viewspace')
if not exe: raise RuntimeError('git-viewspace must be on PATH')
helper=str(args.router.resolve())
rows=[]
def run(*args,input=None):
 r=subprocess.run(list(map(str,args)),input=input,capture_output=True,timeout=120)
 if r.returncode: raise RuntimeError((args,r.stdout.decode(),r.stderr.decode()))
 return r.stdout
for count in (100,100000):
 root=base/str(count);root.mkdir();seed=root/'seed';seed.mkdir();vmroot=root/'vm'
 run('git','init','-b','main',seed)
 # Shared blob, shallow fanout: representative many-file namespace, tiny disk use.
 lines=[b'blob\nmark :1\ndata 1\nx\n',b'commit refs/heads/main\ncommitter Test <test@example.invalid> 1 +0000\ndata 4\nseed\n']
 lines += [f'M 100644 :1 directory-{i//100}/file-{i}\n'.encode() for i in range(count)]
 lines += [b'\ndone\n'];run('git','-C',seed,'fast-import','--quiet',input=b''.join(lines))
 run('git','-C',seed,'read-tree','HEAD')
 run('git','-C',seed,'update-index','--skip-worktree','-z','--stdin',input=b''.join(f'directory-{i//100}/file-{i}\0'.encode() for i in range(count)))
 def vm(*args):return run(exe,'--root',vmroot,*args)
 vm('init','--copy',seed,'--git-name','Test','--git-email','test@example.invalid')
 try:
  vm('start');vm('view','init','readonly','test','--at','main');vm('view','init','overlay','test','--branch','main')
  mount=vmroot/'views/test/mnt'
  for routed in (False,True):
   vm('view','routing','enable' if routed else 'disable','test',*(['--executable',helper] if routed else []))
   durations=[]
   for repeat in range(3):
    started=time.perf_counter();vm('view','start','test');durations.append(time.perf_counter()-started)
    path=mount/'directory-0/file-0';times=[]
    for i in range(100):
     t=time.perf_counter_ns();fd=os.open(path,os.O_RDONLY);os.fstat(fd);os.close(fd);times.append((time.perf_counter_ns()-t)/1000)
    vm('view','stop','test')
   rows.append(dict(files=count,routed=routed,start_seconds=durations,open_stat_close_median_us=statistics.median(times)))
 finally:vm('stop')
print(json.dumps(rows,indent=2));(base/'results.json').write_text(json.dumps(rows,indent=2));print(base)
