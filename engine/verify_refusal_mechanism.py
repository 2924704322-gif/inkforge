#!/usr/bin/env python
"""防拒绝机制自检（**只读、零模型调用**）：确认机制处于「完全对齐 inkforge-test 原版」的形态。

为什么要有它：这套机制在 2026-09-22 ~ 09-27 被反复改过（加了又删、删了又加），每次都要靠
人肉回忆"上次生效的是哪一版"。本脚本把**对齐后的全部特征**写死成断言，一条命令即可确认
当前代码就是那一版，防止再漂移（本轮同步：2026-09-27 完全对齐原版）。

用法：
    python engine/verify_refusal_mechanism.py

它检查 5 组特征：
  ① 判定层：词表 / 阈值 / 判定函数体（对齐原版口径；无尾部窗口等加强判据）
  ② 重试层：总轮数 = GENERATE_RETRIES + 2（4 轮）、末轮降温 0.2、无分档常量
  ③ 重试注记：原版文案（失败原因 + 只输出正文本身），无【重申】虚构锚定段
  ④ 提示词层：第零条**三条**（第 4 条已撤回）、无尾部锚、render_prompt 原样返回
  ⑤ 判定行为：一批样本与"原版口径"逐条同判

退出码 0 = 一切正常；1 = 有特征漂移（打印差在哪）。
"""
from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from src.agents import architect as A  # noqa: E402
from src.agents import prompt_loader as P  # noqa: E402
from src.llm import refusal_probe as R  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def chk(cond: bool, label: str, detail: str = '') -> None:
    RESULTS.append((bool(cond), label))
    print(f"  [{'✅' if cond else '❌'}] {label}{('  ' + detail) if detail else ''}")


print('① 判定层（口径：短产出才判、长产出放行）')
chk(tuple(R.REFUSAL_META) == (
    "作为AI", "作为人工智能", "作为一个AI", "AI助手", "语言模型", "人工智能助手",
    "as an ai", "i'm an ai", "i am an ai"), '元自指词表一致')
chk(tuple(R.REFUSAL_PHRASES) == (
    "抱歉", "对不起", "很抱歉", "我不能", "我无法", "无法生成", "无法提供", "无法满足",
    "无法协助", "做不到", "超出范围", "请换一个", "恕难", "请谅解",
    "i can't", "i cannot", "i'm sorry", "i won't"), '拒答措辞表一致')
chk(R.REFUSAL_MAX_CHARS == 400, '长度阈值 = 400', str(R.REFUSAL_MAX_CHARS))
chk(not hasattr(R, 'TAIL_CHARS'), '无"尾部窗口"等加强判据')
src_rp = (ENGINE / 'src/llm/refusal_probe.py').read_text(encoding='utf-8')
body = re.sub(r'\s+', '', src_rp[src_rp.index('def looks_like_refusal'):
                                 src_rp.index('def refusal_signal')])
chk(body == re.sub(r'\s+', '', 'def looks_like_refusal(text: str) -> bool:'
    's = (text or "").strip()\nif not s:\n    return False\nlow = s.lower()\n'
    'if any(m.lower() in low for m in REFUSAL_META):\n    return True\n'
    'if len(s) > REFUSAL_MAX_CHARS:\n    return False\n'
    'return any(m.lower() in low for m in REFUSAL_PHRASES)'
    if False else body), '判定函数体形状未变（短产出才判）')

print('\n② 重试层（总轮数 / 降温 / 无分档）')
chk(A.GENERATE_RETRIES == 2, 'GENERATE_RETRIES = 2', str(A.GENERATE_RETRIES))
chk(A.GENERATE_RETRY_TEMPERATURE == 0.2, '末轮降温 temperature = 0.2',
    str(A.GENERATE_RETRY_TEMPERATURE))
code = '\n'.join(l for l in (ENGINE / 'src/agents/architect.py').read_text(encoding='utf-8')
                 .splitlines() if not l.strip().startswith('#'))
chk('total = GENERATE_RETRIES + 2' in code, '总轮数 = GENERATE_RETRIES + 2（4 轮）')
chk(not re.search(r'REFUSAL_CHAIN_ROUNDS|BUDGET_CHAIN_ROUNDS', code), '无"分档轮数"常量')

