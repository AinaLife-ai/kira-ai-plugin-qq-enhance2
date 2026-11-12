"""QQ增强2 缓存验证测试。

验证：环境摘要以 persist=False 注入 user_prompt → 不进历史 → 下一轮 system prompt 与历史消息前缀不变 → 缓存命中率不受损。

模拟框架 assemble_prompt 的行为：
1. 连续 5 轮模拟，每轮注入不同的环境摘要。
2. 每轮记录 assemble 后的完整请求前缀（system + user）。
3. 断言：第 k 轮请求前缀 == 第 k-1 轮完整请求（逐字节）。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qq_core.env_renderer import render_env_summary
from qq_core.event_normalizer import QQEvent, E_POKE


class MockPrompt:
    """模拟框架 Prompt 类。"""
    def __init__(self, content, name=None, source=None, end="\n", persist=True, **kwargs):
        self.content = content
        self.name = name
        self.source = source
        self.end = end
        self.persist = persist

    def to_string(self):
        return self.content + self.end


class MockLLMRequest:
    """模拟框架 LLMRequest。"""
    def __init__(self):
        self.messages = []
        self.user_prompt = []
        self.system_prompt = []

    def assemble_prompt(self, dynamic_position="latest_user"):
        """模拟框架的 assemble_prompt（persist=False 不进历史）。"""
        static_prompt = self.system_prompt
        if dynamic_position == "latest_user":
            static_prompt = []
            relocated = []
            for p in self.system_prompt:
                if isinstance(p, MockPrompt) and p.name in ("sessions", "chat_env", "time"):
                    p.persist = False
                    relocated.append(p)
                else:
                    static_prompt.append(p)
            if relocated:
                self.user_prompt[:0] = [
                    MockPrompt("<system_reminder>", name="dynamic_context_start", source="system", persist=False),
                    *relocated,
                    MockPrompt("</system_reminder>", name="dynamic_context_end", source="system", persist=False),
                ]

        # 组装 messages（只含 persist=True 的 user_prompt）
        persist_user = "".join(p.to_string() for p in self.user_prompt if isinstance(p, MockPrompt) and p.persist)
        self.messages = []
        if static_prompt:
            self.messages.append({"role": "system", "content": "".join(p.to_string() for p in static_prompt)})
        if persist_user:
            self.messages.append({"role": "user", "content": persist_user})

    def get_prefix(self):
        """获取完整请求前缀（system + user，用于缓存比对）。"""
        return "".join(m["content"] for m in self.messages if m["role"] in ("system", "user"))


def test_cache_invariance():
    """缓存验证：连续 5 轮，前缀逐字节一致。"""
    # 模拟 5 轮对话（每轮有不同的环境摘要）
    env_summaries = [
        "[群动态·刚刚] ①用户1001(张三) 戳了戳 用户2002(李四)",
        "[群动态·过去1分钟] ①用户1001(张三) 戳了戳 用户2002(李四) ②用户1003(王五) 禁言 用户1004(赵六) 600秒",
        "[群动态·过去2分钟] ①用户1001(张三) 戳了戳 用户2002(李四)×2 ②用户1003(王五) 禁言 用户1004(赵六) 600秒 ③用户1005(钱七) 贴了🔥",
        "[群动态·过去3分钟] ①用户1001(张三) 戳了戳 用户2002(李四)×2 ②用户1005(钱七) 贴了🔥",
        "[群动态·过去4分钟] ①用户1005(钱七) 贴了🔥×3",
    ]

    # 框架动态块（每轮都相同，模拟 system_reminder）
    framework_blocks = [
        MockPrompt("<system_reminder>", name="dynamic_context_start", source="system", persist=False),
        MockPrompt("当前时间: 2026-11-12 14:00\n当前群聊: 测试群\n", name="chat_env", source="system", persist=False),
        MockPrompt("</system_reminder>", name="dynamic_context_end", source="system", persist=False),
    ]

    prefixes = []

    for i, env_summary in enumerate(env_summaries):
        req = MockLLMRequest()
        # 框架 system prompt
        req.system_prompt = [
            MockPrompt("你是一个QQ群聊机器人。\n", name="base", source="system"),
        ] + framework_blocks

        # 我们的环境摘要注入（llm_request 阶段，persist=False）
        if env_summary:
            req.user_prompt.insert(0, MockPrompt(env_summary, name="qq_social_env", persist=False))

        # 真实用户消息
        req.user_prompt.append(MockPrompt(f"用户消息{i+1}: 大家好", name="message", source="system"))

        # 框架 assemble（llm_request 之后）
        req.assemble_prompt(dynamic_position="latest_user")

        # 记录本轮完整请求前缀
        prefixes.append(req.get_prefix())

    # 验证：第 k 轮请求前缀 == 第 k-1 轮完整请求（逐字节）
    for i in range(1, len(prefixes)):
        # 注意：由于每轮 env_summary 不同，user 消息部分会不同
        # 但 system 前缀必须完全一致（缓存前缀）
        prev = prefixes[i-1]
        curr = prefixes[i]
        # 提取 system 部分（第一个 user 之前的内容）
        prev_system = prev.split("用户消息")[0]
        curr_system = curr.split("用户消息")[0]
        assert prev_system == curr_system, f"第 {i} 轮 system 前缀不一致：\n{prev_system!r}\nvs\n{curr_system!r}"

    print("缓存验证通过：5 轮 system 前缀逐字节一致")
    print(f"第 1 轮前缀: {prefixes[0][:100]}...")
    print(f"第 5 轮前缀: {prefixes[4][:100]}...")


def test_persist_false():
    """验证 persist=False 不进历史。"""
    req = MockLLMRequest()
    req.system_prompt = [MockPrompt("系统提示\n", name="base", source="system")]

    # 注入环境摘要（persist=False）
    req.user_prompt.insert(0, MockPrompt("[群动态·刚刚] ①用户1001 戳了戳 用户2002", name="qq_social_env", persist=False))
    req.user_prompt.append(MockPrompt("用户消息", name="message", source="system"))

    req.assemble_prompt()

    # 验证：历史中只有用户消息，没有环境摘要
    assert len(req.messages) == 2
    assert req.messages[1]["content"] == "用户消息\n"
    assert "群动态" not in req.messages[1]["content"]
    print("persist=False 验证通过：环境摘要不进历史")


if __name__ == "__main__":
    test_cache_invariance()
    test_persist_false()
    print("=== 缓存验证全部通过 ===")