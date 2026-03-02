"""
astrbot_plugin_memory
跨会话持久化记忆 + TODO + 备忘录插件

架构：
- on_llm_request 注入：soul / profile / history_index / memo / 行为引导
- FunctionTool（返回给 LLM）：read_memory_detail / read_todo
- @filter.llm_tool（写入，return str 回传 LLM）：其余所有写入操作

数据存储：/AstrBot/data/memory/
  templates/         - 全局模板目录（用户可自定义）
    history_content.md - 历史记录 content 格式模板
  {user_id}/
    soul.md          - Soul 设定（注入）
    profile.md       - 用户画像（注入）
    history_index.md - 历史对话索引（注入）
    history/         - 历史对话详情（按需 FunctionTool 读取）
    memo.md          - 跨会话备忘录（注入）
    todo.md          - 会话级 TODO（Agent 自主管理，FunctionTool 读取）
"""

import os
import re
import datetime

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

# ── 数据目录 ──────────────────────────────────────────────
MEMORY_BASE = "/AstrBot/data/memory"
TEMPLATE_DIR = os.path.join(MEMORY_BASE, "templates")
HISTORY_TEMPLATE_PATH = os.path.join(TEMPLATE_DIR, "history_content.md")

# 沉淀记忆触发关键词
_SAVE_KEYWORDS = ["沉淀记忆", "更新记忆", "收录记忆", "记住这次对话", "把这个记下来"]

# ── 注入指令常量 ──────────────────────────────────────────

_NEW_USER_HINT = """

## 新用户引导
这是该用户第一次对话，尚未建立记忆档案。
请在处理用户当前需求的同时，以非侵入式的方式提示用户可以完善基础助理设定
（例如助理的名字、对用户的称呼、用户的习惯与偏好、所在位置等）。
如果用户有其他紧迫需求，优先处理，不要强制引导。
无论用户是否主动完善设定，在对话中要悄悄记住用户流露出的信息
（习惯、偏好、背景、口癖等），通过 update_profile 工具静默写入，不向用户声明。
"""

_MEMO_GUIDE = """

## 备忘录使用指南
以上备忘录内容为用户的跨会话待办事项，已内化为你的既有认知。
备忘录采用 block 结构，每个 block 有唯一 ID（分钟级时间戳，如 202602281310 表示 2026-02-28 13:10 创建）。
block 内部自由组织，可以是单条待办、一组相关任务、或含讨论细节的复合事项。
这些 block ID 仅用于工具调用定位，不属于向用户展示的信息。

输出规范：
- 向用户提及备忘事项时，只呈现内容本身，不输出 block ID 和时间戳
- 除非用户明确询问创建时间，才用自然语言说明（如"这条是昨天下午记的"）
- block 内有详细讨论内容时，根据上下文自然融入回答

触发场景：
- 用户问"我还有什么没做" / "接下来做什么" / "帮我看看计划"时，用自然语言组织回答
- 对话开始时有未完成事项，可简短提示一次
- 用户提到某件事"之后再做" / "有空再说"时，询问是否记入备忘录
- 用户一次提到多件待办时，使用 add_memo_block 批量记入，相关事项可归入同一 block
- 用户就某条备忘展开讨论时，将讨论结果通过 write_memo_block 补充到对应 block
- 需要整理合并备忘时：add_memo_block 新建合并后的 block，再 delete_memo_block 删除被合并的旧 block
用户明确完成某事后，考虑是否需要删除对应 block。
- 如果备忘录已有相似项，向用户确认是否需要记录
- 对于一次写入多个待做事项，优先按照block分类，以获得良好的组织结构

Operation rules:
- Use write_memo_block to update or partially remove content within a block — rewrite the block with the desired content.
- Use delete_memo_block ONLY when the entire block is obsolete and should be discarded completely.
- Never delete a block just to remove one item inside it; rewrite instead.
- When a user marks something as done, confirm whether the whole block is completed before deleting.
"""

