
"""Durable Telegram update inbox for python-telegram-bot 21.x.

Features:
- Persist Telegram updates into SQLite before advancing Telegram offset.
- Resume pending updates after process restart.
- Retry failed updates with exponential backoff.
- Use a single worker to process updates in order.
- Compatible with both __main__.py and __main2__.py.

Important:
- One Telegram Bot Token must have only one polling process.
- Each Bot instance must use its own SQLite database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time

from pathlib import Path
from datetime import timedelta

from telegram import Update
from telegram.error import RetryAfter


log = logging.getLogger(__name__)


class DurableUpdateQueue:

    def __init__(
        self,
        db_path: str = "./assets/durable_inbox.sqlite3",
        poll_timeout: int = 25,
        lease_seconds: int = 300,
    ):
        self.db_path = str(Path(db_path))
        self.poll_timeout = int(poll_timeout)
        self.lease_seconds = int(lease_seconds)

        self._stopping = asyncio.Event()

        Path(self.db_path).parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._init_db()

    # ==========================================
    # SQLite
    # ==========================================

    def _connect(self):

        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
        )

        conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")

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

            conn.execute("""
                INSERT OR IGNORE INTO queue_state
                (id, next_offset)
                VALUES (1, NULL)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_inbox_ready

                ON inbox (
                    status,
                    next_retry_at,
                    lease_until
                )
            """)

    # ==========================================
    # Telegram Offset
    # ==========================================

    def _get_offset(self):

        with self._connect() as conn:

            row = conn.execute("""
                SELECT next_offset

                FROM queue_state

                WHERE id = 1
            """).fetchone()

            return row["next_offset"] if row else None

    # ==========================================
    # Persist Telegram Updates
    # ==========================================

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
                    update.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

                conn.execute("""
                    INSERT OR IGNORE INTO inbox
                    (
                        update_id,
                        payload,
                        status,
                        received_at
                    )

                    VALUES (?, ?, 'pending', ?)
                """, (
                    update_id,
                    payload,
                    now,
                ))

                if max_id is None:
                    max_id = update_id
                else:
                    max_id = max(max_id, update_id)

            if max_id is not None:

                # Update 数据与 Telegram offset
                # 必须在同一个事务中提交。

                conn.execute("""
                    UPDATE queue_state

                    SET next_offset = ?

                    WHERE id = 1
                """, (
                    max_id + 1,
                ))

            conn.commit()

        log.debug(
            "Persisted %s Telegram updates.",
            len(updates),
        )

    # ==========================================
    # Claim Pending Update
    # ==========================================

    def _claim_one(self):

        now = time.time()

        with self._connect() as conn:

            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute("""
                SELECT
                    update_id,
                    payload,
                    attempts

                FROM inbox

                WHERE
                    (
                        status = 'pending'
                        AND next_retry_at <= ?
                    )

                    OR

                    (
                        status = 'processing'
                        AND lease_until <= ?
                    )

                ORDER BY update_id

                LIMIT 1
            """, (
                now,
                now,
            )).fetchone()

            if row is None:

                conn.commit()

                return None

            conn.execute("""
                UPDATE inbox

                SET
                    status = 'processing',

                    attempts = attempts + 1,

                    lease_until = ?

                WHERE update_id = ?
            """, (
                now + self.lease_seconds,
                row["update_id"],
            ))

            conn.commit()

            return {
                "update_id": row["update_id"],

                "payload": row["payload"],

                "attempts": row["attempts"] + 1,
            }

    # ==========================================
    # Mark Update Successful
    # ==========================================

    def _mark_done(self, update_id: int):

        with self._connect() as conn:

            conn.execute("""
                UPDATE inbox

                SET
                    status = 'done',

                    processed_at = ?,

                    lease_until = 0,

                    last_error = NULL

                WHERE update_id = ?
            """, (
                time.time(),
                update_id,
            ))

    # ==========================================
    # Mark Update Failed And Retry
    # ==========================================

    def _mark_retry(
        self,
        update_id: int,
        attempts: int,
        error: str,
        delay=None,
    ):

        # 指数退避：
        # 5秒、10秒、20秒、40秒……
        # 最大300秒。

        backoff = min(
            5 * (2 ** max(0, attempts - 1)),
            300,
        )

        if delay is not None:

            if isinstance(delay, timedelta):
                delay = delay.total_seconds()

            backoff = max(
                backoff,
                min(float(delay), 3600),
            )

        with self._connect() as conn:

            conn.execute("""
                UPDATE inbox

                SET
                    status = 'pending',

                    next_retry_at = ?,

                    lease_until = 0,

                    last_error = ?

                WHERE update_id = ?
            """, (
                time.time() + backoff,
                str(error)[:4000],
                update_id,
            ))

        log.error(
            "Update %s failed. Retry in %.1f seconds. Error: %s",
            update_id,
            backoff,
            error,
        )

    # ==========================================
    # Telegram Polling
    # ==========================================

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

                # 只有成功写入 SQLite 后，
                # 才会推进下次 Telegram offset。

                self._persist_batch(updates)

            except asyncio.CancelledError:

                raise

            except Exception:

                log.exception(
                    "Telegram polling failed. "
                    "Retrying in 5 seconds."
                )

                await asyncio.sleep(5)

    # ==========================================
    # Update Worker
    # ==========================================

    async def _worker_loop(self, application):

        bot = application.bot

        failed_ids = application.bot_data.setdefault(
            "_durable_failed_update_ids",
            set(),
        )

        while not self._stopping.is_set():

            item = self._claim_one()

            if item is None:

                await asyncio.sleep(0.5)

                continue

            update_id = item["update_id"]

            # 清理上一次可能遗留的失败标记。

            failed_ids.discard(update_id)

            try:

                update = Update.de_json(
                    json.loads(item["payload"]),
                    bot,
                )

                await application.process_update(update)

                # PTB 会把 Handler 异常交给 error_handler。
                # error_handler 通过 failed_ids 通知队列。

                if update_id in failed_ids:

                    failed_ids.discard(update_id)

                    self._mark_retry(
                        update_id,
                        item["attempts"],
                        "Handler raised an exception.",
                    )

                else:

                    self._mark_done(update_id)

            except asyncio.CancelledError:

                # 不标记完成。
                # 进程重启后 lease 到期会重新处理。

                raise

            except Exception as exc:

                retry_after = None

                if isinstance(exc, RetryAfter):

                    retry_after = exc.retry_after

                self._mark_retry(
                    update_id,
                    item["attempts"],
                    repr(exc),
                    retry_after,
                )

    # ==========================================
    # Run Queue
    # ==========================================

    async def run(self, application):

        poller = asyncio.create_task(
            self._poll_loop(application),
            name="telegram-durable-poller",
        )

        worker = asyncio.create_task(
            self._worker_loop(application),
            name="telegram-inbox-worker",
        )

        try:

            await asyncio.gather(
                poller,
                worker,
            )

        finally:

            self._stopping.set()

            poller.cancel()
            worker.cancel()

            await asyncio.gather(
                poller,
                worker,
                return_exceptions=True,
            )


# ==========================================
# Synchronous Application Startup
# ==========================================

def start_durable_polling(
    application,
    db_path="./assets/durable_inbox.sqlite3",
):

    async def runner():

        queue = DurableUpdateQueue(
            db_path=db_path,
        )

        initialized = False

        try:

            await application.initialize()

            initialized = True

            # 使用 getUpdates 轮询前，
            # 删除可能存在的 Webhook。
            # 不丢弃 Telegram 尚未处理的更新。

            await application.bot.delete_webhook(
                drop_pending_updates=False,
            )

            await application.start()

            log.info(
                "Durable Telegram polling started. Database: %s",
                db_path,
            )

            await queue.run(application)

        finally:

            try:

                if application.running:

                    await application.stop()

            finally:

                if initialized:

                    await application.shutdown()

    try:

        asyncio.run(runner())

    except KeyboardInterrupt:

        log.info(
            "Durable Telegram polling stopped by user."
        )
