---
taskId: IR-0004
specVersion: 5
title: 验证体系缺口闭环（变异/属性/模糊/蜕变/符号/形式化条件触发/SAST 台账）+ spec 质量测量（DSL 编译与骨架 fan-out）+ 实现 fan-out 生命周期（early-exit/champion/oracle/红队燃料管道）+ CNB 临时算力底座条款级规格
irRef: Cloudbird-Software/.github#315
amendments:
- rev: 2
  reason: 红队 R1 审计 insufficient（CNB 4 窗口，3 份有效报告、24 项有效命中经机械核对成立）——IR 保真度补全（AC-21 token 决策、AC-10 merger 分支、AC-12 oracle 适用判据、AC-19 月度干跑、light=1C 硬数值、checklist 结构化前提、kernel 唯一锚点、趋同度突变弱信号）；负向断言补全堵 fail-open（AC-1/3/5/14 及 AC-2/15/16/17 采信前提与交叉核对）；suite 增语义级断言；ADR 承接补引（ADR-0066 底噪扣减/聚簇、ADR-0067 恶意合规分工、ADR-0062 hash 链、ADR-0035/T-13 口径对齐）；弱模型触发侧边界明文与不适用抽样复核；nonGoals 补多平台抽象层/CNB SDK 两项；R1-B 迟归（重跑确认）补 8 项——blastRadius 漏报补登（REPOS.yaml/cost-check.sh/dashboard-update.py/CI-Workflows workflows+scripts）、试点仓真名勘误（AI_Web_School）、路径存在性语义改 planned 双向、IFACE-01 治理仓范围与执行层落点定义、IFACE-02 承接既有 cnb_bridge.py（ADR-0082）、多账号 secret 口径修订（DECISION-06）、BUDGET-01 措辞、S8 仓清单同步、凭据扫描覆盖面清单化
- rev: 3
  reason: 红队 R2 复审 insufficient（3 窗口，4 blocking/9 hardening/6 advisory）——DECISION-06 补时序护栏（ADR-0082 修订落地前多账号条款不生效、实施证据即红）；blastRadius 四条计划路径补 planned 标记（自身双向存在性自洽）；AC-8 声明与 spec-check.py 并存分工+DSL 编译以验证者 APP 身份执行（g060/ADR-0081 边界）；IFACE-02 补现状→目标增量映射；AC-2 补 ≥10 条量化锚点；AC-5 补复算锚点（复算命令+SHA 基准+CI 重放）；AC-7 抽样复核绑定 10%/月度下限；AC-11/12 补对拍停摆负向断言；ADR-0062 措辞改迁移思路、ADR-0081 燃料产物归属留待承接修订、ADR-0083 注明正本待迁以墓碑为准；熔断/死开关/停摆正交性声明；suite 负向断言覆盖扩展+词表收紧
- rev: 4
  reason: 红队 R3 终审收敛（2 窗口；R3-B 判"无 blocking 级新缝隙"，R3-A 剩余 2 blocking 均系文本级）——ADR-0083 引用修正（正本已迁 archive 且 sha256 与 INDEX 一致，撤回"待迁"过时陈述）；AC-8 时态澄清（spec CI 关卡为 W2-C1 实施后的验收状态，实施前由 ADR-0083 suite 门+adversary 守门）；DECISION-06 时序护栏补检测载体（周审计 secret 清单对账+隔离审计 grep）；AC-4/10/19/21 补显式负向断言；suite 负向覆盖扩至 17 AC+关键 AC 工件词绑定
- rev: 5
  reason: judge-deep 恶意合规攻击轮（CNB 沙箱，owner 委托，ADR-0067 语义）——J1 模板句复用击穿 v3（21 条 then 同句样板）→ 套件 v4：then 两两 4-gram 查重<0.55、given≥12/when≥8 深度下限、正文条款内容锚（INV-01 沙箱/INV-02 判定链/INV-03 SHA/IFACE-02 cnb_bridge/DECISION-05 ADR-0082/ASSUMPTION-01 1600）、规模锚（blastRadius≥12/nonGoals≥8/条款正文≥12 字）；J2"真身突变"击穿 v3 → 同被 v4 拦截（本地重放双确认）；AC when/given 语义充实、AC-8→AC-10 竞速语义、DECISION-05 锚短语
