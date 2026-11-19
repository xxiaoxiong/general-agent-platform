# 技能模块标准多文件 Skill 兼容完善计划

## Context（背景与目标）

平台技能（Skill）模块目前**只兼容提示词维度**，不兼容标准 Agent Skills 规范的多文件形态（`SKILL.md + scripts/*.py + references/*.md` 等目录组合）：

1. **目录技能丢文件**：`SkillRegistry.files_payload()`（`backend/app/services/agent_runtime/skills.py`）只注入 `/skills/<name>/SKILL.md`，技能目录里的脚本/引用文件被全部丢弃——SKILL.md 里引用 `scripts/foo.py` 时运行态虚拟文件系统里根本没有该文件，标准 skill 装不完整。
2. **管理面技能只能纯文本**：`Skill` 表（`backend/app/models/skill.py`）只有 `body` 列，API（`backend/app/api/v2/skills.py`）与管理界面（`frontend/src/views/system/SkillManage.vue`）只有单 textarea，无法创建带脚本的组合技能。

底层 deepagents `SkillsMiddleware` 本身已完整支持标准 skill（渐进式披露、按绝对路径读附属文件、`metadata.include_tools`），因此只需补齐**平台注入层与管理层**。

用户确认范围：**两处都补**（不做 zip 导入）。

安全口径不变（不放宽也不收紧）：
- 技能只注入 `/skills/**` 虚拟路径，**不携带任何工具授权**（工具面始终由 Capability Registry / ToolPolicy 决定）；
- 脚本执行能力仍由既有 fail-closed 策略控制（execute 需外部沙箱或 `allow_unsafe_python`），本次不改。

## 决策（已定）

- **存储**：`skills` 表加 `files = Column(JSON, nullable=True)`（`{相对POSIX路径: 文本内容}`，None=无附属文件），配套幂等 Alembic 迁移（head 已确认为 `c7d8e9f0a1b2`，照抄 `has_column` 守卫风格）。不建新表。
- **二进制口径**：目录技能的非 UTF-8 文件以 base64 注入（deepagents `create_file_data(content, encoding="base64")` 原生支持）；管理面 DB 附属文件本期**仅文本**（JSON 字符串 + UI 文本编辑）。
- **体积上限**（模块级常量，不进 config.py）：单文件 256KB、单技能总量 1MB（base64 后计）、单技能文件数 ≤50；超限跳过不注入（warning 日志），写入侧超限 400。
- **同名覆盖语义**：DB 技能与目录/内置技能同名时**整目录覆盖**（不是只覆盖 SKILL.md 的杂交残留）——`service.py::_skill_files()` 先剔除 registry 中 `/skills/<name>/` 前缀键再合并 DB 文件。
- **list API 返回完整 `files`**（与 `body` 同口径，编辑对话框需要）；按 AGENTS.md 陷阱 #2 口径把 `select(Skill).order_by(Skill.id)` 改为无序取回 + Python `sorted(key=id)`，避免 JSON 列放大后的 MySQL sort buffer 风险。

## 实施步骤

### 1. `backend/app/services/agent_runtime/skills.py`（核心）
- 新增模块级常量：`MAX_SKILL_FILE_BYTES`(256KB)、`MAX_SKILL_TOTAL_BYTES`(1MB)、`MAX_SKILL_FILE_COUNT`(50)。
- 新增 `validate_skill_relpath(path) -> str`：拒绝对路径、`..` 段、反斜杠、空段、`SKILL.md`（由 body 独占）、超长（>200）与非法字符；非法抛 `ValueError`（API 层映射 400，注入层防御性跳过）。
- 新增 `validate_skill_name(name)`：`^[A-Za-z0-9_-]{1,64}$`（防 `/skills/` 前缀逃逸），仅 create/update 校验，存量名不动。
- `SkillRegistry.files_payload()`：对 `os.path.isdir(meta.source)` 的目录技能 `os.walk` 递归注入 `/skills/<name>/<相对POSIX路径>`（跳过隐藏文件/目录、`__pycache__`、`*.pyc`）；UTF-8 文本直接注入，decode 失败转 base64（`encoding="base64"`）；应用体积/数量上限；内置技能行为不变（仅 SKILL.md）。
- `db_files_payload(rows, agent_id)`：除 `body` → `SKILL.md` 外，读 `getattr(row, "files", None)`，逐个 `validate_skill_relpath` 后注入 `/skills/<name>/<path>`（文本）；非法路径防御性跳过。
- 新增 `db_enabled_names(rows, agent_id)`：返回通过 enabled + `_binding_allows` 过滤的技能名集合（供整目录覆盖复用）。
- 修正误导性 docstring（"只注入指令文件"→"注入技能目录全部文件（指令 + 附属脚本/引用），但不携带工具授权"）。

