# Async TUI 重构 — 实施记录

## 实施状态：核心完成 ✅

### 已完成的文件

| 文件 | 状态 | 说明 |
|------|------|------|
| `src/tui/display.py` | ✅ 新建 | ChatDisplay — 消息存储 + Rich→ANSI→FormattedText 渲染 |
| `src/tui/engine_bridge.py` | ✅ 新建 | submit_async() — 线程桥接, Engine → asyncio.Queue → Display |
| `src/tui/async_app.py` | ✅ 新建 | AsyncApp — 持久 prompt_toolkit.Application + 命令处理 |
| `src/tui/app.py` | ✅ 修改 | 非交互模式保留同步, REPL 替换为 asyncio.run(async_repl) |
| `src/core/engine.py` | ✅ 修改 | tool 事件新增第5元素 tool_use_id，兼容旧格式 |
| `src/core/permissions.py` | ✅ 不改 | 已有 prompt_provider 回调机制，复用 |
| `src/tui/query.py` | ✅ 修改 | 兼容5元素事件解包 |
| `src/tui/notebook.py` | ✅ 修改 | 同上 |
| `src/commands/__init__.py` | ✅ 不改 | 通过 _DisplayConsole shim 桥接 |
| `tests/test_async_tui.py` | ✅ 新建 | 12 个测试覆盖 ChatDisplay + EngineBridge |
| `tests/test_engine.py` | ✅ 修修 | 兼容5元素事件 |

### 架构变化

**之前：** REPL 循环 → bordered_prompt (每轮新建 PTApp) → run_query (Rich Live/Spinner 渲染) → console.print 遍地

**之后：** 单个持久 PTApp ──→ TextArea 输入 → asyncio task → EngineBridge (线程) → ChatDisplay (Rich→ANSI→FormattedText) → PT Window 实时刷新

### 保留不变

- 所有 Engine 业务逻辑（提交/重试/工具执行/取消）
- 所有 PermissionChecker 逻辑
- 所有 Tool 逻辑
- 所有 / 命令处理逻辑
- --print/--auto-approve/--config 等参数
- Session 持久化
- Memory 系统

### 待后续完善

1. ~~**异步权限提示**~~ ✅ y/n/a key bindings + asyncio.Future
2. ~~**Buddy 模块**~~ ✅ 已删除 src/buddy/ 及测试
3. **模型选择** — /model 当前用的小 PTApp → 集成到主 App 浮层
4. **Worker 通知** — coordinator mode worker 结果展示

## 一、当前前后端调用关系 (原始分析)

## 一、当前前后端调用关系

```
┌─────────────────────────────────────────────────────────────────────┐
│  tui/app.py main() — REPL 循环                                      │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐  │
│  │ bordered_     │───>│ parse_input()    │───>│ run_query()      │  │
│  │ prompt()      │    │                  │    │ (tui/query.py)   │  │
│  │ (PTApp.run()) │    └──────────────────┘    │                  │  │
│  └──────────────┘                             │ for event in     │  │
│       ↑                                       │ engine.submit(): │  │
│       │ 用户输入                               │   text → Stream  │  │
│       │                                       │   tool → Spinner │  │
│       │                                       │   result → print │  │
│       │                                       └────────┬─────────┘  │
│       │                                                │            │
│  ┌────┴──────────────────────────────────────────────────────────┐  │
│  │  console.print() 遍布各处:                                     │  │
│  │  - app.py: 状态/错误/cost                                     │  │
│  │  - query.py: tool_call/tool_result 展示                       │  │
│  │  - rendering.py: StreamingMarkdown + SpinnerManager           │  │
│  │  - shell.py: shell/sandbox 输出                                │  │
│  │  - commands/__init__.py: /命令输出 (30+处)                    │  │
│  │  - buddy/*: 卡片 + 动画                                        │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  core/engine.py — Engine.submit()                                   │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐  │
│  │ LLM API call │───>│ yield text/       │───>│ tool execution   │  │
│  │ (sync HTTP)  │    │ tool_call/        │    │ ThreadPoolExecutor│  │
│  │              │    │ waiting events    │    │                  │  │
│  └──────────────┘    └──────────────────┘    └────────┬─────────┘  │
│                                                       │            │
│  ┌────────────────────────────────────────────────────┘            │
│  │  core/permissions.py — PermissionChecker._prompt_user()         │
│  │  ┌──────────────────────────────────────────────────────┐       │
│  │  │ console.print("Permission required: ...")            │       │
│  │  │ os.read(fd, 1) ← 直接读终端，阻塞整个线程            │       │
│  │  │ console.print(choice) ← 回显                         │       │
│  │  └──────────────────────────────────────────────────────┘       │
│  └─────────────────────────────────────────────────────────────────│
└─────────────────────────────────────────────────────────────────────┘

调用方向:
  tui/app.py → engine.submit()  (同步 generator)
  tui/app.py → console.print()  (直接渲染)
  tui/app.py → permissions.check() → os.read() (裸终端 I/O)
  commands/* → ctx.console.print() (直接渲染)
  buddy/* → console.print() + Live() (直接渲染 + 阻塞动画)
```

