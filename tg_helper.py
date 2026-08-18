#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
辅助工具：列出你加入的所有群/频道，以及打印最近消息的发送者ID
用法:
  1. 先配置 config.json 的 api_id/api_hash/phone/proxy
  2. 运行: python tg_helper.py chats [-a 账号索引]   -> 列出所有群/频道的 id 和标题
  3. 运行: python tg_helper.py users -n 50 [-a 账号索引]  -> 抓取最近N条消息，打印发送者信息
  4. 运行: python tg_helper.py find "关键词" [-a 账号索引]  -> 查找特定群的chat_id
  5. 运行: python tg_helper.py members -c <群ID> [-a 账号索引]  -> 列出群成员
  账号索引: 0=第一个账号(默认), 1=第二个账号, 以此类推
"""

import asyncio
import json
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import AuthRestartError
from telethon.utils import get_display_name

CONFIG_PATH = Path(__file__).parent / "config.json"


def _session_path(phone: str) -> str:
    return str(Path(__file__).parent / f"session_{phone.replace('+', '').replace(' ', '')}")


def load_cfg(account_idx: int = 0):
    if not CONFIG_PATH.exists():
        print(f"[错误] 找不到配置文件: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 兼容多账号格式（version 2）：从指定账号取配置
    if "accounts" in cfg:
        if len(cfg["accounts"]) > account_idx:
            acct = cfg["accounts"][account_idx]
            cfg["api_id"] = acct.get("api_id", cfg.get("api_id"))
            cfg["api_hash"] = acct.get("api_hash", cfg.get("api_hash"))
            cfg["phone"] = acct.get("phone", cfg.get("phone"))
            cfg["proxy"] = acct.get("proxy", cfg.get("proxy"))
        else:
            print(f"[错误] config.json 中共 {len(cfg['accounts'])} 个账号（索引 0-{len(cfg['accounts'])-1}），你指定的 -a {account_idx} 超出范围")
            sys.exit(1)
    required = ["api_id", "api_hash", "phone"]
    for k in required:
        val = cfg.get(k)
        if not val or (isinstance(val, str) and ("你的" in val or "填这里" in val)):
            print(f"[错误] config.json 中的 {k} 还没填，请先填写正确值")
            sys.exit(1)
    return cfg


def build_client(cfg: dict, request_timeout: int = 20) -> TelegramClient:
    """构建带代理的 Telethon 客户端，并做依赖与配置检查"""
    proxy_cfg = cfg.get("proxy") or {}
    scheme = (proxy_cfg.get("scheme") or "").strip().lower()
    host = (proxy_cfg.get("host") or "").strip()
    port = proxy_cfg.get("port") or 0

    client_kwargs = dict(
        session=_session_path(cfg.get("phone", "")),
        api_id=cfg["api_id"],
        api_hash=cfg["api_hash"],
        request_retries=3,
        connection_retries=3,
        timeout=request_timeout,
    )

    if scheme and host and port:
        # Telethon proxy tuple: (scheme, host, port, rdns, username?, password?)
        proxy_tuple = (scheme, host, int(port), True)
        user = proxy_cfg.get("username")
        if user:
            proxy_tuple += (str(user), str(proxy_cfg.get("password") or ""))
        client_kwargs["proxy"] = proxy_tuple
        print(f"[代理] 已启用: {scheme}://{host}:{port}")
        # 确保 pysocks 已装（socks5 模式需要）
        if scheme.startswith("socks"):
            try:
                import socks  # noqa: F401
            except ImportError:
                print("[警告] 缺少 socks 代理依赖，请运行: pip install pysocks python-socks[asyncio]")
    else:
        print("[代理] 未启用（若在中国大陆使用，必须填 proxy 配置，否则会超时）")

    return TelegramClient(**client_kwargs)


def print_troubleshoot(err):
    print("\n" + "=" * 60)
    print("❌ 连接 Telegram 失败 (TimeoutError / 连接超时)")
    print("=" * 60)
    print("原因: 你的电脑无法直连 Telegram 服务器 (中国大陆必须走代理)")
    print("\n解决: 打开 config.json，找到 proxy 段，按你本地的代理软件填写:")
    print("")
    print("  Clash / Clash Verge (默认值):")
    print('    "scheme": "socks5",')
    print('    "host": "127.0.0.1",')
    print('    "port": 7890,')
    print("")
    print("  V2RayN (默认值):")
    print('    "scheme": "socks5",')
    print('    "host": "127.0.0.1",')
    print('    "port": 10808,')
    print("")
    print("  Shadowsocks / 其他:")
    print("    在代理软件里查看『本地监听端口』和『协议类型』")
    print("")
    print("⚠️  填完 config.json 保存后，必须重新运行本脚本")
    print(f"\n原始错误: {type(err).__name__}: {err}")
    print("=" * 60)


# ============================================================
# 命令
# ============================================================
async def cmd_chats(account_idx: int = 0):
    cfg = load_cfg(account_idx)
    client = build_client(cfg)
    await client.start(phone=cfg["phone"])
    try:
        me = await client.get_me()
        print(f"✅ 登录成功: {get_display_name(me)} (@{me.username or '无'}, id={me.id})")
        print(f"\n{'ID':<20} {'类型':<10} {'标题/名称'}")
        print("-" * 90)
        async for d in client.iter_dialogs():
            t = type(d.entity).__name__
            name = d.name or "(无标题)"
            print(f"{d.id:<20} {t:<10} {name}")
    finally:
        await client.disconnect()
    print("\n💡 提示: 把上面的 ID 填入 config.json 的 monitor_targets[*].chat_ids 数组中")


async def cmd_users(limit: int = 50, chat_id=None, account_idx: int = 0):
    cfg = load_cfg(account_idx)
    client = build_client(cfg)
    await client.start(phone=cfg["phone"])
    try:
        me = await client.get_me()
        print(f"✅ 登录成功: {get_display_name(me)}")
        target = chat_id
        target_desc = f"指定聊天 {chat_id}"
        if not target:
            target = "me"
            target_desc = "Saved Messages"
        print(f"\n🔍 在【{target_desc}】中抓取最近 {limit} 位发送者:")
        print(f"{'User ID':<15} {'Username':<20} {'显示名':<25} 最近消息预览")
        print("-" * 95)

        seen = set()
        count = 0
        async for m in client.iter_messages(target, limit=limit * 10):
            if count >= limit:
                break
            if not m.sender_id or m.sender_id in seen:
                continue
            seen.add(m.sender_id)
            try:
                sender = await m.get_sender()
            except Exception:
                sender = None
            uid = m.sender_id
            uname = getattr(sender, "username", "") or ""
            dname = get_display_name(sender) if sender else "(未知)"
            text = (m.text or "(媒体消息)").replace("\n", " ")[:35]
            print(f"{uid:<15} @{uname:<18} {dname:<25} {text}")
            count += 1
    finally:
        await client.disconnect()

    print("\n💡 提示: 把 User ID 填到 target_user_ids（推荐），或把 @xxx 填到 target_usernames")


async def cmd_find(keyword: str, account_idx: int = 0):
    cfg = load_cfg(account_idx)
    client = build_client(cfg)
    await client.start(phone=cfg["phone"])
    try:
        kw = keyword.lower()
        found = False
        print(f"\n🔍 搜索标题包含 '{keyword}' 的对话:\n")
        print(f"{'ID':<20} {'标题'}")
        print("-" * 70)
        async for d in client.iter_dialogs():
            if kw in (d.name or "").lower():
                print(f"{d.id:<20} {d.name}")
                found = True
        if not found:
            print("(未找到匹配结果)")
    finally:
        await client.disconnect()


async def cmd_members(chat_id: int, limit: int = 200, account_idx: int = 0):
    """列出指定群/频道的所有成员（包括未发言的成员）"""
    cfg = load_cfg(account_idx)
    client = build_client(cfg)
    await client.start(phone=cfg["phone"])
    try:
        me = await client.get_me()
        print(f"✅ 登录成功: {get_display_name(me)}")
        entity = await client.get_entity(chat_id)
        print(f"\n📋 【{getattr(entity, 'title', entity.id)}】的成员列表:\n")
        print(f"{'User ID':<15} {'Username':<20} {'显示名':<25} {'手机号':<15}")
        print("-" * 80)
        count = 0
        async for p in client.iter_participants(entity, limit=limit):
            uid = p.id
            uname = p.username or ""
            dname = get_display_name(p)
            phone = p.phone or ""
            print(f"{uid:<15} @{uname:<18} {dname:<25} {phone:<15}")
            count += 1
        print(f"\n共 {count} 位成员")
    finally:
        await client.disconnect()


def _parse_account_idx(args: list[str]) -> tuple[int, list[str]]:
    """从参数列表中提取 -a/--account 参数，返回 (account_idx, 剩余参数)"""
    rest = []
    idx = 0
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a in ("-a", "--account") and i + 1 < len(args):
            try:
                idx = int(args[i + 1])
            except ValueError:
                print(f"[警告] -a 后的数字无效，使用默认 0")
            skip_next = True
        else:
            rest.append(a)
    return idx, rest


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    account_idx, cmd_args = _parse_account_idx(args)
    cmd = cmd_args[0] if cmd_args else ""
    try:
        if cmd == "chats":
            asyncio.run(cmd_chats(account_idx))
        elif cmd == "users":
            limit = 50
            for i, a in enumerate(cmd_args[1:]):
                if a == "-n" and i + 1 < len(cmd_args[1:]):
                    try:
                        limit = int(cmd_args[1:][i + 1])
                    except ValueError:
                        print(f"[警告] -n 后面的数字无效，使用默认 50")
            asyncio.run(cmd_users(limit, None, account_idx))
        elif cmd == "members":
            chat_id = None
            member_limit = 200
            for i, a in enumerate(cmd_args[1:]):
                if a == "-c" and i + 1 < len(cmd_args[1:]):
                    try:
                        chat_id = int(cmd_args[1:][i + 1])
                    except ValueError:
                        print(f"[错误] -c 后面必须是数字 chat_id")
                        return
                if a == "-n" and i + 1 < len(cmd_args[1:]):
                    try:
                        member_limit = int(cmd_args[1:][i + 1])
                    except ValueError:
                        print(f"[警告] -n 后面的数字无效，使用默认 200")
            if not chat_id:
                print("用法: python tg_helper.py members -c <群ID> [-n 最多人数]")
                print("提示: 先用 python tg_helper.py chats 查看群ID")
                return
            asyncio.run(cmd_members(chat_id, member_limit, account_idx))
        elif cmd == "find":
            if len(cmd_args) < 2:
                print("用法: python tg_helper.py find <关键词>")
                return
            asyncio.run(cmd_find(cmd_args[1], account_idx))
        else:
            print(f"未知命令: {cmd}")
            print(__doc__)
    except (TimeoutError, ConnectionRefusedError, OSError, AuthRestartError) as e:
        print_troubleshoot(e)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n已取消")
    except Exception as e:
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg or "connect" in msg:
            print_troubleshoot(e)
        else:
            print(f"\n❌ 运行错误: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
