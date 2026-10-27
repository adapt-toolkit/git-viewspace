// SPDX-License-Identifier: Apache-2.0
// Copyright Adapt Framework Solutions
// Optional high-level FUSE routing layer. Ordinary data I/O stays in the
// kernel.
#define FUSE_USE_VERSION 317
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "descriptor.hh"
#include <openssl/sha.h>
#include <linux/magic.h>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fstream>
#include <fuse.h>
#include <fuse_lowlevel.h>
#include <grp.h>
#include <linux/fuse.h>
#include <memory>
#include <mutex>
#include <pwd.h>
#include <string>
#include <sys/file.h>
#include <sys/fsuid.h>
#include <sys/ioctl.h>
#include <sys/statfs.h>
#include <sys/xattr.h>
#include <unistd.h>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {
const std::string prefix = ".git-viewspace-#SF#-";
int rootfd = -1, fusefd = -1;
bool readonly_view = false, passthrough = false;
std::string state_dir;
std::atomic<unsigned long> ordinary_reads{0}, ordinary_writes{0},
    special_reads{0}, special_writes{0}, attribute_reads{0}, timestamp_writes{0};
struct Binding {
  std::string target, fault_file;
  dev_t dev;
  ino_t ino;
  int anchor = -1;
  std::mutex mutex;
  bool fault = false;
  int error = 0;
  struct stat descriptor_stat{};
  routing::Descriptor declaration;
  ~Binding() {
    if (anchor >= 0)
      close(anchor);
  }
};
struct Registration {
  int id = 0;
  unsigned handles = 0;
  std::string path;
};
struct Handle {
  std::string directory_path;
  DIR *directory = nullptr;
  int fd = -1;
  std::shared_ptr<Binding> binding;
  bool descriptor = false;
  std::string inode_key;
  std::vector<std::string> names;
  bool listed = false;
  std::shared_ptr<Registration> registration;
};
std::unordered_map<std::string, std::shared_ptr<Binding>> bindings;
std::unordered_map<std::string, unsigned> open_inodes;
std::string managed_root;
std::vector<std::string> readonly_paths;
std::unordered_set<std::string> protected_dirs;
std::vector<std::string> runtime_roots;
std::unordered_map<std::string, std::shared_ptr<Registration>> registrations;
// Only namespace/open/release operations use this lock; ordinary read/write
// bypass us.
std::recursive_mutex namespace_mutex;
const char *relative(const char *p) { return !p || !p[1] ? "." : p + 1; }
std::string name(const char *p) { return p ? p : ""; }
int result(int r) { return r < 0 ? -errno : r; }
Handle *handle(fuse_file_info *f) {
  return f ? reinterpret_cast<Handle *>(f->fh) : nullptr;
}
std::shared_ptr<Binding> binding(const char *p);
bool under(const std::string &path, const std::string &parent) {
  return path == parent || path.compare(0, parent.size()+1, parent + "/") == 0;
}
bool protected_readonly(const char *p) {
  for (const auto &parent : readonly_paths) if (under(name(p), parent)) return true;
  return false;
}
std::string inode_key(const struct stat &st) {
  return std::to_string(st.st_dev) + ":" + std::to_string(st.st_ino);
}
bool reserved(const char *p) {
  if (!p)
    return false;
  return routing::descriptor_name(p);
}

