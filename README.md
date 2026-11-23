# 出行证件补件包的浏览器采集与受理关联

本项目包含两部分：

1. **隔离的本地演练门户**（`source/`）：家庭资料入口、原件页面下载与独立受理台，
   所有姓名、联系方式、证件、费用和机构均为演练数据，不连接实际业务系统。
2. **证件补件采集工具**（`collector/`）：资料员使用的 Python 产品，只经
   Playwright Chromium 的页面操作在门户与受理台之间领取原件、组卷送审、
   核对收件号并跟踪逐项结论与补件。

## 环境和运行

使用 Python 3.11，依赖固定在 `requirements.txt`：

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install --with-deps chromium
```

启动演练门户（仅监听回环地址；重启后回到阶段 0、模拟受理记录清空）：

```bash
python3 -m source.app --port 8765
# http://127.0.0.1:8765/
```

## 采集工具用法

工具的运行状态保存在独立目录（默认 `.collector-state/`），与代码隔离；
下载的原件属于不可信数据，统一落在 `state/downloads/<旅客>/<材料号>/` 下，
同名不同字节的版本各自分目录保存，不会互相覆盖。

```bash
# 扫描门户当前旅客与出行批次契约
python3 -m collector scan
# 按旅客领取当前批次全部所需原件（阻断原因逐材料留痕）
python3 -m collector collect P-201
# 组卷并向受理台送审（已接受材料不重交；--lose-response 演练先收件后 503）
python3 -m collector submit P-201
# 用受理台历史页核对未确认送审，绝不因超时建立第二件
python3 -m collector reconcile
# 查询各收件号的逐项受理结论
python3 -m collector poll
# 针对某收件号的某项补件要求提交指定原件（保留 receipt 归属）
python3 -m collector supplement intake-1 passport P-201
# 按旅客/批次查看缺什么、为何暂不能交、已由谁受理
python3 -m collector status
# 扫描→领取→送审→核对→查结论 一次跑完
python3 -m collector run
```

常用参数：`--base-url`、`--state-dir`、`--headed`（显示浏览器窗口）、
`--verbose`；`status`/`poll` 支持 `--traveler` 与 `--trip` 过滤。

## 业务规则落地

* **领取权限属于旅客和用途，不属于家庭入口**：能看到姓名不代表能领取原件。
  过期委托（`delegation_expired`）、用途不符（`purpose_denied`）、
  旅客不在文件范围（`traveler_denied`）分别记录为不同待办原因。
* **按（旅客 × 出行批次 × 材料类型）归属**：转团后旧批次记录保留可查，
  但新批次必须重新领取授权与建立受理归属，旧收件结论不会套到新身份组合。
* **共用文件按内容 SHA256 只存一份**，但每名旅客的下载授权、送审包、
  受理结论与补件归属独立保存。
* **送审先拿收件号、后有逐项结论**：网页 503/超时只把送审包置为
  `pending_reconcile`，下次从受理台历史核对；确定性送审引用保证重复执行
  不会产生第二份待审包。
* **补件针对收件号里的具体材料要求**：只能送该旅客当前批次要求、
  且经其授权成功领取的材料号；与被审原件字节相同会被拒绝；
  一项被驳回不重交其他已接受材料。
* **只走 Chromium**：不引入任何直接 HTTP 客户端，不读取 `fixtures/` 与
  `source/` 实现，不手工在地址栏拼装下载地址，所需契约只从已展示 DOM 读取。
* **不修改来源记录、不代替受理员下结论**：错误材料留在本地待处理，
  接受/驳回/补件结论全部以受理台页面为准。
* **证件号遮蔽**：普通列表与日志中的 4 位以上数字串仅保留后三位；
  原件内容从不进入日志或终端输出。

## 检查

```bash
python3 -m compileall -q source collector tests
python3 -m unittest discover -s tests -v
```

检查实际启动 Chromium 完成页面下载、表单上传、收件核对、补件与中断恢复，
不连接外部站点。`tests/test_source.py` 验证演练门户自身契约；
`tests/test_collector.py` 覆盖采集产品的可重复端到端场景（含 503 恢复、
换版补件、转团隔离、共用文件复用、状态持久化与遮蔽）。

## 目录

```
collector/
  config.py    运行配置与本地目录布局
  masking.py   证件号遮蔽与日志过滤器
  store.py     SQLite 持久化（旅客批次、授权、原件、送审包、结论、补件）
  browser.py   Playwright Chromium 会话（唯一的门户交互通道）
  service.py   业务编排：扫描/领取/送审/核对/补件/状态
  cli.py       资料员命令行与状态渲染
docs/collector-architecture.md  状态模型与命令语义
```