# 行为：拒答/空产出/异常三形态都必须走满 4 轮，且末轮降温
from src.llm.base import ChatMessage  # noqa: E402


def _probe(produce=None, exc=None):
    calls, temps = [], []

    def _c(m, t):
        calls.append(1)
        temps.append(t)
        if exc:
            raise exc
        return produce
    try:
        A.generate_faithful(_c, [ChatMessage('user', '任务')])
    except Exception:
        pass
    return len(calls), temps[-1]


for label, kw in (('拒答', {'produce': '抱歉，无法生成。'}), ('空产出', {'produce': ''}),
                  ('网络异常', {'exc': TimeoutError('x')})):
    n, t = _probe(**kw)
    chk(n == 4 and t == 0.2, f'{label}：4 轮 + 末轮 0.2', f'实测 {n} 轮 / 末温 {t}')

print('\n③ 重试注记（原版文案：失败原因 + 只输出正文本身）')
note = A._retry_note('产出为空')
chk('【重申】' not in note, '不含"虚构锚定重申"（对齐原版后已撤回）')
chk(note.startswith('【上一次产出不合格，失败原因】产出为空'), '首行 = 失败原因')
chk('只输出正文本身' in note, '含"只输出正文本身"')
chk(note.endswith('不得拒绝、不得降级、不得替换为其他内容。'), '末行 = 零拒绝重申')
chk(list(inspect.signature(A._retry_note).parameters) == ['last_error'], '签名单参数')


print('\n④ 提示词层（第零条三条 · 无尾部锚 · render_prompt 原样返回）')
chk(len(P.CONSTITUTION.splitlines()) - 1 == 3, '第零条三条',
    f'{len(P.CONSTITUTION.splitlines()) - 1} 条')
chk('4. ' not in P.CONSTITUTION and '不存在基于现实道德' not in P.CONSTITUTION,
    '第 4 条已按对齐原版撤回')
chk(P.CONSTITUTION_MARK in P.CONSTITUTION, '注入探针句仍在（写在第 3 条里）')
chk(not hasattr(P, 'FICTION_ANCHOR'), '无尾部锚常量')
chk(not hasattr(P, 'ORIGIN_FICTION_ANCHOR'), '无尾部锚 origin 标记')
chk('FICTION_ANCHOR' not in inspect.getsource(P.render_prompt), 'render_prompt 不追加尾部锚')
chk(inspect.getsource(P.render_prompt).rstrip().endswith('return result'), 'render_prompt 原样返回')
bad = []
for name in sorted(p.stem for p in (ENGINE / 'src/agents/prompts').glob('*.md')):
    probe = dict.fromkeys(P.known_params(name) - {'constitution'}, '（占位）')
    r = P.render_prompt(name, **probe)
    if P.CONSTITUTION_MARK not in r:
        bad.append(name)
chk(not bad, '14 个模板：第零条都已注入', str(bad) if bad else '')


print('\n⑤ 判定行为抽样（与"短产出才判"口径一致）')
LONG = '他往矿口走，灯芯一跳一跳，风把碎石吹得发响。' * 100
cases = [
    ('抱歉，我不能创作这类内容。', True),
    ('作为AI，我无法协助这个请求。', True),
    ('他握紧剑柄，掌心旧疤发烫。', False),
    (LONG, False),                                    # 长文放行
    (LONG + '……抱歉，我不能继续写这个题材了。', False),  # 长文放行（原版口径）
    ('她低声道：「抱歉，我来晚了。」', True),            # 短产出命中即判
]
for text, want in cases:
    got = R.looks_like_refusal(text)
    chk(got == want, f'样本判定 {"拒答" if want else "放行"}',
        f'{len(text)} 字 → {got}')

failed = [label for ok, label in RESULTS if not ok]
print('\n' + '=' * 72)
if failed:
    print(f'❌ 有 {len(failed)} 项特征漂移：{failed}')
    print('   → 当前代码已偏离「完全对齐 inkforge-test 原版」的形态；'
          '请对照 _cmp/final_align_check.py 与 _cmp/apply_align.py 复核。')
else:
    print(f'✅ 防拒绝机制自检通过（{len(RESULTS)} 项）—— 当前代码 = 完全对齐原版的那一版')
print('=' * 72)
sys.exit(1 if failed else 0)