_TODO_GUIDE = """

## TODO 自主管理
你拥有会话级 TODO 能力，用于自我规划复杂任务的执行进度。
注意，这仅用于辅助你自己记忆推理。因此当用户希望你帮忙记住某些待做事情时，不应该使用TODO，而应该使用备忘录。

触发条件：
> 必须满足
- 当前会话需要立即完成的任务
> 同时, 至少满足以下一项
- 用户一次提出多个问题或需求
- 任务涉及多个步骤或阶段
- 预计需要多轮对话才能完成

工作方式：
- 通过 read_todo 工具查看当前 TODO 状态
- 创建 TODO 后按顺序逐项处理，每完成一项调用 complete_todo 标记
- 主动告知用户当前进度和下一步计划
- 所有项目完成后调用 clear_todo 清空

格式约定（create_todo 的 items 参数，每行一个）：
[ ] 任务描述一
[ ] 任务描述二
"""


# ── 文件操作 ──────────────────────────────────────────────

def _user_dir(user_id: str) -> str:
    return os.path.join(MEMORY_BASE, user_id)

def _history_dir(user_id: str) -> str:
    return os.path.join(_user_dir(user_id), "history")

def _fpath(user_id: str, name: str) -> str:
    return os.path.join(_user_dir(user_id), name)

def _hpath(user_id: str, record_id: str) -> str:
    return os.path.join(_history_dir(user_id), f"{record_id}.md")

def _gen_id() -> str:
    """生成基于分钟级时间戳的基础 ID，格式如 202602281310"""
    return datetime.datetime.now().strftime("%Y%m%d%H%M")

def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

