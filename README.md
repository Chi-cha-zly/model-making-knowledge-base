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

NMM 涂装工作台支持上传模型照片、绘制或导入遮罩、明确批准模型调用、选择候选图，以及逐步查看和下载涂装结果。底层包含独立图片存储、确定性步骤渲染、持久工作流、HTTP API，以及 Qwen 视觉分析和 OpenAI 图像编辑适配器。未配置模型时生成请求明确返回“未配置”；接入层与工作台已通过模拟模型和隔离数据库验证，尚未验收真实服务的出图质量。NMM 项目与资料附件分开保存；现有知识库 ZIP 不包含 NMM 项目或工作流检查点，恢复知识库会保留已有 NMM 数据。NMM 独立备份仍待实现。

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

## NMM 涂装工作台

从左侧“NMM 涂装”进入项目列表，或访问 `/#/nmm`。新建项目时上传原图，用画笔圈出要修改的区域；可擦除、撤销、清空、调整预览透明度，也可导入与原图同尺寸的不透明黑白 PNG 遮罩。白色表示编辑区域，导出的遮罩始终保持原图尺寸。参考图按效果、配色、过程分类，过程参考仅在本地保存。

保存项目不会调用模型。点击估算后，确认框展示模型、调用次数上限、剩余额度和费用未知提示；明确批准后才开始生成。选择候选后再次确认后续教学分析。网络异常时可重试原操作，前端保留同一个请求 ID 和参数，不会自动增加新的生成请求；参数或版本变化后需要重新估算。

已经保存成功的生成结果可按零图片调用额度恢复到候选确认。工作台当前仍要求正数视觉授权；如果项目视觉额度已经耗尽，即使本次仅恢复缓存，也无法通过该入口继续。

结果页支持原图与当前步骤对比、步骤切换、红色变化区域、教学说明、放大预览、汇总图及单图下载。输出 ZIP 包含当前版本的效果、步骤、变化遮罩、汇总图和教学 JSON，不包含输入原图、参考图或内部调用记录，**不能用于恢复项目**。未压缩内容上限为 64 MiB，超限时可下载单张图片。项目删除需要再次确认。

进度优先使用 SSE，连接失败时降级为只读轮询；刷新页面不会自动重发模型调用。遮罩和生成的反光结构仍需人工核对，算法步骤与近似色值不能替代实际颜料配方或专业涂装指导。

## NMM 工作流 API

`/api/v1/nmm/projects` 支持创建、列出和读取项目；项目下的 `estimate`、`run`、`confirm`、`stop`、`events` 提供调用次数预览、执行、确认、停止及 SSE 进度，`export` 下载当前版本的输出 ZIP。图片由 `/api/v1/nmm/assets/{id}` 读取，添加 `?download=1` 可下载文件。具体上传和请求格式见后端 API 文档。

工作流通过 PostgreSQL 保存检查点，使用共享任务服务运行。等待确认时任务结束、项目保留暂停点；确认后沿同一版本继续。修改生成参数会创建新版本，生成尝试计数不会重置。默认允许首次生成及最多五次重新生成，失败或结果未知的生成也计入上限。服务重启会停止执行中的任务，用户需要显式恢复；不会在启动时重新调用模型。

同一次请求应重复使用相同的 `request_id` 和参数，新的操作使用新 ID。生成操作还必须提交当前 `revision` 和明确确认。教学参考图保留本地，不发送给生成或视觉 Provider。

真实模型调用需要先获取 `estimate`，再通过 `run` 或 `confirm` 的 `cost_authorization` 提交对应 `estimate_id`、`max_image_calls`、`max_vision_calls` 和 `accept_unknown_cost: true`。POST estimate 可携带拟修改的 `params`，估算本身不会修改项目参数。授权绑定输入、版本、参数和模型配置；配置或参数变化后须重新估算。每个项目累计最多 6 次图像调用、30 次视觉调用，失败和结果未知的尝试不退还次数。

这些限制是**调用次数预算，不是金额上限**。费用预览可能未知；记录的费用是按公开参考单价与返回用量估算，不能代替服务商账单。缺少用量明细时保留未知，不当作免费。每次请求发送前持久记录；取消、超时或重启留下的未知调用会冻结该次授权，需要重新明确批准。不会自动重发，成功视觉结果可从缓存恢复。外部服务执行和本地保存之间无法保证恰好一次；停止也不代表服务商停止计费。