acceptanceCriteria:
- id: AC-1
  given: AI_Web_School 仓 weekly 变异测试执行入口已落地
  when: 该入口每周运行且 LLM 生成的定向变异体候选提交入池
  then: 分数与趋势出现在治理仪表盘、趋势下滑自动告警、候选经机械初筛（可执行性与被现有套件杀死率预演）后入池且淘汰率入账；分数核算与入池判定仅由 GitHub CI 执行；任一连续 7 天无变异 run 记录或分数缺失时治理仪表盘判红并自动开 issue（入口被摘除或静默失败不得默认绿）；运行时证据为 run 链接、仪表盘 JSON 与入池清单三件
- id: AC-2
  given: 属性不变量候选已经机械过滤为可执行属性
  when: 变异裁判对全部候选逐条执行裁定
  then: 每条候选产生"杀死的变异体清单"日志、杀死数为零者标记平凡并拒收、存活者进入常驻测试随 CI 运行、当轮不变量候选不少于 10 条（IR 量化锚点，未达阈值记 infra 失败）；日志可采信前提为当轮变异池非空且引擎真实执行——引擎未运行、变异池为空或杀死清单为空且无对应变异池记录时该轮判红（不得伪装"零杀死"）；运行时证据为裁判日志（含至少一条平凡拒收样本）、变异池记录与常驻测试运行记录
- id: AC-3
  given: schema 感知的模糊测试种子语料已入库且深跑任务已派发至外部沙箱
  when: 深跑任务在外部沙箱完成执行并回收产物
  then: 覆盖率增量与唯一崩溃数由 GitHub 侧核算（沙箱自报数字不采信）、崩溃样本经栈哈希机械去重、LLM 产物仅标记为草稿的分诊建议；深跑产物为空、核算脚本失败或超时、GitHub 侧核算无输出时该轮记 infra 失败并红（不得静默绿）；运行时证据为语料入库记录与 GitHub 侧核算日志
- id: AC-4
  given: AI_Web_School 领域自然蜕变关系经 fan-out 盘点产出结构化候选
  when: 人类对候选清单完成抽检
  then: 候选不少于 15 条且每条含机械可验证方式字段（产出不足 15 条该轮记 infra 失败并红）、不少于 3 条实现为常驻测试、testing.yaml 新增对应条款；运行时证据为候选清单、抽检记录与条款 diff
- id: AC-5
  given: 符号执行试点已在解析/实例化纯函数目标上运行
  when: 试点运行时指标数据采集齐备
  then: 产出含路径覆盖、求解超时率、单位时间发现数的证据报告并按三段式判据回答 adopt 或 reject；adopt 则登记触发式条款、reject 则登记拒绝条款并带翻案条件；指标原始数据为空、缺路径覆盖/求解超时率/单位时间发现数任一字段、或报告未附机器可复算的原始数据时该试点验收不通过（CI 红）；reject 结论必须附机器可复算指标且翻案条件字段非空；复算锚点——报告随附复算命令与基准 SHA、CI 对原始数据重放复算、复算失败即红；两种结论本身均视为验收通过、但空壳报告不通过；运行时证据记录为报告、指标原始数据与条款 diff
- id: AC-6
  given: SAST 告警分诊台账已建立并回填存量告警
  when: weekly 清点运行
  then: 每条告警处置（固修/豁免附 ADR 引用/判误报附理由）结构化入账且不可改写、未处置告警自动开 issue、误报率与豁免存量入仪表盘；台账 append-only 校验失败即红（hash 链思路承接 ADR-0062）；运行时证据为台账文件、一次清点 issue 与仪表盘 JSON
