export const roles = {werewolf:"狼人",villager:"村民",seer:"预言家",witch:"女巫",hunter:"猎人"};
export const phases = {
  setup:"准备开局",night_start:"夜幕降临",wolf_vote:"狼人刀票",witch_action:"女巫行动",
  seer_action:"预言家查验",night_resolution:"夜间结算",hunter_action:"猎人开枪",
  last_words:"遗言阶段",day_speech:"依次发言",day_vote:"放逐投票",pk_speech:"平票发言",
  pk_vote:"PK 复投",day_resolution:"放逐结算",ended:"对局结束",night:"夜间行动",day:"白天讨论"
};
export const names = {
  gamerule:"规则已载入",game_start:"玩家入席",role:"身份分配",werewolves_info:"狼人队伍",
  phase_changed:"阶段变更",werewolves_act:"狼人刀票",werewolves_revote:"狼人重投",
  werewolves_result:"刀票结算",witch_act:"女巫行动",witch_result:"用药结果",
  seer_act:"预言家查验",seer_result:"查验结果",hunter_act:"猎人行动",hunter_result:"猎人决定",
  hunter_shoot:"猎人开枪",death:"死亡公告",speech:"发言请求",speech_dying:"遗言请求",
  speech_public:"玩家发言",vote:"放逐投票",vote_result:"放逐结算",action_ack:"动作已接收",
  action_error:"动作错误",request_closed:"请求关闭",game_end:"终局结算",game_control:"比赛控制",
  step_control:"推进模式"
};
export const outcomes = {good_win:"好人阵营获胜",werewolves_win:"狼人阵营获胜",draw:"对局平局",aborted:"对局已中止"};
export const reasons = {all_wolves_dead:"全部狼人出局",all_villagers_dead:"全部平民出局",all_specials_dead:"全部神职出局",max_days:"达到最大轮数",platform_error:"平台中断",organizer_abort:"主持人中止"};
const requestTypes = new Set(["werewolves_act","werewolves_revote","witch_act","seer_act","hunter_act","speech","speech_dying","vote"]);

export function target(value, wolf=false) {
  return value===null ? "弃票" : value===0 ? (wolf?"空刀":"弃权") : `${value} 号`;
}

export function actionText(action) {
  if (!action) return "跳过行动";
  if (action.action==="speak") return action.text;
  const labels={kill_vote:"刀票",save:"解药",poison:"毒药",inspect:"查验",shoot:"开枪",vote:"放逐票",pass:"不使用技能"};
  const suffix=action.action!=="pass" && "target" in action ? ` → ${target(action.target,action.action==="kill_vote")}` : "";
  return `${labels[action.action]||action.action}${suffix}`;
}

export function describe(event) {
  const c=event.content;
  switch(event.type) {
    case "game_control": return c.paused?"主持人暂停了比赛，行动时间已冻结。":"比赛继续，恢复行动计时。";
    case "step_control": return c.mode==="auto"?"主持人切换到自动推进。":"主持人切换回手动放行。";
    case "game_start": return `${c.your_player_id} 号已入席`;
    case "phase_changed": return c.period==="ended"?"本局所有行动已结算":`第 ${c.period==="night"?c.night:c.day} ${c.period==="night"?"夜":"天"} · ${c.alive_players.length} 人存活`;
    case "speech_public": return `${c.speaker_id} 号${c.kind==="last_words"?"遗言":c.kind==="pk"?" PK 发言":""}：${c.status==="skipped"?"跳过发言":c.text}`;
    case "death": return c.players.length?`${c.players.join("、")} 号出局`:"平安夜，无人死亡";
    case "hunter_shoot": return `${c.hunter} 号猎人开枪带走 ${c.target} 号`;
    case "vote_result":
    case "werewolves_result": {
      const decisions={revote:"出现平票，继续刀票",kill:`刀口：${target(c.selected_target,true)}`,no_kill:"今晚空刀",pk:"最高票并列，进入 PK",exile:`放逐 ${c.exiled_player} 号`,no_exile:"本轮无人放逐"};
      return c.votes.map(v=>`${v.voter} → ${target(v.target,event.type==="werewolves_result")}`).join("   /   ")+`\n${decisions[c.decision]||""}${c.selection_method==="random_tie"?"（最终平票随机选择）":""}`;
    }
    case "game_end": return `${outcomes[c.outcome]} · ${reasons[c.reason]||c.reason}`;
    case "action_ack": return actionText(c.accepted_action);
    case "action_error": return `${c.code}${c.retryable?" · 允许在原截止时间前纠正":""}`;
    case "request_closed": return c.reason==="timeout"?`行动超时 · ${actionText(c.default_action)}`:"请求已取消";
    case "role": return roles[c.role];
    case "werewolves_info": return `狼人座位：${c.players.join("、")}`;
    case "seer_result": return c.status==="skipped"?"跳过查验":`${c.target} 号：${c.alignment==="werewolf"?"狼人":"好人"}`;
    case "witch_result":
    case "hunter_result": return actionText(c);
    default: return requestTypes.has(event.type)&&event.recipients?.length===1?`等待 ${event.recipients[0]} 号行动`:names[event.type]||event.type;
  }
}

// Reconstruct only events at or before the playhead, not the saved final state.
// Roles are intentionally visible in these host-only replays.
export function replayView(data, position) {
  const events=data.events.slice(0,Math.max(0,position)), g=data.game;
  const players=Object.entries(g.roles).map(([p,role])=>({player_id:Number(p),role,alive:true,connected:false,agent_id:data.seats[p]}));
  const requests=new Map();
  let day=0,night=0,period="night",phase="setup",result=null,paused=false;
  for(const event of events) {
    const c=event.content;
    if(event.type==="phase_changed") {day=c.day;night=c.night;period=c.period;phase=period;}
    if(event.type==="death") for(const player of players) if(c.players.includes(player.player_id)) player.alive=false;
    if(requestTypes.has(event.type)) {
      requests.set(event.request_id,{player_id:event.recipients[0],type:event.type,state:"pending",request_id:event.request_id,deadline_at:c.deadline_at,action:null});
      phase=({werewolves_act:"wolf_vote",werewolves_revote:"wolf_vote",witch_act:"witch_action",seer_act:"seer_action",hunter_act:"hunter_action",speech_dying:"last_words"})[event.type] || (event.type==="vote"?(c.round===2?"pk_vote":"day_vote"):(c.kind==="pk"?"pk_speech":"day_speech"));
    }
    const request=requests.get(event.request_id);
    if(request&&event.type==="action_ack") {request.state="accepted";request.action=c.accepted_action;}
    if(request&&event.type==="request_closed") {request.state=c.reason==="timeout"?"timeout":"cancelled";request.action=c.default_action;}
    if(event.type==="game_control")paused=c.paused;
    if(event.type==="game_end") {result=c;period=phase="ended";paused=false;}
  }
  return {mode:"host",lobby:[],requests:[...requests.values()],events,game:{id:g.id,day,night,period,phase,players,result,paused}};
}
