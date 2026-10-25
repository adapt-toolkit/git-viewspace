// SPDX-License-Identifier: Apache-2.0
#include "descriptor.hh"
#include <cassert>
#include <cstdio>

template<class F> void invalid(F f) {
  bool rejected = false;
  try { f(); } catch (const routing::Error &) { rejected = true; }
  assert(rejected);
}

int main() {
  using namespace routing;
  assert(parse("/external/file").target == "/external/file");
  assert(!parse("/external/file\n").deleted);
  auto removed = parse("/external/file\ndeleted\n");
  assert(removed.deleted);
  assert(parse(removed.encode()).deleted);
  assert(parse("/данные/файл\n").target == "/данные/файл");
  for (auto s : {"", "relative", "/file\nunknown", "/file\n\n",
                 "/file\ndeleted\nextra", "/file\xff", "/file\xc0\xaf"})
    invalid([&] { parse(s); });
  invalid([] { parse(std::string("/file\0x", 7)); });
  invalid([] { parse("/" + std::string(descriptor_limit, 'x')); });
  assert(virtual_name("/a/" + prefix + ".auth") == "/a/.auth");
  assert(descriptor_path("/a/.auth") == "/a/" + prefix + ".auth");
  invalid([] { virtual_name(prefix); });
  invalid([] { virtual_name(prefix + ".."); });
  invalid([] { virtual_name(prefix + prefix + "x"); });

  char temp[] = "/tmp/gvr-descriptor-XXXXXX";
  char *dir = mkdtemp(temp);
  assert(dir);
  int root = open(dir, O_RDONLY | O_DIRECTORY);
  assert(root >= 0);
  int fd = openat(root, "descriptor", O_WRONLY | O_CREAT | O_EXCL, 0600);
  assert(fd >= 0);
  assert(write(fd, "/external\ndeleted\n", 18) == 18);
  close(fd);
  assert(read_descriptor(root, "descriptor").deleted);
  assert(symlinkat("descriptor", root, "alias") == 0);
  invalid([&] { read_descriptor(root, "alias"); });
  assert(mkfifoat(root, "fifo", 0600) == 0);
  invalid([&] { read_descriptor(root, "fifo"); });
  for (auto name : {"descriptor", "alias", "fifo"}) unlinkat(root, name, 0);
  close(root);
  rmdir(dir);
  puts("descriptor grammar and bounded reads: passed");
}
