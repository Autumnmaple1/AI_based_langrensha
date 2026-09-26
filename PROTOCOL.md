# AI 狼人杀 WebSocket 接口规范

协议版本：`1.1`（规则已确认；新增主持人暂停控制）  
规则版本：`9p-seer-witch-hunter/1.0`  
日期：2026-09-25

本文是裁判端、社员客户端和 Agent 的共同实现契约。MUST／必须表示强制要求；建议表示可调整的实现选择。没有写在协议里的客户端动作一律不改变游戏状态。

## 1. 范围与规则状态

已确认的要求：9 人，3 狼人、3 村民、预言家／女巫／猎人各 1 人；女巫解药未用时每夜都可自救；狼人不能私聊；狼人每轮能看到本轮结束后各狼的刀票；最多 3 轮刀票，最终平票随机决定；Agent 在社员机器运行，主动建立双向 WebSocket 连接。

以下规则已由组织者于 2026-09-23 确认，作为本版固定规则。同一对局中不得修改；后续调整需发布新规则版本：

| 项目 | 本版规则 |
|---|---|
| 胜负 | 屠边：狼全死则好人胜；否则平民全死或神职全死则狼人胜 |
| 猎人和胜负顺序 | 先结算合法开枪，再检查胜负；双方条件同时满足时按狼全死优先，好人胜 |
| 警长、自爆 | 均不设置 |
| 狼人刀票 | 唯一最高票即结束，不要求绝对多数；仅本轮计票，不累计；新一轮可选择任意存活玩家，包括狼人和自己 |
| 空刀与弃票 | 狼刀 `0` 为有效空刀选项，`null` 为弃票；最终平票在最高票选项中随机，可能抽中 `0` |
| 全员弃刀票 | 前两轮继续，第三轮空刀 |
| 女巫 | 一解药一毒药，每夜最多用一瓶；有解药时看刀口，用完后不可见；毒药不能毒自己；空刀时不能用解药 |
| 预言家 | 查验其他存活玩家，可重复查验；仅返回狼人／好人 |
| 猎人 | 被刀或放逐可开枪，可放弃；被毒或同夜既刀又毒不可开枪 |
| 夜间行动 | 入夜存活者完成行动后统一结算死亡 |
| 白天放逐 | 不能投自己，可弃票；最高票平票则候选人补充发言、其他存活者复投一次；再平票不放逐 |
| 遗言 | 首夜死亡者、白天放逐者有遗言；猎人带走者无遗言；其他夜死无遗言；游戏已结束则取消未执行的遗言 |
| 发言顺序 | 首日随机起点和方向；之后沿用方向，从上日首位发言者后沿该方向找到第一名存活者开始；PK 按本日顺序；多人遗言按座位升序 |
| 超限 | 完成第 30 个白天仍未结束则 `draw`，不计入任一阵营胜局 |
| 行动预算 | 每次 60 秒，发言和遗言最多 500 个 Unicode 码点；所有人相同 |

## 2. 部署与连接

```text
社员进程 A ──主动连接──┐
社员进程 B ──主动连接──┼── ws(s)://裁判地址/ws/agent
社员进程 C ──主动连接──┘
```

- 所有人连接同一个地址，不给每人启动独立服务端。
- 公网部署使用 `wss://`；受控局域网开发可用 `ws://`。
- 每个 Agent 实例有独立 `agent_id` 和不可猜测的 token；token 绑定实例，不在日志、URL或回放中明文保存。
- 一条连接绑定一个实例；v1 中一个实例同时最多参加一局、占一个座位。一个社员需要多个实例时领取多组凭证，分别连接。
- 服务器分配对局与座位；客户端不能自行指定角色或冒充其他座位。
- UTF-8 JSON 文本消息，一条 WebSocket 消息一个 JSON 对象。不接受二进制消息，不支持同一消息内批量数组。
- 最大单条消息 1 MiB（按 UTF-8 字节）；若完整状态超过上限，按平台错误中止，不能静默截断可见历史。
- 连接后 10 秒内必须完成 `auth`。已认证实例的新连接成功认证后替换旧连接，旧连接关闭；旧连接剩余响应不再接受。
- SDK 在网络断开后指数退避重连：1、2、4、8 秒，之后最多 10 秒并加入抖动。断线不延长行动期限。
- 服务端每 20 秒发送 WebSocket Ping，10 秒内没有 Pong 则关闭连接；SDK 自动处理，不将心跳交给 Agent。
- v1 无需 HTTP 业务接口、轮询接口或消息队列。观赛和管理接口不在本规范范围内。

## 3. ID 与通用信封

ID 都是不透明字符串，除 `player_id` 为 1～9 的整数。客户端不得通过 ID 的格式推断隐藏信息。

