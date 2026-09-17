# 嘟嘟脸与蜡笔板

[ff14-ddl-qbot 主体](https://github.com/lchlzk/ff14-ddl-qbot) 的独立插件。包含嘟嘟脸查询指令与**完整蜡笔板网页**，不是只有聊天命令入口。

## 包含什么

- 角色资料查询、国服/韩服名称对照、图片卡片、随机角色与兑换码。
- 蜡笔板三层百分比节点、批量点亮/撤销、金币和金蜡笔统计、Soshage JSON 导入导出。
- 桌面与手机网页，角色立绘、搜索、拥有状态及加点状态筛选。
- 独立角色点亮板：统一查看所有可用角色的拥有状态，支持别名搜索、已拥有/未拥有筛选和点亮全部；添加拥有不会增加节点，取消拥有会先确认再清除该角色的节点。
- 蜡笔板用户账号、群聊回执绑定、绑定新机器人和跨机器人共享进度。
- 第三方只读 API、用户令牌管理及公开网页 API 文档。
- Soshage 与 Crayon-note 资料源，每天北京时间 18:00 自动刷新目录。
- Soshage 明确标为不可用的角色不会显示在蜡笔板、参与统计或被批量点亮；Crayon-note 补充资料不会覆盖该状态。已有拥有和加点记录仍保留，资料源开放角色后会自动恢复显示。

## 安装

先安装并配置新版机器人主体，再在**同一个 Python 环境**安装：

```bash
python -m pip install --upgrade 'git+https://github.com/lchlzk/ff14-ddl-qbot-plugin-trickcal.git'
```

重启机器人后自动加载 `plugins.trickcal` 并注册网页和接口，无需单独复制网页或运行前端构建。主体必须支持插件的 `install_web(app, store)` 挂载入口；0.2.0 的完整拆分需要同步更新主体，不能只替换旧版主体中的命令文件。标准 Docker 部署请更新主体及其插件版本锁定文件后重新构建镜像。

主体 `.env` 设置示例：

```dotenv
TRICKCAL_WEB_PUBLIC_URL=https://你的域名/tr-board/
TRICKCAL_WEB_SECURE_COOKIE=true
```

通过 Nginx 等反向代理将请求转发至主体服务：

| 地址 | 用途 |
| --- | --- |
| `/tr-board/` | 蜡笔板网页与用户登录 |
| `/tr-board/api-docs` | 无需登录的 API 文档 |
| `/tr-board/api/*` | 网页账号与蜡笔板操作接口 |
| `/api/v1/tr-board/catalog` | 第三方只读角色和节点目录 |
| `/api/v1/tr-board/board` | 第三方只读个人进度 |

首次使用，在群里发送 `/tr 蜡笔板 网页`，按网页提示完成回执确认、创建账号；之后可使用账号密码登录。在已登录网页点击“绑定新机器人”，再到新群发送生成的绑定命令，即可共用原有蜡笔板。

仅本地 HTTP 测试时可以使用 `http://127.0.0.1:8080/tr-board/` 并将安全 Cookie 设置为 `false`；公网务必使用 HTTPS。

## 源码位置

```text
src/plugins/trickcal.py            聊天命令入口
src/qbot_trickcal/
  runtime.py                      网页挂载和定时更新
  schema.py                       蜡笔板数据库表初始化
  trickcal.py                     角色资料查询
  trickcal_media.py               角色卡片绘制
  trickcal_role_aliases.json       可编辑角色名称对照
  trickcal_board.py               节点管理、统计、数据源
  trickcal_web.py                 账号、绑定、网页和 API 路由
  web/                            完整 HTML / CSS / JavaScript 与 API 文档
```

网页和名称对照表均作为安装包资源发布。用户记录继续保存在主体的 `BOT_DATA_DIR`（默认 `data`）下，数据库表名和缓存位置不变；升级不需要重新绑定、重新导入或迁移数据库。卸载插件不会删除数据。插件只依赖主体公共服务，不要求安装 FF14 或 B站插件，也不宣称可直接用于任意 NoneBot 项目。

[详细指令及 API 说明](TRICKCAL.md)

许可证：AGPL-3.0-only。游戏和第三方素材的权利归原权利人所有。
