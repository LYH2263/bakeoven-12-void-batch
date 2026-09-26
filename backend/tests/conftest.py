import os
import tempfile

# 在导入 app 之前把数据库切到临时 SQLite，避免依赖 Postgres。
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
