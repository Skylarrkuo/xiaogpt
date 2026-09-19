# xiaogpt — 小爱音箱接入 LLM（L05C 二次开发版）

把大模型接进小爱音箱：你对音箱说话，问题转给 LLM，回答用 TTS 播出来。

本仓库是 [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt) 的**大幅改造版**，
围绕「小爱音箱 Play 增强版（L05C）+ DeepSeek」这一套实际在用的组合重写过。
与上游的逐条差异、每个改动的实测数据与未验证项，全部记录在 **[CHANGELOG.md](CHANGELOG.md)**，
看那份比看这份更接近真相。

> **只针对 L05C 验证过。** 其它型号的代码路径按设计保持与上游一致，但没有真机
> 回归测试——这是有意识的范围取舍。

## 这个版本有什么不一样

### 不用触发词也能提问（兜底接管）

上游要求每句话都以 `请` / `帮我` 开头才会转给 AI。本版本改成**看小爱自己的表现**：
她答得上来的自己答，答不上来的自动转给 AI。

```
你：小爱同学，现在几点
小爱：现在是下午三点二十分          ← 她自己答了，不打扰

你：小爱同学，详细讲讲三国演义里赤壁之战的过程
小爱：被你问住了，看来要更努力学习了   ← 她答不上来
AI  ：赤壁之战始于曹操率二十万大军南下……  ← 自动接管
```

判据是**小爱的回答文本**，不是你说的话。实现上分三层，越靠前越便宜：

| 层 | 来源 | 匹配方式 | 成本 |
| --- | --- | --- | --- |
| 1 | 型号自带的词干（L05C 已固化 3 套实测话术） | 回答开头 12 字内的子串 | 零 |
| 2 | `learned_fallbacks.json`（学习模式攒下的判定） | 归一化整句精确匹配 | 零 |
| 3 | LLM 分类（仅学习模式，且前两层未命中） | — | 一次 API 调用 |

设计上**宁可漏判、不可误判**：把小爱的正常回答抢给 AI 是灾难性的，漏判只是退回
用触发词。所以只在回答开头匹配、短语不得短于 2 字、超过 40 字的长回答直接放行、
回答为空一律不接管（执行开灯/放歌/设闹钟时 `answers` 就是空的）。

**已知取舍**：小米的对话接口会把「小爱同学」剥掉，所以追问窗口期内无法区分设备指令
和追问——窗口里说「关灯」会被 AI 抢答而不是真的关灯。

### 追问窗口

AI 回答完之后的 `follow_up_seconds` 秒内，接着问就行：**不用再说触发词**。窗口在回答
**播完之后**才开始计时（300 字的回答要播一分钟以上），过期自动关闭并清空对话历史，
不会把上一次无关的问答当成上下文。

但要说清楚一件事：**这个窗口管的是"这句话要不要交给 AI"，管不了麦克风。** 音箱不
被唤醒就不记录，我们也就什么都看不到。所以实际用法是「小爱同学，那它的原理是
什么」——唤醒词会被小米的接口从记录里剥掉，不会干扰判定；除非你的设备自己开着
连续对话（有些型号/固件支持回答后自动听几秒），才可以省掉唤醒词。日志里出现
「追问窗口内：这句直接交给 AI」就说明设备把它记下来了；没有这行就是它压根没听。

### 对话记忆与缓存（省钱的关键）

历史**只在尾部追加**：提示词放 `system`，之后每轮把问答追加在后面。为什么要这么
死板——三家（DeepSeek / MiMo / GLM）都有**前缀缓存**，命中的前提是这次请求的
messages 前缀与上次逐字节一致；只要从中间删一条、或每轮重写摘要，前缀就变了，
缓存全废。

所以这里**没有**"只留最近 5 轮"的滑动窗口。要控长度，就等历史超过
`history_budget_chars`（默认 16000 字，约几十轮）时，把**最老的一半**压成一段
摘要塞进 `system`——一次性失效，之后继续只追加。摘要会随历史一起长期保留，
所以"很久以前说过的事"也还记得。

每次回答后终端会打一行缓存命中，方便你盯着这个指标：

```
[cache] 输入 493 tokens：命中 256（52%），未命中 237
```

实测规律（DeepSeek：同一个账号、同一前缀连续请求）：

| 前缀规模 | 命中 |
| --- | --- |
| ≤ 100 tokens | 0（**太短根本不会被缓存**） |
| 280 tokens | 128（46%） |
| 384 tokens | 128（33%） |
| 493 tokens | 256（52%） |
| 819 tokens | 640（78%） |

