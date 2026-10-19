#!/bin/sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/../.." && pwd)
test_repo="$project_dir/test_repo"

rm -rf -- "$test_repo"
mkdir "$test_repo"
cd "$test_repo"
git init --quiet --initial-branch=master --object-format=sha1
git config user.name "GitViewspaceFS Test"
git config user.email git-viewspace-fs@example.invalid
git config commit.gpgSign false
git config tag.gpgSign false

date_commit()
{
  commit_date="$1"T12:00:00Z
  shift
  GIT_COMMITTER_DATE="$commit_date" GIT_AUTHOR_DATE="$commit_date" git commit --quiet "$@"
}

touch file_a empty
git add file_a empty
date_commit 2005-06-07 -m 'Add initial files'
git tag t20050607

printf 'Contents\n' >file_a
date_commit 2005-06-10 -am 'Update file a'

mkdir -p dir_a/dir_b/dir_c .git-parents
printf 'nested\n' >dir_a/dir_b/dir_c/file_ca
printf 'nearby\n' >dir_a/file_aa
printf 'repository content\n' >.git-parents/literal-parent
printf 'literal author file\n' >.author
printf 'literal author email file\n' >.author-email
printf 'literal descendants file\n' >.git-descendants
printf 'literal names file\n' >.git-names
printf '#!/bin/sh\necho executable\n' >executable.sh
chmod +x executable.sh
printf 'a\000b\n' >binary.bin
printf 'old\n' >version.txt
ln -s file_a link_relative
ln -s /absolute/target link_absolute
ln -s ../outside-the-view link_parent
ln -s does-not-exist link_dangling
ln -s café link_unicode
ln -s ../file_a dir_a/link_parent
git add --all
date_commit 2009-10-10 -m 'Add representative tree entries'

# Construct entries that a normal checkout cannot represent: malformed link
# targets and a gitlink.  They let the object-reader tests cover safe failures.
invalid_utf8_oid=$(printf '\377invalid' | git hash-object -w --stdin)
nul_oid=$(printf 'bad\000target' | git hash-object -w --stdin)
gitlink_oid=$(git rev-parse HEAD)
git update-index --add --cacheinfo "120000,$invalid_utf8_oid,link_invalid_utf8"
git update-index --add --cacheinfo "120000,$nul_oid,link_nul"
git update-index --add --cacheinfo "160000,$gitlink_oid,gitlink"
printf 'new\n' >version.txt
git add version.txt
GIT_AUTHOR_DATE='2009-10-10T12:00:00Z' \
  GIT_COMMITTER_DATE='2009-10-11T12:00:00Z' \
  git commit --quiet -m 'Add special entries'
GIT_COMMITTER_DATE='2009-10-12T12:00:00Z' \
  git tag tannotated -a -m 'Annotated test tag'

# Retain an existing but unreachable commit object.  Storing its ID in local
# Git configuration makes it discoverable to tests without creating a ref.
empty_tree=$(git mktree </dev/null)
dangling_commit=$(
  printf 'Unreachable test commit\n' |
    GIT_AUTHOR_NAME='GitViewspaceFS Test' \
    GIT_AUTHOR_EMAIL='git-viewspace-fs@example.invalid' \
    GIT_AUTHOR_DATE='2008-01-02T12:00:00Z' \
    GIT_COMMITTER_NAME='GitViewspaceFS Test' \
    GIT_COMMITTER_EMAIL='git-viewspace-fs@example.invalid' \
    GIT_COMMITTER_DATE='2008-01-02T12:00:00Z' \
    git commit-tree "$empty_tree"
)
git config viewspace-test.danglingCommit "$dangling_commit"
