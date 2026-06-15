# 个人 Fork 同步说明

本仓库是 [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) 的私有 fork，在官方基础上叠加了个人定制。

## 远端配置

| 远端 | 仓库 | 用途 |
|------|------|------|
| `origin` | `difeizheng/MoneyPrinterTurbo` | **个人 fork，推送自己的改动** |
| `upstream` | `harry0703/MoneyPrinterTurbo` | **官方仓库，拉取上游更新** |

查看：`git remote -v`

## 已纳入版本库的个人定制

- `feat(voice)` — edge_tts 代理支持（`config.proxy` → `edge_tts.Communicate`）
- `build(docker)` — Dockerfile.gpu 用清华镜像源 + docker-compose.gpu.yml 用 extends 单文件启动
- `feat(webui)` — 历史任务列表页（`webui/pages/`）+ 对应 i18n
- `chore` — `.gitignore` 屏蔽个人一次性脚本

## 被忽略、不进版本库的文件

`config.toml`（含 API key）、`storage/`、`ppt_slides/`、`ppt_to_video.py`、`resubmit_ppt_video.py`、`run_claude.bat`。
这些只在本地保留，不会被 push，也不会被官方更新覆盖。

---

## 同步官方更新的标准流程

每隔一段时间执行：

```bash
# 1. 拉取官方最新（只下载，不改动本地代码）
git fetch upstream

# 2. 合并到 main（你的改动 + 官方更新做 3-way 合并）
git merge upstream/main

# 3. 推送到个人 fork
git push origin main
```

无冲突时一行搞定：

```bash
git fetch upstream && git merge upstream/main && git push origin main
```

### 冲突处理

官方可能改了你定制过的同一文件，主要风险点：

- `app/services/voice.py`（代理相关）
- `Dockerfile.gpu`
- `webui/i18n/zh.json`、`webui/i18n/en.json`
- `docker-compose.gpu.yml`

冲突时打开带 `<<<<<<<` 标记的文件，手动保留双方需要的内容，然后：

```bash
git add <已解决的文件>
git commit            # 完成合并提交
git push origin main
```

i18n JSON 冲突通常只是双方各自在 `Translation` 对象末尾加了 key，按结构拼在一起即可，注意逗号和括号闭合，改完用 `python -c "import json;json.load(open('webui/i18n/zh.json',encoding='utf-8'))"` 校验。

---

## 安全提醒

- **永不提交密钥**：所有 API key 只放在被忽略的 `config.toml` 里，或读环境变量。
- **fork 保持 Private**：在 Settings → Danger Zone 确认可见性为私有。
- 若密钥曾在别处暴露过，立即到对应控制台轮换。
