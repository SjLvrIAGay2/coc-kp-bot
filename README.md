# COC7 QQ 守秘人

在 QQ 群里主持《克苏鲁的呼唤》第七版跑团。玩家可以用自然语言调查场景、与 NPC 交谈，也可以直接发掷骰命令。AI 负责叙事和提出裁定，程序负责检定、伤害、资源和行动记录。战役保存在 SQLite 中，群聊、玩家私聊和守秘人资料各有可见范围。

## 先跑一场离线演示

需要 Python 3.11 或更高版本。在仓库根目录打开 PowerShell，运行以下命令。脚本会创建虚拟环境、安装依赖，并为这次演示新建存档。

```powershell
.\scripts\start.ps1 -Mode demo
```

Linux 或 macOS 可以在虚拟环境里运行同一场演示。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation .
python -m cockp --database data/demo.sqlite3 demo
```

演示自带原创模组和三名虚构调查员，全程在本机运行。输出中的 `"phase": "ended"` 和 `"restart_verified": true` 表示战役已结束并验证了读档。演示回放在数据库旁的 `demo-artifacts/` 下。再次运行演示时请使用新的数据库文件名。

要自己操作，可运行 `python -m cockp terminal`，或在 PowerShell 运行 `.\scripts\start.ps1 -Mode terminal`。输入 `/help` 查看当前阶段可用的命令，输入 `exit` 退出。

## 群里怎样开团

1. 团主发送 `/建团 雾港调查`，上传模组并说明用途。
2. 玩家发送 `/车卡模板` 领取 Excel 人物卡，或上传自己的角色卡。机器人会提示补充缺项，团主完成审核后发送 `/开团`。
3. 开团后可以说「我检查门锁有没有被撬过的痕迹」，也可以用 `.r 2d6+3` 掷骰、用 `.ra 侦查` 检定。骰点由程序结算。
4. 发送 `/帮助 资料` 查询导入方式，发送 `/config` 查看团级设置。机器人只能处理平台实际推送的消息，使用仅推送提及消息的 QQ 设置时，请先 @ 机器人。

支持 TXT、Markdown、JSON、DOCX、PDF 和 Excel。角色卡与模组分别整理，资料处理结果返回发起者所在的群或私聊。需要检索自己的规则书时，可运行 `python -m cockp import-rules "规则书.pdf"` 建立本地索引。

## 接入官方 QQ 机器人

在项目根目录将 `.env.example` 复制为 `.env`。官方服务从 `--env` 指定的文件读取配置。首次接入需要以下值。

| 配置项 | 用途 |
| --- | --- |
| `QQBOT_APP_ID`、`QQBOT_APP_SECRET` | QQ 开放平台分配给机器人的应用 ID 和密钥 |
| `QQBOT_EXPECTED_LEGACY_GROUP`、`QQBOT_EXPECTED_LEGACY_OWNER` | 要绑定的 QQ 群号和群主 QQ 号 |
| `QQBOT_ENROLLMENT_CODE_HASH` | 首次绑定口令的 SHA-256 十六进制摘要 |
| `DEEPSEEK_API_KEY` | 官方服务调用主 KP 和资料整理模型所用的密钥 |

首次部署时运行以下命令，在本机生成随机接入口令和对应摘要。第一行交给群主，第二行填入 `QQBOT_ENROLLMENT_CODE_HASH`。

```sh
python -c "import hashlib, secrets; code=secrets.token_urlsafe(24); print(code); print(hashlib.sha256(code.encode()).hexdigest())"
```

初始化数据库，再启动官方 Webhook 服务。

```sh
python -m cockp --database data/runtime/state.sqlite3 migrate
python scripts/qq_official_session.py --env .env --database data/runtime/state.sqlite3
```

服务监听 `127.0.0.1:19057`。通过自己的 HTTPS 反向代理将 QQ 回调转到 `/qqbot/webhook`，在开放平台启用群消息事件，然后由群主在目标群内 @ 机器人发送 `/接入 口令`。绑定成功后即可建团。要关联私聊，在群里发送 `/绑定私聊`，按机器人提示确认。

群名片桥是可选服务。需要名片同步时，在独立的本机配置文件中填写 `ONEBOT_SELF_ID`、`COCKP_GROUP_IDS`、`ONEBOT_API_URL`、`ONEBOT_ACCESS_TOKEN` 和 `QQ_PERSONAL_BRIDGE_TOKEN`。桥接令牌使用独立生成的至少 32 个 ASCII 字符。使用 `python scripts/qq_personal_admin_bridge.py --check --env 配置文件` 检查配置，将 `--check` 改为 `--serve` 即可启动。官方服务一侧填写相同身份的 `QQ_PERSONAL_SELF_ID`、相同的桥接令牌和本机回环地址 `QQ_PERSONAL_BRIDGE_URL`。

## OneBot、HTTP 和存档

`python -m cockp serve` 启动 FastAPI 服务，默认监听 `127.0.0.1:8080`。可以访问 `/health` 检查状态。OneBot v11 的事件入口为 `/onebot/v11/events`，需在 `.env` 填写网关地址、签名密钥和群白名单。管理接口 `/api/command` 使用 `COCKP_API_TOKEN` 进行 Bearer 认证。HTTP 服务默认离线，配置 `DEEPSEEK_API_KEY` 并设置 `COCKP_LLM=deepseek` 后启用真实模型。

也可以用 Docker Compose 运行这一 HTTP 服务。准备好 `.env` 后执行 `docker compose up --build -d`。默认端口仅绑定本机，SQLite 存在 `kp-data` 命名卷。

备份和恢复命令如下。恢复时使用一个新文件名，之后将 `COCKP_DATABASE` 指向恢复的存档。

```sh
python -m cockp --database data/cockp.sqlite3 backup data/backups/campaign.sqlite3
python -m cockp restore data/backups/campaign.sqlite3 data/restored.sqlite3
```