## 二、问题总结

| 问题 | 位置 | 影响 |
|------|------|------|
| core 层直接 `console.print` | `permissions.py:144,150,182` | 核心逻辑与渲染耦合 |
| core 层 `os.read(fd, 1)` 裸读终端 | `permissions.py:197` | 无法在 async TUI 中使用 |
| `time.sleep()` 阻塞 | `engine.py:310`, `buddy/render.py:176,182,195,197` | 阻塞事件循环 |
| Thread + /dev/tty Esc 检测 | `keylistener.py` | 绕过 prompt_toolkit，无法复用 |
| `console.print` 散落 80+ 处 | 全项目 | 无法统一切换渲染方式 |
| `bordered_prompt()` 每次创建新 PTApp | `prompt.py` | 无法持久化 UI 状态 |

## 三、接口设计

### 3.1 Display 协议 (前端抽象)

```python
class Display:
    """统一的前端渲染接口 —— 替代所有 console.print + Rich Live/Spinner。
    
    后端只调用这些方法，不直接接触终端。
    """

    # -- 消息生命周期 --
    def add_user_message(self, text: str) -> None: ...
    def start_assistant_stream(self) -> str: ...  # 返回 msg_id
    def append_token(self, msg_id: str, token: str) -> None: ...
    def finish_assistant_stream(self, msg_id: str) -> None: ...

    # -- 工具调用生命周期 --
    def add_tool_call(self, tool_name: str, tool_input: dict, activity: str|None) -> str: ...
    def update_tool_running(self, key: str) -> None: ...
    def update_tool_done(self, key: str, content: str, is_error: bool) -> None: ...

    # -- 系统消息 --
    def show_info(self, text: str) -> None: ...
    def show_error(self, text: str) -> None: ...
    def show_status(self, text: str) -> None: ...

    # -- 渲染 (供 prompt_toolkit 调用) --
    def render(self) -> "FormattedText": ...
    def render_status_line(self) -> list[tuple[str, str]]: ...
```

### 3.2 Prompt 协议 (用户输入抽象)

```python
class PromptBackend:
    """统一的用户输入接口 —— 替代 os.read() + input()。
    
    在同步 TUI 中用 os.read 实现，在 async TUI 中用 PT 内联弹窗实现。
    """

    async def ask_permission(self, tool_name: str, tool_input: dict) -> str:
        """返回 "allow" | "deny" | "always" """
        ...

    async def ask_user_question(self, questions: list) -> dict:
        """AskUserQuestion tool 的渲染。返回用户答案。"""
        ...
```

### 3.3 Engine 异步接口

```python
# 方案：Engine.submit_async() — 用线程桥接同步 submit()
# 原因：LLM SDK (anthropic/openai) 的同步 API 在线程中跑，
#       避免重写整个 LLMClient 层。

async def submit_async(engine: Engine, user_input, 
                       display: Display, prompt: PromptBackend,
                       loop=None) -> None:
    """在线程池中运行 engine.submit()，事件通过 asyncio.Queue 传回主线程。
    
    每个事件到达时调用 display 的相应方法更新 UI。
    """
    ...
```

