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

累计差异（截至本文件所在提交）：44 个文件，+4339 / −1943 行。
（该数字由 `git diff --stat 3de0af6` 的 37 个文件 +3065 / −1943 汇总，
加上七个未跟踪的新增文件 —— `device.py` / `tts/http.py` / `fallback.py` /
`learn.py` / `providers.py` / `bot/openai_compat_bot.py` / `tts/doubao.py`
—— 的全部行数得到；`git diff` 本身不计未跟踪文件。）

---

## 2026-09-19 — 修复多轮 TTS 截断、重复与队列挂死

- 把“是否交给 AI”与“是否命中兜底话术”拆开：触发词、持续对话、追问不再
  等待兜底分类，所有 AI 路径都立即进入持续静音。
- `停止播放` 等内部指令在进入 `last_record` 前过滤，不再被误当成用户插话
  而截断模型流。
- 静音任务按轮次分配所有权；AI 播放前会撤销静音权并等待后台任务退出，旧轮次
  无法再发送迟到的停止命令。
- `FileTTS` 改为有界生产者/消费者队列：合成异常原样传播，取消会回收 worker
  和上游模型流，临时音频在本轮结束后删除。
- 豆包默认请求 3 秒明确句尾静音，L05C 在静音区内提前 2.5 秒发送停止；配置
  为 0 时完全不提前，避免切掉回答正文。
- 音箱未在超时内取流时终止本轮，不再从超时时刻重新等待完整音频时长；后者会
  让下一段覆盖迟到开播的上一段，造成确定性漏播。
- L05C 接管只发送一次停止并等待云端命令落地后才允许 AI 播放，避免多条迟到
  命令误杀 AI。由于问题来自小米对话历史（回答开始后才可见），设备原声开头仍
  可能漏出数秒；真机已确认音乐 URL、临时音量和空 TTS 都不能提前抢占。
- 新增 `tests/test_tts_pipeline.py`，覆盖路由、内部指令、静音所有权、异常传播、
  取消清理和豆包收尾策略；补回可直接运行的 `check_mute.py`、`check_tts.py`。

---

## 2026-09-19 — 让"接管"真的瞬间：先开火 + 补词干

用户反馈：AI 接管了，但小爱那句"答不上来"还是会念出来。

### 根因一：判定本身先花了 1~2 秒（这条最关键）

用户日志里有这一行：

```
[learn] 判定为兜底：'这个问题我暂时还回答不上，需要再学习一下'
```

`[learn]` 出现就意味着**词干表没命中**，于是先跑了一次 LLM 分类。等分类结果回来，
她已经把整句念完了——这跟"掐得快不快"无关，是**根本没轮到掐**。

对比一下：L05C 固化的词干是「这个我暂时还回答不上」，而真机这次的说法是
「**这个**问**题**我暂时还回答不上，需要再学习一下」——多了个"题"字，子串匹配
就漏了。修法是把词干换成更短的辨识部分「我暂时还回答不上」，「这个…」与
「这个问题…」两种变体都能覆盖（已加断言：两种真机说法都命中、正常回答不误伤）。

教训写进 README：**兜底词干表要取"辨识部分"而不是整句**，否则每次都要多花一次
分类调用，还顺带让接管慢一两秒。

### 根因二：掐的动作慢了两拍

原来是"每 1.5 秒查一次她在不在播，查到才发停止指令"，加上指令本身的云端往返，
用户能听完半句。现在：

| | 之前 | 现在 |
| --- | --- | --- |
| 检测 | 每 1.5 秒查一次 | 前 3 秒每 0.3 秒查一次，之后 1 秒 |
| 首次开火 | 要等检测到才开始 | **一上来就发**（记录比声音先到，这一枪正好落在她刚出声时） |
| 补枪 | 每 1.5 秒最多一次 | 每 0.5 秒最多一次，直到我们要出声 |
| 内部节奏 | — | 0.15 秒（纯本地，不产生请求） |

离线断言（`check_mute.py`）：第一条动作必须是停止指令；窗口内反复检查；她在播就
继续补枪；她没在播时只保留开头那一枪、不刷屏；要出声时任务被取消且取消后不再发。

代价：一次接管最多多几条 MIoT 指令（都在她确实在播时才发），换来的是"半句都听
不到"。如果哪天小米的风控对这个频率敏感，把 `MUTE_CHECK_FAST` 调大即可。

---

## 2026-09-19 — 持续对话记忆 + 前缀缓存命中

用户诉求：**别说了上文忘下文**，同时**把大模型的缓存命中率做上去**（省钱）。
两件事其实是同一个设计问题，先说结论，再给实测。

### 先摸底：DeepSeek 的前缀缓存到底认什么

拿真实 key 做对照实验（同一账号、同一前缀重复请求两次，看第二次的命中）：

| 前缀规模 | 命中 |
| --- | --- |
| 7 / 28 / 104 tokens | **0** —— 太短根本不会被缓存 |
| 408 tokens | 256 |
| 819 tokens | 640（78%） |
| 同前缀、加 3 秒间隔 | 不变（不是"写缓存要时间"） |

结论两条：**命中以 64 tokens 为单位**（408→256、819→640），且**尾部约
150~250 tokens 永远算未命中**。所以：短问答没有可省的空间；稳定前缀越长，
命中率越高——这正好和"保留完整对话历史"是一件事。

### 改了什么

| 之前 | 现在 |
| --- | --- |
| 提示词拼进第一条用户消息（`query + config.prompt`） | 提示词进 **system**，位置固定 |
| `add_message` 里"只留第一轮 + 最近 5 轮" | **只追加**，不再砍中间 |
| 追问窗口过期 → `clear_history()` | 不清历史，改用**「新一轮对话」标记**（只动追加部分） |
| — | 超过 `history_budget_chars` 时，把**最老的一半**压成摘要放进 system |
| 看不到缓存情况 | 每次回答后打一行 `[cache] 输入 X：命中 Y（Z%）` |

为什么"只留最近 N 轮"必须删掉：那等于每轮换一个新前缀，缓存全部失效。而"攒着
一次压掉最老的一半"只在压缩那一次失效，之后又能长时间稳定命中——压缩因此默认
设在 16000 字（约几十轮）才触发。

### 实测（用配置里真实的提示词跑 6 轮）

| 轮次 | 输入 tokens | 命中 |
| --- | --- | --- |
| 1 | 70 | 0 |
| 2 | 105 | 0 |
| 3 | 173 | 0 |
| 4 | 280 | 128（46%） |
| 5 | 384 | 128（33%） |
| 6 | 493 | **256（52%）** |

同时验证了记忆本身：第 2 轮能复述上一句、第 4 轮能总结已聊内容、**第 6 轮能
答出"你最开始问的是让我用一句话介绍自己"**——这正是"别忘上文"。

### 顺带

- 「配音参谋」「历史压缩」「兜底分类」三种内部调用全部**无状态**（每次先清历史）
  且静音：它们跟主对话共用历史只会污染前缀、白花 token
- 流式请求按 provider 决定是否带 `stream_options.include_usage`（DeepSeek 实测
  支持，MiMo/GLM 未实测故默认不带；可用 `stream_usage: true/false` 覆盖）
- 又踩了一次 rich 的坑：`print("[cache] …")` 被当成样式标记吃掉，日志里只剩
  "输入 70 tokens"——写成 `\\[cache]` 才显示（仓库里 `[record]` 有同样的注释）
- `learn.py` 的分类调用之前会带着历史越滚越长，现在每次清空

---

## 2026-09-19 — 真机第三轮：日志混行、播完重复一句、追问窗口说不清

用户第三次真机反馈里的三条，逐条对应：

### 1. 回答正文里插进了「按场景配音：…」

那行字是**打印**的，不是被念出来的（音频只来自模型回答的句子流），但它正好插在
回答正文中间——因为配音参谋是在"第一句已经打印、还没开始合成"的缝里落地的。

改成：应用时不打印，回答播完后跟总结一起出一行：

```
以下是 DeepSeek 的回答：好啦，别不开心了……
回答完毕（按场景配音：用温柔轻声哄人的语气说（speech_rate=-15，loudness_rate=-10，pitch=2））
```

