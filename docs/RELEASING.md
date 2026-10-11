# 发布清单

> npm 侧的**症状对照与省上下文技巧**在 [NPM-PUBLISH.md](NPM-PUBLISH.md)：
> 404 / 409 / ETARGET 到底是失败还是在传播，查那一份，不用通读本文。

npm 包与 GitHub 分开发布，中间隔多久都不奇怪——`1.5.0` 与主线之间曾拉开 128 个提交、
两个多月，直接导致用户照着 README 敲 `init --path` 却报 `unknown option`。

按下面顺序走，每一步都有对应的**失败长什么样**。

## 1. 定版本号

按语义化版本，不是按「改了多少行」：

| 改动性质 | 升哪一位 | 例 |
| --- | --- | --- |
| 新功能、新命令、新参数 | **minor** | 1.5.0 → 1.6.0 |
| 只修 bug、只改文档 | patch | 1.6.0 → 1.6.1 |
| 破坏兼容 | major | 1.6.0 → 2.0.0 |

已备好但**从未发布**的版本号可以并进下一个：`1.5.1` 准备过却没发，
它的修复就直接并进了 `1.6.0`。

## 2. 整理 CHANGELOG

- 把 `## Unreleased` 改名为 `## <新版本号>`
- 若上一版已写好但没发布，把它的条目并入，删掉那个空段
- 通用说明（如「npm 与 GitHub 分开发布」）移到文件顶部，别留在版本段里

## 3. 构建与测试

```powershell
npm run build
npm test
py scripts/check_readme_commands.py          # README 里写的命令必须真的存在
```

最后那条盯的是**用户先发现的那一类漂移**：`1.5.0` 与主线拉开 128 个提交时，用户照着 README 敲
`init --path` 得到 `unknown option`。它逐级问 CLI 的 `--help`（不复刻命令表），只扫 README 的
围栏代码块，`--self-test` 是它自己的正对照。CI 的 `node` job 每次推送都会跑这两步。

## 4. 打包检查（**不能跳过**）

```powershell
npm pack --dry-run
```

本地常有 `output/`（几 GB 产出）、`models/`（几 GB 模型）、`_env.json`（含密钥）
这类东西。`package.json` 的 `files` 是白名单，理论上挡住——但**白名单也可能写漏**，
所以逐项确认：

```powershell
npm pack --dry-run 2>&1 | grep -E "npm notice [0-9]" > /tmp/list.txt
grep -E "^output/|^models/|node_modules|\.env|\.db$|\.dat$|secret|credential" /tmp/list.txt
```

**这条命令必须没有输出。** 有输出就停下来查。

同时确认新增文件确实**在**包里（白名单是按目录配的，容易漏）：

```powershell
grep -E "新文件名" /tmp/list.txt
```

这后半句（"**该有的在不在**"）现在有自动判据：`test/pack-contents.test.ts` 跑一次
`npm pack --dry-run --json`，断言 `scripts/` 下每个跟踪文件（`.ts` 开发入口除外）与两份 README 引用的
本地图片都在包里。它防的故障 2026-10-11 在**真实发布物**上量到过：registry 上 `weflow-cli@1.9.0`
的 501 个文件里同时缺 `scripts/data/wechat_emoji.json` 与 `logo.png` —— 前者让 npm 用户导出的
聊天记录里一批表情码原样显示成 `[名字]`，后者让包页面挂着一张裂图。这条测试跟着 `npm test` 跑，
CI 的 `node` job 也在跑；上面那条人工命令留着，用来核对本次**新加**的文件名。

想验到"**装出来真的能跑**"（最强的一道，代价是多装一次依赖）：把包打进临时目录、再装进一个空
prefix，然后从**装好的那一份**里问版本。它盯的是白名单之外的另一半 —— 清单对、运行时不成立的情况。

