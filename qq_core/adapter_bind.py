"""适配器绑定：按名字取**当前有效**的适配器实例。

为什么需要这个模块（2026-11-25 实机）：
    KiraAI 的 `AdapterManager` 在 WebUI 里"重启适配器 / 改配置"时，会先
    `stop_adapter()`（把旧实例从 `_adapters` 移除）再 `register_adapter()`
    创建**全新实例**。插件如果在 initialize() 时把实例缓存下来，之后拿到的
    就是"已关闭的僵尸"：`bot.websocket is None`，任何 send_action 都会抛
    `ConnectionError: NapCat WebSocket 未连接，无法执行 xxx`——而实际连接正常。

规则：
    1. **实时优先**：先向 `ctx.adapter_mgr.get_adapter(name)` 要当前实例；
    2. 拿到就顺手刷新缓存（其它同步路径也能读到活实例）；
    3. 实时取不到 → 用缓存；
    4. 缓存也没有 → 用默认实例（无会话上下文时的兜底）。

纯逻辑、无框架依赖，便于单测。
"""

from __future__ import annotations


def get_live_adapter(ctx, name: str):
    """从框架实时注册表取适配器实例。

    Args:
        ctx: PluginContext（读 adapter_mgr）。
        name: 适配器名。

    Returns:
        适配器实例；取不到/异常时返回 None。
    """
    name = str(name or "")
    if not name or ctx is None:
        return None
    mgr = getattr(ctx, "adapter_mgr", None)
    getter = getattr(mgr, "get_adapter", None) if mgr is not None else None
    if not callable(getter):
        return None
    try:
        return getter(name)
    except Exception:
        return None


def resolve_adapter(
    ctx,
    name: str,
    cached: dict,
    default_adapter=None,
    default_self_id: str = "",
    self_id_of=None,
):
    """按名字解析 (adapter, self_id)：**实时实例优先**。

    Args:
        ctx: PluginContext。
        name: 适配器名（sid 第一段 / event.adapter.name）。
        cached: {name: (adapter, self_id)}，**会被就地刷新**。
        default_adapter: 兜底实例。
        default_self_id: 兜底 self_id。
        self_id_of: callable(adapter) -> str，从实例提取 self_id。

    Returns:
        (adapter, self_id)。
    """
    name = str(name or "")
    live = get_live_adapter(ctx, name)
    if live is not None:
        cached_hit = (cached or {}).get(name) if isinstance(cached, dict) else None
        self_id = ""
        if callable(self_id_of):
            try:
                self_id = str(self_id_of(live) or "")
            except Exception:
                self_id = ""
        if not self_id and cached_hit:
            self_id = str(cached_hit[1] or "")
        try:
            if isinstance(cached, dict):
                cached[name] = (live, self_id)
        except Exception:
            pass
        return live, self_id

    hit = (cached or {}).get(name) if isinstance(cached, dict) else None
    if hit:
        return hit
    return default_adapter, default_self_id