| 字段 | 类型 | 方向／必填规则 | 含义 |
|---|---|---|---|
| `protocol_version` | string | 双向必填，固定 `1.1` | 协议版本 |
| `type` | string | 双向必填 | 消息类型 |
| `message_id` | string | 双向必填 | 发送者生成的消息唯一编号；不是行动请求 ID |
| `game_id` | string/null | 双向必填 | 局外为 null；局内为实际对局 ID |
| `player_id` | integer/null | 服务端必填，客户端禁止填写 | 接收者本局座位；公开消息仍按各连接填接收者座位；行为发起者写在 content 中 |
| `request_id` | string/null | 双向必填 | 行动请求、回复、ACK、错误、关闭通知关联同一值；其他消息为 null |
| `seq` | integer/null | 服务端必填，客户端禁止填写 | 局内该玩家自己的可见事件序号，从 1 递增；局外及状态快照为 null |
| `sent_at` | string | 服务端必填，客户端禁止填写 | UTC RFC3339 时间，例如 `2026-09-23T08:00:00.000Z` |
| `content` | object | 双向必填 | 按消息类型定义，禁止重复 JSON 键 |

服务端 `seq` 是每局每个玩家独立的连续序列，不能直接使用全局事件编号，否则可能暴露隐藏事件数量。所有局内服务端消息（快照除外）均进入该玩家流；给不同玩家的同一公告有各自 seq。

客户端同一发送实例中 `message_id` 不得用于不同内容。结构校验严格拒绝额外字段；未来新增字段通过协议版本协商发布。表中未标“可选”的 content 字段均必填；null 和字段缺失不同。

### 3.1 服务端请求示例

```json
{
  "protocol_version": "1.1",
  "type": "werewolves_act",
  "message_id": "msg_7f23",
  "game_id": "game_001",
  "player_id": 2,
  "request_id": "req_a81c",
  "seq": 6,
  "sent_at": "2026-09-23T08:00:00.000Z",
  "content": {
    "night": 1,
    "round": 1,
    "max_rounds": 3,
    "previous_votes": [],
    "legal_targets": [0,1,2,3,4,5,6,7,8,9],
    "allow_abstain": true,
    "deadline_at": "2026-09-23T08:00:30.000Z"
  }
}
```

### 3.2 客户端回复示例

```json
{
  "protocol_version": "1.1",
  "type": "action",
  "message_id": "client_msg_001",
  "game_id": "game_001",
  "request_id": "req_a81c",
  "content": {"action": "kill_vote", "target": 5}
}
```

## 4. 通用数据类型

| 类型 | 字段／取值 |
|---|---|
| `Role` | `werewolf`、`villager`、`seer`、`witch`、`hunter` |
| `Camp` | `werewolves`、`good`；good 包括神职与村民 |
| `VoteRecord` | `{"voter":整数座位,"target":整数或null}`；公开票型不含超时原因 |
| `PlayerState` | `{"player_id":整数座位,"alive":布尔}`；不附其他人的角色 |
| `CheckRecord` | `{"night":正整数,"target":整数座位,"alignment":"werewolf或good"}` |
| `phase` | `setup`、`night_start`、`wolf_vote`、`witch_action`、`seer_action`、`night_resolution`、`hunter_action`、`last_words`、`day_speech`、`day_vote`、`pk_speech`、`pk_vote`、`day_resolution`、`ended` |

狼人票中的 `0` 计入最高票比较，`null` 不计票；白天票中的 `0` 为弃权，不计票，禁止 null。非法回复不进入票型；超时后生成默认票，狼票为 null，白天票为 0。原始非法回复和故障原因仅存裁判审计日志。

所有数组顺序都有意义：票型按 voter 升序，候选人和普通目标列表按座位升序；发言顺序用独立字段，不能依赖 players 列表。

## 5. 消息总表与接收范围

S→C 表示裁判到客户端，C→S 表示客户端到裁判。“全体”指本局所有已分配实例，包括已死亡玩家；死者客户端不得把后续信息传给存活实例。

| type | 方向 | 接收范围／作用 |
|---|---|---|
| `auth` | C→S | 认证，连接后第一条消息 |
| `auth_result` | S→C | 认证结果，仅本连接 |
| `ready` | C→S | 表示 SDK 和 Agent 已准备好加入下一局 |
| `ready_result` | S→C | 就绪状态确认 |
| `gamerule` | S→C | 全体，规则及运行限制 |
| `game_start` | S→C | 全体，本局座位表和本人座位 |
| `role` | S→C | 仅本人，身份 |
| `werewolves_info` | S→C | 仅狼人，本局全部狼人座位 |
| `phase_changed` | S→C | 仅公开昼夜切换／结束，不广播隐藏角色行动阶段 |
| `game_control` | S→C | 全体，主持人暂停或继续；`content={paused:boolean,reason:"host"}` |
| `werewolves_act` | S→C | 存活狼人，第一轮刀票请求 |
| `werewolves_revote` | S→C | 存活狼人，第二或第三轮刀票请求 |
| `werewolves_result` | S→C | 存活狼人，每轮票型及是否完成决策 |
| `witch_act` | S→C | 入夜存活女巫，用药请求 |
| `witch_result` | S→C | 仅女巫，接受的操作及药剂状态 |
| `seer_act` | S→C | 入夜存活预言家，查验请求 |
| `seer_result` | S→C | 仅预言家，查验结果或超时跳过 |
| `hunter_act` | S→C | 仅有开枪资格的猎人，开枪请求 |
| `hunter_result` | S→C | 仅猎人，开枪或放弃结果 |
| `hunter_shoot` | S→C | 全体，猎人公开身份并射击 |
| `death` | S→C | 全体，新增死亡名单；夜间死因不公开 |
| `speech` | S→C | 当前发言者，正常／PK 发言请求 |
| `speech_dying` | S→C | 符合遗言资格者，遗言请求 |
| `speech_public` | S→C | 全体，公开发言或遗言 |
| `vote` | S→C | 当前合法投票者，放逐或 PK 投票请求 |
| `vote_result` | S→C | 全体，白天票型及下一步 |
| `action` | C→S | 提交当前行动 |
| `action_ack` | S→C | 仅行动者，动作已接受；不等于阶段已结算 |
| `action_error` | S→C | 仅本连接，协议或行动错误 |
| `request_closed` | S→C | 仅请求接收者，超时／取消的最终处理 |
| `sync` | C→S | 请求恢复状态 |
| `state_sync` | S→C | 仅本人，完整可见快照及未完成请求 |
| `game_end` | S→C | 全体，终局结果及身份 |

