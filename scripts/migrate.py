#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate —— ctx.py migrate 的等价入口（薄包装）。

保证 `python migrate.py ...` 与 `ctx.py migrate ...` 两种调用方式都可用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctx  # noqa: E402

if __name__ == "__main__":
    sys.exit(ctx.main(["migrate"] + sys.argv[1:]))
