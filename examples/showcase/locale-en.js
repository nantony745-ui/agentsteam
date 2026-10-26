/* Showcase-only DOM translation. The product does not ship native English i18n. */
'use strict';
(() => {
  const phrases = {
    '职责范围、工作准则与持续学习的方向':'Responsibilities, working principles and continued learning',
    '未设置的选项继承全局配置，保存后用于新会话。':'Unset options inherit global configuration and apply to new conversations after saving.',
    '在以下目录维护技能包，修改后新会话生效。':'Maintain skill packages in this directory. Changes apply to new conversations.',
    '跨项目与会话，持续积累这位队员的工作记忆。':'Build this teammate\'s working memory across projects and conversations.',
    '启用全局长期记忆后可查看此队员的记忆。':'Enable global long-term memory to view this teammate\'s memory.',
    '用于之后开始的新会话':'Applies to new conversations', '使用已保存的配置':'Use saved configuration',
    '使用个人记忆':'Use personal memory', '个人记忆':'Personal memory',
    '已保存的记忆':'Saved memories', '调整沉淀轮数':'Adjust retention interval',
    '返回会话':'Back to conversations', '队员信息':'Teammate profile', '必填':'Required',
    '已关闭':'Disabled', '记忆库':'Memory library', '上一页':'Previous', '下一页':'Next',
    '从已配置的队员中组建团队，在新建项目时直接使用。':'Build reusable teams from configured teammates and apply them to new projects.',
    '团队人数不限。工作区最多同时显示三个对话窗。':'Teams can have any number of members. Show up to three conversations side by side.',
    '创建团队，明确各位成员与队长。':'Create a team with clear members and a lead.',
    '检测配置后，可设置模型与权限。':'Detect configuration to choose models and permissions.',
    '目录与配置发现结果不代表当前会话已加载或连接。':'Discovered configuration does not mean it is loaded or connected.',
    '目录发现结果不代表当前会话已加载或启用。':'Discovery does not mean a skill is loaded or enabled.',
    '选择左侧技能，预览 Markdown 正文。':'Select a skill to preview its Markdown content.',
    '资料与运行配置':'Profiles and runtime configuration',
    '当前信息已保存':'All changes saved', '有未保存修改':'Unsaved changes',
    '新建队长会话':'New lead conversation', '新建会话':'New conversation',
    '专业职责':'Responsibilities', '工作准则':'Working principles', '成长课题':'Learning goals',
    '工作指引':'Working instructions', '团队角色':'Team role', '运行配置':'Runtime configuration',
    '检测配置':'Detect configuration', '队员会话':'Teammate conversations', '个人 Skills':'Personal Skills',
    '已保存团队':'Saved teams', '团队配置':'Team configuration', '选择员工':'Available teammates',
    '已选成员':'Selected members', '团队名称':'Team name', '设为队长':'Set as lead',
    '删除团队':'Delete team', '保存团队':'Save team', '返回工作台':'Back to workbench',
    '团队设置':'Team settings', '长期记忆':'Long-term memory', '最近会话':'Recent conversations',
    '等待选择会话':'Choose a conversation', '未选择会话':'No conversation selected',
    '未选择 Agent':'No Agent selected', '未选择项目':'No project selected', '尚无执行':'No runs yet',
    '实时已连接':'Live connection established', '已连接':'Connected', '空闲':'Idle',
    '队长':'Lead', '队员':'Teammates', '项目':'Projects', '设置':'Settings', '管理':'Management',
    '姓名':'Name', '新增':'Add', '删除':'Delete', '保存':'Save', '放弃修改':'Discard changes',
    '工作中':'Working', '待授权':'Awaiting approval', '排队中':'Queued', '已完成':'Completed',
    '已取消':'Canceled', '运行中':'Running', '待验收':'Awaiting review', '失败':'Failed',
    '思考过程':'Reasoning', '执行命令':'Run command', '读取文件':'Read file', '编辑文件':'Edit file',
    '预览':'Preview', '差异':'Diff', '文件变更':'File changes', '任务变更':'Task changes',
    '工作记录':'Work log', '执行过程':'Activity', '执行记录':'Run history', '执行详情':'Run details',
    '展开':'Expand', '收起':'Collapse', '专注':'Focus', '关闭':'Close', '刷新':'Refresh',
    '发送消息':'Send message', '发送给':'Message', '发送':'Send', '消息输入':'Message',
    '输入消息':'Write a message', '回到底部':'Back to bottom', '本轮':'This round',
    '搜索姓名或角色':'Search name or role', '搜索团队':'Search teams', '搜索队员':'Search teammates',
    '例如：产品研发组':'For example: Product Launch Studio', '技能预览':'Skill preview',
    '技能目录':'Skill directory', '技能信息':'Skill details', '个人技能':'Personal skills',
    '只读预览':'Read-only preview', '只读':'Read-only', '尚未启动':'Not started',
    '连接未启动':'Connection not started', '未报告':'Not reported', '身份快照':'Identity snapshot',
    '新增队员':'Add teammate', '删除队员':'Delete teammate', '新会话':'New conversation',
    '团队':'Teams', '工作台':'Workbench', '状态':'Status', '内容':'Content',
    '配置':'Configuration', '移除':'Remove', '取消':'Cancel', '已选':'Selected',
    '查看':'View', '无个人技能':'No personal skills', '默认':'Default',
    '继续该员工此前任务历史':'Continue teammate history', '单开独立会话':'New independent conversation',
    '未设置角色':'No role set', '资料':'Profile', '模型':'Model', '权限':'Permissions'
  };
  const pairs = Object.entries(phrases).sort((a,b)=>b[0].length-a[0].length);
  function translate(s) {
    if (!/[\u4e00-\u9fff]/.test(s)) return s;
    for (const [from,to] of pairs) s=s.split(from).join(to);
    return s.replace(/(\d+) 位成员/g,'$1 members').replace(/(\d+) 个/g,'$1 items')
      .replace(/每 (\d+) 轮自动沉淀/g,'Auto-retain every $1 rounds')
      .replace(/第 (\d+) 页/g,'Page $1').replace(/(\d+)\s*秒/g,'$1 sec').replace(/(\d+)\s*分钟/g,'$1 min');
  }
  function refresh() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n=walker.nextNode())) {
      if (['SCRIPT','STYLE','TEXTAREA'].includes(n.parentElement?.tagName)) continue;
      const t=translate(n.data); if(t!==n.data)n.data=t;
    }
    for (const el of document.querySelectorAll('[placeholder],[title],[aria-label]')) {
      for (const key of ['placeholder','title','aria-label']) {
        const value=el.getAttribute(key); if(value){const t=translate(value);if(t!==value)el.setAttribute(key,t);}
      }
    }
    document.documentElement.lang='en';
  }
  window.showcaseTranslate=refresh;
  refresh();
  new MutationObserver(refresh).observe(document.body,{subtree:true,childList:true,characterData:true});
})();