不提供玩家间任意消息转发接口。不接收 `werewolves_discuss` 或任何私聊文本。

## 6. 连接与开局字段

以下各表定义 content；均继承通用信封。

| type | content 字段 |
|---|---|
| `auth` | `agent_id:string`、`token:string`、`client_version:string` |
| `auth_result` | `ok:boolean`、`agent_id:string或null`、`connection_id:string或null`、`code:string或null`、`message:string`；失败统一 `AUTH_FAILED` 或 `UNSUPPORTED_VERSION`，不透露凭证细节 |
| `ready` | `{}`；game_id 和 request_id 均 null；局中不允许 |
| `ready_result` | `ready:boolean`；已 ready 的重复请求仍返回 true |
| `gamerule` | `rule_version:string`、`rules:object`（见下表）、`limits:object`（见下表）、`description:string`（规则中文说明） |
| `game_start` | `players:PlayerState[9]`、`your_player_id:integer` |
| `role` | `role:Role`、`camp:Camp` |
| `werewolves_info` | `players:integer[3]`，包含本人及另外两狼 |
| `phase_changed` | `period:"night或day或ended"`、`day:integer`、`night:integer`、`alive_players:integer[]`、`speech_order:integer[]`；夜间 order 为空；首夜 day=0、night=1；首日 day=1、night=1 |

`gamerule.rules` 必须完整提供以下字段；v1 仅支持表中固定规则，改变规则需另发规则版本并确认客户端支持：

| 字段 | 类型／默认值 |
|---|---|
| `role_counts` | object：`{"werewolf":3,"villager":3,"seer":1,"witch":1,"hunter":1}` |
| `win_mode` | `"kill_all_wolves_or_eliminate_villagers_or_specials"` |
| `win_check_order` | `["resolve_hunter","wolves_eliminated","villagers_eliminated","specials_eliminated"]` |
| `sheriff_enabled` / `wolf_self_destruct_enabled` / `wolf_chat_enabled` | boolean，均 false |
| `wolf_max_rounds` | integer，3 |
| `wolf_vote_policy` | `"unique_plurality_else_revote_final_random_top"` |
| `wolf_all_abstain_policy` | `"revote_then_no_kill"` |
| `wolf_allow_self_target` / `wolf_allow_teammate_target` / `wolf_allow_no_kill` | boolean，均 true |
| `witch_self_save` | `"every_night"` |
| `witch_max_potions_per_night` | integer，1 |
| `witch_poison_self_allowed` | boolean，false |
| `witch_kill_visibility` | `"while_antidote_available"` |
| `seer_repeat_allowed` | boolean，true |
| `hunter_poison_blocks_shot` | boolean，true |
| `day_vote_policy` | `"one_pk_revote_then_no_exile"` |
| `last_words_policy` | `"first_night_and_exile_unless_game_ended"` |
| `speech_order_policy` | `"random_first_day_rotate_start_keep_direction"` |

`limits` 字段：`action_timeout_ms:60000`、`speech_max_codepoints:500`、`max_days:30`、`max_message_bytes:1048576`。赛事可在开局前调整 limits，同局冻结。发言按 Unicode 码点计数，不按字节或 UTF-16 单元计数。

开局顺序：所有实例 ready → 分配座位 → gamerule → game_start → role → 狼人私有 werewolves_info → 首夜 phase_changed。按连接顺序发送；客户端此时不需要额外 ACK。开局后连接中断按既定超时处理。终局消耗 ready 状态，需要再次 ready 才能加入下一局。

## 7. 行动请求字段

本节每条消息都有非空 `request_id`，content 均必须附 `deadline_at:string`（UTC RFC3339）。服务器创建请求即开始计时，重连和纠错不重置；是否超时以服务器接收完整消息的时间为准，恰好等于 deadline 视为超时。客户端时钟不作为裁判依据。

