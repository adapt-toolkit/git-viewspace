#define FUSE_USE_VERSION 317
#define _GNU_SOURCE
#include <fuse.h>
#include <fuse_lowlevel.h>
#include <linux/fuse.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <dirent.h>
#include <string.h>
#include <pthread.h>
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
static int shared_id, opens;
static int rootfd, fusefd, fast;
static unsigned long reads, writes;
static const char *rel(const char *p) {return p[1]?p+1:".";}
static void *init(struct fuse_conn_info *c,struct fuse_config *cfg) {
 fast=getenv("PROBE_FAST")!=NULL && fuse_set_feature_flag(c,FUSE_CAP_PASSTHROUGH);
 if(fast)c->max_backing_stack_depth=FUSE_BACKING_STACKED_OVER;
 cfg->entry_timeout=0;cfg->attr_timeout=0;cfg->negative_timeout=0;
 fusefd=fuse_session_fd(fuse_get_session(fuse_get_context()->fuse));
 fprintf(stderr,"passthrough=%d\n",fast);return NULL;
}
static int attrs(const char *p,struct stat *s,struct fuse_file_info *fi){return (fi?fstat(fi->fh,s):fstatat(rootfd,rel(p),s,AT_SYMLINK_NOFOLLOW))<0?-errno:0;}
static int opened(const char *p,struct fuse_file_info *fi){
 int fd=openat(rootfd,rel(p),fi->flags);if(fd<0)return -errno;fi->fh=fd;
 if(fast){pthread_mutex_lock(&lock);if(!shared_id){struct fuse_backing_map m={.fd=fd};shared_id=ioctl(fusefd,FUSE_DEV_IOC_BACKING_OPEN,&m);}if(shared_id<=0){int e=errno;shared_id=0;pthread_mutex_unlock(&lock);close(fd);return -e;}fi->backing_id=shared_id;opens++;pthread_mutex_unlock(&lock);}
 else fi->direct_io=1;
 return 0;
}
static int released(const char *p,struct fuse_file_info *fi){(void)p;if(fast){pthread_mutex_lock(&lock);if(!--opens){ioctl(fusefd,FUSE_DEV_IOC_BACKING_CLOSE,&shared_id);shared_id=0;}pthread_mutex_unlock(&lock);}close(fi->fh);return 0;}
static int readfile(const char *p,char *b,size_t n,off_t o,struct fuse_file_info *fi){(void)p;__sync_fetch_and_add(&reads,1);int r=pread(fi->fh,b,n,o);return r<0?-errno:r;}
static int writefile(const char *p,const char *b,size_t n,off_t o,struct fuse_file_info *fi){(void)p;__sync_fetch_and_add(&writes,1);int r=pwrite(fi->fh,b,n,o);return r<0?-errno:r;}
static int syncfile(const char *p,int data,struct fuse_file_info *fi){(void)p;return (data?fdatasync(fi->fh):fsync(fi->fh))<0?-errno:0;}
static int listing(const char *p,void *b,fuse_fill_dir_t fill,off_t off,struct fuse_file_info *fi,enum fuse_readdir_flags flags){(void)fi;(void)flags;int fd=openat(rootfd,rel(p),O_RDONLY|O_DIRECTORY);if(fd<0)return -errno;DIR *d=fdopendir(fd);if(off)seekdir(d,off);struct dirent *e;while((e=readdir(d))){if(fill(b,e->d_name,NULL,telldir(d),0))break;}closedir(d);return 0;}
static void done(void *p){(void)p;fprintf(stderr,"callbacks read=%lu write=%lu\n",reads,writes);close(rootfd);}
int main(int argc,char **argv){if(argc<3)return 2;rootfd=open(argv[1],O_RDONLY|O_DIRECTORY);if(rootfd<0)return 3;struct fuse_operations ops={.init=init,.getattr=attrs,.open=opened,.release=released,.read=readfile,.write=writefile,.fsync=syncfile,.readdir=listing,.destroy=done};argv[1]=argv[0];return fuse_main(argc-1,argv+1,&ops,NULL);}