- id: AC-7
  given: 形式化验证适用性 checklist（机器可读、治理路径）与卡模板风险等级字段已落地
  when: 一张卡进入形式化触发判定
  then: 判断由弱模型或纯脚本按元数据执行（凡卡元数据可机械判定的字段优先纯脚本、弱模型只填语义字段）且逐项理由留痕入卡、同输入重跑判定一致、checklist 为人类一次性投入且其修改走 ADR、风险等级缺失被 fail-closed 拦截、判适用即自动启动形式化作业并以 kernel 二值结果回写（kernel 为形式化链唯一锚点；弱模型 checklist 判断属触发侧、非判定锚点）、判不适用同样留痕且其记录接受抽样复核与独立重放（比例对齐 BUDGET-03：默认 10% 可降不可免；周期下限每月一次；弱模型不得成为形式化覆盖的单点否决）；X-04 由无条件拒绝修订为条件触发（经 ADR）；运行时证据为一次真实触发全链记录、一次不适用留痕及复核记录、拦截反向测试与 ADR diff
- id: AC-8
  given: 验收 DSL 已定义且新 spec 以 DSL 书写验收标准
  when: spec PR 合并
  then: DSL 验收标准机械编译为测试骨架且携带 spec hash 溯源头、手改生成测试触发 CI 红、修改 spec 重编译后转绿、spec CI 关卡（结构校验/可编译/blastRadius 申报双向存在性——已存在路径必须被申报、申报中不存在路径必须标记 planned）随实施卡落地后须在 specs 路径全部 PR 生效且关卡被摘除时 CI 必红（本句为实施后的验收状态；实施前 specs 路径 PR 由 ADR-0083 suite 门与 adversary 红队 required check 守门）；豁免仅经 ADR 登记通道且计数入账；与既有 spec-check.py（g010 过渡版，ADR-0050）为并存分工——spec-check.py 保留 frontmatter 结构与注入双扫，本关卡新增 DSL 可编译与 blastRadius 双向存在性，重叠面从严者生效、不重复建设；DSL 编译以验证者 APP 身份执行——AG-1 已由 ADR-0076 修订为"开发身份唯一 + 验证者身份独立"，suite 写入授权边界随 ADR-0081（g060 授权面同步：verifier-app 与 owner 可写、开发 APP 改测试路径仍锁）；生成物绑定对应卡 Card 引用；与既有 T-13（ADR-0035 test-integrity）的口径对齐：hash 溯源校验管辖编译生成物（直接红），T-13 管辖一般测试文件四形态（TI-R4 为 require_adr）——同一文件双命中时从严者生效，本对齐为承接声明不修改 T-13 语义；运行时证据为编译产物 diff、篡改红记录与一次 spec PR 全关卡绿记录
- id: AC-9
  given: 一张卡处于 spec 阶段且骨架 fan-out 已派发（N 默认 3–4、含至少一个异构模型实例）
  when: N 份骨架（路线陈述/接口签名草稿/测试草案/假设清单四组件）齐备
  then: 分歧正交分解由脚本执行（契约解读分歧输出为 spec 修订项、路线分歧落盘为策略菜单）、N 份测试草案的交集进入验收标准候选、并集减交集进入红队输入、假设清单去重并集成为 spec 缺口显式清单、趋同度与分歧率入仪表盘且高分歧自动退回规划层；骨架独立性机器校验——相似度判定参数（算法、输入规范化步骤、阈值及其版本化配置位置）入 policy，任两份骨架文本相似度超阈值判疑似串通或拷贝、该份作废或该轮判红（防人为放大交集），机械计算日志记录所用配置版本与判定结果；歧义归因承接 ADR-0066 方法论——同族底噪基线扣减与双向蕴含聚簇，仅当跨族簇数减底噪不小于 2 时归因 spec 歧义（模型固有噪声不得误判为 spec 歧义触发退回）；运行时证据为骨架产物、机械计算日志（含独立性校验与底噪扣减记录）与仪表盘数据
- id: AC-10
  given: 实现 fan-out 决策矩阵（决策密度×可判定性）已入政策
  when: 一张卡进入实现阶段
  then: 决策矩阵三分支机械生效——高决策密度且高可判定为阻塞式 fan-out、语义敏感为组合模式（merger 合成）、低决策密度为单实例加机械门禁（自动获得免 fan-out 标签）；通过 gating 的卡以 N 路并行实现（策略来自骨架策略菜单）、第一个通过全部既有 gate 的实现立即合并且不跳过任何 gate、其余实现转后台执行全量评价；运行时证据为标签记录、三分支路由记录与多路实现的完整时间线日志（路由记录缺失即红）