### 2. 播完又冒出最后一句的开头，然后才断

音箱把音频当音乐播、**播完立刻从头重播**；而"停止播放"指令要绕云端一圈，落地比
音频结束晚一点，用户就听见重复的开头（这次是"我给你讲个小事"）。

原来只按预估值提前 0.4 秒发一次停止，赌它能在重播前落地。现在加一道**反应式**
保险：设备重播必然**重新来取流**，所以发完停止后再盯 1.5 秒取流列表，一看到同一
文件又被取就立刻补发一次停止——不再赌延时。离线断言：检测到重播就补发且 1 秒内
收工、没重播只发一次且窗口有上限、别的文件的取流不算重播、无指令通道的型号直接
返回。

### 3. 追问窗口"没什么用，并不会真的听取我的声音"

代码逻辑是对的，**说错的是文档和我之前的话术**：窗口只决定"这句话要不要交给 AI"，
麦克风归音箱管——**不唤醒就不记录**，我们什么都看不到。所以：

- 提示语改准：不再说"不用唤醒词"，改成"接着问就行（不用触发词，但还是要先叫
  「小爱同学」唤醒设备）"——唤醒词会被小米接口从记录里剥掉，不干扰判定
- 新增一行可验证的日志：真在窗口里接管时打印「追问窗口内：这句直接交给 AI」，
  看不到这行就说明设备压根没把它记下来
- README 的追问说明与排错条目同步改写（含"设备自带连续对话才能省掉唤醒词"）

顺带把"掐小爱"的冷却从 2 秒收到 1.5 秒（冷却管的是**检查**，她没在播就不发指令，
所以只在她真说话时才多几条 MIoT 指令），窗口从 15 秒收到 12 秒——反正第一句出声
时我们就主动收手了。

---

## 2026-09-19 — 两处真机反馈：豆包音量偏小、接管时没掐断小爱

### 1. 音量：豆包比小爱本嗓轻约 5 dB

用户反馈"用豆包播放时声音比小爱自己说话小"。实测（同一句话合成多次、ffmpeg 解码
后量 RMS / 峰值）：

| `loudness_rate` | RMS | 峰值 |
| --- | --- | --- |
| 不传 / `0`（上游默认） | −22.8 / −22.2 dBFS | −8.1 dBFS |
| `+20` | −20.2 dBFS | −6.6 dBFS |
| `+40` | −19.3 dBFS | −5.1 dBFS |
| `+60` | −18.1 dBFS | −4.1 dBFS |
| `+80` | −17.4 dBFS | −3.1 dBFS |
| `+100` | −17.5 dBFS | −2.0 dBFS |

两点结论：**豆包默认输出确实偏轻**（RMS −22 dBFS 对语音偏小）；`loudness_rate`
过了 +60 就被服务端压限（RMS 不再涨），但峰值一直有余量，所以**加音量是安全的**。

于是新增 `loudness_boost`（默认 `50`）：在场景音量之上固定再抬一档。
经生产代码路径实测：`0` → −23.2 dBFS、`50` → **−18.1 dBFS（+5.1 dB）**、
`80` → −15.0 dBFS。嫌小继续加，`0` = 不补偿。

**语义要看清**：场景配音给的 `speech_rate` / `loudness_rate` 是"这一句用多少"
（会覆盖配置里的同名默认值），而 `loudness_boost` 永远叠加、不参与逐句覆盖——
这样"哄睡轻一点"不会被补偿抹平（实测 −18.1 → −21.1）。

### 2. 接管时没有立刻掐断小爱

用户反馈：她答不上来的那句"这个我暂时还回答不上诶，我要再学习学习"还是整句念完，
之后才轮到 AI。

根因是**对话记录比声音先到**：记录里 `answers` 已经填好（我们据此判定接管）时，
她往往还没开口，而旧代码只查一次 `get_if_xiaoai_is_playing()` —— 必然扑空。等她
真的开口，人已经在听她把整句念完了。

修法：判定接管后起一个"持续掐"的任务——每 0.5 秒看一眼、最多每 2 秒发一次停止
指令、窗口 15 秒；**到我们要出声前立刻收手**（再掐下去会把我们自己的音频也停掉）。
`mute_xiaoai: false` 保持旧行为（不打断、等她说完），那是明确要求"听完"。

顺带一条调试经验：这个循环里的异常是**故意吞掉**的（掐不断不能影响回答），所以
循环里出错只会留一行 debug 日志。我自己就踩过——测试脚本漏了一个 `import time`，
被吞掉的 NameError 让循环"看着像只跑了一轮"。查这类问题时把 `-v` 打开。

离线验证按三种语义断言：窗口内反复检查（0.9 秒 9 次）、按冷却发指令、没在播就
不发指令、要出声时任务被取消且取消后不再发、取消后 `_mute_task` 清空。

---

## 2026-09-19 — 修复「豆包合成好了但音箱不响」

用户真机第一次跑豆包 TTS 的日志：

```
INFO     Serving on 198.18.0.1:8063
...
WARNING  等 tmpzwf2p0ui.mp3 的取流超时，按未开始播放处理
```

**问题不在豆包，在"告诉音箱来哪个地址取音频"这一步**：`get_hostname()` 用的是
上游那一招——连一下 `8.8.8.8`，看本机被分配了哪个地址。装了 Clash / Mihomo 这类
**TUN 代理**之后，去 8.8.8.8 的流量会走进虚拟网卡，拿到的是 `198.18.0.1`
（RFC2544 保留段，Clash 拿它当 fake-ip）。音箱在 `192.168.1.x`，够不着这个地址，
于是**永远不来取流**：音频明明合成好了，它一声不吭，日志里只有一句取流超时。

这个坑与豆包无关（edge 一样会踩），只是这次才被日志暴露出来。

### 修法

`get_hostname()` 改成：枚举本机所有 IPv4 → 滤掉根本不可能被音箱访问的地址
（`198.18/15`、`169.254/16`、`127.`、`0.`）→ 按 `192.168.x` → `10.x` →
`172.16~31.x` 排序挑最优 → 候选多于一个时把选择结果打印出来。
`XIAOGPT_HOSTNAME` 依然最优先（多网段时手动指定）。

本机实测（Clash 开着也一样）：

```
候选: ['172.23.80.1', '192.168.1.9']     ← 198.18.0.1 已被滤掉
[tts] 本机有多个地址 [...], 选 192.168.1.9 给音箱取流
选中: 192.168.1.9
```

另外，音箱没来取流时的日志从"等 xxx.mp3 的取流超时"改成直接打出**它被要求访问的
URL** + 排查清单（同网段 / 防火墙 8050-8089 / XIAOGPT_HOSTNAME）——这条日志以前
只说"超时"，看不出到底是地址不对还是被防火墙挡了。

### 顺带修掉：配音参谋的 JSON 出现在回答里

第一次真机运行时终端长这样：

```
以下是 DeepSeek 的回答：别难过啦，我在这儿陪着你呢。今天不管{"instruction": "用温柔轻声的语气说", ...}
发生了什么，都已经过去了…
```

`OpenAICompatBot.ask()` 会把模型回复打印出来（主对话需要它，终端就是给人看的），
而"配音参谋"走的是同一个 `ask()`，于是它回的 JSON 被打印，正好和流式回答交错在
一起，看着像被念出来了。修法：

- 给 bot 加 `quiet` 标志，参谋实例设为静音（主对话照旧打印真实回答）
- 参谋每次调用前清空历史（它是无状态的，上一句的场景不该影响这一句）
- 只在套用时打一行 `按场景配音：用温柔轻声的语气…（speech_rate=-15，loudness_rate=-10）`

### 验证

- **取流链路在局域网地址上端到端实测**（不需要音箱）：本地起同一个
  `RangeRequestHandler`，按 ffmpeg 的三次请求打过去——探测 `bytes=0-` 回 206 且
  `Content-Range` 正确、跳尾 `bytes=<size-4096>-` 回 206 且字节与原文一致、
  完整请求回 200 且内容一致、越界 Range 回 416、取流回调记录的正是文件名
  （播放起点判定依赖它）
