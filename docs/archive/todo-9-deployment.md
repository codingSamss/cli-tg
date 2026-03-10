# TODO-9: 部署与文档

## 目标
为生产部署和开源发布做准备，包括完善文档、Docker 配置、CI/CD 流水线和社区贡献指南。

## 部署架构

### 基础设施选项
```
部署选项：
├── Docker 单机部署
├── Docker Compose
├── Kubernetes
├── 云服务
│   ├── AWS (EC2, ECS, Lambda)
│   ├── Google Cloud (Compute, Cloud Run)
│   └── Azure (VMs, Container Instances)
└── VPS (DigitalOcean, Linode 等)
```

## Docker 配置

### 生产环境 Dockerfile
```dockerfile
# docker/Dockerfile
FROM python:3.11-slim as builder

# 安装构建依赖
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# 安装 Claude Code
RUN curl -fsSL https://storage.googleapis.com/public-download-service-anthropic/claude-code/install.sh | bash

# 创建虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制依赖文件
COPY requirements/base.txt /tmp/requirements.txt

# 安装 Python 依赖
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# 生产阶段
FROM python:3.11-slim

# 安装运行时依赖
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制 Claude Code
COPY --from=builder /usr/local/bin/claude /usr/local/bin/claude

# 复制虚拟环境
COPY --from=builder /opt/venv /opt/venv

# 创建非 root 用户
RUN useradd -m -u 1000 botuser && \
    mkdir -p /app /data && \
    chown -R botuser:botuser /app /data

# 设置环境变量
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CLAUDE_CODE_PATH=/usr/local/bin/claude

# 复制应用代码
WORKDIR /app
COPY --chown=botuser:botuser . .

# 切换到非 root 用户
USER botuser

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python scripts/check_health.py

# 运行 bot
CMD ["python", "-m", "src.main"]
```

### Docker Compose 配置
```yaml
# docker/docker-compose.yml
version: '3.8'

services:
  bot:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    container_name: claude-code-bot
    restart: unless-stopped
    env_file:
      - ../.env
    volumes:
      - bot-data:/data
      - ${APPROVED_DIRECTORY}:/projects:ro
    networks:
      - bot-network
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '0.5'
          memory: 512M

  # 可选：监控
  prometheus:
    image: prom/prometheus:latest
    container_name: bot-prometheus
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus-data:/prometheus
    networks:
      - bot-network
    ports:
      - "9090:9090"

  # 可选：Grafana
  grafana:
    image: grafana/grafana:latest
    container_name: bot-grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
      - GF_USERS_ALLOW_SIGN_UP=false
    volumes:
      - grafana-data:/var/lib/grafana
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards
    networks:
      - bot-network
    ports:
      - "3000:3000"

volumes:
  bot-data:
  prometheus-data:
  grafana-data:

networks:
  bot-network:
    driver: bridge
```

## Kubernetes 部署

### Kubernetes 清单
```yaml
# k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: claude-code-bot
  labels:
    app: claude-code-bot
spec:
  replicas: 1
  selector:
    matchLabels:
      app: claude-code-bot
  template:
    metadata:
      labels:
        app: claude-code-bot
    spec:
      serviceAccountName: claude-code-bot
      containers:
      - name: bot
        image: your-registry/claude-code-bot:latest
        imagePullPolicy: Always
        env:
        - name: DATABASE_URL
          value: "sqlite:///data/bot.db"
        envFrom:
        - secretRef:
            name: claude-code-bot-secrets
        volumeMounts:
        - name: data
          mountPath: /data
        - name: projects
          mountPath: /projects
          readOnly: true
        resources:
          requests:
            memory: "512Mi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "2"
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 30
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /ready
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 10
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: bot-data-pvc
      - name: projects
        hostPath:
          path: /opt/projects
          type: Directory

---
apiVersion: v1
kind: Service
metadata:
  name: claude-code-bot
spec:
  selector:
    app: claude-code-bot
  ports:
  - port: 8080
    targetPort: 8080
  type: ClusterIP

---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: bot-data-pvc
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
```

