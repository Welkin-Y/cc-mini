# Async TUI 踩坑指南

## 1. `asyncio.run()` 不能嵌套

**现象**: `asyncio.run() cannot be called from a running event loop`

**原因**: prompt_toolkit 的 `Application.run_async()` 已经在 asyncio event loop 中运行。命令（如 `/model`）内部调用 `app.run()` 会再次尝试 `asyncio.run()`。

**修复**: 用 `loop.run_in_executor()` 把命令丢到独立线程执行，或改为 overlay 模态弹窗。

---

## 2. `Condition` 必须在所有使用之前导入

**现象**: `UnboundLocalError: local variable 'Condition' referenced before assignment`

**原因**: `_build_ui()` 方法内部有多处 `from prompt_toolkit.filters import Condition`，但如果某个 key binding 或 `ConditionalContainer` 的 filter 参数在 import 语句之前就引用了 `Condition`，就会报错。

**修复**: 把 `from prompt_toolkit.filters import Condition` 放到 `_build_ui()` 方法的最开头，只导入一次。

---

## 3. `FormattedTextControl` 不支持文本选择，且大量内容时渲染异常

**现象**: 聊天内容占满窗口后新 token 不渲染（stops rendering after window fills）

**原因**: `FormattedTextControl` 对超大 `FormattedText` 处理有上限，且不支持原生文本选择。

**修复**: 改用 `BufferControl` + `Buffer(read_only=False, focusable=False)`：
- 用 `buffer.text = plain_text` 设置内容
- `focusable=False` 确保键盘输入不意外进入缓冲区
- 支持文本选择 + 鼠标滚动 + 键盘翻页

---

## 4. `asyncio.Queue(maxsize=10)` 太小导致 `QueueFull`

**现象**: `Unhandled exception in event loop: asyncio.queues.QueueFull`

**原因**: 引擎线程通过 `call_soon_threadsafe(queue.put_nowait, event)` 推送事件，主线程渲染大内容时消耗慢，队列（maxsize=10）瞬间填满。

**修复**:
1. 增大 `maxsize` 到 500
2. 包装 `put_nowait` 为 `_enqueue_safe()`，捕获 `QueueFull` 静默丢弃（避免未处理异常）

---

## 5. 终端选中文本 vs PT 鼠标支持互斥

**现象**: 设置 `mouse_support=True` 后终端原生文本选择失效

**原因**: prompt_toolkit 的鼠标支持会拦截所有鼠标事件，终端模拟器无法处理鼠标拖拽选择。

**修复**: `mouse_support=False`，保留终端原生选择能力。鼠标滚动通过 `BufferControl` 的 PageUp/PageDown 键位替代。

---

## 6. 权限提示 key binding 必须在 filter 中判断状态

**现象**: 用户无法输入 `y`, `n`, `a` 字符

**原因**: 直接 `@kb.add("y")` 会全局拦截按键，即使没有权限提示。用户在这些字母时按键被消费而无法输入。

**修复**: 使用 `filter=Condition(lambda: self._permission_future is not None)` 让 key binding 仅在权限提示激活时生效。

---

## 7. `BufferControl` 设置 `read_only=True` 后无法直接赋值 `.text`

**现象**: `prompt_toolkit.buffer.EditReadOnlyBuffer`

**原因**: `buffer.text = value` 在 `read_only=True` 时会抛异常。

**修复**: 设置 `read_only=False` + `focusable=False`（后者防止用户意外编辑）。

---

## 8. `Window.height` 在 `ConditionalContainer` 中自动折叠

**现象**: overlay 始终不可见

**原因**: `Window.visible` 不是 prompt_toolkit 的公共 API，`Window` 没有 `visible` 属性。

**修复**: 用 `ConditionalContainer(content=Window(...), filter=Condition(...))` 控制显隐，不要手动设置 `window.visible`。

---

## 9. Engine 事件格式变更需保持向后兼容

**现象**: 添加 tool_use_id 作为第5元素后 `ValueError: too many values to unpack`

**原因**: 老代码用 4 元素解包 `_, name, input, result = event`，新增第5元素打破解包。

**修复**: 所有解包改为 `event[:4]`，兼容 5 元素格式。

---

## 10. `_refresh()` 必须在每次 display 变更后调用

**现象**: LLM token 不流式显示，只在最后一次性出现

**原因**: `display.append_token()` 只更新内存状态，不通知 PT 刷新。原来的 `_refresh()` 只在 `_run_engine()` 开始和结束时调用。

**修复**: 在 `submit_async()` 的每个 display-mutating 事件后调用 `refresh_callback`。