```powershell
npm pack --pack-destination $env:TEMP
mkdir $env:TEMP\wf-smoke ; cd $env:TEMP\wf-smoke     # 空目录
npm install --no-save --no-audit --no-fund "$env:TEMP\weflow-cli-<版本>.tgz"
node node_modules\weflow-cli\cli.cjs --version
node node_modules\weflow-cli\cli.cjs --help
```

2026-10-11 拿 1.10.0 跑过一遍：`ALL_NAMES = 222`（不是只有原图的 118 —— 那正是上一版缺
`scripts/data/wechat_emoji.json` 时的值）、`[乒乓]`/`[冷汗]`/`[开红包]` 都渲染成对应表情、
`--version` 答 `1.10.0`。

## 5. 发布

需要 npm 凭据。`ribiaosu` 账号开了 2FA，所以两条路：

**凭据（推荐，一次配置长期可用）** — 用**带 `Bypass 2FA` 的 Granular Access Token**，
在 https://www.npmjs.com/settings/ribiaosu/tokens 生成时**必须勾选那个框**。
漏勾的 token 会在发布时报 `403 ... bypass 2fa enabled is required`。

| 现象 | 原因 |
| --- | --- |
| `403 ... two-factor authentication ... required` | token 没勾 Bypass 2FA，或没用 OTP |
| `ENEEDAUTH` | 没登录 / 没配 token |

**验证 token 是否可用**（`bypass_2fa` 必须是 `true`）：

```powershell
curl -s -H "Authorization: Bearer <token>" https://registry.npmjs.org/-/npm/v1/tokens
```

**一次性验证码**：

```powershell
npm publish --otp=<6位码>
```

## 6. 触发国内镜像同步（**新加的必要步骤**）

国内用户大多用 `registry.npmmirror.com`（淘宝源），它对新版本有同步延迟。
不同步的话，发布后几个小时内会有人来提「版本不存在」的 issue——
`weflow-cli@1.6.0` 就这样被报过一次（Issue #8）。

```powershell
curl -X PUT https://registry.npmmirror.com/-/package/weflow-cli/syncs
```

返回 `{"ok":true,...,"state":"waiting"}` 即已受理。**约 5 分钟后**生效。

## 7. 验证两个源都是新版本

```powershell
npm view weflow-cli version --registry=https://registry.npmjs.org
npm view weflow-cli version --registry=https://registry.npmmirror.com
```

两个都要是新版本号才对外宣告。官方源发布后自身也有几分钟 CDN 传播延迟——
期间 `npm view` 可能显示旧版本、tarball 可能 404，**这是在传播，不是失败**。
用这个判断是否真的发出去了：

```powershell
npm publish    # 再发一次
```

报 `409 Conflict - Cannot publish over previously staged version` → **已经发布成功**。

但 409 只说明**版本槽位已被占**，不等于 registry 已经能查到它。发布后 npm 要先扫描再让版本可读，
**官方不承诺上限**：常见 5 分钟，实测出现过 14、16、25、55 分钟。这个窗口里的表现是：

- `npm view weflow-cli version` 还是旧版本号
- tarball 直链仍然 404
- 再发一次仍然是 409 —— 所以 409 **区分不了**「还在传播」和「已经好了」

窗口期内只做一件事：等 + 轮询，**不要改版本号重发**。想确认不是卡在人工审批：

```powershell
npx --yes npm@11.17.0 stage list weflow-cli --json   # 返回 [] 即无待审批版本，属传播中
```

（`npm stage` 需要 npm ≥ 11.15；本机装的是 11.6.2，直接跑会报 `Unknown command: "stage"`，
所以用 npx 临时拉一个高版本。`npm@11.17.0` 是在本机 Node 24.11.1 上验证过能用的。）

## 8. 兜底：告诉用户怎么绕开镜像

```powershell
npm config get registry                                            # 看用的哪个源
npm view weflow-cli version --registry=https://registry.npmjs.org  # 用官方源查
```

两个源结果不一致 → 是镜像同步问题，不是包的问题：
`npm install -g weflow-cli@<版本> --registry=https://registry.npmjs.org` 立刻可用。

