# Romance Expert (亲密关系 RAG)

与 `digital twin` 项目（同机上的 Obsidian RAG）同源的自用网页应用：只对 **指定文件夹内的 Obsidian 笔记** 建索引，再基于检索到的片段对话（RAG）。

## 与 Digital Twin 的区别

| 项目 | Digital Twin | Romance Expert（本项目） |
|------|----------------|---------------------------|
| 索引范围 | 整个 vault | 仅 **`VAULT_PATH`** 下的 Markdown（专题目录由你在 `.env` 指定） |
| 产品定位 | 个人知识分身 | 亲密关系 / 恋爱向笔记问答 |

模型、网关、向量存储方式与 Digital Twin 一致。

## Models (configured)

| Step | Choice |
|------|--------|
| **Chat** | `deepseek` via AI Builders gateway (`POST .../chat/completions`) |
| **Embeddings** | `text-embedding-3-small` via gateway (`POST .../embeddings`); **BM25 fallback** if embeddings fail |

Secrets live in **[`.env`](.env)** (see [`.env.example`](.env.example)). Do **not** commit `.env`.

**Note:** ChromaDB was skipped so you do not need Visual C++ build tools on Windows. Vectors are stored in [`data/embeddings.npz`](data/embeddings.npz) with NumPy cosine search.

## Quick start

端口与 **digital twin**（8000 / 5173）区分：Romance Expert 默认 **8001**（API）与 **5174**（前端）。

**1. Backend** (from repo root):

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

**2. Frontend:**

```powershell
cd web
npm install
npm run dev
```

Open **http://localhost:5174** → **构建索引** → 对话。

## Layout

- [`backend/app/`](backend/app/) — FastAPI, ingest, embeddings, retrieval, NDJSON-streaming chat  
- [`web/src/App.tsx`](web/src/App.tsx) — Chat layout (sidebar + thread + composer)  
- Indexed data cache: **`data/`** (gitignored), `chunks.jsonl`, `embeddings.npz`, `tag_embeddings.npz`

## Notes path

Set in `.env` (create from `.env.example`):

```env
VAULT_PATH=./notes
```

Use any folder that contains your relationship-topic Obsidian notes (only `*.md` under that path are indexed).

Only `*.md` files **under this directory** (recursively) are indexed. After changing path or note content, click **Build index** again.

Tag routing, multi-note retrieval, and tuning knobs match Digital Twin — see that project’s README for `TAG_ROUTING_*` and `RETRIEVE_*` behavior.

## Privacy（推送到 GitHub 时）

- **Obsidian 笔记不会进仓库**：笔记只在 `VAULT_PATH` 指向的本机目录，项目运行时读取，**不会**把 `.md` 或 `data/` 索引打进 git。
- **`.env` 与 `data/` 已忽略**：含 API Token 与本地索引（`chunks.jsonl`、`embeddings.npz`），克隆者拿不到你的密钥和已建索引。
- **其他人如何使用**：克隆后复制 `.env.example` → `.env`，填自己的 `AI_BUILDER_TOKEN`，把 `VAULT_PATH` 改成**他们自己的**笔记目录，再「构建索引」即可；没有你的笔记也能跑，只是回答基于他们自己的内容。

## 多账户登录（小范围分享）

公开站使用 **账户名 + 密码**，每位用户只能看到自己的对话记录（存在服务端 `data/users.db`）。

**推荐：邀请码自助注册**（在 `.env` 中设置，部署时由 `scripts/deploy.py` 注入）：

```env
ALLOW_REGISTRATION=true
REGISTRATION_INVITE_CODE=你私下发给朋友的邀请码
SESSION_SECRET=随机长字符串
```

访客打开网站 →「注册」→ 填账户名、密码、邀请码 → 自动登录。把邀请码私下发给要邀请的人即可。

**或管理员手动建账户：**

```powershell
cd backend
.\.venv\Scripts\python ..\scripts\manage_users.py add 张三 your-password
.\.venv\Scripts\python ..\scripts\manage_users.py list
```

或在 `.env` 中一次性引导（仅在 `users.db` 为空时）：

```env
USERS_BOOTSTRAP=张三:pass1,李四:pass2
```

**上线时保留账户库：** 把 `data/users.db` 打进索引包（`.\scripts\package_index.ps1` 会自动包含），重新上传 Release 后部署；或单独设置 `USERS_DB_URL` 指向私密下载地址。

部署脚本会从 `.env` 注入 `USERS_BOOTSTRAP`、`USERS_DB_URL`（勿提交真实密码到 GitHub）。

## Deploy（AI Builders Space）

公开托管遵循 [AI Builders 部署说明](https://www.ai-builders.com/resources/students-backend/openapi.json)：单进程、根目录 `Dockerfile`、`PORT` 环境变量、静态前端由 FastAPI 同端口提供。

**公开版行为**（`PUBLIC_DEPLOY=true`，见 `deploy-config.json`）：

- 回答**不显示**出处列表、标签路由说明，也不在正文里要求 `[1]` 引用编号
- 访客**不能**在网页上重建索引（索引由你在服务端维护）

**知识库上云（不提交源 `.md`）**：

1. 本地先构建索引（`data/` 已有 `chunks.jsonl` 等）
2. `.\scripts\package_index.ps1` → 生成 `relationship-expert-index.zip`（已在 `.gitignore`，勿提交公开仓库）
3. 把 zip 上传到你控制的私密存储（对象存储 / 私有下载链接等）
4. 在 `deploy-config.json` 的 `env_vars` 里增加 `INDEX_BUNDLE_URL` 为该 zip 的 HTTPS 地址
5. 推送代码后执行部署：

```powershell
cd backend
.\.venv\Scripts\python ..\scripts\deploy.py
```

部署成功后访问：**https://relationship-expert.ai-builders.space**（`service_name` 与 `deploy-config.json` 一致）。

平台会自动注入 `AI_BUILDER_TOKEN`，无需写进 `env_vars`。

## Security

Treat `AI_BUILDER_TOKEN` like a password; rotate it if it was ever pasted into chat or checked into git.
