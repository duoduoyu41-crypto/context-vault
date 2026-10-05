#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""search —— ctx.py search 的等价入口（薄包装）。

保证 `python search.py ...` 与 `ctx.py search ...` 两种调用方式都可用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctx  # noqa: E402

if __name__ == "__main__":
    sys.exit(ctx.main(["search"] + sys.argv[1:]))
