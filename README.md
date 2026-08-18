# Telegram 多账号监控工具

监控 Telegram 群或频道中**指定用户**的发言，支持**多账号同时监控**，自动转发到 Saved Messages + Webhook 手机推送（企业微信机器人 / Telegram Bot）。

---

## 项目文件

| 文件 | 作用 |
|------|------|
| [web_app.py](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/web_app.py) | **Web 管理后台** — FastAPI 后端 |
| [templates/index.html](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/templates/index.html) | Web 前端页面 |
| [templates/login.html](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/templates/login.html) | 登录页面 |
| [templates/setup.html](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/templates/setup.html) | 首次初始化管理员页面 |
| [tg_helper.py](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/tg_helper.py) | 辅助工具：查 chat_id / user_id |
| [config.json](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/config.json) | 配置文件（Web 界面自动管理） |
| [requirements.txt](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/requirements.txt) | 依赖列表 |
| [ecosystem.config.js](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/ecosystem.config.js) | PM2 进程管理配置 |
| [tg-monitor.nginx.conf](file:///e:/Code/Pythonproject/007Telegram群消息监控工具/tg-monitor.nginx.conf) | Nginx 反向代理配置示例 |
| session_xxx.session | 登录会话文件（每个账号各一个，自动生成） |
| history.db | 历史消息记录数据库（自动生成） |
| monitor.log | 运行日志 |

---

## Web 管理后台

### 1. 安装依赖

```bash
cd 007Telegram群消息监控工具
pip install -r requirements.txt
```

### 2. 启动服务

```bash
python web_app.py
```

默认访问：`http://localhost:8000`

首次访问会进入**初始化管理员页面**，设置用户名和密码后，跳转到登录页。

### 3. 功能说明

**用户登录：**
- 首次启动自动进入初始化页面，设置管理员账号密码
- 所有页面需登录后访问，防止未授权使用
- 点击右上角「退出」按钮退出登录，不影响监控任务

**账号管理：**
- 点击「添加账号」填入手机号、api_id、api_hash、代理地址
- 支持添加多个账号，每个账号独立 session 文件，互不影响
- 每个账号可独立配置代理

**登录流程：**
- 选择账号 → 点击「登录」
- 首次登录会弹出验证码输入框，输入 Telegram 收到的验证码
- 如果开启了两步验证，会自动弹出密码输入框
- 登录成功后 session 自动保存，下次无需再登录
- 页面自动刷新登录状态：🟢 监控中 / 🔵 已登录 / ⚪ 未登录

**监控规则：**
- 每个账号可添加多条规则
- **目标用户留空 = 监控该聊天所有消息**（适用于频道监控）
- 支持按关键词包含/排除过滤
- 必填项标记红星，输入框有默认示例提示
- 支持 Webhook 推送（企业微信机器人 / Telegram Bot 等）

**通知方式：**
| 方式 | 说明 |
|------|------|
| 转发到 Saved Messages | Telegram 收藏夹，所有设备同步 |
| Webhook 推送（企业微信机器人） | 国内可访问，支持文本 + 图片/视频/文件等媒体推送 |
| Webhook 推送（Telegram Bot） | 需 VPN 访问，文本推送 |

**历史消息记录：**
- 所有匹配的消息自动保存到 SQLite 数据库
- 在「历史消息」页面可按账号、日期、关键词筛选查看
- 支持分页浏览，方便回溯

**启动监控：**
- 账号登录后 → 点击「启动监控」
- 状态实时更新，无需手动刷新
- 日志仅显示匹配规则的消息

### 4. 获取 chat_id 和 user_id

**方法 A：查群成员列表（推荐，不需要对方说话）**

```bash
# 列出所有群/频道，拿到 ID
python tg_helper.py chats

# 指定账号（索引从 0 开始）
python tg_helper.py chats -a 1

# 列出群成员，拿到 user_id
python tg_helper.py members -c -100xxxxxxxx -n 500
```

**方法 B：转发消息给 @userinfobot**

在 Telegram APP 里转发目标用户的消息给 **@userinfobot**，它会回复：`ID: 123456789`

**方法 C：抓取最近消息中的发送者**

```bash
python tg_helper.py users -c -100xxxxxxxx -n 100
```

---

## Webhook 配置（手机推送）

### 企业微信机器人

1. 在企业微信群聊中添加「群机器人」，复制 Webhook URL
2. 在 Web 界面添加规则时，勾选「启用 Webhook」，填入 URL

### Telegram Bot 方式

1. 在 @BotFather 创建 Bot，拿到 token
2. 获取你的 user_id（通过 @userinfobot）
3. 在规则中填入：

| 字段 | 值 |
|------|-----|
| webhook_bot_token | `123456:ABC-DEF...` |
| webhook_chat_id | 你的 user_id |
| webhook_url | 留空 |

---

## 频道监控说明

频道发消息时，发送者是频道本身，看不到具体用户。

- **转发频道所有消息**：规则里 `目标用户` 留空，`聊天 ID` 填频道 ID
- **多账号监控**：如果一个号没加入频道，用另一个号登录并监控

### 获取频道 ID

```bash
# 列出所有对话（包括频道）
python tg_helper.py chats

# 指定第二个账号
python tg_helper.py chats -a 1
```

输出中频道也以 `-100xxxxxxxxxx` 格式显示，直接复制到规则的 `聊天 ID` 即可。

---

## 常用命令

| 命令 | 用途 |
|------|------|
| `python web_app.py` | 启动 Web 管理后台 |
| `python tg_helper.py chats` | 列出所有群/频道 ID |
| `python tg_helper.py chats -a 1` | 列出第二个账号的群/频道 |
| `python tg_helper.py members -c <群ID>` | 列出群成员，快速获取 user_id |
| `python tg_helper.py users -c <群ID> -n 100` | 抓取最近消息中的发送者 ID |
| `python tg_helper.py find <关键词>` | 搜索对话中的消息 |

---

## 常见问题

**Q: 登录多久掉线？**
A: 持续运行的情况下 session **永久有效**。长期间隔（1-2 个月）未使用可能过期，需要重新登录。

**Q: my.telegram.org 创建不了应用怎么办？**
A: 请自行搜索对应的公开 API 信息，或使用官方客户端 API 作为兜底方案。

**Q: 会封号吗？**
A: 只收消息不群发，正常使用无风险。

**Q: 多个账号怎么管理？**
A: Web 界面直接点「添加账号」，每个账号独立 session 和配置，互不影响。

**Q: 为什么启动监控后一直显示「启动中」？**
A: 首次连接 Telegram 服务器可能需要几秒钟，如果长时间无响应，请检查代理配置和网络连接。

**Q: 如何修改已有规则？**
A: 在 Web 界面点击规则旁的「编辑」按钮即可修改，修改后点击「保存」。

---

## 部署到服务器（Linux + PM2）

### 1. 上传项目到服务器

```bash
# 在服务器上
mkdir -p /opt/tg-monitor
# 将整个 007Telegram群消息监控工具 目录上传到 /opt/tg-monitor
# 可以使用 scp / rsync 或 git clone
```

### 2. 安装依赖

```bash
cd /opt/tg-monitor
pip install -r requirements.txt
```

### 3. 安装 PM2（如未安装）

```bash
npm install -g pm2
```

### 4. 启动服务

```bash
cd /opt/tg-monitor
pm2 start ecosystem.config.js
pm2 save
pm2 startup   # 设置开机自启
```

### 5. 配置 Nginx 反向代理（可选）

将 `tg-monitor.nginx.conf` 复制到 Nginx 配置目录：

```bash
# 方法一：使用 sites-available
sudo cp tg-monitor.nginx.conf /etc/nginx/sites-available/tg-monitor
sudo ln -s /etc/nginx/sites-available/tg-monitor /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# 方法二：直接使用 conf.d
sudo cp tg-monitor.nginx.conf /etc/nginx/conf.d/tg-monitor.conf
sudo nginx -t
sudo systemctl reload nginx
```

修改 `tg-monitor.nginx.conf` 中的 `server_name` 为你的域名或 IP 地址。

### 6. 配置 SSL 证书（推荐，使用 Let's Encrypt）

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

### 7. PM2 常用命令

```bash
pm2 status                    # 查看状态
pm2 logs tg-monitor           # 查看日志
pm2 restart tg-monitor        # 重启
pm2 stop tg-monitor           # 停止
pm2 delete tg-monitor         # 删除进程
```