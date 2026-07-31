# 我的简历 - Resume Editor

一款**轻量、便捷、多功能**的在线简历编辑器。数据存储在浏览器 localStorage，后端仅负责 PDF 导出，支持 Docker 一键云部署。
![输入图片说明](%E7%AE%80%E5%8E%86%E7%BD%91%E7%AB%99.png)

## 🚀 快速开始

### Docker 部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/peng6661/jianli.git
cd jianli

# 2. 一键启动
docker-compose up -d

# 3. 访问
# http://localhost  （前端页面）
# http://localhost/api/health  （后端健康检查）
```

> **前提条件**：服务器已安装 Docker 和 Docker Compose。
>
> 首次构建会自动安装 Playwright Chromium 浏览器和中文字体包（fonts-noto-cjk），确保 PDF 导出效果与本地一致。

### 手动部署（开发/调试）

```bash
# 1. 克隆项目
git clone https://github.com/peng6661/jianli.git
cd jianli

# 2. 安装后端依赖
pip install -r back/requirements.txt
playwright install chromium

# 3. 启动后端服务（端口 8080）
cd back
python main.py

# 4. 用任意静态文件服务器托管前端（端口 3000）
#    例如: python -m http.server 3000
#    或:   npx serve .
```

## 🏗️ 技术栈

| 层次 | 技术 |
|------|------|
| 前端 | 原生 HTML + CSS + JavaScript（零框架依赖） |
| Markdown 渲染 | [marked.js](https://cdn.jsdelivr.net/npm/marked/marked.min.js) |
| 后端 | Python FastAPI + uvicorn（仅 PDF 导出，无状态） |
| PDF 生成 | Playwright Chromium（`page.pdf()`，跨平台） |
| 数据存储 | 浏览器 localStorage（数据不上传服务器） |
| 离线支持 | Service Worker（预缓存 + 运行时缓存） |
| 静态托管 | Nginx（Docker 内） |
| 部署 | Docker + docker-compose |

## 📁 项目结构

```
├── index.html              # 首页 — 简历列表（卡片网格展示 + 增删改查）
├── resume-editor.html      # ⭐ 核心页面 — 简历编辑器（6000+ 行，含模板引擎）
├── about.html              # 更多资料 / 关于页面
├── styles.css              # 全局样式（CSS 变量设计系统）
├── sw.js                   # Service Worker 离线缓存
├── logo.ico                # 网站图标
├── logo.png                # 项目 Logo
├── back/
│   ├── main.py             # FastAPI PDF 导出服务（Playwright 渲染）
│   └── requirements.txt    # Python 依赖（fastapi / uvicorn / pydantic / playwright）
├── Dockerfile              # Docker 镜像构建（Python + Playwright + Nginx + 中文字体）
├── docker-compose.yml      # 容器编排配置
├── docker-entrypoint.sh    # 容器启动脚本（启动 FastAPI + Nginx）
└── nginx.conf              # Nginx 配置（静态托管 + /api/ 反向代理）
```

## 📐 架构设计

```
用户浏览器 (localStorage 存储所有简历数据)
    ↕ HTTPS
Nginx (:80)
    ├── / → 静态文件 (HTML/CSS/JS/图片)
    └── /api/ → 反向代理
                    ↕
              FastAPI (:8080)
                    ↕
              Playwright Chromium (PDF 渲染)
```

**设计原则**：后端完全无状态——不存数据、不认用户、不留痕迹。数据全在浏览器里。

## 📄 使用须知

本工具仅供个人学习与交流使用，**严禁用于任何商业用途**。未经作者许可，不得将本工具或其衍生版本用于商业目的。

本项目基于 **Apache License 2.0** 开源协议。

## 📱 获取更多资料

关注微信公众号或添加作者微信，免费获取简历模板和编程学习资料。

> 关注公众号 阿鹏随笔录 后回复「简历」即可领取