| type | 除 deadline_at 外的 content 字段 |
|---|---|
| `werewolves_act` / `werewolves_revote` | `night:integer`、`round:1..3`、`max_rounds:3`、`previous_votes:VoteRecord[]`、`legal_targets:integer[]`（含 0）、`allow_abstain:true`；第一轮 previous_votes=[] |
| `witch_act` | `night:integer`、`antidote_remaining:0或1`、`poison_remaining:0或1`、`kill_target_visible:boolean`、`kill_target:integer或null`、`legal_actions:string[]`、`legal_save_targets:integer[]`、`legal_poison_targets:integer[]` |
| `seer_act` | `night:integer`、`legal_targets:integer[]`（其他入夜存活玩家） |
| `hunter_act` | `trigger:"night或exile"`、`legal_targets:integer[]`（死亡结算后其他存活者）、`allow_pass:true` |
| `speech` | `day:integer`、`kind:"normal或pk"`、`order:integer[]`、`max_codepoints:integer` |
| `speech_dying` | `day:integer`、`night:integer`、`kind:"last_words"`、`max_codepoints:integer` |
| `vote` | `day:integer`、`round:1或2`、`candidates:integer[]`、`legal_targets:integer[]`（包含 0，不含本人）、`allow_abstain:true` |

女巫的 `legal_actions` 始终含 pass；有药且有刀口时才含 save；有毒药且存在合法目标时才含 poison。没有解药时 kill_target_visible=false、kill_target=null、legal_save_targets=[]，不能通过其他字段泄露刀口；有解药但空刀时 visible=true、target=null。解药最多救当前刀口，即使目标为女巫自己。药剂均耗尽时仍可发请求，但只允许 pass。

白天首轮 candidates 为所有存活玩家，个体 legal_targets 排除本人并加 0；PK 只向非候选的存活者请求，目标为全部平票候选加 0。若没有合法投票者，直接宣布不放逐。

SDK 从此前消息构建完整可见历史，将历史及当前请求交给 Agent；线上普通行动请求不重复携带历史。客户端必须能通过第 11 节 state_sync 恢复，不依赖某个隐藏的本地状态才能理解请求。

## 8. 客户端动作、确认和错误

### 8.1 action.content

| 对应请求 | action | 其他字段 | 约束 |
|---|---|---|---|
| 狼人刀票 | `kill_vote` | `target:integer或null` | 整数必须在 legal_targets；null 弃票 |
| 女巫救人 | `save` | `target:integer` | 在 legal_save_targets |
| 女巫毒人 | `poison` | `target:integer` | 在 legal_poison_targets |
| 女巫／猎人放弃 | `pass` | 无 | 不允许附 target |
| 预言家查验 | `inspect` | `target:integer` | 在 legal_targets；不能主动 pass |
| 猎人开枪 | `shoot` | `target:integer` | 在 legal_targets |
| 发言／遗言 | `speak` | `text:string` | 非纯空白且不超 max_codepoints，按纯文本处理 |
| 白天投票 | `vote` | `target:integer` | 在 legal_targets，0 弃票 |

### 8.2 接受与重复

`action_ack.content`：`status:"accepted或already_accepted"`、`accepted_action:object`（与合法 action.content 相同）。

- 每个 request_id 只接受一个有效动作。接受后立即 ACK，但多人同时行动必须等屏障收齐／到期后再公布结果。
- 同一请求重发相同动作，返回 already_accepted 和原动作，不重复执行。连接重连后也适用；终局后可通过最终快照查询原回执。
- 同一请求试图改动作，返回 ACTION_ALREADY_ACCEPTED，不能覆盖。
- 不合法的动作不占用请求，若尚未到期可修正，用新的 message_id、原 request_id 提交。
- 服务器必须将接受记录和业务变更原子化持久保存，再发 ACK，避免重启后重复消耗药剂或执行开枪。

### 8.3 action_error.content

字段：`code:string`、`message:string`（简短人类说明）、`retryable:boolean`、`deadline_at:string或null`。request_id 仅在能安全关联本人的请求时填值，否则 null。

| code | 含义 | 可重试 |
|---|---|---|
| `INVALID_JSON` | JSON 解析失败或重复键 | 纠正消息；能否补交取决于原请求期限 |
| `INVALID_MESSAGE` | 字段、类型、额外字段或消息类型错误 | 同上 |
| `UNSUPPORTED_VERSION` | 不支持的协议版本 | 否，需更换客户端 |
| `NOT_AUTHENTICATED` | 未认证 | 否，重新认证 |
| `GAME_MISMATCH` | 不是本实例的当前对局 | 否 |
| `UNKNOWN_REQUEST` | 不存在或不属于本实例的请求 | 否，不泄露他人请求 |
| `REQUEST_EXPIRED` | 行动已过期 | 否 |
| `REQUEST_CLOSED` | 请求已取消或游戏结束 | 否 |
| `ACTION_ALREADY_ACCEPTED` | 已有另一动作被接受 | 否 |
| `ILLEGAL_ACTION` | 当前阶段不允许此动作 | 截止前是 |
| `ILLEGAL_TARGET` | 目标不在已发送的合法列表 | 截止前是 |
| `NO_ANTIDOTE` / `NO_POISON` | 对应药剂耗尽 | 截止前是 |
| `TEXT_TOO_LONG` / `EMPTY_TEXT` | 发言长度非法 | 截止前是 |
| `NOT_READY_ALLOWED` | 局中不能 ready | 否 |
| `RATE_LIMITED` | 发送频率超限 | 原期限内退避重试 |
| `INTERNAL_ERROR` | 平台故障 | 否，由平台恢复或中止对局 |

