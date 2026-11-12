"""Durable, per-identity idempotency keys for Buzz message sends."""
import asyncio
import hashlib
import json
import os
import sqlite3
import time


class MessageReceipts:
    def __init__(self, path=None):
        if path is not None:
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            if path.stat().st_mode & 0o077:
                raise RuntimeError("Message receipts must have permissions 600")
        self.db = sqlite3.connect(str(path) if path is not None else ":memory:")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created REAL NOT NULL)")
        self.db.commit()
        self.lock = asyncio.Lock()

    async def send_once(self, agent_id, key, request, send):
        receipt_key = hashlib.sha256(json.dumps([agent_id, key]).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.lock:
            row = self.db.execute("SELECT digest, status, result FROM receipts WHERE key=?", (receipt_key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("Idempotency key has already been used; do not send another reply for this message")
                if row[1] == "done":
                    return {**json.loads(row[2]), "deduplicated": True}
                raise ValueError("This idempotency key is in progress or its prior send outcome is unknown; read Buzz before retrying, and do not invent a new key")
            # Claim before sending. A crash or lost relay response never causes
            # an automatic second post under the same key.
            self.db.execute("INSERT INTO receipts VALUES (?, ?, 'pending', NULL, ?)", (receipt_key, digest, time.time()))
            self.db.commit()
        try:
            result = await send()
        except BaseException:
            async with self.lock:
                self.db.execute("UPDATE receipts SET status='uncertain' WHERE key=?", (receipt_key,))
                self.db.commit()
            raise
        async with self.lock:
            self.db.execute("UPDATE receipts SET status='done', result=? WHERE key=?", (json.dumps(result), receipt_key))
            self.db.commit()
        return {**result, "deduplicated": False}
