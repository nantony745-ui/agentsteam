// 真实 Chromium 执行两个提交中的原始 JS；只替换 HTTP/Agent 边界。
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');

const output = path.resolve(process.argv[2] || 'profile_output/p1p2-ab-20261029');
const streamOnly = process.argv.includes('--stream-only');
const chromium = path.join(process.env.LOCALAPPDATA,'ms-playwright/chromium-1228/chrome-win64/chrome.exe');
const sleep = ms => new Promise(resolve=>setTimeout(resolve,ms));
const source = (mode,file) => fs.readFileSync(path.join(output,mode,'app/static',file),'utf8');
const setup = `
  TeamConversation.prototype.load=async function(){this.loading=false;this.input.disabled=false;};
  api=async()=>({files:[]});
  globalThis.owner={project:{team:{leader_id:'e'}},roster:{sessions:[{id:'s',employee_id:'e',title:'bench'}]},activate(){}};
  globalThis.member={profile:{employee_id:'e',name:'fixture'}};
  globalThis.makePane=()=>{
    const pane=new TeamConversation({id:'s',project_id:'p',harness:'codex'},member,owner);
    document.querySelector('#team-workspace').hidden=false;
    document.querySelector('.conversation').hidden=true;
    document.querySelector('#team-pane-grid').append(pane.root);
    return pane;
  };
  globalThis.pane=makePane();
  globalThis.addRuns=async(start,end)=>{
    for(let n=start;n<end;n++){
      const id='r'+n,created_at=new Date(1700000000000+n*10000).toISOString();
      pane.ensureRun(id,{id,session_id:'s',status:'running',created_at,finished_at:new Date(1700000005000+n*10000).toISOString()});
      await pane.apply({id:n*4+1,session_id:'s',run_id:id,created_at,kind:'user.message',payload:{message:'benchmark '+n}});
      await pane.apply({id:n*4+2,session_id:'s',run_id:id,created_at,kind:'agent.tool',payload:{toolCallId:'tool-'+n,title:'读取文件',kind:'read',status:'completed'}});
      const text=Array.from({length:64},(_,i)=>'paragraph '+n+'/'+i+' '+('content '+n+' ').repeat(12)).join(String.fromCharCode(10,10));
      await pane.apply({id:n*4+3,session_id:'s',run_id:id,created_at,kind:'agent.text',payload:{channel:'message',text}});
      await pane.apply({id:n*4+4,session_id:'s',run_id:id,created_at,kind:'run.status',payload:{status:'completed'}});
    }
    pane.flushText();await Promise.resolve();
  };
  globalThis.counts=()=>({runs:pane.runs.size,tools:pane.tools.size,slots:pane.slots?.size||0,
    dom_nodes:pane.root.querySelectorAll('*').length,
    seen:[...pane.runs.values()].reduce((sum,run)=>sum+run.seen.size,0),
    source_characters:[...pane.runs.values()].reduce((sum,run)=>sum+run.segments.reduce((n,item)=>n+(item.text?.length||0),0),0)});
`;

