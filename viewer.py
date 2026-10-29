#!/usr/bin/env python3
"""Claude Export Viewer: read, search and carry over your claude.ai data export.

    python3 viewer.py                      # find the export in ~/Downloads and open it in the browser
    python3 viewer.py "path/to/export"     # open a specific export folder
    python3 viewer.py "path" --rebuild     # rebuild the search index
    python3 viewer.py "path" --dump        # write memory, projects, routines and Claude's files out as .md

Standard library only, Python 3.9+. Nothing leaves your machine: the server listens on 127.0.0.1.
The export zips don't need to be unpacked, they are read directly.
"""
import argparse
import fnmatch
import html
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

DB_NAME = "claude-export-viewer.db"
SCHEMA_VERSION = 4
WEB_TOOLS = ("web_search", "web_fetch", "websearch", "webfetch", "firecrawl", "exa", "extended_search", "web-browser")


def norm(s):
    # Russian «ё» and «е» are interchangeable in practice: index and search both without «ё»
    return (s or "").replace("ё", "е").replace("Ё", "Е")


# ---------- reading the export: unpacked files and zips side by side ----------

class Export:
    def __init__(self, root):
        self.root = root
        self.files = {}  # relative path -> ("fs", abs) | ("zip", zip, member)
        self._zips = {}
        for z in sorted(fnmatch.filter(os.listdir(root), "*.zip")):
            zp = os.path.join(root, z)
            try:
                names = zipfile.ZipFile(zp).namelist()
            except zipfile.BadZipFile:
                continue
            for n in names:
                if n.endswith("/"):
                    continue
                key = n if n not in self.files else f"{n}#{z}"  # parts -000, -001 share member names
                self.files[key] = ("zip", zp, n)
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not d.startswith(".") and d != "claude-export-md"]
            for f in fn:
                if f.startswith(".") or f.endswith(".zip") or f.startswith(DB_NAME):
                    continue
                ab = os.path.join(dp, f)
                self.files[os.path.relpath(ab, root).replace(os.sep, "/")] = ("fs", ab)

    def find(self, pattern):
        return sorted(k for k in self.files if fnmatch.fnmatch(k.split("#")[0], pattern))

    def read(self, key):
        src = self.files[key]
        if src[0] == "fs":
            with open(src[1], "rb") as f:
                return f.read()
        zf = self._zips.get(src[1]) or self._zips.setdefault(src[1], zipfile.ZipFile(src[1]))
        return zf.read(src[2])

    def json(self, key):
        return json.loads(self.read(key))


def looks_like_export(path):
    try:
        names = os.listdir(path)
    except OSError:
        return False
    return any(fnmatch.fnmatch(n, p) for n in names for p in
               ("conversations*.zip", "conversations*.json", "manifest-*.json"))


def find_exports():
    """Export folders in ~/Downloads (and Downloads itself), newest first."""
    downloads = os.path.expanduser("~/Downloads")
    found = []
    if looks_like_export(downloads):
        found.append(downloads)
    try:
        for n in os.listdir(downloads):
            p = os.path.join(downloads, n)
            if os.path.isdir(p) and looks_like_export(p):
                found.append(p)
    except OSError:
        pass
    return sorted(found, key=os.path.getmtime, reverse=True)


def pick_folder_mac():
    if sys.platform != "darwin":
        return None
    script = 'POSIX path of (choose folder with prompt "Choose the folder with your Claude export")'
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    except Exception:
        return None
    return out.stdout.strip() or None


# ---------- building the index ----------

SCHEMA = """
create table meta(k text primary key, v text);
create table convs(id text primary key, title text, created text, updated text, n int, chars int, summary text,
                   web int, nfiles int, att int);
create table msgs(conv text, idx int, sender text, created text, blocks text, primary key(conv, idx));
create table files(id integer primary key, conv text, path text, name text, content text,
                   delivered int, created text, edits int, failed_edits int);
create table notes(id integer primary key, kind text, title text, body text, parent int, created text);
create table arts(id text primary key, title text, descr text, created text, versions text, base text);
create virtual table fts using fts5(body, kind unindexed, ref unindexed, title unindexed, created unindexed,
                                    tokenize='trigram remove_diacritics 1');
"""


def _short(v, n):
    v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return v if len(v) <= n else v[:n] + f"… [+{len(v) - n}]"


def tool_label(name, inp):
    inp = inp or {}
    for k in ("query", "url", "command", "path", "description", "title"):
        if inp.get(k):
            return _short(str(inp[k]), 300)
    return _short(inp, 200) if inp else ""


def result_text(b):
    c = b.get("content")
    if isinstance(c, list):
        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return _short(c or "", 3000)


def apply_edit(f, old, new):
    if old and old in f["content"]:
        f["content"] = f["content"].replace(old, new or "", 1)
        f["edits"] += 1
    else:
        f["failed"] += 1


def build(exp, db_path):
    t0 = time.time()
    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
    except sqlite3.OperationalError:  # trigram's remove_diacritics needs SQLite 3.45+
        db.close()
        os.remove(db_path)
        db = sqlite3.connect(db_path)
        db.executescript(SCHEMA.replace(" remove_diacritics 1", ""))
    fts = []

    def note(kind, title, body, parent=None, created=""):
        cur = db.execute("insert into notes(kind,title,body,parent,created) values(?,?,?,?,?)",
                         (kind, title, body, parent, created))
        shown = note_title(title, MD["en"])
        fts.append((norm(f"{shown}\n{body}"), kind, f"n:{cur.lastrowid}", title, created))
        return cur.lastrowid

    # chats
    seen = set()
    for key in exp.find("*conversations*.json"):
        if "design_chats" in key:
            continue
        print(f"  reading {key} …", flush=True)
        for c in exp.json(key):
            if c["uuid"] in seen:
                continue
            seen.add(c["uuid"])
            title = c.get("name") or "(untitled)"
            files, n_chars, web, att = {}, 0, 0, 0
            msgs = c.get("chat_messages") or []
            for i, m in enumerate(msgs):
                blocks, text_parts = [], []
                content = m.get("content") or [{"type": "text", "text": m.get("text", "")}]
                for b in content:
                    t = b.get("type")
                    if t == "text" and b.get("text"):
                        blocks.append({"t": "text", "v": b["text"]})
                        text_parts.append(b["text"])
                    elif t == "thinking" and b.get("thinking"):
                        blocks.append({"t": "thinking", "v": b["thinking"]})
                    elif t == "tool_use":
                        name, inp = b.get("name") or "", b.get("input") or {}
                        blk = {"t": "tool", "name": name, "v": tool_label(name, inp)}
                        web = web or int(any(k in name.lower() for k in WEB_TOOLS))
                        # Files Claude wrote in the chat are rebuilt by replaying create_file and its later edits
                        if name == "create_file" and inp.get("path"):
                            files[inp["path"]] = {"content": inp.get("file_text") or "", "created": m.get("created_at"),
                                                  "edits": 0, "failed": 0, "delivered": 0, "name": None}
                            blk["path"] = inp["path"]
                        elif name == "str_replace" and inp.get("path") in files:
                            apply_edit(files[inp["path"]], inp.get("old_str"), inp.get("new_str"))
                            blk["path"] = inp["path"]
                        elif name == "artifacts" and inp.get("id"):  # older artifacts tool
                            p = f"artifact:{inp['id']}"
                            cmd = inp.get("command")
                            if cmd in ("create", "rewrite") or p not in files:
                                files[p] = {"content": inp.get("content") or "", "created": m.get("created_at"),
                                            "edits": 0, "failed": 0, "delivered": 1, "name": inp.get("title")}
                            elif cmd == "update":
                                apply_edit(files[p], inp.get("old_str"), inp.get("new_str"))
                            if inp.get("title"):
                                files[p]["name"] = inp["title"]
                            blk["path"] = p
                        elif name == "present_files":
                            for p in inp.get("filepaths") or []:
                                if p in files:
                                    files[p]["delivered"] = 1
                        blocks.append(blk)
                    elif t == "tool_result":
                        r = result_text(b)
                        if r:
                            blocks.append({"t": "result", "name": b.get("name") or "", "v": r})
                att = att or int(bool(m.get("attachments") or m.get("files")))
                for a in m.get("attachments") or []:
                    if a.get("extracted_content"):
                        blocks.append({"t": "att", "name": a.get("file_name") or "attachment", "v": a["extracted_content"]})
                        text_parts.append(a["extracted_content"])
                for f in m.get("files") or []:
                    if f.get("file_name"):
                        blocks.append({"t": "file", "name": f["file_name"]})
                body = "\n".join(text_parts)
                n_chars += len(body)
                db.execute("insert into msgs values(?,?,?,?,?)",
                           (c["uuid"], i, m.get("sender"), m.get("created_at"), json.dumps(blocks, ensure_ascii=False)))
                if body.strip():
                    fts.append((norm(body), "msg", f"c:{c['uuid']}:{i}", title, m.get("created_at") or ""))
            nfiles = 0
            for path, f in files.items():
                if not f["content"].strip():
                    continue
                delivered = int(bool(f["delivered"] or "/outputs/" in path))
                nfiles += delivered
                name = f["name"] or os.path.basename(path)
                cur = db.execute("insert into files(conv,path,name,content,delivered,created,edits,failed_edits) "
                                 "values(?,?,?,?,?,?,?,?)",
                                 (c["uuid"], path, name, f["content"], delivered, f["created"], f["edits"], f["failed"]))
                fts.append((norm(f"{name}\n{f['content']}"), "file", f"f:{cur.lastrowid}", name, f["created"] or ""))
            db.execute("insert into convs values(?,?,?,?,?,?,?,?,?,?)",
                       (c["uuid"], title, c.get("created_at"), c.get("updated_at"), len(msgs), n_chars, c.get("summary"),
                        web, nfiles, att))

    # projects (current format: projects/<uuid>.json, older one: projects.json with a list)
    projects = [exp.json(k) for k in exp.find("*projects/*.json")]
    for key in exp.find("*projects.json"):
        projects.extend(exp.json(key))
    proj_names = {}
    for p in sorted(projects, key=lambda p: p.get("updated_at") or "", reverse=True):
        proj_names[p.get("uuid")] = p.get("name") or "(untitled)"
        docs = p.get("docs") or []
        if not (p.get("prompt_template") or "").strip() and not docs and not (p.get("description") or "").strip():
            continue
        body = "\n\n".join(x for x in [p.get("description"), p.get("prompt_template")] if x)
        pid = note("project", p.get("name") or "(untitled)", body, created=p.get("created_at") or "")
        for d in docs:
            note("project_doc", d.get("filename") or "file", d.get("content") or "", parent=pid, created=d.get("created_at") or "")

    # memory. Titles starting with "@" are translated by the UI
    for key in exp.find("*memories/*.json") + exp.find("*memories.json"):
        data = exp.json(key)
        for m in data if isinstance(data, list) else [data]:
            if m.get("conversations_memory"):
                note("memory", "@general", m["conversations_memory"])
            for pid, text in (m.get("project_memories") or {}).items():
                note("memory", f"@project {proj_names.get(pid, pid)}",
                     text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))
            for f in m.get("memory_files") or []:
                note("memory", f.get("path") or "memory file", f.get("content") or "")

    # routines
    for key in exp.find("*routines.json"):
        data = exp.json(key)
        items = data if isinstance(data, list) else next(iter(data.values()), [])
        for r in items:
            sched = r.get("schedule") or {}
            cron = sched.get("cron") if isinstance(sched, dict) else str(sched)
            head = f"`{cron}` · {'on' if r.get('enabled') else 'off'}\n\n"
            note("routine", r.get("name") or "routine", head + (r.get("prompt") or ""), created=r.get("created_at") or "")

    # artifacts
    for key in exp.find("*artifacts/*/artifact.json"):
        a = exp.json(key)
        vs = a.get("versions") or []
        if not vs:
            continue
        base = key.split("#")[0][: -len("artifact.json")]
        vers = []
        for v in vs:
            single = f"{base}versions/{v['id']}.html"
            entry = single if single in exp.files else f"{base}versions/{v['id']}/index.html"
            vers.append({"id": v["id"], "title": v.get("title"), "created": v.get("created_at"),
                         "entry": entry[len(base):] if entry in exp.files else None})
        vers.sort(key=lambda v: v["created"] or "", reverse=True)
        top = vers[0]
        db.execute("insert or replace into arts values(?,?,?,?,?,?)",
                   (a["id"], top["title"] or "(untitled)", vs[0].get("description") or "",
                    top["created"], json.dumps(vers, ensure_ascii=False), base))
        if top["entry"]:
            raw = exp.read(base + top["entry"]).decode("utf-8", "replace")
            txt = re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", raw, flags=re.S)
            fts.append((norm(f"{top['title']}\n{vs[0].get('description') or ''}\n{html.unescape(txt)}"),
                        "art", f"a:{a['id']}", top["title"] or "", top["created"] or ""))

    db.executemany("insert into fts values(?,?,?,?,?)", fts)
    db.execute("insert into meta values('schema', ?)", (str(SCHEMA_VERSION),))
    db.commit()
    db.close()
    print(f"  index built in {time.time() - t0:.0f}s: {len(seen)} chats, {len(fts)} searchable entries", flush=True)


