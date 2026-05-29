# 私密备份 users.db

账户与对话存在 `data/users.db`。公开站重新部署后容器会清空，需从**私密**地址恢复。

## 打包

```powershell
.\scripts\package_users_db.ps1
```

生成 `relationship-expert-users.zip`（仅含 `users.db`）。**不要**上传到公开 GitHub Release。

## 上传到哪里

任选一种**外人无法随意列举/下载**的方式：

| 方式 | 说明 |
|------|------|
| 云对象存储 | 阿里云 OSS / 腾讯云 COS / Cloudflare R2：对象设为私有，使用**带过期时间的签名 URL** 或仅服务端知道的直链 |
| 私有仓库 + Token | 私有 GitHub 仓库的 Release 资产需 API + `USERS_DB_BEARER_TOKEN`，不要用浏览器公开链接 |
| 本机暂不上云 | 仅适合本地开发；线上必须提供 `USERS_DB_URL` |

得到 HTTPS 地址后写入 `.env`：

```env
USERS_DB_URL=https://...
# USERS_DB_BEARER_TOKEN=   # 可选
```

然后：

```powershell
cd backend
.\.venv\Scripts\python ..\scripts\deploy.py
```

## 日常维护

- 有新用户注册或重要对话后：重新 `package_users_db.ps1`，覆盖私密 zip，再部署（或只更新存储里的文件）。
- 公开索引更新：`package_index.ps1` → 上传公开 Release → 与账户库分开维护。

## 安全说明

- zip 内为 **bcrypt 密码哈希**，不是明文密码。
- 仍包含 **账户名** 与 **对话 JSON**；因此必须私密存放。