### 3.4 Command 接口调整

```python
# CommandContext 不再持有 Console，改为持有 Display
@dataclass
class CommandContext:
    engine: Engine
    display: Display       # ← 替代 console
    prompt: PromptBackend  # ← 新增
    ...  # 其余不变
```

## 四、新文件/修改清单

### 新建
1. `src/tui/display.py` — ChatDisplay 类（消息存储 + Rich → ANSI → FormattedText 渲染）
2. `src/tui/async_app.py` — AsyncApp 类（持久 prompt_toolkit.Application，async 事件循环）

### 修改
3. `src/tui/app.py` — main() 改为 `asyncio.run(async_main())`，创建 AsyncApp + 桥接 Engine
4. `src/core/permissions.py` — 添加 `_prompt_callback`，支持外部注入异步处理
5. `src/core/engine.py` — 添加 async 桥接工具函数
6. `src/commands/__init__.py` — CommandContext 使用 Display 替代 Console

### 可废弃（保留但不再使用）
7. `src/tui/query.py` — run_query() 逻辑进 AsyncApp
8. `src/tui/keylistener.py` — Esc 改用 PT keybindings
9. `src/tui/rendering.py` — StreamingMarkdown/SpinnerManager 被 ChatDisplay 替代

## 五、目标架构图

```
┌────────────────────────────────────────────────────────────┐
│  asyncio event loop                                        │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  AsyncApp (prompt_toolkit Application)               │  │
│  │  ┌─────────────────────────────────────────────────┐ │  │
│  │  │  ChatDisplay (FormattedTextControl)              │ │  │
│  │  │  - add_user_message / append_token / ...         │ │  │
│  │  │  - render() → FormattedText                     │ │  │
│  │  └─────────────────────────────────────────────────┘ │  │
│  │  ┌─────────────────────────────────────────────────┐ │  │
│  │  │  TextArea (input)                                │ │  │
│  │  │  accept_handler → _on_send()                     │ │  │
│  │  └─────────────────────────────────────────────────┘ │  │
│  │  ┌─────────────────────────────────────────────────┐ │  │
│  │  │  StatusLine                                       │ │  │
│  │  └─────────────────────────────────────────────────┘ │  │
│  └──────────────────────────────────────────────────────┘  │
│                          │                                  │
│                    asyncio.create_task()                     │
│                          ▼                                  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  EngineBridge (在 ThreadPoolExecutor 中跑 engine)     │  │
│  │  ┌──────────────┐    ┌────────────────────────────┐  │  │
│  │  │ LLM API call │───>│ 事件 → asyncio.Queue        │  │  │
│  │  │ (sync HTTP)  │    │ → display 方法更新 UI       │  │  │
│  │  └──────────────┘    └────────────────────────────┘  │  │
│  │  ┌──────────────────────────────────────────────────┐ │  │
│  │  │ PermissionChecker                                 │ │  │
│  │  │ → prompt.ask_permission() (async, PT 内联弹窗)   │ │  │
│  │  └──────────────────────────────────────────────────┘ │  │
│  └──────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

## 六、实施步骤

1. **创建 `Display` 协议 + `ChatDisplay` 实现** — 消息存储 + Rich → ANSI 渲染
2. **创建 `PromptBackend`** — 抽象权限/问题提示
3. **修改 `permissions.py`** — 支持 callback-driven prompt
4. **创建 `EngineBridge`** — 在线程中跑 engine.submit()，事件入 asyncio.Queue
5. **创建 `AsyncApp`** — 持久 PT Application，集成 ChatDisplay + TextArea + StatusLine
6. **修改 `app.py` main()** — asyncio.run(async_main())
7. **修改 `commands/__init__.py`** — 适配 Display 接口
8. **适配 buddy** — 动画改为 async
9. **删除/清理** — EscListener, StreamingMarkdown, SpinnerManager