非法 JSON 无法关联请求时，retryable=false、deadline_at=null；这只表示该错误消息不能直接关联重试，不自动关闭尚有效的行动请求。服务器错误文本不能包含隐藏角色、他人私有动作、token 或堆栈。

建议限制每连接每秒最多 10 条应用消息，允许瞬时 20 条；心跳不计入。限流不延长行动预算。

### 8.4 request_closed.content

字段：`reason:"timeout或game_ended或platform_abort"`、`default_action:object或null`。正常接受的请求以 ACK 结束，不再额外发送 request_closed。

| 请求 | 超时默认动作 |
|---|---|
| 狼刀 | `{"action":"kill_vote","target":null}` |
| 女巫／猎人 | `{"action":"pass"}` |
| 预言家 | null，跳过查验 |
| 发言／遗言 | null，生成空文本 skipped 公告 |
| 白天票 | `{"action":"vote","target":0}` |

超时关闭消息先发送给本人，再执行默认值及后续阶段结果。非法回复直到到期仍未纠正的，按超时兜底。平台中止／终局取消时 default_action=null，不执行默认动作。

## 9. 角色私有结果字段

| type | content 字段 |
|---|---|
| `werewolves_result` | `night:integer`、`round:integer`、`votes:VoteRecord[]`、`top_choices:integer[]`、`decision:"revote或kill或no_kill"`、`selected_target:integer或null`、`selection_method:"pending或unique_plurality或random_tie或all_abstain"` |
| `witch_result` | `night:integer`、`action:"save或poison或pass"`、`target:integer或null`、`antidote_remaining:0或1`、`poison_remaining:0或1` |
| `seer_result` | `night:integer`、`status:"checked或skipped"`、`target:integer或null`、`alignment:"werewolf或good或null"`（null 为 JSON null） |
| `hunter_result` | `action:"shoot或pass"`、`target:integer或null` |

werewolves_result 是组结果，request_id=null；各人的完成状态由 ACK 或 request_closed 确定。其他角色结果关联本人 request_id。revote 时 selected_target=null；最终空刀 selected_target=0；kill 为 1～9。top_choices 无有效票时为空，否则包括所有最高票选项，可能含 0。

每轮必发 werewolves_result；只有 decision=revote 时才随后发 werewolves_revote。前两轮全弃票时 method=pending，第三轮全弃票时 method=all_abstain。最终随机是对 top_choices 等概率抽样，裁判记录内部随机选择；运行中不公开可预测未来身份或随机结果的种子。

女巫结果表示操作已接受并扣药，不保证目标最终存活；同夜中毒不能被解药抵消。预言家超时的 target、alignment 均为 null。猎人私有结果不直接改变客户端公开存活表，等 death 公告。

## 10. 公开结果字段

| type | content 字段 |
|---|---|
| `death` | `period:"night或day"`、`day:integer`、`night:integer`、`source:"night_resolution或exile或hunter"`、`players:integer[]` |
| `hunter_shoot` | `hunter:integer`、`target:integer` |
| `speech_public` | `speaker_id:integer`、`kind:"normal或pk或last_words"`、`text:string`、`status:"spoken或skipped"` |
| `vote_result` | `day:integer`、`round:1或2`、`votes:VoteRecord[]`、`top_candidates:integer[]`、`decision:"pk或exile或no_exile"`、`exiled_player:integer或null` |
| `game_end` | `outcome:"good_win或werewolves_win或draw或aborted"`、`reason:string`、`roles:object[]`（每项 player_id、role）、`day:integer`、`night:integer` |

夜间 death 每夜必须发送一次，players=[] 表示平安夜；不公开各人的具体死因。放逐无人死亡不额外发空 death，vote_result 已表达结果。猎人开枪先发 hunter_shoot，再发 source=hunter 的 death；只有 death 修改公开 alive 状态，已死亡者不重复列入。

白天全弃票：top_candidates=[]、decision=no_exile。首轮有效票最高并列：decision=pk；第二轮仍并列：decision=no_exile。唯一最高票：decision=exile、exiled_player 为该玩家，随后发 death。

正常终局 reason 为 `all_wolves_dead`、`all_villagers_dead` 或 `all_specials_dead`；draw 原因为 `max_days`；aborted 原因为 `platform_error` 或 `organizer_abort`。若平民与神职都清空且仍有狼，固定记录 all_villagers_dead。所有终局均揭示角色；中止对局不恢复继续用于正式评测。

公开消息 request_id=null，不把某个玩家的私有请求编号广播。speech_public 的跳过文本为 ""，不把异常堆栈或模型错误公开。

## 11. 断线重连与状态同步

客户端重新连接后再次 auth。认证成功且已分配对局时，服务端必须立即发送 state_sync；连接不变但本地发现 seq 缺口时，客户端发送 sync。先恢复状态，再把未完成请求交给 Agent。

`sync.content`：`last_seq:integer`（无记录为 0），game_id 为当前对局，request_id=null。v1 始终返回完整快照；last_seq 仅供诊断，不采用增量补发。