工作流表由 Alembic 迁移 `0014` 创建，`0015` 添加项目授权与用量记录；启动或请求不自动建表。升级前备份运行库，再显式执行上述迁移命令。服务仍按单进程运行。

### 模型配置与单张验证

NMM 视觉分析使用独立的 `VISION_BASE_URL`、`VISION_MODEL=qwen3-vl-plus`、`VISION_API_KEY`，支持百炼北京和新加坡官方兼容端点。图像编辑独立使用 `IMAGE_BASE_URL=https://api.openai.com/v1`、`IMAGE_MODEL`、`IMAGE_API_KEY`，不会借用对话或视觉密钥。图像模型显式选择 `gpt-image-2.5-sunburst`、`gpt-image-2.5-flare` 或对应的 `-2026-09-08` 固定版本；未配置或不支持的组合会明确拒绝。原图与参考图会缩小后发送，编辑遮罩自动转换为服务要求的透明区域；输出再合成回原图，遮罩外保持原样。

当前请求将输入长边限制在 1536 像素，使用 PNG 透明通道遮罩（透明为可编辑，严格小于 4MB）；输出为单张不透明 PNG、medium 质量，按原图方向选择 1024×1024、1536×1024 或 1024×1536。GPT Image 2.5 不发送 `input_fidelity` 参数。视觉请求使用 JSON 模式，返回后再由本地 schema 校验。

接入协议依据 [OpenAI 图像编辑文档](https://developers.openai.com/api/reference/python/resources/images/methods/edit) 和 [百炼视觉文档](https://help.aliyun.com/zh/model-studio/qwen-vl-compatible-with-openai)。参考费用依据 [OpenAI 定价](https://developers.openai.com/api/docs/pricing) 和 [百炼国际站美元价目](https://www.alibabacloud.com/help/en/model-studio/model-pricing)，未计算缓存折扣或免费额度，价格及服务可用性可能变化。

在 `backend` 目录运行单张验证预演：

```sh
uv run python -m scripts.nmm_live_check \
  --original /path/to/original.png \
  --mask /path/to/mask.png
```

默认只检查指定输入与当前进程环境配置，不读取 `.env`、连接数据库、写文件或调用模型。执行真实验证需先在进程中设置独立模型变量，并显式追加 `--apply --i-understand-cost --max-calls 2 --database-url <专用且已迁移的PostgreSQL连接> --output <尚不存在的输出目录>`。成功路径执行一次视觉分析和一次图像编辑，停在等待确认，不自动生成教学步骤；提前失败时调用次数可能更少。失败也会保留调用账本；不要在命令行或公开日志中暴露连接凭据。

结果保存在指定数据库和新输出目录，包含候选图、用量及状态报告。后续 API 要继续该项目，必须在 CLI 退出后使用同一个数据库，并将附件根目录指向该输出目录下的 `attachments`；日常 API 不会自动发现隔离验证项目。参数及参考图角色见 `--help`。

## 当前限制

**默认使用词法检索。** 配置 `RETRIEVAL_MODE=hybrid` 并准备匹配的 `text-embedding-v4` / 1024 维向量后，搜索和对话可使用混合检索。查询会发送最多 2000 字符的问题到配置的向量服务，并可能产生费用；不会因此重算整库。模型未配置、没有可用向量、超时或返回无效向量时，会退回词法检索并记录原因。评测页面也可明确选择 BM25 / 混合对比。

HNSW 是近似索引，小资料库也可能由 PostgreSQL 选择精确扫描。可设置 `RETRIEVAL_MODE=rerank`，在混合召回后通过独立配置的 `qwen3-rerank` 重排并按阈值过滤，精确编号/别名命中保留；重排失败会保留融合结果并记录降级原因。最近邻结果不等于资料能回答问题，也不代表答案置信度。

`CHAT_MODE=agent` 可启用带检索工具的 Agent，默认仍为 RAG。Agent 引用需要真实检索证据并在保存前重新校验；工具轨迹不进入知识库备份。NMM 工作流独立于检索 Agent，工作台已提供，真实涂装效果尚未验收。

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