- 地址选择：本机多网卡（Hyper-V + Clash fake-ip + 真实网卡）下选中 `192.168.1.9`，
  `XIAOGPT_HOSTNAME` 仍能覆盖
- `black --check .`、`python -m xiaogpt --help` 与 7 个离线检查脚本全过

---

## 2026-09-19 — 豆包 TTS 真机联调：按场景自动配音 + 参数实测

拿到真实 API Key 之后的这一轮：跑通了真机合成，量出了**哪些参数真的生效**，
并据此实现了「根据用户说的话自动决定这句该怎么念」。

### 先说结论：豆包的参数不是文档写什么就生效什么

判定方法：同一音色、同一段话（11.5 秒的长文本）各合成 3~5 次，比对时长、
能量（RMS）、基频（自相关法）——合成本身有随机性（同样请求两次时长能差 0.4s），
所以单次比较没有意义，只能比分布。

| 参数 | 实测 |
| --- | --- |
| `audio_params.speech_rate` | **有效**：`50` → 4.79s 变 3.16s；`40` → 11.52s 变 8.41s |
| `audio_params.loudness_rate` | **有效**：`-50` → RMS 0.086 变 0.051（且波动同步变小） |
| `additions`（字符串） | **有效**：`silence_duration: 5000` 精确加 5.07s |
| `additions.explicit_dialect` | 接受、不报错；口音**未验证**（没有可判定口音的仪器，样音留给耳朵） |
| `context_texts`（语音指令） | **收下但不生效**：极慢指令 5 次采样比值 1.00；愤怒/ASMR 指令的基频中位数 255/257 对基线 259，能量与过零率同样在噪声内 |
| `post_process.pitch` | `±10` 对基频无可测影响，疑似同样被忽略 |

顺带把两种编码都验证了，免得赌：

- `additions` **必须是 JSON 字符串**。传对象时服务端直接报
  `json: cannot unmarshal object into Go struct field TTSReqParams.req_params.additions of type string`
  ——文档参数表标 `string` 是对的，嵌套对象的写法是错的。
- 官方最佳实践里的 `[#指令]` 语法**只属于控制台界面**：把它内联进 `text` 会被
  **念出来**（时长增量 ≈ 标签字数 × 0.25s，实测 `[#苹果香蕉橘子西瓜]` +2.4s），
  放进 `context_texts` 也不生效。
- 错误帧的字段顺序，官方 Java 与 Python 两个实现自己不一致（Python 的 marshal
  多写一个 sessionId）；解析时用「载荷长度必须顶到帧尾」分辨，两种都能读。

### 于是「按场景自动配音」落在了真正生效的旋钮上

新增 `tts_auto_instruction`（默认开，仅 `tts: doubao` 生效）：每句回答之前，
让模型看一眼**用户刚说的那句话**，回一行 JSON：

```json
{"instruction": "压低嗓音、语速放缓地轻声说",
 "speech_rate": -25, "loudness_rate": -20, "pitch": -4, "dialect": ""}
```

`speech_rate` / `loudness_rate` / `dialect` 直接进请求；`instruction` 照文档写进
`context_texts`（现在不生效，但上游若在权限或端点上放开就自动生效）。也就是说：
**情绪效果目前来自语速与音量**，而不是那个语气字段本身。

真机实测（DeepSeek 出方案 + 豆包合成同一段话）：

| 用户说 | 参谋方案 | 合成时长 |
| --- | --- | --- |
| 给我讲个恐怖故事 | 语速 -25、音量 -20 | 14.21s |
| 我睡不着，哄哄我吧 | 语速 -30、音量 -30 | 16.08s |
| 讲个笑话逗我开心 | 语速 +15、音量 +10 | 9.79s |
| 请解释一下什么是量子纠缠 | 语速 -10、音量 0 | 13.39s |

工程上的几个取舍：

- **和回答并行跑**：参谋任务在提问时启动，等回答第一句到手时再取结果（最多等
  2 秒），实测参谋 0.65~1.77 秒完成，所以基本不增加首句延迟
- **专用 bot 实例**：不能复用主对话——把「你是配音导演…」写进主历史，下一轮模型
  会把导演指令当上下文，回答直接跑偏
- **失败一律退回配置默认**：解析不出 JSON、调用超时、值超范围（数字夹到边界、
  方言名不认识、类型不对）都只是"这句不调"，绝不让配音把回答卡住或说错话
- **手动优先**：用「语音指令 xxx」设过一次，本次运行不再自动覆盖——显式意图 >
  自动判断

### 顺带

- `tts: doubao` 的配置写进了 `config.yaml`（含真实 key）、`xiao_config.yaml.example`
  加了解释与关闭方式
- README 的豆包小节改成「哪些参数真的生效」的实测表，而不是照抄文档

---

## 2026-09-19 — TTS 收敛为 mi / edge，并新增豆包（含语音指令）

**结论先行**：`tts` 只剩 `mi`（小爱原生）和 `edge`（微软，免费），
**新增 `doubao`**（火山引擎豆包语音合成大模型，自带语音指令：情绪 / 方言 / 语气 /
语速）。删掉 openai / azure / google / baidu / volc / fish / minimax 七家，以及
那条只为 fish 存在、在本 fork **从未验证过**的流式播放链路。

| `tts` | 谁在说话 | 音色 | 说话方式 |
| --- | --- | --- | --- |
| `mi` | 小爱自己的嗓子，不依赖外网 | 固定 | 无 |
| `edge` | 微软 edge-tts | `tts_options.voice`（14 个中文音色） | `rate` / `pitch` / `volume` |
| `doubao` | 豆包语音合成大模型 2.0 | `tts_options.speaker` | **语音指令** + 方言/语速/音量/音调 |

### 为什么删

- 上游 `tetos` 支持九家，但本 fork 只配过 `edge` 和 `fish`；其余七家的依赖
  （各家 SDK）纯属陪跑
- `fish` 走的是"边合成边播"的流式链路，在 L05C 上**没跑通也没验证**过——
  `device.py` 里那个 `LIVE_TAIL_SECONDS`（"这个值是估的，未验证"）就是它的产物。
  删掉这条链路之后，剩下的 mi / edge / 豆包**全都走文件模式**，也就是已经调通的
  「落地音频 → 音箱来拉 → 按音频时长等 → 显式停止」那一套
- 代价是首句延迟比流式略高（要等第一句合成完），换来的是播放/停止行为可预测

### 豆包 TTS 是怎么接的

官方这个接口**没有 SDK**，只有文档 + 一份示例代码（`websocket unidirectional.zip`）。
`tts/doubao.py` 是按官方示例的 `protocols.py` 逐行对照写的：

> 4 字节二进制头（版本 / 头长 / 消息类型 / 标志 / 序列化 / 压缩）+ JSON 载荷，
> 音频以二进制帧流式返回；事件号区分 TTSResponse（352）/ SessionFinished（152）等。

写协议解析踩到的两个坑，都写进代码注释了：

1. **`additions` 是 JSON 字符串，不是嵌套对象**。文档参数表标的是 `string`，
   发音词典示例也是 `"additions": "{\"tone\":[...]}"`——写成对象服务端会解析失败，
   而报错未必指向这里。`explicit_dialect` / `explicit_language` 这些也在这串
   JSON 里。
2. **错误帧的字段顺序，官方两个实现自己就不一致**：Java 是 事件→错误码→载荷，
   而官方 Python 的 `marshal` 会多写一个 sessionId。这里用「载荷长度必须正好顶到
   帧尾」这条硬约束来分辨，两种都能读——否则鉴权失败时拿到的是一串乱码，
   而不是 `invalid api key`。

握手被拒（401/403）单独翻译成人话："豆包 TTS 鉴权失败，检查 tts_options.api_key，
并确认已开通「语音合成大模型」"。**实测拿假 key 打真端点，就是这条**（见下）。

### 语音指令：配一次，或者用说的改

配置里写一次：

```yaml
tts: doubao
tts_options:
  api_key: "..."
  speaker: zh_female_vv_uranus_bigtts
  instruction: "用特别特别痛心的语气说话吗?"   # → context_texts
  dialect: sichuan                            # → additions.explicit_dialect
  speech_rate: 10                             # → audio_params.speech_rate
```

