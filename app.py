# -*- coding: utf-8 -*-
"""AgentAtlas(星图) — 本地 AI 协作项目全景看板(单文件,零依赖)。

自动发现你机器上所有 Claude Code / Codex 协作过的项目文件夹,
按"对话"粒度无损续接或跨工具接力。

用法:  python app.py            → 自动打开浏览器 http://127.0.0.1:8765
       python app.py --scan     → 命令行纯文本输出项目清单

数据:  只读取两个工具的会话日志;平台状态存 ~/.agent-atlas/state.json
       不上传任何数据,只监听本机 127.0.0.1。
"""
import datetime
import html
import json
import os
import platform as _platform
import re
import subprocess
import sys
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

APP_NAME = "AgentAtlas · 星图"
PORT = 8765
CODEX_DIR = Path.home() / ".codex"
CLAUDE_DIR = Path.home() / ".claude"
STATE_FILE = Path.home() / ".agent-atlas" / "state.json"
IS_WIN = _platform.system() == "Windows"
IS_MAC = _platform.system() == "Darwin"

MARKERS_DIR = (".claude", ".codex", ".ai-context")
MARKERS_FILE = ("CLAUDE.md", "AGENTS.md")
SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build",
             "$RECYCLE.BIN", "System Volume Information"}
MAX_DEPTH = 6

CLAUDE_SKIP = ("<system-reminder>", "<command-name>", "<local-command", "<task-notification>",
               "Caveat: The messages below")
CODEX_SKIP = ("<environment_context>", "<user_instructions>", "<permissions", "<turn_aborted",
              "<ephemeral", "<turn_")


# ---------------- 路径归一化 ----------------

def normalize_path(p: str) -> str:
    if not p:
        return ""
    p = p.strip().strip('"')
    if p.startswith("\\\\?\\"):
        p = p[4:]
    if IS_WIN:
        p = p.replace("/", "\\")
        p = re.sub(r"\\+$", "", p)
        return p.lower()
    return re.sub(r"/+$", "", p)


# ---------------- 发现层:三源扫描 ----------------

def _codex_session_files():
    for sub in ("sessions", "archived_sessions"):
        root = CODEX_DIR / sub
        if root.is_dir():
            yield from root.rglob("rollout-*.jsonl")


def _codex_meta(jf: Path):
    try:
        with jf.open(encoding="utf-8", errors="replace") as fh:
            meta = json.loads(fh.readline(1024 * 1024))
    except (OSError, json.JSONDecodeError):
        return None
    if meta.get("type") != "session_meta":
        return None
    return meta.get("payload") or {}


def _claude_cwd(jf: Path) -> str:
    try:
        with jf.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 50:
                    break
                if '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd", "")
                        if cwd:
                            return cwd
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return ""


def _mtime_iso(p: Path) -> str:
    return datetime.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


def _marker_projects(roots):
    for root in roots:
        rootp = Path(root)
        if not rootp.is_dir():
            continue
        stack = [(rootp, 0)]
        while stack:
            d, depth = stack.pop()
            if d.name in SKIP_DIRS or d.name.startswith("$"):
                continue
            marker = next((m for m in MARKERS_DIR if (d / m).is_dir()), "") or \
                next((m for m in MARKERS_FILE if (d / m).is_file()), "")
            if marker:
                yield normalize_path(str(d)), str(d), _mtime_iso(d), marker
            if depth < MAX_DEPTH:
                try:
                    stack.extend((c, depth + 1) for c in d.iterdir()
                                 if c.is_dir() and not c.name.startswith("."))
                except OSError:
                    pass


def scan(scan_roots=None) -> dict:
    projects = {}

    def add(tool, canonical, display, ts):
        e = projects.setdefault(canonical, {
            "display": display, "tools": {}, "sessions": 0,
            "last_activity": "", "first_activity": ts, "markers": [],
        })
        e["tools"][tool] = e["tools"].get(tool, 0) + 1
        e["sessions"] += 1
        e["last_activity"] = max(e["last_activity"], ts)
        e["first_activity"] = min(e["first_activity"], ts)

    for jf in _codex_session_files():
        mp = _codex_meta(jf)
        if not mp:
            continue
        cwd = (mp.get("cwd") or "").replace("\\\\?\\", "")
        if cwd:
            add("codex", normalize_path(cwd), cwd, _mtime_iso(jf))

    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.is_dir():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jf in proj_dir.glob("*.jsonl"):  # 只取直属=主会话,子代理不计
                cwd = _claude_cwd(jf)
                if cwd:
                    add("claude", normalize_path(cwd), cwd, _mtime_iso(jf))

    for canonical, display, ts, marker in _marker_projects(scan_roots or []):
        e = projects.setdefault(canonical, {
            "display": display, "tools": {}, "sessions": 0,
            "last_activity": ts, "first_activity": ts, "markers": [],
        })
        if marker not in e.setdefault("markers", []):
            e["markers"].append(marker)

    for e in projects.values():
        p = Path(e["display"])
        e["exists"] = p.is_dir()
        handoff = p / ".ai-context" / "HANDOFF.md"
        if handoff.exists():
            hts = _mtime_iso(handoff)
            e["handoff_at"] = hts
            e["handoff_stale"] = e["last_activity"] > hts
        else:
            e["handoff_at"] = ""
            e["handoff_stale"] = False
    return projects