## 文档

### README.md
```markdown
# Claude Code Telegram Bot

[![Tests](https://github.com/yourusername/claude-code-telegram/workflows/Tests/badge.svg)](https://github.com/yourusername/claude-code-telegram/actions)
[![Coverage](https://codecov.io/gh/yourusername/claude-code-telegram/branch/main/graph/badge.svg)](https://codecov.io/gh/yourusername/claude-code-telegram)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

通过 Telegram 远程运行 Claude Code，提供类终端界面。

![演示 GIF](docs/images/demo.gif)

## 功能特性

- **类终端命令** - 使用熟悉的命令（`cd`、`ls`、`pwd`）导航项目
- **完整 Claude Code 集成** - 远程访问所有 Claude Code 功能
- **安全优先** - 目录隔离、用户认证、限流
- **项目管理** - 便捷的项目切换和会话持久化
- **高级功能** - 文件上传、Git 集成、快捷操作
- **使用量追踪** - 按用户监控费用和使用情况
- **可扩展** - 插件化架构，支持自定义功能

## 快速开始

### 1. 前置条件

- Python 3.9+
- 已安装 Claude Code CLI
- Telegram Bot Token（从 [@BotFather](https://t.me/botfather) 获取）
- Linux/macOS（支持 Windows WSL）

### 2. 安装

```bash
# 克隆仓库
git clone https://github.com/yourusername/claude-code-telegram.git
cd claude-code-telegram

# 安装依赖
pip install -r requirements/base.txt

# 复制环境变量模板
cp .env.example .env

# 编辑配置
nano .env
```

### 3. 配置

编辑 `.env` 填入你的配置：

```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_BOT_USERNAME=your_bot_username
APPROVED_DIRECTORY=/home/user/projects
ALLOWED_USERS=123456789,987654321  # 你的 Telegram 用户 ID
```

### 4. 运行

```bash
# 开发环境
poetry run claude-telegram-bot

# 生产环境使用 Docker
docker-compose up -d
```

## 使用方法

### 基本命令

```
/start - 初始化 bot
/ls - 列出当前目录文件
/cd <dir> - 切换目录（恢复该项目的会话）
/pwd - 显示当前目录
/projects - 显示所有项目
/new - 清除上下文，开始新会话
/continue - 明确继续上一个会话
/end - 结束当前会话并清除上下文
/status - 显示会话信息（含可恢复会话）
```

### 示例工作流

1. **开始会话**
   ```
   /projects
   [选择你的项目]
   ```

2. **导航和探索**
   ```
   /ls
   /cd src
   /pwd
   ```

3. **与 Claude 编码**
   ```
   你: 创建一个用户认证的 FastAPI 端点
   Claude: 我来创建一个用户认证的 FastAPI 端点...
   ```

4. **使用快捷操作**
   ```
   [运行测试] [安装依赖] [代码检查]
   ```

## 安全

- **目录隔离**：所有操作限制在已批准的目录内
- **用户认证**：白名单或令牌方式
- **限流**：防止滥用和控制费用
- **输入验证**：防护注入攻击
- **审计日志**：追踪所有操作

详见 [SECURITY.md](SECURITY.md)。

## 开发

### 搭建开发环境

```bash
# 安装开发依赖
pip install -r requirements/dev.txt

# 安装 pre-commit 钩子
pre-commit install

# 运行测试
pytest