运行时用说的改（新增配置项 `change_tts_instruction_keyword`，默认 `["语音指令"]`）：

```
你：小爱同学，语音指令 用四川话温柔一点
小爱：好的        ← 之后所有回答都按这个来；重启回到配置文件里的值
```

设计上有几点是刻意的：

- 指令**只存在内存里**（改的是 TTS 实例），重启回配置值——不做落盘，免得"我明明
  改回去了它怎么还是四川话"这种幽灵状态
- 小爱原生 TTS 与 edge 没有这个概念，`TTS.set_instruction()` 返回 False，音箱会
  明说"当前音色还不支持语音指令"——**答应了却什么都不变比不答应更糟**
- 这条命令处理完就 `continue`，不会把"语音指令 xxx"当成问题再问一遍 AI
- 与提问路径一致：先把小爱自己的回答掐掉，再说确认语（避免两句话叠在一起）

限制（来自官方文档，已写进 README）：语音指令只有**豆包语音合成模型 2.0** 的音色
支持（音色 ID 带 `_bigtts`），声音复刻（`seed-icl-2.0`）不支持；方言也要求音色本身
支持方言。

### edge 音色可以配了，中文音色列全在示例里

```yaml
tts: edge
tts_options:
  voice: zh-CN-YunxiNeural   # 留空 = 按语言自动挑（和以前一样）
  rate: "+10%"
```

`xiao_config.yaml.example` 里把 `edge-tts --list-voices` 的**全部 14 个中文音色**
连同特点列了出来（普通话 6 + 东北话 + 陕西话 + 粤语 3 + 台湾国语 3），其它语种
给了一行命令自查（共 300+）。

### 文件与配置变化

| 动作 | 内容 |
| --- | --- |
| 新增 | `xiaogpt/tts/doubao.py` —— 火山单向流式协议 + `DoubaoSpeaker` |
| 删除 | `xiaogpt/tts/live.py`（只为 fish 存在的流式播放链路） |
| 改名 | `TetosFileTTS` → `FileTTS`，speaker 由 `make_speaker()` 按 `tts` 造，edge 与豆包共用 |
| 新增 | `TTS.set_instruction()` 钩子（默认返回 False），`FileTTS` 转发给 speaker |
| 删除 | `device.py` 的 `LIVE_TAIL_SECONDS`（未验证的流式收尾等待） |
| 删除配置 | `volc_access_key` / `volc_secret_key`，CLI 的 `--volc_*` 与 `--fish_*` |
| 新增配置 | `change_tts_instruction_keyword` |
| 新增校验 | `tts` 白名单；`tts: doubao` 缺 `api_key`/`speaker` 直接拒绝启动；`tts_options` 里拼错的键在造 speaker 时报出来（不静默忽略） |

豆包用的 `aiohttp`（已是本项目依赖）说 websocket，**没有新增依赖**；`tetos` 仍然
保留，但只剩 edge 在用。

### 实测（2026-09-19）

离线（`check_tts.py`）：

- **编解码与官方示例逐字节对齐**：`build_request()` 的输出 == 官方 `protocols.py`
  的 `marshal()`；官方构造的纯音频帧 / 带事件与 sessionId 的音频帧 / 会话结束帧 /
  错误帧，本实现都能解析出正确的 type/event/session/payload
- **请求体**：语音指令 → `context_texts`、方言/语种/过滤开关 → `additions`
  （且是 JSON 字符串）、语速音量音调各就各位；未配置的字段不出现
- **端到端**：起一个本地假 WebSocket 服务端按协议回两帧音频 + SessionFinished，
  `DoubaoSpeaker.synthesize()` 拿到完整音频、写出文件、返回时长；请求头的
  `X-Api-Key` / `X-Api-Resource-Id` / `X-Api-Request-Id` 都在
- 错误帧 → 抛出可读异常；`tts: doubao` 缺参数、`tts: fish`、拼错的 `tts_options`
  键都被拦住

在线：

- **edge 真实合成成功**：`zh-CN-YunxiNeural` 18KB / 2.23s、`+10%` 语速生效；
  东北话音色 `zh-CN-liaoning-XiaobeiNeural` 同样正常
- **豆包端点可达**：用假 key 打真实 wss 端点，服务端在握手阶段就返回 **401**，
  并被我方翻译成"鉴权失败，检查 api_key"——说明地址、请求头、握手方式都对，
  只是没有真 key

未验证：

- **豆包没有真实 key，没跑通真实合成**（协议层与官方示例对齐、假服务端端到端通过，
  但真机音色、语音指令效果没听过）
- 真机音箱播放链路（豆包 → L05C → 停止）未跑；离线逻辑与 edge 完全共用，风险主要在
  音频格式（默认 mp3/24000Hz，与 edge 一致）

---

## 2026-09-19 — bot 收敛为三家：DeepSeek / MiMo / GLM

**结论先行**：删掉 `chatgptapi` / `gemini` / `doubao`，只留三家 OpenAI 兼容的
provider；新增**小米 MiMo** 与**智谱 GLM**。三家协议相同，所以现在是**一张表
（`xiaogpt/providers.py`）+ 一个实现（`xiaogpt/bot/openai_compat_bot.py`）**，
而不是三个各自维护 `ask` / `ask_stream` 的类。

### 为什么能合并

`chatgptapi`（openai SDK）、`gemini`（google-generativeai）、`doubao`
（volcengine-python-sdk）各用一套独立 SDK、各自的鉴权与返回解析，这才是上游
bot 目录十来个文件、几百行重复代码的来源。而 DeepSeek / MiMo / GLM 三家官网
给的都是**OpenAI Chat Completions 兼容端点**（MiMo 与 GLM 的官方文档都把它
写在第一行），差异只剩三处：接口地址、默认模型、**思考模式语义**。

