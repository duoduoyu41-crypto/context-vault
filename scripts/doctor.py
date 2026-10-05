#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doctor —— ctx.py doctor 的等价入口（薄包装）。

保证 `python doctor.py ...` 与 `ctx.py doctor ...` 两种调用方式都可用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ctx  # noqa: E402

if __name__ == "__main__":
    sys.exit(ctx.main(["doctor"] + sys.argv[1:]))