# ---------------- 对话层:枚举 / 无损接力 ----------------

def _clean_title(text: str) -> str:
    text = " ".join(text.split())
    return text[:70] + ("…" if len(text) > 70 else "")


def _claude_turns(jf: Path):
    with jf.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") not in ("user", "assistant") or obj.get("isSidechain") \
                    or obj.get("isMeta"):
                continue
            content = (obj.get("message") or {}).get("content")
            texts = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts += [b.get("text", "") for b in content
                          if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(t for t in texts if t).strip()
            if not text or any(text.startswith(s) for s in CLAUDE_SKIP):
                continue
            yield obj.get("type"), text


def _codex_turns(jf: Path):
    with jf.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "response_item":
                continue
            p = obj.get("payload") or {}
            if p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
                continue
            texts = [b.get("text", "") for b in (p.get("content") or [])
                     if isinstance(b, dict) and b.get("type") in ("input_text", "output_text")]
            text = "\n".join(t for t in texts if t).strip()
            if not text or any(text.startswith(s) for s in CODEX_SKIP):
                continue
            if p.get("role") == "assistant" and p.get("phase") not in (None, "final_answer"):
                continue
            yield p["role"], text


def list_sessions(canonical: str):
    out = []
    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.is_dir():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jf in proj_dir.glob("*.jsonl"):
                if normalize_path(_claude_cwd(jf)) != canonical:
                    continue
                title, turns = "", 0
                for role, text in _claude_turns(jf):
                    turns += 1
                    if not title and role == "user":
                        title = _clean_title(text)
                if turns:
                    out.append({"tool": "claude", "id": jf.stem, "path": str(jf),
                                "time": _mtime_iso(jf), "turns": turns,
                                "title": title or "(无用户发言)"})
    for jf in _codex_session_files():
        mp = _codex_meta(jf)
        if not mp or normalize_path((mp.get("cwd") or "")) != canonical:
            continue
        sid = mp.get("id") or jf.stem
        title, turns = "", 0
        for role, text in _codex_turns(jf):
            turns += 1
            if not title and role == "user":
                title = _clean_title(text)
        if turns:
            out.append({"tool": "codex", "id": sid, "path": str(jf),
                        "time": _mtime_iso(jf), "turns": turns,
                        "title": title or "(无用户发言)"})
    out.sort(key=lambda s: s["time"], reverse=True)
    return out


RELAY_PROMPT = """请先完整阅读 {rel} —— 这是一条 {tool} 对话的完整记录(未做任何摘要),\
我要你原地接续这条对话继续工作。文件较长时:先读末尾 1/3 掌握当前进度,再按需检索前文。\
读完后告诉我你理解的当前进度和下一步,等我确认后再动手。"""


def build_continue_file(session: dict, project_dir: str):
    """把整条对话原文写入 <项目>/.ai-context/CONTINUE-<id8>.md(不蒸馏,只新建不改旧)。"""
    jf = Path(session["path"])
    turns_fn = _claude_turns if session["tool"] == "claude" else _codex_turns
    role_label = {"user": "🧑 用户", "assistant": "🤖 " + session["tool"].capitalize()}
    ai_dir = Path(project_dir) / ".ai-context"
    ai_dir.mkdir(exist_ok=True)
    out = ai_dir / f"CONTINUE-{session['id'][:8]}.md"
    lines = [f"# 对话完整记录(接力用,未蒸馏) · {session['tool']}",
             f"> 原会话: {session['id']} · 时间: {session['time']} · {session['turns']} 条发言",
             f"> 源文件: {session['path']}",
             "> 本文件为对话原文,仅滤除工具调用报文等格式噪音,内容未做任何删减或摘要。",
             "", "---", ""]
    for role, text in turns_fn(jf):
        lines += [f"## {role_label.get(role, role)}", text, ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out), RELAY_PROMPT.format(rel=f".ai-context/{out.name}", tool=session["tool"])


# ---------------- 平台状态 ----------------

DEFAULT_SETTINGS = {"min_sessions": 1, "show_archived": False, "show_missing": False,
                    "scan_roots": [], "digest_model": "claude-haiku-4-5-20251001"}


def load_state() -> dict:
    data = {}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"settings": {**DEFAULT_SETTINGS, **data.get("settings", {})},
            "projects": data.get("projects", {})}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 记忆库:📚保全同步(无损档案) + 🧠蒸馏(逻辑体) ----------------