也就是说：**聊得越长越省**（稳定前缀越长，命中率越高），短问答本来也没多少
token 可省。`follow_up_seconds` 过期不再清空历史（那样每轮都从头开始，既忘上
文又丢缓存），改成给下一句追加一条「新一轮对话」标记——标记只动追加的部分，
前缀照样命中。

### 第三方 TTS 真的能用了

上游 README 曾写「L05C 只支持小爱原本的 tts」，**实测是错的**。真正的原因是内置的
HTTP 服务不支持 `Range` 分段请求：音箱用 ffmpeg 取音频，会发探测 / 定位 / 播放三个
请求，而 `SimpleHTTPRequestHandler` 一律回 `200` + 整个文件，ffmpeg 因此疯狂重试、
音箱永远进不了播放态——日志里全是正常的 200，音箱却一声不吭，极具迷惑性。

修好之后 `tts: edge` 可用，音色换成微软晓晓，不再是原来那个嗓子。

```
tts: edge
tts_options:
  voice: zh-CN-XiaoxiaoNeural   # 留空则按语言自动挑
```

### 学习模式

兜底话术是个黑盒——实测发现小爱至少有 **3 套互不相同**的模板，靠手工枚举追不上。
开启 `learn_fallback: true` 后，没命中词干表的回答交给 AI 判断是否属于「答不上来」，
判定结果落到 `learned_fallbacks.json`，正负都记，下次直接查表不再调 AI。

攒一段时间后，你可以把反复出现的词干**提升**成配置里的静态词干，连 AI 都不用问。
注意静态词干只在**回答开头 12 个字**内匹配，取词干时要取开头的辨识部分，而不是
多套模板共用的尾巴。

## 快速开始

### 1. 环境

Python 3.9 ~ 3.12（`requires-python = ">=3.9,<3.13"`）。

```bash
pip install -r requirements.txt
```

### 2. 登录（扫码，不用 cookie）

小米对密码登录有二次验证风控，本版本改用米家 App 扫码：

```bash
python login_qr.py
```

凭据写入 `~/.mi.token`（Windows 为 `C:\Users\你的用户名\.mi.token`）。其中的
`passToken` 每次启动自动换取新的 `serviceToken`，**不会像 cookie 那样过几周失效**。
这个文件可以直接拷到别的机器上用。

### 3. 拿到设备 DID

```bash
micli list        # 需要先 pip install miservice_fork
```

输出里找你的设备，记下 `miotDID`。**型号**（如 `L05C`）印在音箱底部。

### 4. 写配置

```bash
cp xiao_config.yaml.example config.yaml
```

最小可用配置：

```yaml
hardware: L05C
account: "你的小米账号"
mi_did: "上一步拿到的 did"

bot: deepseek
deepseek_api_key: "sk-..."

use_command: true     # L05C 需要，否则可能出现终端有回复但音箱不说话的
mute_xiaoai: true     # 快速掐掉小爱自己的回答
tts: edge
```

> `config.yaml` 已加进 `.gitignore`（含明文密码与 API key），**不要提交它**。

### 5. 启动

```bash
python xiaogpt.py --config config.yaml
```

仓库里带了两个便捷脚本，会自动激活名为 `xiaogpt` 的 conda 环境并设置代理：
`one_click.ps1`（PowerShell）/ `one_click.bat`（CMD）。

加 `-v` 打开详细日志，每条对话记录都会打出原文与判定原因，排查问题很有用。

## 怎么跟它说话

| 方式 | 例子 | 说明 |
| --- | --- | --- |
| 直接正常说 | 小爱同学，量子纠缠是什么 | 小爱答不上来就自动转给 AI |
| 触发词强制走 AI | 小爱同学，请解释一下相对论 | 以 `keyword` 里的词开头，任何时候都生效 |
| 持续对话 | 小爱同学，开始持续对话 | 之后**所有**输入都进 AI，直到说「结束持续对话」 |
| 追问 | 小爱同学，那它的原理是什么 | `follow_up_seconds` 秒内不用再说触发词 |
| 换提示词 | 更改提示词 你现在是一个诗人 | 运行时生效，不用重启 |
| 换说话方式 | 语音指令 用四川话温柔一点 | 情绪/方言/语气，仅豆包 TTS 支持 |

