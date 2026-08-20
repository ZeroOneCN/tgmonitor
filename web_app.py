#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 多账号监控工具 - Web 管理后台 (FastAPI)
支持多账号同时监控，浏览器管理，实时日志推送
"""

import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
import hashlib
import secrets
import shutil
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.utils import get_display_name
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_DB_PATH = BASE_DIR / "history.db"
SHANGHAI_TZ = timezone(timedelta(hours=8))
SESSION_SECRET = secrets.token_hex(32)


def default_config() -> dict:
    return {
        "version": 3,
        "admin": {
            "username": "admin",
            "password_hash": "",
            "password_salt": "",
        },
        "accounts": [],
        "webhooks": [
            {
                "enabled": False,
                "name": "",
                "platform": "",
                "telegram_bot_token": "",
                "telegram_chat_id": "",
                "url": "",
            }
        ],
        "cleanup": {
            "enabled": False,
            "keep_days": 30,
        },
        "rule_templates": [],
    }


class ConfigManager:
    def __init__(self):
        self.cfg = default_config()
        self.load()

    def load(self):
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.cfg = json.load(f)
                if "accounts" not in self.cfg:
                    old = self.cfg
                    self.cfg = default_config()
                    self.cfg["webhooks"] = old.get("webhooks", self.cfg["webhooks"])
                    self.cfg["admin"] = old.get("admin", self.cfg["admin"])
                    if old.get("api_id") and old.get("phone"):
                        self.cfg["accounts"].append({
                            "remark": "账号1",
                            "phone": old["phone"],
                            "api_id": old["api_id"],
                            "api_hash": old.get("api_hash", ""),
                            "proxy": old.get("proxy", {"scheme": "", "host": "", "port": 0}),
                            "rules": [
                                {
                                    "remark": t.get("remark", "规则"),
                                    "target_user_ids": t.get("target_user_ids", []),
                                    "target_usernames": t.get("target_usernames", []),
                                    "chat_ids": t.get("chat_ids", []),
                                    "chat_titles": t.get("chat_titles", []),
                                    "keywords_include": t.get("keywords_include", []),
                                    "keywords_exclude": t.get("keywords_exclude", []),
                                    "forward_to_saved": t.get("forward_to_saved", True),
                                    "forward_to_chats": t.get("forward_to_chats", []),
                                    }
                                for t in (old.get("monitor_targets") or [])
                            ],
                        })
                elif "admin" not in self.cfg:
                    self.cfg["admin"] = default_config()["admin"]
            except Exception:
                self.cfg = default_config()

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)

    def get_accounts(self):
        return self.cfg.get("accounts", [])

    def add_account(self, account: dict):
        self.cfg["accounts"].append(account)
        self.save()

    def update_account(self, idx: int, account: dict):
        self.cfg["accounts"][idx] = account
        self.save()

    def delete_account(self, idx: int):
        if 0 <= idx < len(self.cfg["accounts"]):
            del self.cfg["accounts"][idx]
            self.save()

    def get_webhooks(self):
        return self.cfg.get("webhooks", [])

    def set_webhooks(self, webhooks: list):
        self.cfg["webhooks"] = webhooks
        self.save()

    def get_admin(self):
        return self.cfg.get("admin", {})

    def get_cleanup(self) -> dict:
        return self.cfg.get("cleanup", {"enabled": False, "keep_days": 30})

    def set_cleanup(self, cleanup: dict):
        self.cfg["cleanup"] = cleanup
        self.save()

    def get_rule_templates(self) -> list:
        return self.cfg.setdefault("rule_templates", [])

    def set_rule_templates(self, templates: list):
        self.cfg["rule_templates"] = templates
        self.save()

    def set_admin_password(self, username: str, password: str):
        salt = secrets.token_hex(16)
        pwd_hash = hashlib.sha256((password + salt).encode()).hexdigest()
        self.cfg["admin"] = {"username": username, "password_hash": pwd_hash, "password_salt": salt}
        self.save()

    def verify_admin(self, username: str, password: str) -> bool:
        admin = self.get_admin()
        if not admin.get("password_hash") or not admin.get("password_salt"):
            return False
        pwd_hash = hashlib.sha256((password + admin["password_salt"]).encode()).hexdigest()
        return username == admin.get("username", "") and pwd_hash == admin["password_hash"]

    def is_admin_configured(self) -> bool:
        admin = self.get_admin()
        return bool(admin.get("password_hash"))


config = ConfigManager()


# ============================================================
# 用户认证
# ============================================================
USERS_DB_PATH = BASE_DIR / "users.db"


# ============================================================
# 多租户数据层：users / sessions（每用户独立账号/规则/通知/模板）
# ============================================================
def init_users_db():
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(USERS_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            accounts TEXT NOT NULL DEFAULT '[]',
            webhooks TEXT NOT NULL DEFAULT '[]',
            cleanup TEXT NOT NULL DEFAULT '{}',
            rule_templates TEXT NOT NULL DEFAULT '[]',
            banned INTEGER DEFAULT 0,
            ban_reason TEXT DEFAULT '',
            banned_at TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    # 旧库迁移：补充封禁相关字段
    try:
        ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        for col, ddl in [
            ("banned", "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0"),
            ("ban_reason", "ALTER TABLE users ADD COLUMN ban_reason TEXT DEFAULT ''"),
            ("banned_at", "ALTER TABLE users ADD COLUMN banned_at TEXT DEFAULT ''"),
        ]:
            if col not in ucols:
                conn.execute(ddl)
    except Exception:
        pass
    conn.commit()
    conn.close()
    migrate_legacy_config()


def migrate_legacy_config():
    """首次使用：把历史 config.json 里的管理员/账号/通知迁移到 users 表。
    若 users 表为空且 config.json 已配置过账号，则创建首个 admin 用户承载这些数据。"""
    conn = sqlite3.connect(str(USERS_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if cnt > 0:
            return
        admin = config.get_admin()
        accounts = config.get_accounts()
        if not admin.get("password_hash") and not accounts:
            return  # 全新安装，走后端 setup 流程
        username = admin.get("username") or "admin"
        pwd_hash = admin.get("password_hash") or ""
        pwd_salt = admin.get("password_salt") or ""
        if not pwd_hash:
            # 历史无管理员但有账号：生成一个默认口令 "admin123"
            salt = secrets.token_hex(16)
            pwd_hash = hashlib.sha256(("admin123" + salt).encode()).hexdigest()
            pwd_salt = salt
        conn.execute(
            "INSERT INTO users (username, password_hash, password_salt, role, accounts, webhooks, cleanup, rule_templates) "
            "VALUES (?, ?, ?, 'admin', ?, ?, ?, ?)",
            (username, pwd_hash, pwd_salt,
             json.dumps(accounts, ensure_ascii=False),
             json.dumps(config.get_webhooks(), ensure_ascii=False),
             json.dumps(config.get_cleanup(), ensure_ascii=False),
             json.dumps(config.get_rule_templates(), ensure_ascii=False)),
        )
        admin_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
        # 历史 history 数据归属到新 admin 用户
        try:
            hist = sqlite3.connect(str(HISTORY_DB_PATH))
            hist.execute("UPDATE history SET user_id = ? WHERE user_id = 0", (admin_id,))
            hist.commit()
            hist.close()
        except Exception:
            pass
        conn.commit()
        logger.info(f"[多租户] 已将历史 config.json 迁移到 admin 用户（{username}，含 {len(accounts)} 个账号）")
    finally:
        conn.close()


def _users_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(USERS_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((password + salt).encode()).hexdigest()


def create_user(username: str, password: str, role: str = "user"):
    conn = _users_conn()
    try:
        salt = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO users (username, password_hash, password_salt, role) VALUES (?, ?, ?, ?)",
            (username, hash_password(password, salt), salt, role),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(400, "用户名已存在")
    finally:
        conn.close()


def get_user_by_username(username: str):
    conn = _users_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return row
    finally:
        conn.close()


def get_user_by_id(user_id: int):
    conn = _users_conn()
    try:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()


def verify_user(username: str, password: str):
    row = get_user_by_username(username)
    if not row:
        return None
    if row["password_salt"] and hash_password(password, row["password_salt"]) == row["password_hash"]:
        return row
    return None


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    conn = _users_conn()
    try:
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
        conn.commit()
    finally:
        conn.close()
    return token


def delete_session(token: str):
    conn = _users_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def get_user_by_token(token: str):
    if not token:
        return None
    conn = _users_conn()
    try:
        row = conn.execute(
            "SELECT u.* FROM users u JOIN sessions s ON s.user_id = u.id WHERE s.token = ?", (token,)
        ).fetchone()
        return row
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    d = {k: row[k] for k in row.keys()}
    d.setdefault("banned", 0)
    d.setdefault("ban_reason", "")
    d.setdefault("banned_at", "")
    return d


def get_user_config(row) -> dict:
    """把 users 行的业务配置字段解析为 dict"""
    if row is None:
        return default_config()
    return {
        "accounts": json.loads(row["accounts"] or "[]"),
        "webhooks": json.loads(row["webhooks"] or "[]"),
        "cleanup": json.loads(row["cleanup"] or "{}"),
        "rule_templates": json.loads(row["rule_templates"] or "[]"),
    }


def save_user_config(user_id: int, cfg: dict):
    conn = _users_conn()
    try:
        conn.execute(
            "UPDATE users SET accounts=?, webhooks=?, cleanup=?, rule_templates=? WHERE id=?",
            (json.dumps(cfg.get("accounts", []), ensure_ascii=False),
             json.dumps(cfg.get("webhooks", []), ensure_ascii=False),
             json.dumps(cfg.get("cleanup", {}), ensure_ascii=False),
             json.dumps(cfg.get("rule_templates", []), ensure_ascii=False),
             user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_webhooks(user_id: int) -> list:
    user = get_user_by_id(user_id)
    if user is None:
        return config.get_webhooks()
    return get_user_config(user)["webhooks"]


def make_session_token(username: str) -> str:
    # 兼容保留：新逻辑使用 create_session
    user = get_user_by_username(username)
    if user is None:
        return ""
    return create_session(user["id"])


def verify_session(request: Request):
    """返回当前用户行(dict) 或 None"""
    token = request.cookies.get("session_token", "")
    row = get_user_by_token(token)
    return _row_to_dict(row) if row else None


def _user_ctx(request: Request):
    """未登录抛 401；返回 (user_id, 该用户的业务配置 dict)"""
    user = verify_session(request)
    if not user:
        raise HTTPException(401, "未登录")
    return user["id"], get_user_config(user)


# ============================================================
# 历史消息数据库
# ============================================================
def init_history_db():
    HISTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            account_name TEXT NOT NULL,
            account_idx INTEGER NOT NULL,
            chat_title TEXT,
            chat_id TEXT,
            sender_name TEXT,
            sender_id TEXT,
            text TEXT,
            has_media INTEGER DEFAULT 0,
            media_type TEXT DEFAULT '',
            media_path TEXT DEFAULT '',
            rule_remark TEXT DEFAULT '',
            msg_id TEXT DEFAULT '',
            topic_id TEXT DEFAULT '',
            topic_name TEXT DEFAULT '',
            starred INTEGER DEFAULT 0,
            user_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 迁移：旧库补充 media_path / msg_id 字段
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(history)").fetchall()]
        if "media_path" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN media_path TEXT DEFAULT ''")
        if "msg_id" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN msg_id TEXT DEFAULT ''")
        if "topic_id" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN topic_id TEXT DEFAULT ''")
        if "topic_name" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN topic_name TEXT DEFAULT ''")
        if "starred" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN starred INTEGER DEFAULT 0")
        if "user_id" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN user_id INTEGER DEFAULT 0")
    except Exception:
        pass
    # 去重唯一索引：(user_id, account_idx, msg_id)（多租户下按用户隔离去重）
    try:
        conn.execute("DROP INDEX IF EXISTS idx_history_dedup")
    except Exception:
        pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_history_dedup ON history (user_id, account_idx, msg_id)")
    except Exception:
        # 旧库已存在重复数据时降级为普通索引，去重交给应用层检查
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_dedup ON history (user_id, account_idx, msg_id)")
        except Exception:
            pass
    
    # 创建推送日志表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            user_id INTEGER DEFAULT 0,
            account_name TEXT NOT NULL,
            account_idx INTEGER NOT NULL,
            channel_type TEXT NOT NULL,
            channel_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 迁移：旧库补充 push_logs 字段（channel_type/channel_index/retry_count/error_message/user_id）
    try:
        pcols = [r[1] for r in conn.execute("PRAGMA table_info(push_logs)").fetchall()]
        for col, ddl in (
            ("channel_type", "channel_type TEXT NOT NULL DEFAULT ''"),
            ("channel_index", "channel_index INTEGER NOT NULL DEFAULT 0"),
            ("retry_count", "retry_count INTEGER DEFAULT 0"),
            ("error_message", "error_message TEXT DEFAULT ''"),
            ("user_id", "user_id INTEGER DEFAULT 0"),
        ):
            if col not in pcols:
                conn.execute(f"ALTER TABLE push_logs ADD COLUMN {ddl}")
        conn.commit()
    except Exception:
        pass
    
    # FTS5 全文索引（trigram 分词，支持中文；失败时自动跳过，不影响现有 LIKE 搜索）
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
                text, sender_name, chat_title,
                content='history', content_rowid='id',
                tokenize='trigram'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS history_ai AFTER INSERT ON history BEGIN
                INSERT INTO history_fts(rowid, text, sender_name, chat_title)
                VALUES (new.id, new.text, new.sender_name, new.chat_title);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS history_ad AFTER DELETE ON history BEGIN
                INSERT INTO history_fts(history_fts, rowid, text, sender_name, chat_title)
                VALUES ('delete', old.id, old.text, old.sender_name, old.chat_title);
            END
        """)
        # 首次使用或索引为空时重建，保证历史数据也可被搜索
        cnt = conn.execute("SELECT COUNT(*) FROM history_fts").fetchone()[0]
        if cnt == 0:
            conn.execute("INSERT INTO history_fts(history_fts) VALUES ('rebuild')")
    except Exception as e:
        logger.warning(f"FTS5 全文索引未启用（可忽略）: {e}")
    
    conn.commit()
    conn.close()