# ---------- search ----------

def snippet(body, needle_re, width=160):
    m = needle_re.search(body)
    if not m:
        return html.escape(body[:width * 2])
    a, b = max(0, m.start() - width), min(len(body), m.end() + width)
    out = html.escape(body[a:m.start()]) + "<mark>" + html.escape(m.group(0)) + "</mark>" + html.escape(body[m.end():b])
    return ("…" if a else "") + out + ("…" if b < len(body) else "")


def search(db, q, mode, kind, sort, limit=300):
    q = norm(q).strip()
    if not q:
        return {"hits": [], "total": 0}
    where, args = [], []
    if kind != "all":
        kinds = {"note": ("memory", "project", "project_doc", "routine")}.get(kind, (kind,))
        where.append(f"kind in ({','.join('?' * len(kinds))})")
        args += kinds
    if mode == "re":
        try:
            terms_rx = re.compile(q, re.I)
        except re.error as e:
            return {"error": f"regex: {e}"}
        sql = "select body, kind, ref, title, created from fts" + (" where " + " and ".join(where) if where else "")
        hits = [h for h in db.execute(sql, args) if terms_rx.search(h[0])]
    else:
        terms = [t.strip('"') for t in re.findall(r'"[^"]+"|\S+', q) if t.strip('"')]
        terms_rx = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I)
        long_t = [t for t in terms if len(t) >= 3]
        w, a = list(where), list(args)
        if long_t:
            w.append("fts match ?")
            a.append(" AND ".join('"' + t.replace('"', '""') + '"' for t in long_t))
        for t in terms:
            if len(t) < 3:  # trigrams can't match words shorter than 3 characters
                w.append("body like ?")
                a.append(f"%{t}%")
        order = " order by rank" if long_t else ""
        hits = db.execute("select body, kind, ref, title, created from fts where " + " and ".join(w) + order, a).fetchall()
    total = len(hits)
    if sort == "date":
        hits = sorted(hits, key=lambda h: h[4] or "", reverse=True)
    out = [{"kind": k, "ref": ref, "title": title, "created": created, "snippet": snippet(body, terms_rx)}
           for body, k, ref, title, created in hits[:limit]]
    return {"hits": out, "total": total, "pattern": terms_rx.pattern}


# ---------- markdown for copying and the basket ----------

MD = {
    "en": {"me": "Me", "file_made": "file created", "attachment": "attachment", "fragment": "excerpt",
           "from_chat": "from the chat", "chat_in": "Chat in claude.ai", "file_by": "A file Claude made in the chat",
           "project_file": "Project file", "general": "General memory", "project_mem": "Project memory"},
    "ru": {"me": "Я", "file_made": "создан файл", "attachment": "вложение", "fragment": "фрагмент",
           "from_chat": "из чата", "chat_in": "Чат в claude.ai", "file_by": "Файл, который Claude сделал в чате",
           "project_file": "Файл проекта", "general": "Общая память", "project_mem": "Память проекта"},
}


def note_title(title, t):
    if title == "@general":
        return t["general"]
    if title.startswith("@project "):
        return f"{t['project_mem']}: {title[9:]}"
    return title


def blocks_md(blocks, t):
    out = []
    for b in blocks:
        if b["t"] == "text":
            out.append(b["v"])
        elif b["t"] == "tool" and b.get("name") in ("create_file", "artifacts") and b.get("path"):
            out.append(f"*[{t['file_made']}: {os.path.basename(b['path'])}]*")
        elif b["t"] in ("att", "file"):
            out.append(f"*[{t['attachment']}: {b['name']}]*")
    return "\n\n".join(out).strip()


def to_md(db, ref, lang="en"):
    t = MD.get(lang, MD["en"])
    kind, _, rest = ref.partition(":")
    if kind == "c":
        cid, _, idx = rest.partition(":")
        title, created = db.execute("select title, created from convs where id=?", (cid,)).fetchone()
        url = f"https://claude.ai/chat/{cid}"
        if idx:
            sender, cr, blocks = db.execute("select sender, created, blocks from msgs where conv=? and idx=?",
                                            (cid, int(idx))).fetchone()
            who = t["me"] if sender == "human" else "Claude"
            return f"# {title} — {t['fragment']}\n\n{who}, {(cr or '')[:10]}, {t['from_chat']} {url}\n\n{blocks_md(json.loads(blocks), t)}\n"
        parts = [f"# {title}\n\n{t['chat_in']}, {(created or '')[:10]}, {url}"]
        for sender, blocks in db.execute("select sender, blocks from msgs where conv=? order by idx", (cid,)):
            text = blocks_md(json.loads(blocks), t)
            if text:
                parts.append(f"**{t['me'] if sender == 'human' else 'Claude'}:**\n\n{text}")
        return "\n\n".join(parts) + "\n"
    if kind == "f":
        name, content, created, ctitle = db.execute(
            "select f.name, f.content, f.created, c.title from files f left join convs c on c.id=f.conv where f.id=?",
            (int(rest),)).fetchone()
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        body = content if ext in ("", "md", "markdown", "txt") else f"```{ext}\n{content}\n```"
        return f"# {name}\n\n{t['file_by']} «{ctitle}», {(created or '')[:10]}\n\n{body}\n"
    if kind == "n":
        title, body = db.execute("select title, body from notes where id=?", (int(rest),)).fetchone()
        out = f"# {note_title(title, t)}\n\n{body}\n"
        for dt, dbody in db.execute("select title, body from notes where parent=? order by id", (int(rest),)):
            out += f"\n## {t['project_file']}: {dt}\n\n{dbody}\n"
        return out
    raise ValueError("this item can't be exported to markdown")


# ---------- server ----------

