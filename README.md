# 一封桥批（A-letter-from-overseas）

参考《给阿嫲的情书》侨批，开发了一个用于生成侨批的 Web 应用。用户可以填写收信人、落款、祝福语和提示词，生成带有旧式书信气质的侨批正文，并以可翻页的视觉形式预览、保存和分享。

## 功能

- 生成侨批正文：支持调用阿里云百炼 DashScope 大模型生成正文；未配置 API Key 时会使用本地兜底文案。
- 手动编辑正文：生成后可继续修改正文、封面、收信人、落款、红包金额和祝福语。
- 侨批预览：使用竖排正文、红线格纸、封面和翻页效果展示。
- 分享链接：保存侨批后生成 `/s/{id}` 分享页，分享页无需登录访问。
- 登录保护：生成器页面和接口可通过账号密码保护，分享页公开访问。
- Docker 部署：提供 `Dockerfile` 和 `docker-compose.yml`。
* 主界面
  
  <img width="2413" height="1358" alt="image" src="https://github.com/user-attachments/assets/7627ac7c-dd2e-4868-91a1-3659ab5951b2" />

* 预览界面
  
<img width="2337" height="1232" alt="image" src="https://github.com/user-attachments/assets/84ba62d3-0e55-43e9-8812-3e7016ee959e" />

* 实际展示界面
  
<img width="376" height="666" alt="image" src="https://github.com/user-attachments/assets/669658c7-5eec-494b-ac6e-415c971972b5" />

## 技术栈

- Python 3.12
- FastAPI
- Jinja2
- Uvicorn
- httpx
- 阿里云百炼 DashScope 兼容 OpenAI Chat Completions 接口

## 目录结构

```text
.
├── main.py                 # FastAPI 应用入口
├── templates/
│   ├── qiaopi.html          # 侨批生成与分享页面
│   └── login.html           # 登录页面
├── static/                  # 静态资源
├── data/
│   └── letters.json         # 已生成侨批数据
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── start.sh
```

## 部署

部署详见[DEPLOY.md](https://github.com/harry-zheng-cust/A-letter-from-overseas/blob/main/DEPLOY.md)文档


## 说明

“侨批”是海外华侨寄回家乡的家书与汇款凭证，承载了跨海谋生、亲情往来和乡土记忆。本项目以数字化方式生成具有侨批视觉风格和书信气质的互动页面，适合用于纪念、展示、祝福和分享。
## 效果体验
[http://43.136.81.27:8000/s/d081d73bb1](http://43.136.81.27:8000/s/d081d73bb1)

## 致谢
* 《给阿嫲的情书》侨批的灵感来源，感谢其对本项目的贡献。
行船入夜，恰江上升明月，江海万里，心中念你，便不觉遥远。
* 58 【阿嫲同款侨批生成器，体验给阿嫲写情书 - 个体狐 | 小红书 - 你的生活兴趣社区】 😆 ywK1Rane6TOaU87 😆 https://www.xiaohongshu.com/discovery/item/6a043022000000000603191d?source=webshare&xhsshare=pc_web&xsec_token=ABTraAuQ-FnZzkzcD1F_F9G2ELaY6eNfQEHrnCLhlA8TQ=&xsec_source=pc_share
