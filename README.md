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

## Deploy: pull the public images and run (recommended)

This project publishes ready-to-use images to GitHub Container Registry. Users
do **not** need to build images, create their own GitHub repo, or log in to
GHCR. The images are public:

```
ghcr.io/hjxwz123/aurelia-sandbox:latest          # Python runtime
ghcr.io/hjxwz123/aurelia-sandbox-sidecar:latest  # control service
```

**1. Clone the public repo on your server**

```bash
git clone https://github.com/hjxwz123/aurelia-sandbox.git
cd aurelia-sandbox
```

**2. Pull the public images**

```bash
export OWNER=hjxwz123

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
SANDBOX_API_KEY=<same value printed above>
```

That's the whole loop. Users pull and run the public images; they do not build
Docker images.

---

## 部署：拉取公开镜像并运行（推荐）

本项目已经把可直接使用的镜像发布到 GitHub Container Registry。使用者
不需要构建镜像，不需要创建自己的 GitHub 仓库，也不需要登录 GHCR。镜像
是公开的：

```
ghcr.io/hjxwz123/aurelia-sandbox:latest          # Python 运行时镜像
ghcr.io/hjxwz123/aurelia-sandbox-sidecar:latest  # 控制服务镜像
```

**1. 在服务器上克隆公开仓库**

```bash
git clone https://github.com/hjxwz123/aurelia-sandbox.git
cd aurelia-sandbox
```

**2. 拉取公开镜像**

```bash
export OWNER=hjxwz123

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
SANDBOX_API_KEY=<上一步打印出来的同一个值>
```

这样就完成了。使用者直接拉取并运行公开镜像，不需要构建 Docker 镜像。

---

## Optional: build locally for development or customization

Regular users should use the public images above. This section is only for
maintainers or developers who want to change the runtime image. It requires a
running Docker engine (Docker Desktop or Colima) and Python 3.10+.

```bash
cd aurelia-sandbox

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
export SANDBOX_URL=${SANDBOX_URL:-http://localhost:48217}

SID=$(curl -s -XPOST "$SANDBOX_URL/sessions" \
  -H "Authorization: Bearer $SANDBOX_API_KEY" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -s -XPOST "$SANDBOX_URL/exec" \
  -H "Authorization: Bearer $SANDBOX_API_KEY" \
  -H 'content-type: application/json' \
  -d "{
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
| `SANDBOX_MAX_SESSIONS` | `16` | max active sandbox containers |
| `SANDBOX_MAX_CONCURRENT_EXECS` | `4` | max concurrent `/exec` calls across sessions |
| `SANDBOX_MAX_CONCURRENT_CREATES` | `2` | max concurrent Docker container creates |
| `SANDBOX_QUEUE_TIMEOUT_SECONDS` | `150` | how long a request waits for an internal slot |
| `SANDBOX_MAX_UPLOAD_BYTES` | `20971520` | max decoded size for one `/files` upload |
| `SANDBOX_MAX_FILES_PER_EXEC` | `20` | max returned artifacts per `/exec` |
| `SANDBOX_MAX_TOTAL_ARTIFACT_BYTES` | `52428800` | max total returned artifact bytes per `/exec` |
| `SANDBOX_READ_ONLY_ROOTFS` | `0` | opt in to read-only rootfs + tmpfs workspace |
| `SANDBOX_TMPFS_SIZE` | `256m` | tmpfs size for `/tmp` and `/home/sandbox` when read-only mode is on |
| `SANDBOX_WORKSPACE_TMPFS_SIZE` | `512m` | tmpfs size for `/workspace` when read-only mode is on |
| `SANDBOX_NOFILE_ULIMIT` | `1024:1024` | per-container open-file ulimit |

## Security posture (dev-grade)

Each session container runs **non-root**, `--network none`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, with memory/cpu/pids/nofile limits and a
120s exec timeout. `/exec` calls are serialized per session, globally
rate-limited, and stdout/stderr are streamed through a 32KB cap before
returning. Produced files are capped at 20MB each, 20 files, and 50MB total per
exec — matching the §4.5 安全基线 while keeping the HTTP contract unchanged.

On startup the sidecar also discovers existing `aurelia.sandbox=1` containers,
so a sidecar restart can keep tracking live sessions and reap stale ones later.

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