def _read(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def _write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def _append(path: str, content: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)

def _memo_block_pattern(block_id: str) -> re.Pattern:
    """生成匹配指定 block_id 的完整 block 正则"""
    return re.compile(
        r'<!-- block:' + re.escape(block_id) + r' -->[\s\S]*?<!-- /block -->'
    )

def _next_memo_block_seq(existing: str, base_ts: str) -> int:
    """提取同一分钟前缀下已有的最大后缀值，返回下一个可用序号"""
    matches = re.findall(
        r'<!-- block:' + re.escape(base_ts) + r'(?:_(\d{3}))? -->',
        existing
    )
    if not matches:
        return 0
    return max(int(s) if s else 0 for s in matches) + 1

def _is_new_user(user_id: str) -> bool:
    d = _user_dir(user_id)
    return not os.path.exists(d) or not os.listdir(d)

def _ensure_user(user_id: str):
    os.makedirs(_history_dir(user_id), exist_ok=True)
    _write_if_absent(_fpath(user_id, "soul.md"),
        "# Soul 设定\n\n（待完善：助理名称、对用户的称呼、人格风格、行为约束等）\n")
    _write_if_absent(_fpath(user_id, "profile.md"),
        "# 用户画像\n\n（待完善：用户背景、习惯、偏好、所在地等）\n")
    _write_if_absent(_fpath(user_id, "history_index.md"),
        "# 历史对话索引\n\n> 由插件自动维护，请勿手动编辑。\n\n")
    _write_if_absent(_fpath(user_id, "memo.md"), "# 备忘录\n\n")
    _write_if_absent(_fpath(user_id, "todo.md"), "# TODO\n\n")

def _write_if_absent(path: str, content: str):
    if not os.path.exists(path):
        _write(path, content)

# ── 模板管理 ──────────────────────────────────────────────

# 内置默认模板：仅约束 content 正文结构，不含 title/ID/Time 头部
_DEFAULT_HISTORY_TEMPLATE = """\
## 用户指令
> YYYY-MM-DD HH:MM
> <用户原始消息，保留原文>

<一句话概括用户意图>

## 助理回复
<助理回复内容的精炼摘要；若有工具调用，穿插在对应位置描述：工具名 | 参数=值 | 结果摘要>

---

（如需记录多个交互回合，重复以上 ## 用户指令 / ## 助理回复 结构）

规则（填写 content 时遵守，不要出现在最终内容中）：
- content 只包含正文，必须从 `## 用户指令` 开始，不包含标题、ID、时间头部
- 每个交互回合独立成对，保持时间顺序
- 用户指令保留原文，下方一行简短说明意图（不加引号或标签）
- 助理回复只摘要关键决策和结论，不逐字照抄
- 工具调用信息穿插在对应回合内，格式：工具名 | 参数=值 | 结果摘要
"""

def _ensure_global_templates():
    """若模板文件不存在则写入内置默认模板（首次启动初始化，不覆盖用户自定义）"""
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    _write_if_absent(HISTORY_TEMPLATE_PATH, _DEFAULT_HISTORY_TEMPLATE)

def _read_history_template() -> str:
    """读取用户自定义模板，读取失败则回退内置默认模板"""
    try:
        content = _read(HISTORY_TEMPLATE_PATH).strip()
        return content if content else _DEFAULT_HISTORY_TEMPLATE
    except Exception:
        return _DEFAULT_HISTORY_TEMPLATE

def _normalize_tags(tags) -> list:
    """清洗标签列表：去空白、去重、过滤非法值，最多保留 5 个"""
    if not isinstance(tags, list):
        return []
    seen, result = set(), []
    for t in tags:
        if not isinstance(t, str):
            continue
        v = t.strip()
        if v and v not in seen:
            seen.add(v)
            result.append(v)
            if len(result) >= 5:
                break
    return result

def _format_tags(tags: list) -> str:
    """将标签列表格式化为显示字符串，空列表返回空串"""
    return " / ".join(tags) if tags else ""

def _build_save_hint() -> str:
    """动态构建记忆沉淀指令（每次注入时从模板文件读取，支持用户自定义）"""
    template = _read_history_template()
    return f"""

## 记忆沉淀指令
用户希望沉淀本轮对话记忆。请先确认本轮对话是否还有需要调整的内容。
确认无误后，使用 create_memory 工具记录本轮对话。

create_memory 参数说明：
- title(string): 简洁的对话标题
- summary(string): 一句话结论，仅用于索引检索
- tags(array[string]): 分类标签，最多 5 个，例如 ["技术讨论", "用户画像"]（可为空数组）
- content(string): 按对话回合组织的正文，必须从"## 用户指令"开始，不包含 title/ID/Time 头部

content 格式模板（来自数据目录，用户可自定义）：

{template}
"""

def _build_inject_block(user_id: str) -> str:
    """构建注入 system_prompt 的完整块：记忆 + 备忘录 + 行为引导"""
    soul    = _read(_fpath(user_id, "soul.md")).strip()
    profile = _read(_fpath(user_id, "profile.md")).strip()
    index   = _read(_fpath(user_id, "history_index.md")).strip()
    memo    = _read(_fpath(user_id, "memo.md")).strip()

    parts = [
        "\n\n---\n# 用户记忆\n"
        "以下为该用户的持久化记忆，作为你对该用户的既有认知，"
        "自然体现在对话中，无需向用户声明你读取了记忆。"
    ]
    if soul:
        parts.append(f"\n## Soul 设定\n{soul}")
    if profile:
        parts.append(f"\n## 用户画像\n{profile}")
    if index:
        parts.append(f"\n## 历史索引\n{index}")

    # 备忘录：有实质内容才注入
    memo_body = memo.replace("# 备忘录", "").strip()
    if memo_body:
        parts.append(f"\n## 备忘录\n{memo_body}")
        parts.append(_MEMO_GUIDE)

    # TODO 行为引导（内容不注入，由 Agent 通过 read_todo 自主获取）
    parts.append(_TODO_GUIDE)

    return "\n".join(parts)


# ── FunctionTool：返回给 LLM 的只读查询工具 ────────────────

@dataclass
class ReadMemoryDetail(FunctionTool[AstrAgentContext]):
    name: str = "read_memory_detail"
    description: str = "读取某条历史对话的详细内容。当历史索引中有相关记录需要深入了解时调用。结果返回给你用于推理，不会直接发给用户。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "用户 ID，从当前会话上下文获取"
            },
            "record_id": {
                "type": "string",
                "description": "历史记录 ID，从历史索引中获取，格式为分钟级时间戳如 202507121430"
            }
        },
        "required": ["user_id", "record_id"]
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        user_id = kwargs.get("user_id", "")
        record_id = kwargs.get("record_id", "")
        detail = _read(_hpath(user_id, record_id))
        if not detail:
            return f"未找到记录 {record_id}"
        return detail


@dataclass
class ReadTodo(FunctionTool[AstrAgentContext]):
    name: str = "read_todo"
    description: str = "读取当前会话的 TODO 列表。用于检查任务进度，结果返回给你用于推理，不会直接发给用户。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "用户 ID，从当前会话上下文获取"
            }
        },
        "required": ["user_id"]
    })

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        user_id = kwargs.get("user_id", "")
        todo = _read(_fpath(user_id, "todo.md")).strip()
        if not todo or todo == "# TODO":
            return "当前无待办任务"
        return todo


