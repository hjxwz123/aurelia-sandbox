# Aurelia local Python sandbox (sidecar)

A tiny, self-hosted Python execution sandbox for local development. It implements
the exact 3-endpoint HTTP protocol the Go backend already speaks
(`server/internal/sandbox/sandbox.go`), so wiring it up is just an env var.

It is **not** a from-scratch sandbox: each session is a locked-down Docker
container running a pre-baked Python image. `/workspace` persists across `exec`
calls within a session (like ChatGPT Code Interpreter), and Chinese renders
correctly because the image ships Noto CJK fonts.

```
┌────────────┐   POST /sessions /exec /files   ┌──────────────┐   docker exec   ┌──────────────────┐
│ Go backend │ ──────────────────────────────► │ app.py (this)│ ──────────────► │ session container │
└────────────┘   SANDBOX_BASE_URL              └──────────────┘                 │  aurelia-sandbox  │
                                                                                 └──────────────────┘
```

## What's in the runtime image

- **Data science**: numpy, pandas, scipy, scikit-learn, statsmodels,
  matplotlib, seaborn, plotly (+ kaleido), pillow, sympy, networkx
- **Documents (§4.5.1)**: python-pptx, python-docx, openpyxl, xlsxwriter,
  reportlab, weasyprint (HTML→PDF), markdown, jinja2
- **Utilities**: pypdf, tabulate, requests, lxml, beautifulsoup4, pyyaml
- **Fonts**: Noto Sans CJK (SC/TC/JP/KR), Noto Color Emoji, DejaVu — matplotlib
  is pre-configured to use them, so no more tofu boxes (□□□) for Chinese.

Heavy extras (Playwright/Chromium for HTML screenshots, LibreOffice for format
conversion) are left commented at the bottom of `Dockerfile.runner` — uncomment
if you need them; they add ~400–500MB each.

## Deploy: build in the cloud, run on your server (recommended)

No local Docker needed. GitHub Actions builds both images and pushes them to
GitHub Container Registry; your server just pulls and runs them.

**1. Push the repo to GitHub** (the workflow lives at
`.github/workflows/build.yml`):

```bash
git init && git add . && git commit -m "sandbox service"
git branch -M main
gh repo create aurelia --private --source=. --remote=origin --push
# or: git remote add origin git@github.com:<you>/aurelia.git && git push -u origin main
```

The push triggers the build. Watch it under the repo's **Actions** tab; when
green you'll have:

```
ghcr.io/<you>/aurelia-sandbox:latest          # the Python runtime
ghcr.io/<you>/aurelia-sandbox-sidecar:latest  # the control service
```

> First time, the packages may be private. Make them visible to your server by
> either keeping them private and `docker login ghcr.io` on the server with a
> PAT (read:packages), or set the package visibility to public in GitHub.

**2. Pull the images on the server** (needs Docker installed there):

```bash
cd sandbox-service
export OWNER=<your-github-account-lowercase>

# Only needed when the GHCR packages are private:
# 1. Create a GitHub PAT with read:packages.
# 2. Paste that PAT when Docker asks for a password.
docker login ghcr.io -u "$OWNER"

docker pull ghcr.io/$OWNER/aurelia-sandbox:latest
docker pull ghcr.io/$OWNER/aurelia-sandbox-sidecar:latest
docker images "ghcr.io/$OWNER/aurelia-sandbox*"
```

**3. Generate and display the API key**

```bash
export SANDBOX_API_KEY=$(openssl rand -hex 24)
printf 'SANDBOX_API_KEY=%s\n' "$SANDBOX_API_KEY"
```

Keep this value. The sidecar requires it for requests, and the Go backend must
use the same key.

**4. Start the service**

```bash
docker compose pull
docker compose up -d
docker compose ps
curl -H "Authorization: Bearer $SANDBOX_API_KEY" http://localhost:48217/healthz
```

**5. Point the Go backend at it:**

```
SANDBOX_BASE_URL=http://<server-host>:48217
SANDBOX_API_KEY=<same value you exported above>
```

That's the whole loop — your local machine never builds or runs Docker.

---

## 部署：云端构建，服务器拉取运行（推荐）

本地不需要 Docker。每次推送到 GitHub 后，GitHub Actions 会构建两个镜像
并推送到 GitHub Container Registry（GHCR），服务器只负责拉取和运行。

**1. 推送仓库到 GitHub**（工作流文件在 `.github/workflows/build.yml`）：

```bash
git init && git add . && git commit -m "sandbox service"
git branch -M main
gh repo create aurelia --private --source=. --remote=origin --push
# 或者：
# git remote add origin git@github.com:<you>/aurelia.git
# git push -u origin main
```

推送后会触发 Actions。到仓库的 **Actions** 页面确认构建成功。成功后会得到：

```
ghcr.io/<you>/aurelia-sandbox:latest          # Python 运行时镜像
ghcr.io/<you>/aurelia-sandbox-sidecar:latest  # 控制服务镜像
```

