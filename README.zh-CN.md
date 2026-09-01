<div align="center">

# skillbay

**适用于 LangChain 的可插拔技能中间件 —— 为业务服务设计的Skill系统**

[English](./README.md) | [简体中文](./README.zh-CN.md)

![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)
![LangChain](https://img.shields.io/badge/langchain-1.x-1C3C3C?logo=langchain&logoColor=white)
![uv](https://img.shields.io/badge/uv-managed-DE5FE9)
![Ruff](https://img.shields.io/badge/ruff-checked-261230?logo=ruff)

</div>

## 为什么要有 skillbay

Anthropic 提出了一套相当出色的Skill模型：通过一个`SKILL.md`文件来把一个目录里的流程、参考资料、脚本变成模型可以按需发现、延迟加载和精确执行的能力。这是 Anthropic 强大的Harness团队在该领域卓越的贡献之一，解决了一个非常经典的问题——一个Agent如何同时携带多种专业能力，又不撑爆上下文窗口。

这套机制本身是通用的，但主流实现是为一个特定场景打造的：开发者的编码与工作台。
skillbay 要把它搬进另一个场景——**业务服务**：消费 App 的客服入口、公司的智能财务/人事助手、接入内部系统的运营Copilot。要解的上下文窗口难题相同，但是运行环境和规则完全不同。

| | Claude Code：编码 / 工作场景 | skillbay：业务服务场景 |
|---|---|---|
| 和 agent 对话的人 | 开发者，能自己判断「这条命令该不该跑」 | 终端顾客或普通员工——没人能评审一次工具调用，会话也不可能挂起等一个确认框 |
| 技能是什么 | 编码工作流，用户随手安装 | 企业的业务能力——退款处理、报销政策、假期查询——由公司拥有，像业务代码一样被治理 |
| agent 以谁的身份行事 | 开发者本人的账号 | 公司的身份，而顾客从没同意过 agent 内部的任何细节——爆炸半径必须由设计兜底 |
| 会话形态 | 一个开发者、一个终端会话 | 成千上万路并发会话，带 checkpoint、可恢复 |

**skillbay** 因此把这套 Skill 系统在语义上 1:1 移植到 LangChain 的 middleware API 上，然后在所有「场景使然」（而非机制使然）的地方做了刻意的偏离。这些偏离才是本项目真正的内容。

## 业务改造

### 1. 技能是部署产物，不是运行时发现

在 Claude Code 里，安装技能的人就是被技能影响的人——运行时发现和技能市场是合理的。业务服务打破了这个对称：公司运营 Agent，承担后果的却是顾客和员工。技能的性质
也不同——退款规则、报销流程、人事政策是业务能力，不是开发者的便利工具。让一个技能
在挂载目录里「冒出来」，等于把未经评审的行为直接推到顾客面前：没有 review、没有版本、
没有回滚。

在Coding Agent中，安装技能的人就是使用技能的人，因此运行时发现Skill是合理的。而在业务服务中，开发者提供Skill，使用的人是客户，承担后果的人是公司。因此Skill必须由后端团队提供，无需运行时发现。

因此 skillbay 在**构造时一次性加载技能集合并锁定**：

- 技能进 git、走 code review、随版本发布。
- 运行期的文件变更在进程重启前不生效。
- 技能身份以目录名为准；frontmatter 的 `name` 只是显示名——磁盘上的改名不会悄悄
  改变路由行为。

### 2. allowed-tools 工具闸

业务服务里的 Agent 以客服/智能助手的身份行事——客服技能绝不能触及自身职权之外的工具。技能的
frontmatter 可以声明 `allowed-tools`；该技能生效期间，中间件的 `wrap_tool_call` 钩子
会对**每一次工具调用**强制执行白名单——不是「相信模型会遵守」，而是让它根本执行不了。

生效窗口完全从消息历史推导（没有可被污染的额外状态）：

- **开启**：某技能调用成功——其 ToolMessage 以 `Launching skill:` 开头。
- **关闭**：下一条真实 user 消息；`<system-reminder>` 注入不算新任务，不关窗口。
- **并集**：多个技能同时生效时白名单取并集或不允许多个Skill同时生效（可以使用Subagent机制来调用多Skill），且**只紧不松**——没声明 `allowed-tools` 的技能永远不能放宽已生效的限制。
- **窗口内连 `skill` 工具本身也拦**：封死「先调受限技能、再链一个不带限制的技能」
  的提权路径。

### 3. 记账放在 agent state，而不是进程全局字典

参考实现把已播报/已调用的技能记在模块级 dict 里——一个开发者的单会话进程无所谓；
客服系统要同时服务成千上万路可恢复、带 checkpoint 的会话，这么做就是错的。skillbay
把两本账都放进 `SkillState`（`AgentState` 扩展），换来三件事：

- **持久化**——记账随 checkpoint 存活；进程恢复后不会重复播报。
- **隔离**——按 thread 隔离，并发会话互不串扰。
- **压缩存活**——当 `SummarizationMiddleware` 把调用技能的那一轮对话压缩掉时，调用
  记录仍在 state 里。`before_model` 发现 `tool_call_id` 从消息列表里消失，就重新展开
  正文并以 system-reminder 注入。技能的指引活过了杀死它的转写记录的那次压缩。

### 4. 技能退场——模型可以释放不再需要的技能

其他技能系统（Claude Code、Codex、Cursor，以及我们见过的每一个LangChain Skill 中间件）都把Skill激活当作"一发不可收回"：一旦Skill正文被注入，它就会一直占用上下文窗口，直到对话结束或上下文被压缩掉。这对开发者会话来说没问题——人类知道任务何时结束——但对处理多轮对话的服务代理来说是错误的，因为对话会自然地跨越不同话题。

skillbay 引入了**技能退场**：模型可以调用 `skill_dismiss` 来释放一个技能范围不再
匹配当前对话的技能。退场后：

- 技能的完整正文**不再被重注入**到摘要压缩后的上下文中。
- 退场状态**持久化在 agent state** 中（随 checkpoint 存活/恢复）。
- **审计事件**（`skill_dismissed`）记录哪个技能被退场以及原因。

模型根据对话上下文决定何时退场——客户先问退款政策，然后转向询问物流时效，
就不需要退款技能的 12 步流程在整个会话中继续占用上下文了。

这是一个小机制，但对长期服务对话很重要。一是减少后端服务消耗的Token数量，二是尽力避免话题漂移。

## 鲁棒性设计

- **一个技能写坏，拖不垮启动。** frontmatter 解析永不抛异常：先试原文，失败后自动
  补引号重试（防住 `paths: **/*.{ts,tsx}` 这类经典手误），再失败降级为空头。缺
  `description` 的技能跳过并告警，而不是致命错误。
- **审计内建。** 五类审计事件（`skill_invoked`、`skill_reinjected`、
  `skills_announced`、`tool_call_blocked`、`skill_dismissed`）经由同一个回调缝发出，
  接到你的日志/指标栈即可。审计自身的故障不会拖垮 agent。
- **上下文预算与优雅降级。** 清单超出 1% 预算时，各条描述均分剩余额度截断；极端
  情况只播报名字。发现层永远不去挤占任务本身的上下文。

### LangChain接入
接入任意 LangChain `create_agent` agent：


```python
from skillbay import SkillMiddleware

mw = SkillMiddleware(
    skills_dirs=["skills"],
    audit=lambda event: print(event),  # 接到你的可观测性栈
)
agent = create_agent(model, tools=[...], middleware=[mw])
```

### Frontmatter 字段参考

| 字段 | 必填 | 作用 |
|---|---|---|
| `description` | 是 | 清单文案，驱动模型选择；缺失则技能被跳过。 |
| `allowed-tools` | 否 | 生效窗口内强制执行的工具白名单。 |
| `arguments` | 否 | 声明命名参数（`$foo`），按位置对应。 |
| `argument-hint` | 否 | 给人看的参数提示。 |
| `when_to_use` | 否 | 补充触发指引，追加到清单描述后。 |
| `disable-model-invocation` | 否 | 不进清单，仅允许用户手动触发。 |
| `shell` | 否 | `` !`...` `` 块的解释器（功能默认关闭）。 |
| `model` / `paths` / `version` | 否 | 已解析并携带；执行逻辑在路线图上。 |