int mutate(const char *p, bool subtree = false) {
  if (readonly_view || protected_readonly(p))
    return -EROFS;
  if (reserved(p) || binding(p) || (subtree && protected_dirs.count(name(p))))
    return -EPERM;
  return 0;
}
[[noreturn]] void fault_persistence_failure() {
  perror("router: cannot persist fault; exiting uncertain");
  // Closing the connection makes the failure visible. The manager requires an
  // explicit rebind after a failed helper exit instead of trusting old identity.
  _exit(1);
}
void latch(Binding &b) {
  if (b.fault) return;
  b.fault = true;
  int fd = open(b.fault_file.c_str(),
                O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd >= 0) {
    const char message[] = "ESTALE: backing identity changed; explicit rebind required\n";
    if (write(fd, message, sizeof(message)-1) != sizeof(message)-1 || fsync(fd) < 0)
      fault_persistence_failure();
    if (close(fd) < 0) fault_persistence_failure();
    int d = open(state_dir.c_str(), O_DIRECTORY | O_RDONLY);
    if (d < 0 || fsync(d) < 0) fault_persistence_failure();
    if (close(d) < 0) fault_persistence_failure();
  } else if (errno != EEXIST) {
    fault_persistence_failure();
  }
  fprintf(stderr,"router: ESTALE binding target=%s\n",b.target.c_str());
}
int check(Binding &b) {
  std::lock_guard<std::mutex> guard(b.mutex);
  if (b.error) return -b.error;
  if (b.fault)
    return -ESTALE;
  struct stat s{};
  if (lstat(b.target.c_str(), &s) < 0 || !S_ISREG(s.st_mode) ||
      s.st_dev != b.dev || s.st_ino != b.ino) {
    latch(b);
    return -ESTALE;
  }
  return 0;
}
std::string hash(const std::string &value) {
  unsigned char digest[SHA256_DIGEST_LENGTH];
  SHA256(reinterpret_cast<const unsigned char *>(value.data()), value.size(), digest);
  std::string result;
  const char *hex = "0123456789abcdef";
  for (auto c : digest) { result += hex[c >> 4]; result += hex[c & 15]; }
  return result;
}
void durable_text(const std::string &path, const std::string &text) {
  std::string tmp = path + ".tmp-XXXXXX";
  int fd = mkstemp(tmp.data());
  if (fd < 0) throw routing::Error(errno, "cannot create routing state");
  size_t offset = 0;
  while (offset < text.size()) {
    auto n = write(fd, text.data()+offset, text.size()-offset);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) { int e=errno; close(fd); unlink(tmp.c_str()); throw routing::Error(e, "cannot write routing state"); }
    offset += n;
  }
  if (fsync(fd) < 0) { int e=errno; close(fd); unlink(tmp.c_str()); throw routing::Error(e, "cannot sync routing state"); }
  close(fd);
  if (rename(tmp.c_str(), path.c_str()) < 0) { int e=errno; unlink(tmp.c_str()); throw routing::Error(e, "cannot install routing state"); }
  auto parent = path.substr(0,path.find_last_of('/'));
  int d = open(parent.c_str(), O_RDONLY|O_DIRECTORY|O_CLOEXEC);
  if (d < 0) throw routing::Error(errno, "cannot open routing state directory");
  int rc = fsync(d), e=errno; close(d);
  if (rc < 0) throw routing::Error(e, "cannot sync routing state directory");
}
int external_open(const std::string &target) {
  if (under(target, managed_root)) throw routing::Error(EINVAL, "recursive routing target");
  int fd = open(target.c_str(), O_PATH|O_NOFOLLOW|O_CLOEXEC);
  if (fd < 0) throw routing::Error(errno, "external target unavailable");
  struct stat st{}; struct statfs fs{};
  char proc[80]; snprintf(proc,sizeof(proc),"/proc/self/fd/%d",fd);
  char resolved[4096]; auto n=readlink(proc,resolved,sizeof(resolved));
  bool recursive = n < 0 || n == sizeof(resolved) || under(std::string(resolved,n < 0 ? 0 : n),managed_root);
  if (fstat(fd,&st)<0 || fstatfs(fd,&fs)<0 || !S_ISREG(st.st_mode) ||
      fs.f_type == FUSE_SUPER_MAGIC || fs.f_type == OVERLAYFS_SUPER_MAGIC || recursive) {
    close(fd); throw routing::Error(EINVAL,"unsupported external target");
  }
  return fd;
}
std::shared_ptr<Binding> binding(const char *p) {
  if (!p || name(p)=="/" || reserved(p)) return {};
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  std::string path = name(p), desc = routing::descriptor_path(path);
  struct stat ds{}, ordinary{};
  if (fstatat(rootfd,relative(desc.c_str()),&ds,AT_SYMLINK_NOFOLLOW)<0) {
    // The descriptor prefix can exceed NAME_MAX for a valid ordinary name.
    // Let the ordinary filesystem operation validate the original path.
    if (errno==ENOENT || errno==ENAMETOOLONG) {
      bindings.erase(path);
      return {};
    }
    auto bad=std::make_shared<Binding>(); bad->error=errno; return bad;
  }
  auto bad=std::make_shared<Binding>();
  try {
    if (protected_readonly(p)) throw routing::Error(EROFS,"routing in submodule unsupported");
    if (fstatat(rootfd,relative(p),&ordinary,AT_SYMLINK_NOFOLLOW)==0)
      throw routing::Error(EEXIST,"real/virtual name collision");
    if (errno!=ENOENT) throw routing::Error(errno,"cannot inspect virtual path");
    auto found=bindings.find(path);
    if (found!=bindings.end()) {
      auto &old=found->second->descriptor_stat;
      if (old.st_dev==ds.st_dev && old.st_ino==ds.st_ino && old.st_size==ds.st_size &&
          old.st_mtim.tv_sec==ds.st_mtim.tv_sec && old.st_mtim.tv_nsec==ds.st_mtim.tv_nsec &&
          old.st_ctim.tv_sec==ds.st_ctim.tv_sec && old.st_ctim.tv_nsec==ds.st_ctim.tv_nsec)
        return found->second;
    }
    auto d=routing::read_descriptor(rootfd,relative(desc.c_str()),&ds);
    auto b=std::make_shared<Binding>(); b->declaration=d; b->target=d.target; b->descriptor_stat=ds;
    if (d.deleted) b->error=ENOENT;
    else {
      auto key=hash(d.encode());
      b->fault_file=state_dir+"/"+key+".fault";
      b->fault=access(b->fault_file.c_str(),F_OK)==0;
      std::string record=state_dir+"/"+key+".identity";
      errno=0;
      std::ifstream prior(record);
      if (!prior && errno!=ENOENT) throw routing::Error(errno ? errno : EIO,"cannot read admitted identity");
      if (prior) {
        unsigned long long dev,ino;
        if (!(prior>>dev>>ino)) throw routing::Error(EIO,"invalid admitted identity");
        b->dev=dev; b->ino=ino;
      }
      if (!b->fault) {
        try { b->anchor=external_open(b->target); }
        catch (const routing::Error &) { if (prior.is_open()) { latch(*b); } else throw; }
        struct stat st{};
        if (b->anchor>=0 && fstat(b->anchor,&st)<0) throw routing::Error(errno,"cannot stat external target");
        if (prior.is_open()) {
          if (b->anchor>=0 && (st.st_dev!=b->dev || st.st_ino!=b->ino)) latch(*b);
        } else {
          b->dev=st.st_dev; b->ino=st.st_ino;
          durable_text(record,std::to_string(b->dev)+" "+std::to_string(b->ino)+"\n");
        }
      }
    }
    if (bindings.size()>=256) bindings.erase(bindings.begin());
    bindings[path]=b;
    return b;
  } catch (const routing::Error &e) {
    fprintf(stderr,"router: descriptor %s: %s (errno=%d)\n",desc.c_str(),e.what(),e.code);
    bad->error=e.code; return bad;
  }
}
int pinned_open(Binding &b, int flags) {
  int e = check(b);
  if (e)
    return e;
  char path[80];
  snprintf(path, sizeof(path), "/proc/self/fd/%d", b.anchor);
  int fd = open(path, (flags & ~(O_CREAT | O_EXCL | O_NOFOLLOW | O_TRUNC)) |
                          O_CLOEXEC);
  if (fd < 0)
    return -errno;
  if (flags & O_TRUNC) {
    if (readonly_view) {
      close(fd);
      return -EROFS;
    }
    if (ftruncate(fd, 0) < 0) {
      e = -errno;
      close(fd);
      return e;
    }
  }
  return fd;
}
void *initialize(fuse_conn_info *conn, fuse_config *cfg) {
  passthrough = fuse_set_feature_flag(conn, FUSE_CAP_PASSTHROUGH);
  conn->max_backing_stack_depth = FUSE_BACKING_STACKED_OVER;
  // Native backing syscalls and kernel passthrough perform privilege stripping.
  // Without this capability the FUSE layer queries security metadata per write.
  fuse_set_feature_flag(conn, FUSE_CAP_HANDLE_KILLPRIV_V2);
  fuse_unset_feature_flag(conn, FUSE_CAP_WRITEBACK_CACHE);
  cfg->entry_timeout = 0;
  cfg->attr_timeout = 0;
  cfg->negative_timeout = 0;
  cfg->hard_remove = 1;
  cfg->nullpath_ok = 1;
  fusefd = fuse_session_fd(fuse_get_session(fuse_get_context()->fuse));
  if (!passthrough) {
    fprintf(stderr, "router: kernel passthrough required\n");
    fuse_exit(fuse_get_context()->fuse);
  } else
    fprintf(stderr, "router: ready passthrough=1 bindings=%zu readonly=%d\n",
            bindings.size(), readonly_view);
  return nullptr;
}
int getattr_cb(const char *p, struct stat *s, fuse_file_info *fi) {
  attribute_reads++;
  auto *h = handle(fi);
  auto b = h ? h->binding : binding(p);
  if (b) {
    int e = check(*b);
    if (e)
      return e;
    return result(fstat(b->anchor, s));
  }
  return result(h ? fstat(h->fd, s)
                  : fstatat(rootfd, relative(p), s, AT_SYMLINK_NOFOLLOW));
}
int open_common(const char *p, fuse_file_info *fi, mode_t mode, bool creating) {
  if (reserved(p) && (creating || (fi->flags & O_ACCMODE)!=O_RDONLY || (fi->flags & O_TRUNC)))
    return -EPERM;
  if ((readonly_view || protected_readonly(p)) && ((fi->flags & O_ACCMODE) != O_RDONLY || creating ||
                        (fi->flags & O_TRUNC)))
    return -EROFS;
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  auto h = std::make_unique<Handle>();
  h->descriptor = reserved(p);
  h->binding = binding(p);
  if (h->binding) {
    if (creating && (fi->flags & O_EXCL))
      return -EEXIST;
    int fd = pinned_open(*h->binding, fi->flags);
    if (fd < 0)
      return fd;
    h->fd = fd;
    fi->direct_io = 1;
  } else {
    h->fd = openat(rootfd, relative(p),
                   fi->flags | O_CLOEXEC | (creating ? O_CREAT : 0), mode);
    if (h->fd < 0)
      return -errno;
    auto &slot = registrations[name(p)];
    if (!slot) {
      slot = std::make_shared<Registration>();
      slot->path = name(p);
    }
    if (!slot->id) {
      fuse_backing_map m{};
      m.fd = h->fd;
      slot->id = ioctl(fusefd, FUSE_DEV_IOC_BACKING_OPEN, &m);
      if (slot->id <= 0) {
        int e = errno;
        registrations.erase(name(p));
        close(h->fd);
        fprintf(stderr, "router: passthrough registration failed errno=%d\n",
                e);
        return -e;
      }
    }
    slot->handles++;
    h->registration = slot;
    fi->backing_id = slot->id;
  }
  struct stat opened{};
  if (fstat(h->fd,&opened)==0) { h->inode_key=inode_key(opened); open_inodes[h->inode_key]++; }
  fi->fh = reinterpret_cast<uint64_t>(h.release());
  return 0;
}
int open_cb(const char *p, fuse_file_info *fi) {
  return open_common(p, fi, 0, false);
}
int create_cb(const char *p, mode_t m, fuse_file_info *fi) {
  return open_common(p, fi, m, true);
}
int release_cb(const char *, fuse_file_info *fi) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  std::unique_ptr<Handle> h(handle(fi));
  if (!h->inode_key.empty() && !--open_inodes[h->inode_key]) open_inodes.erase(h->inode_key);
  if (h->registration) {
    std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
    auto &slot = *h->registration;
    if (!--slot.handles) {
      if (ioctl(fusefd, FUSE_DEV_IOC_BACKING_CLOSE, &slot.id) < 0)
        perror("router backing close");
      auto it = registrations.find(slot.path);
      if (it != registrations.end() && it->second == h->registration)
        registrations.erase(it);
    }
  }
  return result(close(h->fd));
}
int read_cb(const char *, char *buf, size_t size, off_t offset,
            fuse_file_info *fi) {
  auto *h = handle(fi);
  if (h->binding) {
    special_reads++;
    int e = check(*h->binding);
    if (e)
      return e;
  } else
    ordinary_reads++;
  return result(pread(h->fd, buf, size, offset));
}
int write_cb(const char *, const char *buf, size_t size, off_t offset,
             fuse_file_info *fi) {
  if (readonly_view)
    return -EROFS;
  auto *h = handle(fi);
  if (h->descriptor) return -EPERM;
  if (h->binding) {
    special_writes++;
    int e = check(*h->binding);
    if (e)
      return e;
  } else
    ordinary_writes++;
  return result(pwrite(h->fd, buf, size, offset));
}
int sync_cb(const char *, int data, fuse_file_info *fi) {
  auto *h = handle(fi);
  if (h->binding) {
    int e = check(*h->binding);
    if (e)
      return e;
  }
  return result(data ? fdatasync(h->fd) : fsync(h->fd));
}
int flush_cb(const char *, fuse_file_info *fi) {
  auto *h = handle(fi);
  if (h->binding)
    return check(*h->binding);
  return 0;
}
int truncate_cb(const char *p, off_t n, fuse_file_info *fi) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  if (readonly_view)
    return -EROFS;
  if (reserved(p))
    return -EPERM;
  auto *h = handle(fi);
  if (h && h->descriptor) return -EPERM;
  auto b = h ? h->binding : binding(p);
  if (b) {
    int e = check(*b);
    if (e)
      return e;
  }
  if (h)
    return result(ftruncate(h->fd, n));
  int fd = b ? pinned_open(*b, O_WRONLY)
             : openat(rootfd, relative(p), O_WRONLY | O_CLOEXEC);
  if (fd < 0)
    return b ? fd : -errno;
  int r = result(ftruncate(fd, n));
  close(fd);
  return r;
}
int directory_open(const char *p, fuse_file_info *fi) {
  if (binding(p))
    return -ENOTDIR;
  int fd = openat(rootfd, relative(p), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (fd < 0)
    return -errno;
  DIR *d = fdopendir(fd);
  if (!d) {
    int e = errno;
    close(fd);
    return -e;
  }
  auto *h = new Handle;
  h->directory = d;
  h->directory_path = p;
  h->fd = fd;
  fi->fh = reinterpret_cast<uint64_t>(h);
  return 0;
}
int directory_read(const char *p, void *buf, fuse_fill_dir_t fill, off_t off,
                   fuse_file_info *fi, fuse_readdir_flags) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  auto *h=handle(fi);
  if (!p) p=h->directory_path.c_str();
  if (off<0) return -EINVAL;
  if (!h->listed) {
    std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
    rewinddir(h->directory);
    std::vector<std::string> names;
    std::unordered_set<std::string> seen;
    auto add = [&](const std::string &n) { if (seen.insert(n).second) names.push_back(n); };
    errno=0;
    while (auto *entry=readdir(h->directory)) {
      std::string leaf=entry->d_name;
      add(leaf);
      if (routing::descriptor_name(leaf)) {
        try {
          auto target=routing::virtual_name(leaf);
          auto path=(name(p)=="/" ? "" : name(p))+"/"+target;
          auto declaration=routing::read_descriptor(dirfd(h->directory),leaf);
          if (!declaration.deleted) add(target);
        } catch(const routing::Error &e) {
          // Keep the descriptor discoverable for repair. Target access returns
          // the detailed failure; listing never opens external backing files.
          fprintf(stderr,"router: listing descriptor %s: %s (errno=%d)\n",leaf.c_str(),e.what(),e.code);
        }
      }
      errno=0;
    }
    if (errno) return -errno;
    h->names=std::move(names); h->listed=true;
  }
  for (size_t i=off; i<h->names.size(); ++i)
    if (fill(buf,h->names[i].c_str(),nullptr,i+1,FUSE_FILL_DIR_DEFAULTS)) break;
  return 0;
}
int directory_release(const char *, fuse_file_info *fi) {
  std::unique_ptr<Handle> h(handle(fi));
  return result(closedir(h->directory));
}
int mkdir_cb(const char *p, mode_t m) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  int e = mutate(p);
  return e ? e : result(mkdirat(rootfd, relative(p), m));
}
int unlink_cb(const char *p) {
  if (readonly_view || protected_readonly(p)) return -EROFS;
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  if (protected_dirs.count(name(p))) return -EPERM;
  if (!reserved(p)) {
    auto b=binding(p);
    if (b) {
      if (b->error) return -b->error;
      try {
        auto d=b->declaration; d.deleted=true;
        auto desc=routing::descriptor_path(name(p));
        durable_text("/proc/self/fd/"+std::to_string(rootfd)+"/"+relative(desc.c_str()),d.encode());
        bindings.erase(name(p));
        return 0;
      } catch(const routing::Error &e) { return -e.code; }
    }
  }
  int r=result(unlinkat(rootfd,relative(p),0));
  if (!r) { registrations.erase(name(p)); bindings.clear(); }
  return r;
}
int rmdir_cb(const char *p) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  int e = mutate(p, true);
  return e ? e : result(unlinkat(rootfd, relative(p), AT_REMOVEDIR));
}
int rename_cb(const char *a, const char *b, unsigned flags) {
  if (readonly_view || protected_readonly(a) || protected_readonly(b)) return -EROFS;
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  if (protected_dirs.count(name(a)) || protected_dirs.count(name(b))) return -EPERM;
  bool destination=reserved(b), source=reserved(a);
  if (destination) {
    if (flags & ~RENAME_NOREPLACE) return -EINVAL;
    try {
      auto target=routing::virtual_name(name(b));
      struct stat st{}, collision{};
      auto d=routing::read_descriptor(rootfd,relative(a),&st);
      if (st.st_nlink!=1 || open_inodes.count(inode_key(st))) return -EBUSY;
      if (fstatat(rootfd,relative(target.c_str()),&collision,AT_SYMLINK_NOFOLLOW)==0) return -EEXIST;
      if (errno!=ENOENT) return -errno;
      for (const auto &path : runtime_roots)
        if (under(target,path) || under(path,target)) return -EPERM;
      if (!d.deleted) { int fd=external_open(d.target); close(fd); }
    } catch (const routing::Error &e) { return -e.code; }
  } else {
    if (source) return -EPERM;
    int e=mutate(a,true); if (!e) e=mutate(b,true);
    if (e) return e;
  }
  int r = result(renameat2(rootfd, relative(a), rootfd, relative(b), flags));
  if (r)
    return r;
  bindings.clear();
  // Open descriptors retain their registrations; names are only aliases for
  // future opens.
  std::vector<std::pair<std::string, std::shared_ptr<Registration>>> moved;
  std::string x = a, y = b;
  for (auto it = registrations.begin(); it != registrations.end();) {
    const auto key = it->first;
    bool from = key == x || key.rfind(x + "/", 0) == 0;
    bool to = key == y || key.rfind(y + "/", 0) == 0;
    if (from || to) {
      if (from)
        moved.emplace_back(y + key.substr(x.size()), it->second);
      else if (flags & RENAME_EXCHANGE)
        moved.emplace_back(x + key.substr(y.size()), it->second);
      it = registrations.erase(it);
    } else
      ++it;
  }
  for (auto &item : moved) {
    item.second->path = item.first;
    registrations[item.first] = item.second;
  }
  return 0;
}
int link_cb(const char *a, const char *b) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  int e = mutate(a);
  if (!e)
    e = mutate(b);
  return e ? e : result(linkat(rootfd, relative(a), rootfd, relative(b), 0));
}
int symlink_cb(const char *target, const char *p) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  int e = mutate(p);
  return e ? e : result(symlinkat(target, rootfd, relative(p)));
}
int readlink_cb(const char *p, char *buf, size_t n) {
  if (binding(p))
    return -EINVAL;
  ssize_t r = readlinkat(rootfd, relative(p), buf, n - 1);
  if (r < 0)
    return -errno;
  buf[r] = 0;
  return 0;
}
int chmod_cb(const char *p, mode_t mode, fuse_file_info *fi) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  if (readonly_view)
    return -EROFS;
  if (reserved(p))
    return -EPERM;
  auto *h = handle(fi);
  if (h && h->descriptor) return -EPERM;
  auto b = h ? h->binding : binding(p);
  if (b) {
    int e = check(*b);
    if (e)
      return e;
  }
  if (h)
    return result(fchmod(h->fd, mode));
  if (b) {
    char proc[80];
    snprintf(proc, sizeof(proc), "/proc/self/fd/%d", b->anchor);
    return result(chmod(proc, mode));
  }
  return result(fchmodat(rootfd, relative(p), mode, 0));
}
int chown_cb(const char *p, uid_t uid, gid_t gid, fuse_file_info *fi) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  if (readonly_view)
    return -EROFS;
  if (reserved(p))
    return -EPERM;
  auto *h = handle(fi);
  if (h && h->descriptor) return -EPERM;
  auto b = h ? h->binding : binding(p);
  if (b) {
    int e = check(*b);
    if (e)
      return e;
    return result(fchownat(b->anchor, "", uid, gid, AT_EMPTY_PATH));
  }
  return result(
      h ? fchown(h->fd, uid, gid)
        : fchownat(rootfd, relative(p), uid, gid, AT_SYMLINK_NOFOLLOW));
}
int time_cb(const char *p, const timespec tv[2], fuse_file_info *fi) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  timestamp_writes++;
  if (readonly_view)
    return -EROFS;
  if (reserved(p))
    return -EPERM;
  auto *h = handle(fi);
  if (h && h->descriptor) return -EPERM;
  auto b = h ? h->binding : binding(p);
  if (b) {
    int e = check(*b);
    if (e)
      return e;
  }
  if (h)
    return result(futimens(h->fd, tv));
  if (b) {
    char proc[80];
    snprintf(proc, sizeof(proc), "/proc/self/fd/%d", b->anchor);
    return result(utimensat(AT_FDCWD, proc, tv, 0));
  }
  return result(utimensat(rootfd, relative(p), tv, AT_SYMLINK_NOFOLLOW));
}
int statfs_cb(const char *, struct statvfs *s) {
  return result(fstatvfs(rootfd, s));
}
int access_cb(const char *p, int mode) {
  if (readonly_view && (mode & W_OK))
    return -EROFS;
  auto b = binding(p);
  if (b) {
    int e = check(*b);
    if (e)
      return e;
    return result(faccessat(b->anchor, "", mode, AT_EMPTY_PATH | AT_EACCESS));
  }
  return result(faccessat(rootfd, relative(p), mode, AT_EACCESS));
}
int flock_cb(const char *, fuse_file_info *fi, int op) {
  auto *h = handle(fi);
  return h->binding ? -EOPNOTSUPP : result(flock(h->fd, op));
}
int allocate_cb(const char *, int mode, off_t off, off_t len,
                fuse_file_info *fi) {
  if (readonly_view)
    return -EROFS;
  auto *h = handle(fi);
  if (h->descriptor) return -EPERM;
  return h->binding ? -EOPNOTSUPP : result(fallocate(h->fd, mode, off, len));
}
std::string sourcepath(const char *p) {
  return "/proc/self/fd/" + std::to_string(rootfd) + "/" + relative(p);
}
int getxattr_cb(const char *p, const char *key, char *value, size_t size) {
  if (binding(p))
    return -EOPNOTSUPP;
  return result(lgetxattr(sourcepath(p).c_str(), key, value, size));
}
int listxattr_cb(const char *p, char *value, size_t size) {
  if (binding(p))
    return -EOPNOTSUPP;
  return result(llistxattr(sourcepath(p).c_str(), value, size));
}
int setxattr_cb(const char *p, const char *key, const char *value, size_t size,
                int flags) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  int e = mutate(p);
  return e ? e
           : result(lsetxattr(sourcepath(p).c_str(), key, value, size, flags));
}
int removexattr_cb(const char *p, const char *key) {
  std::lock_guard<std::recursive_mutex> lock(namespace_mutex);
  int e = mutate(p);
  return e ? e : result(lremovexattr(sourcepath(p).c_str(), key));
}
void destroy(void *) {
  fprintf(stderr,
          "router: callbacks ordinary_read=%lu ordinary_write=%lu "
          "special_read=%lu special_write=%lu\n",
          ordinary_reads.load(), ordinary_writes.load(), special_reads.load(),
          special_writes.load());
  fprintf(stderr, "router: metadata getattr=%lu utimens=%lu\n", attribute_reads.load(), timestamp_writes.load());
  bindings.clear();
  if (rootfd >= 0)
    close(rootfd);
}
std::string field(std::istream &in) {
  std::string s;
  if (!std::getline(in, s, '\0'))
    throw std::runtime_error("invalid binding table");
  return s;
}
} // namespace

