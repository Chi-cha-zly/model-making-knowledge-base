# 模型制作知识库

面向个人的本地资料管理工具：整理模型制作技巧、产品记录、原图和来源，检索制作资料，并在配置模型后进行带来源引用的对话。前端使用 React / TypeScript，后端使用 FastAPI / SQLAlchemy，数据保存在 PostgreSQL。

## 当前能力

- 管理来源、技巧和产品，支持图文正文、附件、发布与撤回。
- 在页面内居中阅读资料、切换原图，关闭后回到原浏览位置。
- 在数据库中执行关键词检索；可配置启用 1024 维向量召回、RRF 融合和编号/别名规则重排，查看命中的文字与图片引用。
- 本地 OCR 与可选的视觉模型描述分别标注；保留原图供人工核对。
- 对本地资料运行 BM25 / 混合检索对比，记录语料版本、实际执行策略和降级原因。旧版评测可继续查看；合成示例需要显式启用，历史 LangSmith 实验入口保留。
- 导出和恢复资料备份；支持显式入库、视觉描述和向量生成任务。
- 自定义模型连接的密钥保存在系统凭据库，设置文件仅保存元数据和凭据引用；`.env` 作为独立的只读外部配置来源。

NMM 涂装工作台已有图片存储、确定性步骤渲染、持久工作流与 HTTP API，可保存候选图、暂停等待确认、恢复后生成步骤和检查结果。目前没有工作台页面或真实图像模型适配器；默认生成请求会明确返回“未配置”，完整流程仅通过显式注入的离线测试 Provider 验证。NMM 项目与资料附件分开保存；现有知识库 ZIP 不包含 NMM 项目或工作流检查点，恢复知识库会保留已有 NMM 数据。NMM 独立备份仍待实现。

NMM 文件存储目前使用 macOS/Linux 的 POSIX 文件接口，尚不支持 Windows；单张图片限制为 20 MiB，参考图最多 2000 万像素，工作流原图和候选图最多 400 万像素且必须不透明。

## 启动

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 22.22+ 与 npm，以及 Docker Compose。示例数据库使用 PostgreSQL 17，包含 pgvector 和 pg_trgm 扩展。

在仓库根目录准备配置并启动数据库：

```sh
cp .env.example .env
docker compose up -d db
docker compose ps
```

等数据库状态为 healthy 后启动后端：

```sh
cd backend
uv sync --extra test --extra lint
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端，在仓库根目录启动前端：

```sh
cd frontend
npm ci
npm run dev
```

打开 <http://127.0.0.1:5173/>。API 文档位于 <http://127.0.0.1:8000/docs>。首次启动没有个人资料，可从资料管理页面添加来源和记录。

本工具目前按本机单用户场景设计，不提供面向公网的多用户认证与权限隔离。示例数据库端口只绑定本机；示例账号仅用于本地开发。

## 模型与 OCR

不配置模型密钥也可以管理资料、执行关键词检索和本地检索评测。对话使用设置页或 `.env` 中的 `AI_*` 配置；视觉描述使用独立的 `VISION_*` 配置。LangSmith 实验需要单独配置，并会上传所选问题、检索片段及实验结果。

默认本地 OCR 使用 macOS Apple Vision，需要 Swift 命令行工具。其他平台需安装 Tesseract 及中文、英文语言数据，并显式设置 `OCR_ENGINE=tesseract`；不会自动切换引擎。图片中没有可靠文字时允许保留空 OCR 结果。

向量生成使用独立的 `EMBEDDING_*` 环境变量。维护脚本默认预演，只有显式 `--apply` 才发送待处理文本。脚本不会自动读取 `.env`；请在执行进程中配置变量，并明确指定目标数据库。可在 `backend` 目录查看参数：

```sh
uv run python -m scripts.embed_documents --help
uv run python -m scripts.import_nmm --help
uv run python -m scripts.describe_images --help
```

原图、OCR、人工说明与模型视觉推断具有不同来源；识别结果和生成内容都需要对照原始资料核实。启用外部模型前，请确认所选资料适合发送给对应服务。

## 本地 NMM 步骤预览

准备原图、已确认的目标效果图和与原图同尺寸的黑白遮罩（白色为涂装区域），在 `backend` 目录运行：

```sh
uv run python -m scripts.nmm_render_demo \
  --original /path/to/original.png \
  --final /path/to/confirmed.png \
  --mask /path/to/mask.png \
  --output /path/to/new-output-directory \
  --steps 7
