#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 多账号监控工具 - Web 管理后台 (FastAPI)
支持多账号同时监控，浏览器管理，实时日志推送
"""

import asyncio
import json
import logging
import sqlite3
import hashlib
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
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
                "telegram_bot_token": "",
                "telegram_chat_id": "",
                "url": "",
            }
        ],
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
def make_session_token(username: str) -> str:
    raw = f"{username}:{secrets.token_hex(16)}:{datetime.now(SHANGHAI_TZ).timestamp()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_session(request: Request) -> bool:
    token = request.cookies.get("session_token", "")
    if not token:
        return False
    stored = config.cfg.get("session_token", "")
    return token == stored and bool(stored)


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
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 迁移：旧库补充 media_path 字段
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(history)").fetchall()]
        if "media_path" not in cols:
            conn.execute("ALTER TABLE history ADD COLUMN media_path TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()


def save_history(account_name: str, account_idx: int, chat_title: str, chat_id, sender_name: str, sender_id, text: str, has_media: bool, media_type: str, rule_remark: str, media_path: str = ""):
    try:
        conn = sqlite3.connect(str(HISTORY_DB_PATH))
        conn.execute(
            "INSERT INTO history (ts, account_name, account_idx, chat_title, chat_id, sender_name, sender_id, text, has_media, media_type, media_path, rule_remark) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                str(account_name), int(account_idx),
                str(chat_title or ""), str(chat_id or ""),
                str(sender_name or ""), str(sender_id or ""),
                str(text or ""), 1 if has_media else 0,
                str(media_type or ""), str(media_path or ""), str(rule_remark or ""),
            ]
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# 启动时初始化
init_history_db()


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
        with urllib.request.urlopen(req, timeout=30) as resp:
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


async def send_webhook_alerts(alert_text: str, webhooks: list, media_data: Optional[dict] = None):
    for idx, wh in enumerate(webhooks):
        url = wh.get("url", "").strip()
        if not url or not wh.get("enabled", True):
            continue
        bot_token = wh.get("telegram_bot_token", "").strip()
        bot_chat_id = wh.get("telegram_chat_id", "").strip()
        if bot_token and bot_chat_id:
            text = alert_text[:4096]
            params = urllib.parse.urlencode({"chat_id": bot_chat_id, "text": text, "parse_mode": "Markdown"})
            bot_url = f"https://api.telegram.org/bot{bot_token}/sendMessage?{params}"
            ok, err_msg = await asyncio.to_thread(_do_webhook_post, bot_url, {})
            if ok:
                logger.info(f"  Webhook [{idx}] (Bot) 推送成功")
            else:
                logger.warning(f"  Webhook [{idx}] (Bot) 推送失败: {err_msg[:200]}")
            continue
        # 企业微信机器人：支持媒体推送
        if "qyapi.weixin.qq.com" in url:
            # 先发送文本消息
            payload = {"msgtype": "markdown", "markdown": {"content": alert_text}}
            ok, err_msg = await asyncio.to_thread(_do_webhook_post, url, payload)
            if ok:
                logger.info(f"  Webhook [{idx}] 文本推送成功")
            else:
                logger.warning(f"  Webhook [{idx}] 文本推送失败: {err_msg[:200]}")
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
                        wecom_type = "video"
                    else:
                        wecom_type = "file"
                    if not file_bytes:
                        continue
                    file_size_mb = len(file_bytes) / (1024 * 1024)
                    if wecom_type == "image":
                        # 图片：直接用 base64 发送，无需上传
                        logger.info(f"  Webhook [{idx}] 发送图片 {filename} (size={file_size_mb:.2f}MB)")
                        ok2, err2 = await asyncio.to_thread(wecom_send_image_direct, url, file_bytes)
                        if ok2:
                            logger.info(f"  Webhook [{idx}] 图片推送成功 ({filename})")
                        else:
                            logger.warning(f"  Webhook [{idx}] 图片推送失败: {err2[:200]}")
                    else:
                        # 视频/文件：上传后用 media_id 发送
                        logger.info(f"  Webhook [{idx}] 上传 {filename} (type={wecom_type}, size={file_size_mb:.2f}MB)")
                        mid, err_msg = await asyncio.to_thread(wecom_upload_media, url, file_bytes, filename, wecom_type)
                        if err_msg:
                            logger.warning(f"  Webhook [{idx}] {err_msg}")
                        if mid:
                            ok2, err2 = await asyncio.to_thread(wecom_send_media, url, mid, wecom_type)
                            if ok2:
                                logger.info(f"  Webhook [{idx}] 媒体推送成功 ({filename})")
                            else:
                                logger.warning(f"  Webhook [{idx}] 媒体推送失败: {err2[:200]}")
                        else:
                            logger.warning(f"  Webhook [{idx}] 媒体上传失败")
        else:
            payload = {"title": "Telegram监控告警", "text": alert_text, "source": "tg_monitor"}
            ok, err_msg = await asyncio.to_thread(_do_webhook_post, url, payload)
            if ok:
                logger.info(f"  Webhook [{idx}] 推送成功")
            else:
                logger.warning(f"  Webhook [{idx}] 推送失败: {err_msg[:200]}")


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


def format_alert(event, rule_remark, chat_title, sender_name):
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
    def __init__(self, account: dict, account_idx: int):
        self.account = account
        self.account_idx = account_idx
        self.client: Optional[TelegramClient] = None
        self.running = False
        self._stop_event = asyncio.Event()

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
        webhooks = config.get_webhooks()
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
                    matched = True
                    logger.info(f"[{account_name}] 收到消息: chat={chat_title}({chat_id}) sender={sender_name}({sender_id}) text={text[:50]}")
                    logger.info(f"[{account_name}] 规则 '{rule.get('remark', '')}': 匹配成功")
                    remark = rule.get("remark", "规则")
                    alert = format_alert(event, remark, chat_title, sender_name)
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
                                            export_gif(animation, gif_buf, fps=10)
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
                    save_history(account_name, self.account_idx, chat_title, chat_id,
                                 sender_name, sender_id, text, has_media, media_type, remark, media_path)
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
                        asyncio.ensure_future(send_webhook_alerts(alert, wh_list, media_data))
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
        self.monitors: dict[int, AsyncMonitor] = {}
        self.tasks: dict[int, asyncio.Task] = {}

    async def start_monitor(self, account_idx: int):
        if account_idx in self.monitors and self.monitors[account_idx].running:
            return {"status": "already_running"}
        accounts = config.get_accounts()
        if account_idx < 0 or account_idx >= len(accounts):
            raise HTTPException(404, "账号不存在")
        # 先检查 session 是否有效
        chk = check_session(accounts[account_idx])
        if not chk["valid"]:
            return {"status": "error", "message": "session 未登录，请先点击「登录」"}
        monitor = AsyncMonitor(accounts[account_idx], account_idx)
        self.monitors[account_idx] = monitor
        self.tasks[account_idx] = asyncio.create_task(monitor.start())
        logger.info(f"[{accounts[account_idx].get('remark', '')}] 监控任务已创建")
        return {"status": "started"}

    async def stop_monitor(self, account_idx: int):
        if account_idx in self.monitors:
            await self.monitors[account_idx].stop()
            if account_idx in self.tasks:
                self.tasks[account_idx].cancel()
                del self.tasks[account_idx]
            del self.monitors[account_idx]
            return {"status": "stopped"}
        return {"status": "not_running"}

    def is_running(self, account_idx: int) -> bool:
        m = self.monitors.get(account_idx)
        if m is None:
            return False
        if not m.running:
            # 可能还在连接中，检查 task 是否活着
            t = self.tasks.get(account_idx)
            if t is not None and not t.done():
                return False  # 还在连接中，不删除
            # task 已结束，清理
            if account_idx in self.tasks:
                del self.tasks[account_idx]
            if account_idx in self.monitors:
                del self.monitors[account_idx]
            return False
        # 检查 task 是否还活着
        t = self.tasks.get(account_idx)
        if t is None or t.done():
            m.running = False
            if account_idx in self.tasks:
                del self.tasks[account_idx]
            if account_idx in self.monitors:
                del self.monitors[account_idx]
            return False
        return True

    def get_status(self, account_idx: int) -> dict:
        return {
            "idx": account_idx,
            "running": self.is_running(account_idx),
        }

    def all_status(self) -> list[dict]:
        result = []
        accounts = config.get_accounts()
        for i in range(len(accounts)):
            result.append({
                "idx": i,
                "running": self.is_running(i),
            })
        return result


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
    if config.is_admin_configured():
        html_path = BASE_DIR / "templates" / "login.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
    else:
        html_path = BASE_DIR / "templates" / "setup.html"
        if html_path.exists():
            return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>请先创建 templates/login.html</h1>")


@app.post("/api/login")
async def login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not config.is_admin_configured():
        # 首次设置密码
        config.set_admin_password(username, password)
        token = make_session_token(username)
        config.cfg["session_token"] = token
        config.save()
        return {"status": "ok", "token": token}
    if config.verify_admin(username, password):
        token = make_session_token(username)
        config.cfg["session_token"] = token
        config.save()
        return {"status": "ok", "token": token}
    return {"status": "error", "message": "用户名或密码错误"}


@app.post("/api/setup")
async def setup(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return {"status": "error", "message": "用户名和密码不能为空"}
    if len(password) < 6:
        return {"status": "error", "message": "密码长度至少6位"}
    config.set_admin_password(username, password)
    token = make_session_token(username)
    config.cfg["session_token"] = token
    config.save()
    return {"status": "ok", "message": "管理员账号设置成功，请重新登录"}


@app.post("/api/logout")
async def logout(request: Request):
    config.cfg["session_token"] = ""
    config.save()
    return {"status": "ok"}


# 全局鉴权中间件：未登录跳转 /login
from fastapi import Depends
from fastapi.security import APIKeyCookie
from typing import Callable

security = APIKeyCookie(name="session_token", auto_error=False)

async def require_auth(request: Request, token: str = Depends(security)) -> bool:
    stored = config.cfg.get("session_token", "")
    if not stored or not token or token != stored:
        return False
    return True


# ============================================================
# API - 账号管理
# ============================================================
@app.get("/api/accounts")
async def list_accounts():
    accounts = config.get_accounts()
    statuses = monitor_mgr.all_status()
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
def add_account(data: dict):
    account = {
        "remark": data.get("remark", "未命名"),
        "phone": data.get("phone", ""),
        "api_id": data.get("api_id", 2040),
        "api_hash": data.get("api_hash", ""),
        "proxy": data.get("proxy", {"scheme": "", "host": "", "port": 0, "username": "", "password": ""}),
        "rules": [],
    }
    config.add_account(account)
    logger.info(f"已添加账号: {account['remark']} ({account['phone']})")
    return {"status": "ok", "idx": len(config.get_accounts()) - 1}


@app.put("/api/accounts/{idx}")
def update_account(idx: int, data: dict):
    accounts = config.get_accounts()
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
    config.update_account(idx, acc)
    logger.info(f"已更新账号: {acc.get('remark', '')}")
    return {"status": "ok"}


@app.delete("/api/accounts/{idx}")
def delete_account(idx: int):
    accounts = config.get_accounts()
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    # 先停止监控
    asyncio.create_task(monitor_mgr.stop_monitor(idx))
    config.delete_account(idx)
    logger.info(f"已删除账号: {accounts[idx].get('remark', '')}")
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
async def login_start(idx: int):
    accounts = config.get_accounts()
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
    if idx in _login_states:
        old = _login_states[idx]
        try:
            await old.get("client", None).disconnect()
        except Exception:
            pass
        del _login_states[idx]

    state = {
        "client": None,
        "done_event": asyncio.Event(),
        "code_event": asyncio.Event(),
        "code": "",
        "result": None,
        "error": None,
    }
    _login_states[idx] = state

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
        del _login_states[idx]
        return {"status": "ok", "message": state["result"]}
    if state.get("error"):
        err = state["error"]
        del _login_states[idx]
        return {"status": "error", "message": err}

    return {"status": "need_code", "message": "请输入验证码"}


@app.post("/api/accounts/{idx}/login/code")
async def login_code(idx: int, data: dict):
    state = _login_states.get(idx)
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
        del _login_states[idx]
        return {"status": "ok", "message": state["result"]}
    if state.get("error"):
        err = state["error"]
        del _login_states[idx]
        return {"status": "error", "message": err}

    del _login_states[idx]
    return {"status": "error", "message": "登录失败，请重试"}


@app.post("/api/accounts/{idx}/login/password")
async def login_password(idx: int, data: dict):
    state = _login_states.get(idx)
    if not state:
        raise HTTPException(400, "没有进行中的登录，请先点击登录")
    password = data.get("password", "").strip()
    if not password:
        raise HTTPException(400, "密码不能为空")

    client = state.get("client")
    if not client:
        del _login_states[idx]
        raise HTTPException(400, "登录会话已过期，请重新登录")

    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        msg = f"登录成功: {get_display_name(me)} (id={me.id})"
        logger.info(f"[{config.get_accounts()[idx].get('remark', '')}] {msg}")
        await client.disconnect()
        del _login_states[idx]
        return {"status": "ok", "message": msg}
    except Exception as e:
        return {"status": "error", "message": f"密码错误: {e}"}


# ============================================================
# API - 规则管理
# ============================================================
@app.get("/api/accounts/{idx}/rules")
def list_rules(idx: int):
    accounts = config.get_accounts()
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    rules = accounts[idx].get("rules", [])
    result = []
    for ri, rule in enumerate(rules):
        result.append({"idx": ri, **rule})
    return result


@app.post("/api/accounts/{idx}/rules")
def add_rule(idx: int, data: dict):
    accounts = config.get_accounts()
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
    config.update_account(idx, accounts[idx])
    logger.info(f"已添加规则: {rule['remark']}")
    return {"status": "ok"}


@app.put("/api/accounts/{idx}/rules/{ridx}")
def update_rule(idx: int, ridx: int, data: dict):
    accounts = config.get_accounts()
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
    config.update_account(idx, accounts[idx])
    logger.info(f"已更新规则: {rule.get('remark', '')}")
    return {"status": "ok"}


@app.delete("/api/accounts/{idx}/rules/{ridx}")
def delete_rule(idx: int, ridx: int):
    accounts = config.get_accounts()
    if idx < 0 or idx >= len(accounts):
        raise HTTPException(404, "账号不存在")
    rules = accounts[idx].get("rules", [])
    if ridx < 0 or ridx >= len(rules):
        raise HTTPException(404, "规则不存在")
    del rules[ridx]
    accounts[idx]["rules"] = rules
    config.update_account(idx, accounts[idx])
    logger.info(f"已删除规则 #{ridx}")
    return {"status": "ok"}


# ============================================================
# API - 监控控制
# ============================================================
@app.post("/api/accounts/{idx}/start")
async def start_monitor(idx: int):
    return await monitor_mgr.start_monitor(idx)


@app.post("/api/accounts/{idx}/stop")
async def stop_monitor(idx: int):
    return await monitor_mgr.stop_monitor(idx)


@app.get("/api/accounts/{idx}/status")
def get_status(idx: int):
    return monitor_mgr.get_status(idx)


# ============================================================
# API - 全局设置
# ============================================================
@app.get("/api/settings")
def get_settings():
    return {
        "webhooks": config.get_webhooks(),
    }


@app.put("/api/settings")
def update_settings(data: dict):
    if "webhooks" in data:
        config.set_webhooks(data["webhooks"])
    logger.info("已更新全局设置")
    return {"status": "ok"}


# ============================================================
# API - 历史消息
# ============================================================
@app.get("/api/history")
def get_history(account_idx: int = -1, page: int = 1, page_size: int = 20,
                date_from: str = "", date_to: str = "", keyword: str = ""):
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    conditions = []
    params = []
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
    # 总数
    count = conn.execute(f"SELECT COUNT(*) FROM history {where}", params).fetchone()[0]
    # 分页
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT id, ts, account_name, account_idx, chat_title, chat_id, sender_name, sender_id, text, has_media, media_type, media_path, rule_remark FROM history {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [page_size, offset]
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        items.append({
            "id": r[0], "ts": r[1], "account_name": r[2], "account_idx": r[3],
            "chat_title": r[4], "chat_id": r[5], "sender_name": r[6], "sender_id": r[7],
            "text": r[8], "has_media": bool(r[9]), "media_type": r[10], "media_path": r[11] or "", "rule_remark": r[12],
        })
    return {"items": items, "total": count, "page": page, "page_size": page_size}


# ============================================================
# API - 检查登录状态
# ============================================================
@app.get("/api/check_auth")
async def check_auth(request: Request):
    return {"authenticated": verify_session(request)}


# ============================================================
# WebSocket - 实时日志
# ============================================================
@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
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
    print("按 Ctrl+C 停止服务")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
