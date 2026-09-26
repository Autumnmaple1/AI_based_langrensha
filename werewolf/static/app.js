import {roles,phases,names,outcomes,reasons,actionText,describe,replayView,target} from "./view-model.mjs";

const $=id=>document.getElementById(id);
function node(tag,cls,text) {
  const element=document.createElement(tag);
  if(cls)element.className=cls;
  if(text!==undefined)element.textContent=text;
  return element;
}
const significant=new Set(["phase_changed","game_control","speech_public","vote_result","werewolves_result","witch_result","seer_result","hunter_shoot","death","game_end","action_error","request_closed"]);
let token=sessionStorage.getItem("mooncourt-host")||"";
let socket,reconnectTimer,liveState,currentState,replayData=null,replayTimer=null;
let renderedPlayers="",renderedEvents="",renderedFocus="",renderedVotes="",toastTimer;
let serverOffset=0;

function toast(text) {
  $("toast").textContent=text;$("toast").hidden=false;clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>$("toast").hidden=true,4000);
}

function connect() {
  clearTimeout(reconnectTimer);
  if(socket){socket.onclose=null;socket.close();}
  socket=new WebSocket(`${location.protocol==="https:"?"wss:":"ws:"}//${location.host}/ws/watch`);
  socket.onopen=()=>socket.send(JSON.stringify({token}));
  socket.onmessage=event=>{
    liveState=JSON.parse(event.data);
    serverOffset=Date.parse(liveState.server_time)-Date.now();
    $("network").className="network";$("network").setAttribute("aria-label","已连接");
    if(!replayData)render(liveState);
  };
  socket.onclose=event=>{
    $("network").className="network offline";$("network").setAttribute("aria-label","未连接");
    $("start").disabled=true;
    if(event.code===1008){token="";sessionStorage.removeItem("mooncourt-host");leaveReplay();toast("主持人凭证已失效");}
    reconnectTimer=setTimeout(connect,2000);
  };
}

function renderPlayers(players,pending,game) {
  const signature=JSON.stringify([players,pending,game?.id,game?.paused,!!replayData]);
  if(signature===renderedPlayers)return;
  renderedPlayers=signature;
  $("players").replaceChildren(...players.map(player=>{
    const req=pending.find(r=>r.player_id===player.player_id);
    const offline=!replayData&&player.connected===false;
    const card=node("article",`player-card${req?" active":""}${!player.alive?" dead":""}${offline?" offline":""}${player.role==="werewolf"?" wolf":""}`);
    const top=node("div","card-top");
    const badge=node("span","card-state");
    if(req&&!replayData) {
      const count=node("span","countdown");
      if(game?.paused)count.textContent=`${Math.ceil((req.remaining_ms||0)/1000)}s`;
      else count.dataset.deadline=req.deadline_at;
      badge.append(count);
    }
    top.append(node("span","seat",String(player.player_id).padStart(2,"0")),badge);
    const bottom=node("div","card-bottom");
    bottom.append(node("span","role",player.role?roles[player.role]:game?"未知":"待入席"),node("span","agent-name",player.agent_id||"—"));
    card.append(top,bottom);return card;
  }));
}

function render(state) {
  currentState=state;
  const game=state.game,host=state.mode==="host",active=game&&!game.result;
  const ready=state.lobby.filter(a=>a.connected&&a.ready).length;
  $("login-button").textContent=host?"退出主持人 ↗":"主持人登录 ↗";
  const title=game?(game.result?"对局结束":`第 ${game.period==="night"?game.night:game.day} ${game.period==="night"?"夜":"天"}`):"等待开局";
  $("round-title").replaceChildren(document.createTextNode(title),node("span","title-dot","."));
  $("stage").textContent=game?(game.paused?`已暂停 · ${phases[game.phase]||game.phase}`:game.result?(reasons[game.result.reason]||game.result.reason):(phases[game.phase]||game.phase)):`${ready} / 9 位 Agent 已就绪`;
  $("start").hidden=!host||!!replayData||!!active;$("start").disabled=ready<9;
  const step=host&&!!active&&!replayData&&state.step?state.step:null,auto=step?.mode==="auto";
  $("step-next").hidden=$("step-toggle").hidden=!step;
  $("step-next").disabled=!!auto;
  $("step-toggle").textContent=auto?"切换手动":"切换自动";
  $("step-toggle").classList.toggle("on",!!auto);
  $("host-menu").hidden=!host;$("abort").hidden=!!replayData||!active;$("export").disabled=!game;
  const pending=(state.requests||[]).filter(r=>r.state==="pending");
  const players=game?.players||Array.from({length:9},(_,i)=>({player_id:i+1,...state.lobby[i],alive:true}));
  renderPlayers(players,pending,game);
  renderFocus(state,pending);renderEvents(state.events);renderVotes(state.events);
  $("diagnostics").hidden=!host;
  if($("diagnostics").open)renderDiagnostics();
  countdowns();
}

