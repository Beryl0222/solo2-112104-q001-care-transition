# 出院叙述与照护交接中的指令冲突核对

社区照护协调员会把医院出院记录、家属补充、门诊安排整理给接班人员。不同记录常
用简称指同一项护理动作，修订版可能只改了其中一个条件（例如只改频次，不动时段
与负责人员）。若把整段摘要当成当前指令，已被改掉的旧说法会重新进入交接单，照
护人员也无法看出哪条结论来自患者陈述、哪条来自正式记录。

本项目提供一套可重复使用的 Python 文本处理能力（仅标准库，Python 3.11），把
合成照护材料整理成**有出处、可确认、可完成**的交接事项：

- 患者、药物、护理动作、执行时段、负责人员**分别识别**；只有显式列出的别名才会
  被合并，名称相似不会被当作同一对象。
- 正式记录之间若对同一字段给出不一致安排，**并列展示冲突及双方原文**，不由系统
  择新、择多或择"版本号更高"者。
- 更正**只替换它点名的那一个条件**，其余条件保留；更正只笼统指向"测量安排"而
  无法唯一对应时，不自动改写，只报歧义。
- 家属补充只作为**观察线索**：可关联到事项、可标记与正式安排不一致，但绝不覆盖
  专业人员的明确安排。
- 新材料到达后，**未完成**事项按完整语料重新核对并退回待确认；**已完成**事项保留
  当时采用的依据（冻结快照），新材料单独标注，不改写历史记录。
- 每项判断都可追到来源 id、原文片段与字符区间；信息缺失与相互矛盾分开呈现。

> 本项目只使用合成照护文本；任何临床处置仍由有权限的医护人员确认。

## 快速开始

```bash
# 1. 读入一批材料（可以分多次、按到达顺序读入）
python3 -m care_handoff ingest data/care.json --state handoff_state.json

# 2. 查看某位患者的待确认 / 已确认 / 已完成事项
python3 -m care_handoff board 林宁 --state handoff_state.json
python3 -m care_handoff board 林宁 --state handoff_state.json --json

# 3. 查看单项事项及其全部出处
python3 -m care_handoff show 林宁::复诊 --state handoff_state.json

# 4. 确认。存在冲突或缺失时，必须显式承认问题；承认只记录"已读"，不解决冲突
python3 -m care_handoff confirm 林宁::复诊 --by 王护士 \
    --acknowledge missing:responsible --state handoff_state.json

# 5. 完成。确认后内容若有变化，完成会被拒绝，需先重新确认
python3 -m care_handoff complete 林宁::复诊 --by 王护士 --state handoff_state.json

# 6. 列出所有患者及各类事项数量
python3 -m care_handoff patients --state handoff_state.json
```

`--acknowledge` 接受 `conflict:<字段>` 与 `missing:<字段>`，字段为
`timing` / `responsible` / `medication`。确认与完成都会记录操作人和时间。

## 作为库使用

```python
from care_handoff import CareState, build_ledger, load_batch

ledger = build_ledger(load_batch("data/care.json"))
state = CareState()
state.ingest(ledger)

board = state.board("林宁")
for item in board["pending"]:
    print(item.snapshot.display)
    for resolution in item.snapshot.fields:
        print(" ", resolution.field, resolution.display)
        for value in resolution.distinct:
            p = value.provenance
            print("    ", p.source_id, p.quote, p.start, p.end)
```

主要入口：

| 模块 | 职责 |
| --- | --- |
| `care_handoff.sources` | 读取、校验来源 JSON；判定来源权威类型 |
| `care_handoff.extract` | 别名表、时段维度、分句、指令与更正解析 |
| `care_handoff.ledger` | 把一批来源整理成带出处的快照、冲突、观察线索与提示 |
| `care_handoff.workflow` | 待确认/已确认/已完成状态、确认与完成规则、状态持久化 |
| `care_handoff.render` | Markdown 交接单与 JSON 输出 |
| `care_handoff.cli` | 上述能力的命令行入口 |

## 来源材料格式

`data/care.json` 是 JSON 数组。最小字段为 `id` 与 `text`；可选字段：

- `kind`：决定权威类型。正式记录：`discharge`、`discharge_correction`、
  `outpatient`、`clinic_note`；家属补充：`family_note`、`family_update`；
  其他/未知类型按行政备注处理，**不会**成为护理安排。
- `version`：正整数。仅用于同一来源内部的排序与展示，**不用于**决定冲突中谁对。
- `patient`：患者姓名。缺省时只在同一批次已明确出现该姓名后，才沿用上文患者。
- `author`、`received_at`：可选元数据，随出处一起保留。

