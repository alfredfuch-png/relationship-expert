# 私密备份 users.db（自动同步到 Cloudflare R2）

账户与对话在服务端 `data/users.db`。**你不需要知道用户的密码**——服务器会在注册、保存对话后自动上传到 R2。

## 一次性配置（Cloudflare R2）

### 1. 创建 API Token

Cloudflare 控制台 → **R2** → **Manage R2 API Tokens** → **Create API token**

- 权限：**Object Read & Write**
- 限定到你的 bucket（例如 `relationship-expert-private`）

记下：

- **Account ID**（R2 概览页右侧）
- **Access Key ID**
- **Secret Access Key**

### 2. 公开下载链接（给服务器拉取用）

Bucket → **Settings** → 开启 **Public access**（R2.dev 子域）  
上传对象名：`relationship-expert-users.zip`（首次可空 zip，或等自动同步）

复制对象 URL，例如：

`https://pub-xxxx.r2.dev/relationship-expert-users.zip`

### 3. 写入 `.env`（勿提交 GitHub）

```env
# 冷启动时从 R2 下载恢复
USERS_DB_URL=https://pub-xxxx.r2.dev/relationship-expert-users.zip

# 注册 / 保存对话后自动上传（S3 API）
R2_ACCOUNT_ID=你的AccountID
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET_NAME=relationship-expert-private
R2_USERS_OBJECT_KEY=relationship-expert-users.zip

# 可选：手动触发一次备份（见下文）
USERS_DB_SYNC_SECRET=随机长字符串
```

### 4. 部署

```powershell
cd backend
.\.venv\Scripts\python ..\scripts\deploy.py
```

## 日常流程（无需本机建账户）

| 事件 | 服务器行为 |
|------|------------|
| 用户 **注册** | 写入 `users.db` → **立即上传 R2** |
| 用户 **聊天**（自动保存） | 更新 `users.db` → 约每 90 秒最多上传一次 |
| **重新部署** | 从 `USERS_DB_URL` 下载 zip → 账户与对话恢复 |

**你不需要**在其他用户注册后在本机 `manage_users.py add`。

## 把当前线上的 alfredcfu 备份进 R2（仅一次）

在配置好 R2 并部署**新代码之前**，若线上已有用户但 R2 仍为空，可先手动触发：

```powershell
$secret = "你在.env里设的USERS_DB_SYNC_SECRET"
Invoke-WebRequest -Method POST `
  -Uri "https://relationship-expert.ai-builders.space/api/admin/sync-users-db" `
  -Headers @{ "X-Sync-Secret" = $secret }
```

成功后再 `deploy.py`，避免用空 R2 覆盖现有账户。

## 本机脚本（仅管理员自用）

`package_users_db.ps1` 仅用于本机调试或紧急手工备份，**不是**给其他注册用户用的流程。

## 安全说明

- zip 内为 bcrypt 密码哈希 + 账户名 + 对话 JSON；R2 桶勿公开列举，下载链接勿发到公开场合。
- `R2_SECRET_ACCESS_KEY` 与 `USERS_DB_SYNC_SECRET` 只放在 `.env` / 部署环境。
