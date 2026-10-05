#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""summarize —— ctx.py summarize 的等价入口（薄包装）。

保证 `python summarize.py ...` 与 `ctx.py summarize ...` 两种调用方式都可用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctx  # noqa: E402

if __name__ == "__main__":
    sys.exit(ctx.main(["summarize"] + sys.argv[1:]))
