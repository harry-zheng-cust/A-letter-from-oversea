# Docker 部署

## 1. 配置环境变量

复制 `.env.example` 为 `.env`，并修改：

```env
DASHSCOPE_API_KEY=你的百炼APIKey
HOST=0.0.0.0
PORT=8000
PUBLIC_BASE_URL=http://你的公网IP:8000
QIAOPI_USERNAME=admin
QIAOPI_PASSWORD=改成一个强密码
QIAOPI_SESSION_SECRET=改成一段随机字符串
QIAOPI_DATA_KEY=改成一段随机的加密密钥
```

如果你有域名和 HTTPS，改成：

```env
PUBLIC_BASE_URL=https://你的域名
```

`PUBLIC_BASE_URL` 会影响“生成分享”返回的链接。部署在云服务器时必须配置成外网能访问的地址。

`QIAOPI_USERNAME` 和 `QIAOPI_PASSWORD` 用于保护侨批生成器首页和生成接口；已经生成的分享链接 `/s/{id}` 不需要登录，若生成时设置了分享密码则需要输入密码查看。

`QIAOPI_USERNAME` 同时是后台管理员账号。管理员登录后可以访问 `/admin` 查看注册用户、百炼正文点击次数、生成分享次数和每日总分享链接次数。

普通注册用户每天最多点击“百炼生成正文”15 次，超过后接口会返回提示，前端按钮会显示今日剩余额度。

`QIAOPI_DATA_KEY` 用于加密 `data/letters.json` 中的侨批数据。建议固定保存，不要频繁更换，否则旧数据无法解密。

生成分享链接时可以额外设置分享密码。设置后，访问 `/s/{id}` 会先要求输入密码，验证通过后才显示正文。

## 2. 放置音乐文件

分享页背景音乐固定读取：

```text
static/music/yuexiazhucha.mp3
```

把“月下煮茶”的 mp3 放到这个位置。

## 3. 启动

```bash
docker compose up -d --build
```

访问：

```text
http://你的公网IP:8000
```

## 4. 云服务器安全组

在云服务器控制台放行 TCP `8000` 端口。如果使用 Nginx 反向代理到 80/443，则放行 80/443，并把 `PUBLIC_BASE_URL` 配成域名。

## 5. 数据持久化

`docker-compose.yml` 已挂载：

```text
./data:/app/data
./static/music:/app/static/music
```

生成的分享侨批会保存在 `data/letters.json`，容器重启不会丢。