function renderFocus(state,pending) {
  const game=state.game;
  const lastPhase=state.events.findLastIndex(e=>e.type==="phase_changed");
  const recent=state.events.slice(Math.max(0,lastPhase));
  const latest=recent.findLast(e=>["speech_public","seer_result","witch_result","werewolves_result","vote_result","hunter_shoot","death"].includes(e.type));
  const step=state.mode==="host"&&state.step?state.step:null;
  const signature=JSON.stringify([game?.id,game?.paused,game?.result,game?.phase,latest,pending.map(p=>[p.player_id,p.type]),state.lobby.filter(a=>a.ready).length,step?.current,step?.waiting]);
  if(signature===renderedFocus)return;
  renderedFocus=signature;
  const panel=document.querySelector(".focus-panel"),content=$("focus-content");
  panel.className="focus-panel";content.className="focus-content";
  $("focus-label").textContent="正在发生";$("focus-time").textContent="";
  let footer=pending.length?`等待 ${pending.map(p=>p.player_id).join("、")} 号回复`:game?"裁判正在推进对局":"九人就绪后即可开局";
  if(step&&game&&!game.result&&!game.paused) {
    if(step.mode==="auto")footer=step.waiting?`自动推进中 · 下一步：${step.waiting}`:"自动推进中…";
    else if(step.waiting){footer=`下一步：${step.waiting}`;panel.classList.add("gated");}
    else if(step.current)footer=`正在执行：${step.current}`;
  }
  else if(game&&!game.result&&!game.paused&&!pending.length&&state.step?.waiting)footer="等待主持人放行下一步";
  $("focus-footer").textContent=footer;
  if(game?.paused) {
    panel.classList.add("paused");
    content.replaceChildren(node("span","focus-symbol","Ⅱ"),node("h2","","对局暂停"),node("p","","当前行动时间已冻结。\n已接受的动作保留，继续后恢复比赛。"));
    $("focus-footer").textContent="等待主持人继续";return;
  }
  if(game?.result) {
    const mark=game.result.outcome==="good_win"?"＋":game.result.outcome==="werewolves_win"?"↗":"—";
    content.replaceChildren(node("span","focus-symbol",mark),node("h2","",outcomes[game.result.outcome]),node("p","",reasons[game.result.reason]||game.result.reason));
    $("focus-footer").textContent="本局已结算，可在历史对局中回放";return;
  }
  if(latest?.type==="speech_public") {
    content.classList.add("has-speech");
    const speaker=node("div","speaker",String(latest.content.speaker_id).padStart(2,"0"));
    speaker.append(node("span","",latest.content.kind==="last_words"?"号 · 遗言":latest.content.kind==="pk"?"号 · PK 发言":"号 · 发言"));
    content.replaceChildren(speaker,node("p","",latest.content.status==="skipped"?"本轮跳过发言。":latest.content.text));
    $("focus-label").textContent="最新发言";
    $("focus-time").textContent=new Date(latest.at).toLocaleTimeString("zh-CN",{hour12:false});return;
  }
  if(pending.length) {
    panel.classList.add("thinking");
    const number=node("span","focus-number",pending.length===1?String(pending[0].player_id).padStart(2,"0"):String(pending.length).padStart(2,"0"));
    number.append(node("span","",pending.length===1?"号":"位"));
    content.replaceChildren(number,node("h2","",names[pending[0].type]||"等待行动"),node("p","",pending.length===1?"Agent 正在作出决定。":`${pending.map(p=>p.player_id).join("、")} 号同时行动，收齐后统一结算。`));return;
  }
  if(latest) {
    content.replaceChildren(node("span","focus-symbol",latest.type==="death"?"☾":"↗"),node("h2","",names[latest.type]),node("p","",describe(latest)));
    $("focus-label").textContent=latest.public===false?"最新结果 · 仅主持人":"最新结果";return;
  }
  const number=node("span","focus-number",game?String(game.period==="night"?game.night:game.day).padStart(2,"0"):"09");
  number.append(node("span","",game?(game.period==="night"?"夜":"天"):"席"));
  content.replaceChildren(number,node("h2","",game?(game.period==="night"?"夜间行动中":"讨论即将开始"):"即将入夜"),node("p","",game?"行动完成后，裁判将公布本轮结果。":"Agent 就绪后，由主持人开始对局。"));
}

