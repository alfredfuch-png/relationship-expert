# Romance Expert (亲密关系 RAG)

与 `digital twin` 项目（同机上的 Obsidian RAG）同源的自用网页应用：只对 **指定文件夹内的 Obsidian 笔记** 建索引，再基于检索到的片段对话（RAG）。

## 与 Digital Twin 的区别

| 项目 | Digital Twin | Romance Expert（本项目） |
|------|----------------|---------------------------|
| 索引范围 | 整个 vault（`cfuobs_vault`） | 仅 **`关于亲密关系`** 文件夹：`…/cfuobs_vault/关于亲密关系` |
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

Default: `C:\Users\alfre\Documents\GitHub\cfuobs_vault\关于亲密关系`  
Override in `.env`:

```env
VAULT_PATH=C:/path/to/cfuobs_vault/关于亲密关系
```

Only `*.md` files **under this directory** (recursively) are indexed. After changing path or note content, click **Build index** again.

Tag routing, multi-note retrieval, and tuning knobs match Digital Twin — see that project’s README for `TAG_ROUTING_*` and `RETRIEVE_*` behavior.

## Privacy（推送到 GitHub 时）

- **你的 Obsidian 笔记不会进仓库**：笔记在 `VAULT_PATH` 指向的目录（默认在 vault 仓库外或你本机的 `cfuobs_vault/关于亲密关系`），项目只通过路径读取，**不会**把 `.md` 打进 git。
- **`.env` 与 `data/` 已忽略**：含 API Token 与本地索引（`chunks.jsonl`、`embeddings.npz`），克隆者拿不到你的密钥和已建索引。
- **其他人如何使用**：克隆后复制 `.env.example` → `.env`，填自己的 `AI_BUILDER_TOKEN`，把 `VAULT_PATH` 改成**他们自己的**笔记目录，再「构建索引」即可；没有你的笔记也能跑，只是回答基于他们自己的内容。

## Security

Treat `AI_BUILDER_TOKEN` like a password; rotate it if it was ever pasted into chat or checked into git.
