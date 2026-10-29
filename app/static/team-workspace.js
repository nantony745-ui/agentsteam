'use strict';

const TeamWorkspace = (() => {
  const q = selector => document.querySelector(selector);
  class Workspace {
    constructor() {
      this.project = null; this.roster = null; this.panes = []; this.active = null;
      this.source = null; this.cursor = 0; this.generation = 0; this.unread = new Map(); this.chain = Promise.resolve();
      this.focused = null; this.refreshTimer = null; this.reconnect = null; this.leaderRevision = 0;
      this.deliveries = new Map();
      this.pendingEvents = 0;
      this.observer = new ResizeObserver(() => this.layout()); this.observer.observe(q('#team-workspace'));
      q('#team-new-leader').addEventListener('click', () => this.newLeader().catch(error => this.error(error)));
    }
    error(error) {q('#team-workspace-error').textContent = error.message;}
    key() {return `agentsteam.team.layout.${this.project.id}`;}
    save() {try {localStorage.setItem(this.key(), JSON.stringify({leader:this.roster.leader_session_id, sessions:this.panes.map(pane => pane.session.id), active:this.active?.session.id, focused:this.focused?.session.id}));} catch {}}
    saved() {try {return JSON.parse(localStorage.getItem(this.key())) || {};} catch {return {};}}
    async openProject(project) {
      if (project?.id === this.project?.id && project?.team) return;
      const version = ++this.generation;
      this.source?.close(); this.source = null; clearTimeout(this.reconnect); clearTimeout(this.refreshTimer);
      this.reconnect = this.refreshTimer = null; this.chain = Promise.resolve();
      this.deliveries.clear(); this.pendingEvents = 0; this.tabSignature = null;
      for (const pane of this.panes) pane.destroy();
      this.panes = []; this.active = null; this.focused = null; this.roster = null; this.unread.clear(); this.project = project?.team ? project : null;
      q('#team-workspace').hidden = !this.project;
      q('.conversation').hidden = Boolean(this.project);
      q('.app-shell').classList.toggle('team-mode', Boolean(this.project));
      q('#team-workspace-error').textContent = '';
      if (!this.project) return;
      q('#team-workspace-name').textContent = project.team.name;
      q('#team-workspace-status').textContent = '正在加载团队…';
      try {
        const sessions = await api(`/api/projects/${project.id}/sessions`);
        if (version !== this.generation) return;
        const saved = this.saved();
        const leaders = sessions.filter(item => !item.parent_id && item.employee_id === project.team.leader_id);
        const leader = leaders.find(item => item.id === saved.leader) || leaders[0];
        if (leader) await this.loadLeader(leader.id, version, saved);
        else await this.newLeader();
      } catch (error) {if (version === this.generation) this.error(error);}
    }
    async newLeader() {
      if (!this.project || this.creatingLeader) return;
      this.creatingLeader = true;
      const project = this.project, version = this.generation;
      try {
        const leader = await api('/api/sessions', {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:project.id,employee_id:project.team.leader_id})});
        if (version !== this.generation) return;
        await this.loadLeader(leader.id, version, {}); scheduleSidebarRefresh();
      } finally {this.creatingLeader = false;}
    }
    async loadLeader(id, version, saved) {
      const revision = ++this.leaderRevision;
      const roster = await api(`/api/sessions/${id}/team`);
      if (version !== this.generation || revision !== this.leaderRevision) return;
      this.source?.close(); clearTimeout(this.reconnect); this.reconnect = null;
      this.deliveries.clear(); this.pendingEvents = 0; this.tabSignature = null;
      for (const pane of this.panes) pane.destroy(); this.panes = []; this.active = null; this.focused = null;
      this.roster = roster; this.cursor = roster.high_watermark; this.unread.clear();
      this.subscribe(); this.tabs();
      const ids = (saved.sessions || []).filter(sessionId => roster.sessions.some(item => item.id === sessionId)).slice(0,3);
      if (!ids.length) ids.push(id);
      for (const sessionId of ids) {
        if (version !== this.generation || revision !== this.leaderRevision) return;
        await this.openSession(roster.sessions.find(item => item.id === sessionId));
      }
      this.focused = this.panes.find(pane => pane.session.id === saved.focused) || null;
      this.activate(this.panes.find(pane => pane.session.id === saved.active) || this.panes[0]);
    }
    async refresh() {
      if (!this.roster) return;
      const version = this.generation, leader = this.roster.leader_session_id;
      const roster = await api(`/api/sessions/${leader}/team`);
      if (version !== this.generation || leader !== this.roster?.leader_session_id) return;
      this.roster = roster; this.tabs();
      for (const pane of this.panes) {
        pane.member = roster.members.find(member => member.profile.employee_id === pane.session.employee_id);
        pane.update();
      }
    }
    scheduleRefresh() {
      if (this.refreshTimer) return;
      this.refreshTimer = setTimeout(() => {this.refreshTimer = null; this.refresh().catch(error => this.error(error));}, 200);
    }
    subscribe() {
      const version = this.generation, leader = this.roster.leader_session_id;
      const query = new URLSearchParams({project_id:this.project.id, after_event_id:String(this.cursor)});
      const source = this.source = new EventSource(`/api/stream?${query}`);
      source.addEventListener('open', () => {if (this.source === source) q('#team-workspace-status').textContent = `${this.roster.members.length} 位成员 · 已连接`;});
      source.addEventListener('lab', event => {
        if (version !== this.generation || this.source !== source) return;
        let row;
        try {row = JSON.parse(event.data);} catch (error) {this.error(error); source.close(); return;}
        if (this.pendingEvents >= LIVE_EVENT_LIMIT) {this.disconnect(source); return;}
        this.pendingEvents += 1;
        this.chain = this.chain.then(async () => {
          if (version !== this.generation || this.source !== source || leader !== this.roster?.leader_session_id) return;
          if (row.id <= this.cursor || this.deliveries.has(row.id)) {this.pendingEvents -= 1; return;}
          if (row.kind === 'session.deleted') {
            for (const pane of [...this.panes]) if (row.payload.session_ids.includes(pane.session.id)) this.closePane(pane);
            if (row.payload.session_ids.includes(leader)) {
              this.source.close(); this.source = null; this.error(new Error('队长会话已删除，请新建队长会话')); return;
            }
          }
          const delivery = {done:false}; this.deliveries.set(row.id, delivery);
          Promise.all(this.panes.filter(pane => pane.session.id === row.session_id).map(pane => pane.receive(row))).then(() => {
            if (version !== this.generation || this.source !== source) return;
            delivery.done = true;
            for (const [id, item] of this.deliveries) {
              if (!item.done) break;
              this.cursor = id; this.deliveries.delete(id); this.pendingEvents -= 1;
            }
          }).catch(error => {
            if (version !== this.generation || this.source !== source) return;
            this.error(error); this.disconnect(source);
          });
          if (version !== this.generation || leader !== this.roster?.leader_session_id) return;
          const session = this.roster.sessions.find(item => item.id === row.session_id);
          if (session) {
            if (row.kind === 'run.status') session.status = row.payload.status;
            if (row.kind === 'agent.text' && this.active?.session.id !== row.session_id) {
              if (!this.unread.has(session.employee_id)) this.unread.set(session.employee_id,new Set());
              this.unread.get(session.employee_id).add(`${row.session_id}:${row.run_id}`);
            }
          }
          if (['task.created','task.status','user.message','run.status','session.deleted'].includes(row.kind)) this.scheduleRefresh();
          this.tabs();
        }).catch(error => {
          if (version !== this.generation || this.source !== source) return;
          this.error(error); this.disconnect(source);
        });
      });
      source.addEventListener('error', () => {if (this.source === source) this.disconnect(source);});
    }
    disconnect(source) {
      source.close(); this.source = null;
      this.deliveries.clear(); this.pendingEvents = 0;
      q('#team-workspace-status').textContent = '连接中断，正在补拉…';
      const version = this.generation, leader = this.roster?.leader_session_id;
      const drained = Promise.all(this.panes.map(pane => Promise.all([pane.events, pane.ready])));
      if (!this.reconnect) this.reconnect = setTimeout(() => {
        drained.then(() => {
          if (version !== this.generation || leader !== this.roster?.leader_session_id) return;
          this.reconnect = null;
          if (!this.source) this.subscribe();
        });
      }, RECONNECT_DELAY_MS);
    }
    tabs() {
      if (!this.roster) return;
      const signature = JSON.stringify([this.active?.member.profile.employee_id,
        this.roster.members, this.roster.sessions.map(item => [item.id,item.employee_id,item.status]),
        [...this.unread].map(([id, items]) => [id,items.size])]);
      if (signature === this.tabSignature) return;
      this.tabSignature = signature;
      const tabs = q('#team-member-tabs'); tabs.replaceChildren();
      for (const member of this.roster.members) {
        if (member.state === 'deleted') continue;
        const profile = member.profile, sessions = this.roster.sessions.filter(item => item.employee_id === profile.employee_id);
        const button = document.createElement('button'); button.type = 'button'; button.className = 'team-member-tab';
        button.setAttribute('aria-pressed', String(this.active?.member.profile.employee_id === profile.employee_id));
        button.append(EmployeeSelect.icon(profile.harness), document.createTextNode(`${profile.name}${profile.employee_id === this.project.team.leader_id ? ' ♛' : ''}`));
        const states = sessions.map(item => item.status);
        const status = document.createElement('span'); status.className = 'team-member-state';
        status.textContent = states.includes('waiting_permission') ? '待授权' : states.includes('running') ? '工作中' : states.includes('queued') ? '排队中' : '空闲'; button.append(status);
        if (this.unread.has(profile.employee_id)) {const badge = document.createElement('span'); badge.className = 'team-member-unread'; badge.textContent = this.unread.get(profile.employee_id).size; button.append(badge);}
        button.addEventListener('click', () => {
          const pane = this.panes.find(item => item.member.profile.employee_id === profile.employee_id);
          const task = pane ? this.activate(pane) : sessions.length ? this.openSession(sessions.at(-1)) : this.newMember(profile.employee_id);
          Promise.resolve(task).catch(error => this.error(error));
        }); tabs.append(button);
      }
      if (this.roster.members.some(member => member.state === 'deleted')) {
        const notice = document.createElement('span'); notice.className = 'team-member-state';
        notice.textContent = '有队员已被删除'; tabs.append(notice);
      }
    }
    async newMember(employeeId, replacing) {
      if (!this.roster) return;
      if (employeeId === this.project.team.leader_id) return this.newLeader();
      const version = this.generation, leader = this.roster.leader_session_id;
      const session = await api('/api/sessions', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project_id:this.project.id,employee_id:employeeId,leader_session_id:leader})});
      if (version !== this.generation || leader !== this.roster?.leader_session_id) return;
      await this.refresh(); await this.openSession(session,replacing); scheduleSidebarRefresh();
    }
    async openSession(session, replacing) {
      if (!session || !this.project || session.project_id !== this.project.id) return;
      const leader = session.parent_id || session.id;
      if (!this.roster || this.roster.leader_session_id !== leader) {
        await this.loadLeader(leader, this.generation, {sessions:[session.id]}); return;
      }
      const existing = this.panes.find(item => item.session.id === session.id);
      if (existing) {this.activate(existing); return;}
      if (!this.roster.sessions.some(item => item.id === session.id)) await this.refresh();
      if (leader !== this.roster?.leader_session_id || session.project_id !== this.project?.id) return;
      const member = this.roster.members.find(item => item.profile.employee_id === session.employee_id);
      if (!member) return;
      const target = replacing || (this.panes.length === 3 ? this.active || this.panes.at(-1) : null);
      const index = target ? this.panes.indexOf(target) : this.panes.length;
      if (target) {target.destroy(); this.panes.splice(index,1);}
      const pane = new TeamConversation(session,member,this); this.panes.splice(index,0,pane);
      for (const item of this.panes) q('#team-pane-grid').append(item.root);
      this.focused = null; this.activate(pane); this.layout(); this.save();
    }
    activate(pane) {
      if (!pane) return;
      this.active = pane;
      if (this.focused) this.focused = pane;
      const unread = this.unread.get(pane.member.profile.employee_id);
      if (unread) {
        for (const item of unread) if (item.startsWith(`${pane.session.id}:`)) unread.delete(item);
        if (!unread.size) this.unread.delete(pane.member.profile.employee_id);
      }
      for (const item of this.panes) item.root.dataset.active = String(item === pane);
      this.layout(); this.tabs(); this.save();
    }
    closePane(pane) {
      pane.destroy(); this.panes = this.panes.filter(item => item !== pane); this.focused = null;
      if (this.active === pane) this.active = this.panes[0] || null;
      this.layout(); this.tabs(); this.save();
    }
    focus(pane) {this.focused = this.focused === pane ? null : pane; this.activate(pane);}
    layout() {
      if (!this.project) return;
      const width = q('#team-workspace').clientWidth, capacity = width >= 1200 ? 3 : width >= 800 ? 2 : 1;
      let shown = this.focused ? [this.focused] : this.panes.slice(0,capacity);
      if (this.active && !shown.includes(this.active)) shown = [...shown.slice(0,Math.max(0,capacity-1)),this.active];
      if (this.focused) shown = [this.focused];
      for (const pane of this.panes) pane.root.hidden = !shown.includes(pane);
      q('#team-pane-grid').style.setProperty('--team-columns',Math.max(1,shown.length));
    }
    file(title,content) {
      const dialog = document.createElement('dialog'); dialog.className = 'team-file-dialog';
      const heading = document.createElement('h3'); heading.textContent = title;
      const pre = document.createElement('pre'); pre.textContent = content || '';
      const close = document.createElement('button'); close.type = 'button'; close.textContent = '关闭'; close.addEventListener('click',()=>dialog.close());
      dialog.append(heading,close,pre); document.body.append(dialog); dialog.addEventListener('close',()=>dialog.remove()); dialog.showModal();
    }
  }
  let workspace;
  function init() {TeamsUI.init(); workspace = new Workspace(); window.addEventListener('beforeunload',()=>workspace.source?.close());}
  init();
  return {
    openProject:project => {
      if (project?.team && project.id === workspace.project?.id) return workspace.ready;
      workspace.ready = workspace.openProject(project); return workspace.ready;
    },
    openSession:async session => {await workspace.ready; return workspace.openSession(session);},
    newLeader:async () => {await workspace.ready; return workspace.newLeader();},
    openMember:async employeeId => {await workspace.ready; return workspace.newMember(employeeId);},
    refresh:() => workspace.refresh(),
  };
})();