import hashlib
import shutil
import time

HUB_DIR = Path.home() / ".agent-atlas" / "memory"

DIGEST_SYSTEM_PROMPT = """你是一个记忆提炼器。输入是一份"用户与 AI 编程助手"的对话文字稿。
你的任务是站在"以后接手这个项目的 AI"的角度,提炼值得长期记住的内容。

只输出一个 JSON 对象,不要任何其他文字,结构:
{
  "memories": [
    {"type": "preference|decision|knowledge|lesson", "content": "一句话中文,自包含、脱离本次对话也能看懂"}
  ],
  "episode": {"title": "本次会话干了什么(15字内)", "summary": "2-3 句中文概括:任务、结果、遗留"}
}

提炼标准(宁缺毋滥,0 条也可以):
- preference:用户表达的工作偏好/习惯/分工要求(最好含用户原话)
- decision:定下来的、以后不应重新讨论的决定
- knowledge:项目的可复用事实(架构、关键文件、参数、结论)
- lesson:踩过的坑和正确做法
- 不要提炼:一次性的操作细节、寒暄、与项目无关的内容
- memories 最多 8 条;若对话无实质内容,memories 给空数组"""

TYPE_LABELS = {"preference": "用户偏好", "decision": "关键决策",
               "knowledge": "可复用知识", "lesson": "教训"}


def slug_for(canonical: str, display: str) -> str:
    base = re.sub(r"[^\w一-鿿.-]", "_", (Path(display).name or "project"))[:40] or "project"
    return f"{base}-{hashlib.sha1(canonical.encode('utf-8')).hexdigest()[:6]}"


