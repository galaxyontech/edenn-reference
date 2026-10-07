# Edenn 控制台

用户端(自助注册 / 余额 / 用量 / API key)与管理端(全部账户、key、用量、流水)共用一份代码。

## 本地开发

```bash
cd Frontend/console
npm install
cp .env.example .env.local     # 只在没有后端时才需要填(见下)
npm run dev                    # http://localhost:3100/console/
```

## 无后端的 UI 开发(mock 模式)

`.env.local` 里 `NEXT_PUBLIC_MOCK=1`,控制台就整套跑在浏览器里:不需要 API,
不需要 Firebase 项目,不需要真的收短信。右下角有一个"MOCK DATA"角标和场景
切换器 —— **看得见假数据就一定看得见那个角标**。

只替换两处接缝(`src/lib/api.js` 的 `request()` 和 `src/lib/firebase.js` 的六个
认证函数),组件一行没改,所以你看到的就是真实渲染路径,不是另一套会漂移的
storybook。

场景用 `?mock=<id>` 切换,也可以用角标里的菜单:

| id | 屏幕 |
|---|---|
| `active` | 有余额、四个月用量、四把 key(默认) |
| `low` | 余额告警 |
| `broke` | 余额为 0,提交返回 402 |
| `new` | 零作业零 key —— 首次使用的三步引导 |
| `signup` | 手机已验证但账户未开通 |
| `admin` | 在 active 之上多一个管理端 |
| `heavy` | 超过 5,000 行,触发两处截断提示 |
| `degraded` | 三个请求全挂,看错误态 |
| `signed-out` | 手机登录流程;任意六位验证码都通过 |

登录流程里几个刻意留的入口:尾号 `0000` 的号码触发号码非法,尾号 `9999`
触发"发送很慢"的提示,验证码 `000000` 是验证码错误。

创建 / 改名 / 吊销 key 都是真的会改内存里的状态,刷新页面复位。每个请求有
260ms 延迟 —— 骨架屏是这个项目里最容易写坏又最看不见的东西,同步返回的 mock
等于把它们全藏起来。

**不会进生产包。** `NEXT_PUBLIC_MOCK` 是编译期常量,不等于 `1` 时 webpack 连
`src/mock/` 的 import 都不会跟进去,导出的产物里没有假数据、没有角标。

## 当前部署

| | |
|---|---|
| 控制台 | https://console-app.worker.example.invalid/console/ |
| 容器应用 | `console-app`(资源组 `rg-edenn-newapp-japaneast`,环境 `cae-edenn-newapp-jp`)|
| 表存储命名空间 | `newapp` —— 与 `staging-app` **共用账户与 key**,所以管理端看得到现有数据 |

`staging-app` 是测试节点,有人在用,**不要往它上面部署控制台镜像**。控制台自己有容器。

## Firebase 配置从哪来

**运行时从后端取** —— 页面加载时先请求 `GET /api/v1/console/config`,后端把
Firebase 的五个公开值下发下来。所以:

- **改 Firebase 项目不需要重建前端**,改后端环境变量刷新即可。
- 那五个值只存在后端环境变量一处,和后端验签用的 `FIREBASE_PROJECT_ID` 天然一致。
  两处配置漂移的后果是"token 签给项目 A、后端拿项目 B 验" —— 一个两边日志都不会
  解释的 401。
- `.env.local` 里的 `NEXT_PUBLIC_FIREBASE_*` **只是本地无后端开发时的兜底**。

## 构建与部署

**V2 · Azure 同源(默认,唯一在线的形态)**
镜像里有一个 `console-builder` 阶段会执行 `npm ci && npm run build`,产物 COPY 到
`/app/Frontend/console/out`,后端启动时挂到 `/console`。**不需要本地先构建**,
也不需要任何 build-arg。

```bash
az acr build --registry exampleregistry --image edenn-api:<短 commit> \
  --file Dockerfile --platform linux/amd64 .
```

**V1 · Firebase Hosting(代码路径保留,本轮不上线)**

```bash
npm run build && npm run pack:hosting    # → deploy/console + firebase.json
firebase deploy --only hosting --project <project-id>
```

## 需要在 Firebase 控制台做的事

1. Authentication → Sign-in method → **启用 Phone**。
2. **升级到 Blaze**:免费档发不出验证短信。
3. Authentication → Settings → **Authorized domains** 加上容器域名,否则 reCAPTCHA 直接拒绝:
   - `console-app.worker.example.invalid`
   - (只有上线 V1 时才需要再加 `<project-id>.web.app`)

## 后端需要的环境变量

| 变量 | 说明 |
|---|---|
| `FIREBASE_PROJECT_ID` | 不配 = 验证注册和会话登录都不提供(503),控制台显示"未配置" |
| `FIREBASE_WEB_API_KEY` | 下发给控制台的 `apiKey` |
| `FIREBASE_AUTH_DOMAIN` | 可留空,默认 `<project-id>.firebaseapp.com` |
| `FIREBASE_APP_ID` / `FIREBASE_MESSAGING_SENDER_ID` | 下发给控制台 |
| `ADMIN_PHONE_NUMBERS` | 逗号分隔;命中的手机号登录后是管理员 |
| `ADMIN_FIREBASE_UIDS` | 逗号分隔;比手机号更强的锚点,建议拿到 UID 后换成它 |

## 几个刻意的选择

- **不用 Firestore。** 每个数字都来自 Azure API,账本只有一份。
- **Firebase 配置不烘焙进产物。** 见上,一份配置胜过两份会漂移的配置。
- **admin secret 不在这个包里。** 管理员身份由服务端白名单判定,管理端和用户端发出的请求
  完全一样,由后端决定看到什么。
- **页面本身零外部请求。** Firebase SDK 是打包进来的,没有 CDN、没有外链字体。只有点"发送
  验证码"时才会联系 Google —— 那一步包了 8 秒超时,超时后切到人工开通的文案,而不是转圈。
- **登录后先探 `/account/balance`,不调 `/signup/verified`。** 后者每次成功都会增发一把 key,
  拿它当"确认登录"会让每次访问都多一把 key。