class App:
    def __init__(self, exp, db_path):
        self.exp = exp
        self.db_path = db_path
        self.local = threading.local()

    @property
    def db(self):
        if not hasattr(self.local, "db"):
            self.local.db = sqlite3.connect(self.db_path)
        return self.local.db


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype="application/json; charset=utf-8", code=200, headers=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode() if "json" in ctype else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = unquote(u.path)
            db = app.db
            # Pages Claude made run sandboxed, so their scripts can't reach the API
            sandbox = {"Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals"}
            try:
                if p == "/":
                    return self.send(PAGE, "text/html; charset=utf-8")
                if p == "/api/stats":
                    c = lambda sql: db.execute(sql).fetchone()[0]
                    return self.send({
                        "chats": c("select count(*) from convs"),
                        "files": c("select count(*) from files where delivered=1"),
                        "memory": c("select count(*) from notes where kind='memory'"),
                        "projects": c("select count(*) from notes where kind='project'"),
                        "routines": c("select count(*) from notes where kind='routine'"),
                        "arts": c("select count(*) from arts"),
                        "root": app.exp.root,
                    })
                if p == "/api/convs":
                    cols = ("id", "title", "created", "updated", "n", "chars", "web", "nfiles", "att")
                    rows = db.execute(f"select {','.join(cols)} from convs").fetchall()
                    return self.send([dict(zip(cols, r)) for r in rows])
                if p == "/api/md":
                    return self.send({"md": to_md(db, qs.get("ref", ""), qs.get("lang", "en"))})
                if p.startswith("/api/conv/"):
                    cid = p.split("/")[3]
                    c = db.execute("select id,title,created,updated,n,chars,summary from convs where id=?", (cid,)).fetchone()
                    if not c:
                        return self.send({"error": "no such chat"}, code=404)
                    msgs = [{"idx": i, "sender": s, "created": cr, "blocks": json.loads(b)}
                            for i, s, cr, b in db.execute("select idx,sender,created,blocks from msgs where conv=? order by idx", (cid,))]
                    files = [dict(zip(("id", "path", "name", "delivered"), r))
                             for r in db.execute("select id,path,name,delivered from files where conv=?", (cid,))]
                    return self.send({"conv": dict(zip(("id", "title", "created", "updated", "n", "chars", "summary"), c)),
                                      "msgs": msgs, "files": files})
                if p == "/api/files":
                    rows = db.execute("select f.id,f.name,f.path,f.delivered,f.created,length(f.content),f.conv,c.title "
                                      "from files f left join convs c on c.id=f.conv order by f.created desc").fetchall()
                    return self.send([dict(zip(("id", "name", "path", "delivered", "created", "size", "conv", "conv_title"), r)) for r in rows])
                if p.startswith("/api/file/"):
                    r = db.execute("select f.id,f.name,f.path,f.content,f.delivered,f.created,f.edits,f.failed_edits,f.conv,c.title "
                                   "from files f left join convs c on c.id=f.conv where f.id=?", (int(p.split("/")[3]),)).fetchone()
                    return self.send(dict(zip(("id", "name", "path", "content", "delivered", "created", "edits", "failed", "conv", "conv_title"), r)))
                if p.startswith("/raw/file/"):
                    name, content = db.execute("select name,content from files where id=?", (int(p.split("/")[3]),)).fetchone()
                    ctype = (mimetypes.guess_type(name)[0] or "text/plain") + "; charset=utf-8"
                    hdr = dict(sandbox)
                    if qs.get("download"):
                        hdr["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(name)
                    return self.send(content, ctype, headers=hdr)
                if p == "/api/notes":
                    rows = db.execute("select n.id,n.title,length(n.body),n.created,(select count(*) from notes d where d.parent=n.id) "
                                      "from notes n where kind=? order by n.id", (qs.get("kind", "memory"),)).fetchall()
                    return self.send([dict(zip(("id", "title", "size", "created", "children"), r)) for r in rows])
                if p.startswith("/api/note/"):
                    nid = int(p.split("/")[3])
                    r = db.execute("select id,kind,title,body,parent,created from notes where id=?", (nid,)).fetchone()
                    kids = [dict(zip(("id", "title", "size"), k)) for k in
                            db.execute("select id,title,length(body) from notes where parent=? order by id", (nid,))]
                    return self.send({**dict(zip(("id", "kind", "title", "body", "parent", "created"), r)), "children": kids})
                if p == "/api/arts":
                    rows = db.execute("select id,title,descr,created,versions from arts order by created desc").fetchall()
                    return self.send([{"id": i, "title": t, "descr": d, "created": c, "versions": json.loads(v)} for i, t, d, c, v in rows])
                if p.startswith("/a/"):  # /a/<artifact>/<path inside the artifact>
                    _, _, aid, rest = p.split("/", 3)
                    row = db.execute("select base from arts where id=?", (aid,)).fetchone()
                    key = row and row[0] + rest
                    if not key or ".." in rest or key not in app.exp.files:
                        return self.send("not found", "text/plain; charset=utf-8", 404)
                    ctype = mimetypes.guess_type(rest)[0] or "application/octet-stream"
                    if ctype.startswith("text/"):
                        ctype += "; charset=utf-8"
                    return self.send(app.exp.read(key), ctype, headers=sandbox)
                if p == "/api/search":
                    return self.send(search(db, qs.get("q", ""), qs.get("mode", "fts"), qs.get("kind", "all"), qs.get("sort", "rank")))
                return self.send({"error": "not found"}, code=404)
            except Exception as e:  # a local tool: show the error as is
                return self.send({"error": f"{type(e).__name__}: {e}"}, code=500)

    return H


# ---------- writing out as .md ----------

def safe(name, n=80):
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", name or "").strip()
    return name[:n].strip() or "untitled"


def dump(db_path, out):
    db = sqlite3.connect(db_path)
    t = MD["en"]

    def write(rel, text):
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        base, ext = os.path.splitext(path)
        i = 2
        while os.path.exists(path):
            path = f"{base} ({i}){ext}"
            i += 1
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    for title, body in db.execute("select title, body from notes where kind='memory'"):
        rel = title.lstrip("/") if title.startswith("/") else safe(note_title(title, t)) + ".md"
        write(os.path.join("memory", rel), body)
    for pid, title, body in db.execute("select id, title, body from notes where kind='project'").fetchall():
        d = os.path.join("projects", safe(title))
        if body.strip():
            write(os.path.join(d, "instructions.md"), body)
        for dt, dbody in db.execute("select title, body from notes where parent=?", (pid,)):
            write(os.path.join(d, "files", safe(dt, 120)), dbody)
    for title, body in db.execute("select title, body from notes where kind='routine'"):
        write(os.path.join("routines", safe(title) + ".md"), body)
    n = 0
    for name, content, created, ctitle in db.execute(
            "select f.name, f.content, f.created, c.title from files f left join convs c on c.id=f.conv where f.delivered=1"):
        fname = safe(name, 120)
        if "." not in fname:
            fname += ".md"
        write(os.path.join("claude-files", f"{(created or '')[:10]} {safe(ctitle, 60)}", fname), content)
        n += 1
    print(f"Done: {out}\n  memory/, projects/, routines/ and {n} of Claude's files in claude-files/")


# ---------- startup ----------

def resolve_root(arg):
    if arg:
        return os.path.abspath(os.path.expanduser(arg))
    found = find_exports()
    if found:
        if len(found) > 1:
            print("Found several exports, opening the newest one:")
            for f in found:
                print(("  → " if f == found[0] else "    ") + f)
            print("To open another one: python3 viewer.py \"path/to/folder\"\n")
        return found[0]
    picked = pick_folder_mac()
    if picked:
        return picked.rstrip("/")
    sys.exit("Couldn't find a Claude export in ~/Downloads.\n"
             "Run it with the folder path: python3 viewer.py \"path/to/folder\"")


def serve(app, port, open_browser):
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            break
        except OSError:
            continue
    else:
        sys.exit(f"Ports {port}–{port + 19} are all busy")
    url = f"http://127.0.0.1:{p}/"
    print(f"Open: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Claude Export Viewer")
    ap.add_argument("export", nargs="?", help="folder with the export (zips don't need unpacking); "
                                              "if omitted, it's looked up in ~/Downloads")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--rebuild", action="store_true", help="rebuild the index")
    ap.add_argument("--dump", action="store_true", help="write memory, projects, routines and Claude's files as .md and exit")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    root = resolve_root(args.export)
    if not os.path.isdir(root):
        sys.exit(f"No such folder: {root}")
    exp = Export(root)
    if not exp.find("*conversations*.json"):
        sys.exit(f"No conversations.json in {root} (neither in a zip nor unpacked). Is this the export folder?")
    print(f"Export: {root}")
    db_path = os.path.join(root, DB_NAME)
    fresh = os.path.exists(db_path)
    if fresh:
        try:
            fresh = sqlite3.connect(db_path).execute("select v from meta where k='schema'").fetchone()[0] == str(SCHEMA_VERSION)
        except sqlite3.Error:
            fresh = False
    if args.rebuild or not fresh:
        print("Building the index (a minute or two for big exports, only the first time)…", flush=True)
        build(exp, db_path)
    if args.dump:
        return dump(db_path, os.path.join(root, "claude-export-md"))
    serve(App(exp, db_path), args.port, not args.no_browser)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude archive</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<style>
:root{
  --bg:hsl(0,0%,98%);--side:hsl(0,0%,96%);--panel:#fff;--ink:#000;--ink2:hsl(0,0%,22%);--muted:hsl(0,0%,45%);--faint:hsl(0,0%,62%);
  --line:hsl(0,0%,89%);--line2:hsl(0,0%,80%);--accent:hsl(330,100%,40%);--accent-soft:hsl(330,100%,96%);
  --human:hsl(0,0%,94%);--mark:hsl(330,100%,90%);--code:hsl(0,0%,95%);
  --font:"Onest",-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:hsl(0,0%,8%);--side:hsl(0,0%,6%);--panel:hsl(0,0%,11%);--ink:hsl(0,0%,96%);--ink2:hsl(0,0%,80%);--muted:hsl(0,0%,58%);--faint:hsl(0,0%,42%);
  --line:hsl(0,0%,17%);--line2:hsl(0,0%,26%);--accent:hsl(330,90%,64%);--accent-soft:hsl(330,50%,15%);
  --human:hsl(0,0%,14%);--mark:hsl(330,60%,28%);--code:hsl(0,0%,11%);
}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font:15px/1.5 var(--font);background:var(--bg);color:var(--ink);display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
::selection{background:var(--mark)}
button,select,input{font:inherit;color:inherit}
.label{font-size:.72rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}

header{display:grid;grid-template-columns:190px 1fr;align-items:center;height:58px;border-bottom:1px solid var(--line);background:var(--bg)}
.logo{font-weight:700;white-space:nowrap;padding-left:18px;letter-spacing:-.01em}
.logo i{font-style:normal;color:var(--accent)}
.search{display:flex;align-items:center;gap:18px;padding:0 18px 0 14px;min-width:0}
#q{flex:1;min-width:0;padding:8px 0;border:0;border-bottom:1px solid var(--line2);outline:0;background:transparent;font-size:16px;transition:border-color .15s}
#q:focus{border-color:var(--ink)}
#q::placeholder{color:var(--faint)}
select{appearance:none;field-sizing:content;padding:2px 13px 2px 0;border:0;background:transparent url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='5'%3E%3Cpath d='M.5.5l3.5 3.5 3.5-3.5' fill='none' stroke='%23999' stroke-width='1.2'/%3E%3C/svg%3E") no-repeat right 0 center;font-size:13px;color:var(--muted);cursor:pointer;outline:0}
select:hover,select:focus-visible{color:var(--ink)}
select.set{color:var(--ink)}
.btn{padding:6px 12px;border:1px solid var(--line2);border-radius:999px;background:transparent;font-size:13px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--ink)}
.btn.ink{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.btn.ink:hover{opacity:.85}
.btn.done{color:var(--accent);border-color:var(--accent)}

main{flex:1;display:grid;grid-template-columns:190px 370px 1fr;min-height:0}
nav{border-right:1px solid var(--line);padding:16px 0;overflow:auto;display:flex;flex-direction:column}
nav>a{display:flex;justify-content:space-between;align-items:baseline;padding:7px 18px;color:var(--ink2);text-decoration:none;cursor:pointer}
nav>a:hover{color:var(--ink)}
nav>a.on{color:var(--ink);font-weight:500;box-shadow:inset 2px 0 0 var(--accent)}
nav>a span{color:var(--faint);font-size:12px;font-variant-numeric:tabular-nums}
nav a.basket{margin-top:14px;border-top:1px solid var(--line);padding-top:14px}
nav a.basket.full span{color:var(--accent);font-weight:700}
nav a.help{color:var(--muted)}
.langs{margin-top:auto;padding:14px 18px 0;font-size:12px;color:var(--faint);display:flex;gap:10px}
.langs a{cursor:pointer;color:var(--faint);text-decoration:none}
.langs a.on{color:var(--ink);font-weight:700}

#list{border-right:1px solid var(--line);overflow:auto;display:flex;flex-direction:column}
#tools{padding:12px 14px 10px;border-bottom:1px solid var(--line);display:flex;gap:8px 16px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--muted);position:sticky;top:0;background:var(--bg);z-index:1}
#tools input[type=text]{flex:1 1 100%;padding:4px 0 6px;border:0;border-bottom:1px solid var(--line);background:transparent;outline:0;font-size:14px}
#tools input[type=text]:focus{border-color:var(--ink)}
#tools input[type=text]::placeholder{color:var(--faint)}
.tog{display:inline-flex;align-items:center;gap:5px;cursor:pointer;user-select:none}
.tog input{position:absolute;opacity:0;pointer-events:none}
.tog::before{content:"";width:7px;height:7px;border-radius:50%;border:1px solid var(--faint)}
.tog:has(input:checked){color:var(--ink)}
.tog:has(input:checked)::before{background:var(--accent);border-color:var(--accent)}
.count{color:var(--faint);font-size:12px;padding:10px 14px 4px;font-variant-numeric:tabular-nums}
.item{display:flex;gap:8px;align-items:flex-start;padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.item>div{flex:1;min-width:0}
.item:hover{background:var(--side)}
.item.on{background:var(--panel);box-shadow:inset 2px 0 0 var(--accent)}
.item .t{font-weight:500;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.item .m{color:var(--muted);font-size:12px;margin-top:2px;font-variant-numeric:tabular-nums}
.item .s{font-size:13px;margin-top:4px;color:var(--ink2);word-break:break-word}
.kind{font-size:.68rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-right:6px}
.add{flex:none;width:24px;height:24px;border-radius:50%;border:1px solid var(--line2);background:transparent;color:var(--muted);cursor:pointer;font-size:15px;line-height:1;opacity:0;transition:opacity .1s}
.item:hover .add,.add.in{opacity:1}
.add:hover{border-color:var(--ink);color:var(--ink)}
.add.in{border-color:var(--accent);background:var(--accent);color:#fff}

#reader{overflow:auto;padding:30px 44px 120px}
.page{max-width:740px;margin:0 auto}
#reader h1{font-size:1.75rem;font-weight:700;line-height:1.15;letter-spacing:-.02em;margin:0 0 10px}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 26px}
a.u,.sub a,.files a{color:inherit;text-decoration:underline;text-decoration-color:var(--line2);text-underline-offset:3px;cursor:pointer}
a.u:hover,.sub a:hover,.files a:hover{text-decoration-color:var(--ink)}
.phead{position:sticky;top:-30px;z-index:2;margin:-30px -44px 0;padding:30px 44px 2px;background:var(--bg);transition:padding .15s}
.phead.stuck{padding-top:14px;border-bottom:1px solid var(--line);margin-bottom:24px}
.phead.stuck h1{font-size:1.15rem;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.phead.stuck .sub{margin-bottom:10px}
.phead.stuck .actions,.phead.stuck .cont{margin-bottom:12px}
.cont{margin:-10px 0 26px;padding:6px 0 4px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.step{display:flex;gap:14px;padding:10px 0;font-size:14px}
.step+.step{border-top:1px solid var(--line)}
.step>b{flex:none;width:20px;color:var(--accent);font-weight:700;font-variant-numeric:tabular-nums}
.step .m{color:var(--muted);font-size:12.5px;margin-top:3px}
.prose{font-size:16px;line-height:1.65}
.prose h1,.prose h2,.prose h3{font-weight:700;line-height:1.25;margin:1.4em 0 .5em;letter-spacing:-.01em}
.prose h1{font-size:1.3rem}.prose h2{font-size:1.15rem}.prose h3{font-size:1rem}
.prose p{margin:.6em 0}
.prose ul,.prose ol{padding-left:1.3em}
.prose li{margin:.2em 0}
.prose blockquote{margin:1em 0;padding:0 0 0 1em;border-left:1px solid var(--accent);color:var(--ink2)}
.prose a{color:inherit;text-decoration-color:var(--line2);text-underline-offset:3px}
.prose hr{border:0;border-top:1px solid var(--line);margin:1.6em 0}
.msg{margin:28px 0;scroll-margin-top:200px}
.who{display:flex;gap:10px;align-items:baseline;margin-bottom:6px}
.who .label{color:var(--ink2)}
.who .when{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}
.who .mact{display:flex;gap:10px;opacity:0;transition:opacity .1s;font-size:12px}
.msg:hover .mact{opacity:1}
.mact a{color:var(--muted);cursor:pointer;text-decoration:none}
.mact a:hover{color:var(--ink)}
.mact a.in{color:var(--accent)}
.msg.human .bubble{background:var(--human);border-radius:14px;padding:10px 16px;font-size:15px}
.msg.human .bubble p{margin:.35em 0}
.msg.target .bubble,.msg.assistant.target .body{box-shadow:-12px 0 0 -10px var(--accent)}
.tool{display:flex;max-width:100%;font-size:12px;color:var(--muted);margin:4px 0;font-family:var(--mono)}
.tool span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
details{margin:6px 0;font-size:13px}
details summary{cursor:pointer;color:var(--muted);list-style:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"+ ";font-family:var(--mono)}
details[open] summary::before{content:"− "}
details.tools{margin:10px 0;padding-left:12px;border-left:1px solid var(--line)}
details.tools pre{margin:4px 0 10px;max-height:260px;font-size:12px}
.made{font-size:13px;color:var(--muted);margin:6px 0 10px}
.made a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--accent);text-underline-offset:3px;cursor:pointer;margin-right:10px}
pre,code{font-family:var(--mono);font-size:13px}
pre{background:var(--code);padding:14px 16px;border-radius:6px;overflow:auto;white-space:pre-wrap;word-break:break-word;line-height:1.5}
code{background:var(--code);padding:1px 4px;border-radius:3px}
pre code{padding:0;background:none}
table{border-collapse:collapse;margin:12px 0;font-size:14px}
td,th{border-bottom:1px solid var(--line);padding:6px 12px 6px 0;text-align:left;vertical-align:top}
th{border-bottom-color:var(--ink);font-weight:700}
mark{background:var(--mark);color:inherit;border-radius:2px}
iframe{width:100%;height:calc(100vh - 240px);border:1px solid var(--line);background:#fff}
.empty{color:var(--muted);padding:70px 30px;text-align:center;max-width:460px;margin:0 auto}
.empty b{display:block;color:var(--ink);font-size:1.1rem;margin-bottom:8px}
.files{display:flex;flex-wrap:wrap;gap:4px 14px;margin:0 0 18px;font-size:13px;color:var(--muted)}
.bsum{display:flex;gap:28px;margin:6px 0 20px}
.bsum b{display:block;font-size:1.6rem;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.guide h2{font-size:1.05rem;font-weight:700;margin:2em 0 .5em}
.guide p,.guide li{color:var(--ink2)}
.guide b{color:var(--ink);font-weight:500}
.guide ol,.guide ul{padding-left:1.2em}
.guide li{margin:.35em 0}
.lede{font-size:1.1rem;line-height:1.55;color:var(--ink2);margin:0 0 8px}
.guide .credit{margin-top:3em;padding-top:14px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--bg);padding:8px 16px;border-radius:999px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.on{opacity:1}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--line2);border-radius:10px;border:3px solid transparent;background-clip:content-box}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:900px){header{grid-template-columns:1fr;height:auto;padding:10px 0;gap:6px}.search{flex-wrap:wrap;gap:6px 14px;padding:0 16px}#q{flex:1 1 100%}main{grid-template-columns:1fr}nav{flex-direction:row;overflow-x:auto;padding:0;border-bottom:1px solid var(--line)}nav>a{white-space:nowrap;gap:6px;padding:10px 14px}nav a.basket{margin:0;border:0;padding-top:10px}nav>a.on{box-shadow:inset 0 -2px 0 var(--accent)}.langs{margin:0;padding:10px 14px}#list{max-height:45vh;border-right:0;border-bottom:1px solid var(--line)}#reader{padding:20px 16px 80px}.phead{top:-20px;margin:-20px -16px 0;padding:20px 16px 2px}.add{opacity:1}}
</style>
</head>
<body>
<header>
  <div class="logo" id="logo"></div>
  <div class="search">
    <input id="q" autocomplete="off">
    <select id="kind"></select>
    <select id="sort"></select>
    <select id="mode"></select>
    <button class="btn ink" id="go"></button>
  </div>
</header>
<main>
  <nav id="nav"></nav>
  <div id="list"></div>
  <div id="reader"></div>
</main>
<div class="toast" id="toast"></div>
<script>
// ---------- strings ----------
const STR = {
en: {
  logo: 'claude archive', title: 'Claude archive',
  chats: 'Chats', files: 'Claude’s files', memory: 'Memory', projects: 'Projects', routines: 'Routines', arts: 'Artifacts', basket: 'Basket', help: 'How to use',
  q: 'Search chats, files, memory, projects', find: 'Search',
  k_all: 'everywhere', k_msg: 'in chats', k_file: 'in Claude’s files', k_note: 'in memory and projects', k_art: 'in artifacts',
  s_rank: 'most relevant', s_date: 'newest first', m_fts: 'words', m_re: 'regex',
  f_chat: 'Chat title', f_file: 'File or chat name', f_title: 'Filter by title', f_any: 'Filter',
  cs_upd: 'recent', cs_len: 'longest', cs_new: 'by start date',
  p_all: 'all time', p_30: 'last 30 days', p_90: 'last 3 months', p_year: 'year', p_month: 'month',
  t_any: 'any kind of work', t_web: 'with web search', t_files: 'with Claude’s files', t_att: 'with attachments',
  short: 'short ones', drafts: 'drafts', drafts_tip: 'files Claude made for itself along the way',
  of: (a, b) => `${a} of ${b}`, nfiles: n => `${n} files`, msgs: n => `${n} msgs`, chars: n => `${n} chars`, filesN: n => `files: ${n}`, versions: n => `versions: ${n}`, empty: 'nothing here',
  open_ai: 'open in claude.ai ↗', cont: 'Continue in a new chat', copy_md: 'Copy as markdown', add: '+ Add to basket', added: '✓ In basket',
  files_lbl: 'files', summary: 'Claude’s summary', me: 'me', claude: 'claude', copy: 'copy', add_s: '+ basket', added_s: '✓ in basket',
  thinking: 'thinking', tool_results: 'tool results', attachment: 'attachment', made: 'Created file:',
  from_chat: 'from the chat', dl_file: 'download file', failed: n => `${n} of Claude’s edits didn’t apply, this may differ from the final version`,
  proj_files: 'project files', back_proj: '← back to project', latest: ' (latest)', newtab: 'open in a new tab ↗', no_vers: 'this artifact has no saved versions',
  general: 'General memory', project_mem: 'Project memory',
  kinds: {msg: 'chat', file: 'file', memory: 'memory', project: 'project', project_doc: 'project file', routine: 'routine', art: 'artifact'},
  b_order: 'In the order added', b_empty: 'The basket is empty', b_clear: 'Clear', b_title: 'Basket', b_pieces: 'pieces', b_chars: 'characters', b_tokens: 'tokens',
  b_copy: 'Copy', b_dl: 'Download .md', b_trunc: '… [the preview shows the first 200k characters, the file has everything]',
  b_empty_long: 'Press + next to chats, files, memory or single messages. They come together here as one markdown file you can paste into a new project, a CLAUDE.md, or hand to another assistant.',
  b_head: (d, n) => `# Context from my Claude archive\n\nCollected ${d}, pieces: ${n}.`, b_name: d => `Claude archive selection ${d}`,
  bk: {msg: 'message', chat: 'chat', file: 'file', note: 'memory/project'}, b_fail: 'could not export',
  collecting: 'collecting…', searching: 'searching…', found: (n, m) => `Found: ${n}` + (m ? `, showing the first ${m}` : ''), reset: 'Reset',
  toast_add: 'Added to the basket', toast_copy: n => `Copied: ${n} characters`, toast_prompt: 'Prompt copied',
  c1: 'File downloaded:', c2: 'Open a new chat:', c2h: p => `A prompt for the chat gets copied automatically: “${p}”`, c3: 'Paste the prompt (⌘V / Ctrl+V), drag the file from Downloads into the chat and send.',
  p_chat: 'Attached is our earlier conversation from my archive. Read all of it, briefly tell me where we left off, and wait for my question.',
  p_bundle: 'Attached is a selection of material from my chat archive. Read all of it, briefly summarize what’s there, and wait for my question.',
  k: 'k', locale: 'en-US', MON: ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],
  MON1: ['January','February','March','April','May','June','July','August','September','October','November','December'],
  day: (d, mon, y) => `${mon} ${d}` + (y ? `, ${y}` : ''),
  guide: s => `<h1>How to use</h1>
  <p class="lede">This is your claude.ai export laid out on shelves: ${s.chats} chats, ${s.files} files Claude made, memory, projects, routines and artifacts. It all runs on your computer and nothing is sent anywhere.</p>
  <h2>What’s where</h2>
  <ul>
    <li><b>Chats</b> — every conversation. Sort by recency or length, filter by period and kind of work: with web search, with Claude’s files, with attachments. Chats of one or two messages are hidden; the “short ones” toggle shows them.</li>
    <li><b>Claude’s files</b> — documents, reports, code and pages Claude made in your chats, rebuilt with its later edits.</li>
    <li><b>Memory</b> — what Claude remembered about you: general memory, project memory, memory files.</li>
    <li><b>Projects</b> — project instructions and the files uploaded to them.</li>
    <li><b>Routines and artifacts</b> — prompts of cloud routines and published pages with all their versions.</li>
  </ul>
  <h2>Search</h2>
  <p>The bar at the top searches everything at once. Narrow it to chats, files or memory, sort by date, or switch to regular expressions. Clicking a result opens the chat right at that message.</p>
  <h2>Basket: collect context to carry over</h2>
  <ol>
    <li>Press <b>+</b> next to chats, files, memory, projects or single messages. A handy way: search a topic and drop the right pieces in straight from the results.</li>
    <li>Open the <b>Basket</b>: it shows how many characters and tokens you’ve collected, and a preview of the final text.</li>
    <li><b>Copy</b> or <b>download the .md</b> and put it into a new project, a CLAUDE.md, another assistant’s instructions. Or press <b>Continue in a new chat</b> to start a conversation with this context right away.</li>
  </ol>
  <h2>Continue an old conversation</h2>
  <p>Every chat has a <b>Continue in a new chat</b> button. The conversation moves as a file, so length doesn’t matter:</p>
  <ol>
    <li>The file with the conversation downloads.</li>
    <li>You open a new chat in Claude or ChatGPT, and a short prompt for it is copied for you.</li>
    <li>You paste the prompt, drag the file into the chat and send.</li>
  </ol>
  <p>There’s also <b>Copy as markdown</b> for the whole chat and for each message.</p>
  <h2>What isn’t here</h2>
  <ul>
    <li>Skills you uploaded to claude.ai: Anthropic doesn’t put them in the export.</li>
    <li>Files Claude wrote through the terminal: only the command is visible.</li>
    <li>Which project a chat belongs to: the export doesn’t record it.</li>
  </ul>
  <p class="credit">Built by Claude Code for Nailia · <a class="u" href="https://nailia.work" target="_blank" rel="noopener">nailia.work</a></p>`,
},
ru: {
  logo: 'архив claude', title: 'Архив Claude',
  chats: 'Чаты', files: 'Файлы Claude', memory: 'Память', projects: 'Проекты', routines: 'Рутины', arts: 'Артефакты', basket: 'Корзина', help: 'Как пользоваться',
  q: 'Искать в чатах, файлах, памяти, проектах', find: 'Найти',
  k_all: 'везде', k_msg: 'в чатах', k_file: 'в файлах Claude', k_note: 'в памяти и проектах', k_art: 'в артефактах',
  s_rank: 'по релевантности', s_date: 'сначала новые', m_fts: 'по словам', m_re: 'regex',
  f_chat: 'Название чата', f_file: 'Имя файла или чата', f_title: 'Фильтр по названию', f_any: 'Фильтр',
  cs_upd: 'свежие', cs_len: 'длинные', cs_new: 'по дате начала',
  p_all: 'за все время', p_30: 'последние 30 дней', p_90: 'последние 3 месяца', p_year: 'год', p_month: 'месяц',
  t_any: 'любая работа', t_web: 'с поиском в вебе', t_files: 'с файлами Claude', t_att: 'с вложениями',
  short: 'короткие', drafts: 'черновые', drafts_tip: 'файлы, которые Claude создавал для себя по ходу работы',
  of: (a, b) => `${a} из ${b}`, nfiles: n => `${n} файлов`, msgs: n => `${n} сообщ.`, chars: n => `${n} зн.`, filesN: n => `файлов: ${n}`, versions: n => `версий: ${n}`, empty: 'пусто',
  open_ai: 'открыть в claude.ai ↗', cont: 'Продолжить в новом чате', copy_md: 'Скопировать как markdown', add: '+ В корзину', added: '✓ В корзине',
  files_lbl: 'файлы', summary: 'саммари Claude', me: 'я', claude: 'claude', copy: 'копировать', add_s: '+ в корзину', added_s: '✓ в корзине',
  thinking: 'размышления', tool_results: 'результаты инструментов', attachment: 'вложение', made: 'Создан файл:',
  from_chat: 'из чата', dl_file: 'скачать файл', failed: n => `часть правок Claude (${n}) не применилась, версия может отличаться от финальной`,
  proj_files: 'файлы проекта', back_proj: '← к проекту', latest: ' (последняя)', newtab: 'открыть в новой вкладке ↗', no_vers: 'у артефакта нет сохраненных версий',
  general: 'Общая память', project_mem: 'Память проекта',
  kinds: {msg: 'чат', file: 'файл', memory: 'память', project: 'проект', project_doc: 'файл проекта', routine: 'рутина', art: 'артефакт'},
  b_order: 'В порядке добавления', b_empty: 'Корзина пуста', b_clear: 'Очистить', b_title: 'Корзина', b_pieces: 'фрагментов', b_chars: 'знаков', b_tokens: 'токенов',
  b_copy: 'Скопировать', b_dl: 'Скачать .md', b_trunc: '… [в превью первые 200 тыс. знаков, в файле все]',
  b_empty_long: 'Нажимайте + у чатов, файлов, памяти или отдельных сообщений. Здесь соберется один markdown-файл: его можно вставить в новый проект, в CLAUDE.md или отдать другому ассистенту.',
  b_head: (d, n) => `# Контекст из архива Claude\n\nСобрано ${d}, фрагментов: ${n}.`, b_name: d => `Подборка из архива ${d}`,
  bk: {msg: 'сообщение', chat: 'чат', file: 'файл', note: 'память/проект'}, b_fail: 'не удалось выгрузить',
  collecting: 'собираю…', searching: 'ищу…', found: (n, m) => `Найдено: ${n}` + (m ? `, показаны первые ${m}` : ''), reset: 'Сбросить',
  toast_add: 'Добавлено в корзину', toast_copy: n => `Скопировано: ${n} знаков`, toast_prompt: 'Подсказка скопирована',
  c1: 'Файл скачан:', c2: 'Откройте новый чат:', c2h: p => `Подсказка для чата скопируется сама: «${p}»`, c3: 'Вставьте подсказку (⌘V / Ctrl+V), перетащите туда файл из Загрузок и отправьте.',
  p_chat: 'Во вложении наш прошлый разговор из архива. Прочитай его целиком, коротко скажи, на чем мы остановились, и жди моего вопроса.',
  p_bundle: 'Во вложении подборка материалов из моего архива чатов. Прочитай ее целиком, коротко перескажи, что там есть, и жди моего вопроса.',
  k: ' тыс.', locale: 'ru-RU', MON: ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'],
  MON1: ['январь','февраль','март','апрель','май','июнь','июль','август','сентябрь','октябрь','ноябрь','декабрь'],
  day: (d, mon, y) => `${d} ${mon}` + (y ? ` ${y}` : ''),
  guide: s => `<h1>Как пользоваться</h1>
  <p class="lede">Это ваш экспорт из claude.ai, разложенный по полкам: ${s.chats} чатов, ${s.files} файлов, которые сделал Claude, память, проекты, рутины и артефакты. Все работает на вашем компьютере, данные никуда не отправляются.</p>
  <h2>Что где лежит</h2>
  <ul>
    <li><b>Чаты</b> — все переписки. Сортировка по свежести и длине, фильтры по периоду и по типу работы: с поиском в вебе, с файлами Claude, с вложениями. Чаты из одного-двух сообщений скрыты, включаются переключателем «короткие».</li>
    <li><b>Файлы Claude</b> — документы, отчеты, код и страницы, которые Claude создавал в чатах, собранные с учетом его правок.</li>
    <li><b>Память</b> — то, что Claude запомнил о вас: общая память, память проектов, файлы памяти.</li>
    <li><b>Проекты</b> — инструкции проектов и загруженные в них файлы.</li>
    <li><b>Рутины и артефакты</b> — промпты облачных рутин и опубликованные страницы со всеми версиями.</li>
  </ul>
  <h2>Поиск</h2>
  <p>Строка сверху ищет сразу везде. Можно сузить до чатов, файлов или памяти, отсортировать по дате и переключиться на регулярные выражения. Клик по результату открывает чат прямо на нужном сообщении.</p>
  <h2>Корзина: собрать контекст для переезда</h2>
  <ol>
    <li>Нажимайте <b>+</b> у чатов, файлов, памяти, проектов или отдельных сообщений. Удобно искать тему и складывать в корзину нужные куски прямо из результатов.</li>
    <li>Откройте <b>Корзину</b>: там видно, сколько набралось знаков и токенов, и превью итогового текста.</li>
    <li><b>Скопируйте</b> или <b>скачайте .md</b> и положите в новый проект, в CLAUDE.md, в инструкции другого ассистента. Или нажмите <b>Продолжить в новом чате</b>, чтобы сразу начать разговор с этим контекстом.</li>
  </ol>
  <h2>Продолжить старый разговор</h2>
  <p>В каждом чате есть кнопка <b>Продолжить в новом чате</b>. Разговор переезжает файлом, так что длина не важна:</p>
  <ol>
    <li>Файл с разговором скачивается сам.</li>
    <li>Вы открываете новый чат в Claude или ChatGPT, короткая подсказка для него копируется в буфер.</li>
    <li>Вставляете подсказку, перетаскиваете файл в окно чата и отправляете.</li>
  </ol>
  <p>Есть и <b>Скопировать как markdown</b>: у чата целиком и у каждого сообщения отдельно.</p>
  <h2>Чего здесь нет</h2>
  <ul>
    <li>Скиллов, загруженных в claude.ai: Anthropic не кладет их в экспорт.</li>
    <li>Файлов, которые Claude писал через терминал: видна только команда.</li>
    <li>Привязки чатов к проектам: в экспорте ее нет.</li>
  </ul>
  <p class="credit">Собрано Claude Code у Наили · <a class="u" href="https://nailia.work" target="_blank" rel="noopener">nailia.work</a></p>`,
}};

const getLang = () => { const u = new URLSearchParams(location.search).get('lang'); if (u) return u; try { const l = localStorage.getItem('lang'); if (l) return l; } catch(e) {} return (navigator.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en'; };
let LANG = STR[getLang()] ? getLang() : 'en';
let T = STR[LANG];

// ---------- helpers ----------
const $ = s => document.querySelector(s);
const esc = s => (s ?? '').toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = u => fetch(u).then(r => r.json());
const md = s => (window.marked && window.DOMPurify) ? DOMPurify.sanitize(marked.parse(s || '')) : '<pre>' + esc(s) + '</pre>';
const thous = n => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + T.k : String(n);
const THIS_YEAR = new Date().getFullYear();
const day = s => { const d = new Date(s || ''); return isNaN(d) ? '' : T.day(d.getDate(), T.MON[d.getMonth()], d.getFullYear() !== THIS_YEAR ? d.getFullYear() : 0); };
const dayTime = s => { const d = new Date(s || ''); return isNaN(d) ? '' : day(s) + ', ' + String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0'); };
const span = (a, b) => day(a) === day(b) ? day(a) : day(a) + ' — ' + day(b);
const today = () => new Date().toISOString().slice(0, 10);
// memory titles that start with "@" are written by the indexer in a language-neutral form
const noteTitle = t => t === '@general' ? T.general : t.startsWith('@project ') ? `${T.project_mem}: ${t.slice(9)}` : t;
let tab = 'chats', hl = null, cache = {}, ROOT = '', STATS = {};

const TABS = ['chats', 'files', 'memory', 'projects', 'routines', 'arts'];

// ---------- basket: kept in the browser, separately for each export folder ----------
let basket = [];
const bkey = () => 'basket:' + ROOT;
function loadBasket(){ try { basket = JSON.parse(localStorage.getItem(bkey()) || '[]'); } catch(e) { basket = []; } }
function saveBasket(){ try { localStorage.setItem(bkey(), JSON.stringify(basket)); } catch(e) {} syncBasketUI(); }
const inBasket = ref => basket.some(b => b.ref === ref);
function toggleBasket(ref, title){
  if (inBasket(ref)) basket = basket.filter(b => b.ref !== ref);
  else { basket.push({ref, title}); toast(T.toast_add); }
  saveBasket();
}
function syncBasketUI(){
  const a = $('nav a.basket');
  if (a) { a.classList.toggle('full', basket.length > 0); a.querySelector('span').textContent = basket.length; }
  document.querySelectorAll('[data-add]').forEach(el => {
    const on = inBasket(el.dataset.add);
    el.classList.toggle('in', on);
    if (el.classList.contains('add')) el.textContent = on ? '✓' : '+';
    else if (el.classList.contains('btn')) { el.textContent = on ? T.added : T.add; el.classList.toggle('done', on); }
    else el.textContent = on ? T.added_s : T.add_s;
  });
}

// ---------- copying: clipboard API with a textarea fallback ----------
async function copyText(t){
  try { await navigator.clipboard.writeText(t); }
  catch(e) { const ta = document.createElement('textarea'); ta.value = t; ta.style.cssText = 'position:fixed;opacity:0'; document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  toast(T.toast_copy(thous(t.length)));
}
let toastT;
function toast(t){ const el = $('#toast'); el.textContent = t; el.classList.add('on'); clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove('on'), 1800); }
const getMd = ref => api('/api/md?lang=' + LANG + '&ref=' + encodeURIComponent(ref)).then(r => { if (r.error) throw new Error(r.error); return r.md; });

document.addEventListener('click', async e => {
  const add = e.target.closest('[data-add]');
  if (add) { e.stopPropagation(); e.preventDefault(); return toggleBasket(add.dataset.add, add.dataset.title || ''); }
  const cp = e.target.closest('[data-copy]');
  if (cp) { e.stopPropagation(); e.preventDefault(); try { copyText(await getMd(cp.dataset.copy)); } catch(err) { toast(err.message); } }
}, true);

const addBtn = (ref, title) => `<button class="add" data-add="${esc(ref)}" data-title="${esc(title)}">+</button>`;
const actions = (ref, title, extra = '') => `<div class="actions">${extra}<button class="btn" data-copy="${esc(ref)}">${T.copy_md}</button><button class="btn" data-add="${esc(ref)}" data-title="${esc(title)}">${T.add}</button></div>`;

// ---------- continue in a new chat ----------
// A file can't be attached to a Claude or ChatGPT chat from outside, and long text breaks in a link.
// So a conversation of any length moves as a file: download it, open a new chat, drag it in.
// The prompt isn't put into the link (?q=): on some accounts it gets sent right away, before the file.
const fileName = t => (t || 'chat').replace(/[\\/:*?"<>|\n\r\t]+/g, ' ').trim().slice(0, 80) + '.md';
function download(text, name){
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], {type: 'text/markdown'}));
  a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const contBtn = attrs => `<button class="btn ink" data-cont ${attrs}>${T.cont}</button>`;
async function continuePanel(btn){
  const box = btn.closest('.page').querySelector('.cont');
  const isBundle = !!btn.dataset.bundle;
  const text = isBundle ? await bundle() : await getMd(btn.dataset.ref);
  const name = fileName(isBundle ? T.b_name(today()) : btn.dataset.title);
  const prompt = isBundle ? T.p_bundle : T.p_chat;
  download(text, name);
  box.innerHTML = `
    <div class="step"><b>1</b><div>${T.c1} <a class="u" data-redl>${esc(name)}</a> <span class="m">· ${T.chars(thous(text.length))}</span></div></div>
    <div class="step"><b>2</b><div>${T.c2} <a class="u" data-go href="https://claude.ai/new" target="_blank" rel="noopener">Claude ↗</a>&nbsp;&nbsp;<a class="u" data-go href="https://chatgpt.com/" target="_blank" rel="noopener">ChatGPT ↗</a>
      <div class="m">${esc(T.c2h(prompt))}</div></div></div>
    <div class="step"><b>3</b><div>${T.c3}</div></div>`;
  box.hidden = false;
  box.querySelector('[data-redl]').onclick = () => download(text, name);
  box.querySelectorAll('[data-go]').forEach(a => a.addEventListener('click', () => {
    navigator.clipboard.writeText(prompt).catch(() => {});
    toast(T.toast_prompt);
  }));
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-cont]'); if (!b) return;
  e.preventDefault();
  continuePanel(b);
});

// ---------- layout ----------
function chrome(){
  document.documentElement.lang = LANG;
  document.title = T.title;
  $('#logo').innerHTML = esc(T.logo) + '<i>.</i>';
  $('#q').placeholder = T.q;
  $('#kind').innerHTML = ['all','msg','file','note','art'].map(k => `<option value="${k}">${T['k_' + k]}</option>`).join('');
  $('#sort').innerHTML = `<option value="rank">${T.s_rank}</option><option value="date">${T.s_date}</option>`;
  $('#mode').innerHTML = `<option value="fts">${T.m_fts}</option><option value="re">${T.m_re}</option>`;
  $('#go').textContent = T.find;
  const n = STATS;
  $('#nav').innerHTML = TABS.map(k => `<a data-tab="${k}">${T[k]} <span>${n[k] ?? ''}</span></a>`).join('')
    + `<a data-tab="basket" class="basket">${T.basket} <span>0</span></a><a data-tab="help" class="help">${T.help}</a>`
    + `<div class="langs"><a data-lang="en">EN</a><a data-lang="ru">RU</a></div>`;
  document.querySelectorAll('.langs a').forEach(a => a.classList.toggle('on', a.dataset.lang === LANG));
  syncBasketUI();
}

async function init(){
  STATS = await api('/api/stats');
  ROOT = STATS.root; loadBasket();
  chrome();
  $('#nav').onclick = e => {
    const l = e.target.closest('[data-lang]');
    if (l) { LANG = l.dataset.lang; T = STR[LANG]; try { localStorage.setItem('lang', LANG); } catch(err) {} chrome(); showTab(tab === 'help' ? 'chats' : tab); showGuide(); return; }
    const a = e.target.closest('a[data-tab]'); if (!a) return;
    if (a.dataset.tab === 'help') return showGuide(true);
    showTab(a.dataset.tab);
  };
  await showTab('chats');
  // deep link: #c:<chat>, #c:<chat>:<message>, #f:<file>, #n:<note>
  const ref = decodeURIComponent(location.hash.slice(1));
  if (/^(c|f|n):[\w-]+(:\d+)?$/.test(ref)) open(ref); else showGuide();
}

function setTabUI(){ document.querySelectorAll('nav a[data-tab]').forEach(a => a.classList.toggle('on', a.dataset.tab === tab)); }

function showGuide(fromNav){
  if (fromNav) document.querySelectorAll('nav a[data-tab]').forEach(a => a.classList.toggle('on', a.dataset.tab === 'help'));
  $('#reader').innerHTML = `<div class="page guide">${T.guide(STATS)}</div>`;
}

async function showTab(t){
  tab = t; setTabUI(); hl = null;
  if (t === 'chats') return renderChats();
  if (t === 'files') return renderFiles();
  if (t === 'arts') return renderArts();
  if (t === 'basket') return renderBasket();
  const kind = {memory: 'memory', projects: 'project', routines: 'routine'}[t];
  const rows = cache[kind] ||= await api('/api/notes?kind=' + kind);
  $('#list').innerHTML = `<div id="tools"><input type="text" id="flt" placeholder="${T.f_title}"></div><div id="rows"></div>`;
  const draw = () => {
    const f = $('#flt').value.toLowerCase();
    $('#rows').innerHTML = rows.filter(r => noteTitle(r.title).toLowerCase().includes(f)).map(r =>
      `<div class="item" data-ref="n:${r.id}"><div><div class="t">${esc(noteTitle(r.title))}</div><div class="m">${T.chars(thous(r.size))}${r.children ? ' · ' + T.filesN(r.children) : ''}</div></div>${addBtn('n:' + r.id, noteTitle(r.title))}</div>`).join('') || `<div class="empty">${T.empty}</div>`;
    syncBasketUI();
  };
  $('#flt').oninput = draw; draw();
}

async function renderChats(){
  const rows = cache.convs ||= await api('/api/convs');
  const months = [...new Set(rows.map(r => (r.created || '').slice(0, 7)).filter(Boolean))].sort().reverse();
  const years = [...new Set(months.map(m => m.slice(0, 4)))];
  $('#list').innerHTML = `<div id="tools"><input type="text" id="flt" placeholder="${T.f_chat}">
    <select id="csort"><option value="updated">${T.cs_upd}</option><option value="chars">${T.cs_len}</option><option value="created">${T.cs_new}</option></select>
    <select id="period"><option value="">${T.p_all}</option><option value="d30">${T.p_30}</option><option value="d90">${T.p_90}</option>
      <optgroup label="${T.p_year}">${years.map(y => `<option value="${y}">${y}</option>`).join('')}</optgroup>
      <optgroup label="${T.p_month}">${months.map(m => `<option value="${m}">${T.MON1[+m.slice(5) - 1]} ${m.slice(0, 4)}</option>`).join('')}</optgroup></select>
    <select id="ctype"><option value="">${T.t_any}</option><option value="web">${T.t_web}</option><option value="nfiles">${T.t_files}</option><option value="att">${T.t_att}</option></select>
    <label class="tog"><input type="checkbox" id="short">${T.short}</label></div><div id="rows"></div>`;
  const draw = () => {
    const f = $('#flt').value.toLowerCase(), s = $('#csort').value, sh = $('#short').checked, ty = $('#ctype').value, pr = $('#period').value;
    ['period', 'ctype'].forEach(id => $('#' + id).classList.toggle('set', !!$('#' + id).value));
    const since = pr[0] === 'd' ? new Date(Date.now() - (+pr.slice(1)) * 864e5).toISOString() : null;
    const inPeriod = r => !pr || (since ? (r.updated || '') >= since : (r.created || '').startsWith(pr));
    const list = rows.filter(r => (sh || r.n > 2) && r.title.toLowerCase().includes(f) && (!ty || r[ty] > 0) && inPeriod(r))
      .sort((a, b) => s === 'chars' ? b.chars - a.chars : (b[s] || '').localeCompare(a[s] || ''));
    $('#rows').innerHTML = `<div class="count">${T.of(list.length, rows.length)}</div>` + list.map(r =>
      `<div class="item" data-ref="c:${r.id}"><div><div class="t">${esc(r.title)}</div><div class="m">${day(s === 'created' ? r.created : r.updated)} · ${T.msgs(r.n)} · ${T.chars(thous(r.chars))}${r.nfiles ? ' · ' + T.filesN(r.nfiles) : ''}</div></div>${addBtn('c:' + r.id, r.title)}</div>`).join('');
    syncBasketUI();
  };
  ['flt', 'csort', 'ctype', 'short', 'period'].forEach(id => $('#' + id).oninput = draw); draw();
}

async function renderFiles(){
  const rows = cache.files ||= await api('/api/files');
  $('#list').innerHTML = `<div id="tools"><input type="text" id="flt" placeholder="${T.f_file}">
    <label class="tog" title="${T.drafts_tip}"><input type="checkbox" id="drafts">${T.drafts}</label></div><div id="rows"></div>`;
  const draw = () => {
    const f = $('#flt').value.toLowerCase(), d = $('#drafts').checked;
    const list = rows.filter(r => (d || r.delivered) && (r.name + ' ' + (r.conv_title || '')).toLowerCase().includes(f));
    $('#rows').innerHTML = `<div class="count">${T.nfiles(list.length)}</div>` + list.map(r =>
      `<div class="item" data-ref="f:${r.id}"><div><div class="t">${esc(r.name)}</div><div class="m">${day(r.created)} · ${T.chars(thous(r.size))} · ${esc(r.conv_title || '')}</div></div>${addBtn('f:' + r.id, r.name)}</div>`).join('');
    syncBasketUI();
  };
  ['flt', 'drafts'].forEach(id => $('#' + id).oninput = draw); draw();
}

async function renderArts(){
  const rows = cache.arts ||= await api('/api/arts');
  $('#list').innerHTML = `<div id="tools"><input type="text" id="flt" placeholder="${T.f_any}"></div><div id="rows"></div>`;
  const draw = () => {
    const f = $('#flt').value.toLowerCase();
    $('#rows').innerHTML = rows.filter(r => (r.title + r.descr).toLowerCase().includes(f)).map(r =>
      `<div class="item" data-ref="a:${r.id}"><div><div class="t">${esc(r.title)}</div><div class="m">${day(r.created)} · ${T.versions(r.versions.length)}</div><div class="s">${esc(r.descr)}</div></div></div>`).join('') || `<div class="empty">${T.empty}</div>`;
  };
  $('#flt').oninput = draw; draw();
}

const bkind = ref => ref[0] === 'c' ? (ref.split(':').length > 2 ? T.bk.msg : T.bk.chat) : ref[0] === 'f' ? T.bk.file : T.bk.note;
function renderBasket(){
  $('#list').innerHTML = `<div id="tools"><span>${basket.length ? T.b_order : T.b_empty}</span>${basket.length ? `<button class="btn" id="clear">${T.b_clear}</button>` : ''}</div><div id="rows">`
    + basket.map(b => `<div class="item" data-ref="${esc(b.ref)}"><div><div class="t"><span class="kind">${bkind(b.ref)}</span>${esc(b.title)}</div></div><button class="add in" data-add="${esc(b.ref)}">✓</button></div>`).join('') + '</div>';
  if ($('#clear')) $('#clear').onclick = () => { basket = []; saveBasket(); renderBasket(); };
  $('#rows').addEventListener('click', e => { if (e.target.closest('[data-add]')) setTimeout(renderBasket, 0); });
  bundlePreview();
}

async function bundle(){
  const parts = [];
  for (const b of basket) { try { parts.push(await getMd(b.ref)); } catch(e) { parts.push(`# ${b.title}\n\n*[${T.b_fail}: ${e.message}]*\n`); } }
  return T.b_head(today(), basket.length) + '\n\n---\n\n' + parts.join('\n---\n\n');
}

async function bundlePreview(){
  const R = $('#reader');
  if (!basket.length) { R.innerHTML = `<div class="empty"><b>${T.b_empty}</b>${T.b_empty_long}</div>`; return; }
  R.innerHTML = `<div class="empty">${T.collecting}</div>`;
  const text = await bundle();
  R.innerHTML = `<div class="page"><h1>${T.b_title}</h1><div class="bsum"><div><b>${basket.length}</b><span class="label">${T.b_pieces}</span></div><div><b>${thous(text.length)}</b><span class="label">${T.b_chars}</span></div><div><b>~${thous(Math.round(text.length / 3.5))}</b><span class="label">${T.b_tokens}</span></div></div>
    <div class="actions">${contBtn('data-bundle="1"')}<button class="btn" id="bcopy">${T.b_copy}</button><button class="btn" id="bsave">${T.b_dl}</button></div><div class="cont" hidden></div>
    <pre id="bpre"></pre></div>`;
  pinHead(R);
  $('#bpre').textContent = text.length > 200000 ? text.slice(0, 200000) + '\n\n' + T.b_trunc : text;
  $('#bcopy').onclick = () => copyText(text);
  $('#bsave').onclick = () => download(text, fileName(T.b_name(today())));
}

$('#list').addEventListener('click', e => {
  const it = e.target.closest('.item[data-ref]'); if (!it) return;
  document.querySelectorAll('.item.on').forEach(x => x.classList.remove('on')); it.classList.add('on');
  open(it.dataset.ref);
});

async function open(ref){
  const [k, id, idx] = ref.split(':');
  const R = $('#reader'); R.scrollTop = 0;
  if (k === 'c') {
    const d = await api('/api/conv/' + id);
    const files = Object.fromEntries(d.files.map(f => [f.path, f]));
    const t = d.conv.title;
    const mact = m => `<span class="mact"><a data-copy="c:${id}:${m.idx}">${T.copy}</a><a data-add="c:${id}:${m.idx}" data-title="${esc(t)} — #${m.idx + 1}">${T.add_s}</a></span>`;
    const msg = m => `<div class="msg ${m.sender}" id="m${m.idx}"><div class="who"><span class="label">${m.sender === 'human' ? T.me : T.claude}</span><span class="when">${dayTime(m.created)}</span>${mact(m)}</div>`
      + (m.sender === 'human' ? `<div class="bubble">` : `<div class="body prose">`) + renderBlocks(m.blocks, files) + `</div></div>`;
    const delivered = d.files.filter(f => f.delivered);
    R.innerHTML = `<div class="page"><h1>${esc(t)}</h1><div class="sub"><span>${span(d.conv.created, d.conv.updated)}</span><span>${T.msgs(d.conv.n)}</span><a href="https://claude.ai/chat/${d.conv.id}" target="_blank" rel="noopener">${T.open_ai}</a></div>`
      + actions('c:' + id, t, contBtn(`data-ref="c:${id}" data-title="${esc(t)}"`)) + '<div class="cont" hidden></div>'
      + (delivered.length ? `<div class="files"><span class="label">${T.files_lbl}</span>${delivered.map(f => `<a data-file="${f.id}">${esc(f.name)}</a>`).join('')}</div>` : '')
      + (d.conv.summary ? `<details><summary>${T.summary}</summary><div class="prose">${md(d.conv.summary)}</div></details>` : '')
      + d.msgs.map(msg).join('') + '</div>';
    R.querySelectorAll('[data-file]').forEach(a => a.onclick = () => open('f:' + a.dataset.file));
    pinHead(R); highlight(R); syncBasketUI();
    const target = idx !== undefined && document.getElementById('m' + idx);
    if (target) { target.classList.add('target'); target.scrollIntoView({block: 'start'}); }
    else { const m = R.querySelector('mark'); if (m) m.scrollIntoView({block: 'center'}); }
  } else if (k === 'f') {
    const f = await api('/api/file/' + id);
    const isHtml = /\.html?$/i.test(f.name) || /^\s*<!doctype html|^\s*<html/i.test(f.content);
    const isMd = /\.(md|markdown|txt)$/i.test(f.name) || !/\.\w+$/.test(f.name);
    R.innerHTML = `<div class="page"><h1>${esc(f.name)}</h1><div class="sub"><span>${day(f.created)}</span><span>${T.from_chat} <a data-conv="${f.conv}">${esc(f.conv_title || '')}</a></span><a href="/raw/file/${f.id}?download=1">${T.dl_file}</a>`
      + (f.failed ? `<span>${T.failed(f.failed)}</span>` : '') + `</div>`
      + actions('f:' + id, f.name)
      + (isHtml ? `<iframe src="/raw/file/${f.id}"></iframe>` : isMd ? `<div class="prose">${md(f.content)}</div>` : `<pre>${esc(f.content)}</pre>`) + '</div>';
    R.querySelector('[data-conv]').onclick = () => open('c:' + f.conv);
    pinHead(R);
    if (!isHtml) highlight(R);
    syncBasketUI();
  } else if (k === 'n') {
    const n = await api('/api/note/' + id);
    const title = noteTitle(n.title);
    R.innerHTML = `<div class="page"><h1>${esc(title)}</h1><div class="sub"><span class="label">${esc(T.kinds[n.kind] || n.kind)}</span>${n.created ? '<span>' + day(n.created) + '</span>' : ''}</div>`
      + actions('n:' + id, title)
      + (n.children.length ? `<div class="files"><span class="label">${T.proj_files}</span>${n.children.map(c => `<a data-note="${c.id}">${esc(c.title)}</a>`).join('')}</div>` : '')
      + (n.parent ? `<div class="files"><a data-note="${n.parent}">${T.back_proj}</a></div>` : '')
      + (/^---\n/.test(n.body) || n.kind !== 'project_doc' || /\.md$/i.test(n.title) ? `<div class="prose">${md(n.body)}</div>` : `<pre>${esc(n.body)}</pre>`) + '</div>';
    R.querySelectorAll('[data-note]').forEach(a => a.onclick = () => open('n:' + a.dataset.note));
    pinHead(R); highlight(R); syncBasketUI();
  } else if (k === 'a') {
    const rows = cache.arts ||= await api('/api/arts');
    const a = rows.find(r => r.id === id);
    const vers = a.versions.filter(v => v.entry);
    const src = v => `/a/${a.id}/${v.entry}`;
    R.innerHTML = `<h1>${esc(a.title)}</h1><div class="sub"><span>${esc(a.descr)}</span></div><div class="sub">
      <select id="ver">${vers.map((v, i) => `<option value="${i}">${dayTime(v.created)}${i ? '' : T.latest}</option>`).join('')}</select>
      <a id="newtab" target="_blank" rel="noopener" href="${vers[0] ? src(vers[0]) : '#'}">${T.newtab}</a></div>`
      + (vers.length ? `<iframe id="frame" src="${src(vers[0])}"></iframe>` : `<div class="empty">${T.no_vers}</div>`);
    const sel = $('#ver'); if (sel) sel.onchange = () => { $('#frame').src = $('#newtab').href = src(vers[sel.value]); };
  }
}

// The document head (title, dates, buttons) sticks to the top while scrolling and shrinks.
function pinHead(R){
  const page = R.querySelector('.page');
  const stop = page && (page.querySelector(':scope > .cont') || page.querySelector(':scope > .actions'));
  if (!stop) return;
  const head = document.createElement('div');
  head.className = 'phead';
  while (page.firstChild) { const n = page.firstChild; head.append(n); if (n === stop) break; }
  const sentinel = document.createElement('div');
  page.prepend(sentinel, head);
  new IntersectionObserver(([e]) => head.classList.toggle('stuck', !e.isIntersecting), {root: R}).observe(sentinel);
}

// Consecutive tool calls and their results fold into one line:
// long chats have dozens of them, and unfolded they bury the conversation itself.
function renderBlocks(blocks, files){
  let out = '', run = [];
  const flush = () => { if (run.length) out += toolGroup(run, files); run = []; };
  for (const b of blocks) {
    if (b.t === 'tool' || b.t === 'result') run.push(b);
    else { flush(); out += block(b); }
  }
  flush();
  return out;
}

function toolGroup(run, files){
  const tools = run.filter(b => b.t === 'tool');
  const counts = {};
  tools.forEach(b => counts[b.name] = (counts[b.name] || 0) + 1);
  const label = Object.entries(counts).map(([n, c]) => esc(n) + (c > 1 ? ' ×' + c : '')).join(', ') || T.tool_results;
  const made = [...new Map(tools.filter(b => /create_file|artifacts/.test(b.name) && files[b.path])
    .map(b => [b.path, files[b.path]])).values()];
  const body = run.map(b => b.t === 'tool'
    ? `<div class="tool" title="${esc(b.v)}"><span>${esc(b.name)}${b.v ? ' · ' + esc(b.v) : ''}</span></div>`
    : `<pre>${esc(b.v)}</pre>`).join('');
  return `<details class="tools"><summary>${label}</summary>${body}</details>`
    + (made.length ? `<div class="made">${T.made} ${made.map(f => `<a data-file="${f.id}">${esc(f.name)}</a>`).join('')}</div>` : '');
}

function block(b){
  if (b.t === 'text') return md(b.v);
  if (b.t === 'thinking') return `<details><summary>${T.thinking}</summary><pre>${esc(b.v)}</pre></details>`;
  if (b.t === 'att') return `<details><summary>${T.attachment}: ${esc(b.name)}</summary><pre>${esc(b.v)}</pre></details>`;
  if (b.t === 'file') return `<div class="tool"><span>${T.attachment}: ${esc(b.name)}</span></div>`;
  return '';
}

function highlight(root){
  if (!hl) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    if (n.parentElement.closest('.actions,.mact,.who,.phead')) continue;
    const text = n.nodeValue, norm = text.replace(/ё/g, 'е').replace(/Ё/g, 'Е');
    const rx = new RegExp(hl, 'gi'); let m, last = 0, frag = null;
    while ((m = rx.exec(norm)) && m[0].length) {
      frag ||= document.createDocumentFragment();
      frag.append(text.slice(last, m.index));
      const mk = document.createElement('mark'); mk.textContent = text.slice(m.index, m.index + m[0].length); frag.append(mk);
      last = m.index + m[0].length;
    }
    if (frag) { frag.append(text.slice(last)); n.replaceWith(frag); }
  }
}

async function runSearch(){
  const q = $('#q').value.trim(); if (!q) return showTab(tab);
  $('#list').innerHTML = `<div class="empty">${T.searching}</div>`;
  const p = new URLSearchParams({q, mode: $('#mode').value, kind: $('#kind').value, sort: $('#sort').value});
  const r = await api('/api/search?' + p);
  if (r.error) { $('#list').innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
  hl = r.pattern;
  document.querySelectorAll('nav a[data-tab]').forEach(a => a.classList.remove('on'));
  const canAdd = ref => /^(c|f|n):/.test(ref);
  $('#list').innerHTML = `<div id="tools"><span>${T.found(r.total, r.total > r.hits.length ? r.hits.length : 0)}</span><button class="btn" id="back">${T.reset}</button></div><div>`
    + r.hits.map(h => {
      const title = ['memory', 'project', 'project_doc', 'routine'].includes(h.kind) ? noteTitle(h.title) : h.title;
      return `<div class="item" data-ref="${h.ref}"><div><div class="t"><span class="kind">${T.kinds[h.kind] || h.kind}</span>${esc(title)}</div><div class="m">${day(h.created)}</div><div class="s">${h.snippet}</div></div>${canAdd(h.ref) ? addBtn(h.ref, title + (h.kind === 'msg' ? ' — ' + T.bk.msg : '')) : ''}</div>`;
    }).join('') + '</div>';
  $('#back').onclick = () => { $('#q').value = ''; showTab(tab); };
  syncBasketUI();
}
$('#go').onclick = runSearch;
$('#q').onkeydown = e => { if (e.key === 'Enter') runSearch(); };
init();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
