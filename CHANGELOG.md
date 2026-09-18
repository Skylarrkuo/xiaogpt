# CHANGELOG

本仓库是 [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt) 的改造版 fork。
**本文件只记录本 fork 相对上游的差异**，上游自身的版本历史请见上游仓库。

## 上游基线

| 项 | 值 |
| --- | --- |
| 上游仓库 | <https://github.com/yihong0618/xiaogpt> |
| 分叉基线 | `3de0af6` — Feat/add jiekou (#602) |
| 基线日期 | 2026-02-24 |

本 fork 与上游**共享完整祖先**，可以直接 `git merge` 上游更新。
同步注意事项见文末「与上游同步」。

累计差异（截至本文件所在提交）：25 个文件，+530 / −1027 行。

---

## 2026-09-18

### 登录方式重做：废弃 cookie，改用扫码 + 自动续期

**背景**：上游推荐的风控绕行方案是手工抓 cookie，但 cookie 里的
`serviceToken` 有效期只有数周，过期后 `get_latest_ask_from_xiaoai` 会拿到
HTTP 401 的 HTML 页面，`r.json()` 抛异常，表现为每秒刷屏的
`get latest ask from xiaoai error, retry`——错误提示完全指不到真正原因。
而账号密码登录在小米要求二次验证时必然失败：`serviceLoginAuth2` 返回
`code: 0` 却不带 `userId`，转而给出 `notificationUrl`，`miservice` 没有实现
这个验证流程，于是在 `miaccount.py:71` 抛 `KeyError: 'userId'`。

**方案**：改用米家 App 扫码登录，凭据落到 `~/.mi.token`，其中的 `passToken`
每次启动自动换取新的 `serviceToken`，无需任何人工维护。

- **新增 `login_qr.py`** —— 直接调用小米官方的
  `/longPolling/loginUrl` → 长轮询 → `clientSign` 换取 serviceToken 流程，
  写入 `miservice.MiTokenStore` 期望的 schema
  （`deviceId` / `userId` / `passToken` / `micoapi`），并在结尾自动调用对话
  接口验证。未安装 `qrcode` 库时退化为打印链接，可在浏览器扫码。
- **删除 `get_cookie.py`** —— 浏览器抓 cookie 的旧流程。
- **`config.py`** 移除 `cookie` 字段；**`cli.py`** 移除 `--cookie` 参数。
- **`xiaogpt.py`** 移除四处 cookie 分支：`login_miboy` 无条件登录、
  `_init_data_hardware` 不再提前返回、`get_cookie` 只保留从 `~/.mi_token`
  构造、401 分支统一走 `_retry()` 刷新并给出扫码提示（带 30 秒退避，
  避免把登录接口打爆触发风控升级）。
- 保留 `COOKIE_TEMPLATE` 与 `parse_cookie_string`：仍用于把 `~/.mi_token`
  构造为 aiohttp 的 cookie jar。

### 日志不再输出明文密码与 API key

`Config` 是 dataclass，默认 `__repr__` 会原样输出所有字段，而
`xiaogpt.py` 在 `-v`/`-vv` 下会 `log.debug(config)`——等于把小米密码和各家
API key 明文写进终端，随手贴日志就会泄露。

覆盖 `Config.__repr__` 做打码（而非只改调用点，这样任何打印 config 的地方
都自动安全）：

- 顶层用显式集合 `_MASKED_FIELDS`，含 `password` 与各 API key，
  另含 `account`（手机号属个人信息）
- `tts_options` / `gpt_options` 递归按字典键名匹配，因为 `from_options`
  会把 volc 的 `access_key`/`secret_key`、fish 的 `api_key` 注入进去
- 打码标记为 ASCII `***`：日志重定向到文件时中文标记会因控制台编码变乱码
- **必须用显式集合而非正则匹配字段名**：`keyword` / `change_prompt_keyword`
  名字里带 `key`，但装的是唤醒词，误伤会让日志失去意义

### Bot 精简

上游内置十余个 bot，本 fork 只保留实际使用的四个。

| 动作 | 内容 |
| --- | --- |
| 重命名 | `ppio_bot.py` → `deepseek_bot.py`（`DeepseekBot`，接入 `api.deepseek.com`） |
| 删除 | `glm_bot` `jiekou_bot` `langchain_bot` `llama_bot` `moonshot_bot` `qwen_bot` `yi_bot` |
| 删除 | 整个 `xiaogpt/langchain/` 模块（含 email 示例） |
| 保留 | `chatgptapi` `deepseek` `gemini` `doubao` |

`BOTS` 注册表与 `cli.py` 的 `--bot` 选项同步收窄。

### 其他

- **新增** `one_click.ps1` / `one_click.bat` 启动脚本，统一从 `config.yaml`
  读取配置（不再硬编码账号密码）。
- **`requirements.txt`**：移除 `async-timeout`、`exceptiongroup` 上的
  `python_version < "3.11"` 标记。
- **`.gitignore`**：新增排除 `config.yaml`、`conf.ini`、`.mi.token`、
  `*.token`、`micli.exe`。前两者含明文密码，务必保持排除。
- **`README.md` / `xiao_config.yaml.example`**：同步移除 cookie 相关说明，
  改为指向 `login_qr.py`。

---

## 已验证 / 未验证

**已实测**（2026-09-18）：

- 扫码登录成功，`~/.mi.token` 四个键齐全且符合 `miservice` 期望
- 对话接口返回 HTTP 200（此前为 401）
- 轮询正常，长时间运行无警告
- **自动续期**：删除 `micoapi` 键后重启，自动换出**不同的**新 `serviceToken`
- `-v`/`-vv` 输出中不再出现密码、DeepSeek key、手机号；`keyword` 等
  非凭据字段仍正常显示

**未验证**：

- 真实语音链路（唤醒 → DeepSeek → TTS 回放）尚未端到端跑通
- `deepseek_bot` 的默认模型名 `deepseek-v4-flash` 未实测调用成功

---

## 与上游同步

因为保留了共同祖先，可以直接：

```bash
git fetch origin
git merge origin/main
```

**注意**：本 fork 删除了上游 8 个 bot 与整个 `langchain` 模块（约 −900 行）。
上游若继续修改这些文件，合并时会**反复产生删除冲突**，每次都需要手动确认
「保持删除」。这是删代码型 fork 的固有代价，无法避免，只能每次合并时留意。

如果不再需要跟随上游，可以移除上游 remote，或改用 `git rebase` 维护线性历史。