# ── 插件主体 ──────────────────────────────────────────────

@register("astrbot_plugin_memory", "Bluezeamer", "跨会话持久化记忆插件", "1.2.0",
          "https://github.com/Bluezeamer/astrbot_plugin_memory")
class MemoryPlugin(Star):

    def __init__(self, context: Context):
        super().__init__(context)
        # 只读 FunctionTool 通过 add_llm_tools 注册
        context.add_llm_tools(ReadMemoryDetail(), ReadTodo())
        _ensure_global_templates()
        logger.info(f"[memory] 插件已加载，数据目录：{MEMORY_BASE}")

    # ── LLM 请求前注入 ────────────────────────────────────

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        user_id = event.get_sender_id()
        is_new = _is_new_user(user_id)

        if is_new:
            _ensure_user(user_id)
            logger.info(f"[memory] 新用户 {user_id}，已初始化记忆目录")

        inject = _build_inject_block(user_id)
        if is_new:
            inject += _NEW_USER_HINT

        msg = event.message_str.strip()
        if any(kw in msg for kw in _SAVE_KEYWORDS):
            inject += _build_save_hint()

        req.system_prompt += inject

    # ── 记忆写入 Tools（return str 回传 LLM 继续推理）─────

    @filter.llm_tool()
    async def update_profile(self, event: AstrMessageEvent, content: str) -> str:
        """更新用户画像。对话中识别到用户习惯、偏好、背景等信息时静默调用，无需告知用户。

        Args:
            content(string): 完整的用户画像 Markdown 内容，覆盖写入
        """
        user_id = event.get_sender_id()
        _ensure_user(user_id)
        _write(_fpath(user_id, "profile.md"), content)
        logger.info(f"[memory] {user_id} profile 已更新")
        return "profile updated"

    @filter.llm_tool()
    async def update_soul(self, event: AstrMessageEvent, content: str) -> str:
        """更新 Soul 设定。用户对助理的人格、名字、称呼或行为提出持久化要求时调用。

        Args:
            content(string): 完整的 Soul 设定 Markdown 内容，覆盖写入
        """
        user_id = event.get_sender_id()
        _ensure_user(user_id)
        _write(_fpath(user_id, "soul.md"), content)
        logger.info(f"[memory] {user_id} soul 已更新")
        return "soul updated"

    @filter.llm_tool()
    async def reset_profile(self, event: AstrMessageEvent) -> str:
        """将用户画像重置为空模板。

        Args:
        """
        user_id = event.get_sender_id()
        _write(_fpath(user_id, "profile.md"),
               "# 用户画像\n\n（待完善：用户背景、习惯、偏好、所在地等）\n")
        return "profile reset"

    @filter.llm_tool()
    async def reset_soul(self, event: AstrMessageEvent) -> str:
        """将 Soul 设定重置为空模板。

        Args:
        """
        user_id = event.get_sender_id()
        _write(_fpath(user_id, "soul.md"),
               "# Soul 设定\n\n（待完善：助理名称、对用户的称呼、人格风格、行为约束等）\n")
        return "soul reset"

    @filter.llm_tool()
    async def create_memory(self, event: AstrMessageEvent,
                            title: str, summary: str, content: str,
                            tags: list = None) -> str:
        """将本轮对话沉淀为历史记录。仅在用户明确要求沉淀记忆时调用。

        Args:
            title(string): 简洁的对话标题
            summary(string): 一句话结论，仅用于索引检索
            content(string): 按对话回合组织的正文，必须从"## 用户指令"开始，不包含 title/ID/Time 头部
            tags(array[string]): 分类标签数组，最多 5 个（可为空数组）
        """
        user_id = event.get_sender_id()
        _ensure_user(user_id)
        base_ts = _gen_id()
        h_dir = _history_dir(user_id)
        existing_files = os.listdir(h_dir) if os.path.exists(h_dir) else []
        suffixes = []
        for fname in existing_files:
            if fname.startswith(base_ts) and fname.endswith(".md"):
                stem = fname[:-3]
                suffix_part = stem[len(base_ts):]
                if not suffix_part:
                    suffixes.append(0)
                elif suffix_part.startswith("_") and suffix_part[1:].isdigit():
                    suffixes.append(int(suffix_part[1:]))
        record_id = f"{base_ts}_{max(suffixes) + 1:03d}" if suffixes else base_ts
        tags_clean = _normalize_tags(tags)
        tags_str = _format_tags(tags_clean)
        # 详情文件头部：标签行仅在有标签时写入
        header = f"# {title}\n\nID: {record_id}\nTime: {_now()}\n"
        if tags_str:
            header += f"标签：{tags_str}\n"
        _write(_hpath(user_id, record_id), f"{header}\n{content}\n")
        # 索引条目：标签行仅在有标签时写入
        entry_lines = [f"\n## {_now()} {title}", f"ID：{record_id}"]
        if tags_str:
            entry_lines.append(f"标签：{tags_str}")
        entry_lines += [f"摘要：{summary}", f"详情：history/{record_id}.md\n"]
        _append(_fpath(user_id, "history_index.md"), "\n".join(entry_lines))
        logger.info(f"[memory] {user_id} 新建记忆 {record_id}")
        return f"memory saved, ID: {record_id}"

    @filter.llm_tool()
    async def update_memory(self, event: AstrMessageEvent,
                            record_id: str, summary: str) -> str:
        """更新索引中某条历史记录的摘要。

        Args:
            record_id(string): 历史记录 ID
            summary(string): 新的一句话摘要
        """
        user_id = event.get_sender_id()
        idx_path = _fpath(user_id, "history_index.md")
        idx = _read(idx_path)
        # 按 "\n## " 分块，兼容索引条目中含任意字段（标签、摘要顺序不限）
        sections = idx.split("\n## ")
        record_found = False
        for i, block in enumerate(sections):
            if f"ID：{record_id}" not in block:
                continue
            record_found = True
            lines = block.split("\n")
            summary_updated = False
            for j, line in enumerate(lines):
                if line.startswith("摘要："):
                    lines[j] = f"摘要：{summary}"
                    summary_updated = True
                    break
            # 摘要行损坏或缺失时：在 ID 行后补写
            if not summary_updated:
                for j, line in enumerate(lines):
                    if line.startswith("ID："):
                        lines.insert(j + 1, f"摘要：{summary}")
                        break
            sections[i] = "\n".join(lines)
            break
        if not record_found:
            return f"record {record_id} not found"
        _write(idx_path, "\n## ".join(sections))
        return f"memory {record_id} updated"

    @filter.llm_tool()
    async def delete_memory(self, event: AstrMessageEvent, record_id: str) -> str:
        """删除某条历史记录，同步清理索引条目和详情文件。

        Args:
            record_id(string): 要删除的历史记录 ID
        """
        user_id = event.get_sender_id()
        removed = False
        path = _hpath(user_id, record_id)
        if os.path.exists(path):
            os.remove(path)
            removed = True
        idx_path = _fpath(user_id, "history_index.md")
        idx = _read(idx_path)
        pat = re.compile(
            r'\n## [^\n]+\nID：' + re.escape(record_id) + r'\n.*?(?=\n## |\Z)',
            re.DOTALL
        )
        new_idx, count = pat.subn("", idx)
        if count > 0:
            _write(idx_path, new_idx)
            removed = True
        return f"deleted {record_id}" if removed else f"record {record_id} not found"

    # ── TODO 写入 Tools ───────────────────────────────────

    @filter.llm_tool()
    async def create_todo(self, event: AstrMessageEvent, items: str) -> str:
        """创建当前会话的 TODO 列表。用户提出多个问题或任务涉及多步骤时主动调用。

        Args:
            items(string): 每行一个条目，格式为 '[ ] 任务描述'，多个条目用换行分隔
        """
        user_id = event.get_sender_id()
        _ensure_user(user_id)
        _write(_fpath(user_id, "todo.md"), f"# TODO\n\n{items}\n")
        logger.info(f"[memory] {user_id} TODO 已创建")
        return "todo created"

    @filter.llm_tool()
    async def complete_todo(self, event: AstrMessageEvent, item_index: int) -> str:
        """将 TODO 中第 N 个未完成任务标记为已完成。

        Args:
            item_index(number): 未完成条目的序号，从 1 开始
        """
        user_id = event.get_sender_id()
        path = _fpath(user_id, "todo.md")
        raw = _read(path)
        if not raw:
            return "no todos found"
        lines = raw.split("\n")
        unchecked = [i for i, l in enumerate(lines) if l.strip().startswith("[ ]")]
        target = int(item_index) - 1
        if target < 0 or target >= len(unchecked):
            return f"index {item_index} out of range"
        lines[unchecked[target]] = lines[unchecked[target]].replace("[ ]", "[x]", 1)
        _write(path, "\n".join(lines))
        return f"todo item {item_index} completed"

    @filter.llm_tool()
    async def update_todo(self, event: AstrMessageEvent, items: str) -> str:
        """覆盖更新整个 TODO 列表。

        Args:
            items(string): 完整的 TODO 条目内容，覆盖写入
        """
        user_id = event.get_sender_id()
        _write(_fpath(user_id, "todo.md"), f"# TODO\n\n{items}\n")
        return "todo updated"

    @filter.llm_tool()
    async def clear_todo(self, event: AstrMessageEvent) -> str:
        """清空当前 TODO 列表。所有任务完成后调用。

        Args:
        """
        user_id = event.get_sender_id()
        _write(_fpath(user_id, "todo.md"), "# TODO\n\n")
        return "todo cleared"

    # ── 备忘录写入 Tools ──────────────────────────────────

    @filter.llm_tool()
    async def add_memo_block(self, event: AstrMessageEvent, blocks: list) -> str:
        """在备忘录中批量新增 block，每个 block 内容自由组织。用户提到待办事项时调用。

        Args:
            blocks(array[string]): block 内容列表，每个元素是一段完整 Markdown 内容
        """
        user_id = event.get_sender_id()
        _ensure_user(user_id)
        path = _fpath(user_id, "memo.md")
        existing = _read(path)
        if not isinstance(blocks, list):
            return "error: blocks must be a list"
        clean_blocks = [b.strip("\n") for b in blocks if isinstance(b, str) and b.strip()]
        if not clean_blocks:
            return "error: no valid blocks provided"
        base_ts = _gen_id()
        next_seq = _next_memo_block_seq(existing, base_ts)
        created_ids = []
        entries = []
        for i, c in enumerate(clean_blocks):
            seq = next_seq + i
            block_id = base_ts if seq == 0 else f"{base_ts}_{seq:03d}"
            entries.append(f"<!-- block:{block_id} -->\n{c}\n<!-- /block -->")
            created_ids.append(block_id)
        _append(path, "\n\n" + "\n\n".join(entries) + "\n")
        logger.info(f"[memory] {user_id} 备忘录批量新增 {len(created_ids)} 个 block：{created_ids}")
        return f"added {len(created_ids)} memo blocks: {created_ids}"

    @filter.llm_tool()
    async def write_memo_block(self, event: AstrMessageEvent,
                               block_id: str, content: str) -> str:
        """按 block_id 覆盖写入某个备忘录 block 的内容，用于整理、合并或更新。部分修改时优先使用此工具而非删除。

        Args:
            block_id(string): 备忘录 block ID
            content(string): 新的 block Markdown 内容，覆盖写入
        """
        user_id = event.get_sender_id()
        path = _fpath(user_id, "memo.md")
        raw = _read(path)
        pattern = _memo_block_pattern(block_id)
        new_block = f"<!-- block:{block_id} -->\n{content.strip()}\n<!-- /block -->"
        new_raw, n = pattern.subn(new_block, raw, count=1)
        if n == 0:
            return f"block {block_id} not found"
        _write(path, new_raw)
        return f"memo block {block_id} updated"

    @filter.llm_tool()
    async def delete_memo_block(self, event: AstrMessageEvent, block_id: str) -> str:
        """按 block_id 删除某个备忘录 block。仅在整个 block 的全部内容均已完成或作废时调用。若只需移除 block 内的部分内容，应使用 write_memo_block 覆盖写入。

        Args:
            block_id(string): 要删除的备忘录 block ID
        """
        user_id = event.get_sender_id()
        path = _fpath(user_id, "memo.md")
        raw = _read(path)
        pattern = _memo_block_pattern(block_id)
        new_raw, n = pattern.subn("", raw, count=1)
        if n == 0:
            return f"block {block_id} not found"
        new_raw = re.sub(r'\n{3,}', '\n\n', new_raw).rstrip('\n') + '\n'
        _write(path, new_raw)
        return f"memo block {block_id} deleted"

    async def terminate(self):
        logger.info("[memory] 插件已卸载")