> 如果 GHCR package 是私有的，服务器需要先登录 GHCR。可以创建一个带
> `read:packages` 权限的 GitHub PAT；或者在 GitHub Packages 页面把镜像
> 可见性改成 public。

**2. 在服务器上拉取镜像**（服务器需要已安装 Docker）：

```bash
cd sandbox-service
export OWNER=<你的-github-用户名-小写>

# 仅私有镜像需要执行：
# 1. 创建一个带 read:packages 权限的 GitHub PAT。
# 2. Docker 提示输入密码时粘贴这个 PAT。
docker login ghcr.io -u "$OWNER"

docker pull ghcr.io/$OWNER/aurelia-sandbox:latest
docker pull ghcr.io/$OWNER/aurelia-sandbox-sidecar:latest
docker images "ghcr.io/$OWNER/aurelia-sandbox*"
```

**3. 生成并显示密钥**

```bash
export SANDBOX_API_KEY=$(openssl rand -hex 24)
printf 'SANDBOX_API_KEY=%s\n' "$SANDBOX_API_KEY"
```

请保存输出的值。sidecar 会用它校验请求，Go 后端也必须配置同一个密钥。

**4. 启动服务**

```bash
docker compose pull
docker compose up -d
docker compose ps
curl -H "Authorization: Bearer $SANDBOX_API_KEY" http://localhost:48217/healthz
```

**5. 配置 Go 后端**

```
SANDBOX_BASE_URL=http://<服务器地址>:48217
SANDBOX_API_KEY=<上一步输出的同一个值>
```

这样就完成了：本地机器不用构建或运行 Docker。

---

## Optional: run locally (if you have a working Docker engine)

Requires a running Docker engine (Docker Desktop or Colima) and Python 3.10+.

```bash
cd sandbox-service

# 1. Build the runtime image (one-time, ~5–8 min, downloads the wheels + fonts)
docker build -f Dockerfile.runner -t aurelia-sandbox:latest .

# 2. Install the sidecar deps and run it on :8000
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```

Then set `SANDBOX_BASE_URL=http://127.0.0.1:8000` for the Go server.

## Smoke test (no backend needed)

```bash
SID=$(curl -s -XPOST localhost:8000/sessions | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -s -XPOST localhost:8000/exec -H 'content-type: application/json' -d "{
  \"session_id\": \"$SID\",
  \"code\": \"import matplotlib.pyplot as plt; plt.plot([1,2,3]); plt.title('中文标题'); plt.savefig('/workspace/outputs/p.png'); print('rows', 3)\"
}" | python3 -m json.tool
```

You should get `stdout: "rows 3\n"`, `exit_code: 0`, and one file `p.png`
(base64) in `files` — with the Chinese title rendered, not boxes.

## Configuration (env vars)

| Var | Default | Notes |
|---|---|---|
| `SANDBOX_IMAGE` | `aurelia-sandbox:latest` | runtime image tag |
| `SANDBOX_NETWORK` | `none` | set `bridge` to allow `pip install` at runtime |
| `SANDBOX_MEMORY` | `2g` | per-container memory cap |
| `SANDBOX_CPUS` | `1` | per-container CPU cap |
| `SANDBOX_PIDS_LIMIT` | `256` | fork-bomb guard |
| `SANDBOX_API_KEY` | _(empty)_ | when set, require `Authorization: Bearer …` |
| `SANDBOX_EXEC_TIMEOUT_CAP_MS` | `120000` | hard ceiling per exec (§4.5) |
| `SANDBOX_IDLE_TTL_SECONDS` | `1800` | idle sessions reaped after 30 min |

## Security posture (dev-grade)

Each session container runs **non-root**, `--network none`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, with memory/cpu/pids limits and a 120s exec
timeout. stdout/stderr are truncated to 32KB and produced files capped at 20MB
before returning — matching the §4.5 安全基线.

This is container-level isolation, fine for a single-host dev box. It is **not**
gVisor/microVM-grade. For production, replace the `docker run`/`docker exec`
calls in `app.py` with a gVisor, Firecracker, or E2B backend — the HTTP contract
and the Go side stay identical (that's the whole point of the thin adapter).

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/sessions` | `{}` | `{session_id}` |
| POST | `/exec` | `{session_id, code, timeout_ms?}` | `{stdout, stderr, exit_code, files[]}` |
| POST | `/files` | `{session_id, path, data_base64}` | `{ok}` |
| DELETE | `/sessions/{id}` | — | `{ok}` |
| GET | `/healthz` | — | `{ok, docker, image}` |

Artifacts in `files[]` are whatever the code wrote under `/workspace/outputs/`
during that exec (`{name, mime_type, data_base64}`). User uploads should be
written to `/workspace/uploads/` via `/files` — that's the path the
`python_execute` tool description tells the model about.