原始字段会完整保存在状态文件中，因此更正可以晚于被更正的记录单独到达。

## 设计规则

### 1. 分别辨认，不因相似而合并

- 动作别名是**显式白名单**（`ACTION_ALIASES`）。`测血压` 与 `血压测量` 合并，
  `血压` 与 `血糖` 不合并；`测量血压和血糖` 会拆成两项。
- 药物是独立命名空间（`MEDICATION_ALIASES`），不根据动作词推断药物。
- 患者姓名只在被声明时识别（`患者X`、`X出院`、已在本批次出现的姓名），
  `患者昨晚头晕` 不会被读成名为"昨晚头晕"的患者；`林宁` 不会匹配 `林宁安`。
- 负责人员是**角色**（如社区护士、家属），"由家属陪同"只说明陪同，不会被当作
  负责人员。

### 2. 时段按维度保存

时段被拆为 `frequency`（频次）、`relation`（饭前/饭后/睡前）、`anchor`
（出院后/3日后等）。因此"把频次改为每日一次"只替换频次，`饭后`、`出院后`
等未提及的维度原样保留；被替换的旧值仍保留在 `superseded` 中备查。

### 3. 冲突不自动消解

同一字段出现两个以上不同的正式取值即为冲突：

- `FieldResolution.is_conflict` 为真，`Conflict.message` 列出各取值及对应原文；
- 确认时必须逐个承认 `conflict:<字段>`，承认不会减少冲突；
- 若同一字段已存在冲突，随后到达的、未明确指向某一原始记录的更正**不会**被应用，
  而是报告 `correction_ambiguous_due_to_conflict`，由医护人员决定更正哪一条。

### 4. 家属补充只提供线索

家属文本按分句保存为 `Observation`。若其中提出的时段维度与正式安排不一致：

- `differs_from_formal=True`，并尽量关联到唯一匹配的事项；
- 不会向正式快照写入任何值，也不会产生冲突计数；
- 在交接单"观察线索"一节单独呈现，并提示待医护核对。

### 5. 状态流转

| 当前 | 事件 | 结果 |
| --- | --- | --- |
| 待确认 | `confirm`（已承认全部冲突/缺失） | 已确认，冻结依据快照 |
| 待确认 | `complete` | 拒绝，必须先确认 |
| 已确认 | 新材料使内容指纹变化 | 退回待确认，列出变化原因 |
| 已确认 | 新材料仅为重复/补充出处 | 保持已确认 |
| 已确认 | `complete` | 已完成，冻结确认时依据 |
| 已完成 | 新材料使内容指纹变化 | 保持已完成，记录 `post_completion_changes` |

内容指纹只取临床取值（动作、药物、各字段取值集合、缺失字段），不取"哪条来源
重复了同一句话"，所以重复材料不会逼迫重新确认。

## 输出示例（节选）

```text
### 复诊
- 事项编号：`林宁::复诊`　状态：**待确认**　修订：2
- 摘要：复诊；执行时段：4日后；负责人员：信息缺失
- **执行时段**：4日后
  - 原文：「复诊时间改为四日后」（care-a5 · 正式记录 · 版本 3 · 字符 0-12）
  - 已被明确更正替换的旧值（保留备查）：
    - 原文：「三日后复诊」（care-a1 · 正式记录 · 版本 1 · 字符 16-21）
    - 原文：「复诊时间改为两日后」（care-a3 · 正式记录 · 版本 2 · 字符 0-12）
- **负责人员**：信息缺失（未见明确记录，需人工补齐）
- 需重新核对的原因：
  - timing: “2日后” -> “4日后”
  - 新材料到达，需重新确认
```

## 演示与测试

```bash
python3 examples/demo.py                       # 端到端演示（含跨批次更正）
python3 -m unittest discover -s tests -v       # 全部测试
```

`data/care.json` 为四周交接场景的最小合成样本；`data/care-week2.json` 演示多动作
与药物记录。

## 已知限制

- 规则与别名表面向合成语料与固定表达，不覆盖自由书写的全部中文句式。未识别的
  时段或负责人员不会被猜测，而是标记为信息缺失并给出提示；同一医疗动作写成
  "服用 A，服用 B，由某角色负责"这类尾随角色时，会暂按共同负责记录并给出
  `shared_responsible` 提示。
- 时段只做相对表达（如"3日后"）的规范化，不换算成绝对日期，避免引入时区或
  出院日缺失导致的错误。
- 患者名与角色依赖显式声明；无法确定归属的材料会报告 `missing_patient`，不会
  强行归到上一位患者。
- 系统不判断临床正确性，也不替团队选择冲突中的哪一条记录。
