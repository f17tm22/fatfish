# 大肥鱼（fatfish）

一个跑在 QQ / 微信群里的 DeepSeek 水群机器人：傲娇鲸鱼娘人格、硬预算闸门、0 token 模板功能、本地 Tkinter 调参台。

代码大部分是「一个人自用」的产物：目标是便宜（¥5/天以内）、可控（热更新、能看账本）、有人味（短句、口癖、随机语气）。

## 特性

- **人格可控**：`persona.md` 是纯文本提示词；`fatfish/persona.py` 负责装配运行时上下文（时间、心情、本轮语气、每轮随机编号、知识卡、群上下文）。
- **预算硬闸门**：按 DeepSeek 峰谷价计费，落到 SQLite 账本；达到日上限直接拒答。
- **0 token 小功能**：抽签、签到、喂饭/喂饭榜、好感度/好感榜、贴贴、夸/骂、骰子、猜数字、群报、报时、余额（主人）。模板回复，不经过模型。
- **好感度机制**：喂饭/签到/夸/猜中加分，被骂与叫胖鱼扣分；每日涨幅封顶；等级带彩蛋（签到多一碗、抽签抬档、专属称呼）。
- **群成员固定称呼**：QQ 群事件只给 `member_openid`，本项目支持 `config` 里写死、本人说「我是 xxx」自报、以及主人说「6289 是小羊」预挂，三级取名。
- **看图 / 联网**：图片走多模态直读；关键词命中时调用 DeepSeek Search（按次计费，写进同一账本）。
- **调参台（GUI）**：本地 Tkinter，10 个页签——人格、模型、预算、触发/功能、渠道、会话、好感度、日志、定时推送、生效确认。
  - 保存是「写盘 + 让机器热更新 + 回读运行中真实参数逐项比对」，不需要重启进程（`app_id`/渠道开关/微信配置除外）。
  - 所有耗时操作走后台线程 + 队列泵，界面不卡（实测保存主线程耗时 < 40ms）。
- **热更新**：改 `config.yaml` / persona / 知识卡都不用重启，进程按 mtime 轮询。

## 快速开始

```bash
git clone <this-repo> fatfish && cd fatfish
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml      # 填自己的渠道与预算
cp .env.example .env && chmod 600 .env  # 填 DeepSeek key（唯一存放处）
$EDITOR persona.md                     # 想改人格就改这里

./run.sh check        # 离线自检：不联网、不花钱
./run.sh start        # 前台起机器人（Ctrl-C 退出）
./run.sh gui          # 打开调参台
```

常用命令：

```bash
./run.sh status | usage | logs | stop
./run.sh say "夸我"                    # 本地模拟一条消息，看回复（会花钱，几分钱）
./run.sh sim                          # 群消息端到端模拟（打桩，不联网不花钱）
python3 scripts/selfcheck.py          # 完整自检
```

## 目录

```
fatfish/
├── run.sh                 # 便捷入口（start/stop/status/gui/check/sim/autostart…）
├── config.example.yaml    # 配置模板（仓库里不含真实配置）
├── persona.md             # 人格提示词
├── fatfish/
│   ├── settings.py        # 路径、.env 读取、配置合并
│   ├── store.py           # SQLite：账本 / 会话历史 / kv（好感度、昵称、冷却）
│   ├── llm.py             # DeepSeek 客户端、峰谷计价、预算闸门、回复清洗
│   ├── persona.py         # 系统提示词装配（心情/语气/编号/知识卡）
│   ├── brain.py           # 触发 → 冷却 → 限流 → 模板功能 → LLM → 清洗
│   ├── features.py        # 0 token 模板功能
│   ├── affinity.py        # 好感度：分值表、等级、每日封顶
│   ├── names.py           # 群成员固定称呼
│   ├── knowledge.py       # knowledge/*.md 关键词命中注入
│   └── channels/          # console / weixin(iLink) / qq(官方机器人 API v2)
├── gui/tuner.py           # 调参台
├── scripts/               # selfcheck / e2e 模拟 / 导出提示词 / 建档案
├── knowledge/             # 知识卡（二次元、番剧、音游、材料力学…）
├── docs/QQ后台配置清单.md  # QQ 开放平台后台怎么配
└── assets/                # 调参台图标
```

## 调参台要点

- 「保存并应用」→ 写盘后向进程发 `SIGUSR1`，进程把**当前实际生效的参数**打成一行 JSON 进日志，界面回读并逐项比对，标「一致 / 需重启」。
- 「会话」页支持时间正/倒序与四列筛选（时间、角色、说话人、内容），输入 150ms 去抖，只过滤内存数据。
- 「好感度」页按群看排行榜，可手动加减分、给群友固定名字。
- 「日志」页只读日志尾部并按 mtime 去重，3 秒自动刷新也不卡。

## 安全与隐私

- **API key 只放 `.env`**（权限 600），`.gitignore` 已排除 `.env*`、`data/`、`logs/`、`profiles/`、`config.yaml`、表情包素材。
- 仓库里的 `config.example.yaml` 与 `knowledge/群内.md.example` 已把 openid、群号、AppID、账号 ID 全部替换成占位符。
- 运行时数据（会话历史、账本、pid、同步状态）都在本地 `data/`，不入库。
- 联网搜索与模型调用都会写账本；日志不打印 key。

## 已知限制

- QQ 官方机器人接口拿不到群成员昵称，只能靠 openid 映射（见 `fatfish/names.py`）。
- 群里「聊天记录」类型的合并转发卡片内容读不到，只能明确回一句看不了。
- `deepseek-flash` 在专业问题上会编，本项目用知识卡 + 低温度 + 「记不清就说记不清」来兜。
- 微信渠道依赖 iLink token；没有微信环境时把 `channels.weixin.enabled` 关掉即可（默认就是关）。

## 说明

个人自用项目，未附开源协议；想拿去玩请自己替换人格、渠道凭据与知识卡。
