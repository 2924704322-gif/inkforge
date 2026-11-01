"""拒答探测：把"这段产出是不是在拒绝"收成**一处**判定。

为什么单独抽一个模块（2026-09-22）：
  原实现只长在 `src/agents/architect.py` 里，于是**只有自由文本链路**（`generate_faithful`）
  会判拒答；结构化 JSON 链路（`chat_structured`）解析失败时只说"解析失败"，分不清
  "模型拒答"与"JSON 写坏了"——而这两者的处置完全不同（改提示 vs 加预算/改格式）。
  真机实测就卡在这里：出卡 6 次调用全部 2~3 秒返回 110~242 字、`finish=stop`，
  日志只有"结构化输出解析失败"，无法定性。

  放到 `src/llm/` 而不是 `src/agents/`：`architect` 在**模块级**导入 `src.llm.structured`，
  若 `structured` 反过来导入 `architect` 就会成环。本模块不依赖项目内任何东西，两边都能用。

`architect` 保留了 `_looks_like_refusal` / `_refusal_signal` 两个私有别名，历史调用点
（测试、smoke 脚本）不用改。

口径（2026-09-22 定稿 · 完全对齐原版）：
  词表、长度阈值与 `looks_like_refusal` 的判定逻辑**逐字取自原版**（`inkforge-test` 的
  `architect.py` 常量区 + 函数体），不做任何加强或收紧：

    · **元自指**（作为AI / 语言模型 …）：出现即判拒答（不论长短）；
    · **短产出（≤400 字）**：命中任一拒答措辞即判拒答；
    · **长产出（>400 字）**：**直接放行**，不做任何位置扫描 —— 正文里角色说"抱歉"
      "我做不到"是常态，原版刻意用"只看长度、不看长文内容"来防这类误伤。

  本模块**只做判定与诊断**，不再引入"硬拒答/软信号/空产出/预算耗尽"之类的新档位，
  也不再引入"尾部窗口 / 剥引号 / 强弱词"之类的新判据 —— 那套加强在真机上两头都不对
  （该判的没判到：日志里 19 次判拒答全部落在短产出路径、尾部窗口一次没命中；
  不该判的判了：章节以对白收尾被整章判成拒答，白烧重试预算），故全部撤回。
"""

from __future__ import annotations

#: 元自指信号：出现即判拒答（不论长短）——真实拒答几乎总带这类自我定位语。
REFUSAL_META = (
    "作为AI", "作为人工智能", "作为一个AI", "AI助手", "语言模型", "人工智能助手",
    "as an ai", "i'm an ai", "i am an ai",
)
#: 拒答措辞（**只在短产出里才判**，见 looks_like_refusal）。
REFUSAL_PHRASES = (
    "抱歉", "对不起", "很抱歉", "我不能", "我无法", "无法生成", "无法提供", "无法满足",
    "无法协助", "做不到", "超出范围", "请换一个", "恕难", "请谅解",
    "i can't", "i cannot", "i'm sorry", "i won't",
)
#: 超过这个长度的产出**不因出现礼貌词判拒答**：正文里角色说"抱歉""我做不到"是常态。
REFUSAL_MAX_CHARS = 400


def looks_like_refusal(text: str) -> bool:
    """产出是否为"拒答"（X7 ①）。

    由来（2026-09-19 真机排查）：原实现是 `any(marker in text)`，词表只有
    （无法生成 / 做不到 / 超出范围 / 请换一个）且**任意位置命中即判拒答**，两头都出问题：
    · **漏判**（更严重）：真实拒答多写成「抱歉，我不能创作这类内容」——四个词一个都不含，
      于是拒答文本被**当成正文落盘**，接着被 Editor 打 0 分、判 full_rewrite，
      用户看到的就是"生成不出来 / 一直被打回"；
    · **误判**：正文里角色说一句「我做不到」（或旁白出现"超出范围"）→ 整章被判拒答 →
      重试 4 次全废 → 抛 BriefFidelityError。

    现口径（两头都收）：
    1. **元自指**（作为AI / 语言模型 …）→ 无论长短一律判拒答；
    2. 其余拒答措辞**只在短产出（≤400 字）里判定** —— 正常章节远超此长度，
       而拒答通常是一小段说明；长产出里出现"抱歉/做不到"不再误伤。

    2026-09-22 定稿：本函数与上面的词表/阈值**完全对齐原版实现**（逐字一致），
    不再叠加"尾部窗口 + 剥引号 + 强弱词"等加强口径 —— 那套加强是真机上误判/漏判的来源，
    已连同上层的分档重试一起撤回。
    """
    s = (text or "").strip()
    if not s:
        return False
    low = s.lower()
    if any(m.lower() in low for m in REFUSAL_META):
        return True
    if len(s) > REFUSAL_MAX_CHARS:
        return False
    return any(m.lower() in low for m in REFUSAL_PHRASES)


def refusal_signal(text: str) -> str:
    """回报"拒答判定命中了哪条规则 + 哪个关键词"（**不含产出正文**，仅供日志定位）。

    由来（2026-09-22 真机排障）：日志里只有"产出含拒绝语义"这句话，无法判断是
    模型真拒答、还是词表误伤（例如正文里角色说了句"抱歉"）。命中的关键词本身是
    通用措辞（"抱歉""做不到"…），不是作品内容，所以可以安全写进日志。

    结构化输出链路复用它（`src/llm/structured.py`）：解析失败时附上这条信号，
    才能区分"模型拒答"与"JSON 写坏了"。

    口径随 `looks_like_refusal` 一起对齐原版：只报**元自指 / 短产出**两类命中。
    """
    s = (text or "").strip()
    if not s:
        return "空产出"
    low = s.lower()
    for m in REFUSAL_META:
        if m.lower() in low:
            return f"meta:{m}"
    if len(s) > REFUSAL_MAX_CHARS:
        return "未命中"
    for p in REFUSAL_PHRASES:
        if p.lower() in low:
            return f"短产出:{p}｜{len(s)} 字"
    return "未命中"