# 热重载运行
poetry run claude-telegram-bot --debug
```

### 项目结构

```
claude-code-telegram/
├── src/               # 源代码
├── tests/             # 测试套件
├── docs/              # 文档
├── docker/            # Docker 文件
└── scripts/           # 工具脚本
```

## 贡献

欢迎贡献！请查看 [CONTRIBUTING.md](CONTRIBUTING.md) 了解贡献指南。

### 开发流程

1. Fork 仓库
2. 创建功能分支 (`git checkout -b feature/amazing-feature`)
3. 提交变更 (`git commit -m 'Add amazing feature'`)
4. 推送分支 (`git push origin feature/amazing-feature`)
5. 创建 Pull Request

## 部署

### Docker

```bash
docker build -t claude-code-bot .
docker run -d --name claude-bot --env-file .env claude-code-bot
```

### Kubernetes

```bash
kubectl apply -f k8s/
```

### 云平台

- [AWS 部署指南](docs/deployment/aws.md)
- [Google Cloud 指南](docs/deployment/gcp.md)
- [Azure 指南](docs/deployment/azure.md)

## 配置选项

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TELEGRAM_BOT_TOKEN` | BotFather 颁发的 Bot Token | 必填 |
| `APPROVED_DIRECTORY` | 项目基目录 | 必填 |
| `ALLOWED_USERS` | 逗号分隔的用户 ID | 无 |
| `RATE_LIMIT_REQUESTS` | 每分钟请求数 | 10 |
| `CLAUDE_MAX_COST_PER_USER` | 每用户最大费用（USD） | 10.0 |

完整配置参见 [docs/configuration.md](docs/configuration.md)。

## 故障排查

### 常见问题

**Bot 无响应**
- 检查 bot token 是否正确
- 确认 bot 没有重复运行
- 查看日志：`docker logs claude-bot`

**权限拒绝错误**
- 确保已批准的目录存在且可读
- 检查文件权限

**限流错误**
- 调整配置中的 `RATE_LIMIT_REQUESTS`
- 检查用户是否超出费用限额

更多请参见 [docs/troubleshooting.md](../troubleshooting.md)。

## 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件。

## 致谢

- Anthropic 提供 Claude Code
- Telegram Bot API
- 所有贡献者和测试者

## 支持