- id: AC-11
  given: 后台实现完成全量 gate、性能基准与对拍
  when: 后台结果优于现任 champion 且对拍等价
  then: 替换经改进 PR 通道进行且必须同时通过 oracle 对拍与全部 gate 双重裁决；对拍未运行、数据缺失或超时即判红（不得静默停摆）；运行时证据为改进 PR、双重裁决记录与对拍 run 日志
- id: AC-12
  given: 实现竞速完成且存在跨路线簇的亚军
  when: oracle 冻结执行
  then: 冻结记录含簇归属与去相关理由、champion 与 oracle 对拍常驻运行且硬区/软区边界标注落盘、对拍停摆或数据缺失即判红、硬区分歧即暂停相关合并并路由裁决（缺陷修复或契约修订二选一）、oracle 只换代不修补且换代流程脚本化；oracle 适用判据机器可判——大输入空间×高改动频率×行为即产品的表面设立、纯函数小输入空间与不动胶水不设立（判据入 policy）；运行时证据为冻结记录、对拍运行日志与一次注入分歧的裁决记录
- id: AC-13
  given: fan-out 产物（骨架分歧/假设/淘汰路线/对拍分歧四类）统一以结构化格式落盘（含卡 ID、spec hash、基准 SHA）
  when: 红队或意图道闸运行
  then: 输入适配器只读该产物目录、被淘汰路线机械生成差异攻击查询（champion 是否覆盖其边界）、假设清单进入 S6–S8 扫描候选、S8 集合比对纳入产物路径且其仓识别清单同步登记本 IR 新增仓（cnb-bridge 等，防漏报盲区）、产物消费前经基准 SHA 机械核对且不符作废留痕、产物目录 append-only 校验失败即红（ADR-0062）；趋同度突变（历史免 fan-out 卡突然高分歧）作为弱信号入 metrics 观测面；运行时证据为一次真实红队消费燃料产物的全链记录（含 run 链接）与一次道闸消费留痕
- id: AC-14
  given: cnb-bridge 仓建立且治理仓新增外部算力声明条目（三接缝隔离生效前提）
  when: 隔离审计运行并产出可核验结果
  then: 治理仓内全部 CNB 引用仅命中三处接缝（声明条目/组织秘密清单/两个工作流）、删除清单单页存在且其审计脚本 dry-run 输出影响报告零副作用；grep 命中数不等于三接缝、删除清单缺失、或 dry-run 检出副作用时审计判红并阻断相关 PR（审计越界不得默认绿）；运行时证据为审计命令输出与一次越界反向测试记录
- id: AC-15
  given: 配额以配置存在（账号清单/档位阈值/并发上限/告警线）且快照与实耗两路数据齐备
  when: 记账运行并产出可核验结果
  then: 加账号仅改配置零代码、逐账号核·秒差值可查且偏差超阈值告警、余量低于阈值自动开成本类 issue、档位体系为 light=1C（默认）/std=2C/heavy=4C 须理由/8C 禁用；档位证实以 build logs 实际核数为交叉真源（标签与实耗不一致或缺失即判红，自报标签不采信）；台账 append-only 校验失败即红；运行时证据为仪表盘数据、告警记录与含 cpus=1 的构建记录
- id: AC-16
  given: 账号生命周期 runbook（每步带验收命令）与 append-only 生命周期台账已建立
  when: 一个未参与开发的 AI 仅凭 runbook 执行新账号入职
  then: 全流程无人工判断步骤、执行前以不可变 agent 身份标识自动查询其任务/PR/提交参与记录（存证未参与开发）并与会话记录一并落盘、最终 canary 任务通过、入职/降级/退休/事故全部留痕且纠错仅以追加方式、台账 append-only 校验失败即红；运行时证据为该 AI 会话记录、生命周期台账与 canary 构建记录
