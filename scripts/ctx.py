#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""context-vault CLI 总入口。

所有子命令都在这里实现；scripts/archive.py 等只是薄包装，
保证 `ctx.py archive ...` 与 `archive.py ...` 两种调用方式都可用。

退出码约定：
  0  成功
  1  非致命错误
  3  存储根未初始化
  （永不返回 2：对 PreCompact 意味着阻止压缩，对 UserPromptSubmit 意味着阻塞发言）
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _lib import compat, hooks_io, index, paths, search as searchlib, store, summarize, transcript  # noqa: E402

compat.ensure_utf8_stdio()

APP = "context-vault"


def _out(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def _root_or_die(cfg=None, auto_init: bool = True):
    cfg = cfg or paths.load_config()
    root = paths.resolve_root(cfg)
    if not os.path.exists(os.path.join(root, paths.MARKER_NAME)):
        if auto_init:
            paths.init_root(root, cfg)
        else:
            return None, cfg
    return root, cfg


# ---------------------------------------------------------------- 归档

def do_archive(transcript_path: str, cfg, root: str, force: bool = False,
               verbose: bool = False, session_id: str = "", cwd: str = "",
               throttle: bool = True) -> dict:
    """增量归档单个会话（含子 agent）。返回统计 dict。"""
    sources = transcript.discover_sources(transcript_path)
    if not sources:
        return {"ok": False, "error": "transcript not found", "transcript": transcript_path}

    main = sources[0]["path"]
    if not session_id:
        session_id = os.path.splitext(os.path.basename(main))[0]
    slug = os.path.basename(os.path.dirname(main))

    sd = store.ensure_dirs(paths.session_dir(root, slug, session_id))
    lock = compat.lock_file(os.path.join(sd, ".lock"), timeout_s=5.0)
    if lock is None:
        return {"ok": False, "session_id": session_id, "error": "locked", "new_bytes": 0}
    try:
        st = store.load_state(sd)
        st["session_id"] = session_id
        st.setdefault("cwd", cwd or "")

        # 节流：非 force 时，距上次 <30s 且新增 <4KB 则跳过
        if throttle and not force:
            last = int(st.get("last_run_at") or 0)
            pending_bytes = 0
            for src in sources:
                try:
                    pending_bytes += max(0, os.path.getsize(src["path"]) - int(
                        st.get("sources", {}).get(src["key"], {}).get("offset") or 0))
                except Exception:
                    pass
            if compat.now_ms() - last < 30_000 and pending_bytes < 4096:
                st["last_seen"] = compat.now_ms()
                store.save_state(sd, st)
                return {"ok": True, "session_id": session_id, "skipped": "throttle",
                        "new_bytes": 0, "new_chunks": 0, "root": root}

        max_bytes = int(cfg.get("max_bytes_per_run") or 4 * 1024 * 1024)
        if force:
            max_bytes = max(max_bytes, 64 * 1024 * 1024)

        total_new_bytes = 0
        total_new_chunks = 0
        new_title = ""
        bad_lines = 0

        for src in sources:
            key, path = src["key"], src["path"]
            try:
                size = os.path.getsize(path)
                mtime = int(os.path.getmtime(path) * 1000)
            except Exception:
                continue
            prev = st["sources"].get(key) or {}
            offset = int(prev.get("offset") or 0)

            # 源被重写/截断：旧分片保留，从 0 重建
            if size < offset or (prev.get("mtime") and mtime < int(prev["mtime"]) - 1000):
                store.mark_superseded(sd, key)
                offset = 0
                st["seq"] = 0

            budget = max(0, max_bytes - total_new_bytes)
            if budget <= 0:
                break
            data, new_offset, _ = transcript.read_new_bytes(path, offset, budget)
            if not data:
                st["sources"][key] = dict(prev, size=size, mtime=mtime,
                                          path=path, lines=int(prev.get("lines") or 0))
                continue

            if cfg.get("copy_raw", True):
                store.append_raw(sd, key, data)

            records = []
            for obj in transcript.iter_json_lines(data):
                try:
                    recs = transcript.extract_records(obj)
                except Exception:
                    bad_lines += 1
                    continue
                for r in recs:
                    if r.get("kind") == "title" and r.get("text"):
                        new_title = r["text"]
                    if r.get("kind") == "meta":
                        continue
                    records.append(r)

            chunks = store.records_to_chunks(records, cfg, session_id, key, int(st.get("seq") or 0))
            if chunks:
                st["seq"] = int(chunks[-1]["seq"])
            total_new_chunks += store.append_chunks(sd, chunks)
            total_new_bytes += len(data)

            st["sources"][key] = {
                "path": path,
                "size": size,
                "offset": new_offset,
                "mtime": mtime,
                "lines": int(prev.get("lines") or 0) + data.count(b"\n"),
                "started_at": prev.get("started_at") or compat.now_ms(),
            }

        st["last_run_at"] = compat.now_ms()
        st["last_chunks"] = total_new_chunks
        st["bad_lines"] = int(st.get("bad_lines") or 0) + bad_lines
        store.save_state(sd, st)

        meta = store.read_meta(sd)
        if new_title or not meta.get("title"):
            meta["title"] = new_title or meta.get("title") or ""
        meta.update(store.build_meta(session_id, slug, cwd or st.get("cwd") or "",
                                     st, sd=sd, title=meta.get("title", ""),
                                     chunks=store.count_chunks(sd)))
        store.write_meta(sd, meta)
        index.upsert(root, meta)

        return {
            "ok": True,
            "session_id": session_id,
            "project": slug,
            "root": root,
            "new_bytes": total_new_bytes,
            "new_chunks": total_new_chunks,
            "total_chunks": store.count_chunks(sd),
            "sources": sorted(st["sources"].keys()),
        }
    finally:
        compat.unlock_file(lock)


def find_latest_transcript(cwd: str = "") -> str:
    """按 cwd 定位最近使用的 transcript；找不到则退化为最近修改的那个。"""
    troot = paths.detect_transcript_root()
    if not troot:
        return ""
    slug = paths.encode_project_slug(cwd or os.getcwd())
    pdir = os.path.join(troot, slug)
    candidates = []
    if os.path.isdir(pdir):
        try:
            for name in os.listdir(pdir):
                if name.endswith(".jsonl"):
                    candidates.append(os.path.join(pdir, name))
        except Exception:
            pass
    if not candidates:
        for d in paths.transcript_root_candidates():
            if not os.path.isdir(d):
                continue
            for cur, _dirs, files in os.walk(d):
                for f in files:
                    if f.endswith(".jsonl"):
                        candidates.append(os.path.join(cur, f))
    if not candidates:
        return ""
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def cmd_archive(a) -> int:
    cfg = paths.load_config()
    root, cfg = _root_or_die(cfg)
    if root is None:
        _out({"ok": False, "error": "storage not initialized", "hint": "run: ctx.py init-storage"})
        return hooks_io.EXIT_UNINIT

    targets = []
    if a.all:
        troot = paths.detect_transcript_root() or ""
        if troot and os.path.isdir(troot):
            for cur, _d, files in os.walk(troot):
                for f in files:
                    if f.endswith(".jsonl") and "subagents" not in cur:
                        targets.append(os.path.join(cur, f))
    elif a.transcript:
        targets.append(os.path.abspath(os.path.expanduser(a.transcript)))
    else:
        p = find_latest_transcript(a.cwd or os.getcwd())
        if not p:
            _out({"ok": False, "error": "no transcript found",
                  "hint": "pass --transcript <path> 或 --cwd <工作区>"})
            return hooks_io.EXIT_ERR
        targets.append(p)

    results = []
    for t in targets:
        if "subagents" in t.replace("\\", "/"):
            continue
        try:
            results.append(do_archive(t, cfg, root, force=a.force,
                                      session_id=a.session or "", cwd=a.cwd,
                                      throttle=not a.force))
        except Exception as exc:  # 单个会话失败不影响其他
            results.append({"ok": False, "transcript": t, "error": "%s: %s" % (type(exc).__name__, exc)})

    if len(results) == 1:
        _out(results[0])
    else:
        _out({"ok": all(r.get("ok") for r in results), "count": len(results),
              "new_chunks": sum(int(r.get("new_chunks") or 0) for r in results),
              "root": root, "results": results})
    return hooks_io.EXIT_OK


# ---------------------------------------------------------------- 存储初始化

def cmd_init_storage(a) -> int:
    cfg = paths.load_config()
    if a.pick:
        target = os.path.abspath(os.path.expanduser(a.pick))
        cands = [{"root": target, "free_gb": round(compat.free_bytes(target) / 2 ** 30, 1),
                  "total_gb": round(compat.total_bytes(target) / 2 ** 30, 1),
                  "is_system": False, "has_marker": False, "score": 0.0, "fallback": False}]
    else:
        cands = paths.probe_candidates(cfg)

    if a.dry_run:
        print("候选存储位置（按得分降序）：\n")
        print("%-52s %10s %10s %6s %6s %8s" % ("位置", "可用", "总容量", "系统盘", "已有", "得分"))
        for c in cands:
            print("%-52s %10s %10s %6s %6s %8s" % (
                c["root"][:52], "%.1fG" % c["free_gb"], "%.1fG" % c["total_gb"],
                "是" if c["is_system"] else "否",
                "是" if c.get("has_marker") else "否", c["score"]))
        print("\n运行 `ctx.py init-storage` 将选用第一位；或用 --pick <路径> 指定。")
        return hooks_io.EXIT_OK

    chosen = cands[0]["root"]
    if not a.yes and not a.pick:
        print("将初始化存储根：%s" % chosen)
        print("（可用 %.1f GB；如需更换请用 --pick <路径>）" % cands[0]["free_gb"])
    marker = paths.init_root(chosen, cfg)
    _out({"ok": True, "root": chosen, "marker": marker, "config": paths.config_path()})
    return hooks_io.EXIT_OK


def cmd_migrate(a) -> int:
    cfg = paths.load_config()
    old = paths.resolve_root(cfg, auto_repair=False)
    new = os.path.abspath(os.path.expanduser(a.to))
    if os.path.abspath(old) == new:
        _out({"ok": False, "error": "源与目标相同", "root": old})
        return hooks_io.EXIT_ERR
    compat.mkdirs(new)
    import shutil

    for name in sorted(os.listdir(old)):
        s = os.path.join(old, name)
        d = os.path.join(new, name)
        if os.path.isdir(s):
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(s, d)
        elif os.path.isfile(s):
            shutil.copy2(s, d)
    paths.init_root(new, cfg)
    cfg["root"] = new
    paths.save_config(cfg)
    # 旧目录保留，绝不删除
    try:
        os.rename(old, old + ".old-%d" % compat.now_ms())
    except Exception:
        pass
    _out({"ok": True, "from": old, "to": new, "note": "旧目录已改名保留（未删除）"})
    return hooks_io.EXIT_OK


# ---------------------------------------------------------------- 检索 / 回灌

def cmd_search(a) -> int:
    cfg = paths.load_config()
    root, cfg = _root_or_die(cfg, auto_init=False)
    if root is None:
        print("尚未初始化存储根，先运行：ctx.py init-storage")
        return hooks_io.EXIT_UNINIT
    hits = searchlib.search(root, a.query, session_id=a.session or "", project=a.project or "",
                            cwd=os.getcwd(), kind=a.kind or "", role=a.role or "",
                            use_regex=a.regex, limit=a.limit, after_ts=a.after or 0,
                            context=a.context or 0)
    if a.json:
        _out({"ok": True, "count": len(hits), "root": root, "hits": hits})
    else:
        print(searchlib.format_hits(hits, a.query))
    return hooks_io.EXIT_OK


def cmd_recall(a) -> int:
    cfg = paths.load_config()
    root, cfg = _root_or_die(cfg, auto_init=False)
    if root is None:
        print("尚未初始化存储根，先运行：ctx.py init-storage")
        return hooks_io.EXIT_UNINIT
    hits = searchlib.search(root, a.query, session_id=a.session or "", project=a.project or "",
                            cwd=os.getcwd(), kind=a.kind or "", role=a.role or "",
                            use_regex=a.regex, limit=max(a.limit, 20))
    budget = int(a.budget or 6000)
    picked, used = [], 0
    for h in hits:
        t = store.est_tokens(h.get("text") or "")
        if used + t > budget:
            continue
        picked.append(h)
        used += t
    body = searchlib.format_hits(picked, a.query)
    if a.hook:
        hooks_io.emit(body if picked else "", code=hooks_io.EXIT_OK,
                      event_name=os.environ.get("CONTEXT_VAULT_HOOK_EVENT", ""))
    print(body)
    return hooks_io.EXIT_OK


# ---------------------------------------------------------------- 摘要

def cmd_summarize(a) -> int:
    cfg = paths.load_config()
    root, cfg = _root_or_die(cfg, auto_init=False)
    if root is None:
        print("尚未初始化存储根")
        return hooks_io.EXIT_UNINIT

    sd = None
    if a.session:
        sd = index.find_session(root, a.session)
    else:
        cands = index.sessions_for_project(root, a.project or "", a.cwd or os.getcwd())
        if cands:
            sd = max(cands, key=lambda d: os.path.getmtime(d) if os.path.exists(d) else 0)
        if not sd:
            all_dirs = index.iter_session_dirs(root)
            sd = max(all_dirs, key=lambda d: os.path.getmtime(d)) if all_dirs else None
    if not sd:
        print("未找到会话归档，先运行 archive")
        return hooks_io.EXIT_ERR

    if a.from_host:
        ok = summarize.apply_compact_summary(sd, a.from_host, cfg)
        if ok:
            summarize.clear_pending(sd)
        _out({"ok": ok, "session_id": os.path.basename(sd), "rolling": store.rolling_path(sd)})
        return hooks_io.EXIT_OK

    if a.mark_done:
        summarize.clear_pending(sd)
        _out({"ok": True, "session_id": os.path.basename(sd), "pending": False})
        return hooks_io.EXIT_OK

    if a.write_back:
        text = a.write_back
        store.write_rolling(sd, text)
        summarize.clear_pending(sd)
        _out({"ok": True, "session_id": os.path.basename(sd), "written": len(text)})
        return hooks_io.EXIT_OK

    text = summarize.build_skeleton(sd, cfg)
    store.write_rolling(sd, text)
    summarize.write_pending(sd, os.path.basename(sd))
    if a.print_only:
        print(text)
    else:
        _out({"ok": True, "session_id": os.path.basename(sd),
              "rolling": store.rolling_path(sd), "chars": len(text)})
    return hooks_io.EXIT_OK


# ---------------------------------------------------------------- 钩子安装

HOOK_EVENTS = [
    ("UserPromptSubmit", None, "prompt-submit", 10),
    ("PreCompact", "auto|manual", "pre-compact", 30),
    ("PostCompact", "auto|manual", "post-compact", 20),
    ("SessionStart", "startup|resume|clear|compact", "session-start", 20),
    ("SessionEnd", "clear|logout|prompt_input_exit|other", "session-end", 60),
    ("Stop", None, "stop", 60),
]


def _skill_dir() -> str:
    """技能安装目录的绝对路径。

    钩子命令必须写绝对路径：${CODEBUDDY_SKILL_DIR} 是 SKILL.md 的占位符，
    在 settings.json 中宿主未必展开，写成占位符会导致钩子静默失效。
    """
    here = os.path.dirname(os.path.abspath(__file__))          # <skill>/scripts
    return os.path.dirname(here)                                # <skill>


def _hook_command(sub: str) -> str:
    # 统一正斜杠：Windows 下钩子由 Git Bash 执行，反斜杠会被当作转义
    skill_dir = _skill_dir().replace("\\", "/")
    return '"%s/scripts/ctx.sh" hook %s' % (skill_dir, sub)


def build_hooks_json() -> dict:
    hooks = {}
    for event, matcher, sub, timeout in HOOK_EVENTS:
        entry = {"hooks": [{"type": "command", "command": _hook_command(sub), "timeout": timeout}]}
        if matcher:
            entry["matcher"] = matcher
        hooks.setdefault(event, []).append(entry)
    return {"hooks": hooks}


def _owned(entry: dict) -> bool:
    return any(APP in (h.get("command") or "") for h in entry.get("hooks", []))


def cmd_install_hooks(a) -> int:
    targets = []
    if a.home in ("workbuddy", "both"):
        targets.append(os.path.join(paths.home_dir(), ".workbuddy", "settings.json"))
    if a.home in ("codebuddy", "both"):
        targets.append(os.path.join(paths.home_dir(), ".codebuddy", "settings.json"))

    snippet = build_hooks_json()
    report = []
    for path in targets:
        if not os.path.exists(path):
            report.append({"file": path, "status": "skipped", "reason": "not found"})
            continue
        data = compat.read_json(path, None)
        if data is None:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    json.load(fh)
            except Exception as exc:
                report.append({"file": path, "status": "error",
                               "reason": "无法解析 JSON，拒绝写入：%s" % exc})
                continue
            data = {}
        if not isinstance(data, dict):
            report.append({"file": path, "status": "error", "reason": "顶层不是对象"})
            continue

        existing = data.get("hooks") or {}
        added, existed = 0, 0
        for event, groups in snippet["hooks"].items():
            cur = existing.get(event)
            if not isinstance(cur, list):
                cur = []
            for g in groups:
                if any(_owned(e) and e.get("matcher") == g.get("matcher") for e in cur
                       if isinstance(e, dict)):
                    existed += 1
                    continue
                cur.append(g)
                added += 1
            existing[event] = cur
        data["hooks"] = existing

        if a.dry_run:
            report.append({"file": path, "status": "dry-run", "added": added,
                           "already": existed, "json": snippet})
            continue
        if added == 0:
            report.append({"file": path, "status": "unchanged", "already": existed})
            continue

        backup = "%s.bak.context-vault.%s" % (path, compat.utc_iso().replace(" ", "-").replace(":", ""))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                compat.atomic_write_text(backup, fh.read())
        except Exception:
            pass
        compat.atomic_write_json(path, data)
        report.append({"file": path, "status": "written", "added": added,
                       "already": existed, "backup": backup})

    need_review = any(r.get("status") == "written" for r in report)
    _out({"ok": True, "dry_run": bool(a.dry_run), "results": report,
          "review_required": need_review,
          "note": ("settings.json 是受保护文件，请在 /hooks 菜单中审查并确认后才会生效"
                   if need_review else "")})
    return hooks_io.EXIT_OK


def cmd_uninstall_hooks(a) -> int:
    targets = [os.path.join(paths.home_dir(), d, "settings.json")
               for d in (".workbuddy", ".codebuddy")]
    report = []
    for path in targets:
        if not os.path.exists(path):
            continue
        data = compat.read_json(path, None)
        if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
            report.append({"file": path, "status": "no-hooks"})
            continue
        backup = "%s.bak.context-vault.%s" % (path, compat.utc_iso().replace(" ", "-").replace(":", ""))
        try:
            with open(path, "r", encoding="utf-8") as fh:
                compat.atomic_write_text(backup, fh.read())
        except Exception:
            pass
        removed = 0
        new_hooks = {}
        for event, groups in data["hooks"].items():
            if not isinstance(groups, list):
                continue
            kept = [g for g in groups if not (isinstance(g, dict) and _owned(g))]
            removed += len(groups) - len(kept)
            if kept:
                new_hooks[event] = kept
        data["hooks"] = new_hooks
        compat.atomic_write_json(path, data)
        report.append({"file": path, "status": "removed", "removed": removed, "backup": backup})
    _out({"ok": True, "results": report})
    return hooks_io.EXIT_OK


def cmd_hooks_status(a) -> int:
    report = []
    for path in [os.path.join(paths.home_dir(), d, "settings.json") for d in (".workbuddy", ".codebuddy")]:
        data = compat.read_json(path, None) if os.path.exists(path) else None
        if not isinstance(data, dict):
            report.append({"file": path, "installed": False, "exists": os.path.exists(path)})
            continue
        events = {}
        for event, groups in (data.get("hooks") or {}).items():
            n = sum(1 for g in groups if isinstance(g, dict) and _owned(g))
            if n:
                events[event] = n
        report.append({"file": path, "installed": bool(events), "events": events})
    _out({"ok": True, "results": report,
          "note": "配置写入后需在 /hooks 菜单审查确认才会生效"})
    return hooks_io.EXIT_OK


# ---------------------------------------------------------------- 钩子回调

def cmd_hook(a) -> int:
    """钩子回调：stdin 接收宿主 JSON。"""
    payload = hooks_io.read_hook_input()
    event = payload.get("hook_event_name") or a.event
    os.environ["CONTEXT_VAULT_HOOK_EVENT"] = str(event or "")
    transcript_path = payload.get("transcript_path") or ""
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()

    cfg = paths.load_config()
    root, cfg = _root_or_die(cfg)
    if root is None:
        return hooks_io.EXIT_ERR

    sub = a.event
    try:
        if sub in ("prompt-submit", "stop", "session-end", "pre-compact"):
            force = sub in ("pre-compact", "stop", "session-end")
            if transcript_path and os.path.exists(transcript_path):
                res = do_archive(transcript_path, cfg, root, force=force,
                                 session_id=session_id, cwd=cwd, throttle=not force)
            else:
                res = {"ok": False, "error": "no transcript_path"}

            if sub == "pre-compact":
                sd = index.find_session(root, session_id) if session_id else None
                if sd:
                    text = summarize.build_skeleton(sd, cfg)
                    store.write_rolling(sd, text)
                    summarize.write_pending(sd, session_id,
                                            trigger=str(payload.get("trigger") or "auto"))
                    hint = ("[context-vault] 已归档 %s 条片段到 %s。"
                            "压缩后如需细节，执行：ctx.py recall \"<关键词>\" --budget 4000"
                            % (res.get("total_chunks") or 0, root))
                    hooks_io.emit(hint, code=hooks_io.EXIT_OK, event_name=str(event or ""))
                return hooks_io.EXIT_OK

            if sub == "prompt-submit":
                sd = index.find_session(root, session_id) if session_id else None
                if sd and summarize.read_pending(sd).get("status") == "pending":
                    hooks_io.emit(
                        "[context-vault] 上一轮压缩后有待补写的摘要，"
                        "请执行 ctx.py summarize 补全 rolling.md 的『已确认事实』『关键决策』『待办』三节。",
                        code=hooks_io.EXIT_OK, event_name=str(event or ""))
                return hooks_io.EXIT_OK

            return hooks_io.EXIT_OK

        if sub == "post-compact":
            summary = payload.get("compact_summary") or ""
            sd = index.find_session(root, session_id) if session_id else None
            if sd and summary:
                summarize.apply_compact_summary(sd, summary, cfg)
                summarize.clear_pending(sd)
                return hooks_io.EXIT_OK
            if sd:
                summarize.write_pending(sd, session_id,
                                        trigger=str(payload.get("trigger") or "auto"))
            return hooks_io.EXIT_OK

        if sub == "session-start":
            sd = index.find_session(root, session_id) if session_id else None
            if not sd:
                dirs = index.sessions_for_project(root, "", cwd)
                sd = max(dirs, key=lambda d: os.path.getmtime(d)) if dirs else None
            if not sd:
                return hooks_io.EXIT_OK
            rolling = store.read_rolling(sd)
            if not rolling.strip():
                return hooks_io.EXIT_OK
            tail = rolling[-2500:]
            hint = ("[context-vault] 本会话已有归档摘要（%s）：\n%s\n"
                    "需要细节时执行：ctx.py recall \"<关键词>\" --budget 4000"
                    % (root, tail))
            hooks_io.emit(hint, code=hooks_io.EXIT_OK, event_name=str(event or ""))
            return hooks_io.EXIT_OK

        return hooks_io.EXIT_OK
    except Exception:
        # 钩子绝不因异常影响对话
        return hooks_io.EXIT_ERR


# ---------------------------------------------------------------- 自检

def cmd_doctor(a) -> int:
    cfg = paths.load_config()
    rows = []

    def add(name, ok, detail, fix="", warn_only=False):
        status = "OK" if ok else ("WARN" if warn_only else "FAIL")
        rows.append({"check": name, "status": status,
                     "detail": detail, "fix": ("" if ok else fix)})

    add("python", sys.version_info >= (3, 8), "Python %s" % ".".join(map(str, sys.version_info[:3])))
    add("transcript_root", bool(paths.detect_transcript_root()),
        paths.detect_transcript_root() or "未找到", "确认宿主 projects 目录存在")

    root = cfg.get("root")
    if root and os.path.exists(os.path.join(str(root), paths.MARKER_NAME)):
        add("storage_root", True, root)
        add("free_space", compat.free_bytes(root) > 512 * 1024 * 1024,
            "%.1f GB 可用" % (compat.free_bytes(root) / 2 ** 30), "清理归档或迁移到更大磁盘")
        if a.rebuild:
            index.rebuild(root)
        n = len({m.get("session_id") for m in index.list_sessions(root)})
        add("sessions", True, "%d 个会话（索引 %d 行，可用 --rebuild 去重）"
            % (n, len(index.list_sessions(root))))
    else:
        add("storage_root", False, root or "未设置", "运行 ctx.py init-storage")
        n = 0

    if root and os.path.isdir(os.path.join(str(root), "projects")):
        total_chunks = 0
        unknown = 0
        for sd in index.iter_session_dirs(str(root)):
            total_chunks += store.count_chunks(sd)
        for sd in index.iter_session_dirs(str(root)):
            for c in store.read_chunks(sd):
                if str(c.get("kind") or "").startswith("unknown:"):
                    unknown += 1
        add("chunks", True, "%d 条片段" % total_chunks)
        if total_chunks:
            ratio = unknown / float(total_chunks)
            add("schema_compat", ratio < 0.2,
                "未知类型占比 %.1f%%" % (ratio * 100),
                "transcript 格式可能已变更，考虑升级" if ratio >= 0.2 else "")
        st_dirs = index.iter_session_dirs(str(root))
        stale = sum(1 for d in st_dirs if os.path.exists(os.path.join(d, ".lock")))
        add("locks", stale == 0, "%d 个残留锁" % stale,
            "删除会话目录下的 .lock 文件" if stale else "")

    st = cmd_hooks_status.__name__
    hs = []
    for path in [os.path.join(paths.home_dir(), d, "settings.json") for d in (".workbuddy", ".codebuddy")]:
        data = compat.read_json(path, None) if os.path.exists(path) else None
        if isinstance(data, dict):
            ev = [e for e, gs in (data.get("hooks") or {}).items()
                  if any(_owned(g) for g in gs if isinstance(g, dict))]
            if ev:
                hs.append("%s: %s" % (os.path.basename(os.path.dirname(path)), ",".join(ev)))
    add("hooks", bool(hs), "; ".join(hs) if hs else "未安装（手动模式可用）",
        "运行 ctx.py install-hooks 开启自动归档", warn_only=True)

    failed = [r for r in rows if r["status"] == "FAIL"]
    print("context-vault 自检\n")
    print("%-16s %-6s %s" % ("检查项", "状态", "详情"))
    for r in rows:
        line = "%-16s %-6s %s" % (r["check"], r["status"], r["detail"])
        if r["fix"]:
            line += "  -> 修复：%s" % r["fix"]
        print(line)
    print("\n结论：%s" % ("全部通过" if not failed else "%d 项需要处理" % len(failed)))
    return hooks_io.EXIT_OK if not failed else hooks_io.EXIT_ERR


def cmd_index_rebuild(a) -> int:
    cfg = paths.load_config()
    root, cfg = _root_or_die(cfg, auto_init=False)
    if root is None:
        print("尚未初始化存储根")
        return hooks_io.EXIT_UNINIT
    n = index.rebuild(root)
    _out({"ok": True, "sessions": n, "index": paths.index_path(root)})
    return hooks_io.EXIT_OK


# ---------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctx.py", description="context-vault：长上下文本地归档与按需取回")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("archive", help="增量归档 transcript")
    s.add_argument("--transcript", help="transcript(.jsonl) 绝对路径")
    s.add_argument("--session", default="", help="会话 ID（默认从文件名推断）")
    s.add_argument("--cwd", default="", help="工作区路径")
    s.add_argument("--all", action="store_true", help="归档全部历史项目")
    s.add_argument("--force", action="store_true", help="跳过节流，全量补齐")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_archive)

    s = sub.add_parser("init-storage", help="初始化存储根")
    s.add_argument("--dry-run", action="store_true", help="只打印候选表")
    s.add_argument("--yes", action="store_true", help="自动选最高分")
    s.add_argument("--pick", help="指定存储根路径")
    s.set_defaults(func=cmd_init_storage)

    s = sub.add_parser("migrate", help="迁移存储根")
    s.add_argument("--to", required=True)
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("search", help="检索归档")
    s.add_argument("query")
    s.add_argument("--session", default="")
    s.add_argument("--project", default="")
    s.add_argument("--kind", default="")
    s.add_argument("--role", default="")
    s.add_argument("--regex", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--after", type=int, default=0)
    s.add_argument("--context", type=int, default=0)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("recall", help="回灌上下文片段")
    s.add_argument("query")
    s.add_argument("--session", default="")
    s.add_argument("--project", default="")
    s.add_argument("--kind", default="")
    s.add_argument("--role", default="")
    s.add_argument("--budget", type=int, default=6000, help="token 预算")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--regex", action="store_true")
    s.add_argument("--hook", action="store_true", help="输出 hookSpecificOutput JSON")
    s.set_defaults(func=cmd_recall)

    s = sub.add_parser("summarize", help="滚动摘要")
    s.add_argument("--session", default="")
    s.add_argument("--project", default="")
    s.add_argument("--cwd", default="")
    s.add_argument("--from-host", help="写入宿主提供的 compact_summary")
    s.add_argument("--write-back", help="模型补写的完整摘要文本")
    s.add_argument("--mark-done", action="store_true")
    s.add_argument("--print-only", action="store_true")
    s.set_defaults(func=cmd_summarize)

    s = sub.add_parser("install-hooks", help="安装钩子（按需开启自动化）")
    s.add_argument("--home", choices=("workbuddy", "codebuddy", "both"), default="both")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_install_hooks)

    s = sub.add_parser("uninstall-hooks", help="卸载钩子")
    s.set_defaults(func=cmd_uninstall_hooks)

    s = sub.add_parser("hooks-status", help="查看钩子安装状态")
    s.set_defaults(func=cmd_hooks_status)

    s = sub.add_parser("doctor", help="自检")
    s.add_argument("--rebuild", action="store_true", help="顺带重建索引")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("index-rebuild", help="重建全局索引")
    s.set_defaults(func=cmd_index_rebuild)

    s = sub.add_parser("hook", help="钩子回调（由宿主调用）")
    s.add_argument("event", choices=("prompt-submit", "pre-compact", "post-compact",
                                     "session-start", "session-end", "stop"))
    s.set_defaults(func=cmd_hook)

    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        parser.print_help()
        return hooks_io.EXIT_OK
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return hooks_io.EXIT_OK
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except Exception as exc:
        sys.stderr.write("[context-vault] %s: %s\n" % (type(exc).__name__, exc))
        if os.environ.get("CONTEXT_VAULT_DEBUG"):
            import traceback

            traceback.print_exc()
        return hooks_io.EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
