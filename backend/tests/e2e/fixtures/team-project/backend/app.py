"""端到端模型夹具交付的工单服务。"""

import json
import os
import re
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def database():
    db = sqlite3.connect(os.environ.get("TICKET_DB", "tickets.db"))
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL)")
    return db


class Tickets(BaseHTTPRequestHandler):
    def reply(self, status, data):
        content = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def input_data(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 4096:
            raise ValueError("请求内容超出限制")
        value = json.loads(self.rfile.read(size))
        if not isinstance(value, dict):
            raise ValueError("需要 JSON 对象")
        return value

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/api/tickets":
            self.reply(404, {"error": "接口不存在"})
            return
        query = parse_qs(url.query)
        search = query.get("q", [""])[0].strip()[:80]
        status = query.get("status", [""])[0]
        with closing(database()) as db:
            rows = db.execute("SELECT * FROM tickets WHERE instr(title, ?) > 0 AND (? = '' OR status = ?) ORDER BY id DESC",
                              (search, status, status)).fetchall() if search else db.execute(
                "SELECT * FROM tickets WHERE (? = '' OR status = ?) ORDER BY id DESC", (status, status),
            ).fetchall()
        self.reply(200, {"items": [dict(row) for row in rows]})

    def do_POST(self):
        if self.path != "/api/tickets":
            self.reply(404, {"error": "接口不存在"})
            return
        try:
            title = self.input_data().get("title", "")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
                raise ValueError("请输入 1 至 120 个字符的工单标题")
        except ValueError as error:
            self.reply(422, {"error": str(error)})
            return
        with closing(database()) as db, db:
            row = db.execute("INSERT INTO tickets(title, status) VALUES (?, 'open')", (title.strip(),))
        self.reply(201, {"id": row.lastrowid, "title": title.strip(), "status": "open"})

    def do_PATCH(self):
        match = re.fullmatch(r"/api/tickets/(\d+)", self.path)
        if not match:
            self.reply(404, {"error": "工单不存在"})
            return
        try:
            status = self.input_data().get("status")
            if status not in ("open", "done"):
                raise ValueError("工单状态无效")
        except ValueError as error:
            self.reply(422, {"error": str(error)})
            return
        with closing(database()) as db, db:
            changed = db.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, int(match[1])))
        self.reply(200, {"status": status}) if changed.rowcount else self.reply(404, {"error": "工单不存在"})

    def do_DELETE(self):
        match = re.fullmatch(r"/api/tickets/(\d+)", self.path)
        if not match:
            self.reply(404, {"error": "工单不存在"})
            return
        with closing(database()) as db, db:
            deleted = db.execute("DELETE FROM tickets WHERE id = ?", (int(match[1]),))
        self.reply(200, {"deleted": True}) if deleted.rowcount else self.reply(404, {"error": "工单不存在"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8765"))), Tickets).serve_forever()