### 2. `backend/app/models/skill.py`
- `Skill` 加 `files = Column(JSON, nullable=True, default=None)`，中文注释说明语义。

### 3. `backend/alembic/versions/`（新迁移文件）
- `add_skill_files_column.py`：`revision` 自拟，`down_revision = 'c7d8e9f0a1b2'`；upgrade 用 `has_column` 守卫 `op.add_column`，downgrade 守卫 `drop_column`（参照 `b8c9d0e1f2a3` / `c7d8e9f0a1b2` 风格）。

### 4. `backend/app/api/v2/skills.py`
- `SkillCreate` / `SkillUpdate` 加 `files: Optional[Dict[str, str]] = None`（SkillCreate 默认 `{}`；Update `None`=不改）。
- 写入前逐项 `validate_skill_relpath` + 单文件/总量/数量上限校验，失败 400；`name` 走 `validate_skill_name`（create 与 update 的 name 不可变，仅 create 校验）。
- `_db_skill_to_dict` 携带 `files`。
- `list_skills` 的 DB 查询去掉 `order_by`，Python `sorted(rows, key=lambda r: r.id)`。

### 5. `backend/app/services/agent_runtime/service.py`
- `_skill_files()`：合并前用 `db_enabled_names` 剔除 registry 中同名技能的 `/skills/<name>/` 前缀键，实现 DB 技能整目录覆盖（其余合并逻辑不动）。

### 6. 前端
- `frontend/src/api/extensions.ts`：`SkillItem` 加 `files?: Record<string, string>`。
- `frontend/src/views/system/SkillManage.vue`：
  - 对话框加"附属文件"编辑区：`{path, content}` 行列表（路径 input + 内容 textarea + 删除），可增行；提交时转 `Record<string, string>`；单文件/总量上限前端提示；
  - 表格加"附属文件"列（文件数）；
  - 页面说明补充：脚本执行需沙箱（execute 能力），无执行能力时脚本可读作参考——与既有安全策略一致。

### 7. 测试（服务层 + API 层）
- `backend/tests/test_skills_subagents.py`：
  - `test_files_payload_only_instruction_files` 改语义（断言所有键在 `/skills/` 前缀下、不含工具授权），更名如 `test_files_payload_confined_to_skills_prefix`；
  - 新增：目录技能多文件注入（脚本出现在 payload）、隐藏文件/`__pycache__` 跳过、非 UTF-8 转 base64、超限跳过、`validate_skill_relpath`/`validate_skill_name` 用例。
- `backend/tests/test_skill_management.py`：
  - `_FakeSkill` 加 `files` 属性；
  - 新增：DB 附属文件注入、非法路径防御性跳过、同名 DB 技能整目录覆盖 registry、`db_enabled_names` 绑定过滤；
  - API：create/update 带 files roundtrip、非法路径 400、非法 name 400、超限 400（沿用 `_CLEAN` 清理模式）。
- `backend/tests/test_agent_runtime.py` TestHarness 断言无需改动（多文件注入后 SKILL.md 键仍在），跑一遍确认。

## 验证

1. `cd backend && python -m pytest -q`（全量回归，当前 ~480 passed）。
2. `cd frontend && npm run build`（vue-tsc 类型检查 + 构建）。
3. `cd backend && python -m alembic upgrade head`（迁移幂等，重复执行不报错）。
4. Docker 冒烟（AGENTS.md 陷阱 #3：改代码必须重建镜像）：`docker compose -f docker/docker-compose.yml build backend frontend && docker compose -f docker/docker-compose.yml up -d`；在技能管理界面创建一个带附属文件（如 `scripts/hello.py`）的技能，发起对话，确认模型 `read_file /skills/<name>/SKILL.md` 后能读到 `/skills/<name>/scripts/hello.py`。

## 明确不做（保持最小变更）

- zip 技能包导入（用户未选）；
- DB 附属文件的二进制支持（本期仅文本）；
- execute/沙箱策略调整；
- 新增依赖、配置项。
