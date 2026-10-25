import os,random,time,json,statistics
from pathlib import Path
root=Path(__file__).resolve().parent
random.seed(41)
rows=[]
for repeat in range(3):
 for size in (4096,65536,1048576):
  offsets=[random.randrange(0,(256*1048576-size)//4096)*4096 for _ in range(3000 if size<1048576 else 600)]
  for mode in ('read','write'):
   for name in (('overlay','routed') if repeat%2==0 else ('routed','overlay')):
    fd=os.open(root/name/'data',os.O_RDWR);buf=b'z'*size;times=[];start=time.perf_counter_ns()
    for off in offsets:
     t=time.perf_counter_ns()
     if mode=='read':assert len(os.pread(fd,size,off))==size
     else:assert os.pwrite(fd,buf,off)==size
     times.append(time.perf_counter_ns()-t)
    elapsed=time.perf_counter_ns()-start;os.fsync(fd);os.close(fd)
    row=dict(repeat=repeat,bytes=size,operation=mode,path=name,median_us=statistics.median(times)/1000,p95_us=sorted(times)[int(.95*len(times))]/1000,MiB_s=len(offsets)*size/(elapsed/1e9)/1048576)
    rows.append(row)
print(json.dumps(rows,indent=2))
(root/'benchmark-fast.json').write_text(json.dumps(rows,indent=2))
