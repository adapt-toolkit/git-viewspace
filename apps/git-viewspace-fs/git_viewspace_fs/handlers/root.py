#!/usr/bin/env python
#
# Copyright 2017-2021 Vitalis Salis and Diomidis Spinellis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import errno

from ..gitoper import GitOperError, NodeType


class RootHandler:
    def node_type(self):
        return NodeType.DIRECTORY

    def readdir(self):
        return ["views"]

    def get_commit(self):
        return None

    def file_contents(self):
        raise GitOperError(errno.EISDIR, "path is a directory")

    def symlink_target(self):
        raise GitOperError(errno.EINVAL, "path is not a symbolic link")
