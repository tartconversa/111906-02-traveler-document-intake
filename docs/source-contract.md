# 证件资料和受理台
入口 `/`，旅客页 `/traveler/{id}`。required 是当前出行批次需要的来源材料号；documents 材料内容只经页面下载，文件名可能相同。today 是演练业务日期，delegate_until 早于此日为委托已到期。下载链接以 traveler、purpose 限定用途，已登录浏览会话中点击可取得原件；委托到期、用途拒绝及旅客不属于文件范围分别给出403原因。家庭共用文件仍须对每位旅客检查权限。
受理台 `/intake` 接收 reference、metadata(JSON字符串) 和 passport/guardian 文件。metadata 至少包含 traveler、trip、purpose、documents（类型到来源材料号映射），文件的SHA256出现在远端历史。受理台只登记送审事实，不替发送方判断资料是否可交。重复reference返回第一次送审，不会修改旧文件；确认丢失时在历史页核对原引用，不能仅因HTTP503建立第二件。
逐项结论 `/review/{reference}` 中 receipt 为收件号，results 为按材料类型的待审核结果；空对象表示仍待审，needs_replacement 为该收件的指定补件要求，accepted 为接受该收件中对应材料。补件必须保留receipt归属，用新reference、kind、document_id、file送入同一review页，补件历史独立保存。当前结论不会替业务程序把旧批次迁到新批次。
现有门户只承诺页面交互和浏览器下载/上传。采集产品使用 Chromium，不读取服务器实现、fixtures及演练控制，也不直接调用页面背后的HTTP请求绕过UI。可通过DOM读已展示的必要契约。
检查人员用 X-Operator: local-operator，POST `/__control` JSON stage=0/1/2。阶段0含过期代理；阶段1旅客P-201护照换版且受理要求换护照、监护证明已接受，P-202转团并续委托；阶段2受理员接受指定材料。控制不改变已提交的原件及收件身份。`/__inspect`仅供检查。lose_response=yes会先收件后503。
