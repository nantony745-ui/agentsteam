const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const {environment} = require('./frontend_dom.cjs');

async function stream(team, baseline) {
  const env = environment();
  if (team) env.run(fs.readFileSync('app/static/team-conversation.js','utf8'));
  env.run(`
    let now=0,sequence=0;const jobs=new Map();
    setTimeout=(callback,delay)=>{jobs.set(++sequence,{callback,due:now+delay});return sequence;};
    clearTimeout=id=>jobs.delete(id);
    requestAnimationFrame=callback=>setTimeout(callback,16);cancelAnimationFrame=clearTimeout;
    globalThis.advance=ms=>{const end=now+ms;while(true){const next=[...jobs].sort((a,b)=>a[1].due-b[1].due)[0];if(!next||next[1].due>end)break;jobs.delete(next[0]);now=next[1].due;next[1].callback();}now=end;};
    globalThis.parsed=0;globalThis.renders=0;globalThis.finishing=false;
    const originalRender=renderMarkdown;
    renderMarkdown=(element,text)=>{parsed+=text.length;renders++;if(finishing)originalRender(element,text);};
  `);
  if (baseline) env.run(`scheduleTextFlush=(target,flush)=>{if(target.textTimer!==null||target.textFrame!==null)return;target.textTimer=setTimeout(()=>{target.textTimer=null;target.textFrame=requestAnimationFrame(flush);},100);};`);
  if (team) env.run(`
    globalThis.sessionStorage={getItem(){return null;},setItem(){},removeItem(){}};
    setInterval=()=>1;clearInterval=()=>{};
    globalThis.Option=function(text,value){const node=document.createElement('option');node.textContent=text;node.value=value;return node;};
    TeamConversation.prototype.load=async function(){this.loading=false;};
    const owner={project:{team:{leader_id:'e'}},roster:{sessions:[]},activate(){}};
    globalThis.pane=new TeamConversation({id:'s',harness:'codex'},{profile:{employee_id:'e',name:'员工'}},owner);
    api=async()=>({files:[]});
  `);
  else env.run(`state.context={sessionId:'s',projectId:'p'};state.runRecords.set('r',{id:'r',status:'running'});ensureRun('r');`);
  await env.run(`(async()=>{for(let id=1;id<=1000;id++){
    const row={id,session_id:'s',run_id:'r',kind:'agent.text',payload:{channel:'message',text:'正文'.repeat(128)}};
    ${team ? 'await pane.apply(row);' : 'appendText(row,state.runs.get("r"));'}advance(20);
  }
  globalThis.finishing=true;
  const tail={id:1001,session_id:'s',run_id:'r',kind:'agent.text',payload:{channel:'message',text:String.fromCharCode(10,10)+'**END**'}};
  ${team ? "await pane.apply(tail);await pane.apply({id:1002,session_id:'s',run_id:'r',kind:'run.status',payload:{status:'completed'}});" : "appendText(tail,state.runs.get('r'));sealText('r');"}
  })()`);
  const body = env.run(team ? 'pane.runs.get("r").segments[0].body.innerHTML' : '[...state.textBlocks.values()][0].body.innerHTML');
  assert.match(body, /<strong>END<\/strong>/);
  assert.equal(env.run(team ? 'pane.runs.get("r").segments[0].text' : '[...state.textBlocks.values()][0].text'),undefined);
  return {parsed:env.run('parsed'),renders:env.run('renders')};
}

for (const team of [false,true]) test(`${team?'团队窗':'主会话'}长正文降低累计解析量，终态立即完整渲染并释放源文本`, async () => {
  const baseline=await stream(team,true),adaptive=await stream(team,false);
  assert.ok(adaptive.parsed < baseline.parsed/2,JSON.stringify({baseline,adaptive}));
  assert.ok(adaptive.renders < baseline.renders/2,JSON.stringify({baseline,adaptive}));
});
