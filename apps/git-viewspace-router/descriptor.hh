// SPDX-License-Identifier: Apache-2.0
// Copyright Adapt Framework Solutions
#pragma once

#include <cerrno>
#include <fcntl.h>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <unistd.h>

namespace routing {
inline const std::string prefix = ".git-viewspace-#SF#-";
inline constexpr size_t descriptor_limit = 4096;

struct Error : std::runtime_error {
  int code;
  Error(int value, const std::string &message)
      : std::runtime_error(message), code(value) {}
};

inline bool descriptor_name(const std::string &path) {
  auto at = path.find_last_of('/');
  return path.compare(at == std::string::npos ? 0 : at + 1,
                      prefix.size(), prefix) == 0;
}

inline std::string virtual_name(const std::string &path) {
  auto at = path.find_last_of('/');
  auto begin = at == std::string::npos ? 0 : at + 1;
  if (!descriptor_name(path)) throw Error(EINVAL, "not a descriptor name");
  auto leaf = path.substr(begin + prefix.size());
  if (leaf.empty() || leaf == "." || leaf == ".." ||
      leaf.compare(0, prefix.size(), prefix) == 0)
    throw Error(EINVAL, "invalid virtual basename");
  return path.substr(0, begin) + leaf;
}

inline std::string descriptor_path(const std::string &path) {
  auto at = path.find_last_of('/');
  auto begin = at == std::string::npos ? 0 : at + 1;
  return path.substr(0, begin) + prefix + path.substr(begin);
}

// Legacy one-line descriptors remain valid. The second line, when present,
// has exactly one meaning: the virtual name was deleted, not the backing file.
struct Descriptor {
  std::string target;
  bool deleted = false;
  std::string encode() const {
    return target + "\n" + (deleted ? "deleted\n" : "");
  }
};

inline bool utf8(const std::string &s) {
  for (size_t i = 0; i < s.size();) {
    unsigned char c = s[i++];
    if (c < 0x80) continue;
    unsigned value, count, minimum;
    if (c >= 0xc2 && c <= 0xdf) { value = c & 31; count = 1; minimum = 0x80; }
    else if (c >= 0xe0 && c <= 0xef) { value = c & 15; count = 2; minimum = 0x800; }
    else if (c >= 0xf0 && c <= 0xf4) { value = c & 7; count = 3; minimum = 0x10000; }
    else return false;
    while (count--) {
      if (i == s.size()) return false;
      unsigned char next = s[i++];
      if ((next & 0xc0) != 0x80) return false;
      value = (value << 6) | (next & 63);
    }
    if (value < minimum || value > 0x10ffff ||
        (value >= 0xd800 && value <= 0xdfff)) return false;
  }
  return true;
}

inline Descriptor parse(std::string data) {
  if (data.empty() || data.size() > descriptor_limit ||
      data.find('\0') != std::string::npos || !utf8(data))
    throw Error(EINVAL, "invalid descriptor encoding or size");
  if (data.back() == '\n') data.pop_back();
  auto line = data.find('\n');
  Descriptor d{data.substr(0, line), false};
  if (d.target.empty() || d.target.front() != '/')
    throw Error(EINVAL, "descriptor target must be absolute");
  if (line != std::string::npos) {
    if (data.substr(line + 1) != "deleted")
      throw Error(EINVAL, "invalid descriptor deletion marker");
    d.deleted = true;
  }
  return d;
}

inline Descriptor read_descriptor(int root, const std::string &path,
                                  struct stat *metadata = nullptr) {
  int fd = openat(root, path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
  if (fd < 0) throw Error(errno, "cannot open descriptor");
  try {
    struct stat st{};
    if (fstat(fd, &st) < 0) throw Error(errno, "cannot stat descriptor");
    if (!S_ISREG(st.st_mode) || st.st_size > static_cast<off_t>(descriptor_limit))
      throw Error(EINVAL, "descriptor must be a small regular file");
    std::string data;
    char buf[descriptor_limit + 1];
    while (data.size() <= descriptor_limit) {
      auto n = read(fd, buf, sizeof(buf) - data.size());
      if (n < 0) {
        if (errno == EINTR) continue;
        throw Error(errno, "cannot read descriptor");
      }
      if (!n) break;
      data.append(buf, n);
    }
    auto result = parse(data);
    if (metadata) *metadata = st;
    close(fd);
    return result;
  } catch (...) {
    close(fd);
    throw;
  }
}
} // namespace routing