def sync_archive(projects: dict) -> str:
    """📚 保全同步:把全部会话导出为完整文字稿(增量,无 LLM,零损耗)。返回结果描述。"""
    n_new = 0
    for canonical, v in projects.items():
        slug = slug_for(canonical, v["display"])
        tdir = HUB_DIR / "projects" / slug / "transcripts"
        state_file = tdir / ".state.json"
        try:
            tstate = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
        except (OSError, json.JSONDecodeError):
            tstate = {}
        for s in list_sessions(canonical):
            jf = Path(s["path"])
            size = jf.stat().st_size
            if tstate.get(s["id"], {}).get("size") == size:
                continue  # 未变化
            tdir.mkdir(parents=True, exist_ok=True)
            turns_fn = _claude_turns if s["tool"] == "claude" else _codex_turns
            role_label = {"user": "🧑 用户", "assistant": "🤖 " + s["tool"]}
            lines = [f"# 对话文字稿 · {s['tool']}",
                     f"> 会话: {s['id']} · 源: {s['path']}",
                     "> 完整原文,仅滤除工具调用报文,内容未删减。", ""]
            for role, text in turns_fn(jf):
                lines += [f"## {role_label.get(role, role)}", text, ""]
            out = tdir / f"{s['time'][:10]}-{s['tool']}-{s['id'][:8]}.md"
            out.write_text("\n".join(lines), encoding="utf-8")
            tstate[s["id"]] = {"size": size, "turns": s["turns"], "file": out.name}
            n_new += 1
        if tstate:
            tdir.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(tstate, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    return f"保全同步完成:导出/更新 {n_new} 份文字稿 → {HUB_DIR}"


def _claude_cli() -> str:
    return shutil.which("claude.cmd") or shutil.which("claude") or ""


def _llm_json(prompt_text: str, model: str) -> dict:
    cmd = _claude_cli()
    if not cmd:
        raise RuntimeError("找不到 claude CLI(蒸馏依赖已安装的 Claude Code)")
    for attempt in range(3):
        try:
            r = subprocess.run([cmd, "--model", model, "-p",
                                DIGEST_SYSTEM_PROMPT + "\n\n---\n\n" + prompt_text],
                               capture_output=True, encoding="utf-8", errors="replace",
                               timeout=300)
            text = (r.stdout or "").strip()
            if r.returncode == 0 and text:
                m = re.search(r"\{.*\}", text, flags=re.S)
                if m:
                    return json.loads(m.group(0))
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("LLM 调用失败(重试 3 次)")


def _write_memory_view(slug: str, display: str):
    """由 memories.jsonl 重建该项目的 MEMORY.md,并供给到项目 .ai-context(若存在)。"""
    pdir = HUB_DIR / "projects" / slug
    mem_file = pdir / "memories.jsonl"
    if not mem_file.exists():
        return
    by_type, episodes = {}, []
    for line in mem_file.read_text(encoding="utf-8").splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("type") == "episode":
            episodes.append(m)
        else:
            by_type.setdefault(m.get("type", ""), []).append(m)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 项目记忆 · {display}", f"> 由 AgentAtlas 蒸馏生成于 {now};原始档案在 transcripts/", ""]
    for t in ("preference", "decision", "knowledge", "lesson"):
        if by_type.get(t):
            lines.append(f"## {TYPE_LABELS[t]}")
            lines += [f"- {m['content']}  ⟨{m.get('date', '')}⟩" for m in by_type[t]]
            lines.append("")
    if episodes:
        lines.append("## 情景记忆")
        lines += [f"- {m.get('date', '')} · {m.get('title', '')}:{m['content']}" for m in episodes]
    (pdir / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
    # 供给:项目已有 .ai-context 才写入(opt-in,不主动创建)
    proj = Path(display)
    if (proj / ".ai-context").is_dir():
        (proj / ".ai-context" / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")


def digest_all(projects: dict, model: str) -> str:
    """🧠 蒸馏:用 LLM 把未消化的文字稿提炼为分类记忆。不修改原始档案。"""
    dstate_file = HUB_DIR / "digest_state.json"
    try:
        dstate = json.loads(dstate_file.read_text(encoding="utf-8")) if dstate_file.exists() else {}
    except (OSError, json.JSONDecodeError):
        dstate = {}
    display_of = {slug_for(c, v["display"]): v["display"] for c, v in projects.items()}
    n_ok = n_fail = n_mem = 0
    proj_root = HUB_DIR / "projects"
    if not proj_root.is_dir():
        return "还没有文字稿档案,请先运行 📚保全同步"
    for pdir in sorted(proj_root.iterdir()):
        tdir = pdir / "transcripts"
        if not tdir.is_dir():
            continue
        for f in sorted(tdir.glob("*.md")):
            key = str(f)
            if key in dstate:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            turns = len(re.findall(r"^## ", text, flags=re.M))
            if turns < 3:
                dstate[key] = {"skipped": "太短"}
                continue
            if len(text) > 24000:
                text = text[:16000] + "\n\n…(中段省略)…\n\n" + text[-8000:]
            try:
                out = _llm_json(text, model)
            except RuntimeError:
                n_fail += 1
                continue
            mem_file = pdir / "memories.jsonl"
            date = f.name[:10]
            with mem_file.open("a", encoding="utf-8") as fh:
                for m in (out.get("memories") or [])[:8]:
                    if m.get("type") in TYPE_LABELS and len(m.get("content", "")) >= 8:
                        fh.write(json.dumps({"type": m["type"], "content": m["content"],
                                             "date": date, "src": f.name},
                                            ensure_ascii=False) + "\n")
                        n_mem += 1
                ep = out.get("episode") or {}
                if ep.get("summary"):
                    fh.write(json.dumps({"type": "episode", "title": ep.get("title", ""),
                                         "content": ep["summary"], "date": date,
                                         "src": f.name}, ensure_ascii=False) + "\n")
            dstate[key] = {"memories": n_mem, "model": model}
            n_ok += 1
            _write_memory_view(pdir.name, display_of.get(pdir.name, pdir.name))
            HUB_DIR.mkdir(parents=True, exist_ok=True)
            dstate_file.write_text(json.dumps(dstate, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    HUB_DIR.mkdir(parents=True, exist_ok=True)
    dstate_file.write_text(json.dumps(dstate, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"蒸馏完成:消化 {n_ok} 份文字稿,提炼 {n_mem} 条记忆,失败 {n_fail}"


# ---------------- 打开文件夹 / 启动终端(跨平台) ----------------

def open_folder(path: str):
    if IS_WIN:
        os.startfile(path)  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def launch_terminal(workdir: str, command: str, title: str = "AI"):
    if IS_WIN:
        subprocess.Popen(f'start "{title}" cmd /k "cd /d "{workdir}" && {command}"', shell=True)
    elif IS_MAC:
        script = f'tell app "Terminal" to do script "cd {json.dumps(workdir)} && {command}"'
        subprocess.Popen(["osascript", "-e", script])
    else:
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            try:
                subprocess.Popen([term, "-e", f'bash -c "cd {workdir} && {command}; exec bash"'])
                return
            except FileNotFoundError:
                continue


# ---------------- 页面 ----------------

CSS = """
body{font-family:'Microsoft YaHei',system-ui,sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#1f2937;color:#fff;padding:14px 28px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
header h1{font-size:18px;margin:0}
header h1 a{color:#fff;text-decoration:none}
header .sub{color:#9ca3af;font-size:12px}
main{padding:20px 28px;max-width:1240px;margin:0 auto}
.bar{display:flex;gap:14px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.bar form{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;
      box-shadow:0 1px 3px rgba(0,0,0,.08)}
th{background:#f3f4f6;text-align:left;padding:9px 12px;font-size:12px;color:#555;white-space:nowrap}
td{padding:9px 12px;border-top:1px solid #f0f1f3;font-size:13px;vertical-align:middle}
tr:hover td{background:#fafbfc}
.tool{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;margin-right:4px}
.tool.claude{background:#ede9fe;color:#6d28d9}
.tool.codex{background:#dbeafe;color:#1d4ed8}
.tool.marker{background:#f3f4f6;color:#6b7280}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;background:#ecfdf5;
     color:#047857;margin-right:4px}
.ho-ok{color:#059669}.ho-stale{color:#d97706}.ho-none{color:#9ca3af}
.name{font-weight:600}
.name a{color:#1f2937;text-decoration:none}
.name a:hover{color:#2563eb;text-decoration:underline}
.path{color:#888;font-size:11px}
.missing{opacity:.5}
button,select,input[type=number],input[type=text]{font-size:12px;padding:3px 10px;border:1px solid #d1d5db;
  border-radius:6px;background:#fff;cursor:pointer}
button:hover{background:#f3f4f6}
button.primary{background:#2563eb;color:#fff;border-color:#2563eb}
button.toggle-on{background:#059669;color:#fff;border-color:#059669}
.actions{white-space:nowrap}
.actions form{display:inline}
.muted{color:#999;font-size:12px}
.msg{background:#ecfdf5;border:1px solid #a7f3d0;color:#047857;padding:8px 14px;border-radius:8px;
     margin-bottom:12px;font-size:13px}
.title-cell{max-width:520px}
"""

_cache = {"projects": None}


def get_projects(refresh=False):
    if refresh or _cache["projects"] is None:
        _cache["projects"] = scan(scan_roots=load_state()["settings"].get("scan_roots", []))
    return _cache["projects"]


def _page(body: str, subtitle: str = "") -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{APP_NAME}</title><style>{CSS}</style></head><body>
<header><h1><a href="/">{APP_NAME}</a></h1><span class="sub">{subtitle}</span></header>
<main>{body}</main></body></html>"""


def render_index(msg=""):
    st = load_state()
    settings = st["settings"]
    projects = get_projects()
    min_sessions = settings["min_sessions"]
    view_archived = settings.get("show_archived", False)

    rows = []
    n_hidden = n_archived = 0
    items = sorted(projects.items(), key=lambda kv: kv[1]["last_activity"], reverse=True)
    all_tags = sorted({t for ps in st["projects"].values() for t in ps.get("tags", [])})

    for canonical, v in items:
        ps = st["projects"].get(canonical, {})
        archived = ps.get("archived", False)
        if archived:
            n_archived += 1
        if view_archived != archived:
            continue
        if v["sessions"] < min_sessions and not v.get("markers"):
            n_hidden += 1
            continue
        if not v["exists"] and not settings["show_missing"]:
            n_hidden += 1
            continue

        name = html.escape(Path(v["display"]).name or v["display"])
        path = html.escape(v["display"])
        q = urllib.parse.quote(canonical)
        tools = "".join(
            f'<span class="tool {t}">{t}×{n}</span>' for t, n in sorted(v["tools"].items()))
        if v.get("markers"):
            tools += "".join(f'<span class="tool marker">{html.escape(m)}</span>'
                             for m in v["markers"])
        tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in ps.get("tags", []))
        if v["handoff_at"]:
            ho = ('<span class="ho-stale">⚠ 过期</span>' if v["handoff_stale"]
                  else '<span class="ho-ok">✓ 最新</span>')
        else:
            ho = '<span class="ho-none">—</span>'
        cls = "" if v["exists"] else ' class="missing"'
        rows.append(f"""<tr{cls}>
<td><div class="name"><a href="/project?p={q}" title="查看该项目的全部对话">{name}</a>
 {'' if v['exists'] else '(已删)'}{' 📦' if archived else ''}</div>
    <div class="path">{path}</div></td>
<td>{tools}</td>
<td style="text-align:center">{v['sessions']}</td>
<td>{v['last_activity'][:16].replace('T', ' ')}</td>
<td>{ho}</td>
<td>{tags}
  <form method="post" action="/tag" style="display:inline">
    <input type="hidden" name="p" value="{q}">
    <input type="text" name="tags" value="{html.escape(','.join(ps.get('tags', [])))}" size="8"
           title="逗号分隔,回车保存">
  </form></td>
<td class="actions">
  <form method="post" action="/open"><input type="hidden" name="p" value="{q}">
    <button {'disabled' if not v['exists'] else ''}>📂</button></form>
  <form method="post" action="/launch"><input type="hidden" name="p" value="{q}">
    <input type="hidden" name="tool" value="claude">
    <button title="在此项目新开 Claude Code(全新对话)" {'disabled' if not v['exists'] else ''}>Claude</button></form>
  <form method="post" action="/launch"><input type="hidden" name="p" value="{q}">
    <input type="hidden" name="tool" value="codex">
    <button title="在此项目新开 Codex(全新对话)" {'disabled' if not v['exists'] else ''}>Codex</button></form>
  <form method="post" action="/archive"><input type="hidden" name="p" value="{q}">
    <button title="归档/找回(只影响显示,不动文件)">{'♻ 找回' if archived else '归档'}</button></form>
</td></tr>""")

    both = sum(1 for v in projects.values() if len(v["tools"]) > 1)
    msg_html = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    tag_hint = f"已有标签: {', '.join(all_tags)}" if all_tags else ""
    roots_str = "; ".join(settings.get("scan_roots", []))
    view_btn = (f'<form method="post" action="/toggle_archived_view">'
                f'<button class="{"toggle-on" if view_archived else ""}">'
                f'{"📦 正在看归档(" + str(n_archived) + ") — 点击返回" if view_archived else "📦 查看归档(" + str(n_archived) + ")"}'
                f'</button></form>')

    body = f"""{msg_html}
<div class="bar">
  <form method="post" action="/settings">
    会话门槛 ≥ <input type="number" name="min_sessions" value="{min_sessions}" min="1" max="99"
      style="width:52px">
    扫描根目录 <input type="text" name="scan_roots" value="{html.escape(roots_str)}" size="36"
      title="分号分隔多个目录;在这些目录下查找带 .claude/.codex/.ai-context/CLAUDE.md/AGENTS.md 标记的文件夹(补上日志已被清理的老项目)">
    <label><input type="checkbox" name="show_missing" {'checked' if settings['show_missing'] else ''}>
      显示已删除文件夹</label>
    蒸馏模型 <select name="digest_model" title="蒸馏调用本机 claude CLI 无头模式,额度计入你的订阅">
      {''.join(f'<option value="{m}"{" selected" if m == settings.get("digest_model") else ""}>{label}</option>'
               for m, label in [("claude-haiku-4-5-20251001", "Haiku(快/省,默认)"),
                                ("claude-sonnet-5", "Sonnet(均衡)"),
                                ("claude-opus-4-8", "Opus(最强/贵)")])}
    </select>
    <button class="primary">应用</button>
  </form>
  {view_btn}
  <form method="post" action="/refresh"><button>🔄 重新扫描</button></form>
  <form method="post" action="/sync_archive">
    <button title="把全部对话导出为完整文字稿档案(增量)。无 LLM,零损耗 → ~/.agent-atlas/memory">📚 保全同步</button></form>
  <form method="post" action="/digest">
    <button title="用 LLM 把文字稿提炼为偏好/决策/教训,生成各项目 MEMORY.md(构建理解你的逻辑体)。有损提炼,不影响原始档案">🧠 蒸馏</button></form>
  <span class="muted">{tag_hint}</span>
</div>
<table>
<tr><th>项目(点击查看对话)</th><th>参与 AI / 标记</th><th>会话</th><th>最后活动</th><th>交接</th><th>标签</th><th>操作</th></tr>
{''.join(rows)}
</table>
<p class="muted">发现来源:Claude Code / Codex 会话日志(含 Codex 归档会话)+ 扫描根目录下的磁盘标记。
📚 保全同步 = 完整档案(图书馆,零损耗);🧠 蒸馏 = 提炼理解(逻辑体,有损)。两者独立,蒸馏永不修改档案。
归档与标签仅存于 ~/.agent-atlas/state.json,绝不移动或修改你的文件。</p>"""
    return _page(body, f"发现 {len(projects)} 个 AI 协作文件夹 · 双 AI {both} 个 ·"
                       f" 当前视图隐藏 {n_hidden} 个 · 归档 {n_archived} 个")


def render_project(canonical: str, msg=""):
    projects = get_projects()
    v = projects.get(canonical)
    if not v:
        return _page("<p>未找到该项目。<a href='/'>返回</a></p>")
    display = v["display"]
    sess = list_sessions(canonical)
    q = urllib.parse.quote(canonical)
    msg_html = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""

    rows = []
    for s in sess:
        sid_q = urllib.parse.quote(s["id"])
        other = "claude" if s["tool"] == "codex" else "codex"
        size_kb = Path(s["path"]).stat().st_size // 1024 if Path(s["path"]).exists() else 0
        rows.append(f"""<tr>
<td>{s['time'][:16].replace('T', ' ')}</td>
<td><span class="tool {s['tool']}">{s['tool']}</span></td>
<td style="text-align:center">{s['turns']}</td>
<td style="text-align:center">{size_kb} KB</td>
<td class="title-cell">{html.escape(s['title'])}</td>
<td class="actions">
  <form method="post" action="/resume">
    <input type="hidden" name="p" value="{q}"><input type="hidden" name="sid" value="{sid_q}">
    <input type="hidden" name="tool" value="{s['tool']}">
    <button title="原生 resume,恢复完整上下文,无损">▶ 用 {s['tool']} 续接</button></form>
  <form method="post" action="/relay">
    <input type="hidden" name="p" value="{q}"><input type="hidden" name="sid" value="{sid_q}">
    <button title="生成全文 CONTINUE 文件(不蒸馏,对话原文一字不动),启动 {other} 先读全文再接续">⇄ 接力给 {other}</button></form>
</td></tr>""")

    body = f"""{msg_html}
<p><a href="/">← 返回项目列表</a></p>
<h2 style="margin:6px 0">{html.escape(Path(display).name)}</h2>
<p class="path">{html.escape(display)}</p>
<table>
<tr><th>时间</th><th>工具</th><th>发言</th><th>大小</th><th>对话(第一句)</th><th>续接 / 接力</th></tr>
{''.join(rows) if rows else '<tr><td colspan="6">该项目没有找到会话记录(可能日志已被清理,只剩磁盘标记)。</td></tr>'}
</table>
<p class="muted">▶ 续接 = 同工具原生恢复(claude --resume / codex resume),上下文完整无损。
⇄ 接力 = 跨工具:把该对话<b>完整原文</b>写入项目 .ai-context/CONTINUE-*.md(只滤工具调用报文,
不做任何摘要),然后启动对方 AI 并要求它先读全文再接续;超长对话它会先读末尾再按需检索前文。</p>"""
    return _page(body, f"{len(sess)} 条对话")


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, content, status=200):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, msg="", to="/"):
        loc = to + (("&" if "?" in to else "?") + "msg=" + urllib.parse.quote(msg) if msg else "")
        self.send_response(303)
        self.send_header("Location", loc)
        self.end_headers()

    def _form(self):
        length = int(self.headers.get("Content-Length", 0))
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8")).items()}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        msg = q.get("msg", [""])[0]
        if parsed.path == "/":
            self._send(render_index(msg=msg))
        elif parsed.path == "/project":
            self._send(render_project(normalize_path(q.get("p", [""])[0]), msg=msg))
        else:
            self._send("not found", 404)

    def do_POST(self):
        form = self._form()
        path = urllib.parse.urlparse(self.path).path
        canonical = normalize_path(urllib.parse.unquote(form.get("p", "")))
        projects = get_projects()
        display = projects.get(canonical, {}).get("display", canonical)
        proj_url = f"/project?p={urllib.parse.quote(canonical)}"

        if path == "/settings":
            st = load_state()
            st["settings"]["min_sessions"] = max(1, int(form.get("min_sessions", 1) or 1))
            st["settings"]["scan_roots"] = [r.strip() for r in form.get("scan_roots", "").split(";")
                                            if r.strip()]
            st["settings"]["show_missing"] = "show_missing" in form
            st["settings"]["digest_model"] = form.get("digest_model",
                                                      st["settings"].get("digest_model", ""))
            save_state(st)
            get_projects(refresh=True)
            self._redirect("设置已保存并重新扫描")
        elif path == "/toggle_archived_view":
            st = load_state()
            st["settings"]["show_archived"] = not st["settings"].get("show_archived", False)
            save_state(st)
            self._redirect()
        elif path == "/refresh":
            get_projects(refresh=True)
            self._redirect("已重新扫描")
        elif path == "/tag":
            st = load_state()
            ps = st["projects"].setdefault(canonical, {"tags": [], "archived": False})
            ps["tags"] = [t.strip() for t in form.get("tags", "").split(",") if t.strip()]
            save_state(st)
            self._redirect("标签已保存")
        elif path == "/archive":
            st = load_state()
            ps = st["projects"].setdefault(canonical, {"tags": [], "archived": False})
            ps["archived"] = not ps.get("archived", False)
            save_state(st)
            self._redirect(("已归档: " if ps["archived"] else "已找回: ")
                           + (Path(display).name or display))
        elif path == "/open":
            open_folder(display)
            self._redirect()
        elif path == "/launch":
            tool = form.get("tool", "claude")
            launch_terminal(display, tool, tool)
            self._redirect(f"已在 {Path(display).name} 启动 {tool}(新对话)")
        elif path == "/resume":
            tool = form.get("tool", "claude")
            sid = urllib.parse.unquote(form.get("sid", ""))
            cmd = f'claude --resume {sid}' if tool == "claude" else f'codex resume {sid}'
            launch_terminal(display, cmd, f"resume-{tool}")
            self._redirect(f"已用 {tool} 无损续接对话 {sid[:8]}", to=proj_url)
        elif path == "/relay":
            sid = urllib.parse.unquote(form.get("sid", ""))
            target = next((s for s in list_sessions(canonical) if s["id"] == sid), None)
            if not target:
                self._redirect("未找到该对话", to=proj_url)
                return
            other = "claude" if target["tool"] == "codex" else "codex"
            fpath, prompt = build_continue_file(target, display)
            launch_terminal(display, f'{other} "{prompt.replace(chr(34), chr(39))}"',
                            f"relay-{other}")
            self._redirect(f"已生成全文接力文件 {Path(fpath).name}(未蒸馏)并启动 {other}",
                           to=proj_url)
        elif path == "/sync_archive":
            snapshot = dict(get_projects())
            import threading
            threading.Thread(target=lambda: print(sync_archive(snapshot)), daemon=True).start()
            self._redirect(f"📚 保全同步已在后台运行(增量导出完整文字稿 → {HUB_DIR})")
        elif path == "/digest":
            snapshot = dict(get_projects())
            model = load_state()["settings"].get("digest_model", "claude-haiku-4-5-20251001")
            import threading
            threading.Thread(target=lambda: print(digest_all(snapshot, model)),
                             daemon=True).start()
            self._redirect(f"🧠 蒸馏已在后台运行(模型: {model};逐条消化可能较久,不影响原始档案。"
                           "需先做过保全同步)")
        else:
            self._send("not found", 404)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    if "--scan" in sys.argv:
        data = scan(scan_roots=load_state()["settings"].get("scan_roots", []))
        both = [k for k, v in data.items() if len(v["tools"]) > 1]
        print(f"AI 参与过的文件夹: {len(data)} 个,其中双 AI 协作: {len(both)} 个\n")
        for k in sorted(data, key=lambda k: data[k]["last_activity"], reverse=True):
            v = data[k]
            tools = "+".join(f"{t}×{n}" for t, n in sorted(v["tools"].items())) or "仅标记"
            print(f"{v['last_activity'][:10]}  {v['sessions']:>3}会话  {tools:<18} {v['display']}")
        return

    port = PORT
    server = None
    for _ in range(10):
        try:
            server = HTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if server is None:
        print("无法找到可用端口")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}"
    print(f"{APP_NAME} 已启动: {url}")
    print("按 Ctrl+C 停止")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
