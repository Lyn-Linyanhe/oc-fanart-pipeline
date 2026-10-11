# oc-fanart-pipeline

角色身份 × 构图任务卡 → 调 geiliapi 生图 → 对比。

**分工：** 工作台（主对话，本机 `studio_app.py`）负责选图和点生成；ZCode 对话里的 agent 负责看图写卡、补圣经、限额出图和验收。共用约定见 [`CONTRACT.md`](CONTRACT.md)。

创意、身份与背景设计分开：当前旧版 CLI 仍以 `characters/<id>/bible.json` 和排序后的第一张 `refs/` 为运行时身份来源；V0.1 的 `profile.json`、身份/画风/构图/背景参考包、提示词模块和审核版本链已经记录，但尚未接入。不要拿别人的整张成品当底图翻绘。

Key **不要**写进仓库。脚本会读：

1. 环境变量 `IMAGE_API_KEY` + `IMAGE_BASE_URL`（成对，禁止和本机配置拼套）
2. 否则 `~\.zcode\image-edit.local.json` 里 **当前 active** 那一套（支持 `profiles[]`）

本机改图画布是 `http://127.0.0.1:8765/`，和这条 CLI **分开**，不要把流水线塞进画布。

## Agent 在对话里怎么接

读 `CONTRACT.md`、`docs/initial-settings-v0.1.md` 和角色 `bible.json`。桌面库在 `<desktop-library>`。芙莉莲示例：`characters/frieren/README.md`；V0.1 扩展档案：`characters/frieren/profile.json`。

工作台（不要在这边重做 UI）：

```bash
python scripts/studio_app.py
```

默认 http://127.0.0.1:8788/ 。

## 当前设定记录

`docs/initial-settings-v0.1.md` 固定角色档案、身份/画风/构图/背景参考包、提示词模块、初审状态和版本链的初步口径。当前旧版 `generate.py` 仍只读取 `bible.json` 的 `lock/forbid/style`，新模板不会自动参与生图。

角色扩展档案示例：`characters/frieren/profile.json`。它记录外观、人设、情绪表达、多角度参考和待补缺口；`characters/frieren/README.md` 说明当前哪些参考图仍只是对照。模板和非运行态请求示例在 `docs/templates/` 与 `docs/examples/`。

## 一次跑通（先打通接口）

```bash
cd /d/some-project/oc-fanart-pipeline
python scripts/generate.py --jobs jobs/batch01.json --limit 1
python scripts/sift.py
```

`--limit 1` 先打通。没有 `characters/demo/refs/` 里的图时走文生图，**身份会漂**；有干净正脸/全身再走 `/images/edits`。

姿势参考：线稿放到 `poses/`，任务卡写 `"pose_ref": "poses/xxx.png"`。上游是 GPT Image 软参考，不是 ControlNet。

## 小红书构图（登录在另一个会话）

本仓库**不负责扫码**。空会话里启动 `xiaohongshu-mcp`（默认 `http://127.0.0.1:18060`）并登录一次之后：

```bash
python scripts/run.py --keyword "动漫 构图 回眸 雨夜" --n 3 --limit 1
```

等价于：

```bash
python scripts/xhs_inspire.py --keyword "动漫 构图 回眸 雨夜" --n 3
python scripts/generate.py --jobs jobs/xhs.json --limit 1
python scripts/sift.py
```

封面进 `inbox/xhs/`（已 gitignore），只当构图灵感；任务卡里 `pose_ref` 仍为空，出图用角色圣经。MCP 没起来时：

```bash
python scripts/run.py --keyword dummy --skip-xhs --limit 1
```

会改用 `jobs/batch01.json`。

## 换角色

复制 `characters/demo/`，改 `bible.json`，任务卡 `"character"` 改成新文件夹名。角色图以后放到你自己的文件夹，再复制或链进 `refs/`。

## 已知边界

- 请求带浏览器 UA；Cloudflare 403 不一定是 Key 坏了。
- 生图 POST 失败不要自动重试（可能已扣费）。
- 脸相似度筛以后再加。