async function main(){
  const server=http.createServer((request,response)=>{
    const mode=request.url.includes('before')?'before':'after';
    let page=source(mode,'index.html').replace(/<script[^>]*>[\s\S]*?<\/script>|<link[^>]*>/g,'');
    page=page.replace('</head>','<style>'+['style.css','teams.css'].map(file=>source(mode,file)).join('\n')+'</style></head>');
    response.setHeader('Content-Type','text/html;charset=utf-8');response.end(page);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const address=server.address().port;
  const debugServer=http.createServer();
  await new Promise(resolve=>debugServer.listen(0,'127.0.0.1',resolve));
  const debugPort=debugServer.address().port;
  await new Promise(resolve=>debugServer.close(resolve));
  const chrome=spawn(chromium,['--headless=new','--no-first-run','--disable-gpu','--disable-background-networking',
    `--remote-debugging-port=${debugPort}`,`--user-data-dir=${path.join(output,'chromium-profile')}`,'about:blank'],
    {windowsHide:true,stdio:'ignore'});
  let socket;
  try{
    let target;
    for(let n=0;n<100&&!target;n++){
      try{target=(await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()).find(item=>item.type==='page');}catch{}
      if(!target)await sleep(100);
    }
    assert.ok(target,'Chromium未启动');
    socket=new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve);socket.addEventListener('error',reject);});
    let sequence=0;const pending=new Map(),errors=[];
    socket.addEventListener('message',event=>{
      const message=JSON.parse(event.data);
      if(message.id){const task=pending.get(message.id);pending.delete(message.id);message.error?task.reject(Error(JSON.stringify(message.error))):task.resolve(message.result);}
      else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails);
    });
    const send=(method,params={})=>new Promise((resolve,reject)=>{pending.set(++sequence,{resolve,reject});socket.send(JSON.stringify({id:sequence,method,params}));});
    const evaluate=async expression=>{
      const result=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
      if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));return result.result.value;
    };
    await send('Runtime.enable');await send('Page.enable');await send('Performance.enable');
    await send('Emulation.setDeviceMetricsOverride',{width:1920,height:1080,deviceScaleFactor:1,mobile:false});
    const heap=async()=>{await send('HeapProfiler.collectGarbage');return (await send('Runtime.getHeapUsage')).usedSize;};
    const load=async mode=>{
      await send('Page.navigate',{url:`http://127.0.0.1:${address}/${mode}?sample=${sequence}`});
      for(let n=0;n<100;n++){if(await evaluate('document.readyState')==='complete')break;await sleep(20);}
      for(const file of ['vendor/markdown-it.umd.min.js','app.js','team-conversation.js']){
        let code=source(mode,file);if(file==='app.js')code=code.replace(/\ninit\(\);\s*$/,'\n');await evaluate(code);
      }
      await evaluate(setup);
    };
    const commits=JSON.parse(fs.readFileSync(path.join(output,'commits.json'),'utf8'));
    const results={commits,browser:await send('Browser.getVersion'),samples:{before:[],after:[]},order:['after','before','before','after','after','before']};
    for(const mode of results.order){
      console.log(`browser ${results.samples.before.length+results.samples.after.length+1}/6: ${mode}`);
      let history=[],timer=null;
      if(!streamOnly){
      await load(mode);const baseline=await heap();
      for(const [start,end] of [[0,200],[200,400]]){
        const elapsed=await evaluate(`(async()=>{const start=performance.now();await addRuns(${start},${end});return performance.now()-start;})()`);
        history.push({...await evaluate('counts()'),total_rounds:end,add_200_rounds_ms:elapsed,heap_bytes:await heap(),baseline_heap_bytes:baseline});
      }
      await evaluate(`pane.destroy();pane=makePane();addRuns(0,50)`);
      await evaluate(`
        pane.ensureRun('active',{id:'active',status:'running',created_at:new Date().toISOString()});
        for(let n=0;n<50;n++)pane.questions.set('q'+n,{id:'q'+n,run_id:'r'+n,status:'answered'});
        pane.update();
        globalThis.scanCounts={run_iterations:0,question_iterations:0,update_calls:0,update_ms:0};
        const runValues=pane.runs.values.bind(pane.runs),questionValues=pane.questions.values.bind(pane.questions);
        pane.runs.values=function*(){for(const row of runValues()){scanCounts.run_iterations++;yield row;}};
        pane.questions.values=function*(){for(const row of questionValues()){scanCounts.question_iterations++;yield row;}};
        const update=pane.update.bind(pane);pane.update=()=>{const start=performance.now();scanCounts.update_calls++;update();scanCounts.update_ms+=performance.now()-start;};
      `);
      await sleep(2200);
      timer=await evaluate('({...scanCounts})');
      await evaluate(`if(pane.setVisible)pane.setVisible(false);else pane.root.hidden=true;
        scanCounts.run_iterations=scanCounts.question_iterations=scanCounts.update_calls=scanCounts.update_ms=0;`);
      await sleep(2200);
      timer.hidden=await evaluate('({...scanCounts})');
      }
      const stream={};
      for(const team of [false,true]){
        await load(mode);
        stream[team?'team':'main']=await evaluate(`(async()=>{
          const nativeTimeout=setTimeout,nativeClear=clearTimeout,nativeRAF=requestAnimationFrame,nativeCancel=cancelAnimationFrame;
          let now=0,sequence=0;const jobs=new Map();
          setTimeout=(callback,delay)=>{jobs.set(++sequence,{callback,due:now+delay});return sequence;};clearTimeout=id=>jobs.delete(id);
          requestAnimationFrame=callback=>setTimeout(callback,16);cancelAnimationFrame=clearTimeout;
          const advance=ms=>{const end=now+ms;while(true){const next=[...jobs].sort((a,b)=>a[1].due-b[1].due)[0];if(!next||next[1].due>end)break;jobs.delete(next[0]);now=next[1].due;next[1].callback();}now=end;};
          const actual=renderMarkdown;let calls=0,parseMs=0,characters=0,maxParseMs=0;
          renderMarkdown=(element,text)=>{const start=performance.now();actual(element,text);const elapsed=performance.now()-start;parseMs+=elapsed;maxParseMs=Math.max(maxParseMs,elapsed);calls++;characters+=text.length;};
          const team=${team};
          if(!team){document.querySelector('.conversation').hidden=false;document.querySelector('#team-workspace').hidden=true;state.context={sessionId:'s',projectId:'p'};state.runRecords.set('stream',{id:'stream',status:'running'});ensureRun('stream');}
          for(let id=1;id<=1000;id++){
            const row={id,session_id:'s',run_id:'stream',kind:'agent.text',payload:{channel:'message',text:'paragraph '+id+' '+('content ').repeat(29)+String.fromCharCode(10,10)}};
            if(team)await pane.apply(row);else appendText(row,state.runs.get('stream'));advance(20);
          }
          const tail={id:1001,session_id:'s',run_id:'stream',kind:'agent.text',payload:{channel:'message',text:'**END**'}};
          const finalStart=performance.now();
          if(team){await pane.apply(tail);await pane.apply({id:1002,session_id:'s',run_id:'stream',kind:'run.status',payload:{status:'completed'}});}
          else{appendText(tail,state.runs.get('stream'));sealText('stream');}
          const finalMs=performance.now()-finalStart;
          const body=team?pane.runs.get('stream').segments[0].body:[...state.textBlocks.values()][0].body;
          void (team?pane.root:state.runs.get('stream')).offsetHeight;
          const finalWithLayoutMs=performance.now()-finalStart;
          const complete=body.innerHTML.includes('<strong>END</strong>');
          setTimeout=nativeTimeout;clearTimeout=nativeClear;requestAnimationFrame=nativeRAF;cancelAnimationFrame=nativeCancel;
          return {calls,parse_ms:parseMs,characters,max_parse_ms:maxParseMs,final_ms:finalMs,final_with_layout_ms:finalWithLayoutMs,complete};
        })()`);
        assert.equal(stream[team?'team':'main'].complete,true);
      }
      if(streamOnly){
        results.samples[mode].push({stream});
        fs.writeFileSync(path.join(output,'browser-stream-layout.json'),JSON.stringify(results,null,2));
        continue;
      }
      // 阻塞消费器只替换为可控边界；工作区队列、预算和断流仍使用原代码。
      await load(mode);
      await evaluate(`globalThis.TeamsUI={init(){}};globalThis.EmployeeSelect={icon:()=>document.createElement('span')};
        globalThis.EventSource=class{constructor(){this.listeners={};globalThis.fixtureSource=this;}addEventListener(name,handler){this.listeners[name]=handler;}close(){this.closed=true;}emit(row){this.listeners.lab({data:JSON.stringify(row)});}};`);
      await evaluate(source(mode,'team-workspace.js').replace('let workspace;','let workspace;globalThis.getBenchmarkWorkspace=()=>workspace;'));
      const queueBaseline=await heap();
      const queue=await evaluate(`(async()=>{
        const ws=getBenchmarkWorkspace();ws.project={id:'p',team:{leader_id:'e'}};
        ws.roster={leader_session_id:'leader',members:[member],sessions:[{id:'s',employee_id:'e',status:'running'}]};
        globalThis.blocked=new Promise(resolve=>globalThis.releaseConsumer=resolve);
        ws.panes=[{session:{id:'s'},events:blocked,ready:Promise.resolve(),receive:()=>blocked}];ws.subscribe();
        const original=fixtureSource;globalThis.delivered=0;
        for(let id=1;id<=5000;id++){
          if(original.closed)break;
          original.emit({id,session_id:'s',run_id:'active',kind:'agent.text',payload:{channel:'message',text:id+':'+('event data ').repeat(600)}});
          delivered++;await ws.chain;
        }
        return {offered:5000,delivered,retained_deliveries:ws.deliveries.size,pending:ws.pendingEvents??null,disconnected:Boolean(original.closed)};
      })()`);
      queue.heap_delta_bytes=(await heap())-queueBaseline;
      await evaluate('getBenchmarkWorkspace().generation++;clearTimeout(getBenchmarkWorkspace().reconnect);releaseConsumer();');
      results.samples[mode].push({history,timer,stream,queue});
      fs.writeFileSync(path.join(output,'browser.json'),JSON.stringify(results,null,2));
    }
    assert.deepEqual(errors,[]);
    console.log(path.join(output,streamOnly?'browser-stream-layout.json':'browser.json'));
  }finally{socket?.close();chrome.kill();await new Promise(resolve=>server.close(resolve));}
}
main().then(()=>process.exit(0),error=>{console.error(error);process.exit(1);});
