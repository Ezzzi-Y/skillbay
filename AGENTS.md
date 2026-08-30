# AGENTS.md

## 项目定位

skillbay 是一个面向 LangChain 的、可插拔的、面向后端业务服务的 Agent Skill 系统。
核心思路 1:1 借鉴 Claude Code 的 Skill 系统（参考其 Python 移植版），但按后端
场景做了三项改造：无人工审批（ask ≡ deny）、技能是部署产物（构造时加载并锁定）、
allowed-tools 工具闸（wrap_tool_call 强制执行）。

完整的业务改造设计思路见 `README.md` / `README.zh-CN.md`，不要把设计论述写进
py 文件头。包已从 `skillkit` 完成改名迁移，现在一律叫 `skillbay`。

## 目录结构

- `src/skillbay/` — 核心包（src 布局），五个模块：
  - `middleware.py` — 主入口 `SkillMiddleware`（LangChain `AgentMiddleware`）。
    含 skill 工具、allowed-tools 闸门（`check_allowed_tools` 纯函数）、
    `before_model`（清单播报 + 压缩存活重注入）。改它之前先读 README 的
    「业务改造」章节。
  - `core.py` — `Skill` 数据模型、目录加载（约定 `<skills_dir>/<name>/SKILL.md`，
    靠后目录覆盖靠前目录、按 realpath 去重）、清单格式化（1% 上下文预算 +
    三级降级）。
  - `expansion.py` — SKILL.md 展开管线，五步顺序固定不能颠倒：
    Base directory 头 → 参数替换（$ARGUMENTS/$0/$foo）→ ${SKILL_DIR} →
    ${SESSION_ID} → 可选 !`命令` shell 块。
  - `frontmatter.py` — YAML 头解析。PyYAML 可选（没有则用平铺子集解析器），
    解析失败先修特殊字符再重试——一个技能写坏不能拖垮 agent 启动。
  - `__init__.py` — 公共 API 面；新增导出需同步 `__all__`。
- `tests/` — pytest 测试（纯函数级 + 中间件集成）。
- `pyproject.toml` — 包元数据与工具配置（Python >= 3.12，hatchling 构建，
  pytest / ruff 配置）。
- `uv.lock` / `.venv/` — uv 管理的锁文件与虚拟环境（Windows，
  激活脚本在 `.venv/Scripts/`）。
- `.python-version` — 钉住 Python 3.12。

## 架构边界（改动必读）

- 三层渐进式披露：① `before_model` 把预算化清单包进 `<system-reminder>` 作为
  user 消息写入 state；② 模型调 `skill` 工具时展开 SKILL.md 全文作为
  ToolMessage 返回；③ 正文里的相对路径靠 "Base directory" 头解析，由 agent
  自己的文件工具按需读取（本中间件不提供文件工具）。
- 技能记账（`announced_skills` / `skill_invocations`）放在 `SkillState`
  （AgentState 扩展），不放进程级 dict——为了随 checkpointer 持久化、按
  thread 隔离、resume 后不重复播报。
- 权限策略只有两态 allow/deny；返回 "ask" 按 deny 处理并发审计事件
  （后端服务没有「等人点确认」）。
- allowed-tools 闸门 `check_allowed_tools` 是纯函数（便于单测）：生效窗口从
  成功的 "Launching skill:" ToolMessage 起，到下一条真实 user 消息止；多个
  技能的白名单取并集；限制只紧不松；窗口内连 skill 工具本身也拦（防提权）。
- 被策略拒绝的技能调用不会激活其白名单（拒绝不能成为提权通道）。
- `` !`命令` `` shell 块默认关闭（`enable_shell_blocks=False`），是安全开关。
  Windows 注意：PATH 里的 bash 可能解析到未配置的 WSL 存根，输出需按 UTF-8
  解码（`errors="replace"`，见 expansion.py）。
- 技能集合在构造时一次性加载并锁定，不做运行时动态发现。

## 环境与命令

- 依赖已声明：`langchain`（1.x 的 `langchain.agents.middleware` API）/
  `langgraph` / `pydantic` / `typing-extensions`；`pyyaml` 为可选 extras
  （dev 组里也装了）。
- `uv sync` 装依赖；`uv run pytest` 跑测试（当前 52 个全过）；
  `uv run ruff check src tests` 与 `uv run ruff format src tests` 做 lint
  和格式化；不要用裸 pip / python。

## 约定

- **py 文件只放 `src/` 或 `tests/`，绝不放仓库根目录。**
- 代码注释与 docstring 用英文、保持简短；文件开头不写长篇设计论述（设计思路
  进 README）。日志与提示文案用英文。
- 日志用 `logging`（各模块 `logging.getLogger(__name__)`，包 logger 名
  `skillbay`）；`verbose=True` 时由 `_install_verbose_handler` 挂
  StreamHandler（`[skillbay]` 前缀）。不要用 print。
- 技能名以目录名为准，frontmatter 里的 name 只是显示名。
- Windows 环境运行：展开时把路径反斜杠统一为正斜杠（${SKILL_DIR} 处理），
  新增路径相关逻辑沿用此约定。
- 主分支是 `main`；提交信息用英文祈使句（chore/feat/test/docs 前缀）。