`state_sync.content` 字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `through_seq` | integer | 快照已包含到该玩家的哪个序号 |
| `rule_version` | string | 当前规则版本 |
| `rules` / `limits` | object | 与 gamerule 一致 |
| `status` | `running或ended` | 对局状态 |
| `paused` | boolean | 是否暂停；暂停时不执行 pending_request |
| `period` | `night或day或ended` | 只给公开阶段，不暴露其他角色正在行动 |
| `day` / `night` | integer | 当前回合计数 |
| `players` | PlayerState[] | 当前公开存活表 |
| `self` | object | player_id、role、camp |
| `private_state` | object | 见下文角色专属字段 |
| `history` | object[] | 截至 through_seq 本人收到的局内服务端信封，按 seq 排序；不含快照、认证消息和其他人的事件 |
| `pending_request` | object/null | 唯一尚未接受且未到期的完整行动请求，保留原 request_id、seq、deadline；无则 null |
| `action_receipts` | object[] | 本局已接受动作的 request_id、accepted_action；不含他人动作 |
| `result` | object/null | 未结束为 null，结束后为 game_end.content |

private_state：狼人为 `{"werewolf_players":[...]}`；女巫为 `{"antidote_remaining":1,"poison_remaining":1}`；预言家为 `{"checks":[CheckRecord,...]}`；村民和猎人为 `{}`。女巫刀口仅出现在合法的行动请求及其可见历史中，不在这里增加额外可见渠道。

快照必须按一个一致性边界生成：先排定 through_seq，发送快照后，再发送 seq 更大的新事件。SDK 用快照替换本地游戏视角，后续丢弃 seq≤through_seq 的重复事件。history 里的旧请求只用于历史，不触发执行；仅 pending_request 允许重新调用 Agent。

若动作在断线前已经被接受，快照包含对应回执，pending_request=null；若仍待提交，客户端可提交或重传原动作。若已超时，不补做动作。终局记录至少保留 24 小时用于恢复和调试；实例被分配下一局后不接受上一局的行动。

## 12. 确定性事件推进顺序

### 12.1 夜间

1. 冻结入夜存活名单，公开 phase_changed(period=night)。
2. 向存活狼人并行发送第 1 轮刀票请求，截止时间相同。
3. 暂存各票，仅向本人 ACK；全收齐或截止后统一公布私有 werewolves_result。
4. 若重投，发送下一轮请求；最多 3 轮，确定刀口或空刀。
5. 向入夜存活女巫请求，接受／默认动作，发送私有结果。
6. 向入夜存活预言家请求，发送私有结果。
7. 一次性计算 `(刀口且未被救) ∪ 毒药目标` 的死亡集合，再统一更新存活状态。
8. 公开 phase_changed(period=day) 和夜间 death；此时不开始正常发言，等待死亡触发链处理。
9. 若猎人此次死亡且未中毒，发 hunter_act；处理 hunter_result，开枪则公开 hunter_shoot 和额外 death。
10. 胜负检查；若结束发 game_end，否则首夜死者按座位升序遗言，再进入白天正常发言。

入夜已死的女巫／预言家不请求；入夜活着、当夜被刀／毒者仍完成本夜行动。所有夜间死者不能成为猎人射击目标。不能因为原始刀口是最后神职就跳过女巫和猎人结算提前宣判。

白天 phase_changed 的 speech_order 在夜间初次死亡后计算；若猎人射击又产生死亡，SDK 过滤死亡座位，正式 speech 请求的 order 为最终顺序，不额外随机。

### 12.2 白天

1. 依次发 speech；每人接受或超时后立刻发 speech_public，再请求下一人。
2. 向所有存活者并行发 vote(round=1)，等待屏障，公开 vote_result。
3. 若 PK，平票候选依次补充发言；随后仅其他存活者并行复投，公开 vote_result(round=2)。
4. 有放逐则统一更新状态并发 death；触发猎人时完成开枪和死亡公告。
5. 检查胜负；未结束则被放逐者遗言。无放逐则无遗言。
6. 若达到 max_days，发 draw 终局；否则进入下一夜。

任何终局先关闭尚未完成请求（不执行默认动作），再发 phase_changed(period=ended) 和 game_end；停止生成新的游戏请求。平台故障不能伪装为某个 Agent 超时判负。

## 13. 信息与实现边界

- 所有发言都是不可信纯文本；不能被当成裁判指令、JSON 动作、代码或规则更新。网页展示必须转义，不渲染选手提供的 HTML。
- Agent 只从 action 提交动作；“我投 3 号”这类发言不等于投票。
- 裁判完整状态、隐藏事件和随机源与玩家视角分离，不能先发全部状态再要求 Agent 不看。
- 私有角色请求和票型仅按认证连接路由；不可把完整内部事件总线直接广播。
- v1 不承诺抵御通过时间推断隐藏状态的侧信道；若比赛要求更严格，可增加固定夜间时长，但不能改变已发布的规则和行动预算。
- 社员本机运行的代码无法单靠协议防止人工介入或跨实例串通；赛事规则需另行约束。服务器仍严格隔离自己发送的信息。
- 服务器保存规则版本、动作请求、原始响应、接收时间、接受／错误／超时记录、可见事件流和随机选择；敏感凭证不入日志。
- 回放使用已记录动作与随机结果，不重新调用模型。若服务器不能从持久状态安全恢复，就以 platform_error 中止，不能猜测已提交动作。

