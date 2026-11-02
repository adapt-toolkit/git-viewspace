"""Privileged real-FUSE regression: python test_flock_lifetime.py ROUTER_BINARY.

Requires mount/unshare privileges and passthrough-capable libfuse/kernel.
FIXTURE_UID optionally exercises a nonroot backing identity. Uses only disposable
mounts/processes; a private namespace deliberately retains the retired mount.
"""
import os,sys,time,json,signal,subprocess,tempfile,fcntl,shutil,pwd,select
from pathlib import Path
if len(sys.argv)!=2:raise SystemExit('usage: test_flock_lifetime.py ROUTER_BINARY')
if os.geteuid()!=0:raise SystemExit('requires administrator mount/unshare privileges')
root=Path(tempfile.mkdtemp(prefix='fuse-flock-'));router=str(Path(sys.argv[1]).resolve(strict=True))
uid=int(os.environ.get('FIXTURE_UID','0'));os.chown(root,uid,uid)
processes=[];mounts=[];keeper=None;result={'router':router,'fixture_uid':uid}
def client_command(code,*args):
 command=[sys.executable,'-c',code,*map(str,args)]
 return ['setpriv','--reuid',str(uid),'--regid',str(uid),'--clear-groups',*command] if uid else command
def ready(proc):
 if not select.select([proc.stdout],[],[],10)[0]:raise TimeoutError('fixture client readiness')
 assert proc.stdout.readline().strip()=='ready'
def run(args):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=15)
def wait(fn):
 for _ in range(200):
  try:
   v=fn()
   if v:return v
  except (FileNotFoundError,ValueError):pass
  time.sleep(.05)
 raise TimeoutError('fixture condition')
def mountinfo(path):
 for line in Path('/proc/self/mountinfo').read_text().splitlines():
  if line.split()[4]==str(path):return line
 return None
def lockable(path):
 with path.open('r+') as f:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(f,fcntl.LOCK_UN);return True
  except BlockingIOError:return False
try:
 for name in ['target','control']:
  base=root/name
  for d in ['source','mount','state']:(base/d).mkdir(parents=True)
  (base/'context').write_bytes(str(base/'mount').encode()+b'\0')
  (base/'source/lock').touch();(base/'source/canary').write_text(name)
  for item in [base,*base.rglob('*')]:os.chown(item,uid,uid)
  proc=subprocess.Popen([router,str(base/'source'),str(base/'mount'),str(base/'context'),str(base/'state'),str(uid),str(uid),'rw','fixture-'+name],stdout=subprocess.DEVNULL,stderr=(base/'router.log').open('w'))
  processes.append(proc);mounts.append(base/'mount');wait(lambda:mountinfo(base/'mount'))
 target=root/'target';control=root/'control'
 normal = """import os,sys,fcntl
from pathlib import Path
p=Path(sys.argv[1])/'semantics'
f=p.open('w+');f.write('payload');f.flush();g=p.open('r+')
fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
try:fcntl.flock(g,fcntl.LOCK_EX|fcntl.LOCK_NB);raise AssertionError('exclusive lock bypass')
except BlockingIOError:pass
fcntl.flock(f,fcntl.LOCK_UN);fcntl.flock(f,fcntl.LOCK_SH|fcntl.LOCK_NB);fcntl.flock(g,fcntl.LOCK_SH|fcntl.LOCK_NB)
try:fcntl.flock(g,fcntl.LOCK_EX|fcntl.LOCK_NB);raise AssertionError('shared lock bypass')
except BlockingIOError:pass
fcntl.flock(f,fcntl.LOCK_UN);fcntl.flock(g,fcntl.LOCK_UN);g.close()
p.rename(p.with_name('renamed'));p=p.with_name('renamed');p.chmod(0)
fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(f,fcntl.LOCK_UN)
p.unlink();fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
f.seek(0);assert f.read()=='payload';f.close()
"""
 run(client_command(normal,target/'mount'));result['shared_exclusive_nonblocking_unlink_permissions_no_truncate']=True
 line=mountinfo(target/'mount');connection=int(line.split()[2].split(':')[1]);result['connection']=connection
 client=subprocess.Popen(client_command("import fcntl,sys,time;f=open(sys.argv[1],'r+');fcntl.flock(f,fcntl.LOCK_EX);print('ready',flush=True);time.sleep(300)",target/'mount/lock'),stdout=subprocess.PIPE,text=True)
 processes.append(client);ready(client)
 if len(sys.argv)>1:
  assert not lockable(target/'source/lock')
  (target/'mount/lock').rename(target/'mount/renamed')
  assert not lockable(target/'source/renamed')
  client.terminate();client.wait();wait(lambda:lockable(target/'source/renamed'))
  (target/'mount/renamed').rename(target/'mount/lock')
  result['normal_exclusion_rename_release']=True
  client=subprocess.Popen(client_command("import fcntl,sys,time;f=open(sys.argv[1],'r+');fcntl.flock(f,fcntl.LOCK_EX);print('ready',flush=True);time.sleep(300)",target/'mount/lock'),stdout=subprocess.PIPE,text=True)
  processes.append(client);ready(client)
 keeper=subprocess.Popen(['unshare','--mount','--propagation','private',sys.executable,'-c',"import time;print('ready',flush=True);time.sleep(300)"],stdout=subprocess.PIPE,text=True)
 ready(keeper);result['private_namespace_pid']=keeper.pid
 processes[0].kill();processes[0].wait();client.kill();client.wait()
 run(['umount',str(target/'mount')]);mounts.remove(target/'mount')
 result['lock_retained_before_abort']=not lockable(target/'source/lock')
 orphan=Path('/sys/fs/fuse/connections')/str(connection)
 if orphan.exists():(orphan/'abort').write_text('1')
 time.sleep(.1);result['lock_released_after_abort']=lockable(target/'source/lock')
 assert (control/'mount/canary').read_text()=='control';result['control_unaffected']=True
 assert not result['lock_retained_before_abort'], 'router death retained backing lock in pinned mount'
 assert result['lock_released_after_abort']
 keeper.kill();keeper.wait();keeper=None
 result['result']='PASS';print(json.dumps(result))
finally:
 if keeper and keeper.poll() is None:keeper.kill();keeper.wait()
 for p in processes:
  if p.poll() is None:p.kill();p.wait()
 for mount in reversed(mounts):
  if mountinfo(mount):subprocess.run(['umount',str(mount)],check=True,timeout=10)
 shutil.rmtree(root)