- Issues: [GitHub Issues](https://github.com/yourusername/claude-code-telegram/issues)

---

由社区用心打造
```

### CONTRIBUTING.md
```markdown
# 贡献指南

感谢你有兴趣为本项目做出贡献！我们欢迎所有人的贡献。

## 行为准则

请阅读并遵守我们的 [行为准则](CODE_OF_CONDUCT.md)。

## 如何贡献

### 报告 Bug

1. 检查 [已有 issues](https://github.com/yourusername/claude-code-telegram/issues)
2. 创建新 issue，包含：
   - 清晰的标题和描述
   - 复现步骤
   - 期望行为 vs 实际行为
   - 系统信息

### 功能建议

1. 检查 [已有提案](https://github.com/yourusername/claude-code-telegram/discussions)
2. 发起讨论，包含：
   - 使用场景描述
   - 建议的实现方案
   - 替代方案

### 代码贡献

#### 环境搭建

1. Fork 仓库
2. 克隆你的 fork：
   ```bash
   git clone https://github.com/yourusername/claude-code-telegram.git
   cd claude-code-telegram
   ```

3. 创建虚拟环境：
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   venv\Scripts\activate     # Windows
   ```

4. 安装依赖：
   ```bash
   pip install -r requirements/dev.txt
   pre-commit install
   ```

#### 开发流程

1. 创建功能分支：
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. 按照编码规范进行修改

3. 运行测试：
   ```bash
   pytest
   make lint
   ```

4. 提交描述性 commit：
   ```bash
   git commit -m "feat: add amazing feature"
   ```

5. 推送并创建 PR：
   ```bash
   git push origin feature/your-feature-name
   ```

### 编码规范

- 遵循 PEP 8
- 使用类型标注
- 所有函数添加 docstring
- 行长度不超过 88 字符
- 使用 black 格式化
- 新功能必须编写测试

### 提交消息

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

- `feat:` 新功能
- `fix:` Bug 修复
- `docs:` 文档变更
- `style:` 格式调整
- `refactor:` 代码重构
- `test:` 测试变更
- `chore:` 维护任务

### 测试

- 为新代码编写单元测试
- 确保所有测试通过
- 保持覆盖率 >80%
- 功能需包含集成测试

### 文档

- 必要时更新 README
- 为新函数添加 docstring
- 文档中包含示例
- 更新配置文档

## Pull Request 流程

1. 更新文档
2. 为变更添加测试
3. 确保 CI 通过
4. 请求维护者审查
5. 处理审查反馈
6. 如被要求则合并提交

## 发布流程

1. 维护者按语义化版本进行版本管理
2. Changelog 自动更新
3. Docker 镜像构建并推送
4. 创建 GitHub Release

## 获取帮助

- [Discussions](https://github.com/yourusername/claude-code-telegram/discussions)

感谢你的贡献！
```

### SECURITY.md
```markdown
# 安全策略

## 支持的版本

| 版本 | 是否支持 |
| ------- | ------------------ |
| 1.x.x   | :white_check_mark: |
| < 1.0   | :x:                |

## 报告漏洞

我们非常重视安全问题。如果你发现了漏洞，请遵循负责任的披露流程：

### 1. **不要**创建公开 issue

### 2. 发送邮件至 security@example.com，包含：
- 漏洞描述
- 复现步骤
- 潜在影响
- 修复建议（如有）

### 3. 等待回复
- 我们会在 48 小时内确认收到
- 我们会提供修复时间预估
- 修复完成后会通知你

## 安全措施

### 认证
- Telegram 用户 ID 白名单
- 可选的令牌认证
- 带过期机制的会话管理

### 授权
- 目录遍历防护
- 命令注入防护
- 文件类型验证

### 限流
- 按用户请求限制
- 基于费用的限制
- 并发会话限制

### 数据保护
- 本地 SQLite 数据库
- 日志中不含敏感数据
- 安全的令牌存储

### 基础设施
- 以非 root 用户运行
- 强制资源限制
- 定期更新依赖

## 用户最佳实践

1. **保护 bot token**
   - 不要提交到版本控制
   - 使用环境变量
   - 定期轮换

2. **限制已批准目录**
   - 使用最小必要访问权限
   - 避免系统目录
   - 定期审计权限

3. **监控使用情况**
   - 检查审计日志
   - 监控费用
   - 审查用户活动

4. **保持更新**
   - 应用安全更新
   - 关注公告
   - 更新依赖

## 安全清单

- [ ] Bot token 安全存储
- [ ] 已批准目录范围受限
- [ ] 用户白名单已配置
- [ ] 限流已启用
- [ ] 日志不含密钥
- [ ] 以非 root 用户运行
- [ ] 依赖已更新
- [ ] 备份已配置

## 联系方式

安全问题: security@example.com
PGP 密钥: [下载](https://example.com/pgp-key.asc)
```

## 部署脚本

### 健康检查脚本
```python
# scripts/check_health.py
"""
监控用健康检查
"""

import sys
import asyncio
from pathlib import Path

async def check_health():
    """执行健康检查"""
    checks = {
        'database': check_database(),
        'claude': check_claude(),
        'telegram': check_telegram(),
        'storage': check_storage()
    }

    results = {}
    for name, check in checks.items():
        try:
            results[name] = await check
        except Exception as e:
            results[name] = False
            print(f"{name} 健康检查失败: {e}")

    # 整体健康状态
    healthy = all(results.values())

    if healthy:
        print("所有健康检查通过")
        sys.exit(0)
    else:
        print(f"健康检查失败: {results}")
        sys.exit(1)

async def check_database():
    """检查数据库连接"""
    from src.storage.database import DatabaseManager

    db = DatabaseManager(os.getenv('DATABASE_URL'))
    async with db.get_connection() as conn:
        await conn.execute("SELECT 1")
    return True

async def check_claude():
    """检查 Claude Code 可用性"""
    import subprocess

    result = subprocess.run(['claude', '--version'], capture_output=True)
    return result.returncode == 0

async def check_telegram():
    """检查 Telegram bot token"""
    import aiohttp

    token = os.getenv('TELEGRAM_BOT_TOKEN')
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://api.telegram.org/bot{token}/getMe') as resp:
            return resp.status == 200

async def check_storage():
    """检查存储可用性"""
    data_dir = Path('/data')
    return data_dir.exists() and data_dir.is_dir() and os.access(data_dir, os.W_OK)

if __name__ == '__main__':
    asyncio.run(check_health())
```

### 部署脚本
```bash
#!/bin/bash
# scripts/deploy.sh

set -e

echo "正在部署 Claude Code Telegram Bot"

# 加载环境变量
source .env

# 构建 Docker 镜像
echo "正在构建 Docker 镜像..."
docker build -t claude-code-bot:latest -f docker/Dockerfile .

# 停止现有容器
echo "正在停止现有容器..."
docker stop claude-code-bot || true
docker rm claude-code-bot || true

# 启动新容器
echo "正在启动新容器..."
docker run -d \
  --name claude-code-bot \
  --restart unless-stopped \
  --env-file .env \
  -v claude-bot-data:/data \
  -v "${APPROVED_DIRECTORY}:/projects:ro" \
  claude-code-bot:latest

# 等待健康检查
echo "正在等待健康检查..."
sleep 10

# 执行健康检查
if docker exec claude-code-bot python scripts/check_health.py; then
    echo "部署成功！"
else
    echo "健康检查失败！"
    docker logs claude-code-bot
    exit 1
fi

# 清理旧镜像
echo "正在清理旧镜像..."
docker image prune -f

echo "部署完成！"
```

## 监控配置

### Prometheus 配置
```yaml
# docker/prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'claude-code-bot'
    static_configs:
      - targets: ['bot:8080']
    metrics_path: '/metrics'
```

### Grafana 仪表盘
```json
{
  "dashboard": {
    "title": "Claude Code Bot 指标",
    "panels": [
      {
        "title": "活跃用户",
        "targets": [
          {
            "expr": "bot_active_users"
          }
        ]
      },
      {
        "title": "消息速率",
        "targets": [
          {
            "expr": "rate(bot_messages_total[5m])"
          }
        ]
      },
      {
        "title": "Claude 费用",
        "targets": [
          {
            "expr": "bot_claude_cost_total"
          }
        ]
      },
      {
        "title": "错误率",
        "targets": [
          {
            "expr": "rate(bot_errors_total[5m])"
          }
        ]
      }
    ]
  }
}
```

## 发布流程

### GitHub Actions 发布
```yaml
# .github/workflows/release.yml
name: Release

on:
  push:
    tags:
      - 'v*'

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3

    - name: 构建 Docker 镜像
      run: |
        docker build -t claude-code-bot:${{ github.ref_name }} .
        docker tag claude-code-bot:${{ github.ref_name }} claude-code-bot:latest

    - name: 登录镜像仓库
      uses: docker/login-action@v2
      with:
        username: ${{ secrets.DOCKER_USERNAME }}
        password: ${{ secrets.DOCKER_PASSWORD }}

    - name: 推送镜像
      run: |
        docker push claude-code-bot:${{ github.ref_name }}
        docker push claude-code-bot:latest

    - name: 创建 Release
      uses: softprops/action-gh-release@v1
      with:
        files: |
          README.md
          LICENSE
        generate_release_notes: true
```

## 成功标准

- [ ] Docker 镜像构建成功
- [ ] 健康检查通过
- [ ] 文档完整清晰
- [ ] 所有部署脚本已测试
- [ ] CI/CD 流水线正常运行
- [ ] 监控仪表盘已配置
- [ ] 安全文档完整
- [ ] 贡献指南清晰
- [ ] 发布流程自动化
- [ ] 提供示例配置
- [ ] 故障排查指南全面
- [ ] 开源清单完整