int main(int argc, char **argv) {
  if (argc == 2 && std::string(argv[1]) == "--version") {
    puts("git-viewspace-router 2");
    return 0;
  }
  if (argc != 9 && argc != 10) {
    fprintf(stderr, "usage: git-viewspace-router SOURCE MOUNT TABLE STATE UID "
                    "GID ro|rw FSNAME\n");
    return 2;
  }
  try {
    auto uid = static_cast<uid_t>(std::stoul(argv[5]));
    auto gid = static_cast<gid_t>(std::stoul(argv[6]));
    readonly_view = std::string(argv[7]) == "ro";
    state_dir = argv[4];
    if (geteuid() != 0)
      throw std::runtime_error("mount and passthrough privileges required");
    passwd *pw = getpwuid(uid);
    if (!pw || initgroups(pw->pw_name, gid) < 0)
      throw std::runtime_error("cannot set owner groups");
    setfsgid(gid);
    setfsuid(uid);
    if (static_cast<uid_t>(setfsuid(-1)) != uid ||
        static_cast<gid_t>(setfsgid(-1)) != gid)
      throw std::runtime_error("cannot set filesystem owner");
    rootfd = open(argv[1], O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (rootfd < 0)
      throw std::runtime_error("cannot open source");
    std::ifstream input(argv[3], std::ios::binary);
    if (!input) throw std::runtime_error("cannot read router context");
    managed_root=field(input);
    while(input.peek()!=EOF) readonly_paths.push_back(field(input));
    if (argc == 10) {
      std::ifstream protected_input(argv[9], std::ios::binary);
      if (!protected_input) throw std::runtime_error("cannot read protected directories");
      while (protected_input.peek() != EOF) {
        auto parent = field(protected_input);
        if (parent.empty() || parent[0] != '/') throw std::runtime_error("invalid protected path");
        runtime_roots.push_back(parent);
        while (!parent.empty()) {
          protected_dirs.insert(parent);
          parent = parent.substr(0, parent.find_last_of('/'));
        }
      }
    }
    std::string options =
        "allow_other,default_permissions,fsname=" + std::string(argv[8]) +
        ",subtype=git-viewspace-router,nosuid,nodev" +
        (readonly_view ? ",ro" : "");
    std::vector<char *> args = {argv[0], argv[2], const_cast<char *>("-f"),
                                const_cast<char *>("-o"), options.data()};
    fuse_operations ops{};
    ops.getxattr = getxattr_cb;
    ops.listxattr = listxattr_cb;
    ops.setxattr = setxattr_cb;
    ops.removexattr = removexattr_cb;
    ops.init = initialize;
    ops.destroy = destroy;
    ops.getattr = getattr_cb;
    ops.open = open_cb;
    ops.create = create_cb;
    ops.release = release_cb;
    ops.read = read_cb;
    ops.write = write_cb;
    ops.fsync = sync_cb;
    ops.fsyncdir = sync_cb;
    ops.flush = flush_cb;
    ops.truncate = truncate_cb;
    ops.opendir = directory_open;
    ops.readdir = directory_read;
    ops.releasedir = directory_release;
    ops.mkdir = mkdir_cb;
    ops.rmdir = rmdir_cb;
    ops.unlink = unlink_cb;
    ops.rename = rename_cb;
    ops.link = link_cb;
    ops.symlink = symlink_cb;
    ops.readlink = readlink_cb;
    ops.chmod = chmod_cb;
    ops.chown = chown_cb;
    ops.utimens = time_cb;
    ops.statfs = statfs_cb;
    ops.access = access_cb;
    ops.flock = flock_cb;
    ops.fallocate = allocate_cb;
    int rc =
        fuse_main(static_cast<int>(args.size()), args.data(), &ops, nullptr);
    return passthrough ? rc : 1;
  } catch (const std::exception &e) {
    fprintf(stderr, "router: %s\n", e.what());
    return 1;
  }
}
