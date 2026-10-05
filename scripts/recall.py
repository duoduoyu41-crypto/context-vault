#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recall —— ctx.py recall 的等价入口（薄包装）。

保证 `python recall.py ...` 与 `ctx.py recall ...` 两种调用方式都可用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctx  # noqa: E402

if __name__ == "__main__":
    sys.exit(ctx.main(["recall"] + sys.argv[1:]))
