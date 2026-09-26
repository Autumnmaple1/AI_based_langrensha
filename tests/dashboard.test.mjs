import {test} from "node:test";
import assert from "node:assert/strict";
import {actionText,describe,replayView,target} from "../werewolf/static/view-model.mjs";

test("wolf no-kill is distinct from abstention and daytime pass",()=>{
  assert.equal(target(0,true),"空刀");assert.equal(target(null,true),"弃票");assert.equal(target(0,false),"弃权");
});
test("pass potion result does not appear as a vote",()=>{
  assert.equal(actionText({action:"pass",target:null}),"不使用技能");
});
test("untrusted speech remains literal text",()=>{
  assert.equal(actionText({action:"speak",text:"<script>alert(1)</script>"}),"<script>alert(1)</script>");
});
test("empty death is a peaceful night",()=>{
  assert.equal(describe({type:"death",content:{players:[]}}),"平安夜，无人死亡");
});
test("random final tie is explicitly labeled",()=>{
  assert.match(describe({type:"werewolves_result",content:{votes:[{voter:1,target:0}],decision:"no_kill",selected_target:0,selection_method:"random_tie"}}),/随机选择/);
});
const data={game:{id:"g",roles:{1:"werewolf",2:"villager"},alive:[1],result:{outcome:"werewolves_win"}},seats:{1:"a",2:"b"},events:[
  {type:"phase_changed",content:{day:0,night:1,period:"night",alive_players:[1,2]}},
  {type:"werewolves_act",request_id:"r",recipients:[1],content:{deadline_at:"2026-09-23T00:00:00Z"}},
  {type:"action_ack",request_id:"r",content:{accepted_action:{action:"kill_vote",target:2}}},
  {type:"death",content:{players:[2]}},
  {type:"game_end",content:{outcome:"werewolves_win"}}
]};
test("replay before death does not inherit future final state",()=>{
  const view=replayView(data,3);
  assert.equal(view.game.players.filter(p=>p.alive).length,2);assert.equal(view.game.result,null);
  assert.equal(view.requests[0].state,"accepted");assert.equal(view.game.phase,"wolf_vote");
});
test("replay playhead reconstructs pending then accepted action",()=>{
  assert.equal(replayView(data,2).requests[0].state,"pending");
  assert.deepEqual(replayView(data,3).requests[0].action,{action:"kill_vote",target:2});
});
test("replay at the end reconstructs death and result",()=>{
  const view=replayView(data,5);
  assert.deepEqual(view.game.players.filter(p=>p.alive).map(p=>p.player_id),[1]);
  assert.equal(view.game.result.outcome,"werewolves_win");assert.equal(view.game.phase,"ended");
});

test("replay reconstructs pause and resume without changing the phase",()=>{
  const paused={...data,events:[...data.events.slice(0,2),{type:"game_control",content:{paused:true}}]};
  assert.equal(replayView(paused,3).game.paused,true);
  assert.equal(replayView(paused,3).game.phase,"wolf_vote");
  paused.events.push({type:"game_control",content:{paused:false}});
  assert.equal(replayView(paused,4).game.paused,false);
});

test("step mode switch is described for the public feed",()=>{
  assert.equal(describe({type:"step_control",content:{mode:"auto"}}),"主持人切换到自动推进。");
  assert.equal(describe({type:"step_control",content:{mode:"manual"}}),"主持人切换回手动放行。");
});