注意「持续对话」和「追问窗口」都会把**设备指令**也吞掉——那段时间里说「关灯」会被
AI 抢答。这是已知取舍。

## 配置项

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `hardware` | 设备型号，印在音箱底部 | `LX06` |
| `account` | 小米账号 | 环境变量 `MI_USER` |
| `password` | 小米密码（有 `~/.mi.token` 时用不到） | 环境变量 `MI_PASS` |
| `mi_did` | 设备 DID | 环境变量 `MI_DID` |
| `bot` | 使用哪个 AI：`deepseek` / `mimo` / `glm` | `deepseek` |
| `deepseek_api_key` | DeepSeek key（[申请](https://platform.deepseek.com)） | 环境变量 `DEEPSEEK_API_KEY` |
| `deepseek_model` | DeepSeek 模型名，留空用 `deepseek-flash` | 环境变量 `DEEPSEEK_MODEL` |
| `mimo_api_key` | 小米 MiMo key（[申请](https://mimo.mi.com)） | 环境变量 `MIMO_API_KEY` |
| `mimo_model` | MiMo 模型名，留空用 `mimo-v2.5` | 环境变量 `MIMO_MODEL` |
| `glm_api_key` | 智谱 GLM key（[申请](https://open.bigmodel.cn)） | 环境变量 `GLM_API_KEY` |
| `glm_model` | GLM 模型名，留空用 `glm-5.3-flash` | 环境变量 `GLM_MODEL` |
| `volc_access_key` / `volc_secret_key` | 火山引擎凭据，`tts: volc` 时也要用 | 环境变量 |
| `tts` | TTS 类型：`mi` / `edge` / `doubao`，见下 | `mi` |
| `tts_options` | TTS 参数（音色、语速、语音指令…），见下 | `{}` |
| `change_tts_instruction_keyword` | 用说话改语音指令的触发词，留空关闭 | `["语音指令"]` |
| `tts_auto_instruction` | 豆包 TTS：按用户这句话自动决定怎么说 | `true` |
| `use_command` | 用 MI command 与小爱交互。**L05C 需要开** | `false` |
| `mute_xiaoai` | 快速掐掉小爱自己的回答 | `false` |
| `stream` | 流式响应，首句更快出声 | `false` |
| `prompt` | 自定义提示词 | `以下请用 300 字以内回答，请只回答文字不要带链接` |
| `keyword` | 触发词列表，以任一词开头即强制走 AI | `["帮我", "请"]` |
| `change_prompt_keyword` | 换提示词的触发词 | `["更改提示词"]` |
| `fallback_answer_keyword` | 兜底接管词干。填的是**小爱的回答**片段，会与型号自带的合并 | `[]` |
| `learn_fallback` | 学习模式：让 AI 判断没见过的回答是否属兜底话术 | `false` |
| `follow_up_seconds` | 追问窗口长度，0 = 关闭 | `0` |
| `start_conversation` | 开始持续对话的关键词 | `开始持续对话` |
| `end_conversation` | 结束持续对话的关键词 | `结束持续对话` |
| `proxy` | HTTP 代理，如 `http://127.0.0.1:7890` | 无 |
| `gpt_options` | 传给模型的参数，如 `temperature` / `top_p` / `model` | `{}` |
| `api_base` | 覆盖所选 provider 的官方接口地址（代理 / 自建网关用） | 无 |
| `verbose` | 详细日志级别（`-v` / `-vv`） | `0` |

大部分配置项也能从命令行传，**命令行优先于配置文件**。常用的有：

```
--hardware L05C            设备型号
--account xxx --password xxx
--config config.yaml       指定配置文件
--use_deepseek / --use_mimo / --use_glm
--deepseek_api_key xxx   /   --mimo_api_key xxx   /   --glm_api_key xxx
--mute_xiaoai  --stream  --use_command
--tts edge   -v / -vv
```

注意 **`mi_did` 没有命令行参数**，只能从 `config.yaml` 配或走环境变量 `MI_DID`。
`keyword` / `follow_up_seconds` 这类列表与数值项同样只有配置文件一条路。

### 三个 bot 与思考模式

`bot` 三选一，都走 OpenAI 兼容协议。**默认模型都选的是各自最快最便宜的那档**，
要质量就在 `<provider>_model` 里换成旗舰：

| `bot` | 官方接口 | 默认模型 | 旗舰 |
| --- | --- | --- | --- |
| `deepseek` | `api.deepseek.com` | `deepseek-flash` | — |
| `mimo` | `api.xiaomimimo.com/v1` | `mimo-v2.5` | `mimo-v2.5-pro` |
| `glm` | `open.bigmodel.cn/api/paas/v4` | `glm-5.3-flash` | `glm-5.3` |

思考模式三家的默认值和可关性都不一样，本版本按厂商语义分别处理，**你只管配
`gpt_options`**：

| `bot` | 官方默认 | 本版本默认 | 想开 / 想调强度 |
| --- | --- | --- | --- |
| `deepseek` | 关 | 关（延迟最低） | `reasoning_effort: low/high/max`，设置后自动开思考 |
| `mimo` | **开** | 关（延迟最低） | 设 `reasoning_effort` 即开；MiMo 没有强度档位 |
| `glm` | 开，且**关不掉** | 开 + `reasoning_effort: low` | `reasoning_effort: high/max` 做复杂推理 |

开启思考的厂商若与 `temperature` / `top_p` 冲突（DeepSeek 报错、MiMo 静默忽略），
这两个参数会被自动剔除，避免"配了但没生效"的假象；GLM 支持同时传，故保留。
`gpt_options.thinking` 可以原样透传厂商私有字段（如 `clear_thinking`）作为逃生舱。

启动时会对 `<base_url>/models` 校验模型名，写错直接退出并列出可用模型——否则表现是
「音箱一声不吭」，很难查。哪些家没提供 `/models` 就跳过校验，不阻断启动。

## TTS 与音色

`tts` 三选一。除 `mi` 外都是**本地合成出音频文件、起一个 HTTP 服务让音箱来拉**：

| `tts` | 谁在说话 | 音色与说话方式 |
| --- | --- | --- |
| `mi` | 小爱自己的嗓子，不额外依赖 | 固定，没有可配参数 |
| `edge` | 微软 edge-tts（免费、快） | `tts_options.voice` 选音色，`rate`/`pitch`/`volume` 调语速音调音量 |
| `doubao` | 火山引擎豆包语音合成大模型 | `speaker` 选音色，另有**语音指令**控情绪/方言/语气 |

### edge

```yaml
tts: edge
tts_options:
  voice: zh-CN-YunxiNeural   # 留空则按语言自动挑
  rate: "+0%"                # 语速，如 "+20%" / "-10%"
  volume: "+0%"              # 音量
  pitch: "+0Hz"              # 音调
```

中文音色共 14 个（普通话 6 + 东北话/陕西话 + 粤语 3 + 台湾国语 3），
`xiao_config.yaml.example` 里列全了。其它语种用命令查，共 300 多个：

```bash
edge-tts --list-voices
```

### doubao（豆包语音合成大模型）

```yaml
tts: doubao
tts_options:
  api_key: "..."                        # 火山控制台 > 语音合成大模型 > API Key 管理
  speaker: zh_female_vv_uranus_bigtts   # 音色 ID，控制台 > 音色库
  instruction: "用特别特别痛心的语气说话吗?"   # 语音指令：情绪 / 语气 / 风格
  dialect: sichuan                      # 方言，见下
  speech_rate: 0                        # 语速 -50 ~ 100（100 = 2 倍速）
  loudness_rate: 0                      # 音量 -50 ~ 100
  pitch: 0                              # 音调 -12 ~ 12
```

**按场景自动配音（默认开）**：每句回答之前，AI 先看一眼你说了什么，再决定这句
该怎么念——讲恐怖故事的语速慢、音量低；哄睡的更慢更轻；讲笑话的快一点；正式解释
则放缓。实测参谋耗时 0.7~1.8 秒，与回答生成并行跑，不拖慢首句出声。

```
你：小爱同学，给我讲个恐怖故事
    → 幕后：{"speech_rate": -25, "loudness_rate": -20, "instruction": "压低嗓音、语速放缓地轻声说"}
你：小爱同学，讲个笑话逗我开心
    → 幕后：{"speech_rate": 15,  "loudness_rate": 10,  "instruction": "用轻快活泼、带笑意的语气说"}
```

不想要就 `tts_auto_instruction: false`，只用下面配置里的固定值。

**手动指定**（运行时用说的改，设过之后本次运行不再自动覆盖）：

```
你：小爱同学，语音指令 用四川话温柔一点
小爱：好的        ← 之后所有回答都按这个来（重启回到配置文件里的值）
```

### 豆包哪些参数真的生效（实测，不是照抄文档）

用同一个音色、同一段话各合成多次，比对时长、能量、基频（详见 CHANGELOG）：

| 参数 | 实测结果 |
| --- | --- |
| `speech_rate`（语速） | **有效**：`50` 让 4.79s → 3.16s |
| `loudness_rate`（音量） | **有效**：`-50` 让能量降到 56% |
| `dialect`（方言） | 服务端接受、不报错；**听感未验证**（本机没法判定口音，样音留给耳朵） |
| `instruction` → `context_texts` | 上游**收下但没效果**：5 次采样时长比值 1.00、基频/能量都在噪声内 |
| `pitch`（音调） | `±10` 对基频没有可测影响，同样疑似被忽略 |
| `loudness_boost`（本版本加的补偿） | 豆包默认输出 RMS −22 dBFS，比小爱本嗓轻；`loudness_rate` 实测 +50 约 +3.6 dB、峰值仍有 −4 dBFS 余量（再高被服务端压限），所以默认 `loudness_boost: 50` |

所以在豆包这条链路上，**"情绪"是靠语速 + 音量近似出来的**，`instruction` 仍然照
文档原样发送（万一上游哪天在权限/端点上放开就自动生效），但别指望它现在改变音色。
音量嫌大嫌小都调 `loudness_boost`（0~100，`0` = 上游默认），它加在场景配音之上，
所以"哄睡轻一点"这类相对变化不会被它抹平。
另外两个限制来自官方文档：语音指令只有豆包**语音合成模型 2.0** 的音色支持
（音色 ID 带 `_bigtts`），声音复刻（`seed-icl-2.0`）不支持。

接口是流式的，但本版本按**文件模式**用它（先落地 mp3 再让音箱拉），因为 L05C
的取流/停止逻辑是在文件模式下调通的。合成失败（key 不对、音色不存在）会直接
在终端报出来，不会让你对着沉默的音箱猜。

**两个前提**：电脑和音箱要在**同一局域网**，且防火墙放行 8050-8089 端口——音箱是
主动来拉音频的。

Docker 的话（仓库自带 Dockerfile，`ENTRYPOINT` 是 `pdm run xiaogpt.py`），要把端口
映射出去并告诉容器宿主机的 IP：

```bash
docker build -t xiaogpt .
docker run -v <配置目录>:/config -p 9527:9527 \
  -e XIAOGPT_HOSTNAME=<宿主机 IP> xiaogpt --config=/config/config.yaml
```

国内构建慢可以换源：`docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple -t xiaogpt .`

## 型号差异集中在 `xiaogpt/device.py`

不同型号的小爱行为不一样，这些差异**全部集中在一个文件**里，而不是散落成
`if hardware == "L05C"`。表里只登记与默认行为不同的型号，**未登记的型号拿到
默认值，行为与上游一致**。

L05C 登记了三条：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `status_poll` | `False` | 播放态播完不回落，轮询它判断「播完了没」会死循环挂死 |
| `directive_command` | `"5-4"` | 停止播放要走 MIoT 的「执行文本指令」，ubus 那套停不住 |
| `fallback_phrases` | 3 条实测词干 | 兜底接管的已知模板，见上文 |

顺带一提，`5-4` 这条通道能把**任意小爱语音指令**发给音箱（`5-4 现在几点 #0` 她真的
会报时），理论上可以用来控制米家设备、播音乐、设闹钟——目前只被用作停止手段。

## 与上游的差异

上游仓库：<https://github.com/yihong0618/xiaogpt>

主要差异（逐条细节见 [CHANGELOG.md](CHANGELOG.md)）：

- **登录**：废弃 cookie，改扫码 + 自动续期
- **bot 精简**：只保留 `deepseek` / `mimo` / `glm` 三家（协议相同，合并成
  `xiaogpt/providers.py` 一张表 + 一个实现），删掉上游其余十余个 bot、整个
  langchain 模块，以及 Claude / Gemini / 豆包 / OpenAI 那几套独立 SDK
- **日志脱敏**：`Config.__repr__` 打码，密码与 API key 不会再被打进日志
- **兜底接管 + 追问窗口 + 学习模式**：本版本新增
- **型号差异集中化**：新增 `device.py`
- **TTS 精简**：只保留 `mi` / `edge`，删掉 openai/azure/google/baidu/volc/fish/
  minimax 与那条没验证过的流式播放链路
- **新增豆包 TTS**：自己实现火山「单向流式语音合成」WebSocket 协议，支持语音指令
  （情绪 / 方言 / 语气 / 语速），可用说的改
- **第三方 TTS 修复**：新增 `tts/http.py` 补上 Range 支持
- 修掉若干上游遗留：`answers` 为 `null` 时的崩溃、`re.sub` 未转义、
  `wakeup_xiaoai` 的入参写法、日志标签被 rich 吃掉

**合并上游时的注意事项**在 CHANGELOG 末尾的「与上游同步」一节。

## 排错

**终端有回复但音箱不说话**
先确认 `use_command: true`（L05C 需要）。用第三方 tts 时，检查电脑与音箱是否同网段、
防火墙是否放行 8050-8089。

**日志里出现「等 xxx.mp3 的取流超时」**（音频合成好了，音箱一声不吭）
多半是**挑错了本机地址**：开了 Clash / Mihomo 这类 TUN 代理后，"连 8.8.8.8 看本机
地址"会拿到 `198.18.0.1` 这种虚拟网卡地址，音箱根本连不上。本版本会自动滤掉这类
地址并优先选 `192.168.x.x`，启动时会打印选中的地址：

```
[tts] 本机有多个地址 ['172.23.80.1', '192.168.1.9']，选 192.168.1.9 给音箱取流；若音箱仍不出声，用 XIAOGPT_HOSTNAME 指定局域网地址
```

如果你是多网段、或者自动选择还是不对，直接指定：

```powershell
$Env:XIAOGPT_HOSTNAME="192.168.1.9"   # 换成 ipconfig 里与音箱同网段的那个
python xiaogpt.py --config config.yaml
```

**接管了，但小爱那句"答不上来"还是整句念完**
两层原因，都已处理：

1. **判定要快**：如果她的回答没命中型号自带的词干表，就要先跑一次 LLM 分类
   （1~2 秒），等判定出来她早念完了。日志里出现 `[learn] 判定为兜底` 就是这种
   情况——说明这条话术还没进词干表（`device.py` 里 L05C 已固化实测到的模板；
   新话术可以自己填进 `fallback_answer_keyword`，或让学习模式自动攒）。
2. **掐得要快**：判定接管后**一上来就先发一次"停止播放"**（记录比她的声音先到，
   这一枪正好落在她刚出声那一刻），然后前 3 秒每 0.3 秒看她一眼、在播就再补一枪，
   到我们要出声时才收手。之所以要盯，是因为单次检查必然扑空。

（`mute_xiaoai: false` 时不打断她，那是明确要求听完。）

**回答播完了又冒出最后一句的开头，然后才断**
音箱把音频当音乐播、播完立刻从头重播，而"停止播放"指令要绕云端一圈，慢半拍就会
听见重复的开头。本版本除了按预估时长提前发停止，还会**盯住取流**：设备一开始重播
就必然重新取流，一看到就补发一次停止，把重复掐在开头。

**接了追问窗口，但说话没反应**
窗口只决定"这句话要不要交给 AI"，麦克风归音箱管：不唤醒就不记录。所以追问时要
说「小爱同学，……」；日志里出现「追问窗口内：这句直接交给 AI」才说明设备记下来了。

**登录失败 / `Login failed`**
小米风控。用 `python login_qr.py` 扫码登录，别用密码。凭据过期时重扫一次即可。

**该接管的没接管，或不该接管的被抢了**
开 `-v` 跑一轮，日志里每条记录都会打出 `[record]`（小爱回答原文）和 `[learn]`
（判定结果与命中原因）。把日志留着就能定位。误判时可以把对应词干从
`fallback_answer_keyword` 里删掉，或删掉 `learned_fallbacks.json` 里那一条。

**`讲个笑话` / `讲个故事` 这类请求**
小爱会走**内置音频源**播放节目，而不是语音回答，因此不适合作为判断样本。

**追问窗口期说了设备指令**（如「关灯」）
会被 AI 抢答，这是已知取舍，把 `follow_up_seconds` 设为 0 可关闭窗口。

## 感谢

- [xiaomi](https://www.mi.com/)
- @[Yonsm](https://github.com/Yonsm) 的 [MiService](https://github.com/Yonsm/MiService)
- [Tetos](https://github.com/frostming/tetos) TTS 云服务支持
- @[frostming](https://github.com/frostming) 重构了代码并支持`持续会话功能`
- @[pjq](https://github.com/pjq) 给了上游项目非常多的帮助
- [PDM](https://pdm.fming.dev/latest/)