| `bot` | 官方接口 | 默认模型 | 更强的一档 | 文档 |
| --- | --- | --- | --- | --- |
| `deepseek` | `https://api.deepseek.com` | `deepseek-flash` | — | <https://platform.deepseek.com> |
| `mimo` | `https://api.xiaomimimo.com/v1` | `mimo-v2.5` | `mimo-v2.5-pro` | <https://mimo.mi.com/docs> |
| `glm` | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.3-flash` | `glm-5.3` | <https://docs.bigmodel.cn> |

默认模型一律选各家**最快最便宜**的那档（语音助手场景延迟比智商重要），要质量
就在 `<provider>_model` 里换成旗舰。

### 最容易踩的坑：三家的思考模式语义完全不同

这是本次改动里唯一不能"一套开关硬套"的地方，**按厂商分别处理**，全部记在
`providers.py::ThinkingPolicy` 里（含注释里的取舍理由）：

| `bot` | 官方默认 | 本版本默认 | 可关闭 | 有 `reasoning_effort` | 开思考后 `temperature`/`top_p` |
| --- | --- | --- | --- | --- | --- |
| `deepseek` | 关 | 关（延迟最低） | 是 | 是 | 必须剔除（否则报错） |
| `mimo` | **开** | 关（延迟最低） | 是 | 否 | 会静默忽略 → 一并剔除 |
| `glm` | 开，且 **glm-5.3 系列关不掉** | 开 + `reasoning_effort: low` | **否** | 是（low/high/max，默认 max） | 支持，故保留 |

两个具体后果：

1. **GLM 传 `thinking.type=disabled` 会被 API 拒绝**（官方迁移提示明确要求改成
   `enabled` + `reasoning_effort: low`）。让这种配置走到 API 的表现是"音箱一声
   不吭"，所以就地校正为开启并**打印一次**警告，只在首次纠正时提示，不刷屏。
2. MiMo 没有 `reasoning_effort`，但"用户想开思考"这层意图是明确的：此时只发
   `thinking: {type: enabled}`，**不发**这个 MiMo 不认识的参数（发了会被拒）。

`gpt_options.thinking` 作为逃生舱原样透传给厂商（写 `clear_thinking` 这类私有
字段时需要）；一旦用户自己写了 `thinking`，就不再注入默认的 `reasoning_effort`，
不覆盖显式选择。

### 文件变化

| 动作 | 内容 |
| --- | --- |
| 新增 | `xiaogpt/providers.py` —— provider 元数据表（接口地址 / 默认模型 / 思考策略） |
| 新增 | `xiaogpt/bot/openai_compat_bot.py` —— 三家共用的实现 |
| 删除 | `deepseek_bot.py` `chatgptapi_bot.py` `gemini_bot.py` `doubao_bot.py` |
| 简化 | `bot/__init__.py` 的 `BOTS` 变成"同一个类注册三份"，`get_bot` 先查名字再构造 |

`providers.py` **不 import 本项目任何模块**：`config.py` 在 import 期就要读这张
表，反向依赖会绕成循环 import（`config` → `bot` → `base_bot` → `config`）。

### 配置变化

| 动作 | 项 |
| --- | --- |
| 新增 | `mimo_api_key` / `mimo_model` / `glm_api_key` / `glm_model` |
| 删除 | `openai_key` / `gemini_key` / `gemini_model` / `gemini_api_domain` / `volc_api_key` / `deployment_id` |
| 语义变化 | `api_base` 从"Azure OpenAI 专用"变成**通用地址覆盖**（配了就顶掉所选 provider 的官方地址，给代理 / 自建网关用） |
| 默认值变化 | `bot` 默认从 `chatgptapi` 改成 `deepseek` |
| 别名 | yaml 里 `use_mimo: true` / `use_glm: true` 等价于 `bot: mimo` / `bot: glm`（与既有的 `use_deepseek` 一致） |
| 校验 | bot 名字合法性、以及"所选 provider 的 key 配了没"，统一由 provider 表推导；key 用 `strip()` 后判空，`glm_api_key: "   "` 也算没配 |

删掉的字段若还留在旧 `config.yaml` 里会被**静默忽略**（`read_from_file` 只收
dataclass 里存在的键），不会报错。

### 依赖精简

`pyproject.toml` / `requirements.txt` 删掉随 bot 一起失去用途的依赖：
`zhipuai`（GLM 走 OpenAI 兼容端点，用不上官方 SDK）、`google-generativeai`、
`google-search-results`、`dashscope`、`groq`、`numexpr`、`beautifulsoup4`、
`langchain`、`langchain-community`、`volcengine-python-sdk`。

**同时补了一条隐性依赖**：`requests`。`xiaogpt/utils.py` 一直用
`requests.utils.cookiejar_from_dict`，但它过去是被 langchain / dashscope 顺带
装上的——删掉那几个包之后，干净环境会直接 import 失败，所以这回显式写进
`pyproject.toml`。

**只删顶层，不动传递依赖**：langchain 一家的传递依赖就有 langchain-core /
sqlalchemy / jsonpatch / tenacity 等三十来个，手工从 `requirements.txt` 里
挑着删很容易误伤（例如 `cachetools`、`rsa` 看着像 langchain 的，其实是
google-auth 的运行时依赖，而 google-cloud-texttospeech 还在用）。这类清理
交给解析器做才对。因此：

- `pdm.lock` **没有重新生成**（改这次的环境里没有 pdm），仍带着上述包
- 下次有网络时跑一次 `pdm lock`，重新导出 `requirements.txt`，残留的传递依赖
  会自然消失（PDM 在 `pdm install` 时通常也会自动重新锁定）

### 实测（2026-09-19）

离线：用 mock transport 抓 bot 真实发出的请求体，**30 项断言全过**——
三个 provider 的 url / model / thinking 默认值、`reasoning_effort` 的翻译与剔除、
`temperature` 在思考模式下的保留与剔除、`api_base` 覆盖、流式链路能拼出完整句子、
模型名写错会以 SystemExit 中止并列出可用模型。

在线（真机，经本机 7890 代理）：

- **DeepSeek 全链路正常**：`config.yaml` 的真实 key + bot 代码路径，启动校验通过，
  提问返回真实回答
- **MiMo / GLM 端点可达**：用假 key 探测 `POST {base}/chat/completions`，两家都返回
  **HTTP 401**（而不是 404 / DNS 失败）——地址、路径、鉴权头写法都对，只是 key 不对

未验证：

- MiMo / GLM 没有真实 key，**没跑通完整问答**（模型名与思考模式参数按官方文档写，
  默认模型取自两家的 `/models` 文档与模型页）
- GLM 是否提供 `GET {base}/models` 未知；不提供时只会 warn 一句然后跳过校验
- 真机语音链路（唤醒 → MiMo/GLM → TTS 回放）未测

---

## 2026-09-19 — 兜底接管：不用触发词也能正常提问

**结论先行**：现在不必每次都说「请 xxx」——小爱自己答不上来的问题会自动转给 AI。
代价是必须先把小爱的兜底话术**在真机上采出来**，所以默认关闭；采样与后续维护
交给学习模式自动完成。

**先纠正一个前提**：「请」不是唤醒词。流程是「小爱同学，请 xxx」——「小爱同学」
唤醒设备，「请」决定这句话归谁处理。本改动解决的是"用正常说话方式提问"，
不是"免唤醒"。

### 采样发现（2026-09-19，L05C）

| 问题 | 结果 |
| --- | --- |
| 小爱答不上来时的原话 | 「这个我暂时还回答不上诶，我要再学习学习」 |
| `answers` 是否随记录立即可见 | **是**，首次可见即已填充（方案成立的前提） |
| 执行类指令（"停止播放"）的记录 | `answers` 为**空数组** |

第一条印证了采样是必需的：真实话术与凭经验猜的「还没学会」「没听懂」**完全不同**。
第三条决定了**空回答绝不能算兜底**——执行开灯、放歌、设闹钟时 `answers` 也是空的，
把它们当兜底会把小爱的技能全部抢走。

### 方案：三层递进，越靠前越便宜

新增 `xiaogpt/fallback.py`（纯函数）与 `xiaogpt/learn.py`（学习缓存）：

| 层 | 数据来源 | 匹配方式 | 成本 |
| --- | --- | --- | --- |
| 1. 静态词干表 | `fallback_answer_keyword`（配置，人工维护） | 子串 + 前缀窗口 | 零 |
| 2. 学习缓存 | `learned_fallbacks.json`（运行时数据） | 归一化后**整句精确**匹配 | 零 |
| 3. LLM 分类 | 学习模式，且前两层都没命中 | — | 一次 API 调用 |

第 2 层刻意用整句精确匹配而不是子串：让 AI 从一句话里"提炼"短语，提炼得太短
就会把正常回答也匹配掉，而这个失败模式的代价很大。整句匹配最坏只是同义改写要
多分类一次，有界且安全。

### 三道保守化（误判代价远大于漏判）

把小爱的正常回答抢给 AI 是灾难性的，漏判只是退回用触发词。因此：

1. **前缀窗口**（`PREFIX_WINDOW = 12`）：只在回答开头这段里匹配。「还没学会」完全
   可能出现在正常回答里——"讲个机器人的故事" → "机器人说：抱歉，我还没学会"，
   全文匹配就会误伤
2. **短语长度闸门**：短于 2 字直接拒绝启动（写「我」进去会让**每一条**回答都命中，
   静默且灾难性）；短于 4 字只警告（「没听懂」是真实可用的词干）
3. **长回答直接放行**（`MAX_FALLBACK_LEN = 40`）：兜底话术都是短句，超过 40 字连
   AI 都不问。这条既省调用，更挡住 AI 把长回答误判成兜底

另外**回答为空一律不接管**（见上表第三条），**分类器答不出「是/否」时也不接管**
（返回 None → False，宁可漏判）。

### 顺带修掉的崩溃

`xiaogpt.py` 里打印小爱回答那段只 `except IndexError`：

- `answers` 为 `null` → `None[0]` 抛 **TypeError**
- `answers[0].tts` 为 `null` → `None.get` 抛 **AttributeError**

两者都会冲出 `run_forever` 主循环（`cli.py` 没有兜底捕获），把进程打死。
改为统一的 `xiaoai_answer_text()` 安全读取，判定与打印共用同一个函数。

### `need_ask_gpt` 重写与等价性

上游那行 `(A and not W) or B` 的 `and`/`or` 混用可读性很差，多分支下必然更糟。
改写为先判 B、再判 `not W`、最后 `A or 窗口`。

**等价性已用 90 组组合暴力验证**（5 种 keyword 配置 × 2 种对话状态 × 9 种 query，
含 `keyword` 里包含「小爱同学」、大小写、空串等边角），新旧逐值一致。

### 顺带修掉 `re.sub` 的两个隐患

原先判定用 `.lower()` 而剥触发词用的是**大小写敏感**的正则，`HEY` 能触发却剥不掉，
会把触发词原样喂给 AI；且正则**未转义**，keyword 写成 `*` / `(` / `[a` 会让
`re.sub` 直接抛 `re.error`（已实测）。改为复用 `_match_trigger` 的结果按长度切片。

### 追问窗口

不新增第二套路由，与 `in_conversation` 走**同一个分支**，只是布尔来源不同：
`in_conversation` 是用户显式开关且永久，追问窗口是隐式且自动过期。

两个必须做对的点：

- **窗口在 `speak()` 返回之后才开**，不是提问时开——300 字的回答要播一分钟以上，
  按提问时刻起算窗口必然提前过期
- **窗口过期不需要 `wakeup_xiaoai()` 收尾**——音箱本来就是空闲的（AI 音频已被
  显式停掉），再唤醒一次是凭空多一声提示音，与"无感"直接冲突

窗口过期时清空对话历史（`ChatHistoryMixin.clear_history`）：偶尔接管的模式下
history 会变成一串跨小时、彼此无关的问答，模型会自己脑补出连续性。

### 接管的 UX 调整

- **不播「正在问 XX 请耐心等待」**——用户刚听完小爱说"我暂时还回答不上"，
  再听一句提示语是第二次打断
- **自适应等待替代 `sleep(8)`**：`mute_xiaoai: false` 时上游写死 `sleep(8)`，但接管
  路径上兜底话术的原文我们已经拿到了，用 `calculate_tts_elapse` 按它自己的时长等，
  上限仍取 8 秒

### 真机验证时抓到的死循环（本次最重要的一个修复）

首轮真机验证时日志出现 `问题：停止播放？` **无限重复**，停不下来。成因是一条
完整的闭环：

1. 停止循环播放要发 `5-4 停止播放`（见上一节，这是 L05C 上唯一有效的停止方式）
2. **这句指令会被小米的对话接口当成用户说的话记下来**，变成一条新 record
3. 此时正处在追问窗口里，它被当成新问题转给 AI
4. AI 答完又触发一次停止指令 → 回到第 2 步

它还会连带毁掉长回答：新记录不断涌入会让 `ask_gpt` 的流式中断
（`if not self.last_record.empty(): break`），于是一个字都没产出，`speak()` 拿到
空流抛 `StopAsyncIteration`，而 `str(StopAsyncIteration())` 是空串——用户看到的
只有「Deepseek 回答出错 」这种零信息的报错，完全指不到真正原因。

**修复**：登记自己发出去的指令文本，判定时把它们排除。

- `device.py` 新增 `_SENT_DIRECTIVES` / `remember_directive()` / `is_own_directive()`
- `execute_directive()` 发送前登记
- **`wakeup_xiaoai()` 也必须登记** —— 它直接用 `miio_command` 发
  `5-4 小爱同学 #0`，绕过了 `execute_directive`。不登记的话，持续对话叠加追问
  窗口时「小爱同学」会被当成新提问，是同一个死循环的另一条路径

顺带修掉两处"错误信息被吞"：

- `ask_gpt` 的 `done_callback` 先判 `future.cancelled()` 再调 `exception()`，
  否则任务被 cancel 后（结尾正常会 cancel）回调里抛 `CancelledError`，变成一大段
  `Exception in callback` 噪音
- `run_forever` 单独捕获 `StopAsyncIteration`，给出「没有返回任何内容」并说明可能
  原因；其余异常改为打印 `type(e).__name__`，不再出现空报错

### 另一处需转义：rich 吃掉了日志标签

`xiaogpt.py` 顶部是 `from rich import print`，所以 `[record]` 会被当成样式标记
吃掉，日志里只剩 ` query=...`，看不出这行是哪来的。改成 `\[record]` 转义。

（`fallback.py` / `learn.py` 用的是内置 print，没有这个问题。）

### 真机实测发现：`query` 里**不含**唤醒词

采样与实测中拿到的 query 都是 `现在几点`、`解释一下什么是量子纠缠`、
`详细讲讲三国演义里赤壁之战的过程` —— **小米的对话接口把「小爱同学」剥掉了**。

这条有实际后果：上游那句 `query.startswith(WAKEUP_KEYWORD)` 的排除**在实践中
不会生效**，而追问窗口期内也没有任何办法区分"小爱同学，关灯"（设备指令）和
"那它的原理是什么"（追问）——两者到达时都只剩下后半句。也就是说**追问窗口期内
说「关灯」会被 AI 抢答，而不是真的关灯**。这与持续对话模式是同一个取舍，
使用前需要知晓。

### 逐点改动

| 文件 | 改动 |
| --- | --- |
| `xiaogpt/fallback.py` | **新增** —— 安全读取 + 匹配纯函数 + 长度闸门 |
| `xiaogpt/learn.py` | **新增** —— 学习缓存、LLM 分类、落盘 |
| `xiaogpt/xiaogpt.py` | `need_ask_gpt` 重写、崩溃修复、`-v` 记录 dump、接管 UX、追问窗口 |
| `xiaogpt/config.py` | 三个新字段 + 归一化 + 长度闸门 |
| `xiaogpt/bot/base_bot.py` | `ChatHistoryMixin.clear_history` |
| `.gitignore` | 排除 `learned_fallbacks.json` |
| `README.md` / `xiao_config.yaml.example` | 配置说明；**顺手修 README 第 169 行**（keyword 默认值写成 `["请"]`，代码实际是 `("帮我","请")`） |

### 已实测的兜底词干固化进型号 profile

2026-09-19 真机采到 3 套兜底模板后，把它们的辨识词干写进了
`DEVICE_PROFILES["L05C"].fallback_phrases`：

```
被你问住了 / 这可把我难住了 / 这个我暂时还回答不上
```

取的是每套模板**开头**的辨识部分，不是共用的尾巴——「看来要更努力学习了」同时
出现在两套模板里，但它超出了 `PREFIX_WINDOW`（12 字）的范围，作为词干匹配不到。

**这样做是为了不依赖学习模式与分类器**：删掉 `learned_fallbacks.json` 也不会退化，
已知模板零 API 调用即刻生效，学习模式退化成"只处理没见过的新模板"的安全网。

用户配置与型号词干是**合并**关系而不是二选一（在 `Config.__post_init__` 里合到
同一个字段，调用方读到的就是最终生效的那份，不会漏合）。

### 兼容性声明

**L05C 上兜底接管现在默认开启**——即使 `fallback_answer_keyword` 留空，型号 profile
也会提供 3 个已知词干。这是本次唯一一处"默认行为与上游不同"的地方（`tts: mi` 的
等待方式那处见上一节）。要回到上游行为，把 `hardware` 改成别的型号即可。

对**未登记型号**（`DEVICE_PROFILES` 里没有的），`fallback_answer_keyword: []` +
`learn_fallback: false` + `follow_up_seconds: 0` 时兜底判定恒为 False、追问窗口永不
开启，与上游一致。

开启追问窗口后有一处**可见的行为变化**：窗口过期会清空 history，于是 `prompt`
从"每进程拼一次"变成"每会话拼一次"（`has_history()` 为假时才拼）。这更符合直觉，
但与上游不同，特此声明。

### 与上游同步的冲突面

`fallback.py` / `learn.py` 是新增文件，冲突面恒为 0；`bot/base_bot.py` 是干净追加。
风险最高的是 `xiaogpt.py` 主循环中段（`need_ask_gpt`、剥触发词那行、`mute_xiaoai`
分支）与 `config.py` 的新字段区。

### 已验证 / 未验证

**已实测**：

- 匹配器离线校准：6 条用例全对，含关键负样本「从前有个机器人，它说：抱歉，我还没
  学会走路」→ 正确**不**接管
- 安全读取：`answers` 为 `None` / `[]` / `[{"tts": None}]` / 非列表 / record 为 None
  —— 全部返回空串，不再抛异常
- `need_ask_gpt` 与上游 **90 组组合逐值等价**
- 剥触发词：`HEY` 大小写、`*` / `(` / `[a` 等正则元字符（上游抛 `re.error`）
- 学习机制 8 项：分类、缓存命中、标点变体命中、长回答短路且不落盘、负样本缓存、
  重载保留、分类器答非所问时不接管、文件损坏不阻断启动
- 配置：默认值、裸字符串归一化、单字短语被拒绝
- 采样拿到了真实的兜底话术，并确认 `answers` 随记录立即可见

**真机端到端已跑通**（2026-09-19，L05C，`learn_fallback: true` + `follow_up_seconds: 25`）：

- **死循环修复后不再复现**：整轮只有一条 `停止播放` 回声，被正确过滤，之后安静
- **负样本一个都没被抢**：`现在几点`（小爱答「现在是凌晨2点41分」）、
  `解释一下什么是量子纠缠`（小爱**真的答上来了**，且是长回答）——两条都未被接管
- **兜底接管成功**：`详细讲讲三国演义里赤壁之战的过程` → 小爱答「被你问住了，
  看来要更努力学习了」→ 判定为兜底 → DeepSeek 接管 → 音频正常播放并只播一遍
- **追问窗口如期开启**
- **学习机制实测有效**：小爱至少有 **3 套**互不相同的兜底模板
  （`这个我暂时还回答不上诶…` / `这可把我难住了…` / `被你问住了…`），
  这印证了靠手工枚举追不上、必须让它自己学
- 长回答（25 秒音频）播放正常，没有出现首轮那种「回答出错」

**未验证（点名）**：

- **本 fork 只面向 L05C**（用户只有这一台），其它型号既没采样也没回归测试。
  未登记型号的代码路径按设计走默认值、与上游一致，但只做过代码审查与默认值检查，
  没有真机验证——这是有意识的范围取舍，不是待办缺口
- 固化的 3 个词干覆盖不了小爱将来新增的兜底模板，这正是学习模式仍然需要的原因
- **`停止播放` 能否打断小爱自己的 TTS 朗读**仍未验证。L05C 上
  `stop_if_xiaoai_is_playing` 只会走到这条指令，而它的实测对象一直是 **URL 播放的
  音频**。若打不断，接管时"道歉被切掉"的缓解会落空
- **追问窗口期内无法区分设备指令与追问**（见上：`query` 不含唤醒词）。
  「关灯」会被 AI 抢答而不是真的关灯。这是**已知且已接受的取舍**（决定保持
  默认 25 秒）：窗口只有 25 秒、说完就走，且平时说设备指令大多会带「小爱同学」，
  而那句话在录音开始前就已把音箱唤醒，窗口此时多半已过期
- 分类器的判定准确率没有统计样本，只有 4 条用例
- `讲个笑话` / `讲个故事` 这类会触发小爱**内置音频源**的请求不适合当负样本
  （她走内容播放而非语音回答），这是用户实测反馈，未专门处理

---

## 2026-09-18 — L05C 第三方 TTS：修复播放与循环

**结论先行**：README 里「L05C 只支持小爱原本的 tts」是错的，`tts: edge` 可用。
但要让它在 L05C 上真正跑通，需要修两个 bug —— 其中**一个是所有走第三方 TTS
的设备都踩得到的**。

### 根因一（与型号无关）：HTTP 服务不支持 Range

音箱用 ffmpeg（请求头 `User-Agent: Lavf/...`）来取音频，一次播放会发三个请求：

```
1. Range: bytes=0-             探测
2. Range: bytes=<接近文件尾>-   跳到末尾读 MP3 的标签帧
3. Range: bytes=0-             真正开始播
```

而 stdlib 的 `SimpleHTTPRequestHandler` **完全不支持 Range** —— 源码里连
`Range` / `206` / `Content-Range` 这几个字符串都搜不到，对上面三类请求一律回
`200` + 整个文件。ffmpeg 因此完成不了探测，会以每秒十几次的频率疯狂重试，而
音箱**始终不进入播放态**。

这个 bug 的迷惑性在于：服务端日志里全是正常的 `200`，播放态却一直是"未播放"，
音箱一声不吭。排查时很容易误判成"这个型号不支持第三方 TTS" —— 上游 README
那句话很可能就是这么来的。

**修复**：新增 `xiaogpt/tts/http.py`，提供 `RangeRequestHandler`（206 + 完整的
`Content-Range` / `Accept-Ranges` / 416 处理）。补上之后播放态立刻从 2 变 1。

### 根因二（L05C 特有）：播完不停重播，且播放态不可信

L05C 属于 miservice 的 `_USE_PLAY_MUSIC_API` 名单，`play_by_url` 走的是 ubus
`player_play_music`。实测该路径在 L05C 上：

| # | 现象 | 证据 |
| --- | --- | --- |
| 1 | **播完立刻重播，几乎不留空隙** | 3.96 秒的 mp3 每 4.0 秒起一轮；5.5 秒的每 5.4~5.7 秒一轮 |
| 2 | **播放态播完不回落** | 重播期间 `player_get_status` 恒为 1，`wait_for_duration` 的轮询会死循环挂死 |
| 3 | **ubus 的停止全部无效** | `player_pause` / `player_stop` / miio `3-3` / miio `3-4` 都停不住 |
| 4 | **唯一有效的是 MIoT 文本指令** | `5-4 停止播放 #0` 能真正停下 |
| 5 | `3-3`（播放循环模式）是空壳 | 写入回读恒为 `[None]`，四档取值行为一致，关不掉循环 |

### 方案：型号差异集中到 `xiaogpt/device.py`

表里**只登记与默认行为不同的型号**，未登记的型号拿到 `DEFAULT_PROFILE`，
行为与上游一致。`DeviceProfile` 两个字段：

- `status_poll: bool` —— `player_get_status` 的播放态是否可信
- `directive_command: str | None` —— 执行文本指令的 MIoT 动作（L05C 是 `"5-4"`）

`directive_command` 与既有的 `wakeup_command` **刻意分成两个字段**，即使 L05C 上
两者都是 `"5-4"`：语义不同（唤醒 vs 执行指令），入参个数也不同（`5-3` Play Text
收 1 个文本参数，`5-4` 收 文本 + 是否静默 两个）。

### 停止时机：必须**提前**发，而不是等满

这是本次最反直觉的一处。原本的想法是"等音频播完再停"，实测**不成立**：播完即
重播，等满了第二遍的开头已经出来了。反过来，在**播放中途**发停止指令能取消排队
中的下一轮。

于是改成提前 `STOP_EARLY_SECONDS = 0.4` 秒发指令。之所以听不出来，是因为
edge-tts 生成的 mp3 结尾自带约 0.5 秒静音（实测最后 11 个 50ms 窗格峰值全为 0），
切掉的只是静音。**换成别的 TTS 后端时这个前提可能不成立**，届时需要重新校准。

等待基准取"第一波取流的最后一个请求"（`BURST_QUIET_SECONDS = 0.25` 判定这一波
结束），而不是 `play_by_url` 返回的时刻——后者到真正出声之间有几秒缓冲，直接按时长
等会在还没出声时就发停止指令（实测停止指令会落空）。

### 逐点改动

| 文件 | 改动 |
| --- | --- |
| `xiaogpt/device.py` | **新增** —— 型号差异的唯一集中点 |
| `xiaogpt/tts/http.py` | **新增** —— `RangeRequestHandler`（根因一的修复） |
| `xiaogpt/tts/base.py` | `wait_for_duration` 在 `status_poll=False` 时提前返回；`__init__` 建 `miio_service`；新增 `stop_playback()` |
| `xiaogpt/tts/file.py` | 改用 Range 处理器；按取流起点 + 提前量计时；`try/finally` 里显式停止 |
| `xiaogpt/tts/live.py` | 按 profile 分支，不再死循环 |
| `xiaogpt/tts/mi.py` | 删掉与基类重复的 `miio_service` 赋值 |
| `xiaogpt/xiaogpt.py` | `stop_if_xiaoai_is_playing` 改走指令；新增 `send_directive`；删 `wait_for_tts_finish`；修 `wakeup_xiaoai` 入参 |
| `xiaogpt/config.py` | 只加一个 `cached_property device_profile` |
| `README.md` | 第 185 行的过时说法 |

**没有新增任何用户可见配置项** —— profile 由既有的 `hardware` 驱动，所以 `cli.py` /
`xiao_config.yaml.example` / README 配置表都不用动。

### 其他

- **`xiaogpt/bot/deepseek_bot.py` 重新格式化了**（纯换行，无语义改动）。它是上一个
  提交引入的格式问题，而 CI 用的是**不锁版本**的 `pip install black`，所以在那之前
  `black --check .` 是失败的。顺手带上，让 CI 恢复绿色。

### 修正 `wakeup_xiaoai` 的入参写法

原本拼的是 `f"{wakeup_command} {WAKEUP_KEYWORD} 0"`，末尾是**字符串** `"0"`。
而 miservice 的 `string_or_value()` **只对 `#` 前缀的参数做类型转换**，所以要让
设备收到整数 0，必须写 `#0`。实测能生效的形式是 `5-4 停止播放 #0`；` 0` 这个
写法是否同样被设备接受没有单独验证，但既然 `#0` 是已验证可用的形式，就统一用它。

### 兼容性声明（有意为之）

- **`tts: mi` 在 L05C 上行为会变**：从"播放态轮询可能死循环挂死"变成"按
  `calculate_tts_elapse` 的 4.5 字/秒估算等待"。`status_poll` 是设备属性而非后端
  属性，因为"播放态卡在 1"是关于设备的事实，与用哪个 TTS 后端无关。
- **其它型号行为不变**：两个字段的默认值分别对应"走上游那段轮询"和"走上游那个
  `player_pause`"，分支写法都是先判 profile 再提前 return，默认路径的语句一字未改。

### 已知重复，刻意不合并

`get_if_xiaoai_is_playing`（`xiaogpt.py` ↔ `tts/base.py`）与 `do_tts` ↔ `MiTTS.say`
各是一对逐行相同的实现。它们是**上游**的重复，本次修复也不需要碰，合并会同时改动
两个上游文件、白白扩大与上游的冲突面。记在这里是为了说明是**刻意**不合并，而不是
没看见。

### 与上游同步：本次的冲突面

**特别注意：本次动了 `xiaogpt/tts/`，而此前该目录与上游逐字节相同**
（`git diff 3de0af6 HEAD -- xiaogpt/tts/` 输出为空）。这次之后它不再能干净合并，
`wait_for_duration` 与 `TetosFileTTS.synthesize` 是冲突风险最高的两处。

`xiaogpt/device.py` 与 `xiaogpt/tts/http.py` 是新增文件，冲突面恒为 0。

### 已验证 / 未验证

**已实测**（2026-09-18，小爱音箱 Play 增强版 L05C，mi_did=502970287）：

- Range 修复后播放态从 2 变 1，声音正常，用户确认听感完整
- 提前 0.4 秒停止：三次独立运行均为**单轮取流、停止后 20 秒内 0 次取流、无重播**
- `execute_directive` 拼出的命令串与手工验证过的形式逐字符一致（`5-4 停止播放 #0`）
- 停止指令在播放中途发出有效；`5-4 现在几点 #0` 能让音箱真的报时
- `device_profile` 默认值：`L05B` / `LX06` / 未登记型号均返回
  `DeviceProfile(status_poll=True, directive_command=None)`
- `Config.__repr__` 的凭据打码未受影响

**未验证**：

- **其它型号的行为不变**只做了代码审查 + 默认值检查，没有真机回归（手上只有 L05C）
- 另外 7 个 `_USE_PLAY_MUSIC_API` 型号（LX04/LX05/L05B/L06/L06A/X08A/X10A）走同
  一条 `player_play_music` 路径，很可能有同样的重播问题，但**未实测**，因此没有
  预先登记进 `DEVICE_PROFILES`
- `tts: fish`（流式）在 L05C 上只做到"不再死循环"，`LIVE_TAIL_SECONDS` 是估的
- `STOP_EARLY_SECONDS = 0.4` 依赖"TTS 输出结尾自带静音"，只对 edge-tts 实测过
- 多句连播的句间衔接（`stream: true` 下回答常被切句）未端到端验证
- Ctrl-C 中断时停止指令能否发出（`finally` 里 await 不保证跑完）

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
- **`deepseek-flash` 模型可用**：用 bot 真实代码路径（含流式与非流式）
  调用成功，中英文回答均正常
- **`deepseek-flash` 支持思考模式**：`thinking: {"type": "enabled"}` 返回
  HTTP 200，响应含真实的 `reasoning_content` 字段
- **启动校验**：错误模型名 / 无效 key 均以退出码 1 中止并给出可操作提示；
  网络不通时 warn 后跳过。经本地 7890 代理与直连两种方式验证

**未验证**：

- 真实语音链路（唤醒 → DeepSeek → TTS 回放）尚未端到端跑通
- 修复后的 `chatgptapi_bot` 代理路径只验证了 client 构造，无 OpenAI key
  无法真实调用

### DeepSeek 模型

模型通过配置项 **`deepseek_model`** 选择，留空则用内置默认值
`deepseek-flash`。查看当前可用模型：

```bash
curl -H "Authorization: Bearer $DEEPSEEK_API_KEY" https://api.deepseek.com/models
```

优先级（高 → 低）：

1. `gpt_options.model` —— 通用覆盖，对任何 bot 都生效
2. `deepseek_model` —— 本 bot 的专用配置项
3. 内置 `DEFAULT_MODEL = "deepseek-flash"`

配置示例：

```yaml
deepseek_api_key: "sk-..."
deepseek_model: ""          # 留空即 deepseek-flash
```

### 启动时校验模型名

`deepseek_bot` 的 `ask`/`ask_stream` 会把异常吞掉并返回空字符串，所以模型名
写错的表现是**音箱一声不吭**——终端有报错，但用户听到的只是沉默。加了
`deepseek_model` 配置项之后打错字的概率上升，于是在启动时拦一道。

`BaseBot` 新增 `validate()` 钩子（默认空实现），`run_forever` 在
`init_all_data()` 之后、开始轮询之前调用。`DeepseekBot` 的实现会拉取
`/models` 比对：

- 模型名不在列表 → `SystemExit`，退出码 1，并列出可用模型
- API key 被拒（401/403）→ 同样直接退出
- 网络不通（`ConnectError` 等）→ 打印 warn 后**跳过校验**，不阻断启动

报错信息刻意使用 ASCII 而非中文：本机控制台编码会把中文变成乱码
（启动横幅同样是乱码），而一个看不懂的报错等于没有报错。

### 修复 httpx 代理参数失效

`requirements.txt` 固定 `httpx[socks]==0.28.1`，而 httpx 0.28 已移除
`proxies=` 参数（现为 `proxy=`，单数）。原先三个位置都写的 `proxies=`：

- `deepseek_bot` 的 `ask` / `ask_stream`
- `chatgptapi_bot` 的 `ask` / `ask_stream`

只要配置里设了 `proxy`，`httpx.AsyncClient(**kwargs)` 就会抛
`TypeError: unexpected keyword argument 'proxies'`，而该调用位于 try 块
之外，因此**每个问题都会失败**。而且这个 TypeError 会被误判成「网络不通」，
静默掩盖真实原因。已全部改为 `proxy=`。

注意环境变量代理不受影响：`trust_env=True` 会读取 `HTTP_PROXY`/`HTTPS_PROXY`，
所以 `one_click.ps1` 里设的那两个变量一直是生效的，坏的只是配置文件里的
`proxy` 字段。

命名沿用了 `gemini_model` 的既有模式（同为顶层字段，bot 内用
`配置值 or 硬编码默认`）。`deepseek_api_key` 无法用 `gpt_options` 表达，
所以专用字段是有必要的。

先前代码里写死的 `deepseek-v4-flash` 在 API 上并不存在，是本 fork 自造的
名字，已删除。

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