function renderEvents(events) {
  let filtered=events.filter(e=>significant.has(e.type));
  const signature=JSON.stringify([currentState.game?.id,currentState.mode,filtered]);
  if(signature===renderedEvents)return;
  renderedEvents=signature;
  filtered=filtered.slice(-80).reverse();
  $("events").replaceChildren(...filtered.map(event=>{
    const major=["phase_changed","game_end","game_control"].includes(event.type);
    const item=node("article",`event${major?" major":""}`);
    item.append(node("span","event-avatar",event.type==="speech_public"?String(event.content.speaker_id).padStart(2,"0"):event.type==="game_control"?(event.content.paused?"Ⅱ":"▶"):"↗"));
    const body=node("div"),title=node("div","event-title"),label=node("span","",names[event.type]||event.type);
    if(event.public===false)label.append(node("span","private-label","仅主持人"));
    title.append(label,node("time","",new Date(event.at).toLocaleTimeString("zh-CN",{hour12:false})));
    body.append(title,node("p","",describe(event)));item.append(body);return item;
  }));
  if(!filtered.length)$("events").append(node("p","empty","暂无相关动态。"));
  $("events").scrollTop=0;
}

function renderVotes(events) {
  const event=events.findLast(e=>["vote_result","werewolves_result"].includes(e.type));
  const signature=JSON.stringify(event||null);if(signature===renderedVotes)return;renderedVotes=signature;
  if(!event){$("vote-round").textContent="尚未投票";$("vote-content").replaceChildren(node("p","empty","投票结束后统一公布。"));return;}
  const c=event.content,wolf=event.type==="werewolves_result",counts=new Map();
  $("vote-round").textContent=`第 ${wolf?c.night:c.day} ${wolf?"夜":"天"} · ${wolf?"刀票":"放逐"}${c.round>1?` / 第 ${c.round} 轮`:""}`;
  for(const vote of c.votes)if(vote.target!==null&&(wolf||vote.target!==0))counts.set(vote.target,(counts.get(vote.target)||0)+1);
  const rows=[...counts].sort((a,b)=>b[1]-a[1]||a[0]-b[0]).map(([player,count])=>{
    const row=node("div","vote-row"),bar=node("progress");bar.max=Math.max(1,c.votes.length);bar.value=count;bar.setAttribute("aria-label",`${target(player,wolf)} ${count} 票`);
    row.append(node("span","",target(player,wolf)),bar,node("strong","",String(count)));return row;
  });
  if(!rows.length)rows.push(node("p","empty","本轮全部弃票。"));
  const summary=describe(event).split("\n").at(-1);
  rows.push(node("div","vote-result",summary),node("div","vote-detail",c.votes.map(v=>`${v.voter} → ${target(v.target,wolf)}`).join("　/　")));
  $("vote-content").replaceChildren(...rows);
}

function renderDiagnostics() {
  if(!currentState)return;
  const requests=currentState.requests||[];
  $("diagnostic-count").textContent=`${requests.length} 个请求`;
  $("actions").replaceChildren(...requests.slice(-9).reverse().map(request=>{
    const item=node("div","request-item");
    item.append(node("strong","",`${request.player_id} 号 · ${names[request.type]}`),node("span","",`${({pending:"等待回复",accepted:"已接受",timeout:"超时",cancelled:"取消"})[request.state]} · ${request.action?actionText(request.action):"—"}`));return item;
  }));
  const opened=new Set([...$("technical-events").querySelectorAll("details[open]")].map(e=>e.dataset.id));
  $("technical-events").replaceChildren(...currentState.events.slice(-80).reverse().map(event=>{
    const details=node("details");details.dataset.id=String(event.index);details.open=opened.has(String(event.index));
    details.append(node("summary","",`${names[event.type]||event.type} · ${event.recipients?.join("、")||"全体"}`),node("pre","",JSON.stringify(event.content,null,2)));return details;
  }));
}

