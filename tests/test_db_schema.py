"""init_db() 建出来的表结构，以及「保留但不再使用」的表还在不在。

2026-09-22：本文件原名 `test_raw_key_feature.py`，装着一个明文密钥格式的测试
（`generate_api_key()` 生成 mp- + 24 位）。API Key 通道撤销后那个函数已删，
文件名也就成了假话，故改名并把那条测试删掉，只留下面这条 schema 断言。

为什么留着断言 `api_keys`？
    撤销的是**代码路径**，不是**数据**。`request_logs` 里 1641 行历史记录的
    `api_key_id` 指着这张表，删表会让历史日志失去归属，所以表必须还在。
    这条断言就是「表还在」的守卫：哪天有人顺手把建表语句清掉，
    这里会红，而不是等到查历史日志时才发现 join 不上。
"""
import os
import sqlite3
import tempfile
import unittest

from app import create_app
from src.db import init_db


class TestDatabaseSchema(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path, "SECRET_KEY": "test_secret"})
        self.client = self.app.test_client()

    def tearDown(self):
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_db_init_schema(self):
        """init_db() 建出的 users 有 credits 列；api_keys 表保留可用（历史日志要 join 它）。"""
        with self.app.app_context():
            init_db()

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
            key_cols = [r[1] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()]
            self.assertIn("credits", user_cols)
            self.assertIn("key", key_cols)
            conn.close()


if __name__ == "__main__":
    unittest.main()
