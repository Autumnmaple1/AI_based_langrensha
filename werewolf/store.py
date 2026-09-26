"""Durable audit journal. Interrupted matches are aborted, never blindly resumed."""
import sqlite3
from .protocol import dumps, utc


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, updated TEXT NOT NULL, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, game_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS client_ids(agent_id TEXT, message_id TEXT, digest TEXT NOT NULL, PRIMARY KEY(agent_id,message_id));
        """)

    def commit(self, game_id, snapshot, kind, body):
        with self.db:
            if snapshot is not None:
                self.db.execute("INSERT OR REPLACE INTO games VALUES(?,?,?)", (game_id, utc(), dumps(snapshot)))
            self.db.execute("INSERT INTO audit(game_id,at,kind,body) VALUES(?,?,?,?)", (game_id, utc(), kind, dumps(body)))

    def latest(self):
        import json
        row = self.db.execute("SELECT snapshot FROM games ORDER BY updated DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def check_message_id(self, agent_id, message_id, canonical):
        import hashlib
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        row = self.db.execute("SELECT digest FROM client_ids WHERE agent_id=? AND message_id=?", (agent_id, message_id)).fetchone()
        if row:
            return row[0] == digest
        with self.db:
            self.db.execute("INSERT INTO client_ids VALUES(?,?,?)", (agent_id, message_id, digest))
        return True

    def replay(self, game_id):
        import json
        row = self.db.execute("SELECT snapshot FROM games WHERE id=?", (game_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_games(self):
        import json
        return [dict(game_id=g, updated=u, result=json.loads(s)["game"]["result"])
                for g, u, s in self.db.execute("SELECT id,updated,snapshot FROM games ORDER BY updated DESC LIMIT 100")]

    def close(self):
        self.db.close()