function countdowns() {
  document.querySelectorAll("[data-deadline]").forEach(element=>element.textContent=`${Math.max(0,Math.ceil((Date.parse(element.dataset.deadline)-Date.now()-serverOffset)/1000))}s`);
}
async function api(path,method="GET",body) {
  const headers=token?{Authorization:`Bearer ${token}`}:{};
  if(body)headers["Content-Type"]="application/json";
  const response=await fetch(path,{method,headers,body:body?JSON.stringify(body):undefined});
  if(!response.ok)throw Error(await response.text());return response.json();
}
function stopReplay(){clearInterval(replayTimer);replayTimer=null;$("replay-play").textContent="播放";}
function replayAt(position){$("replay-count").textContent=`${position} / ${replayData.events.length}`;render(replayView(replayData,position));}
function leaveReplay(){stopReplay();replayData=null;$("replay-bar").hidden=true;renderedEvents="";if(liveState)render(liveState);}
async function openReplay(id) {
  try{
    stopReplay();replayData=await api(`/api/replays/${id}`);
    $("replay-position").max=replayData.events.length;$("replay-position").value=replayData.events.length;
    $("replay-bar").hidden=false;$("archive-dialog").close();replayAt(replayData.events.length);
  }catch(error){toast(error.message);}
}
$("login-button").onclick=()=>{
  if(token){token="";sessionStorage.removeItem("mooncourt-host");leaveReplay();connect();}
  else $("login").showModal();
};
$("cancel-login").onclick=()=>$("login").close();
$("login-form").onsubmit=async event=>{
  event.preventDefault();token=$("token").value.trim();
  try{await api("/api/state");sessionStorage.setItem("mooncourt-host",token);$("token").value="";$("login-error").textContent="";$("login").close();connect();}
  catch{token="";$("login-error").textContent="凭证无效，请重试。";}
};
$("start").onclick=async()=>{
  try{$("start").disabled=true;await api("/api/games/start","POST");toast("对局已开始");}
  catch(error){toast(error.message);if(currentState)render(currentState);}
};
// 只有两个主按钮：单击「下一步」放行一步；「切换自动」在手动与自动之间切换。
async function stepAction(path,body) {
  try{await api(path,"POST",body);}
  catch(error){toast(error.message);if(currentState)render(currentState);}
}
$("step-next").onclick=()=>stepAction("/api/games/step");
$("step-toggle").onclick=()=>{
  const mode=currentState?.step?.mode==="auto"?"manual":"auto";
  return stepAction("/api/games/mode",{mode});
};
$("abort").onclick=()=>{$("host-menu").open=false;$("abort-dialog").showModal();};
$("cancel-abort").onclick=()=>$("abort-dialog").close();
$("confirm-abort").onclick=async()=>{
  try{await api("/api/games/abort","POST");$("abort-dialog").close();}catch(error){toast(error.message);}
};
$("diagnostics").ontoggle=()=>{if($("diagnostics").open)renderDiagnostics();};
$("export").onclick=async()=>{
  $("host-menu").open=false;
  try{
    const data=replayData||await api(`/api/replays/${currentState.game.id}`),url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));
    const link=node("a");link.href=url;link.download=`mooncourt-${data.game.id}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(error){toast(error.message);}
};
$("archive-open").onclick=async()=>{
  $("host-menu").open=false;
  try{
    const games=await api("/api/replays");
    $("archive-list").replaceChildren(...games.map(game=>{
      const button=node("button","archive-item");button.append(node("strong","",outcomes[game.result?.outcome]||"进行中"),node("span","",`${new Date(game.updated).toLocaleString("zh-CN",{hour12:false})} ↗`));button.onclick=()=>openReplay(game.game_id);return button;
    }));
    if(!games.length)$("archive-list").append(node("p","empty","还没有历史对局。"));
    $("archive-dialog").showModal();
  }catch(error){toast(error.message);}
};
$("archive-close").onclick=()=>$("archive-dialog").close();
$("exit-replay").onclick=leaveReplay;
$("replay-position").oninput=()=>replayAt(Number($("replay-position").value));
$("replay-play").onclick=()=>{
  if(replayTimer){stopReplay();return;}
  if(Number($("replay-position").value)>=replayData.events.length)$("replay-position").value=0;
  $("replay-play").textContent="暂停";
  replayTimer=setInterval(()=>{
    const position=Number($("replay-position").value)+1;$("replay-position").value=position;replayAt(position);
    if(position>=replayData.events.length)stopReplay();
  },450);
};
setInterval(countdowns,250);connect();