def save_history(user_id: int, account_name: str, account_idx: int, chat_title: str, chat_id, sender_name: str, sender_id, text: str, has_media: bool, media_type: str, rule_remark: str, media_path: str = "", msg_id=None, topic_id="", topic_name=""):
    """保存历史消息，返回 True 表示新插入，False 表示重复已忽略"""
    try:
        conn = sqlite3.connect(str(HISTORY_DB_PATH))
        cur = conn.execute(
            "INSERT OR IGNORE INTO history (ts, user_id, account_name, account_idx, chat_title, chat_id, sender_name, sender_id, text, has_media, media_type, media_path, rule_remark, msg_id, topic_id, topic_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                int(user_id), str(account_name), int(account_idx),
                str(chat_title or ""), str(chat_id or ""),
                str(sender_name or ""), str(sender_id or ""),
                str(text or ""), 1 if has_media else 0,
                str(media_type or ""), str(media_path or ""), str(rule_remark or ""),
                str(msg_id or ""), str(topic_id or ""), str(topic_name or ""),
            ]
        )
        inserted = cur.rowcount > 0
        conn.commit()
        conn.close()
        return inserted
    except Exception:
        return True


def is_history_duplicate(user_id: int, account_idx: int, msg_id) -> bool:
    """检查 (user_id, account_idx, msg_id) 是否已入库（用于去重，避免重启/重连重复推送）"""
    if msg_id is None:
        return False
    try:
        conn = sqlite3.connect(str(HISTORY_DB_PATH))
        row = conn.execute(
            "SELECT 1 FROM history WHERE user_id = ? AND account_idx = ? AND msg_id = ? LIMIT 1",
            (int(user_id), int(account_idx), str(msg_id)),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


# 启动时初始化
init_history_db()
init_users_db()


# ============================================================
# 自动备份（APScheduler 每日 03:00 备份 SQLite，保留最近 7 天）
# ============================================================
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_KEEP_DAYS = 7


def backup_database():
    """复制 history.db 到 backups 目录，并清理 7 天前的旧备份"""
    try:
        if not HISTORY_DB_PATH.exists():
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(SHANGHAI_TZ).strftime("%Y%m%d_%H%M%S")
        dst = BACKUP_DIR / f"history_{ts}.db"
        shutil.copy2(str(HISTORY_DB_PATH), str(dst))
        logger.info(f"数据库已自动备份: {dst.name}")
        # 保留最近 N 天备份，删除更早的
        backups = sorted(BACKUP_DIR.glob("history_*.db"))
        for old in backups[:-BACKUP_KEEP_DAYS]:
            try:
                old.unlink()
                logger.info(f"已清理过期备份: {old.name}")
            except Exception:
                pass
    except Exception as e:
        logger.error(f"数据库备份失败: {e}")


def start_backup_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _sched = BackgroundScheduler(timezone="Asia/Shanghai")
        _sched.add_job(backup_database, "cron", hour=3, minute=0)
        _sched.add_job(cleanup_history, "cron", hour=3, minute=30)
        _sched.start()
        logger.info("自动备份任务已启动（每日 03:00 备份，03:30 清理过期历史）")
    except ImportError:
        logger.warning("未安装 APScheduler，自动备份未启用（pip install APScheduler）")
    except Exception as e:
        logger.error(f"启动自动备份任务失败: {e}")


def cleanup_history():
    """定时清理过期历史消息：按每个用户各自的清理设置，仅删除该用户超过保留天数的记录，清理前自动备份"""
    try:
        # 清理前先备份，防止误删
        backup_database()
        # 读取所有用户的清理设置（多租户各自独立）
        uconn = _users_conn()
        users = uconn.execute("SELECT id, cleanup FROM users").fetchall()
        uconn.close()
        hist = sqlite3.connect(str(HISTORY_DB_PATH))
        deleted = 0
        for u in users:
            clean = json.loads(u["cleanup"] or "{}")
            if not clean.get("enabled"):
                continue
            keep_days = int(clean.get("keep_days") or 30)
            if keep_days <= 0:
                continue
            cutoff = (datetime.now(SHANGHAI_TZ) - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
            cur = hist.execute("DELETE FROM history WHERE user_id=? AND ts<?", (u["id"], cutoff))
            deleted += cur.rowcount
        hist.commit()
        hist.close()
        if deleted:
            logger.info(f"历史消息清理完成: 删除 {deleted} 条过期记录")
    except Exception as e:
        logger.error(f"历史消息清理失败: {e}")


# ============================================================
# 图片压缩（企业微信限制图片 2MB）
# ============================================================
WECOM_IMAGE_MAX_SIZE = 2 * 1024 * 1024  # 2MB


def compress_image(file_bytes: bytes, max_size: int = WECOM_IMAGE_MAX_SIZE) -> bytes:
    """压缩图片到指定大小以内，使用 Pillow 逐步降低质量"""
    if len(file_bytes) <= max_size:
        return file_bytes
    try:
        from PIL import Image
        img = Image.open(BytesIO(file_bytes))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        # 逐步降低分辨率和质量
        for scale in [1.0, 0.8, 0.6, 0.4, 0.3]:
            w = int(img.width * scale)
            h = int(img.height * scale)
            resized = img.resize((w, h), Image.LANCZOS)
            for quality in [85, 70, 55, 40, 25]:
                buf = BytesIO()
                resized.save(buf, format="JPEG", quality=quality, optimize=True)
                if buf.tell() <= max_size:
                    logger.info(f"图片压缩: {len(file_bytes)} -> {buf.tell()} bytes (scale={scale}, q={quality})")
                    return buf.getvalue()
        # 最低兜底
        resized = img.resize((int(img.width * 0.2), int(img.height * 0.2)), Image.LANCZOS)
        buf = BytesIO()
        resized.save(buf, format="JPEG", quality=20, optimize=True)
        return buf.getvalue()
    except ImportError:
        logger.warning("Pillow 未安装，跳过图片压缩")
        return file_bytes
    except Exception as e:
        logger.warning(f"图片压缩失败: {e}")
        return file_bytes


# ============================================================
# 企业微信媒体上传
# ============================================================
def wecom_upload_media(webhook_url: str, file_bytes: bytes, filename: str, file_type: str) -> tuple:
    """上传媒体到企业微信，返回 (media_id, error_msg)"""
    # 根据文件扩展名确定 MIME 类型
    _mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".amr": "audio/amr",
    }
    _ext = Path(filename).suffix.lower()
    content_type = _mime_map.get(_ext, "application/octet-stream")
    try:
        # 从 webhook URL 提取 key
        parsed = urllib.parse.urlparse(webhook_url)
        qs = urllib.parse.parse_qs(parsed.query)
        key = qs.get("key", [""])[0]
        if not key:
            return None, "无法从 webhook URL 中提取 key 参数"
        upload_url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={key}&type={file_type}"
        boundary = "----WebKitFormBoundary" + secrets.token_hex(8)
        body = BytesIO()
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'.encode())
        body.write(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.write(file_bytes)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        data = body.getvalue()
        req = urllib.request.Request(
            upload_url, data=data,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errcode") == 0:
                return result.get("media_id"), None
            err_msg = json.dumps(result, ensure_ascii=False)
            return None, f"企业微信上传媒体失败: {err_msg}"
    except Exception as e:
        import traceback
        return None, f"企业微信上传媒体异常: {e}\n{traceback.format_exc()}"


def wecom_send_image_direct(webhook_url: str, file_bytes: bytes) -> tuple:
    """直接用 base64 发送图片到企业微信（无需上传，图片专用）"""
    import hashlib, base64
    try:
        b64 = base64.b64encode(file_bytes).decode()
        md5 = hashlib.md5(file_bytes).hexdigest()
        payload = {"msgtype": "image", "image": {"base64": b64, "md5": md5}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errcode") == 0:
                return True, ""
            return False, json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return False, str(e)


def wecom_send_media(webhook_url: str, media_id: str, msgtype: str):
    """发送媒体消息到企业微信（文件/语音/视频，需先上传获取 media_id）"""
    payload = {"msgtype": msgtype, msgtype: {"media_id": media_id}}
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True, ""
    except Exception as e:
        return False, str(e)


# ============================================================
# 日志系统（支持 WebSocket 广播）
# ============================================================
class ShanghaiFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=SHANGHAI_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


class LogBroadcaster:
    def __init__(self):
        self.connections: list[WebSocket] = []
        self.logger = logging.getLogger("tg_monitor_web")
        self.logger.setLevel(logging.INFO)
        handler = WebLogHandler(self)
        handler.setFormatter(ShanghaiFormatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        self.logger.addHandler(handler)
        # 也输出到控制台
        console = logging.StreamHandler()
        console.setFormatter(ShanghaiFormatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        self.logger.addHandler(console)

    async def add_connection(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def remove_connection(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, message: str):
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove_connection(ws)


class WebLogHandler(logging.Handler):
    def __init__(self, broadcaster: LogBroadcaster):
        super().__init__()
        self.broadcaster = broadcaster

    def emit(self, record):
        msg = self.format(record)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.broadcaster.broadcast(msg))
        except RuntimeError:
            pass


broadcaster = LogBroadcaster()
# 屏蔽 telethon 内部的 sys.stdout print
import builtins as _builtins
_orig_print = _builtins.print
def _silent_print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    if "Telegram is having internal issues" in msg or "AuthRestartError" in msg:
        return
    _orig_print(*args, **kwargs)
_builtins.print = _silent_print

# ============================================================
# Session 锁（防止多个 check_session 或登录并发访问 SQLite）
# ============================================================
_session_locks: dict[str, asyncio.Lock] = {}

logger = broadcaster.logger

# 屏蔽 Telethon 内部告警的 print/日志输出，避免"Telegram is having internal issues"污染
import warnings
warnings.filterwarnings("ignore")
for _n in ["telethon", "telethon.client", "telethon.network"]:
    _tl = logging.getLogger(_n)
    _tl.setLevel(logging.ERROR)
    _tl.propagate = False

# ============================================================
# Webhook 推送
# ============================================================
def _do_webhook_post(url: str, payload: dict):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True, ""
    except Exception as e:
        return False, str(e)


async def _post_with_retry(url: str, payload: dict, attempts: int = 3):
    """推送失败重试：指数退避（1s→2s→4s）。返回 (ok, err_msg, retry_count)"""
    last_err = ""
    for attempt in range(attempts):
        ok, err_msg = await asyncio.to_thread(_do_webhook_post, url, payload)
        if ok:
            return True, "", attempt
        last_err = err_msg
        if attempt < attempts - 1:
            await asyncio.sleep(2 ** attempt)  # 1s → 2s → 4s
    return False, last_err, attempts


# ============================================================
# 通知频率限制（每通知渠道独立计数器，超过 limit 条/分钟暂停）
# ============================================================
PUSH_RATE_LIMIT = 20          # 每渠道每分钟最多推送条数（超过则跳过，防刷屏）
PUSH_RATE_WINDOW = 60         # 统计窗口（秒）
_channel_rate: dict[str, list] = {}


def _rate_allowed(channel_key: str) -> bool:
    """返回该渠道是否允许继续推送，同时清理过期计数"""
    import time as _time
    now = _time.time()
    records = _channel_rate.setdefault(channel_key, [])
    # 清理窗口外的记录
    _channel_rate[channel_key] = [t for t in records if now - t < PUSH_RATE_WINDOW]
    if len(_channel_rate[channel_key]) >= PUSH_RATE_LIMIT:
        return False
    _channel_rate[channel_key].append(now)
    return True


async def send_webhook_alerts(alert_text: str, webhooks: list, media_data: Optional[dict] = None, 
                              account_name: str = "", account_idx: int = -1, rule_remark: str = "",
                              user_id: int = 0):
    """发送 webhook 推送并记录推送日志"""
    now_ts = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    
    for idx, wh in enumerate(webhooks):
        url = wh.get("url", "").strip()
        if not url or not wh.get("enabled", True):
            continue
        
        # 判断渠道类型
        bot_token = wh.get("telegram_bot_token", "").strip()
        bot_chat_id = wh.get("telegram_chat_id", "").strip()
        
        # 确定渠道类型
        if bot_token and bot_chat_id:
            channel_type = "telegram_bot"
        elif "qyapi.weixin.qq.com" in url:
            channel_type = "wecom"
        elif "open.feishu.cn" in url or "open.larksuite.com" in url:
            channel_type = "feishu"
        elif "oapi.dingtalk.com" in url:
            channel_type = "dingtalk"
        else:
            channel_type = "generic"
        
        # P0-1.3 通知频率限制：每渠道独立计数器，超过限制暂停本次推送
        channel_key = f"{account_name}|{account_idx}|{channel_type}|{url}"
        if not _rate_allowed(channel_key):
            logger.warning(f"  Webhook [{idx}] 触发频率限制（{PUSH_RATE_LIMIT}条/分钟），本次推送已跳过")
            continue
        
        # 记录推送开始
        push_status = "success"
        error_msg = ""
        retry_count = 0
        
        if bot_token and bot_chat_id:
            text = alert_text[:4096]
            params = urllib.parse.urlencode({"chat_id": bot_chat_id, "text": text, "parse_mode": "Markdown"})
            bot_url = f"https://api.telegram.org/bot{bot_token}/sendMessage?{params}"
            ok, err_msg, retry_count = await _post_with_retry(bot_url, {})
            if ok:
                logger.info(f"  Webhook [{idx}] (Bot) 推送成功{'（重试%d次）' % retry_count if retry_count else ''}")
            else:
                logger.warning(f"  Webhook [{idx}] (Bot) 推送失败: {err_msg[:200]}")
                push_status = "failed"
                error_msg = err_msg[:500]
            continue
        # 企业微信机器人：支持媒体推送
        if "qyapi.weixin.qq.com" in url:
            # 先发送文本消息
            payload = {"msgtype": "markdown", "markdown": {"content": alert_text}}
            ok, err_msg, retry_count = await _post_with_retry(url, payload)
            if ok:
                logger.info(f"  Webhook [{idx}] 文本推送成功{'（重试%d次）' % retry_count if retry_count else ''}")
            else:
                logger.warning(f"  Webhook [{idx}] 文本推送失败: {err_msg[:200]}")
                push_status = "failed"
                error_msg = err_msg[:500]
            # 如果有媒体，再推送媒体消息
            if media_data:
                file_bytes = media_data.get("bytes")
                filename = media_data.get("filename", "media")
                media_type = media_data.get("media_type", "file")
                if file_bytes:
                    if media_type == "gif":
                        # GIF 动图：企业微信图片不支持 GIF，改为上传为文件保留动画
                        wecom_type = "file"
                        # 不转换，保持原始 GIF 动画
                    elif media_type in ("image", "sticker"):
                        wecom_type = "image"
                        # 企业微信只支持 jpg/png，所有图片统一用 Pillow 转为标准 JPEG
                        converted = False
                        try:
                            from PIL import Image
                            img = Image.open(BytesIO(file_bytes))
                            if img.mode in ("RGBA", "LA", "P"):
                                img = img.convert("RGB")
                            buf = BytesIO()
                            img.save(buf, format="JPEG", quality=90)
                            file_bytes = buf.getvalue()
                            filename = "telegram_converted.jpg"
                            converted = True
                        except ImportError:
                            logger.warning(f"  Webhook [{idx}] Pillow未安装，跳过图片格式转换")
                        except Exception as e:
                            sticker_mime = media_data.get("sticker_mime", "")
                            if sticker_mime == "application/x-tgsticker":
                                logger.info(f"  Webhook [{idx}] 动态贴纸(TGS)不支持图片转换，仅推送文本")
                            elif sticker_mime == "image/webp":
                                logger.warning(f"  Webhook [{idx}] 静态贴纸转换失败: {e}")
                            else:
                                logger.warning(f"  Webhook [{idx}] {media_type}无法转换: {e}")
                        if not converted:
                            file_bytes = None  # 跳过后续上传
                        # 压缩到 2MB 以内（企业微信限制）
                        if file_bytes and len(file_bytes) > WECOM_IMAGE_MAX_SIZE:
                            file_bytes = compress_image(file_bytes)
                            filename = "telegram_compressed.jpg"
                    elif media_type == "video":
                        # 企业微信机器人不支持 video 类型上传（对 webm/sticker 常报 40004 invalid media type）
                        # 统一按文件上传发送，避免失败
                        wecom_type = "file"
                    else:
                        wecom_type = "file"
                    if not file_bytes:
                        continue
                    file_size_mb = len(file_bytes) / (1024 * 1024)
                    if wecom_type == "image":
                        # 图片：直接用 base64 发送，无需上传（失败指数退避重试）
                        logger.info(f"  Webhook [{idx}] 发送图片 {filename} (size={file_size_mb:.2f}MB)")
                        ok2, err2 = False, "未尝试"
                        for _r in range(3):
                            ok2, err2 = await asyncio.to_thread(wecom_send_image_direct, url, file_bytes)
                            if ok2:
                                break
                            if _r < 2:
                                await asyncio.sleep(2 ** _r)
                        if ok2:
                            logger.info(f"  Webhook [{idx}] 图片推送成功 ({filename})")
                        else:
                            logger.warning(f"  Webhook [{idx}] 图片推送失败: {err2[:200]}")
                    else:
                        # 视频/文件：上传后用 media_id 发送（失败指数退避重试）
                        logger.info(f"  Webhook [{idx}] 上传 {filename} (type={wecom_type}, size={file_size_mb:.2f}MB)")
                        mid = ""
                        err_msg = ""
                        if file_size_mb > 20:
                            err_msg = f"{filename} 超过企业微信上传上限20MB，跳过媒体上传（仅推送文字）"
                            logger.warning(f"  Webhook [{idx}] {err_msg}")
                        else:
                            for _r in range(3):
                                mid, err_msg = await asyncio.to_thread(wecom_upload_media, url, file_bytes, filename, wecom_type)
                                if mid:
                                    break
                                if _r < 2:
                                    await asyncio.sleep(2 ** _r)
                        if err_msg and not mid:
                            logger.warning(f"  Webhook [{idx}] {err_msg}")
                        if mid:
                            ok2, err2 = False, "未尝试"
                            for _r in range(3):
                                ok2, err2 = await asyncio.to_thread(wecom_send_media, url, mid, wecom_type)
                                if ok2:
                                    break
                                if _r < 2:
                                    await asyncio.sleep(2 ** _r)
                            if ok2:
                                logger.info(f"  Webhook [{idx}] 媒体推送成功 ({filename})")
                            else:
                                logger.warning(f"  Webhook [{idx}] 媒体推送失败: {err2[:200]}")
                        else:
                            logger.warning(f"  Webhook [{idx}] 媒体上传失败")
        elif "open.feishu.cn" in url or "open.larksuite.com" in url:
            # 飞书机器人：支持文本 / 富文本
            payload = {"msg_type": "text", "content": {"text": alert_text}}
            ok, err_msg, retry_count = await _post_with_retry(url, payload)
            if ok:
                logger.info(f"  Webhook [{idx}] (飞书) 推送成功{'（重试%d次）' % retry_count if retry_count else ''}")
            else:
                logger.warning(f"  Webhook [{idx}] (飞书) 推送失败: {err_msg[:200]}")
                push_status = "failed"
                error_msg = err_msg[:500]
        elif "oapi.dingtalk.com" in url:
            # 钉钉机器人：支持 markdown 富文本
            payload = {"msgtype": "markdown", "markdown": {"title": "Telegram消息转发", "text": alert_text}}
            ok, err_msg, retry_count = await _post_with_retry(url, payload)
            if ok:
                logger.info(f"  Webhook [{idx}] (钉钉) 推送成功{'（重试%d次）' % retry_count if retry_count else ''}")
            else:
                logger.warning(f"  Webhook [{idx}] (钉钉) 推送失败: {err_msg[:200]}")
                push_status = "failed"
                error_msg = err_msg[:500]
        else:
            payload = {"title": "Telegram监控告警", "text": alert_text, "source": "tg_monitor"}
            ok, err_msg, retry_count = await _post_with_retry(url, payload)
            if ok:
                logger.info(f"  Webhook [{idx}] 推送成功")
            else:
                logger.warning(f"  Webhook [{idx}] 推送失败: {err_msg[:200]}")
                push_status = "failed"
                error_msg = err_msg[:500]
        
        # 写入推送日志
        try:
            conn = sqlite3.connect(str(HISTORY_DB_PATH))
            conn.execute(
                "INSERT INTO push_logs (ts, user_id, account_name, account_idx, channel_type, channel_index, status, error_message, retry_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now_ts, int(user_id), account_name, account_idx, channel_type, idx, push_status, error_msg, retry_count)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"记录推送日志失败: {e}")


# ============================================================
# 规则匹配（复用 gui_app.py 的逻辑）
# ============================================================
def user_matches(user_id, username, rule):
    ids = rule.get("target_user_ids") or []
    names = rule.get("target_usernames") or []
    if not ids and not names:
        return True
    # 统一转为 int 比较，避免类型不一致
    if user_id is not None:
        try:
            user_id_int = int(user_id)
        except (ValueError, TypeError):
            user_id_int = user_id
        ids_int = []
        for v in ids:
            try:
                ids_int.append(int(v))
            except (ValueError, TypeError):
                ids_int.append(v)
        if user_id_int in ids_int:
            return True
    if username and names:
        uname = (username or "").lower().lstrip("@")
        return any(uname == n.lower().lstrip("@") for n in names)
    return False


def _bare_id(val):
    """提取裸 ID（去掉 -100 前缀），支持 int 和 str 输入"""
    try:
        n = int(val)
        s = str(n)
        if s.startswith("-100") and len(s) > 4:
            return int(s[4:])
        return n
    except (ValueError, TypeError):
        return val


def _full_id(val):
    """生成带 -100 前缀的完整 ID（仅对正数生效）"""
    try:
        n = int(val)
        if n > 0:
            return int(f"-100{n}")
        return n
    except (ValueError, TypeError):
        return val


def chat_matches(chat_id, chat_title, rule):
    ids = rule.get("chat_ids") or []
    titles = rule.get("chat_titles") or []
    if not ids and not titles:
        return True
    if chat_id is not None:
        try:
            chat_id_bare = _bare_id(chat_id)
            chat_id_full = _full_id(chat_id)
        except (ValueError, TypeError):
            chat_id_bare = chat_id
            chat_id_full = chat_id
        ids_bare = []
        for v in ids:
            try:
                ids_bare.append(_bare_id(v))
            except (ValueError, TypeError):
                ids_bare.append(v)
        # 同时检查裸 ID 和完整 ID，覆盖 Telethon 返回不一致的情况
        if chat_id_bare in ids_bare or chat_id_full in ids_bare:
            return True
    # 回退到 chat_title 匹配
    if chat_title and titles:
        return any(t.strip().lower() in chat_title.lower() for t in titles)
    return False


def keyword_matches(text, rule):
    inc = rule.get("keywords_include") or []
    exc = rule.get("keywords_exclude") or []
    if inc and not any(k in text for k in inc):
        return False
    if exc and any(k in text for k in exc):
        return False
    return True


def topic_matches(topic_id, topic_name, rule):
    """话题组过滤：规则配置了 topic_ids / topic_titles 时才生效，未配置则放行"""
    ids = rule.get("topic_ids") or []
    names = rule.get("topic_titles") or []
    if not ids and not names:
        return True
    if topic_id is not None and ids:
        id_strs = set(str(v) for v in ids)
        if str(topic_id) in id_strs:
            return True
    if topic_name and names:
        if any(n.strip().lower() in topic_name.lower() for n in names):
            return True
    return False


def get_topic_info(msg):
    """提取消息的话题组基础信息，返回 (topic_id, topic_name)。topic_name 需客户端解析后补充。"""
    topic_id = ""
    topic_name = ""
    try:
        reply_to = getattr(msg, "reply_to", None)
        if reply_to is not None and getattr(reply_to, "forum_topic", False):
            tid = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
            if tid is not None:
                topic_id = str(tid)
    except Exception:
        pass
    return topic_id, topic_name


MEDIA_CN = {
    "image": "图片",
    "video": "视频",
    "sticker": "贴纸",
    "gif": "GIF",
    "document": "文件",
    "other": "其他",
}


def detect_media_type(msg) -> str:
    """判断消息媒体类型，返回内部英文类型"""
    if msg.photo:
        return "image"
    if msg.sticker:
        return "sticker"
    if msg.gif:
        return "gif"
    if msg.video:
        return "video"
    if msg.document:
        mime = (getattr(msg.document, "mime_type", "") or "").lower()
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
        return "document"
    if msg.media:
        return "other"
    return ""


def media_ext(msg, media_type: str) -> str:
    """根据媒体类型推断文件扩展名"""
    if media_type == "image":
        # 图片：优先取 mime，其次用文件扩展名
        if msg.document:
            mime = (getattr(msg.document, "mime_type", "") or "").lower()
            ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                       "image/gif": ".gif", "image/bmp": ".bmp"}
            if mime in ext_map:
                return ext_map[mime]
            if mime.startswith("image/"):
                return "." + mime.split("/")[1].replace("jpeg", "jpg")
        return ".jpg"
    if media_type == "video":
        return ".mp4"
    if media_type == "sticker":
        return ".webp"
    if media_type == "gif":
        return ".gif"
    # 文档/文件：取原始文件名扩展名
    if msg.document:
        for attr in getattr(msg.document, "attributes", []) or []:
            fn = getattr(attr, "file_name", None)
            if fn:
                return Path(fn).suffix or ".bin"
    return ".bin"


def format_alert(event, rule_remark, chat_title, sender_name, topic_name=""):
    msg = event.message
    text = msg.text or ""
    if len(text) > 200:
        text = text[:200] + "..."
    mt = detect_media_type(msg)
    mt_cn = MEDIA_CN.get(mt, mt) if mt else ""
    title = "[Telegram 消息转发]"
    meta_parts = [f"规则:{rule_remark}"]
    if chat_title:
        meta_parts.append(f"来源:{chat_title}")
    if topic_name:
        meta_parts.append(f"话题:{topic_name}")
    if sender_name:
        meta_parts.append(f"发送者:{sender_name}")
    meta_line = " | ".join(meta_parts)
    has_text = bool(msg.text and msg.text.strip())
    if mt and not has_text:
        content_line = f"({mt_cn})"
    elif mt and has_text:
        content_line = f"{text} ({mt_cn})"
    else:
        content_line = text if text else "(无文本)"
    return f"{title}\n{meta_line}\n{'—' * 12}\n{content_line}"


# ============================================================
# 监控引擎（异步版，适配 FastAPI）
# ============================================================
class AsyncMonitor:
    def __init__(self, account: dict, account_idx: int, user_id: int = 0):
        self.account = account
        self.account_idx = account_idx
        self.user_id = user_id
        self.client: Optional[TelegramClient] = None
        self.running = False
        self._stop_event = asyncio.Event()
        self._topic_cache: dict = {}

    async def resolve_topic_name(self, chat_id, topic_id):
        """根据群组 ID + 话题 ID 解析话题名称（带缓存），失败返回空串"""
        key = (str(chat_id), str(topic_id))
        if key in self._topic_cache:
            return self._topic_cache[key]
        name = ""
        try:
            from telethon.tl.functions.messages import GetForumTopicsByIDRequest
            res = await self.client(GetForumTopicsByIDRequest(peer=chat_id, topics=[int(topic_id)]))
            topics = getattr(res, "topics", [])
            if topics and getattr(topics[0], "title", None):
                name = topics[0].title
        except Exception:
            name = ""
        self._topic_cache[key] = name
        return name

    def session_path(self):
        phone = self.account.get("phone", "").replace("+", "").replace(" ", "")
        return str(BASE_DIR / f"session_{phone}")

    def build_client(self):
        proxy_cfg = self.account.get("proxy") or {}
        scheme = (proxy_cfg.get("scheme") or "").strip().lower()
        host = (proxy_cfg.get("host") or "").strip()
        port = proxy_cfg.get("port") or 0
        kwargs = dict(
            session=self.session_path(),
            api_id=self.account["api_id"],
            api_hash=self.account["api_hash"],
            request_retries=3,
            connection_retries=3,
            timeout=25,
        )
        if scheme and host and port:
            proxy_tuple = (scheme, host, int(port), True)
            if proxy_cfg.get("username"):
                proxy_tuple += (proxy_cfg["username"], proxy_cfg.get("password", ""))
            kwargs["proxy"] = proxy_tuple
        return TelegramClient(**kwargs)

    async def start(self):
        self.client = self.build_client()
        account_name = self.account.get("remark", "未知")
        rules = self.account.get("rules", [])
        webhooks = get_user_webhooks(self.user_id)
        self._stop_event.clear()

        try:
            # 1. 连接 Telegram
            await self.client.connect()
            # 2. 检查 session 是否已授权
            if not await self.client.is_user_authorized():
                logger.error(f"[{account_name}] session 未授权，请先登录")
                self.running = False
                return
            me = await self.client.get_me()
            logger.info(f"[{account_name}] 监控启动: {get_display_name(me)} (id={me.id})")
            self.running = True
        except Exception as e:
            logger.error(f"[{account_name}] 连接失败: {e}")
            self.running = False
            return

        # 3. 注册事件处理器
        @self.client.on(events.NewMessage())
        async def handler(event):
            if self._stop_event.is_set():
                return
            try:
                msg = event.message
                chat = await event.get_chat()
                sender = await event.get_sender()
                chat_id = getattr(chat, "id", None)
                chat_title = get_display_name(chat) if chat else "(未知)"
                sender_id = getattr(sender, "id", None)
                sender_username = getattr(sender, "username", None)
                sender_name = get_display_name(sender) if sender else "(未知)"
                text = msg.text or ""
                # 3.6 话题组识别：提取话题 ID/名称（名称由客户端解析，失败则留空）
                topic_id, topic_name = get_topic_info(msg)
                if topic_id and chat_id is not None:
                    topic_name = await self.resolve_topic_name(chat_id, topic_id)
                # 只处理匹配规则的消息
                matched = False
                for rule in rules:
                    cm = chat_matches(chat_id, chat_title, rule)
                    if not cm:
                        continue
                    um = user_matches(sender_id, sender_username, rule)
                    if not um:
                        continue
                    km = keyword_matches(text, rule)
                    if not km:
                        continue
                    # 3.7 话题组过滤：未配置 topic_ids/topic_titles 时放行
                    tm = topic_matches(topic_id, topic_name, rule)
                    if not tm:
                        logger.info(f"[{account_name}] 话题过滤未命中: chat={chat_title} topic_id={topic_id} topic={topic_name}")
                        continue
                    matched = True
                    # P0-1.1 消息去重：同账号同消息已处理过则跳过转发/推送，避免重启/重连重复
                    if is_history_duplicate(self.user_id, self.account_idx, msg.id):
                        logger.info(f"[{account_name}] 重复消息已去重: chat={chat_title} msg_id={msg.id}")
                        continue
                    logger.info(f"[{account_name}] 收到消息: chat={chat_title}({chat_id}) sender={sender_name}({sender_id}) text={text[:50]}")
                    logger.info(f"[{account_name}] 规则 '{rule.get('remark', '')}': 匹配成功")
                    remark = rule.get("remark", "规则")
                    alert = format_alert(event, remark, chat_title, sender_name, topic_name)
                    logger.info(f"\n[{account_name}] {alert}")
                    if rule.get("forward_to_saved", True):
                        try:
                            await self.client.forward_messages("me", msg)
                            logger.info(f"[{account_name}] 已转发到 Saved Messages")
                        except Exception as e:
                            logger.error(f"[{account_name}] 转发失败: {e}")
                    # 检测媒体类型
                    media_type = detect_media_type(msg)
                    has_media = bool(media_type)
                    # 下载媒体（用于 Webhook 推送 + 网页展示存档）
                    media_data = None
                    media_path = ""
                    if has_media:
                        try:
                            # 检查贴纸类型：动态贴纸(TGS)尝试转换为GIF
                            sticker_mime = ""
                            if msg.sticker:
                                sticker_mime = (getattr(msg.sticker, "mime_type", "") or "").lower()
                                if sticker_mime == "application/x-tgsticker":
                                    # 下载后尝试转换为 GIF（TGS = gzip 压缩的 lottie JSON）
                                    file_bytes_val = await self.client.download_media(msg, file=bytes)
                                    if isinstance(file_bytes_val, bytes):
                                        try:
                                            from lottie.parsers.tgs import parse_tgs
                                            from lottie.exporters.gif import export_gif
                                            try:
                                                animation = parse_tgs(BytesIO(file_bytes_val))
                                            except Exception:
                                                import tempfile, os
                                                with tempfile.NamedTemporaryFile(delete=False, suffix=".tgs") as tf:
                                                    tf.write(file_bytes_val)
                                                    tf_path = tf.name
                                                try:
                                                    animation = parse_tgs(tf_path)
                                                finally:
                                                    try:
                                                        os.unlink(tf_path)
                                                    except Exception:
                                                        pass
                                            gif_buf = BytesIO()
                                            export_gif(animation, gif_buf, skip_frames=6)
                                            file_bytes_val = gif_buf.getvalue()
                                            media_type = "gif"
                                            sticker_mime = ""
                                            logger.info(f"[{account_name}] 动态贴纸(TGS)已转换为GIF ({len(file_bytes_val)} bytes)")
                                        except ImportError as e:
                                            logger.info(f"[{account_name}] 动态贴纸(TGS)转换依赖缺失: {e}，请在服务器运行 `pip install lottie cairosvg pillow` 并重启服务")
                                            file_bytes_val = None
                                        except Exception as e:
                                            logger.warning(f"[{account_name}] 动态贴纸(TGS)转换GIF失败: {e}")
                                            file_bytes_val = None
                                    else:
                                        file_bytes_val = None
                                elif sticker_mime == "video/webm":
                                    media_type = "video"  # 视频贴纸当作视频处理
                                    file_bytes_val = await self.client.download_media(msg, file=bytes)
                                else:
                                    file_bytes_val = await self.client.download_media(msg, file=bytes)
                            else:
                                file_bytes_val = await self.client.download_media(msg, file=bytes)
                            if not isinstance(file_bytes_val, bytes):
                                file_bytes_val = None
                            if file_bytes_val:
                                ext = media_ext(msg, media_type)
                                # 存到本地 media 目录，供网页展示
                                media_dir = BASE_DIR / "media"
                                media_dir.mkdir(parents=True, exist_ok=True)
                                fname = f"{msg.id}_{int(datetime.now().timestamp())}{ext}"
                                (media_dir / fname).write_bytes(file_bytes_val)
                                media_path = f"/media/{fname}"
                                media_data = {"bytes": file_bytes_val, "filename": f"telegram{ext}", "media_type": media_type, "sticker_mime": sticker_mime}
                        except Exception as e:
                            logger.warning(f"[{account_name}] 下载媒体失败: {e}")
                    save_history(self.user_id, account_name, self.account_idx, chat_title, chat_id,
                                 sender_name, sender_id, text, has_media, media_type, remark, media_path, msg.id, topic_id, topic_name)
                    # Webhook：规则级优先，有规则级则跳过全局
                    wh_list = []
                    if rule.get("webhook_enabled"):
                        rw = {
                            "enabled": True,
                            "url": rule.get("webhook_url", ""),
                            "telegram_bot_token": rule.get("webhook_bot_token", ""),
                            "telegram_chat_id": rule.get("webhook_chat_id", ""),
                        }
                        u = rw["url"].strip()
                        if u:
                            wh_list.append(rw)
                    else:
                        for gw in (webhooks or []):
                            u = gw.get("url", "").strip()
                            if u:
                                wh_list.append(gw)
                    if wh_list:
                        asyncio.ensure_future(send_webhook_alerts(alert, wh_list, media_data, account_name, self.account_idx, remark, self.user_id))
            except Exception as e:
                logger.error(f"[{account_name}] 处理消息异常: {e}")

        # 4. 保持连接
        try:
            await self.client.run_until_disconnected()
        except Exception as e:
            logger.error(f"[{account_name}] 监控断开: {e}")
        finally:
            self.running = False
            if self.client:
                await self.client.disconnect()

    async def stop(self):
        self._stop_event.set()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.running = False


# ============================================================
# 监控管理器（管理所有账号的监控任务）
# ============================================================
class MonitorManager:
    def __init__(self):
        self.monitors: dict[tuple, AsyncMonitor] = {}
        self.tasks: dict[tuple, asyncio.Task] = {}

    @staticmethod
    def _key(user_id: int, account_idx: int) -> tuple:
        return (user_id, account_idx)

    def user_accounts(self, user_id: int) -> list:
        user = get_user_by_id(user_id)
        return get_user_config(user)["accounts"]

    async def start_monitor(self, user_id: int, account_idx: int):
        key = self._key(user_id, account_idx)
        if key in self.monitors and self.monitors[key].running:
            return {"status": "already_running"}
        # 封禁用户禁止启动监控
        owner = get_user_by_id(user_id)
        if owner is None or owner["banned"]:
            return {"status": "error", "message": "账号已封禁，无法启动监控"}
        accounts = self.user_accounts(user_id)
        if account_idx < 0 or account_idx >= len(accounts):
            raise HTTPException(404, "账号不存在")
        # 先检查 session 是否有效
        chk = check_session(accounts[account_idx])
        if not chk["valid"]:
            return {"status": "error", "message": "session 未登录，请先点击「登录」"}
        monitor = AsyncMonitor(accounts[account_idx], account_idx, user_id=user_id)
        self.monitors[key] = monitor
        self.tasks[key] = asyncio.create_task(monitor.start())
        logger.info(f"[{accounts[account_idx].get('remark', '')}] 监控任务已创建 (user={user_id})")
        return {"status": "started"}

    async def stop_monitor(self, user_id: int, account_idx: int):
        key = self._key(user_id, account_idx)
        if key in self.monitors:
            await self.monitors[key].stop()
            if key in self.tasks:
                self.tasks[key].cancel()
                del self.tasks[key]
            del self.monitors[key]
            return {"status": "stopped"}
        return {"status": "not_running"}

    def is_running(self, user_id: int, account_idx: int) -> bool:
        key = self._key(user_id, account_idx)
        m = self.monitors.get(key)
        if m is None:
            return False
        if not m.running:
            # 可能还在连接中，检查 task 是否活着
            t = self.tasks.get(key)
            if t is not None and not t.done():
                return False  # 还在连接中，不删除
            # task 已结束，清理
            del self.tasks[key]
            del self.monitors[key]
            return False
        # 检查 task 是否还活着
        t = self.tasks.get(key)
        if t is None or t.done():
            m.running = False
            del self.tasks[key]
            del self.monitors[key]
            return False
        return True

    def get_status(self, user_id: int, account_idx: int) -> dict:
        return {
            "idx": account_idx,
            "running": self.is_running(user_id, account_idx),
        }

    def all_status(self, user_id: int) -> list[dict]:
        result = []
        accounts = self.user_accounts(user_id)
        for i in range(len(accounts)):
            result.append({
                "idx": i,
                "running": self.is_running(user_id, i),
            })
        return result

    async def stop_all(self, user_id: int):
        """停止指定用户的所有监控任务（管理员删除用户时调用）"""
        keys = [k for k in list(self.monitors.keys()) if k[0] == user_id]
        for key in keys:
            try:
                await self.monitors[key].stop()
            except Exception:
                pass
            if key in self.tasks:
                self.tasks[key].cancel()
                del self.tasks[key]
            if key in self.monitors:
                del self.monitors[key]


monitor_mgr = MonitorManager()


# ============================================================
# FastAPI 应用
# ============================================================
app = FastAPI(title="Telegram 监控管理后台", version="2.0.0")

# ============================================================
# Auth API - 登录登出
# ============================================================
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if verify_session(request):
        return RedirectResponse(url="/")
    if user_count() == 0:
        html_path = BASE_DIR / "templates" / "setup.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
    else:
        html_path = BASE_DIR / "templates" / "login.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>请先创建 templates/login.html</h1>")


def user_count() -> int:
    conn = _users_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


@app.post("/api/login")
async def login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if user_count() == 0:
        # 首次设置第一个账号（自动成为管理员）
        if not username or not password:
            return {"status": "error", "message": "用户名和密码不能为空"}
        if len(password) < 6:
            return {"status": "error", "message": "密码长度至少6位"}
        create_user(username, password, role="admin")
    user = verify_user(username, password)
    if user is None:
        return {"status": "error", "message": "用户名或密码错误"}
    if user["banned"]:
        return {"status": "error", "message": f"账号已被封禁，无法登录。原因：{user['ban_reason'] or '未填写'}"}
    token = create_session(user["id"])
    return {"status": "ok", "token": token, "username": user["username"], "role": user["role"]}


@app.post("/api/setup")
async def setup(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return {"status": "error", "message": "用户名和密码不能为空"}
    if len(password) < 6:
        return {"status": "error", "message": "密码长度至少6位"}
    if user_count() == 0:
        create_user(username, password, role="admin")
    else:
        return {"status": "error", "message": "系统已初始化"}
    token = create_session(get_user_by_username(username)["id"])
    return {"status": "ok", "token": token, "message": "管理员账号设置成功"}


@app.post("/api/logout")
async def logout(request: Request):
    token = request.cookies.get("session_token", "")
    if token:
        delete_session(token)
    return {"status": "ok"}


# ============================================================
# API - 用户注册 / 管理员用户管理
# ============================================================
@app.post("/api/register")
async def register(request: Request):
    if verify_session(request):
        return {"status": "error", "message": "请先退出当前账号"}
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return {"status": "error", "message": "用户名和密码不能为空"}
    if len(username) < 2:
        return {"status": "error", "message": "用户名至少2个字符"}
    if len(password) < 6:
        return {"status": "error", "message": "密码长度至少6位"}
    try:
        create_user(username, password, role="user")
    except HTTPException as e:
        return {"status": "error", "message": e.detail}
    logger.info(f"[多租户] 新用户注册: {username}")
    token = create_session(get_user_by_username(username)["id"])
    return {"status": "ok", "token": token, "username": username, "role": "user"}


def _require_admin(request: Request) -> dict:
    """返回当前用户 dict；非管理员抛 403"""
    user = verify_session(request)
    if not user:
        raise HTTPException(401, "未登录")
    if user["role"] != "admin":
        raise HTTPException(403, "无管理员权限")
    return user


def _count_user_resources(user_id: int) -> dict:
    """统计用户账号数/规则数/历史消息数"""
    cfg = get_user_config(get_user_by_id(user_id))
    account_cnt = len(cfg["accounts"])
    rule_cnt = sum(len(a.get("rules", [])) for a in cfg["accounts"])
    hist_cnt = 0
    try:
        hist = sqlite3.connect(str(HISTORY_DB_PATH))
        row = hist.execute("SELECT COUNT(*) FROM history WHERE user_id = ?", (int(user_id),)).fetchone()
        hist_cnt = row[0] if row else 0
        hist.close()
    except Exception:
        pass
    return {"accounts": account_cnt, "rules": rule_cnt, "history": hist_cnt}


@app.get("/api/users")
async def list_users(request: Request):
    _require_admin(request)
    conn = _users_conn()
    try:
        rows = conn.execute(
            "SELECT id, username, role, banned, ban_reason, banned_at, created_at FROM users ORDER BY id"
        ).fetchall()
        sessions = dict(conn.execute(
            "SELECT user_id, COUNT(*) FROM sessions GROUP BY user_id"
        ).fetchall())
    finally:
        conn.close()
    result = []
    for row in rows:
        counters = _count_user_resources(row["id"])
        result.append({
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "banned": bool(row["banned"]),
            "ban_reason": row["ban_reason"],
            "banned_at": row["banned_at"],
            "created_at": row["created_at"],
            "online": sessions.get(row["id"], 0) > 0,
            **counters,
        })
    return result


@app.post("/api/users")
async def create_admin_user(request: Request, data: dict):
    _require_admin(request)
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    role = data.get("role", "user")
    if role not in ("admin", "user"):
        role = "user"
    if not username or not password:
        return {"status": "error", "message": "用户名和密码不能为空"}
    if len(username) < 2:
        return {"status": "error", "message": "用户名至少2个字符"}
    if len(password) < 6:
        return {"status": "error", "message": "密码长度至少6位"}
    try:
        create_user(username, password, role=role)
    except HTTPException as e:
        return {"status": "error", "message": e.detail}
    logger.info(f"[多租户] 管理员创建用户: {username} (role={role})")
    return {"status": "ok", "message": f"用户 {username} 创建成功"}


@app.delete("/api/users/{user_id}")
async def delete_admin_user(user_id: int, request: Request):
    admin = _require_admin(request)
    target = get_user_by_id(user_id)
    if target is None:
        return {"status": "error", "message": "用户不存在"}
    if target["id"] == admin["id"]:
        return {"status": "error", "message": "不能删除当前账号"}
    conn = _users_conn()
    try:
        # 清理会话与用户
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    # 停止该用户所有监控任务
    await monitor_mgr.stop_all(user_id)
    logger.info(f"[多租户] 管理员删除用户: {target['username']} (id={user_id})")
    return {"status": "ok", "message": f"用户 {target['username']} 已删除"}


# --- 全局概览（仅管理员） ---
@app.get("/api/admin/overview")
async def admin_overview(request: Request):
    _require_admin(request)
    conn = _users_conn()
    try:
        user_cnt = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        admin_cnt = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
        online_cnt = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM sessions"
        ).fetchone()[0]
        banned_cnt = conn.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
        rows = conn.execute("SELECT accounts, webhooks FROM users").fetchall()
    finally:
        conn.close()
    total_accounts = 0
    total_rules = 0
    for r in rows:
        accs = json.loads(r["accounts"] or "[]")
        total_accounts += len(accs)
        total_rules += sum(len(a.get("rules", [])) for a in accs)
    # 消息总量 / 推送量
    hist = sqlite3.connect(str(HISTORY_DB_PATH))
    try:
        total_msgs = hist.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        today = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
        today_msgs = hist.execute("SELECT COUNT(*) FROM history WHERE ts LIKE ?", (f"{today}%",)).fetchone()[0]
        push_total = hist.execute("SELECT COUNT(*) FROM push_logs").fetchone()[0]
        push_today = hist.execute("SELECT COUNT(*) FROM push_logs WHERE ts LIKE ?", (f"{today}%",)).fetchone()[0]
    finally:
        hist.close()
    return {
        "users": user_cnt, "admins": admin_cnt, "online": online_cnt, "banned": banned_cnt,
        "total_accounts": total_accounts, "total_rules": total_rules,
        "total_msgs": total_msgs, "today_msgs": today_msgs,
        "push_total": push_total, "push_today": push_today,
    }


# --- 用户详情（仅管理员） ---
@app.get("/api/users/{user_id}")
async def get_user_detail(user_id: int, request: Request):
    _require_admin(request)
    user = get_user_by_id(user_id)
    if user is None:
        return {"status": "error", "message": "用户不存在"}
    cfg = get_user_config(user)
    # 最近推送（表结构或数据异常时降级为空列表，避免详情页 500）
    recent_pushes, recent_msgs = [], []
    try:
        hist = sqlite3.connect(str(HISTORY_DB_PATH))
        try:
            recent_pushes = [dict(r) for r in hist.execute(
                "SELECT ts, account_name, channel_type, status, error_message FROM push_logs "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,)
            ).fetchall()]
        except Exception:
            recent_pushes = []
        try:
            recent_msgs = [dict(r) for r in hist.execute(
                "SELECT ts, account_name, chat_title, text FROM history "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,)
            ).fetchall()]
        except Exception:
            recent_msgs = []
    except Exception:
        pass
    finally:
        try:
            hist.close()
        except Exception:
            pass
    return {
        "username": user["username"],
        "role": user["role"],
        "created_at": user["created_at"],
        "banned": bool(user["banned"]),
        "ban_reason": user["ban_reason"],
        "banned_at": user["banned_at"],
        "accounts": cfg["accounts"],
        "webhooks": cfg["webhooks"],
        "cleanup": cfg["cleanup"],
        "rule_templates": cfg["rule_templates"],
        "recent_pushes": recent_pushes,
        "recent_msgs": recent_msgs,
    }


# --- 编辑用户（用户名/角色） ---
@app.put("/api/users/{user_id}")
async def edit_user(user_id: int, request: Request, data: dict):
    admin = _require_admin(request)
    target = get_user_by_id(user_id)
    if target is None:
        return {"status": "error", "message": "用户不存在"}
    new_username = (data.get("username") or target["username"]).strip()
    role = data.get("role") or target["role"]
    if role not in ("admin", "user"):
        return {"status": "error", "message": "角色无效"}
    # 修改当前管理员自身角色时会锁死系统，阻止降级自己为普通用户
    if user_id == admin["id"] and role != "admin":
        return {"status": "error", "message": "不能取消自己的管理员角色"}
    conn = _users_conn()
    try:
        if new_username != target["username"]:
            if conn.execute("SELECT id FROM users WHERE username=?", (new_username,)).fetchone():
                return {"status": "error", "message": "用户名已存在"}
        conn.execute("UPDATE users SET username=?, role=? WHERE id=?", (new_username, role, user_id))
        conn.commit()
    except sqlite3.IntegrityError:
        return {"status": "error", "message": "用户名已存在"}
    finally:
        conn.close()
    logger.info(f"[多租户] 管理员编辑用户 #{user_id}: → {new_username} (role={role})")
    return {"status": "ok", "message": "用户信息已更新"}


# --- 封禁/解封用户（弹窗填理由） ---
@app.post("/api/users/{user_id}/ban")
async def ban_user(user_id: int, request: Request, data: dict):
    admin = _require_admin(request)
    target = get_user_by_id(user_id)
    if target is None:
        return {"status": "error", "message": "用户不存在"}
    if target["id"] == admin["id"]:
        return {"status": "error", "message": "不能封禁当前账号"}
    reason = (data.get("reason") or "").strip()
    conn = _users_conn()
    try:
        conn.execute(
            "UPDATE users SET banned=1, ban_reason=?, banned_at=(datetime('now','localtime')) WHERE id=?",
            (reason, user_id),
        )
        # 端掉该用户现有会话
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    await monitor_mgr.stop_all(user_id)
    logger.info(f"[多租户] 管理员封禁用户: {target['username']} (原因: {reason or '未填写'})")
    return {"status": "ok", "message": f"用户 {target['username']} 已封禁"}


@app.post("/api/users/{user_id}/unban")
async def unban_user(user_id: int, request: Request):
    _require_admin(request)
    target = get_user_by_id(user_id)
    if target is None:
        return {"status": "error", "message": "用户不存在"}
    conn = _users_conn()
    try:
        conn.execute("UPDATE users SET banned=0, ban_reason='' WHERE id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[多租户] 管理员解封用户: {target['username']}")
    return {"status": "ok", "message": f"用户 {target['username']} 已解封"}


# --- 重置用户密码（仅管理员） ---
@app.post("/api/users/{user_id}/reset-password")
async def reset_user_password(user_id: int, request: Request, data: dict):
    _require_admin(request)
    target = get_user_by_id(user_id)
    if target is None:
        return {"status": "error", "message": "用户不存在"}
    password = (data.get("password") or "").strip()
    if len(password) < 6:
        return {"status": "error", "message": "密码长度至少6位"}
    salt = secrets.token_hex(16)
    conn = _users_conn()
    try:
        conn.execute(
            "UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
            (hash_password(password, salt), salt, user_id),
        )
        # 重置密码后强制下线所有会话
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[多租户] 管理员重置用户密码: {target['username']}")
    return {"status": "ok", "message": f"用户 {target['username']} 密码已重置"}


# --- 修改本人密码（管理员/普通用户通用） ---
@app.post("/api/change-password")
def change_password(request: Request, data: dict):
    """当前登录用户修改自己的密码"""
    user = verify_session(request)
    if not user:
        raise HTTPException(401, "未登录")
    old = data.get("old_password", "") or ""
    new = (data.get("new_password") or "").strip()
    if len(new) < 6:
        return {"status": "error", "message": "新密码长度至少6位"}
    if hash_password(old, user["password_salt"]) != user["password_hash"]:
        return {"status": "error", "message": "当前密码不正确"}
    salt = secrets.token_hex(16)
    conn = _users_conn()
    try:
        conn.execute(
            "UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
            (hash_password(new, salt), salt, user["id"]),
        )
        # 修改密码后强制下线其它会话，当前会话保留
        conn.execute(
            "DELETE FROM sessions WHERE user_id=? AND token<>?",
            (user["id"], request.cookies.get("session_token", "")),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(f"[用户] {user['username']} 修改了自己的密码")
    return {"status": "ok", "message": "密码修改成功"}


# 全局鉴权中间件：未登录跳转 /login
from fastapi import Depends
from fastapi.security import APIKeyCookie
from typing import Callable

security = APIKeyCookie(name="session_token", auto_error=False)

async def require_auth(request: Request, token: str = Depends(security)) -> bool:
    user = get_user_by_token(token)
    if user is None:
        return False
    request.state.current_user = _row_to_dict(user)
    return True


# ============================================================
# API - 账号管理
# ============================================================
@app.get("/api/accounts")
async def list_accounts(request: Request):
    user_id, cfg = _user_ctx(request)
    accounts = cfg["accounts"]
    statuses = monitor_mgr.all_status(user_id)
    status_map = {s["idx"]: s["running"] for s in statuses}
    result = []
    for i, acc in enumerate(accounts):
        sess = check_session(acc)
        result.append({
            "idx": i,
            "remark": acc.get("remark", ""),
            "phone": acc.get("phone", ""),
            "api_id": acc.get("api_id", ""),
            "api_hash": acc.get("api_hash", ""),
            "proxy": acc.get("proxy", {"scheme": "", "host": "", "port": 0}),
            "rules": acc.get("rules", []),
            "running": status_map.get(i, False),
            "session_valid": sess.get("valid", False),
            "session_name": sess.get("display_name", ""),
        })
    return result


@app.post("/api/accounts")
def add_account(request: Request, data: dict):
    user_id, cfg = _user_ctx(request)
    account = {
        "remark": data.get("remark", "未命名"),
        "phone": data.get("phone", ""),
        "api_id": data.get("api_id", 2040),
        "api_hash": data.get("api_hash", ""),
        "proxy": data.get("proxy", {"scheme": "", "host": "", "port": 0, "username": "", "password": ""}),
        "rules": [],
    }
    cfg["accounts"].append(account)
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 已添加账号: {account['remark']} ({account['phone']})")
    return {"status": "ok", "idx": len(cfg["accounts"]) - 1}


@app.put("/api/accounts/{idx}")
def update_account(idx: int, request: Request, data: dict):
    user_id, cfg = _user_ctx(request)
    accounts = cfg["accounts"]
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    acc = accounts[idx]
    if "remark" in data:
        acc["remark"] = data["remark"]
    if "phone" in data:
        acc["phone"] = data["phone"]
    if "api_id" in data:
        acc["api_id"] = data["api_id"]
    if "api_hash" in data:
        acc["api_hash"] = data["api_hash"]
    if "proxy" in data:
        acc["proxy"] = data["proxy"]
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 已更新账号: {acc.get('remark', '')}")
    return {"status": "ok"}


@app.delete("/api/accounts/{idx}")
def delete_account(idx: int, request: Request):
    user_id, cfg = _user_ctx(request)
    accounts = cfg["accounts"]
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    # 先停止监控
    asyncio.create_task(monitor_mgr.stop_monitor(user_id, idx))
    del accounts[idx]
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 已删除账号: {accounts[idx].get('remark', '') if idx < len(accounts) else ''}")
    return {"status": "ok"}


# ============================================================
# 辅助 - 检查 session 登录状态（仅检查文件，不连接 Telegram）
# ============================================================
def check_session(account: dict) -> dict:
    """检查 session 文件是否存在且非空。不连接 Telegram，避免并发锁问题。"""
    phone = account.get("phone", "").replace("+", "").replace(" ", "")
    # Telethon 会自动在文件名后追加 .session 后缀
    session_path = BASE_DIR / f"session_{phone}.session"
    if session_path.exists() and session_path.stat().st_size > 0:
        return {"valid": True, "display_name": account.get("remark", "")}
    return {"valid": False}


# ============================================================
# API - 登录（异步交互式）
# ============================================================
_login_states: dict = {}  # idx -> { client, done_event, code_event, code, result, error }


@app.post("/api/accounts/{idx}/login")
async def login_start(idx: int, request: Request):
    user_id, cfg = _user_ctx(request)
    key = (user_id, idx)
    accounts = cfg["accounts"]
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    acc = accounts[idx]
    phone_clean = acc["phone"].replace("+", "").replace(" ", "")
    session_path = str(BASE_DIR / f"session_{phone_clean}")

    # 检查 session 文件是否存在（不连接 Telegram）
    chk = check_session(acc)
    if chk["valid"]:
        return {"status": "ok", "message": f"session 有效，已登录为 {chk.get('display_name', '')}"}

    # 如果之前有登录状态，清理
    if key in _login_states:
        old = _login_states[key]
        try:
            await old.get("client", None).disconnect()
        except Exception:
            pass
        del _login_states[key]

    state = {
        "client": None,
        "done_event": asyncio.Event(),
        "code_event": asyncio.Event(),
        "code": "",
        "result": None,
        "error": None,
    }
    _login_states[key] = state

    async def do_login():
        proxy_cfg = acc.get("proxy") or {}
        scheme = (proxy_cfg.get("scheme") or "").strip().lower()
        host = (proxy_cfg.get("host") or "").strip()
        port = proxy_cfg.get("port") or 0
        kwargs = dict(
            session=session_path,
            api_id=acc["api_id"],
            api_hash=acc["api_hash"],
            request_retries=3,
            connection_retries=3,
            timeout=30,
        )
        if scheme and host and port:
            proxy_tuple = (scheme, host, int(port), True)
            if proxy_cfg.get("username"):
                proxy_tuple += (proxy_cfg["username"], proxy_cfg.get("password", ""))
            kwargs["proxy"] = proxy_tuple

        try:
            client = TelegramClient(**kwargs)
            state["client"] = client
            await client.connect()
            await client.send_code_request(acc["phone"])
            # 等待用户输入验证码
            await state["code_event"].wait()
            state["code_event"].clear()
            try:
                await client.sign_in(phone=acc["phone"], code=state["code"])
            except SessionPasswordNeededError:
                state["result"] = "NEED_PASSWORD"
                state["done_event"].set()
                return
            me = await client.get_me()
            state["result"] = f"登录成功: {get_display_name(me)} (id={me.id})"
            logger.info(f"[{acc.get('remark', '')}] {state['result']}")
            await client.disconnect()
        except Exception as e:
            err_msg = str(e)
            if "AuthRestartError" in err_msg or "AuthRestart" in type(e).__name__:
                try:
                    Path(session_path).unlink(missing_ok=True)
                    logger.info(f"[{acc.get('remark', '')}] session 损坏已删除，请重试登录")
                except Exception:
                    pass
                state["error"] = "session 已损坏，已自动清理，请重新点击登录"
            else:
                state["error"] = err_msg
            logger.error(f"[{acc.get('remark', '')}] 登录失败: {err_msg}")
            try:
                await client.disconnect()
            except Exception:
                pass
        finally:
            state["done_event"].set()

    asyncio.create_task(do_login())
    await asyncio.sleep(0.3)

    # 如果登录任务已经完成（比如不需要验证码的情况），直接返回结果
    if state.get("result") == "NEED_PASSWORD":
        return {"status": "need_password", "message": "需要两步验证密码"}
    if state.get("result"):
        del _login_states[key]
        return {"status": "ok", "message": state["result"]}
    if state.get("error"):
        err = state["error"]
        del _login_states[key]
        return {"status": "error", "message": err}

    return {"status": "need_code", "message": "请输入验证码"}


@app.post("/api/accounts/{idx}/login/code")
async def login_code(idx: int, request: Request, data: dict):
    user_id, _ = _user_ctx(request)
    key = (user_id, idx)
    state = _login_states.get(key)
    if not state:
        raise HTTPException(400, "没有进行中的登录，请先点击登录")
    code = data.get("code", "").strip()
    if not code:
        raise HTTPException(400, "验证码不能为空")
    state["code"] = code
    state["code_event"].set()

    # 等待登录任务完成，最长 30 秒
    try:
        await asyncio.wait_for(state["done_event"].wait(), timeout=30)
    except asyncio.TimeoutError:
        state["error"] = "登录超时，请重试"

    if state.get("result") == "NEED_PASSWORD":
        return {"status": "need_password", "message": "需要两步验证密码"}
    if state.get("result"):
        del _login_states[key]
        return {"status": "ok", "message": state["result"]}
    if state.get("error"):
        err = state["error"]
        del _login_states[key]
        return {"status": "error", "message": err}

    del _login_states[key]
    return {"status": "error", "message": "登录失败，请重试"}


@app.post("/api/accounts/{idx}/login/password")
async def login_password(idx: int, request: Request, data: dict):
    user_id, _ = _user_ctx(request)
    key = (user_id, idx)
    state = _login_states.get(key)
    if not state:
        raise HTTPException(400, "没有进行中的登录，请先点击登录")
    password = data.get("password", "").strip()
    if not password:
        raise HTTPException(400, "密码不能为空")

    client = state.get("client")
    if not client:
        del _login_states[key]
        raise HTTPException(400, "登录会话已过期，请重新登录")

    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        msg = f"登录成功: {get_display_name(me)} (id={me.id})"
        logger.info(f"[user={user_id} acc={idx}] {msg}")
        await client.disconnect()
        del _login_states[key]
        return {"status": "ok", "message": msg}
    except Exception as e:
        return {"status": "error", "message": f"密码错误: {e}"}


# ============================================================
# API - 规则管理
# ============================================================
@app.get("/api/accounts/{idx}/rules")
def list_rules(idx: int, request: Request):
    _, cfg = _user_ctx(request)
    accounts = cfg["accounts"]
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    rules = accounts[idx].get("rules", [])
    result = []
    for ri, rule in enumerate(rules):
        result.append({"idx": ri, **rule})
    return result


@app.post("/api/accounts/{idx}/rules")
def add_rule(idx: int, request: Request, data: dict):
    user_id, cfg = _user_ctx(request)
    accounts = cfg["accounts"]
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    rule = {
        "remark": data.get("remark", "未命名"),
        "target_user_ids": data.get("target_user_ids", []),
        "target_usernames": data.get("target_usernames", []),
        "chat_ids": data.get("chat_ids", []),
        "chat_titles": [],
        "keywords_include": data.get("keywords_include", []),
        "keywords_exclude": data.get("keywords_exclude", []),
        "forward_to_saved": data.get("forward_to_saved", True),
        "forward_to_chats": data.get("forward_to_chats", []),
        "webhook_enabled": data.get("webhook_enabled", False),
        "webhook_url": data.get("webhook_url", ""),
        "webhook_bot_token": data.get("webhook_bot_token", ""),
        "webhook_chat_id": data.get("webhook_chat_id", ""),
    }
    accounts[idx].setdefault("rules", []).append(rule)
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 已添加规则: {rule['remark']}")
    return {"status": "ok"}


@app.put("/api/accounts/{idx}/rules/{ridx}")
def update_rule(idx: int, ridx: int, request: Request, data: dict):
    user_id, cfg = _user_ctx(request)
    accounts = cfg["accounts"]
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    rules = accounts[idx].get("rules", [])
    if ridx < 0 or ridx >= len(rules):
        raise HTTPException(404, "规则不存在")
    rule = rules[ridx]
    for key in ["remark", "target_user_ids", "target_usernames", "chat_ids", "chat_titles",
                 "keywords_include", "keywords_exclude", "forward_to_saved", "forward_to_chats",
                 "webhook_enabled", "webhook_url", "webhook_bot_token", "webhook_chat_id"]:
        if key in data:
            rule[key] = data[key]
    rules[ridx] = rule
    accounts[idx]["rules"] = rules
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 已更新规则: {rule.get('remark', '')}")
    return {"status": "ok"}


@app.delete("/api/accounts/{idx}/rules/{ridx}")
def delete_rule(idx: int, ridx: int, request: Request):
    user_id, cfg = _user_ctx(request)
    accounts = cfg["accounts"]
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    rules = accounts[idx].get("rules", [])
    if ridx < 0 or ridx >= len(rules):
        raise HTTPException(404, "规则不存在")
    del rules[ridx]
    accounts[idx]["rules"] = rules
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 已删除规则 #{ridx}")
    return {"status": "ok"}


# ============================================================
# API - 规则模板（常用规则一键套用）
# ============================================================
@app.get("/api/rule-templates")
def list_rule_templates(request: Request):
    _, cfg = _user_ctx(request)
    return cfg["rule_templates"]


@app.post("/api/rule-templates")
def save_rule_template(request: Request, data: dict):
    user_id, cfg = _user_ctx(request)
    name = str(data.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "模板名称不能为空")
    tpl = {k: v for k, v in data.items() if k != "name"}
    tpl["name"] = name
    templates = cfg["rule_templates"]
    for i, t in enumerate(templates):
        if t.get("name") == name:
            templates[i] = tpl
            break
    else:
        templates.append(tpl)
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 规则模板已保存: {name}")
    return {"status": "ok"}


@app.delete("/api/rule-templates/{name}")
def delete_rule_template(name: str, request: Request):
    user_id, cfg = _user_ctx(request)
    cfg["rule_templates"] = [t for t in cfg["rule_templates"] if t.get("name") != name]
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 规则模板已删除: {name}")
    return {"status": "ok"}


# ============================================================
# API - 监控控制
# ============================================================
@app.post("/api/accounts/{idx}/start")
async def start_monitor(idx: int, request: Request):
    user_id, _ = _user_ctx(request)
    return await monitor_mgr.start_monitor(user_id, idx)


@app.post("/api/accounts/{idx}/stop")
async def stop_monitor(idx: int, request: Request):
    user_id, _ = _user_ctx(request)
    return await monitor_mgr.stop_monitor(user_id, idx)


@app.get("/api/accounts/{idx}/status")
def get_status(idx: int, request: Request):
    user_id, _ = _user_ctx(request)
    return monitor_mgr.get_status(user_id, idx)


# ============================================================
# API - 全局设置
# ============================================================
@app.get("/api/settings")
def get_settings(request: Request):
    _, cfg = _user_ctx(request)
    return {
        "webhooks": cfg["webhooks"],
        "cleanup": cfg["cleanup"],
    }


@app.put("/api/settings")
def update_settings(request: Request, data: dict):
    user_id, cfg = _user_ctx(request)
    if "webhooks" in data:
        cfg["webhooks"] = data["webhooks"]
    if "cleanup" in data:
        cfg["cleanup"] = data["cleanup"]
    save_user_config(user_id, cfg)
    logger.info(f"[user={user_id}] 已更新设置")
    return {"status": "ok"}


@app.post("/api/cleanup")
def run_cleanup_now(request: Request):
    """立即执行历史消息清理（仅当前用户数据，按本人保留天数设置，清理前自动备份）"""
    user_id, cfg = _user_ctx(request)
    clean = cfg.get("cleanup") or {}
    keep_days = int(clean.get("keep_days") or 30)
    if keep_days <= 0:
        keep_days = 30
    backup_database()
    cutoff = (datetime.now(SHANGHAI_TZ) - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    try:
        cur = conn.execute("DELETE FROM history WHERE user_id = ? AND ts < ?", (user_id, cutoff))
        deleted = cur.rowcount
        conn.commit()
    except Exception:
        deleted = 0
    finally:
        conn.close()
    logger.info(f"[user={user_id}] 已清理过期历史 {deleted} 条")
    return {"status": "ok", "deleted": deleted}


# ============================================================
# API - 历史消息
# ============================================================
@app.get("/api/history")
def get_history(request: Request, account_idx: int = -1, page: int = 1, page_size: int = 20,
                date_from: str = "", date_to: str = "", keyword: str = "", star_only: int = 0):
    user_id, _ = _user_ctx(request)
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    conditions = ["user_id = ?"]
    params = [user_id]
    if account_idx >= 0:
        conditions.append("account_idx = ?")
        params.append(account_idx)
    if date_from:
        conditions.append("ts >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("ts <= ?")
        params.append(date_to + " 23:59:59")
    if star_only:
        conditions.append("starred = 1")
    if keyword:
        kw_fts_ids = None
        if len(keyword.strip()) >= 3:
            # 优先用 FTS5 全文索引加速（trigram，适合中文），失败则回退 LIKE
            try:
                found = conn.execute(
                    "SELECT rowid FROM history_fts WHERE history_fts MATCH ?",
                    (f'"{keyword.strip()}"',)
                ).fetchall()
                kw_fts_ids = [r[0] for r in found]
            except Exception:
                kw_fts_ids = None
        if kw_fts_ids:
            if kw_fts_ids:
                placeholders = ",".join("?" * len(kw_fts_ids))
                conditions.append(f"id IN ({placeholders})")
                params.extend(kw_fts_ids)
            else:
                conditions.append("1=0")  # 全文索引无匹配，直接返回空
        else:
            conditions.append("(text LIKE ? OR sender_name LIKE ? OR chat_title LIKE ?)")
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw])
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    # 总数
    count = conn.execute(f"SELECT COUNT(*) FROM history {where}", params).fetchone()[0]
    # 分页
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT id, ts, account_name, account_idx, chat_title, chat_id, sender_name, sender_id, text, has_media, media_type, media_path, rule_remark, topic_id, topic_name, starred FROM history {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [page_size, offset]
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        items.append({
            "id": r[0], "ts": r[1], "account_name": r[2], "account_idx": r[3],
            "chat_title": r[4], "chat_id": r[5], "sender_name": r[6], "sender_id": r[7],
            "text": r[8], "has_media": bool(r[9]), "media_type": r[10], "media_path": r[11] or "", "rule_remark": r[12],
            "topic_id": r[13] or "", "topic_name": r[14] or "", "starred": 1 if r[15] else 0,
        })
    return {"items": items, "total": count, "page": page, "page_size": page_size}


# ============================================================
# API - 批量导出历史消息（CSV，UTF-8 BOM 便于 Excel 打开）
# ============================================================
@app.get("/api/history/export")
def export_history(request: Request, account_idx: int = -1, date_from: str = "", date_to: str = "", keyword: str = ""):
    user_id, _ = _user_ctx(request)
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    conditions = ["user_id = ?"]
    params = [user_id]
    if account_idx >= 0:
        conditions.append("account_idx = ?")
        params.append(account_idx)
    if date_from:
        conditions.append("ts >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("ts <= ?")
        params.append(date_to + " 23:59:59")
    if keyword:
        conditions.append("(text LIKE ? OR sender_name LIKE ? OR chat_title LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"SELECT ts, account_name, chat_title, topic_name, sender_name, sender_id, text, has_media, media_type, media_path, rule_remark FROM history {where} ORDER BY id DESC",
        params
    ).fetchall()
    conn.close()

    sio = io.StringIO()
    writer = csv.writer(sio)
    writer.writerow(["时间", "账号", "群组", "话题", "发送者", "发送者ID", "规则", "媒体", "内容"])
    for r in rows:
        text = (r[6] or "").replace("\r", " ").replace("\n", " ")
        media = MEDIA_CN.get(r[8], r[8]) if r[7] else ""
        writer.writerow([r[0], r[1], r[2] or "", r[3] or "", r[4] or "", r[5] or "", r[10] or "", media, text])
    data = b"\xef\xbb\xbf" + sio.getvalue().encode("utf-8")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="history_export.csv"'})


# ============================================================
# API - 消息收藏/取消收藏
# ============================================================
@app.post("/api/history/{history_id}/star")
def toggle_history_star(history_id: int, request: Request):
    """切换历史消息的收藏状态，返回切换后的状态"""
    user_id, _ = _user_ctx(request)
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    row = conn.execute("SELECT starred FROM history WHERE id = ? AND user_id = ?", (history_id, user_id)).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(404, "消息不存在")
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE history SET starred = ? WHERE id = ?", (new_val, history_id))
    conn.commit()
    conn.close()
    return {"id": history_id, "starred": new_val}


# ============================================================
# API - 统计数据
# ============================================================
@app.get("/api/stats")
def get_stats(request: Request):
    """获取统计数据：概览、趋势、账号分布、群组排行（按当前用户隔离）"""
    user_id, cfg = _user_ctx(request)
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    
    # 1. 概览数据
    today = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    yesterday = (datetime.now(SHANGHAI_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    
    today_messages = conn.execute(
        "SELECT COUNT(*) FROM history WHERE user_id = ? AND ts LIKE ?", (user_id, f"{today}%")
    ).fetchone()[0]
    
    yesterday_messages = conn.execute(
        "SELECT COUNT(*) FROM history WHERE user_id = ? AND ts LIKE ?", (user_id, f"{yesterday}%")
    ).fetchone()[0]
    
    # 活跃账号数（有消息的账号）
    active_accounts = conn.execute(
        "SELECT COUNT(DISTINCT account_idx) FROM history WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    
    # 总账号数
    total_accounts = len(cfg["accounts"])
    
    # 监控群组数（有消息的群组）
    monitored_chats = conn.execute(
        "SELECT COUNT(DISTINCT chat_id) FROM history WHERE user_id = ? AND chat_id IS NOT NULL AND chat_id != ''", (user_id,)
    ).fetchone()[0]
    
    # 推送成功率（从 push_logs 表读取真实数据）
    push_total = conn.execute(
        "SELECT COUNT(*) FROM push_logs WHERE user_id = ? AND ts LIKE ?", (user_id, f"{today}%")
    ).fetchone()[0]
    push_success = conn.execute(
        "SELECT COUNT(*) FROM push_logs WHERE user_id = ? AND ts LIKE ? AND status = 'success'", (user_id, f"{today}%")
    ).fetchone()[0]
    push_success_rate = 0 if push_total == 0 else round(push_success / push_total * 100, 1)
    
    overview = {
        "today_messages": today_messages,
        "yesterday_messages": yesterday_messages,
        "active_accounts": active_accounts,
        "total_accounts": total_accounts,
        "monitored_chats": monitored_chats,
        "push_total": push_total,
        "push_success": push_success,
        "push_success_rate": push_success_rate,
    }
    
    # 2. 近7天每日消息量
    daily_messages = []
    for i in range(6, -1, -1):
        date = (datetime.now(SHANGHAI_TZ) - timedelta(days=i)).strftime("%Y-%m-%d")
        count = conn.execute(
            "SELECT COUNT(*) FROM history WHERE user_id = ? AND ts LIKE ?", (user_id, f"{date}%")
        ).fetchone()[0]
        daily_messages.append({"date": date[5:], "count": count})  # 只显示 MM-DD
    
    # 3. 各账号消息量
    account_rows = conn.execute(
        "SELECT account_name, COUNT(*) as cnt FROM history WHERE user_id = ? GROUP BY account_idx ORDER BY cnt DESC", (user_id,)
    ).fetchall()
    account_messages = [{"account_name": r[0], "count": r[1]} for r in account_rows]
    
    # 4. 活跃群组 TOP10
    chat_rows = conn.execute(
        "SELECT chat_title, COUNT(*) as cnt FROM history WHERE user_id = ? AND chat_id IS NOT NULL AND chat_id != '' GROUP BY chat_id ORDER BY cnt DESC LIMIT 10", (user_id,)
    ).fetchall()
    top_chats = [{"chat_title": r[0] or "未知群组", "count": r[1]} for r in chat_rows]
    
    # 5. 规则命中率排行榜（从 history 表统计 rule_remark）
    rule_rows = conn.execute(
        "SELECT rule_remark, COUNT(*) as cnt FROM history WHERE user_id = ? AND rule_remark != '' GROUP BY rule_remark ORDER BY cnt DESC LIMIT 10", (user_id,)
    ).fetchall()
    rule_hits = [{"rule_name": r[0], "count": r[1]} for r in rule_rows]
    
    # 6. 推送渠道分布（从 push_logs 表统计 channel_type）
    channel_rows = conn.execute(
        "SELECT channel_type, COUNT(*) as cnt FROM push_logs WHERE user_id = ? GROUP BY channel_type ORDER BY cnt DESC", (user_id,)
    ).fetchall()
    channel_distribution = [{"channel": r[0], "count": r[1]} for r in channel_rows]
    
    conn.close()
    
    return {
        "overview": overview,
        "daily_messages": daily_messages,
        "account_messages": account_messages,
        "top_chats": top_chats,
        "rule_hits": rule_hits,
        "channel_distribution": channel_distribution,
    }


# ============================================================
# API - 检查登录状态
# ============================================================
@app.get("/api/check_auth")
async def check_auth(request: Request):
    user = verify_session(request)
    if not user:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "username": user.get("username"),
        "role": user.get("role", "user"),
        "banned": bool(user.get("banned", 0)),
        "ban_reason": user.get("ban_reason", ""),
    }


@app.get("/api/health")
async def health(request: Request):
    """健康检查（无需登录）"""
    running = sum(1 for m in monitor_mgr.monitors.values() if m.running)
    return {
        "status": "ok",
        "time": datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "running_monitors": running,
    }


@app.post("/api/admin/restart")
async def admin_restart(request: Request):
    """一键重启服务（仅管理员）：退出当前进程，由 PM2 自动拉起"""
    _require_admin(request)
    import threading
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return {"status": "ok", "message": "服务将在 0.5 秒后重启"}


# ============================================================
# WebSocket - 实时日志
# ============================================================
@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    token = websocket.cookies.get("session_token", "")
    user = get_user_by_token(token)
    if user is None or user["banned"]:
        await websocket.close(code=1008)
        return
    await broadcaster.add_connection(websocket)
    try:
        while True:
            await websocket.receive_text()  # 保持连接，接收心跳
    except WebSocketDisconnect:
        broadcaster.remove_connection(websocket)


# ============================================================
# 前端页面
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not verify_session(request):
        return RedirectResponse(url="/login")
    html_path = BASE_DIR / "templates" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>请先创建 templates/index.html</h1>")


@app.get("/favicon.ico")
async def favicon():
    svg_path = BASE_DIR / "Telegram_logo.svg"
    if svg_path.exists():
        return FileResponse(svg_path, media_type="image/svg+xml")
    return HTMLResponse("")


@app.get("/logo.svg")
async def logo():
    svg_path = BASE_DIR / "Telegram_logo.svg"
    if svg_path.exists():
        return FileResponse(svg_path, media_type="image/svg+xml")
    return HTMLResponse("")


@app.get("/media/{filename}")
async def get_media(filename: str):
    """提供监控消息媒体文件（图片/视频/文件）"""
    media_dir = BASE_DIR / "media"
    file_path = (media_dir / filename).resolve()
    # 防目录穿越
    if not str(file_path).startswith(str(media_dir.resolve())) or not file_path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(file_path))


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Telegram 多账号监控 - Web 管理后台")
    print("=" * 60)
    print(f"配置文件: {CONFIG_PATH}")
    print()
    print("启动服务: http://localhost:8000")
    print("手机访问: http://<电脑IP>:8000（需在同一局域网）")
    print()
    print("按键 Ctrl+C 停止服务")
    print("=" * 60)
    start_backup_scheduler()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
