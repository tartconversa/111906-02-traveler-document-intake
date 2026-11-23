# 采集工具状态模型与命令语义

## 1. 交互边界

`collector/browser.py` 是产品中唯一接触门户的模块，全部基于
Playwright Chromium：

| 门户能力 | 产品动作 | 不允许的做法 |
| --- | --- | --- |
| 旅客页契约 | 打开 `/traveler/{id}`，解析 `<pre>` 中展示的 JSON | 读取 `fixtures/source.json` 或 `source/` 源码 |
| 原件下载 | 点击页面下载链接，接收浏览器 download 事件落盘 | urllib/requests 等直接 HTTP、地址栏手工拼 URL |
| 受理送审 | 在 `/intake` 表单填 reference/metadata 并 set_input_files | 绕过 UI 构造 multipart 请求 |
| 逐项结论 | 打开 `/review/{reference}` 解析 receipt/results | 自行推断接受/驳回结论 |
| 补件 | 在同一 review 页补件表单提交 | 把任意最新原件塞进收件号 |

`tests/CollectorCase` 中的 `/__control` 阶段切换只属于测试夹具操作，
产品代码不含任何控制端或 `/__inspect` 调用。静态测试
`test_product_never_calls_http_directly_or_reads_fixtures` 会扫描
`collector/` 源码，禁止直接 HTTP 库与来源目录引用。

## 2. 持久化模型（SQLite）

状态库位于 `<state-dir>/state.db`，所有表以（旅客 × 批次）为主线：

* `travelers`：每次扫描观察到的旅客批次行，转团产生新行而非覆盖旧行；
* `requirements`：该旅客该批次当前要求的材料号（换版后整体替换）；
* `documents`：材料号 → 文件名、SHA256、隔离目录中的存储路径（按内容去重）；
* `downloads`：逐旅客、逐批次、逐材料的领取留痕，`allowed=0` 时保存
  `delegation_expired / purpose_denied / traveler_denied` 等原因；
* `submissions` + `submission_files`：送审包、确定性引用、收件号与状态
  （`pending_reconcile` / `received`）；
* `reviews`：按收件号 × 材料类型保存受理台给出的最新结论；
* `supplements`：补件引用及其 receipt 归属、材料号、SHA256 与状态。

### 确定性引用

* 送审引用 `pkg-<sha12(traveler|trip|sorted(kind:document_id))>`：
  同一身份组合与同一材料集合重复执行只会命中本地已存在的包，不会二次上传。
* 补件引用 `supp-<sha12(receipt|kind|traveler|trip|document_id)>`：
  同一收件号的同一补件要求重复执行同样被拦截。

## 3. 命令语义与异常路径

### collect

1. 委托期限早于门户业务日期：整批次阻断，逐材料写 `delegation_expired`，
   不发起下载；
2. 已有该旅客该批次该类型的成功授权且材料号未变：跳过下载（共用文件复用）；
3. 点击下载未触发 download 事件：解析 403 页面 JSON 的 `error` 字段归类；
4. 下载成功：原件写入 `downloads/<旅客>/<材料号>/<门户文件名>`，
   登记 SHA256 与逐旅客授权行。

### submit

1. 委托书（delegation）只按委托期限校验，不进入受理台文件表单；
2. 已被受理台 `accepted` 的类型跳过；`needs_replacement` 的类型等待补件流程；
3. 本地授权缺失、材料号已换版（`superseded_document`）、原件文件缺失
   分别返回明确阻断原因，不凑件；
4. 先在本地创建 `pending_reconcile` 送审包再上传；503 或响应异常保持该状态，
   由 `reconcile` 依据受理台历史页的 reference→receipt 映射收口。

### reconcile / poll

* `reconcile` 只处理 `pending_reconcile` 行：历史页存在对应 reference 时
  登记收件号并置 `received`；不存在则保持待核对，绝不新建送审包。
* `poll` 对每个已有收件号打开 review 页，空 `results` 视为仍待审，
  结论按类型 upsert，保留历史时间戳。

### supplement

提交前依次校验：

1. 该收件号当前对该类型的结论确为 `needs_replacement`；
2. 该旅客当前批次要求中存在该类型，且本地有其本人成功领取的授权记录；
3. 补件材料号与当前要求一致，本地原件可读；
4. 补件 SHA256 与该收件号中被审原件不同（拒绝重复件）。

通过后在原 review 页提交，补件历史挂同一 receipt；本地补件行先
`pending_reconcile`，取得正常响应后置 `submitted`。

## 4. 转团与换版

* **转团**：`travelers` 出现新批次行；旧批次送审包、收件号、结论原样保留，
  新批次的 `_accepted_kinds` 为空集合，必须重新领取与送审。
* **换版**：`requirements` 指向新材料号；旧授权行仍在但与当前要求不符，
  状态显示 `superseded_document`；旧送审包中被审的是旧字节，
  review 页结论不变，补件只能使用新版本。
* **共用监护证明**：`documents` 按 SHA256 去重，两名旅客的 `downloads`
  授权行与各自送审包中的文件归属独立保存。

## 5. 遮蔽规则

`masking.mask_text` 对连续 4 位及以上数字保留后三位、其余替换为 `*`。
日志 Handler 与 `render_status` 统一过滤；SHA256 摘要、材料号（如
`PP201-v1` 中的短数字）不属于证件号，不影响资料员核对。需要完整原件时，
只能到隔离下载目录中打开文件，终端与日志不提供明文。