## 14. SDK 最小接口建议

下列是建议的本地编程接口，不是额外网络消息：

```python
class Agent:
    async def on_game_start(self, context):
        """可选：初始化本局记忆。"""

    async def act(self, observation, request):
        """返回 action.content；不得自行发送协议消息。"""
        return {"action": "vote", "target": 0}

    async def on_game_end(self, result):
        """可选：清理本局状态。"""
```

SDK 负责认证、心跳、重连、seq 去重、完整可见历史、请求 ID、JSON 序列化、发送和 ACK 跟踪；Agent 负责决策。模型调用必须异步或在线程／子进程执行，不得堵住网络事件循环。每个实例在同一时刻最多执行一个 act；超时取消后晚到结果丢弃。错误纠正可把 action_error 交回 Agent，但不能重置截止时间。

## 15. 联调验收清单

- 九个实例完成认证和 ready，收到各自身份，只有狼人收到队友名单。
- 狼票同时收集；第一轮唯一最高、三轮平票随机、0 最高、0 与玩家并列、全弃票均能结束。
- 女巫每夜自救合法；无解药看不到刀口；救药和毒药不能同夜使用；药剂重复请求不重复消耗。
- 夜间双死统一结算；猎人被毒不开枪；最后猎人带走最后狼人按规则判好人胜。
- 白天正常放逐、PK、PK 再平票、全弃票和无合法 PK 投票者均能推进。
- 发言／遗言的先后、长度、死亡资格和终局取消符合规则。
- 非法 JSON、额外字段、未知请求、非法目标、超时、重复提交都不会卡住对局。
- 断线发生在提交前、提交后 ACK 前、截止后，恢复结果均不重复执行动作。
- 玩家 history、错误和 seq 不包含其他角色的隐藏事件。
- 平台重启可安全恢复，或明确 aborted；完整事件日志可重放相同终局。

## 16. 与初始消息草案的对应

保留 gamerule、role、werewolves_info、werewolves_act、werewolves_result、witch_act/result、seer_act/result、hunter_act/result、death、hunter_shoot、speech、speech_public、vote、vote_result、speech_dying、game_end。

将 werewolves_discuss 改为 werewolves_revote；所有角色 error 合并为 action_error；增加 auth/auth_result、ready/ready_result、game_start、phase_changed、action/action_ack、request_closed、sync/state_sync。原先含糊的 id 拆成 agent_id、game_id、player_id、request_id 和 message_id；元组票型改为合法 JSON 对象数组。

## 17. 协议 1.1：主持人暂停／继续

游戏规则版本仍为 `9p-seer-witch-hunter/1.0`。新增通信字段需要双方升级至协议 1.1；旧 1.0 客户端认证返回 UNSUPPORTED_VERSION。

- 主持人调用 `POST /api/games/pause` 或 `/api/games/resume`，使用管理 Bearer token；普通 Agent 没有该权限。
- 只允许对进行中的比赛操作；未开局／已结束返回 HTTP 409。重复暂停或重复继续幂等，不重复增加时间。
- 裁判广播 game_control，content 为 `{"paused":true,"reason":"host"}` 或 `{"paused":false,"reason":"host"}`；不含隐藏阶段信息。
- 暂停立即阻止新请求和后续结算，并冻结每个未完成请求的剩余时间。WebSocket 心跳和重连保持正常。
- 已接受动作和回执保留。暂停期间提交新的动作返回 GAME_PAUSED（retryable=false）；不触发默认动作。Agent 等恢复快照后再提交。
- SDK 收到暂停后取消尚未完成的本地 act。第三方模型已经消耗的推理费用不能撤销；继续后可使用缓存动作，或按剩余预算重新决策。
- 继续时只给未完成请求重设 deadline_at：服务器当前时间 + 暂停时剩余时间。request_id 不变，已接受动作不重做。
- 广播恢复消息后，服务器向各玩家发送 state_sync，其中 paused=false，pending_request 含更新后的截止时间。历史中的初始请求不被改写；恢复后的 pending_request 为有效版本。
- 暂停期间重连得到 paused=true 的完整快照，不执行请求；继续后自动收到新快照。普通网络断线仍不延长期限。
- game_control 的 envelope.seq 按每位玩家各自可见流递增，并进入回放日志。公开看板能知道比赛暂停，但不能知道具体角色的请求。
- 主持人可以在暂停期间中止比赛。服务端重启后的未完成比赛仍统一 aborted，不自动续跑。
- 前文“纠错或重连不延长原截止时间”的规则不变；只有显式主持人暂停会按实际暂停时长顺延未完成请求。

## 18. 主持人步进推进（不改变 Agent 协议）

步进模式只增加主持人 HTTP 接口，Agent 侧的消息、字段、期限计算和超时语义与协议 1.1 完全一致，客户端无需升级。

