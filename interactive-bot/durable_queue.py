"""Durable Telegram update inbox for python-telegram-bot 21.x.

Each Telegram update is committed to SQLite before the next getUpdates request
advances the offset. A single worker processes persisted updates and retries
failed handlers with capped exponential backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path

from telegram import Update
from telegram.error import RetryAfter

log = logging.getLogger(__name__)


class DurableUpdateQueue:
    def __init__(self, db_path: str = "./assets/durable_inbox.sqlite3",
                 poll_timeout: int = 25):
        self.db_path = str(Path(db_path))
        self.poll_timeout = int(poll_timeout)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._stopping = asyncio.Event()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS inbox (
                    update_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    received_at REAL NOT NULL,
                    processed_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_offset INTEGER
                )
            """)
            conn.execute(
                "INSERT OR IGNORE INTO queue_state(id, next_offset) VALUES(1, NULL)"
            )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_inbox_ready
                ON inbox(status, next_retry_at, lease_until)
            """)

    def _get_offset(self):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT next_offset FROM queue_state WHERE id=1"
            ).fetchone()
            return row["next_offset"] if row else None

    def _persist_batch(self, updates):
        if not updates:
            return
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            max_id = None
            for update in updates:
                update_id = int(update.update_id)
                payload = json.dumps(
                    update.to_dict(), ensure_ascii=False, separators=(",", ":")
                )
                conn.execute(
                    """INSERT OR IGNORE INTO inbox
                       (update_id, payload, status, received_at)
                       VALUES (?, ?, 'pending', ?)""",
                    (update_id, payload, now),
                )
                max_id = update_id if max_id is None else max(max_id, update_id)
            if max_id is not None:
                # Inbox rows and next offset are committed in one transaction.
                conn.execute(
                    "UPDATE queue_state SET next_offset=? WHERE id=1",
                    (max_id + 1,),
                )
            conn.commit()

    def _claim_one(self):
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT update_id, payload, attempts
                   FROM inbox
                   WHERE (status='pending' AND next_retry_at <= ?)
                      OR (status='processing' AND lease_until <= ?)
                   ORDER BY update_id LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """UPDATE inbox
                   SET status='processing', attempts=attempts+1, lease_until=?
                   WHERE update_id=?""",
                (now + 300, row["update_id"]),
            )
            conn.commit()
            return {
                "update_id": row["update_id"],
                "payload": row["payload"],
                "attempts": row["attempts"] + 1,
            }

    def _mark_done(self, update_id: int):
        with self._connect() as conn:
            conn.execute(
                """UPDATE inbox SET status='done', processed_at=?,
                   lease_until=0, last_error=NULL WHERE update_id=?""",
                (time.time(), update_id),
            )

    def _mark_retry(self, update_id: int, attempts: int, error: str, delay=None):
        # Keep retrying rather than silently discarding a failed update.
        backoff = min(5 * (2 ** max(0, attempts - 1)), 300)
        if delay is not None:
            backoff = max(backoff, min(float(delay), 3600))
        with self._connect() as conn:
            conn.execute(
                """UPDATE inbox SET status='pending', next_retry_at=?,
                   lease_until=0, last_error=? WHERE update_id=?""",
                (time.time() + backoff, str(error)[:4000], update_id),
            )
        log.error(
            "update_id=%s processing failed; retry in %.1fs: %s",
            update_id, backoff, error,
        )

    async def _poll_loop(self, application):
        bot = application.bot
        while not self._stopping.is_set():
            try:
                offset = self._get_offset()
                updates = await bot.get_updates(
                    offset=offset,
                    timeout=self.poll_timeout,
                    read_timeout=self.poll_timeout + 15,
                    connect_timeout=15,
                    write_timeout=15,
                    pool_timeout=15,
                    allowed_updates=Update.ALL_TYPES,
                )
                # Do not advance Telegram's offset until this batch is safely stored.
                self._persist_batch(updates)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Telegram polling failed; retrying in 5 seconds.")
                await asyncio.sleep(5)

    async def _worker_loop(self, application):
        bot = application.bot
        failed_ids = application.bot_data.setdefault(
            "_durable_failed_update_ids", set()
        )
        while not self._stopping.is_set():
            item = self._claim_one()
            if item is None:
                await asyncio.sleep(0.5)
                continue

            update_id = item["update_id"]
            failed_ids.discard(update_id)
            try:
                update = Update.de_json(json.loads(item["payload"]), bot)
                await application.process_update(update)

                # PTB routes handler exceptions to error handlers; the wrapper
                # error handler sets this marker so the inbox can retry the row.
                if update_id in failed_ids:
                    failed_ids.discard(update_id)
                    self._mark_retry(
                        update_id, item["attempts"],
                        "A handler raised an exception; inspect application logs."
                    )
                else:
                    self._mark_done(update_id)
            except asyncio.CancelledError:
                # Leave as processing; the lease expires after restart.
                raise
            except Exception as exc:
                retry_after = (
                    exc.retry_after if isinstance(exc, RetryAfter) else None
                )
                self._mark_retry(
                    update_id, item["attempts"], repr(exc), retry_after
                )

    async def run(self, application):
        poller = asyncio.create_task(
            self._poll_loop(application), name="telegram-durable-poller"
        )
        worker = asyncio.create_task(
            self._worker_loop(application), name="telegram-inbox-worker"
        )
        try:
            await asyncio.gather(poller, worker)
        finally:
            self._stopping.set()
            poller.cancel()
            worker.cancel()
            await asyncio.gather(poller, worker, return_exceptions=True)