- id: AC-17
  given: work-inbox 工作发现入口协议（pending 定义/租约/心跳/产物写回）已落地
  when: 强模型按小时自起连续七天
  then: 有 pending 时认领带租约且并发会话不重复认领、租约超时可被接管且原认领作废留痕、租约到期未释放且未被接管超阈值时产出告警并开 issue（不得静默停摆）、无 pending 时仅追加心跳行零派单、任何派单以清单 ID 去重；运行时证据为七天运行历史与会话记录
- id: AC-18
  given: 周审计（配额/活性/延迟/产物真实性抽样/平台政策 diff）已自动化
  when: 审计运行并产出可核验结果
  then: 五项齐全以结构化 issue 产出、政策页变更自动告警、任一红项产出死开关置位建议、审计自身失败或数据缺失按红处理（fail-closed）；运行时证据为连续四周审计 issue 链与一次注入红项处置记录
- id: AC-19
  given: 功能演练脚本就绪（前置条件与承载载体均已就位）
  when: 月度静态干跑与季度功能演练按期自动执行
  then: 月度静态干跑（隔离不变式验证）自动产出审计 issue；季度功能演练全程零人工——置位死开关→发散链切换付费 API 回退完成一轮真实任务→gate 全绿→自动复位→计时报告；物理删除仅为一次性退休动作不做演习且此证明力边界已入册；干跑或演练未按期自动产出 issue 即红；运行时证据为干跑 issue 链、演练计时与回退任务 run 链接
- id: AC-20
  given: 本 IR 自举试点的一张高可判定实现卡进入执行
  when: 走完骨架 fan-out→策略菜单→N 路竞速→early-exit 合并→后台对拍→亚军冻结 oracle 全链
  then: 人类触点为一次规划会话加一次抽检、外部沙箱产物经机械核对零作废进入判定；运行时证据为完整时间线日志（骨架产物→多实现 PR→合并→后台记录→冻结记录）
- id: AC-21
  given: token 治理决策需入册
  when: 高权限 token 保持决策以 ADR 落实
  then: ADR 记录 owner 裁决（管理简单性优先）与缓解条款——token 仅存 org secret、永不进入任何 agent 上下文与外部沙箱、泄漏应急流程（吊销→轮换→账本核查）runbook 化；任意派单任务文本经凭据形状扫描不含任何 token 形态、扫描模式集按受管平台清单配置且清单外形状按登记制扩展、扫描步骤缺失或未运行即红（防回归测试常驻）；运行时证据为 ADR diff、org secret 存在性查询记录（key 不可见）与扫描测试运行记录
