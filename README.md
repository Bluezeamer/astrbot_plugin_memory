# astrbot_plugin_memory

AstrBot 跨会话持久化记忆插件。为每位用户独立维护 Soul 设定、用户画像、历史对话索引、备忘录与会话 TODO，在每次 LLM 请求前动态注入到 system_prompt，让 AI 助理真正"记住"用户。

## 功能概览

| 模块 | 说明 | 注入方式 |
|------|------|----------|
| Soul 设定 | 助理人格、名字、称呼等持久化设定 | 每次请求注入 |
| 用户画像 | 用户习惯、偏好、背景等自动积累 | 每次请求注入 |
| 历史索引 | 历史对话摘要，支持按需展开详情 | 每次请求注入 |
| 备忘录（Memo） | 跨会话待办事项，block 结构自由组织 | 每次请求注入 |
| TODO | 会话级任务列表，Agent 自主管理 | 按需读取 |

## 安装

### 方式一：通过 WebUI 输入 GitHub 链接安装（推荐）

1. 打开 AstrBot 后台管理界面，进入**插件**页面。
2. 点击**安装插件**，在输入框中粘贴本仓库的 GitHub 链接：

   ```
   https://github.com/Bluezeamer/astrbot_plugin_memory
   ```

3. 点击确认，AstrBot 会自动拉取并安装插件，无需手动操作。

> **提示**：后续版本更新时，同样在插件页面点击**更新**即可自动拉取最新代码。

### 方式二：手动安装（本地目录）

1. 将本仓库克隆或下载到 AstrBot 的插件目录：

   ```bash
   cd /AstrBot/data/plugins
   git clone https://github.com/Bluezeamer/astrbot_plugin_memory.git
   ```

   或者直接将项目文件夹复制进去，保证目录结构如下：

   ```
   /AstrBot/data/plugins/
   └── astrbot_plugin_memory/
       ├── main.py
       ├── metadata.yaml
       └── requirements.txt
   ```

2. 在 AstrBot WebUI 的**插件**页面点击**重载插件**，或重启 AstrBot。

> **注意**：插件目录名必须为 `astrbot_plugin_memory`，不能修改。

## 数据存储

所有用户数据存储在 AstrBot 数据目录下：

```
data/plugin_data/astrbot_plugin_memory/
├── templates/         # 全局模板目录（用户可自定义）
│   └── history_content.md
└── {user_id}/
    ├── soul.md            # Soul 设定
    ├── profile.md         # 用户画像
    ├── history_index.md   # 历史对话索引
    ├── memo.md            # 跨会话备忘录
    ├── todo.md            # 会话级 TODO
    └── history/           # 历史对话详情
        └── <record_id>.md
```

数据与插件目录分离，更新插件不会丢失用户数据。

### 从旧版本迁移

v1.2.0 之前的版本数据存储在 `/AstrBot/data/memory/`，升级后需手动迁移：

```bash
cp -r /AstrBot/data/memory/* /AstrBot/data/plugin_data/astrbot_plugin_memory/
```

迁移完成并确认新版本正常运行后，可删除旧目录。

## LLM 工具列表

插件注册以下工具供 LLM Agent 调用：

**记忆读取**

- `read_memory_detail` — 按记录 ID 读取历史对话详情
- `read_todo` — 读取当前 TODO 列表

**Soul / 画像管理**

- `update_soul` — 更新 Soul 设定（静默调用）
- `update_profile` — 更新用户画像（静默调用）
- `reset_soul` — 重置 Soul 设定为空模板
- `reset_profile` — 重置用户画像为空模板

**历史对话归档**

- `create_memory` — 将当前对话归档为历史记录
- `update_memory` — 更新已有历史记录
- `delete_memory` — 删除历史记录

**备忘录（block 操作）**

- `add_memo_block` — 批量新增备忘 block
- `write_memo_block` — 按 ID 覆盖写入某个 block
- `delete_memo_block` — 按 ID 删除某个 block

**TODO 管理**

- `create_todo` — 创建 TODO 列表
- `complete_todo` — 将指定条目标记为已完成
- `update_todo` — 覆盖更新 TODO 列表
- `clear_todo` — 清空 TODO 列表

## 触发沉淀记忆

在对话中发送以下任意关键词，可触发将本次对话归档为历史记录：

> 沉淀记忆 / 更新记忆 / 收录记忆 / 记住这次对话 / 把这个记下来

## 依赖

无额外依赖，仅使用 Python 标准库。

## 兼容性

- AstrBot >= 4.18.2