## 建 GitHub Release 时的两个坑（2026-10-02 补 1.8.0/1.8.1/1.8.2 时实测）

- **正文上限 125,000 字符。** 1.8.0 那段 CHANGELOG 是 140,154 字符，直接贴会被 API 拒：
  `HTTP 422 ... body is too long (maximum is 125000 characters)`。处理方式是**按 `### ` 小节边界
  机械截断**（放到装不下为止），末尾补一句"完整条目在 `CHANGELOG.md`"。**不要凭"哪几段更重要"去挑**：
  那既不可复现，也不是发布这件事该做的判断。
- **补历史的 Release 要显式 `--latest=false`。** `gh release create` 默认会把新页面标成 Latest ——
  给 1.8.x 补账时会把 1.9.0 从 Latest 上挤下去。补完用 `gh release list` 确认 Latest 还是最新的那个。
- **发布前先确认工作区干净**（`git status` 为空，HEAD 就是你打算打 tag 的那次提交）。
  理由不是形式主义，是实测出来的：2026-10-02 给 1.8.0/1.8.1/1.8.2 补 tag 时做了逐文件溯源
  （拿 registry 上的 tarball 与 `git archive <tag>` 比，行尾差异不算），结果是 1.8.1 与 1.8.2 的
  发布源码与 tag 树**一致**，而 **1.8.0 的包里有两个文档文件在 git 里找不到对应状态**：
  `docs/DECISIONS.md` 与更晚的一次提交一致，`README.en.md` 则**与任何提交都不一致**（比 tag 那份
  长约 7,000 字符）。代码部分没问题（逐字节相同），但**那次发布出去的文档再也回不来** ——
  这正是"不把 npm 发布包当作 GitHub 源码的实时镜像；先确认版本和提交"这条规矩要防的事。


## 发布后

- 回到相关 issue 回复「已发布 + 升级方式」
- GitHub Release 与本清单解耦：源码、编译包、npm 三者版本差异要对用户可见

### 回复用户的规矩

报障的人多半不是开发者，也不看长文。**回复写给用户，不是写给同事：**

- **先给出结论和做法**（已修复 / 升级到 x.y.z），技术细节不要写进回复
- **三五句话说完**。根因分析、代码片段、执行顺序论证属于 commit message 和
  本文档，不属于 issue 回复
- 升级类回复直接给命令，别让人自己找
- **发出前先给维护者过目**，确认后再发

反面例子：一条回复里贴了 Python 代码片段和逐步的执行顺序分析，内容全对，
但给用户的感受是「看不懂、太长」。

### 新动态邮件提醒

`scripts/watch_issues.py` 轮询本仓库的 issue 与回复，有非维护者发言时发邮件。
由 Windows 计划任务 `WeFlow Issue Watch` 每 30 分钟调用一次，不依赖任何编辑器
或 Claude 会话处于打开状态。

邮件凭据放在仓库外，不进版本库：

```json
// ~/.weflow-issue-watch.json   （QQ 邮箱需用「授权码」，不是登录密码）
{
  "smtp_user": "1473517806@qq.com",
  "smtp_auth": "<授权码>",
  "mail_to":   "1473517806@qq.com",
  "repo":      "zhuobichen/weflow-cli"
}
```

- 首次运行只记录基线、不发信，避免把历史 issue 一次性全发出去
- 发送失败不推进记录，下次自动重试
- 手动跑一次：`py scripts/watch_issues.py`
- 查看/改频率：`Get-ScheduledTask -TaskName 'WeFlow Issue Watch'`

### 发布后自检

```powershell
npm view weflow-cli version --registry=https://registry.npmjs.org
npm view weflow-cli version --registry=https://registry.npmmirror.com
```

两个源都是新版本，再对外说「已发布」。

然后跑一遍体检，确认这次改动没有把读取路径或某个导出格式弄坏：

```powershell
py scripts/health_check.py
```

退出码 `0` 才算干净。检查项与各自的由来见 [HEALTH-CHECK.md](HEALTH-CHECK.md)。
