import sqlite3
import unittest
from datetime import datetime, timezone


class TestBatchOperationsDB(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row

        # Setup schema
        self.db.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                active INTEGER NOT NULL DEFAULT 1,
                qps_limit INTEGER NOT NULL DEFAULT 2,
                credits INTEGER DEFAULT 100,
                created_at TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                key TEXT UNIQUE NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                qps_limit INTEGER,
                last_used_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE platform_settings (
                platform TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                qps_limit INTEGER,
                updated_at TEXT
            )
        """)
        self.db.execute("""
            CREATE TABLE request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                api_key_id INTEGER,
                platform TEXT,
                input_url TEXT,
                status_code INTEGER,
                duration_ms INTEGER,
                created_at TEXT
            )
        """)

        # Insert test users
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.db.execute("INSERT INTO users (username, password_hash, role, active, created_at) VALUES ('admin', 'hash', 'admin', 1, ?)", (now,))
        for i in range(1, 6):
            self.db.execute(
                "INSERT INTO users (username, password_hash, role, active, qps_limit, credits, created_at) VALUES (?, 'hash', 'user', 1, 2, 100, ?)",
                (f"user{i}", now),
            )

        # 2026-09-22：原先这里插 5 行 api_keys 供 `test_batch_keys_disable_and_qps` 用。
        # 密钥通道撤销后那条测试删了，插入也一并删掉 —— 留着是"造数据给没人读的表看"。
        # `CREATE TABLE api_keys` 保留：本 fixture 是照着真实 schema 搭的，表在真实库里也还在
        #（历史日志的 api_key_id 指着它），留空表比留一张不存在的表更贴近真身。

        # Insert test logs
        for i in range(1, 10):
            self.db.execute(
                "INSERT INTO request_logs (user_id, platform, status_code, duration_ms, created_at) VALUES (?, 'douyin', 200, 150, ?)",
                (2, now),
            )

        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_batch_users_update_active(self):
        user_ids = [2, 3, 4]
        placeholders = ",".join(["?"] * len(user_ids))
        self.db.execute(f"UPDATE users SET active=0 WHERE id IN ({placeholders}) AND role!='admin'", user_ids)
        self.db.commit()

        disabled_count = self.db.execute("SELECT COUNT(*) FROM users WHERE active=0").fetchone()[0]
        self.assertEqual(disabled_count, 3)

    def test_batch_users_adjust_credits(self):
        user_ids = [2, 3]
        placeholders = ",".join(["?"] * len(user_ids))
        self.db.execute(f"UPDATE users SET credits=credits+50 WHERE id IN ({placeholders}) AND role!='admin'", user_ids)
        self.db.commit()

        res = self.db.execute("SELECT credits FROM users WHERE id IN (2, 3)").fetchall()
        for r in res:
            self.assertEqual(r["credits"], 150)

    def test_batch_users_protection_on_admin(self):
        # Trying to delete user IDs 1 (admin) and 2 (user)
        user_ids = [1, 2]
        placeholders = ",".join(["?"] * len(user_ids))
        self.db.execute(f"DELETE FROM users WHERE id IN ({placeholders}) AND role!='admin'", user_ids)
        self.db.commit()

        admin_exists = self.db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        self.assertEqual(admin_exists, 1)  # Admin should NOT be deleted!

    def test_batch_logs_delete(self):
        log_ids = [1, 2, 3]
        placeholders = ",".join(["?"] * len(log_ids))
        self.db.execute(f"DELETE FROM request_logs WHERE id IN ({placeholders})", log_ids)
        self.db.commit()

        remaining_logs = self.db.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
        self.assertEqual(remaining_logs, 6)


if __name__ == "__main__":
    unittest.main()