- 推进方式有两种，开局由配置 `step_mode` 决定，主持人可随时切换：`manual` 每一步都等主持人放行，`auto` 由裁判自行放行。`step_interval_ms` 默认 1000，两种方式都保证两步之间至少间隔这么久。
- 步骤边界：入夜、狼人刀票、女巫行动、预言家查验、夜间结算、猎人开枪、每位发言者（白天发言／PK 发言／遗言）、放逐投票、平票复投、放逐结算。
- `POST /api/games/step` 放行一步，返回 `{"accepted":bool,"reason":"released|queued|auto|idle","mode":...,"waiting":...}`。等待中的步骤最多缓存一个，多余的放行返回 `reason:"queued"` 并丢弃，保证任何时刻不会有两个步骤并行请求 Agent；自动模式下返回 `reason:"auto"`，因为放行由裁判自己完成。
- `POST /api/games/mode` 切换推进方式，请求体为 `{"mode":"auto|manual"}`；比赛未开始或已结束返回 409，未知取值返回 400。切换记录为公开事件 `step_control`，content 为 `{"mode":"auto|manual","reason":"host"}`，Agent 可忽略。
- 行动期限仍是每步 `action_timeout_ms`（默认 60 秒），从请求创建时开始计算，与是否步进无关。
- 步骤名会暴露角色是否存活，因此只出现在主持人视图 `step` 字段（`mode`、`interval_ms`、`current`、`waiting`、`queued`），公开视图不含该字段。
- 暂停优先于步进：暂停期间放行不会让对局继续，恢复后按原剩余期限执行；切回手动不会取消已经在途的一步，它完成后停在下一个步骤边界。终局后放行返回 `reason:"idle"`，未使用的放行被清空。
- 服务端重启仍把未完成比赛标记为 aborted，推进方式不跨重启恢复。

## 19. HTTP 管理接口与部署要点（主办方）

这一节记录主持人用的 HTTP 接口与部署/运维要点，Agent 侧不需要实现任何一条。
通用约定：所有管理接口使用 `Authorization: Bearer <admin_token>`（凭证不放 URL）；
只对进行中的对局有效的接口在未开局／已结束时返回 HTTP 409；浏览器只把凭证保存在当前标签页的
sessionStorage 里，退出主持人即清除。客户端须使用协议 1.1，旧版本认证返回 UNSUPPORTED_VERSION。

| 接口 | 权限 | 用途 |
|---|---|---|
| `GET /` | 公开 | 看板页面 |
| `GET /health` | 公开 | 健康检查，返回 `{"status":"ok","protocol_version":"1.1"}` |
| `GET /api/state` | 公开／主持人 | 当前视图；附 Bearer token 得到完整视图 |
| `GET /ws/watch` | 首条消息认证 | `{"token":""}` 公开视图；`{"token":"主持人凭证"}` 完整视图 |
| `GET /ws/agent` | Agent 凭证 | 参赛 WebSocket，见第 2–11 节 |
| `POST /api/games/start` | 主持人 | 九人就绪后开局 |
| `POST /api/games/step` | 主持人 | 放行一步；返回 `accepted` 与等待中的步骤名 |
| `POST /api/games/mode` | 主持人 | `{"mode":"auto"}` 自行推进；`{"mode":"manual"}` 回到逐步放行 |
| `POST /api/games/pause` | 主持人 | 暂停：冻结未完成行动的剩余时间并停止结算（第 17 节） |
| `POST /api/games/resume` | 主持人 | 继续：保留已接受动作，按剩余时间恢复 |
| `POST /api/games/abort` | 主持人 | 中止并标记 aborted，不判任何阵营获胜 |
| `GET /api/replays` | 主持人 | 最近 100 局列表 |
| `GET /api/replays/{game_id}` | 主持人 | 完整快照、可见事件流、动作回执与日志 |

部署与运维要点：

- **单进程单房间**：同一时刻只能有一局在跑，不能让多个进程共同读写同一场对局；反向代理可以做 TLS，
  但裁判进程保持一个。局域网用 `--host 0.0.0.0` 并放行端口；公网用反向代理提供 HTTPS/WSS，
  代理需支持 WebSocket 升级、空闲超时大于心跳周期，不要在公网直接暴露明文 ws。
- **凭证**：`--init` 生成 `runtime/config.json`（全部凭证与设置）与 9 份 `runtime/agent-0X.json`
  （一对一发放）。已有配置时拒绝覆盖；`config.json` 与数据库不得发给参赛者。要换凭证必须先把旧文件
  移走再 `--init`，旧 token 立即失效。
- **持久化**：SQLite 使用 WAL 与 FULL 同步，裁判在 ACK 前保存已接受的动作意图。每个事件与终局都入库，
  默认不自动清理（满足终局至少 24 小时的恢复要求），由主办方自行备份。
- **重启**：服务端重启不续跑未完成比赛，统一标记为 platform_error / aborted（第 13 节）。
- **清理历史**：停掉裁判后删除 `matches.sqlite3`、`-wal`、`-shm` 三个文件即可（凭证不受影响）；
  不要只删 `-wal`/`-shm`，它们是尚未写回主库的已提交数据。
