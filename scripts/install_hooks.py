#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install_hooks —— ctx.py install_hooks 的等价入口（薄包装）。

保证 `python install_hooks.py ...` 与 `ctx.py install_hooks ...` 两种调用方式都可用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctx  # noqa: E402

if __name__ == "__main__":
    sys.exit(ctx.main(["install-hooks"] + sys.argv[1:]))