blastRadius:
- repo: .github
  path: specs/IR-0004/**
- repo: .github
  path: governance/GOVERNANCE.yaml
- repo: .github
  path: governance/policy/automation-limits.yaml
- repo: .github
  path: governance/policy/testing.yaml
- repo: .github
  path: governance/metrics.py
- repo: .github
  path: governance/cost-check.sh
- repo: .github
  path: governance/dashboard-update.py
- repo: .github
  path: governance/REPOS.yaml
- repo: .github
  path: .github/workflows/cnb-dispatch.yml
  planned: true
- repo: .github
  path: .github/workflows/cnb-audit.yml
  planned: true
- repo: .github
  path: .github/ISSUE_TEMPLATE/**
- repo: agent-registry
  path: decisions/**
- repo: archive
  path: adr/**
- repo: CI-Workflows
  path: pipeline/adversary/**
- repo: CI-Workflows
  path: .github/workflows/**
- repo: CI-Workflows
  path: scripts/**
- repo: CI-Workflows
  path: pipeline/spec-template.md
- repo: AI_Web_School
  path: tests/**
- repo: AI_Web_School
  path: .github/workflows/**
- repo: AI_Web_School
  path: formal/**
  planned: true
- repo: cnb-bridge
  path: "**"
  planned: true
nonGoals:
- 不追求全仓形式化——仅条件触发小而稳定高危面
- 不重写存量 spec 为 DSL——新增 spec 起强制、存量自然迁移
- 不自研模糊/变异/符号/形式化框架——全部接入现有工具
- 不做无停止规则的无限 fan-out——N 默认 3–4、大于 16 需 ADR 特殊理由
- 不给所有卡强制 fan-out——决策矩阵 gating
- 不把 oracle 当产品维护——只换代不修补
- 不迁移 SSOT 到 CNB、不做镜像同步与平台互备演练（后续 IR）
- 不依赖 api_trigger 直接触发通道（未开放，仅留月度复验探针）
- 不做多平台算力抽象层、不自研 CNB SDK
- 不做物理删除演习（owner 裁决：一次性退休动作）
- 不修改既有判定语义——testing.yaml 既有条款、gate/org-gate 结构、conductor/arbiter 状态机零变化（新增为增量；与 T-13 的口径对齐为承接声明而非修改）
- 不把 NPC/LLM 输出直接作为任何 gate 输入
---

## INV（不变量）

- **INV-01 生成/裁决分离**：LLM 与外部沙箱仅出现在生成侧；变异分数、测试通过、覆盖率、DSL hash、栈哈希去重、交集/并集计算、竞速选择器、kernel 检查全部由 GitHub CI 内代码核算，沙箱自报数字一律不采信。
- **INV-02 判定链不经过外部算力平台**：一切 CNB 内容可整体删除，删除后 gate/org-gate/conductor/arbiter 语义不变；任何把 CNB 写进判定路径的改动即违约。
- **INV-03 机械核对铁律**：外部沙箱产物进入任何判定链前必经机械核对——基准 SHA 在 run 开始时动态获取并写入报告（#263 erratum 为鉴）、差异可应用、输出格式校验；不符即作废并记 infra 失败。燃料管道产物消费同受此铁律。
- **INV-04 fail-closed**：形式化触发的风险等级缺失即拦截；审计任一红项产出死开关置位建议；平台失效时清单挂起加开 issue，永不为完成而跳过记账或核对；各判定环节异常、超时、数据缺失一律按红处理，无"未定义默认绿"。
- **INV-05 账本不可改写**：用量账本、生命周期台账、分诊台账、fan-out 产物目录均 append-only 且校验失败即红（hash 链思路承接 ADR-0062）；纠错仅追加 erratum 行。
- **INV-06 凭据纪律**：平台 token 仅存 GitHub org secret；任务文本不含任何凭据；沙箱不注入 GitHub 凭据；高权限 token 的额外风险由 ADR 缓解条款覆盖。
- **INV-07 既有护栏全部适用**：派单前检查 AUTO_MERGE_DISABLED 与成本熔断未决 issue；同一卡修红重试上限对沙箱派单同样计数。

## BEH（行为）

- **BEH-01**（AC-1）weekly 变异实跑 + 定向候选机械初筛入池 + 入口静默失效判红。
- **BEH-02**（AC-2）属性候选过滤 + 变异裁判（零杀死 = 平凡拒收；引擎实跑为采信前提）+ 存活常驻。
- **BEH-03**（AC-3）种子入库 + 外部沙箱深跑 + GitHub 侧核算 + 栈哈希去重 + 分诊仅草稿 + 产物空即红。
- **BEH-04**（AC-4）蜕变领域 fan-out 盘点 + 人抽检 + 条款入册 + 常驻实现。
- **BEH-05**（AC-5）符号执行试点出指标 + adopt/reject 二选一登记 + 空壳报告不通过。
- **BEH-06**（AC-6）SAST 分诊台账 + 回填 + weekly 清点 + 误报率入仪表盘 + append-only 红。
- **BEH-07**（AC-7）checklist 触发（弱模型留痕可重放、机械字段优先脚本、触发侧非判定锚点、kernel 为形式化链唯一锚点、不适用抽样复核）+ X-04 经 ADR 修订为条件触发。
- **BEH-08**（AC-8）DSL 编译 + spec hash 溯源 + 篡改红 + spec CI 关卡负向断言 + 与 T-13 口径对齐（从严者生效）。
- **BEH-09**（AC-9）骨架 fan-out 四组件 + 独立性防串通校验 + 分歧正交分解（承接 ADR-0066 底噪扣减与聚簇）+ 交集/并集机械计算 + 高分歧退回规划层。
- **BEH-10**（AC-10）决策矩阵三分支 gating（阻塞 fan-out / merger 组合 / 单实例免 fan-out）+ N 路竞速 + early-exit 不跳 gate。
- **BEH-11**（AC-11）后台评价 + 改进 PR 双重裁决替换。
- **BEH-12**（AC-12）跨簇亚军冻结 + 对拍常驻 + 硬软区 + 分歧路由 + 只换代不修补 + 适用判据入 policy。
- **BEH-13**（AC-13）fan-out 产物四类统一落盘 + 适配器只读 + 差异攻击查询机械生成 + 消费前核对 + 趋同度突变弱信号。
- **BEH-14**（AC-14/15）三接缝隔离（越界即红）+ 配额配置化 + 快照/实耗对账 + light=1C 硬档位 + build logs 交叉真源。
- **BEH-15**（AC-16）runbook 每步验收命令 + 陌生 AI 入职 + 生命周期 append-only。
- **BEH-16**（AC-17）work-inbox 租约/幂等/心跳 + 停摆告警。
- **BEH-17**（AC-18/19）周审计五项（fail-closed）+ 政策 diff + 月度干跑 + 功能演练零人工（置位→回退→复位→计时）。

## IFACE（契约）

- **IFACE-01 三接缝**：治理仓（Cloudbird-Software/.github 仓内）对 CNB 的引用收敛为 GOVERNANCE 声明条目、org secrets、cnb-dispatch/cnb-audit 两个工作流；隔离审计 grep 仅命中此三处，越界即红。执行层实现落点为 CI-Workflows pipeline/adversary/**（既有 cnb_bridge.py，ADR-0082）与 cnb-bridge/**——其 CNB 引用属执行层实现、不计入接缝违规，但不得进入判定路径（INV-02）。
- **IFACE-02 派单协议**（对既有 cnb_bridge.py 的增强要求，承接 ADR-0082 通道链与 fallback 语义）：任务描述机器可读、必含档位与清单 ID、输出契约（差异全文+测试原始输出含退出码+基准 SHA 确认）、run-id 前缀、投递前二次确认窗口归属、单账号并发上限可配置（默认 8）。现状→目标增量映射（增强而非重建）：保留既有 dispatch 接口三字段（task_type/payload/public_only）不变，档位落 payload.cpus、清单 ID 落 payload.list_id、run-id 前缀落 payload.run_id、并发上限为调度侧配置、二次确认为投递前校验步骤，通道复用既有提交路径（ADR-0082 通道链）。
- **IFACE-03 产物目录格式**：fan-out 产物为 append-only 结构化记录，字段含类型（骨架分歧/假设/淘汰路线/对拍分歧）、卡 ID、spec hash、基准 SHA；红队与意图道闸输入适配器只读该目录。
- **IFACE-04 spec 产物契约**：DSL 编译产物携带 spec hash 头；hash 校验为 gate 级检查；豁免仅 ADR 登记通道。
- **IFACE-05 观测口径**：核·秒消耗与档位证实以平台 build logs（duration×labels.cpus）为对账真源，本地快照差分仅作交叉验证。

## BUDGET（预算）

- **BUDGET-01 核时档位**：默认档位为 light=1C，std=2C，heavy=4C 须理由，八核档禁用；档位以 build logs 实际核数交叉证实（标签与实耗不一致即红）；快照差分与构建日志实耗双路对账、偏差超阈值告警。
- **BUDGET-02 fan-out 纪律**：N 默认 3–4；趋同卡获免 fan-out 标签；top-2 差距小于选择器判别力时升级选择器而非加大 N；N>16 需 ADR；预算由收敛带宽决定与 token 无关。
- **BUDGET-03 审计抽样**：产物真实性抽样比例默认 10%、可降不可免。

## DECISION（决策）

- **DECISION-01** 高权限 token 保持不变（owner 裁决：管理简单性优先），缓解条款（仅 org secret、永不进 agent 上下文与外部沙箱、泄漏应急）与验收（AC-21）随本 spec 落实，ADR 随实现卡合并。
- **DECISION-02** X-04 由无条件拒绝修订为条件触发，修订经 ADR 完成（spec 不直接修改治理条款）。
- **DECISION-03** 演练全自动零人工；物理删除为一次性退休动作不做演习，证明力边界（功能脱离经周期证明、物理删除未经演习）如实入册（owner 裁决）。
- **DECISION-04** api_trigger 不作为依赖通道，保留月度复验探针；issue 窗口评论为当前唯一已验证机器派单通道。
- **DECISION-05 承接引用**（含 ADR-0082 红队守门收口）：条款级规格方法承接 ADR-0050；无人值守护栏适用 ADR-0040；中心审判钉点 ADR-0046；ADR 墓碑机制 ADR-0053；fail-before 语义 ADR-0061；verifier 范式 ADR-0072（红队候选生成属选择层不进判定链，不冲突）；测试防篡改口径 ADR-0035/T-13（hash 溯源管生成物、T-13 管一般测试文件，双命中从严者生效）；计量与产物 hash 链 ADR-0062（token 计量口径；账本族 append-only 校验系迁移其 hash 链思路的扩展应用，非正本直接条款）；语义熵分歧度量 ADR-0066（骨架 fan-out 的歧义归因复用其底噪扣减与双向蕴含聚簇——骨架 fan-out 产出 AC 候选/模糊地图/策略菜单，语义熵仪器判定歧义是否达 spec bug 级，两仪器分工不重复建设）；恶意合规 adversary ADR-0067（其套件充分性判定与 AC-2 变异裁判互补——变异裁判测杀变异能力、恶意合规测针对性偷懒抵抗力，统一为测试有效性判定族的两个机械层）；#263 的卡绑定测试与红队守门为上游依赖（T-14/T-15/AR-10；其机器化落地承接 ADR-0083 spec PR suite 强制门与红队 required check（正本 archive/adr/ADR-0083-t14-suite-gate.md，sha256 与 INDEX 登记逐字节一致，经 R3 实测核实））；红队守门收口 ADR-0082（默认 verifier 范式、CNB 通道与 fallback 链——本 spec 的派单协议为其执行层增强、判定环节沿用其 verifier 范式不另设；死开关置位建议/租约停摆告警与其连续 3 次 fallback 熔断为不同触发源、分别入账互不遮蔽，fallback 计数熔断语义沿用不另设）；spec 阶段攻击面 ADR-0079、验证者身份 ADR-0076、holdout 挂载 ADR-0080、验证者写豁免 ADR-0081（DSL 编译产物位于 suite 豁免路径内、以验证者 APP 身份执行；燃料产物写入主体不在 ADR-0081 豁免面，随实施卡以承接修订显式补登）；本 spec 的 DSL 编译与骨架 fan-out 为 #263 体系的机械层承接，不重复建设。
- **DECISION-06** 多账号扩展对 ADR-0082"配置面恰为 1 org secret"口径的修订：每账号恰 1 个同形 org secret（CNB_TOKEN_<alias>）+ 共享 org variable，配置面膨胀（存在非登记 secret）判红；该修订随本 IR 以 ADR 落实。时序护栏（同 ADR-0076 决策 6 范式）：ADR-0082 修订 ADR 未合并前，任何多账号 org secret 的创建或使用证据出现即判红（检测载体：周审计的 org secret 清单对账 + 隔离审计 grep——发现未登记 CNB_TOKEN_* 形态 secret 即开 issue 判红）；本条多账号条款在修订 ADR 落地前不生效（单账号先行）。

## ASSUMPTION（假设）

- **ASSUMPTION-01** CNB 免费额度与 NPC 行为稳定（当前实测：单账号月度开发额度约 1600 核时；政策变化由周审计 diff 监测，失效即回退付费 API）。
- **ASSUMPTION-02** 形式化与符号执行工具链按栈可用，具体选型由试点实测决定（spec 不指定）。
- **ASSUMPTION-03** 强模型可按小时自起（协议就位后由 owner 配置调度；本 spec 只定义协议）。
