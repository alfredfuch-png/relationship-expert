# Expert pack: prof_socio（专家教授）

## 结构

| 文件 | 作用 |
|------|------|
| `manifest.json` | id=`prof_socio` / 显示名 / 是否启用 |
| `persona.md` | 人设与对话风格 |
| `Questions.md` | 追问清单（问法跟 persona） |
| `knowledge.md` | **独立知识体系（单文件）** — 待你上传 |

目录名与 id 均为 `prof_socio`；索引落在 `data/experts/prof_socio/`。

## 启用条件

1. `knowledge.md` 已就位
2. 索引：`POST /api/index?expert_id=prof_socio` → `data/experts/prof_socio/`
3. `manifest.json` 的 `"enabled": true`
4. `/api/experts` 应出现「专家教授」

当前状态：知识文档已接入并已启用。

## 知识与阿FU隔离

- 阿FU：继续用 Obsidian vault → `data/`（或 `data/experts/afu`）
- 本专家：只用本包 `knowledge.md` → `data/experts/prof_socio/`