```

此命令只做本地计算，不读取数据库或调用模型。输出目录必须不存在，父目录须已存在；结果包括各步 PNG、变化区域遮罩、带编号的拼图及 `result.json`。macOS/Linux 的文件系统须支持原子无覆盖发布，不支持时明确报错。使用 `--help` 查看细节强度、过渡宽度、色差阈值等参数。

输入支持 PNG/JPEG/WebP，单张不超过 20 MiB；渲染原图与目标图各不超过 400 万像素，拒绝透明的原图/目标图。目标图尺寸不同时缩放到原图尺寸。先将 ICC 色彩转为 sRGB，再进行 Lab 聚类；调色板是近似色值，不对应具体颜料品牌、型号或调色比例。

请求步骤数为 4–9，颜色不足时会减少实际步骤并写入警告。基础色层的涂装区域逐层嵌套缩小，过渡和最终修饰不保证面积单调；差分遮罩表示相邻步骤的 ΔE2000 超过阈值的区域。每一步遮罩外保持原图，最后一步等于目标图在遮罩内与原图的合成结果。遮罩质量、目标图中的反光结构和保留雕刻细节的效果仍需人工检查；本地预览不能保证真实涂装教学质量。

## NMM 工作流 API

`/api/v1/nmm/projects` 支持创建、列出和读取项目；项目下的 `estimate`、`run`、`confirm`、`stop`、`events` 提供调用次数预览、执行、确认、停止及 SSE 进度。图片由 `/api/v1/nmm/assets/{id}` 读取。具体上传和请求格式见后端 API 文档；本阶段前端尚未提供这些入口。

工作流通过 PostgreSQL 保存检查点，使用共享任务服务运行。等待确认时任务结束、项目保留暂停点；确认后沿同一版本继续。修改生成参数会创建新版本，生成尝试计数不会重置。默认允许首次生成及最多五次重新生成，失败或结果未知的生成也计入上限。服务重启会停止执行中的任务，用户需要显式恢复；不会在启动时重新调用模型。

同一次请求应重复使用相同的 `request_id` 和参数，新的操作使用新 ID。生成操作还必须提交当前 `revision` 和明确确认；默认模型未配置时仍会拒绝执行。教学参考图保留本地，不发送给生成或视觉 Provider。此阶段尚不能配置付费 NMM Provider，调用次数预览也不构成真实服务的费用报价。

工作流表由 Alembic 迁移 `0014` 创建；启动或请求不自动建表。升级前备份运行库，再显式执行上述迁移命令。服务仍按单进程运行。

## 当前限制

**默认使用词法检索。** 配置 `RETRIEVAL_MODE=hybrid` 并准备匹配的 `text-embedding-v4` / 1024 维向量后，搜索和对话可使用混合检索。查询会发送最多 2000 字符的问题到配置的向量服务，并可能产生费用；不会因此重算整库。模型未配置、没有可用向量、超时或返回无效向量时，会退回词法检索并记录原因。评测页面也可明确选择 BM25 / 混合对比。

HNSW 是近似索引，小资料库也可能由 PostgreSQL 选择精确扫描。可设置 `RETRIEVAL_MODE=rerank`，在混合召回后通过独立配置的 `qwen3-rerank` 重排并按阈值过滤，精确编号/别名命中保留；重排失败会保留融合结果并记录降级原因。最近邻结果不等于资料能回答问题，也不代表答案置信度。

`CHAT_MODE=agent` 可启用带检索工具的 Agent，默认仍为 RAG。Agent 引用需要真实检索证据并在保存前重新校验；工具轨迹不进入知识库备份。NMM 工作流独立于检索 Agent；图像模型接入与费用台账仍待后续实现。

OCR 可能误读小字、遮挡、水印和漆号；视觉模型描述属于推断，不能替代原图。检索评测指标衡量与人工标签的一致性，不等同于回答事实正确率；合成示例的结果也不能代表真实资料上的效果。

系统凭据库支持原生平台后端；macOS 已进行真实主机验证，Windows/Linux 后端尚未完成真实主机验收。凭据库锁定或不可用时会明确报错，不会自动退回明文保存。

## 测试

后端数据库测试需要**单独的、可丢弃的 PostgreSQL 测试库**。不要把 `PG_TEST_DATABASE_URL` 指向日常运行库，也不要先对测试库运行项目的 Alembic 迁移；测试 fixture 会为每个测试建立临时 schema 并执行迁移。

以下示例启动与运行库分离的临时容器；数据库内容不持久保存：

```sh
docker run --detach --rm --name modelkb-test \
  -e POSTGRES_DB=modelkb_test \
  -e POSTGRES_USER=modelkb_test \
  -e POSTGRES_PASSWORD=test_only \
  -p 127.0.0.1:55432:5432 \
  --tmpfs /var/lib/postgresql/data \
  pgvector/pgvector:pg17
```

确认 `docker exec modelkb-test pg_isready -U modelkb_test -d modelkb_test` 成功后，从 `backend` 目录运行：

```sh
PG_TEST_DATABASE_URL='postgresql+psycopg://modelkb_test:test_only@127.0.0.1:55432/modelkb_test' \
  uv run --extra test pytest
```

后端风格检查在 `backend` 目录运行：

```sh
uv run --extra lint ruff check app tests scripts alembic
uv run --extra lint ruff format --check app tests scripts alembic
```

自动化测试使用模拟模型请求与模拟系统凭据，不需要真实模型密钥。测试结束后，可执行 `docker stop modelkb-test` 移除这份临时测试库。

前端验证在 `frontend` 目录执行：

```sh
npm test
npm run build
npm run format:check
```

资源受限、并行测试出现超时时，可用 `npm test -- --maxWorkers=1` 顺序运行完整前端套件；不会延长单项测试的超时限制。

## 数据与开发说明

仓库不包含个人素材、资料库、附件、备份或密钥。Git 不能替代资料备份；升级或恢复数据前应另行备份数据库和附件。只导入有权使用的资料，并保留来源信息。

本项目使用 AI 编程助手参与实现、测试和代码审查，部分提交由助手生成。功能范围、验收标准和资料使用由维护者确认；AI 辅助与自动化测试不能替代人工核对或独立的安全审计。
